"""C-16.6: a failed `accept` for want of a resource is waited out, and the daemon asks for room at start.

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


def test_c16_6_emfile_on_accept_is_waited_out_and_the_daemon_keeps_serving(serving):
    """C-16.6 the incident: EMFILE from `accept` no longer ends `serve_forever`."""
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


def test_c16_6_any_other_accept_error_still_ends_serve_forever(serving):
    """C-16.6 only a shortage is waited out; a socket that is gone is not retried for ever."""
    service, thread, failures = serving
    failures.append(errno.EBADF)
    thread.start()                     # it ends at once and removes its socket on the way out
    thread.join(5)
    assert not thread.is_alive() and service.stopping.is_set()
    assert [getattr(exc, "errno", None) for exc in thread.raised] == [errno.EBADF]


def test_c16_6_main_raises_the_limit_before_the_daemon_sizes_its_connection_cap(monkeypatch):
    """C-16.6 `main` lifts the open-file limit first, so C-16.7's cap comes from the raised value."""
    order = []
    monkeypatch.setattr(daemon_module.descriptors, "raise_open_file_limit",
                        lambda: order.append("raise") or (256, 65536, resource.RLIM_INFINITY))

    class Stop(Exception):
        pass

    def construct(root):
        order.append("daemon")
        raise Stop
    monkeypatch.setattr(daemon_module, "Daemon", construct)
    with pytest.raises(Stop):
        daemon_module.main(["--state-root", "/nonexistent-root"])
    assert order == ["raise", "daemon"]


def test_c16_6_the_start_log_says_what_the_raise_achieved():
    """C-16.6 raised: an info line with both values; stuck below the target: a warning naming the hard limit."""
    import logging
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append((record.levelname, record.getMessage()))
    log = logging.getLogger("test-c16-5")
    log.addHandler(Keep())
    log.setLevel(logging.INFO)
    daemon_module.log_open_file_limit(log, 256, 65536, resource.RLIM_INFINITY)
    daemon_module.log_open_file_limit(log, 1024, 1024, 1024)
    daemon_module.log_open_file_limit(log, 1048576, 1048576, resource.RLIM_INFINITY)
    assert records == [("INFO", "open-file limit raised from 256 to 65536 at start"),
                       ("WARNING", "open-file limit left at 1024: the hard limit (1024) or the kernel allows no more")]
