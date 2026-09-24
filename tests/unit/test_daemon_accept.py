"""C-16.5: a failed `accept` for want of a resource is waited out, and the daemon asks for room at start.

Incident, 2026-09-24: with the machine at a load average near 72, sessions retried
`status` every 15 s against replies that took minutes. Each open connection held a
descriptor, the daemon reached launchd's soft limit of 256, `accept` raised
EMFILE, and the uncaught error ended `serve_forever`.
"""

import errno
import json
import resource
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon


@pytest.fixture
def serving(monkeypatch):
    """A real daemon core on a short temp root, whose listening socket fails `accept` on demand."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    monkeypatch.setattr(daemon_module, "ACCEPT_RETRY_BASE_S", .01)
    failures: list[int] = []

    class Flaky(socket.socket):
        def accept(self):
            if failures:
                raise OSError(failures.pop(0), "injected")
            return super().accept()

    monkeypatch.setattr(daemon_module.socket, "socket", Flaky)
    with tempfile.TemporaryDirectory(prefix="sfa-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(root / "socket-check"))
        except PermissionError:
            pytest.skip("sandbox denies unix socket binding")
        service = Daemon(root, tick_s=.05)
        service._recovery_complete.set()          # nothing here needs recovery or timers
        service._control = lambda: service.stopping.wait()
        raised: list[BaseException] = []

        def serve():
            try:
                service.serve_forever()
            except BaseException as exc:          # what ended it is the test's to judge
                raised.append(exc)
        thread = threading.Thread(target=serve, daemon=True)
        thread.raised = raised
        try:
            yield service, thread, failures
        finally:
            service.stopping.set()
            thread.join(5)


def ping(root: Path) -> dict:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(str(root / "daemon.sock"))
        sock.sendall(b'{"v":1,"id":"t","op":"ping","args":{}}\n')
        return json.loads(sock.makefile("rb").readline())


def started(service, thread):
    thread.start()
    deadline = time.monotonic() + 5
    while not (service.root / "daemon.sock").exists():
        assert time.monotonic() < deadline, "the daemon never bound its socket"
        time.sleep(.01)


def test_c16_5_emfile_on_accept_is_waited_out_and_the_daemon_keeps_serving(serving):
    """C-16.5 the incident: EMFILE from `accept` no longer ends `serve_forever`."""
    service, thread, failures = serving
    failures.extend([errno.EMFILE] * 5 + [errno.ENFILE, errno.ECONNABORTED])
    started(service, thread)
    deadline = time.monotonic() + 5
    while failures:
        assert thread.is_alive(), "serve_forever ended on a transient accept failure"
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert ping(service.root)["result"]["pong"] is True
    assert thread.is_alive()
    log = (service.root / "daemon.log").read_text()
    # Logged on the 1st, 2nd and 4th consecutive failure, not the 3rd or 5th, then once on recovery.
    assert [line.split(" (")[1].split(" in a row")[0] for line in log.splitlines()
            if line.startswith("accept failed")] == ["1", "2", "4"]
    assert "accept failed: EMFILE" in log and "accept recovered after 7 failures" in log


def test_c16_5_any_other_accept_error_still_ends_serve_forever(serving):
    """C-16.5 only a shortage is waited out; a socket that is gone is not retried for ever."""
    service, thread, failures = serving
    failures.append(errno.EBADF)
    thread.start()                     # it ends at once and removes its socket on the way out
    thread.join(5)
    assert not thread.is_alive() and service.stopping.is_set()
    assert [getattr(exc, "errno", None) for exc in thread.raised] == [errno.EBADF]


def test_c16_5_the_soft_open_file_limit_is_raised_within_the_hard_limit(monkeypatch):
    """C-16.5 the daemon asks for 8192 descriptors, never more than the hard limit allows."""
    calls = []
    limits = {"soft": 256, "hard": resource.RLIM_INFINITY}
    monkeypatch.setattr(daemon_module.resource, "getrlimit", lambda which: (limits["soft"], limits["hard"]))
    monkeypatch.setattr(daemon_module.resource, "setrlimit", lambda which, value: calls.append(value))
    assert daemon_module.raise_open_file_limit() == (256, 8192)
    assert calls == [(8192, resource.RLIM_INFINITY)]
    calls.clear()
    limits.update(soft=256, hard=1000)
    assert daemon_module.raise_open_file_limit() == (256, 1000) and calls == [(1000, 1000)]
    calls.clear()
    limits.update(soft=10240, hard=resource.RLIM_INFINITY)
    assert daemon_module.raise_open_file_limit() == (10240, 10240) and calls == []     # already enough


def test_c16_5_a_limit_above_the_kernel_ceiling_falls_back_rather_than_failing(monkeypatch):
    """C-16.5 macOS refuses a soft limit above kern.maxfilesperproc; the next lower one is taken."""
    attempts = []

    def refuse_high(which, value):
        attempts.append(value[0])
        if value[0] > 4096:
            raise ValueError("not allowed to raise maximum limit")
    monkeypatch.setattr(daemon_module.resource, "getrlimit", lambda which: (256, resource.RLIM_INFINITY))
    monkeypatch.setattr(daemon_module.resource, "setrlimit", refuse_high)
    assert daemon_module.raise_open_file_limit() == (256, 4096)
    assert attempts == [8192, 4096]


def test_c16_5_the_real_limit_can_be_raised_in_this_process():
    """C-16.5 on this machine the call succeeds and leaves at least what it found."""
    before, after = daemon_module.raise_open_file_limit()
    assert after >= before and resource.getrlimit(resource.RLIMIT_NOFILE)[0] >= min(after, 8192)
