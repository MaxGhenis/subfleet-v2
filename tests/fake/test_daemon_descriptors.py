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
        client.sendall(b'{"v":1,"id":"p","op":"ping","args":{}}\n')
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
        idle.pop().close()
        until(lambda: len(daemon._connections) == 1)
        assert ping(path)["ok"]
    finally:
        for client in idle:
            client.close()


def run_limit(code: str) -> list[int]:
    """Run `code` in a fresh interpreter whose soft limit starts at 256, as under
    launchd; it prints numbers."""
    prelude = ("import resource; soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE); "
               "resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard)); "
               "from subfleet.daemon import raise_open_file_limit; ")
    out = subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[2])
    return [int(x) for x in out.stdout.split()]


def test_the_descriptor_soft_limit_is_raised_but_never_past_the_hard_limit():
    import resource
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    want = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
    soft, now = run_limit("print(raise_open_file_limit(4096)[0], resource.getrlimit(resource.RLIMIT_NOFILE)[0])")
    assert soft == now == max(256, want)
    # A request the kernel refuses (past its own per-process cap) leaves the limit as it was.
    soft, now = run_limit("print(raise_open_file_limit(10**12)[0], resource.getrlimit(resource.RLIMIT_NOFILE)[0])")
    assert soft == now and soft >= 256


def test_main_raises_the_limit_before_it_starts_the_daemon(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "raise_open_file_limit", lambda: calls.append("limit") or (4096, 4096))

    class Refused:
        def __init__(self, root):
            calls.append("daemon")
            raise module.DaemonUnavailable("another daemon holds daemon.lock")

    monkeypatch.setattr(module, "Daemon", Refused)
    assert module.main(["--state-root", "/nonexistent"]) == 69
    assert calls == ["limit", "daemon"]
