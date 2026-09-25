"""The daemon keeps serving when it runs short of descriptors, and never queues a
client until it gives up (2026-09-25: launchd's soft limit of 256 descriptors,
32 reader threads, and `accept` failing with EMFILE stopped the daemon)."""

from __future__ import annotations

import errno
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from subfleet import daemon as module
from subfleet.daemon import Daemon


def until(predicate, timeout=5.0):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("condition timed out")


def ping(path: Path, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        try:
            client.sendall(b'{"v":1,"id":"p","op":"ping","args":{}}\n')
        except BrokenPipeError:
            pass            # a busy daemon answers and closes before reading
        return json.loads(client.makefile().readline())


def listening(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
        return True
    except OSError:
        return False


@pytest.fixture
def serve(monkeypatch):
    """Start a daemon's `serve_forever` on a short socket path; yield a starter so a
    test can patch the module first."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    started = []
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        root = Path(temporary)
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(root / "probe"))
        except PermissionError:
            pytest.skip("sandbox denies Unix socket binding")

        def start():
            daemon = Daemon(root, tick_s=.05)
            thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            thread.start()
            started.append((daemon, thread))
            until(lambda: listening(root / "daemon.sock"))  # the file exists before `listen`
            return daemon, root / "daemon.sock", thread

        yield start
        for daemon, thread in started:
            daemon.stopping.set()
            thread.join(3)


def test_accept_out_of_descriptors_keeps_the_daemon_serving(serve, monkeypatch):
    """EMFILE from `accept` is a moment's shortage: the daemon says so in its log
    and serves the connection once it can (before, `serve_forever` re-raised it
    and the daemon exited)."""
    real = socket.socket.accept
    failures = {"left": 3}

    def accept(self):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError(errno.EMFILE, "Too many open files")
        return real(self)

    monkeypatch.setattr(socket.socket, "accept", accept)
    daemon, path, thread = serve()
    reply = ping(path)
    assert reply["ok"] and failures["left"] == 0 and thread.is_alive()
    assert "accept failed" in (daemon.root / "daemon.log").read_text()


def test_a_shortage_that_never_clears_ends_the_daemon_without_spinning(monkeypatch):
    """`accept` failing without a break for `ACCEPT_GIVE_UP_S` is a leak, not a
    moment: the daemon exits so launchd starts a fresh one, and meanwhile it tries
    at most about five times a second."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    monkeypatch.setattr(module, "ACCEPT_GIVE_UP_S", 1.0)
    calls = []

    def accept(self):
        calls.append(time.monotonic())
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(socket.socket, "accept", accept)
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(Path(temporary), tick_s=.05)
        started = time.monotonic()
        with pytest.raises(OSError) as raised:
            daemon.serve_forever()
        took = time.monotonic() - started
        log = (Path(temporary) / "daemon.log").read_text()
    assert raised.value.errno == errno.EMFILE and 1.0 <= took < 5
    assert len(calls) <= took / .2 + 2, (len(calls), took)
    assert "exiting so a fresh daemon starts" in log


def test_idle_connections_past_the_old_pool_never_starve_a_request(serve):
    """The core of the fix: 40 connections held open (more than the 32 readers the
    daemon had) and a ping is still answered at once; every pool a connection's
    request may wait in is as large as the connection cap."""
    daemon, path, _ = serve()
    assert daemon.readers._max_workers >= module.MAX_CONNECTIONS
    assert daemon.waiters._max_workers >= module.MAX_CONNECTIONS
    idle = [socket.socket(socket.AF_UNIX) for _ in range(40)]
    try:
        for client in idle:
            client.connect(str(path))
        until(lambda: len(daemon._connections) == 40)
        started = time.monotonic()
        assert ping(path, timeout=3)["ok"] and time.monotonic() - started < 1
    finally:
        for client in idle:
            client.close()


def test_accept_raises_what_is_not_a_shortage(monkeypatch):
    """Any other error still ends the loop, as before: it is not retried blindly."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")

    def accept(self):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(socket.socket, "accept", accept)
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(Path(temporary), tick_s=.05)
        with pytest.raises(OSError) as raised:
            daemon.serve_forever()
    assert raised.value.errno == errno.EBADF


def test_a_connection_past_the_cap_is_told_the_daemon_is_busy_at_once(serve, monkeypatch):
    """Each open connection holds a reader; one past `MAX_CONNECTIONS` gets an
    answer (exit 69, try again) at once instead of waiting out its timeout, and is
    served again once a connection closes."""
    monkeypatch.setattr(module, "MAX_CONNECTIONS", 2)
    daemon, path, _ = serve()
    idle = [socket.socket(socket.AF_UNIX) for _ in range(2)]
    try:
        for client in idle:
            client.connect(str(path))
        until(lambda: len(daemon._connections) == 2)       # the readiness probes have closed
        started = time.monotonic()
        refused = ping(path)
        assert time.monotonic() - started < 2
        assert refused["ok"] is False and refused["error"]["code"] == 69
        assert "serving 2 connections" in refused["error"]["message"]
        assert "telling new clients the daemon is busy" in (daemon.root / "daemon.log").read_text()
        # The CLI's client reads the answer even when its write lost the race with
        # the daemon's close (review F1: it reported exit 1, "Broken pipe").
        from subfleet.client import Client, DaemonError
        client = Client(daemon.root, timeout=3)
        client._checked = True           # the lock records the fixture's fake boot id
        for _ in range(30):
            with pytest.raises(DaemonError) as busy:
                client.call("ping")
            assert busy.value.code == 69
        # A client descheduled between connect and send (as under load) always
        # loses that race; it still reads the busy answer.
        import subfleet.client as client_module
        real_encode = client_module.encode
        monkeypatch.setattr(client_module, "encode", lambda request: (time.sleep(.1), real_encode(request))[1])
        for _ in range(3):
            with pytest.raises(DaemonError) as busy:
                client.call("ping")
            assert busy.value.code == 69
        monkeypatch.setattr(client_module, "encode", real_encode)
        idle.pop().close()
        until(lambda: len(daemon._connections) == 1)
        assert ping(path)["ok"]
    finally:
        for client in idle:
            client.close()


def run_limit(code: str, soft: int = 256, hard: int | None = None) -> list[int]:
    """Run `code` in a fresh interpreter whose limits start at (soft, hard), by
    default launchd's 256 and the current hard limit; it prints numbers."""
    prelude = ("import resource; _, h = resource.getrlimit(resource.RLIMIT_NOFILE); "
               f"resource.setrlimit(resource.RLIMIT_NOFILE, ({soft}, {hard if hard is not None else 'h'})); "
               "from subfleet.daemon import raise_open_file_limit; ")
    out = subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[2])
    return [int(x) for x in out.stdout.split()]


def test_the_descriptor_soft_limit_is_raised_but_never_past_the_hard_limit():
    import resource
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    want = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
    report = "print(raise_open_file_limit(4096)[0], resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
    soft, now = run_limit(report)
    assert soft == now == max(256, want)
    assert run_limit(report, 256, 1024) == [1024, 1024]          # never past a finite hard limit
    assert run_limit(report, 256, 300) == [300, 300]
    assert run_limit(report, 5000) == [5000, 5000]               # a higher soft limit is never lowered


def test_main_raises_the_limit_before_it_starts_the_daemon(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "raise_open_file_limit", lambda want=module.OPEN_FILES: calls.append(want) or (want, want))

    class Refused:
        def __init__(self, root):
            calls.append("daemon")
            raise module.DaemonUnavailable("another daemon holds daemon.lock")

    monkeypatch.setattr(module, "Daemon", Refused)
    assert module.main(["--state-root", "/nonexistent"]) == 69
    assert calls == [module.OPEN_FILES, "daemon"] and module.OPEN_FILES == 4096
