"""C-26.4: the guardian relay applies numbered stdin frames once, in order."""

from __future__ import annotations

import json
import os

import pytest

from subfleet.relay import LOG_INLINE_MAX, RelayServer, read_log, socket_path


@pytest.fixture
def server(tmp_path):
    read_end, write_end = os.pipe()
    relay = RelayServer(tmp_path / "run" / "x.sock", tmp_path / "stdin.jsonl")
    relay._pipe = write_end
    yield relay, read_end
    os.close(read_end)
    relay._close_pipe()


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
    assert relay.apply({"seq": 1, "op": "write", "line": '{"a":1}', "tag": "init"}) == {"seq": 1, "ok": True}
    assert relay.apply({"seq": 1, "op": "write", "line": '{"a":1}', "tag": "init"})["dup"] is True
    assert relay.apply({"seq": 3, "op": "write", "line": "x"})["error"] == "gap"
    assert relay.apply({"seq": 2, "op": "write", "line": '{"b":2}', "tag": "user-message"})["ok"]
    assert _drain(read_end) == b'{"a":1}\n{"b":2}\n'


def test_log_is_written_before_the_pipe_and_recovers_the_count(server, tmp_path):
    """C-26.4 each applied frame is in `stdin.jsonl`; a new server resumes numbering
    from the log and never re-applies what it records."""
    relay, _ = server
    relay.apply({"seq": 1, "op": "write", "line": "one", "tag": "init"})
    relay.apply({"seq": 2, "op": "write", "line": "two", "tag": "user-message"})
    records = read_log(tmp_path / "stdin.jsonl")
    assert [(r["seq"], r["tag"], r["line"]) for r in records] == [(1, "init", "one"), (2, "user-message", "two")]
    again = RelayServer(tmp_path / "run" / "x.sock", tmp_path / "stdin.jsonl")
    assert again.last_applied == 2
    read_end, write_end = os.pipe()
    again._pipe = write_end
    assert again.apply({"seq": 2, "op": "write", "line": "two"})["dup"] is True
    assert again.apply({"seq": 3, "op": "write", "line": "three"})["ok"]
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
    assert relay.apply({"seq": 1, "op": "write", "line": big, "tag": "user-message"})["ok"]
    done.wait(5)
    thread.join(5)
    assert b"".join(chunks) == big.encode() + b"\n"
    record = read_log(tmp_path / "stdin.jsonl")[0]
    assert "line" not in record and record["bytes"] == len(big) and len(record["sha256"]) == 64


def test_close_ends_input_and_later_frames_are_refused(server):
    """C-26.4 `close` closes the child's stdin once; a later write is refused as closed."""
    relay, read_end = server
    assert relay.apply({"seq": 1, "op": "close", "tag": "end"})["ok"]
    assert os.read(read_end, 10) == b""
    assert relay.apply({"seq": 2, "op": "write", "line": "late"})["error"] == "closed"


def test_malformed_frames_are_refused_without_logging(server, tmp_path):
    """C-26.4 malformed frames, and a line containing a newline, change nothing."""
    relay, _ = server
    assert relay.apply({"seq": "1", "op": "write", "line": "a"})["error"] == "bad-frame"
    assert relay.apply({"seq": 1, "op": "shout", "line": "a"})["error"] == "bad-frame"
    assert relay.apply({"seq": 1, "op": "write", "line": "a\nb"})["error"] == "newline-in-line"
    assert not (tmp_path / "stdin.jsonl").exists()


def test_torn_log_tail_is_not_counted(tmp_path):
    """C-26.4 a crash mid-append leaves a line without its newline: that frame was
    never written to the pipe, so it does not count as applied."""
    log = tmp_path / "stdin.jsonl"
    log.write_text(json.dumps({"seq": 1, "op": "write", "tag": "a", "line": "x"}) + "\n" + '{"seq": 2, "op"')
    assert [r["seq"] for r in read_log(log)] == [1]


def test_socket_path_fits_af_unix(tmp_path):
    """C-26.4 the relay socket path stays under the 103-byte AF_UNIX limit for the
    real state root and a maximal attempt id."""
    path = socket_path("/Users/maxghenis/.subfleet", "20260924-081504-" + "x" * 40 + "/a3")
    assert len(str(path).encode()) < 103
