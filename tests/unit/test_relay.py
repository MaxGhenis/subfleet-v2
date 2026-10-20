"""C-26.4: the guardian relay applies numbered stdin frames once, in order."""

from __future__ import annotations

import contextlib
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
from subfleet.relay import LOG_INLINE_MAX, REPLY_TIMEOUT_S, RelayServer, frame_sha256, line_sha256, read_log, socket_path
from tests.relay_stalls import OS_POINTS, POINTS, SOCKET_POINTS, Gate, StallingOs, StallingServer, refusing_start


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


@pytest.mark.parametrize("op", ["write", "signal", "close"])
def test_send_clocks_each_frame_after_connecting_and_before_transmission(tmp_path, monkeypatch, op):
    from types import SimpleNamespace
    from subfleet.relay import RelayClient

    client = RelayClient(tmp_path / "unused.sock")
    events = []

    def connect():
        events.append("connect")
        client._sock = SimpleNamespace(sendall=lambda payload: events.append("transmit"))
        client._reader = SimpleNamespace(readline=lambda cap: b'{"seq":1,"ok":true}\n')

    monkeypatch.setattr(client, "_connect", connect)
    assert client.send(1, op, line="stop" if op == "write" else None,
                       sig="INT" if op == "signal" else None,
                       before_send=lambda: events.append("clock")).ok
    assert events == ["connect", "clock", "transmit"]


@pytest.mark.parametrize("failure", ["connect", "oversized"])
def test_send_never_clocks_a_frame_that_cannot_reach_transmission(tmp_path, monkeypatch, failure):
    from subfleet.relay import FrameTooLarge, RelayClient, RelayError

    client = RelayClient(tmp_path / "unused.sock")
    clocks = []

    def unavailable():
        raise RelayError("no connection")

    monkeypatch.setattr(client, "_connect", unavailable)
    if failure == "oversized":
        client.frame_max = 1
    with pytest.raises(FrameTooLarge if failure == "oversized" else RelayError):
        client.send(1, "close", before_send=lambda: clocks.append(True))
    assert clocks == []


def test_a_send_clock_does_not_leak_into_status_or_later_frames(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from subfleet.relay import RelayClient

    client = RelayClient(tmp_path / "unused.sock")
    clocks = []
    client._sock = SimpleNamespace(sendall=lambda payload: None)
    client._reader = SimpleNamespace(readline=lambda cap: b'{"ok":true,"status":{"applied":1}}\n')
    monkeypatch.setattr(client, "_connect", lambda: None)
    assert client.send(1, "close", before_send=lambda: clocks.append(True)).ok
    assert client.status() == {"applied": 1}
    assert client.send(2, "close").ok
    assert clocks == [True]


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


def _relay_on_a_socket(log_path: Path, *, exited_child: bool = False, wrap_server=None):
    """A real relay as the guardian runs it (`guardian.run_guardian`): bound on a
    socket, serving the write end of a stdin pipe. The read end is returned, held
    by the test. With `exited_child`, a child was first started on that pipe as its
    stdin and has exited, and the relay serves with it as its child. `wrap_server`
    replaces the listening socket before the relay serves (a `StallingServer`)."""
    short = Path(tempfile.mkdtemp(prefix="sfrb-", dir="/tmp"))        # AF_UNIX paths stay short
    read_end, write_end = os.pipe()
    child = None
    if exited_child:
        child = subprocess.Popen([sys.executable, "-c", ""], stdin=read_end)
        assert child.wait(30) == 0
    relay = RelayServer(short / "r.sock", log_path)
    relay.bind()
    if wrap_server is not None:
        relay._server = wrap_server(relay._server)
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


def _stop_within(relay, held, bound: float, gate=None) -> bool:
    """Run `stop` and give it `bound` seconds. One still blocked then is released,
    `gate` first and then by closing the held read end (EPIPE), so a failing run
    ends instead of hanging."""
    returned = threading.Event()
    stopper = threading.Thread(target=lambda: (relay.stop(), returned.set()), daemon=True)
    stopper.start()
    if returned.wait(bound):
        return True
    if gate is not None:
        gate.release()
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


def test_stop_cuts_an_exchange_a_retained_stdin_reader_holds(exited_child, capfd):
    """C-26.4 (#230 review r1): a 2 MiB frame waits in the stdin pipe of a child that
    has exited, whose read end is held by a process that never reads it. `stop` gives
    the exchange its bound (`REPLY_TIMEOUT_S`, the reply's own) and then cuts it: it
    returns within that bound plus 1 s, writes one fixed line to descriptor 2 (#236
    review r2: a raw write, not `logging`), leaves no relay thread and no socket file,
    and closes the pipe. The frame is logged `failed` (ECANCELED), the reader got only
    part of it, and the client reads end of stream, never an acknowledgement. At
    d72597b5c, `stop` waited until the reader closed the pipe."""
    relay, client, held = exited_child
    _send_and_wait_for_intent(relay, client, LINE_2MIB)
    assert _stop_within(relay, held, REPLY_TIMEOUT_S + 1), "a retained stdin reader held stop past its bound"
    assert not relay._thread.is_alive(), "the relay thread outlived stop"
    assert not relay.path.exists(), "stop left the socket file"
    # Fixed text, once: nothing of the frame, its tag or its client.
    assert capfd.readouterr().err == relay_module.CUT_LOG_LINE + "\n"
    assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(relay.log_path)] == [
        (1, "failed", errno.ECANCELED)]
    assert client.recv(1 << 16) == b"", "a cut exchange was acknowledged"
    received = _read_to_eof(held.fd)
    whole = (LINE_2MIB + "\n").encode()
    assert 0 < len(received) < len(whole) and whole.startswith(received)


def test_stop_still_answers_an_exchange_that_finishes_inside_its_bound(exited_child, capfd):
    """C-26.4 (kept from #230): the same 2 MiB frame, but the holder of the read end
    drains the pipe as `stop` begins. The exchange finishes inside the bound, so the
    line is written whole and its acknowledgement is sent before `stop` returns, and
    nothing is cut or reported. This passes at d72597b5c too; it pins the behaviour the
    bound keeps, and a cut that does not wait out the bound fails it."""
    relay, client, held = exited_child
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
    assert capfd.readouterr().err == "", "an exchange that finished inside the bound was reported cut"
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


# #236 review r1: what `stop` waits for after the cut is bounded too. C-26.4's whole
# bound sums the exchange's, the relay thread join's and the cleanup's;
# STOP_MARGIN_S covers the thread start and the calls between those waits.
STOP_MARGIN_S = 0.5


def _stop_bound() -> float:
    return relay_module.REPLY_TIMEOUT_S + relay_module.JOIN_TIMEOUT_S + relay_module.CLEANUP_TIMEOUT_S


def _settle(relay) -> None:
    """Once every gate is released: the relay thread and stop's cleanup end."""
    for thread in (relay._thread, getattr(relay, "_cleanup_thread", None)):
        if thread is not None:
            thread.join(10)
            assert not thread.is_alive(), f"{thread.name} did not end once released"


@pytest.mark.parametrize("point", ["journal-fsync", "journal-write"])
def test_a_stalled_failure_record_cannot_hold_stop(exited_child, capfd, monkeypatch, point):
    """C-26.4 (#236 review r1): the reviewer's input. A 2 MiB frame waits in the stdin
    pipe of a child that has exited, whose read end is held and never read; `stop` cuts
    the exchange, and the fsync (or the write) of the frame's `failed` record stalls.
    `stop` still returns within its whole bound (`REPLY_TIMEOUT_S` + `JOIN_TIMEOUT_S` +
    `CLEANUP_TIMEOUT_S`) while that call is held, having closed the pipe (the reader
    holds a strict prefix, then end of file) and unlinked the socket; the client reads
    end of stream. Released, the record lands as before (`failed`, ECANCELED) and the
    cut line is written once. At 9ec7c87fb `stop` waited in `_close_pipe` for the lock that
    `apply` held through the record (still blocked at 2.59 s in the review; here, until
    released)."""
    relay, client, held = exited_child
    gate = Gate()
    monkeypatch.setattr(relay_module, "os", StallingOs(gate, point=point, record="failed"))
    whole = (LINE_2MIB + "\n").encode()
    try:
        _send_and_wait_for_intent(relay, client, LINE_2MIB)
        assert _stop_within(relay, held, _stop_bound() + STOP_MARGIN_S, gate=gate), \
            f"a stalled {point} of the failure record held stop past its bound"
        assert gate.reached.is_set() and not gate.released.is_set(), "stop returned only once the record was released"
        assert not relay.path.exists(), "stop left the socket file"
        assert client.recv(1 << 16) == b"", "a cut exchange was acknowledged"
        received = _read_to_eof(held.fd)
        assert 0 < len(received) < len(whole) and whole.startswith(received)
        # A held fsync leaves the record visible but not yet durable; a held write, unwritten.
        assert [(r["seq"], r["status"]) for r in read_log(relay.log_path)] == [
            (1, "failed" if point == "journal-fsync" else "pending")]
    finally:
        gate.release()
    _settle(relay)
    assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(relay.log_path)] == [
        (1, "failed", errno.ECANCELED)]
    assert capfd.readouterr().err == relay_module.CUT_LOG_LINE + "\n"


def test_stop_keeps_its_bound_when_its_cleanup_thread_cannot_start(exited_child, capfd, monkeypatch):
    """C-26.4 (#236 review r2, P2): the reviewer's input. A 2 MiB frame waits in the
    stdin pipe of a child that has exited, whose read end is held and never read; stop's
    cleanup thread cannot start (CPython's `RuntimeError: can't start new thread`), and
    the socket file's unlink would stall. `stop` still returns within its whole bound:
    it runs only the cleanup steps that cannot wait, and never reaches the unlink. The
    listening socket is closed (a connect is refused), and so is the pipe, which no
    write held once the cut write gave up (the reader holds a strict prefix, then end of
    file). The socket file is left, for the daemon's finalization, and no cut line is
    written; the frame is logged `failed` (ECANCELED) and the client reads end of
    stream. At ce226d226 `stop` ran the whole cleanup inline and waited in the unlink
    (4.35 s in the review, the unlink held until 4.2 s)."""
    relay, client, held = exited_child
    gate = Gate()
    monkeypatch.setattr(relay_module, "os", StallingOs(gate, point="unlink"))
    monkeypatch.setattr(threading.Thread, "start", refusing_start(threading.Thread.start))
    whole = (LINE_2MIB + "\n").encode()
    try:
        _send_and_wait_for_intent(relay, client, LINE_2MIB)
        assert _stop_within(relay, held, _stop_bound() + STOP_MARGIN_S, gate=gate), \
            "with no cleanup thread, stop ran past its bound"
        assert not gate.reached.is_set(), "with no cleanup thread, stop reached the unlink"
        assert relay._cleanup_thread is None
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with pytest.raises(ConnectionRefusedError):
                probe.connect(str(relay.path))
        finally:
            probe.close()
        received = _read_to_eof(held.fd)
        assert 0 < len(received) < len(whole) and whole.startswith(received)
        assert client.recv(1 << 16) == b"", "a cut exchange was acknowledged"
        assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(relay.log_path)] == [
            (1, "failed", errno.ECANCELED)]
        assert relay.path.is_socket(), "the unlink, which can wait on the disk, ran inline"
        assert capfd.readouterr().err == "", "the cut line, whose write can wait on stderr, ran inline"
    finally:
        gate.release()


def _held_shutdown_locks() -> list[str]:
    """The locks interpreter shutdown takes that another thread holds now: logging's
    module lock and each live handler's (`logging.shutdown`, run at exit, takes the
    handlers'), and the buffers of the standard streams (the exit's flush takes those)."""
    held = []
    handlers = [handler for handler in (ref() for ref in list(logging._handlerList)) if handler is not None]
    locks = [("logging._lock", logging._lock)] + [(repr(h), h.lock) for h in handlers if h.lock is not None]
    for name, lock in locks:
        if lock.acquire(timeout=0.5):
            lock.release()
        else:
            held.append(name)
    for name in ("__stdout__", "__stderr__"):
        stream = getattr(sys, name)
        if stream is None:
            continue
        flushed = threading.Event()
        threading.Thread(target=lambda stream=stream: (stream.flush(), flushed.set()), daemon=True).start()
        if not flushed.wait(0.5):
            held.append(f"sys.{name}")
    return held


def test_a_held_cut_line_holds_no_lock_the_guardians_exit_takes(exited_child, capfd, monkeypatch):
    """C-26.4 (#236 review r2, P3): the cut line is a raw `os.write` of descriptor 2,
    never `logging` or `sys.stderr`, so a write held there (the guardian's stderr a pipe
    nobody reads) holds no lock that interpreter shutdown takes: `logging.shutdown`
    takes every handler's lock, and the exit's flush of the standard streams their
    buffers' locks. With that write held, `stop` returns within its whole bound, having
    closed the pipe (the reader holds a strict prefix, then end of file); logging's
    module lock, every handler's lock and the standard streams are free. Released, the
    line lands once, exactly, the socket file is unlinked and the frame is `failed`
    (ECANCELED). At ce226d226 the line went through `logging` (in the guardian, which
    has no handler, `lastResort`, which writes `sys.stderr` under its lock), and the
    reviewer's guardian stayed alive in `logging.shutdown` until the write let go."""
    relay, client, held = exited_child
    gate = Gate()
    monkeypatch.setattr(relay_module, "os", StallingOs(gate, point="stderr-write"))
    whole = (LINE_2MIB + "\n").encode()
    try:
        _send_and_wait_for_intent(relay, client, LINE_2MIB)
        assert _stop_within(relay, held, _stop_bound() + STOP_MARGIN_S, gate=gate), \
            "a held cut line held stop past its bound"
        assert gate.reached.is_set(), "the cut line was not a raw write of descriptor 2"
        assert _held_shutdown_locks() == [], "a held cut line holds a lock the guardian's exit takes"
        received = _read_to_eof(held.fd)
        assert 0 < len(received) < len(whole) and whole.startswith(received)
        assert client.recv(1 << 16) == b"", "a cut exchange was acknowledged"
    finally:
        gate.release()
    _settle(relay)
    assert not relay.path.exists(), "stop left the socket file"
    assert capfd.readouterr().err == relay_module.CUT_LOG_LINE + "\n"
    assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(relay.log_path)] == [
        (1, "failed", errno.ECANCELED)]


def _inside_the_exchange(relay, gate) -> None:
    """Return once the relay has taken frame 1: its intent is logged, or a call of
    the exchange is held at `gate` (an intent whose write is held is not logged)."""
    until = time.monotonic() + 10
    while not gate.reached.is_set() and not read_log(relay.log_path):
        assert time.monotonic() < until, "the relay did not take the frame"
        time.sleep(0.005)


@hypothesis.settings(max_examples=16, deadline=None, derandomize=True, database=None)
@hypothesis.example(point="journal-fsync", record="failed", size=2 << 20, drain=0, release=None)  # #236 review r1
@hypothesis.example(point="journal-write", record="failed", size=2 << 20, drain=0, release=None)
@hypothesis.example(point="journal-write", record="intent", size=10, drain=11, release=None)
@hypothesis.example(point="journal-fsync", record="outcome", size=10, drain=11, release=None)
@hypothesis.example(point="pipe-write", record="outcome", size=2 << 20, drain=0, release=None)
@hypothesis.example(point="conn-close", record="outcome", size=2 << 20, drain=0, release=None)
@hypothesis.example(point="server-close", record="outcome", size=10, drain=11, release=None)
@hypothesis.example(point="unlink", record="outcome", size=2 << 20, drain=0, release=None)
@hypothesis.example(point="stderr-write", record="outcome", size=2 << 20, drain=0, release=None)  # #236 review r2
@hypothesis.given(point=st.sampled_from(("none",) + POINTS), record=st.sampled_from(("intent", "outcome", "failed")),
                  size=st.integers(0, 3 << 19), drain=st.integers(0, 3 << 19),
                  release=st.none() | st.floats(0, 1))
def test_stop_returns_within_its_bound_whatever_stalls(point, record, size, drain, release):
    """C-26.4 (#236 review r1), for every stall point (a stdin pipe write; the write or
    fsync of a journal record, the frame's intent or its outcome; the close of the
    connection or of the listening socket; the cut line's write to stderr, #236 review
    r2; the socket file's unlink), frame size,
    amount the holder of the read end drains, and moment the stall ends (a fraction of
    the bound, or only once `stop` has returned): `stop` returns within its whole
    bound. Unless the held call is a pipe write or comes before the pipe's close in
    the cleanup (the listening socket's close), the pipe is closed when it returns.
    Once released nothing is left: no relay or cleanup thread, socket file or open
    write end. The frame is `written` exactly when the reader got the whole line, else
    `failed` (ECANCELED) with the reader holding a strict prefix, and only a `written`
    frame is acknowledged. The bounds are shortened to keep examples quick; it is the
    same code path."""
    line = "y" * size
    whole = (line + "\n").encode()
    gate = Gate()
    wrap = (lambda server: StallingServer(server, gate, point=point)) if point in SOCKET_POINTS else None
    stalls = (mock.patch.object(relay_module, "os", StallingOs(gate, point=point, record=record))
              if point in OS_POINTS else contextlib.nullcontext())
    with tempfile.TemporaryDirectory(prefix="sfrs-") as scratch, \
            mock.patch.object(relay_module, "REPLY_TIMEOUT_S", 0.25), \
            mock.patch.object(relay_module, "JOIN_TIMEOUT_S", 0.5), \
            mock.patch.object(relay_module, "CLEANUP_TIMEOUT_S", 0.5):
        made = _relay_on_a_socket(Path(scratch) / "stdin.jsonl", wrap_server=wrap)
        relay, client, held, _ = made
        timer = None
        try:
            with stalls:
                got = bytearray()

                def reader():
                    while len(got) < drain:
                        chunk = os.read(held.fd, min(1 << 16, drain - len(got)))
                        if not chunk:
                            return
                        got.extend(chunk)

                draining = threading.Thread(target=reader, daemon=True)
                draining.start()
                client.sendall((json.dumps(F(1, line=line, tag="user-message")) + "\n").encode())
                _inside_the_exchange(relay, gate)
                if release is not None:
                    timer = threading.Timer(release * _stop_bound(), gate.release)
                    timer.start()
                assert _stop_within(relay, held, _stop_bound() + STOP_MARGIN_S, gate=gate), \
                    f"stop past its bound with {point} ({record}) held"
                hypothesis.event(f"{point} {'held' if gate.reached.is_set() else 'not reached'}")
                if point not in ("pipe-write", "server-close"):
                    draining.join(10)
                    assert not draining.is_alive(), "stop returned with the pipe's write end open"
                    got.extend(_read_to_eof(held.fd))
                gate.release()
                _settle(relay)
            assert not relay.path.exists(), "the socket file was left"
            draining.join(10)
            assert not draining.is_alive(), "the reader never reached end of file"
            got.extend(_read_to_eof(held.fd))
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
            gate.release()
            if timer is not None:
                timer.cancel()
            _release(*made)


def test_status_waits_for_the_frame_whose_outcome_is_being_logged(server, monkeypatch):
    """C-26.4, IR-27 (#236 review r1): `status` is answered only after a frame being
    applied is logged, so the log a runner reads after its handshake is final. With
    the journal under a lock of its own, `status` takes that lock: a frame whose
    `written` record is still being fsynced holds the answer until the record is
    durable. Kept behaviour (it passes at 9ec7c87fb); `status` under the pipe lock
    alone fails it."""
    relay, read_end = server
    gate = Gate()
    monkeypatch.setattr(relay_module, "os", StallingOs(gate, point="journal-fsync", record="written"))
    applied: list = []
    answers: list = []
    applier = threading.Thread(target=lambda: applied.append(relay.apply(F(1, line="one", tag="init"))), daemon=True)
    asker = threading.Thread(target=lambda: answers.append(relay.apply({"op": "status"})), daemon=True)
    try:
        applier.start()
        assert gate.reached.wait(5), "the frame never reached its outcome's fsync"
        asker.start()
        asker.join(0.3)
        assert asker.is_alive() and not answers, "status answered before the frame's outcome was durable"
    finally:
        gate.release()
    asker.join(5)
    applier.join(5)
    assert applied == [{"seq": 1, "ok": True}]
    assert answers[0]["status"]["applied"] == 1
    assert [(r["seq"], r["status"]) for r in read_log(relay.log_path)] == [(1, "written")]
    assert _drain(read_end) == b"one\n"


# The first frame is a write, so the frames a stall can pick from are logged ones.
FRAME_OPS = st.tuples(st.just("write"), st.integers(0, LOG_INLINE_MAX + 4096)).flatmap(
    lambda first: st.lists(st.one_of(st.tuples(st.just("write"), st.integers(0, LOG_INLINE_MAX + 4096)),
                                     st.tuples(st.sampled_from(["close", "signal", "dup", "gap"]))),
                           max_size=5).map(lambda rest: [first, *rest]))


@hypothesis.settings(max_examples=25, deadline=None, derandomize=True, database=None)
@hypothesis.example(ops=[("write", 10), ("write", 10)], stop_seq=2, record="intent")      # between intent and write
@hypothesis.example(ops=[("write", 10), ("write", 10)], stop_seq=1, record="outcome")     # between write and outcome
@hypothesis.example(ops=[("close",), ("signal",)], stop_seq=1, record="intent")
@hypothesis.given(ops=FRAME_OPS, stop_seq=st.none() | st.integers(1, 3), record=st.sampled_from(("intent", "outcome")))
def test_the_journal_keeps_its_order_whatever_stop_does(ops, stop_seq, record):
    """C-26.4, C-26.6 (#236 review r1): every reader of `stdin.jsonl` (a relay
    resuming from it, the runner's handshake and replay through `read_log`,
    reconciliation's `frame_status`) reads it as each frame's intent, then that frame's
    outcome, frame by frame. With the journal under its own lock and the pipe under
    another, `stop` can close the pipe while a frame's intent or outcome is being
    fsynced: between the intent and the pipe write, or between the write and the
    outcome. For any frames and that interleaving: every line is a record in that
    order, none left pending, all accounted for as `frame_status` counts them; a new
    relay resumes at the same count; the pipe holds exactly the written frames'
    lines, in order; and only a written frame is acknowledged."""
    with tempfile.TemporaryDirectory(prefix="sfro-") as scratch:
        log = Path(scratch) / "stdin.jsonl"
        relay = RelayServer(Path(scratch) / "r.sock", log)
        read_end, write_end = os.pipe()
        os.set_blocking(write_end, False)               # as `serve` leaves it
        relay._pipe = write_end
        received = bytearray()

        def drain():
            while chunk := os.read(read_end, 1 << 16):
                received.extend(chunk)

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        gate = Gate()
        frames: dict[int, dict] = {}
        answers: list[tuple[dict, dict]] = []

        def run():
            for op in ops:
                seq = relay.last_applied + 1
                if op[0] == "write":
                    frame = F(seq, line="w" * op[1], tag=f"t{seq}")
                elif op[0] == "close":
                    frame = F(seq, "close", tag="close")
                elif op[0] == "signal":
                    frame = {"seq": seq, "op": "signal", "sig": "INT", "tag": "interrupt",
                             "sha256": frame_sha256("signal", sig="INT")}
                elif op[0] == "dup":
                    frame = frames.get(seq - 1)
                    if frame is None:
                        continue
                else:
                    frame = F(seq + 1, line="gap")
                answer = relay.apply(frame)
                if relay.last_applied >= frame["seq"]:           # logged: its intent is in the journal
                    frames.setdefault(frame["seq"], frame)
                answers.append((frame, answer))

        try:
            # Without `stop_seq` nothing is held (no frame has number -1) and stop races the frames.
            with mock.patch.object(relay_module, "os", StallingOs(gate, point="journal-fsync", record=record,
                                                                  seq=stop_seq if stop_seq is not None else -1)):
                applier = threading.Thread(target=run, daemon=True)
                applier.start()
                if stop_seq is not None:
                    until = time.monotonic() + 10
                    while not gate.reached.is_set() and applier.is_alive():
                        assert time.monotonic() < until, "the applier neither finished nor reached the stall"
                        time.sleep(0.005)
                # No exchange or relay thread here: stop's cleanup closes the pipe. A stop
                # still blocked at its bound is released, so a failing run ends.
                stopped = threading.Event()
                stopper = threading.Thread(target=lambda: (relay.stop(), stopped.set()), daemon=True)
                stopper.start()
                in_bound = stopped.wait(_stop_bound() + STOP_MARGIN_S)
                gate.release()
                stopper.join(10)
                assert in_bound, "a held journal fsync held stop past its bound"
                applier.join(10)
                assert not applier.is_alive()
                cleanup = getattr(relay, "_cleanup_thread", None)
                if cleanup is not None:
                    cleanup.join(10)
            drainer.join(10)
            assert not drainer.is_alive(), "the pipe's write end was left open"
        finally:
            gate.release()
            os.close(read_end)
            relay._close_pipe()
        lines = [json.loads(raw) for raw in log.read_bytes().splitlines()] if log.exists() else []
        assert [(r["kind"] == "intent", r["seq"]) for r in lines] == [
            (i % 2 == 0, i // 2 + 1) for i in range(len(lines))]
        records = read_log(log)
        assert 2 * len(records) == len(lines) and all(r["status"] in ("written", "failed") for r in records)
        assert RelayServer(Path(scratch) / "r.sock", log).last_applied == len(records)
        written = [r["seq"] for r in records if r["op"] == "write" and r["status"] == "written"]
        assert bytes(received) == b"".join((frames[seq]["line"] + "\n").encode() for seq in written)
        for frame, answer in answers:
            if answer.get("ok") and frame["op"] == "write":
                assert records[frame["seq"] - 1]["status"] == "written"
        hypothesis.event("stalled" if gate.reached.is_set() else "unstalled")
