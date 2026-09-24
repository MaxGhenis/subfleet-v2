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
