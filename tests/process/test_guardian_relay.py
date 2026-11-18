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

from subfleet import procs
from subfleet.launch_env import build_environment, guardian_pass_args
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


def write_lock(path, pid):
    ident = procs.identity(pid)
    path.write_text(json.dumps({"pid": pid, "boot_id": ident.boot_id, "proc_start": ident.proc_start}))


@pytest.fixture
def guardian(tmp_path, request):
    if sys.platform != "darwin":
        pytest.skip("C-26.4 process test targets the deployment OS")
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-26.4 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-26.4 host sandbox denies ps/sysctl")
    lock = tmp_path / "daemon.lock"
    write_lock(lock, getattr(request, "param", None) or os.getpid())
    attempt = tmp_path / "a1"
    # Short enough for AF_UNIX; a private directory, as `<state root>/run` is.
    sock = Path("/private/tmp") / f"sfr-{os.getpid()}-{time.monotonic_ns() % 10**6}" / "x.sock"
    env = build_environment({"PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                             "SUBFLEET_ATTEMPT": "relay/a1", "SUBFLEET_JOB": "relay"})
    argv = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(attempt), "--cwd", str(tmp_path),
            "--stdout-path", str(attempt / "stdout"), "--stderr-path", str(attempt / "stderr"),
            "--control-socket", str(sock), "--relay-peer-lock", str(lock),
            *guardian_pass_args(env), "--", sys.executable, "-c", ECHO]
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


@pytest.mark.parametrize("guardian", [1], indirect=True)
def test_only_the_daemon_named_by_the_lock_may_write(guardian):
    """C-26.4 a same-user process that is not the recorded daemon is refused and
    nothing reaches the provider (here the lock names pid 1, not this test)."""
    attempt, sock, process = guardian
    wait_for(lambda: (attempt / "start.json").exists())
    ack = RelayClient(sock, timeout_s=5).send(1, "write", line='{"forged":true}', tag="user-message")
    assert not ack.ok and ack.error == "peer-refused"
    assert not (attempt / "stdin.jsonl").exists()
    process.kill()
    process.wait(5)


def test_the_relay_advertises_its_cap_and_answers_status(guardian):
    """C-26.4, IR-27: `start.json` names the relay's version and frame cap; `status`
    answers what was applied and whether the child runs; a frame over the cap is
    refused by the client before anything is sent, and the connection stays usable."""
    from subfleet.relay import FRAME_MAX, RELAY_VERSION, FrameTooLarge
    attempt, sock, process = guardian
    start = json.loads(wait_for(lambda: (attempt / "start.json").exists() and (attempt / "start.json").read_text()))
    assert start["relay"] == {"version": RELAY_VERSION, "frame_max": FRAME_MAX}
    client = RelayClient(sock, timeout_s=5)
    client.frame_max = 1                     # replaced by the relay's own advertisement
    status = client.status()
    assert status["applied"] == 0 and status["child"] == "running" and status["closed"] is False
    assert client.frame_max == FRAME_MAX
    assert client.send(1, "write", line='{"n":1}', tag="init").ok
    client.frame_max = 64
    with pytest.raises(FrameTooLarge):
        client.send(2, "write", line="x" * 100, tag="user-message")
    assert client.status()["applied"] == 1
    assert client.send(2, "close", tag="close").ok
    receipt = json.loads(wait_for(lambda: (attempt / "exit.json").exists() and (attempt / "exit.json").read_text()))
    assert receipt["rc"] == 0
    assert [(r["seq"], r["tag"]) for r in read_log(attempt / "stdin.jsonl")] == [(1, "init"), (2, "close")]
    process.wait(5)


# The provider starts a background process that inherits its stdin and never
# reads it, then exits once the test says so (its first argument names a file),
# with the status its second argument names.
PARK = ("import os, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "while not os.path.exists(sys.argv[1]):\n"
        "    time.sleep(0.01)\n"
        "raise SystemExit(int(sys.argv[2]))\n")


def _parked_guardian(tmp_path, launcher: list[str], *, rc: int = 0, stderr=subprocess.DEVNULL):
    """A real guardian, started by `launcher` (the argv that runs its `main`), whose
    provider runs PARK: it exits with `rc` once the test creates the returned `go`.
    `stderr` is the guardian's own standard error."""
    if sys.platform != "darwin":
        pytest.skip("C-26.4 process test targets the deployment OS")
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-26.4 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-26.4 host sandbox denies ps/sysctl")
    lock = tmp_path / "daemon.lock"
    write_lock(lock, os.getpid())
    attempt, go = tmp_path / "a1", tmp_path / "go"
    sock = Path("/private/tmp") / f"sfr-{os.getpid()}-{time.monotonic_ns() % 10**6}" / "x.sock"
    env = build_environment({"PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                             "SUBFLEET_ATTEMPT": "relay/a1", "SUBFLEET_JOB": "relay"})
    argv = [*launcher, "--attempt-dir", str(attempt), "--cwd", str(tmp_path),
            "--stdout-path", str(attempt / "stdout"), "--stderr-path", str(attempt / "stderr"),
            "--control-socket", str(sock), "--relay-peer-lock", str(lock),
            *guardian_pass_args(env), "--", sys.executable, "-c", PARK, str(go), str(rc)]
    process = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=stderr)
    return attempt, sock, go, process


def _send_2mib(sock, answers: list):
    """Send a 2 MiB frame on a thread; its acknowledgement or RelayError lands in `answers`."""
    import threading

    from subfleet.relay import RelayError

    def send():
        try:
            answers.append(RelayClient(sock, timeout_s=30).send(1, "write", line="x" * (2 << 20), tag="user-message"))
        except RelayError as exc:
            answers.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    return sender


def _end_parked_guardian(sock, process) -> None:
    import signal

    # The guardian leads its own group; the background process is in it.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait(10)
    for path in (sock, sock.parent):
        try:
            path.unlink() if path == sock else path.rmdir()
        except (FileNotFoundError, OSError):
            pass


def test_a_retained_stdin_reader_cannot_hold_the_exit_receipt(tmp_path):
    """C-26.4, C-5.1 (#230 review r1): a 2 MiB frame waits in the provider's stdin
    when the provider exits, while a background process it started holds stdin open
    and never reads. The relay cuts that exchange after its bound, so the guardian
    publishes `exit.json` with the provider's own exit status. The frame is logged
    `failed` (ECANCELED) and the daemon side reads end of stream, never an
    acknowledgement. At d72597b5c the receipt waited for the background process."""
    import errno

    from subfleet.relay import RelayError

    attempt, sock, go, process = _parked_guardian(tmp_path, [sys.executable, "-m", "subfleet.guardian"])
    answers: list = []
    try:
        wait_for(lambda: (attempt / "start.json").exists())
        sender = _send_2mib(sock, answers)
        wait_for(lambda: read_log(attempt / "stdin.jsonl"), timeout=10)
        go.touch()                                                   # the provider exits now
        receipt = json.loads(wait_for(lambda: (attempt / "exit.json").exists()
                                      and (attempt / "exit.json").read_text(), timeout=10))
        assert receipt["rc"] == 0
        assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(attempt / "stdin.jsonl")] == [
            (1, "failed", errno.ECANCELED)]
        sender.join(10)
        assert len(answers) == 1 and isinstance(answers[0], RelayError), answers
        process.wait(10)
        assert not sock.exists()
    finally:
        _end_parked_guardian(sock, process)


# The guardian's `main`, with the fsync of a `failed` journal record held until the
# test creates `release` (argv 2); the guardian creates `reached` (argv 1) once held.
STALLED_FSYNC_GUARDIAN = (
    "import sys\n"
    "from subfleet import relay\n"
    "from tests.relay_stalls import FileGate, StallingOs\n"
    "relay.os = StallingOs(FileGate(sys.argv[1], sys.argv[2]), point='journal-fsync', record='failed')\n"
    "from subfleet.guardian import main\n"
    "raise SystemExit(main(sys.argv[3:]))\n")


def test_a_stalled_journal_fsync_cannot_hold_the_exit_receipt(tmp_path):
    """C-26.4, C-5.1 (#236 review r1): the reviewer's input through a real guardian. A
    2 MiB frame waits in the provider's stdin when the provider exits, a background
    process it started holds stdin open and never reads, and the fsync of the cut
    frame's `failed` record stalls (a patched `os.fsync` in the guardian holds it until
    the test releases it). The guardian publishes `exit.json`, with the provider's own
    exit status, while that fsync is still held: `stop` waits for it only within its
    bound. The record is in the journal (not yet durable), the daemon side reads end of
    stream, and the socket is gone. At 9ec7c87fb the receipt waited for the fsync."""
    import errno

    from subfleet.relay import RelayError

    reached, release = tmp_path / "fsync-held", tmp_path / "fsync-release"
    launcher = [sys.executable, "-c", STALLED_FSYNC_GUARDIAN, str(reached), str(release)]
    attempt, sock, go, process = _parked_guardian(tmp_path, launcher)
    answers: list = []
    try:
        wait_for(lambda: (attempt / "start.json").exists(), timeout=10)
        sender = _send_2mib(sock, answers)
        wait_for(lambda: read_log(attempt / "stdin.jsonl"), timeout=10)
        go.touch()                                                   # the provider exits now
        try:
            receipt = json.loads(wait_for(lambda: (attempt / "exit.json").exists()
                                          and (attempt / "exit.json").read_text(), timeout=10))
        except AssertionError:
            raise AssertionError("no exit.json while the journal fsync was held") from None
        assert reached.exists() and not release.exists(), "exit.json came only once the fsync was released"
        assert receipt["rc"] == 0
        assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(attempt / "stdin.jsonl")] == [
            (1, "failed", errno.ECANCELED)]
        sender.join(10)
        assert len(answers) == 1 and isinstance(answers[0], RelayError), answers
        assert not sock.exists()
    finally:
        release.touch()
        _end_parked_guardian(sock, process)


# The guardian's `main`, with stop's cleanup thread unable to start (as CPython's start
# fails when no thread can be had) and the socket file's unlink held until the test
# creates `release` (argv 2); the guardian creates `reached` (argv 1) if it gets there.
NO_CLEANUP_THREAD_GUARDIAN = (
    "import sys, threading\n"
    "from subfleet import relay\n"
    "from tests.relay_stalls import FileGate, StallingOs, refusing_start\n"
    "threading.Thread.start = refusing_start(threading.Thread.start)\n"
    "relay.os = StallingOs(FileGate(sys.argv[1], sys.argv[2]), point='unlink')\n"
    "from subfleet.guardian import main\n"
    "raise SystemExit(main(sys.argv[3:]))\n")
# Relay stop's whole bound (REPLY_TIMEOUT_S + JOIN_TIMEOUT_S + CLEANUP_TIMEOUT_S), and
# a margin for the provider's own exit and the receipt's publication after it.
STOP_BOUND_S, RECEIPT_MARGIN_S = 3.0, 2.0


def test_a_cleanup_thread_that_cannot_start_cannot_hold_the_exit_receipt(tmp_path):
    """C-26.4, C-5.1 (#236 review r2, P2): the reviewer's input through a real guardian.
    A 2 MiB frame waits in the provider's stdin when the provider exits 7, a background
    process it started holds stdin open and never reads, stop's cleanup thread cannot
    start, and the socket file's unlink would stall (a `FileGate`). The guardian
    publishes `exit.json`, with the provider's own status, within stop's whole bound
    plus a margin of the provider's exit, and never reaches the unlink. The frame is
    logged `failed` (ECANCELED) and the daemon side reads end of stream. The socket
    file is left for the daemon's finalization: nothing listens on it, so a connect is
    refused, as for a guardian gone. At ce226d226 the cleanup ran inline and waited in
    the unlink, and with it the receipt (still withheld at 3.51 s in the review)."""
    import errno
    import socket

    from subfleet.relay import RelayError

    reached, release = tmp_path / "unlink-held", tmp_path / "unlink-release"
    launcher = [sys.executable, "-c", NO_CLEANUP_THREAD_GUARDIAN, str(reached), str(release)]
    attempt, sock, go, process = _parked_guardian(tmp_path, launcher, rc=7)
    answers: list = []
    try:
        wait_for(lambda: (attempt / "start.json").exists(), timeout=10)
        sender = _send_2mib(sock, answers)
        wait_for(lambda: read_log(attempt / "stdin.jsonl"), timeout=10)
        go.touch()                                                   # the provider exits now
        try:
            receipt = json.loads(wait_for(lambda: (attempt / "exit.json").exists()
                                          and (attempt / "exit.json").read_text(),
                                          timeout=STOP_BOUND_S + RECEIPT_MARGIN_S))
        except AssertionError:
            raise AssertionError("no exit.json within stop's bound when its cleanup thread could not start") from None
        assert not reached.exists(), "with no cleanup thread, stop reached the unlink"
        assert receipt["rc"] == 7
        assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(attempt / "stdin.jsonl")] == [
            (1, "failed", errno.ECANCELED)]
        sender.join(10)
        assert len(answers) == 1 and isinstance(answers[0], RelayError), answers
        process.wait(10)
        assert sock.is_socket(), "the socket file was not left for finalization"
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with pytest.raises(ConnectionRefusedError):
                probe.connect(str(sock))
        finally:
            probe.close()
    finally:
        release.touch()
        _end_parked_guardian(sock, process)


# The guardian's linger (none here: the test's Popen did not make it its own
# responsible process) and its interpreter's exit, with margin.
EXIT_MARGIN_S = 5.0


def _fill(fd: int) -> int:
    """Fill the pipe whose write end is `fd` until a write of one byte would wait,
    and leave it blocking. The flag is the open pipe's, shared with the guardian's
    standard error, which writes nothing meanwhile."""
    os.set_blocking(fd, False)
    total = 0
    try:
        for chunk in (1 << 16, 4096, 512, 1):
            while True:
                try:
                    total += os.write(fd, b"f" * chunk)
                except BlockingIOError:
                    break
    finally:
        os.set_blocking(fd, True)
    return total


def _read_all(fd: int, timeout: float = 10.0) -> bytes:
    """Everything in the pipe up to end of file, which comes once every writer is gone."""
    os.set_blocking(fd, False)
    data, until = bytearray(), time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            chunk = os.read(fd, 1 << 16)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise AssertionError("the pipe had a writer left")


def test_a_stalled_stderr_cannot_hold_the_guardians_exit(tmp_path):
    """C-26.4, C-5.1 (#236 review r2, P3): a real, unpatched guardian whose standard
    error is a pipe the test fills once the guardian has started and never reads, so
    the cut line's write waits for good. A 2 MiB frame waits in the provider's stdin
    when the provider exits 7, and a background process it started holds stdin open
    and never reads. The guardian publishes `exit.json` with rc 7 and exits 7 within
    its linger plus a margin, while that write is still held: nothing of the cut line
    reached the pipe. The frame is logged `failed` (ECANCELED). At ce226d226 the line
    went through `logging`; the guardian published rc 7 and stayed alive in
    `logging.shutdown`, which waited for the handler lock the cleanup thread held."""
    import errno

    from subfleet.relay import RelayError

    read_end, write_end = os.pipe()
    try:
        attempt, sock, go, process = _parked_guardian(tmp_path, [sys.executable, "-m", "subfleet.guardian"],
                                                      rc=7, stderr=write_end)
    except BaseException:
        os.close(read_end)
        os.close(write_end)
        raise
    answers: list = []
    try:
        wait_for(lambda: (attempt / "start.json").exists(), timeout=10)
        filled = _fill(write_end)
        os.close(write_end)
        write_end = None
        sender = _send_2mib(sock, answers)
        wait_for(lambda: read_log(attempt / "stdin.jsonl"), timeout=10)
        go.touch()                                                   # the provider exits now
        receipt = json.loads(wait_for(lambda: (attempt / "exit.json").exists()
                                      and (attempt / "exit.json").read_text(), timeout=10))
        assert receipt["rc"] == 7
        try:
            process.wait(EXIT_MARGIN_S)
        except subprocess.TimeoutExpired:
            raise AssertionError("the guardian published rc 7 but did not exit while its stderr write was held") from None
        assert process.returncode == 7
        assert _read_all(read_end) == b"f" * filled, "the cut line's write was not held"
        assert [(r["seq"], r["status"], r.get("errno")) for r in read_log(attempt / "stdin.jsonl")] == [
            (1, "failed", errno.ECANCELED)]
        sender.join(10)
        assert len(answers) == 1 and isinstance(answers[0], RelayError), answers
    finally:
        if write_end is not None:
            os.close(write_end)
        os.close(read_end)
        _end_parked_guardian(sock, process)
