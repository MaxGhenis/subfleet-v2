"""C-26.4: the guardian relay applies numbered stdin frames once, in order."""

from __future__ import annotations

import json
import os

import pytest

from subfleet.relay import LOG_INLINE_MAX, RelayServer, line_sha256, read_log, socket_path


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
