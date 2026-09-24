"""C-26.4 and C-5.1: a turn's guardian relays numbered frames to its child's stdin.

A real guardian runs a child that echoes stdin to stdout. The daemon side sends
frames, loses its connection mid-conversation, reconnects and resends, then
closes; the child sees each line once and exits, and the guardian's receipts
are the ordinary ones.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet.relay import RelayClient, read_log

ECHO = "import sys\nfor line in sys.stdin:\n    sys.stdout.write('got:' + line); sys.stdout.flush()\nprint('eof')\n"


def wait_for(predicate, timeout=5.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not hold in time")


@pytest.fixture
def guardian(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("C-26.4 process test targets the deployment OS")
    attempt = tmp_path / "a1"
    # Short enough for AF_UNIX; a private directory, as `<state root>/run` is.
    sock = Path("/private/tmp") / f"sfr-{os.getpid()}-{time.monotonic_ns() % 10**6}" / "x.sock"
    argv = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(attempt), "--cwd", str(tmp_path),
            "--stdout-path", str(attempt / "stdout"), "--stderr-path", str(attempt / "stderr"),
            "--control-socket", str(sock), "--", sys.executable, "-c", ECHO]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
           "SUBFLEET_ATTEMPT": "relay/a1", "SUBFLEET_JOB": "relay"}
    process = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    yield attempt, sock, process
    if process.poll() is None:
        process.kill()
        process.wait(5)
    for path in (sock, sock.parent):
        try:
            path.unlink() if path == sock else path.rmdir()
        except (FileNotFoundError, OSError):
            pass


def test_frames_reach_the_child_once_across_a_reconnect(guardian):
    """C-26.4 a daemon that disconnects after sending (a crash before it heard the
    acknowledgement) resends the same number; the child reads the line once."""
    attempt, sock, process = guardian
    start = json.loads(wait_for(lambda: (attempt / "start.json").exists() and (attempt / "start.json").read_text()))
    assert start["control_socket"] == str(sock)
    assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
    client = RelayClient(sock, timeout_s=5)
    assert client.send(1, "write", line='{"n":1}', tag="init").ok
    assert client.send(2, "write", line='{"n":2}', tag="user-message").ok
    client.close()                      # the daemon dies here
    again = RelayClient(sock, timeout_s=5)
    ack = again.send(2, "write", line='{"n":2}', tag="user-message")
    assert ack.ok and ack.dup
    assert again.send(3, "close", tag="end").ok
    receipt = json.loads(wait_for(lambda: (attempt / "exit.json").exists() and (attempt / "exit.json").read_text()))
    assert receipt["rc"] == 0
    assert (attempt / "stdout").read_text() == 'got:{"n":1}\ngot:{"n":2}\neof\n'
    assert [(r["seq"], r["op"], r["tag"]) for r in read_log(attempt / "stdin.jsonl")] == [
        (1, "write", "init"), (2, "write", "user-message"), (3, "close", "end")]
    assert oct(os.stat(attempt / "stdin.jsonl").st_mode & 0o777) == "0o600"
    process.wait(5)
    assert not sock.exists()


def test_child_exit_ends_the_relay(guardian):
    """C-26.4 when the child ends by itself the relay stops and exit.json is written
    as for any attempt (C-5.1)."""
    attempt, sock, process = guardian
    wait_for(lambda: (attempt / "start.json").exists())
    client = RelayClient(sock, timeout_s=5)
    assert client.send(1, "close", tag="end").ok
    receipt = json.loads(wait_for(lambda: (attempt / "exit.json").exists() and (attempt / "exit.json").read_text()))
    assert receipt["rc"] == 0
    process.wait(5)
