"""C-26.4: the guardian relay applies numbered stdin frames once, in order."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest import mock

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet import relay as relay_module
from subfleet.relay import LOG_INLINE_MAX, REPLY_TIMEOUT_S, RelayServer, line_sha256, read_log, socket_path


@pytest.fixture
def server(tmp_path):
    read_end, write_end = os.pipe()
    relay = RelayServer(tmp_path / "run" / "x.sock", tmp_path / "stdin.jsonl")
    relay._pipe = write_end
    yield relay, read_end
    os.close(read_end)
    relay._close_pipe()


def F(seq, op="write", line=None, tag=None, **extra):
    frame = {"seq": seq, "op": op, "tag": tag, "sha256": line_sha256(line)}
    if line is not None:
        frame["line"] = line
    return {**frame, **extra}


def _drain(fd: int) -> bytes:
    os.set_blocking(fd, False)
    try:
        return os.read(fd, 1 << 20)
    except BlockingIOError:
        return b""


def test_frames_apply_once_in_order(server):
    """C-26.4 a frame is written once; a resent number is acknowledged as a duplicate
    and not written again; a gap is refused."""
    relay, read_end = server
    assert relay.apply(F(1, line='{"a":1}', tag="init")) == {"seq": 1, "ok": True}
    assert relay.apply(F(1, line='{"a":1}', tag="init"))["dup"] is True
    assert relay.apply(F(3, line="x"))["error"] == "gap"
    assert relay.apply(F(2, line='{"b":2}', tag="user-message"))["ok"]
    assert _drain(read_end) == b'{"a":1}\n{"b":2}\n'


def test_log_is_written_before_the_pipe_and_recovers_the_count(server, tmp_path):
    """C-26.4 each applied frame is in `stdin.jsonl`; a new server resumes numbering
    from the log and never re-applies what it records."""
    relay, _ = server
    relay.apply(F(1, line="one", tag="init"))
    relay.apply(F(2, line="two", tag="user-message"))
    records = read_log(tmp_path / "stdin.jsonl")
    assert [(r["seq"], r["tag"], r["line"], r["status"]) for r in records] == [
        (1, "init", "one", "written"), (2, "user-message", "two", "written")]
    again = RelayServer(tmp_path / "run" / "x.sock", tmp_path / "stdin.jsonl")
    assert again.last_applied == 2
    read_end, write_end = os.pipe()
    again._pipe = write_end
    assert again.apply(F(2, line="two"))["dup"] is True
    assert again.apply(F(3, line="three"))["ok"]
    assert _drain(read_end) == b"three\n"
    os.close(read_end)
    again._close_pipe()


def test_large_lines_are_logged_by_digest(server, tmp_path):
    """C-26.4 a frame larger than the inline limit is delivered whole and logged by
    SHA-256 and size, so the log never duplicates an image attachment."""
    relay, read_end = server
    big = "x" * (LOG_INLINE_MAX + 10)
    os.set_blocking(read_end, False)
    chunks = []
    import threading
    done = threading.Event()

    def reader():
        os.set_blocking(read_end, True)
        total = 0
        while total < len(big) + 1:
            data = os.read(read_end, 1 << 16)
            chunks.append(data)
            total += len(data)
        done.set()

    thread = threading.Thread(target=reader)
    thread.start()
    assert relay.apply(F(1, line=big, tag="user-message"))["ok"]
    done.wait(5)
    thread.join(5)
    assert b"".join(chunks) == big.encode() + b"\n"
    record = read_log(tmp_path / "stdin.jsonl")[0]
    assert "line" not in record and record["bytes"] == len(big) and record["sha256"] == line_sha256(big)


def test_close_ends_input_and_later_frames_are_refused(server):
    """C-26.4 `close` closes the child's stdin once; a later write is refused as closed."""
    relay, read_end = server
    assert relay.apply(F(1, "close", tag="end"))["ok"]
    assert os.read(read_end, 10) == b""
    assert relay.apply(F(2, line="late"))["error"] == "closed"


def test_malformed_frames_are_refused_without_logging(server, tmp_path):
    """C-26.4 malformed frames, and a line containing a newline, change nothing."""
    relay, _ = server
    assert relay.apply(F("1", line="a"))["error"] == "bad-frame"
    assert relay.apply(F(1, "shout", line="a"))["error"] == "bad-frame"
    assert relay.apply(F(1, line="a\nb"))["error"] == "newline-in-line"
    assert relay.apply(F(1, line="a\rb"))["error"] == "newline-in-line"
    assert relay.apply({**F(1, line="a"), "sha256": "0" * 64})["error"] == "bad-hash"
    assert not (tmp_path / "stdin.jsonl").exists()


def test_torn_log_tail_is_not_counted(tmp_path):
    """C-26.4 a crash mid-append leaves a line without its newline: that frame was
    never written to the pipe, so it does not count as applied."""
    log = tmp_path / "stdin.jsonl"
    log.write_text(json.dumps({"kind": "intent", "seq": 1, "op": "write", "tag": "a", "line": "x", "sha256": line_sha256("x")})
                   + "\n" + json.dumps({"kind": "written", "seq": 1}) + "\n" + '{"kind": "intent", "seq": 2, "op"')
    assert [(r["seq"], r["status"]) for r in read_log(log)] == [(1, "written")]


def test_socket_path_fits_af_unix(tmp_path):
    """C-26.4 the relay socket path stays under the 103-byte AF_UNIX limit for the
    real state root and a maximal attempt id."""
    path = socket_path("/Users/maxghenis/.subfleet", "20260924-081504-" + "x" * 40 + "/a3")
    assert len(str(path).encode()) < 103


def test_a_resent_number_with_other_content_is_a_conflict(server):
    """C-26.4 a duplicate is matched by content, not number alone: another line under an
    applied number is refused, never acknowledged as already done."""
    relay, read_end = server
    assert relay.apply(F(1, line="deny", tag="approval:r"))["ok"]
    assert relay.apply(F(1, line="allow", tag="approval:r"))["error"] == "conflict"
    assert _drain(read_end) == b"deny\n"


def test_a_failed_write_is_never_reported_as_applied(tmp_path):
    """C-26.4 a frame whose pipe write failed is logged as failed; resending it is
    answered `failed`, not `dup`, and nothing more is written."""
    read_end, write_end = os.pipe()
    relay = RelayServer(tmp_path / "run" / "x.sock", tmp_path / "stdin.jsonl")
    relay._pipe = write_end
    os.close(read_end)                               # the provider is gone
    assert relay.apply(F(1, line="hello", tag="user-message"))["error"] == "failed"
    assert relay.apply(F(1, line="hello", tag="user-message"))["error"] == "failed"
    assert relay.apply(F(2, line="more"))["error"] == "closed"
    assert read_log(tmp_path / "stdin.jsonl")[0]["status"] == "failed"
    relay._close_pipe()


def test_an_interrupted_write_is_pending_after_a_restart(tmp_path):
    """C-26.4 an intent with no outcome (the guardian died mid-write) makes a new relay
    refuse everything: the provider may have read part of the frame."""
    log = tmp_path / "stdin.jsonl"
    log.write_text(json.dumps({"kind": "intent", "seq": 1, "op": "write", "tag": "u", "line": "x",
                               "sha256": line_sha256("x")}) + "\n")
    relay = RelayServer(tmp_path / "run" / "x.sock", log)
    read_end, write_end = os.pipe()
    relay._pipe = write_end
    assert relay.apply(F(1, line="x"))["error"] == "failed"
    assert relay.apply(F(2, line="y"))["error"] == "closed"
    os.close(read_end)
    relay._close_pipe()


def test_status_reports_what_was_applied_and_the_cap_without_logging(server, tmp_path):
    """C-26.4, IR-27: `status` is not a frame: it carries no number, is never logged, and
    answers the version, the frame cap, the frames applied and whether stdin is closed."""
    from subfleet.relay import FRAME_MAX, RELAY_VERSION
    relay, _ = server
    assert relay.apply({"op": "status"}) == {"ok": True, "status": {
        "version": RELAY_VERSION, "frame_max": FRAME_MAX, "applied": 0, "closed": False, "child": "none"}}
    relay.apply(F(1, line="one", tag="init"))
    relay.apply(F(2, line="two", tag="user-message"))
    assert relay.apply({"op": "status"})["status"]["applied"] == 2
    assert relay.apply(F(3, "close", tag="close"))["ok"]
    status = relay.apply({"op": "status"})["status"]
    assert status["applied"] == 3 and status["closed"] is True
    assert [r["seq"] for r in read_log(tmp_path / "stdin.jsonl")] == [1, 2, 3]
    # A numbered "status" is a malformed frame, not a status request.
    assert relay.apply({"seq": 4, "op": "status", "sha256": line_sha256(None)})["error"] == "bad-frame"


@pytest.mark.parametrize("op", ["close", "status"])
def test_stop_finishes_the_exchange_already_read(server, monkeypatch, op):
    """C-26.4: child exit between apply and reply cannot discard the answer."""
    import socket
    import threading

    relay, read_end = server
    peer, conn = socket.socketpair()
    peer.settimeout(5)
    applied = threading.Event()
    shutdown_finished = threading.Event()
    real_apply = relay.apply

    class ObservedConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        def shutdown(self, how):
            conn.shutdown(how)
            shutdown_finished.set()

    observed = ObservedConnection()

    def apply_then_child_exits(frame):
        answer = real_apply(frame)
        applied.set()
        assert shutdown_finished.wait(5), "guardian did not shut down the read half"
        return answer

    monkeypatch.setattr(relay, "apply", apply_then_child_exits)
    def serve_connection():
        # The real accept loop also handles a peer/stop closing the stream.
        try:
            relay._serve_connection(observed)
        except OSError:
            pass

    relay._conn = observed
    relay._thread = threading.Thread(target=serve_connection)
    relay._thread.start()
    frame = F(1, "close") if op == "close" else {"op": "status"}
    try:
        peer.sendall((json.dumps(frame) + "\n").encode())
        assert applied.wait(5), "relay did not apply the request"
        relay.stop()
        with peer.makefile("rb") as reader:
            raw = reader.readline()
        assert raw, "child exit discarded a request the relay had already read"
        answer = json.loads(raw)
        assert answer["ok"]
        if op == "close":
            assert answer["seq"] == 1 and os.read(read_end, 1) == b""
            assert read_log(relay.log_path)[0]["status"] == "written"
        else:
            assert answer["status"]["applied"] == 0
            assert not relay.log_path.exists()
        assert not relay._thread.is_alive()
    finally:
        shutdown_finished.set()
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        peer.close()
        relay._thread.join(5)
        conn.close()


def test_stop_covers_a_connection_accepted_before_it_was_published(server, monkeypatch):
    """Stop during peer checking must not leave an unregistered reader blocked."""
    import socket
    import threading

    relay, _ = server
    peer, conn = socket.socketpair()
    checking = threading.Event()
    publish = threading.Event()

    class Accepter(threading.Thread):
        def join(self, timeout=None):
            # stop has already inspected _conn by this point. Publishing after
            # that inspection used to escape the shutdown wakeup entirely.
            publish.set()
            super().join(timeout)

    class Listener:
        def accept(self):
            return conn, None

        def close(self):
            pass

    def allowed(_pid):
        checking.set()
        assert publish.wait(5)
        return True

    relay._server = Listener()
    relay._allowed_peer = allowed
    relay._thread = Accepter(target=relay._accept_loop)
    relay._thread.start()
    try:
        assert checking.wait(5)
        relay.stop()
        assert not relay._thread.is_alive(), "accepted connection escaped stop's wakeup"
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        peer.close()
        relay._thread.join(5)
        conn.close()


# #230 review r1: a frame far over any pipe buffer (64 KiB by default; at most
# 1 MiB on Linux), so its write waits for a reader once the buffer is full.
LINE_2MIB = "x" * (2 << 20)


def _relay_on_a_socket(log_path: Path, *, exited_child: bool = False):
    """A real relay as the guardian runs it (`guardian.run_guardian`): bound on a
    socket, serving the write end of a stdin pipe. The read end is returned, held
    by the test. With `exited_child`, a child was first started on that pipe as its
    stdin and has exited, and the relay serves with it as its child."""
    short = Path(tempfile.mkdtemp(prefix="sfrb-", dir="/tmp"))        # AF_UNIX paths stay short
    read_end, write_end = os.pipe()
    child = None
    if exited_child:
        child = subprocess.Popen([sys.executable, "-c", ""], stdin=read_end)
        assert child.wait(30) == 0
    relay = RelayServer(short / "r.sock", log_path)
    relay.bind()
    relay.serve(write_end, child=child)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10)
    client.connect(str(relay.path))
    return relay, client, types.SimpleNamespace(fd=read_end), short


def _release(relay, client, held, short) -> None:
    if held.fd is not None:
        os.close(held.fd)                    # what ended a held write at d72597b5c (EPIPE)
        held.fd = None
    relay.stop()                             # idempotent: closes what a failed test left open
    client.close()
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def exited_child(tmp_path):
    """#230 review r1's input: the relay feeds the stdin pipe of a child that has
    exited, while the pipe's read end stays open and unread (the test holds it, as
    a background process the provider started and that inherited stdin would)."""
    made = _relay_on_a_socket(tmp_path / "stdin.jsonl", exited_child=True)
    try:
        yield made[:3]
    finally:
        _release(*made)


def _send_and_wait_for_intent(relay, client, line: str) -> None:
    """Send frame 1 and return once the relay has logged its intent: it is then
    inside the exchange, writing to the pipe."""
    client.sendall((json.dumps(F(1, line=line, tag="user-message")) + "\n").encode())
    until = time.monotonic() + 10
    while not read_log(relay.log_path):
        assert time.monotonic() < until, "the relay did not take the frame"
        time.sleep(0.01)


def _stop_within(relay, held, bound: float) -> bool:
    """Run `stop` and give it `bound` seconds. One still blocked then is released
    by closing the held read end (EPIPE), so a failing run ends instead of hanging."""
    returned = threading.Event()
    stopper = threading.Thread(target=lambda: (relay.stop(), returned.set()), daemon=True)
    stopper.start()
    if returned.wait(bound):
        return True
    os.close(held.fd)
    held.fd = None
    stopper.join(10)
    return False


def _read_to_eof(fd: int) -> bytes:
    """What is left in the pipe, which must be closed at its write end by now."""
    os.set_blocking(fd, False)
    data = bytearray()
    while True:
        try:
            chunk = os.read(fd, 1 << 16)
        except BlockingIOError:
            raise AssertionError("the relay left the pipe's write end open") from None
        if not chunk:
            return bytes(data)
        data.extend(chunk)


def test_stop_cuts_an_exchange_a_retained_stdin_reader_holds(exited_child, caplog):
    """C-26.4 (#230 review r1): a 2 MiB frame waits in the stdin pipe of a child that
    has exited, whose read end is held by a process that never reads it. `stop` gives
    the exchange its bound (`REPLY_TIMEOUT_S`, the reply's own) and then cuts it: it
    returns within that bound plus 1 s, logs one fixed line, leaves no relay thread
    and no socket file, and closes the pipe. The frame is logged `failed` (ECANCELED),
    the reader got only part of it, and the client reads end of stream, never an
    acknowledgement. At d72597b5c, `stop` waited until the reader closed the pipe."""
    relay, client, held = exited_child
    caplog.set_level(logging.WARNING, logger="subfleet.relay")
    _send_and_wait_for_intent(relay, client, LINE_2MIB)
    assert _stop_within(relay, held, REPLY_TIMEOUT_S + 1), "a retained stdin reader held stop past its bound"
    assert not relay._thread.is_alive(), "the relay thread outlived stop"
    assert not relay.path.exists(), "stop left the socket file"
    cut = [record for record in caplog.records if record.name == "subfleet.relay"]
    assert [(record.levelno, record.getMessage()) for record in cut] == [(logging.WARNING, relay_module.CUT_LOG_LINE)]
    assert not cut[0].args                                       # fixed text: nothing of the frame
    assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(relay.log_path)] == [
        (1, "failed", errno.ECANCELED)]
    assert client.recv(1 << 16) == b"", "a cut exchange was acknowledged"
    received = _read_to_eof(held.fd)
    whole = (LINE_2MIB + "\n").encode()
    assert 0 < len(received) < len(whole) and whole.startswith(received)


def test_stop_still_answers_an_exchange_that_finishes_inside_its_bound(exited_child, caplog):
    """C-26.4 (kept from #230): the same 2 MiB frame, but the holder of the read end
    drains the pipe as `stop` begins. The exchange finishes inside the bound, so the
    line is written whole and its acknowledgement is sent before `stop` returns, and
    nothing is cut or logged. This passes at d72597b5c too; it pins the behaviour the
    bound keeps, and a cut that does not wait out the bound fails it."""
    relay, client, held = exited_child
    caplog.set_level(logging.WARNING, logger="subfleet.relay")
    whole = (LINE_2MIB + "\n").encode()
    _send_and_wait_for_intent(relay, client, LINE_2MIB)
    go, drained = threading.Event(), bytearray()

    def drain():
        go.wait(10)
        while len(drained) < len(whole):
            chunk = os.read(held.fd, 1 << 16)
            if not chunk:
                return
            drained.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    go.set()
    assert _stop_within(relay, held, REPLY_TIMEOUT_S + 1), "stop did not return within its bound"
    reader.join(10)
    client.setblocking(False)
    try:
        raw = client.recv(1 << 16)
    except BlockingIOError:
        raw = b""
    assert raw.endswith(b"\n"), "the acknowledgement was not sent before stop returned"
    assert json.loads(raw) == {"seq": 1, "ok": True}
    assert bytes(drained) == whole
    assert [(r["seq"], r["status"]) for r in read_log(relay.log_path)] == [(1, "written")]
    assert not [record for record in caplog.records if record.name == "subfleet.relay"]
    assert not relay._thread.is_alive() and not relay.path.exists()


@hypothesis.settings(max_examples=12, deadline=None, derandomize=True, database=None)
@hypothesis.example(size=2 << 20, drain=0)                      # #230 review r1: cut
@hypothesis.example(size=10, drain=0)                           # fits the pipe: written
@hypothesis.given(size=st.integers(0, 3 << 19), drain=st.integers(0, 3 << 19))
def test_an_acknowledged_frame_is_one_the_reader_can_read_whole(size, drain):
    """C-26.4, for every frame size and every amount the holder of the read end
    drains before it stops reading: `stop` returns within its bound plus 1 s and
    leaves no thread, socket file or open write end; the frame is logged `written`
    exactly when the reader can read the whole line, and otherwise `failed`
    (ECANCELED) with the reader holding a strict prefix; an `ok` acknowledgement is
    only ever sent for a `written` frame. The bound is shortened to keep examples
    quick; it is the same code path."""
    line = "y" * size
    whole = (line + "\n").encode()
    with tempfile.TemporaryDirectory(prefix="sfrp-") as scratch, \
            mock.patch.object(relay_module, "REPLY_TIMEOUT_S", 0.25):
        made = _relay_on_a_socket(Path(scratch) / "stdin.jsonl")
        relay, client, held, _ = made
        try:
            got = bytearray()

            def reader():
                while len(got) < drain:
                    chunk = os.read(held.fd, min(1 << 16, drain - len(got)))
                    if not chunk:
                        return
                    got.extend(chunk)

            draining = threading.Thread(target=reader, daemon=True)
            draining.start()
            _send_and_wait_for_intent(relay, client, line)
            assert _stop_within(relay, held, relay_module.REPLY_TIMEOUT_S + 1), "stop past its bound"
            draining.join(10)
            assert not draining.is_alive(), "the reader never reached end of file"
            got.extend(_read_to_eof(held.fd))
            assert not relay._thread.is_alive() and not relay.path.exists()
            records = read_log(relay.log_path)
            assert len(records) == 1 and records[0]["status"] in ("written", "failed")
            hypothesis.event(records[0]["status"])
            if records[0]["status"] == "written":
                assert bytes(got) == whole
            else:
                assert records[0]["errno"] == errno.ECANCELED
                assert len(got) < len(whole) and whole.startswith(bytes(got))
            client.setblocking(False)
            try:
                raw = client.recv(1 << 16)
            except (BlockingIOError, ConnectionResetError):
                raw = b""
            if raw:
                assert json.loads(raw) == {"seq": 1, "ok": True} and records[0]["status"] == "written"
        finally:
            _release(*made)
