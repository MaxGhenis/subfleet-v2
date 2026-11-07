"""Catalog close revokes later Owner checks and reaps every run it starts, including
a reserved child handed over after close (C-30.1, design D-23). Operations following
an earlier successful Owner check retain the contract's check-then-act window.

2026-09-25: `ConversationService.close()` left the detached catalog process running.
It finished after its owner's teardown had removed the state root, recreated the
root and wrote `catalog.json` and `catalog-cache.json` into it (69 `/tmp/sfd-*`
directories at the review), or wrote while `rmtree` was still emptying it
(`OSError: [Errno 66] Directory not empty`, about one teardown in 30).

Catalog children here are real `python -m subfleet.conversations.catalog`; the
starter ownership property separately simulates spawn with real pipes and Owner.
The FIFO fixture places one Codex rollout under the isolated HOME and holds it
open read-write from the start, with one blank line in it. A run opens it at once, reads the line
and waits for more: once the line is gone (`Runs.hold`) the run is past its lock
and every check before its scan, and publishes nothing until `Runs.let_go` closes
the test's end. Neither step races: a writer is there before the run opens the
FIFO, and the test lets go only after the run has read from it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon as daemon_module, guardian
from subfleet.conversations import catalog as catalog_module
from subfleet.conversations import service as service_module
from subfleet.daemon import Daemon

REPO = Path(__file__).resolve().parents[2]
ROLLOUT = "rollout-2026-09-25T00-00-00-0f0e0d0c-1111-2222-3333-444455556666.jsonl"


def until(predicate, timeout=30.0, what="condition"):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError(f"{what} timed out")


class Runs:
    """The catalog runs a test starts, and the FIFO held open until `let_go`; whatever
    is still alive or open at the end is killed and closed."""

    def __init__(self, fifo: Path):
        self.fifo = fifo
        self.processes: list[subprocess.Popen] = []
        self.writers = [os.open(fifo, os.O_RDWR | os.O_NONBLOCK)]
        os.write(self.writers[0], b"\n")       # a blank line: a run reads it and skips it

    def spawn(self, root: Path, fence_fd: int | None = None) -> subprocess.Popen:
        process = catalog_module.spawn_refresh(root, fence_fd=fence_fd)
        assert process is not None, "another run held the lock"
        return self.track(process)

    def track(self, process: subprocess.Popen) -> subprocess.Popen:
        self.processes.append(process)
        return process

    def unread(self) -> bool:
        return bool(select.select([self.writers[0]], [], [], 0)[0])

    def hold(self) -> None:
        """Wait until a run has read the FIFO's line: it is then waiting in its read."""
        until(lambda: not self.unread(), what="a catalog run to read the FIFO")

    def let_go(self, process: subprocess.Popen | None = None) -> int | None:
        """Close the test's end of the FIFO, so a run reading it reads end-of-file and
        goes on; with a process, reap it."""
        while self.writers:
            os.close(self.writers.pop())
        return None if process is None else process.wait(30)

    def cleanup(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass
                process.wait(30)
        self.let_go()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HOME: no Claude transcripts, and one Codex rollout that is a FIFO."""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    day = home / ".codex" / "sessions" / "2026" / "09" / "25"
    day.mkdir(parents=True)
    fifo = day / ROLLOUT
    os.mkfifo(fifo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home / ".claude"))
    monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(home / ".codex"))
    return SimpleNamespace(path=home, fifo=fifo)


@pytest.fixture
def runs(home):
    runs = Runs(home.fifo)
    try:
        yield runs
    finally:
        runs.cleanup()


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    return root


@pytest.fixture
def fence():
    """An owner's fence pipe as `ConversationService` keeps one: [read, write]."""
    ends = list(os.pipe())
    try:
        yield ends
    finally:
        for fd in ends:
            if fd is not None:
                os.close(fd)


def close_write_end(fence) -> None:
    os.close(fence[1])
    fence[1] = None


# --- a run on its own --------------------------------------------------------------


def test_a_run_writes_the_catalog_while_its_owner_is_open(runs, root, fence):
    """The control case: with the fence's write end open, a run writes both files."""
    process = runs.spawn(root, fence[0])
    runs.hold()
    assert runs.let_go(process) == 0
    assert sorted(os.listdir(root)) == ["catalog-cache.json", "catalog.json", "catalog.lock"]


def test_a_run_whose_owner_closes_mid_run_writes_nothing(runs, root, fence):
    """C-30.1: the service closes (its fence's write end with it) while the run
    reads; the run finishes reading, publishes nothing and exits OWNER_GONE (after
    close() no service tracks it, so nothing logs that)."""
    process = runs.spawn(root, fence[0])
    runs.hold()
    close_write_end(fence)
    assert runs.let_go(process) == catalog_module.OWNER_GONE
    assert sorted(os.listdir(root)) == ["catalog.lock"]


def test_a_run_started_after_its_owner_closed_touches_nothing(runs, root, fence):
    """C-30.1: a run whose owner had already closed ends at once, before it takes the
    lock, opens `state.sqlite3` or reads a session: it cannot write into the root, not
    even its lock file, and exits OWNER_GONE. The parent's probe also writes nothing."""
    close_write_end(fence)
    process = runs.spawn(root, fence[0])
    assert process.wait(30) == catalog_module.OWNER_GONE
    assert os.listdir(root) == [] and runs.unread()           # it read no session


def test_a_run_whose_root_is_removed_never_brings_it_back(runs, root):
    """2026-09-25: a run that outlived its owner recreated the removed root. Now it
    finds its lock gone, writes nothing, never makes a directory, and exits OWNER_GONE."""
    process = runs.spawn(root)
    runs.hold()
    shutil.rmtree(root)
    assert runs.let_go(process) == catalog_module.OWNER_GONE
    assert not root.exists()


def test_a_run_whose_root_is_replaced_writes_nothing_into_the_new_one(runs, root):
    """A new state root is not the one the run locked. A new daemon's probe leaves
    that root empty, and the old run cannot publish into it."""
    process = runs.spawn(root)
    runs.hold()
    shutil.rmtree(root)
    root.mkdir()
    assert catalog_module.refresh_running(root) is False     # the old run holds only the old lock
    assert runs.let_go(process) == catalog_module.OWNER_GONE
    assert os.listdir(root) == []


def test_a_stopped_run_unwinds_and_leaves_only_its_lock(runs, root):
    """SIGTERM raises SystemExit in the run, so `atomic_publish` removes its temporary
    file on the way out; the exit status says it unwound (128 + 15). A signal that
    lands just before the run enters its read of the FIFO interrupts nothing, and
    Python runs the handler when that read returns: letting the FIFO go covers both
    orders, and the handler still runs long before anything is published. (A read
    that never returns is what close()'s SIGKILL is for.)"""
    process = runs.spawn(root)
    runs.hold()
    os.killpg(process.pid, 15)
    assert runs.let_go(process) == 143
    assert sorted(os.listdir(root)) == ["catalog.lock"]


def test_publishing_never_makes_the_state_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        catalog_module._atomic(tmp_path / "absent" / "catalog.json", {})
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("failure", [errno.ENOENT, errno.EACCES, errno.ENOSPC])
@pytest.mark.parametrize("pending_term", [False, True])
def test_atomic_error_cleans_temp_and_restores_sigterm_deferral(tmp_path, monkeypatch, failure, pending_term):
    def failing_fsync(fd):
        if pending_term:
            catalog_module._terminated(signal.SIGTERM, None)
        raise OSError(failure, os.strerror(failure))

    with monkeypatch.context() as patch:
        patch.setattr(guardian.os, "fsync", failing_fsync)
        with pytest.raises(SystemExit if pending_term else OSError) as caught:
            catalog_module._atomic(tmp_path / "catalog.json", {})
    if pending_term:
        assert caught.value.code == 143
    else:
        assert caught.value.errno == failure
    assert list(tmp_path.iterdir()) == []
    # Neither an error nor a deferred TERM leaves a stale flag for a later call.
    catalog_module._atomic(tmp_path / "catalog.json", {})
    with pytest.raises(SystemExit, match="143"):
        catalog_module._terminated(signal.SIGTERM, None)


def test_nested_publish_defers_sigterm_until_the_outer_cleanup(tmp_path, monkeypatch):
    events = []

    def publish(path, data):
        try:
            if path.name == "outer":
                catalog_module._atomic(tmp_path / "inner", {})
                events.append("outer continued")
            else:
                catalog_module._terminated(signal.SIGTERM, None)
        finally:
            events.append(f"{path.name} cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(guardian, "atomic_publish", publish)
        with pytest.raises(SystemExit, match="143"):
            catalog_module._atomic(tmp_path / "outer", {})
    assert events == ["inner cleanup", "outer continued", "outer cleanup"]
    catalog_module._atomic(tmp_path / "catalog.json", {})


def test_a_run_without_a_state_root_makes_none(home, tmp_path):
    absent = tmp_path / "absent"
    done = subprocess.run([sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(absent)],
                          capture_output=True, text=True, timeout=60, cwd=REPO)
    assert done.returncode == catalog_module.OWNER_GONE, done.stderr
    assert not absent.exists()


@pytest.mark.parametrize("how", ["replaced by the run's stdin", "never passed"])
def test_a_run_that_cannot_read_its_fence_writes_nothing_and_says_so(runs, root, fence, how):
    """Review of #47: a run whose `--fence-fd` is not the pipe its owner made cannot
    tell whether that owner is open. It writes nothing, not even its lock, reads no
    session, and exits FENCE_BROKEN rather than 0, so the service logs it. (A fence at
    fd 0 was replaced by the run's stdin redirect; the run read end-of-file and exited
    0, and the catalog went stale without a word.)"""
    number = "0" if how == "replaced by the run's stdin" else str(fence[0])
    process = runs.track(subprocess.Popen(
        [sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(root), "--fence-fd", number],
        cwd=REPO, pass_fds=(), start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    assert process.wait(30) == catalog_module.FENCE_BROKEN
    assert os.listdir(root) == [] and runs.unread()


def test_spawning_refuses_a_fence_the_runs_standard_streams_would_replace(root):
    """The run's stdin, stdout and stderr are /dev/null: a fence at 0, 1 or 2 would be
    replaced by that redirect, so `spawn_refresh` refuses one before it touches the root."""
    for fd in (0, 1, 2):
        with pytest.raises(ValueError):
            catalog_module.spawn_refresh(root, fence_fd=fd)
    assert os.listdir(root) == []


# The service's process with some of fds 0, 1 and 2 closed (argv[3]) starts its run and
# reports its fence and the run's exit status in argv[2], a file opened while all three
# were still open. It writes nothing to stdout or stderr, which may be closed or the fence.
FENCE_CHILD = r"""
import json, logging, os, sys, traceback, types
from pathlib import Path
root, closed = Path(sys.argv[1]), [int(n) for n in sys.argv[3].split(",") if n]
report, result = open(sys.argv[2], "w"), {"log": []}
log = logging.getLogger("fence-child")
log.propagate = False
log.addHandler(type("Keep", (logging.Handler,), {"emit": lambda self, r: result["log"].append(r.getMessage())})())
try:
    from subfleet.conversations.service import ConversationService
    service = ConversationService(types.SimpleNamespace(root=root, log=log))
    for fd in closed:
        os.close(fd)
    result["started"] = service._start_catalog()
    result["fence"] = list(service._catalog_fence or ())
    process = service._catalog_proc
    result["rc"] = None if process is None else process.wait(60)
    service.close()
except BaseException:
    result["error"] = traceback.format_exc()
report.write(json.dumps(result))
report.close()
"""


@pytest.fixture
def plain_home(tmp_path, monkeypatch):
    """An isolated HOME with nothing to index, so a run finishes at once."""
    home = tmp_path / "plain-home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home / ".claude"))
    return home


@pytest.mark.parametrize("recreate", [False, True], ids=["intact-root", "recreated-root"])
def test_a_child_launched_after_close_finishes_cannot_take_its_first_lock(
        plain_home, root, monkeypatch, recreate):
    """Hold the real Popen beyond the handoff, then keep the starter from stopping
    its child until that child has finished. Eventual TERM cannot hide a write."""
    monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(plain_home / ".codex"))
    service = service_module.ConversationService(SimpleNamespace(root=root, log=logging.getLogger("late-catalog")))
    entered, release = threading.Event(), threading.Event()
    popen = subprocess.Popen
    children, errors = [], []

    def held_popen(*args, **kwargs):
        entered.set()
        assert release.wait(60), "the test never released Popen"
        process = popen(*args, **kwargs)
        children.append(process)
        process.wait(60)                   # withhold spawn_refresh's return until exit
        return process

    def start():
        try:
            service._start_catalog()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(catalog_module.subprocess, "Popen", held_popen)
    starter = threading.Thread(target=start, name="late-catalog-starter")
    try:
        starter.start()
        assert entered.wait(30), "the starter never reached Popen"
        service.close()                   # spend the real one-second handoff deadline
        assert service._catalog_stop_abandoned and service._catalog_starting
        if recreate:
            shutil.rmtree(root)
            root.mkdir()
        before = _entry_snapshot(root)
        release.set()
        starter.join(90)
        assert not starter.is_alive() and not errors, errors
        assert len(children) == 1
        observed = {"returncode": children[0].returncode, "entries": sorted(os.listdir(root))}
        assert children[0].returncode == catalog_module.OWNER_GONE, observed
        assert not (root / "catalog.lock").exists(), observed
        assert _entry_snapshot(root) == before, observed
        assert service._catalog_proc is None and service._catalog_fence is None
    finally:
        release.set()
        starter.join(90)
        for process in children:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(30)
        service.close()


@pytest.mark.parametrize("raises", [False, True], ids=["declined", "spawn-error"])
def test_listing_and_failed_starts_leave_no_parent_lock(root, monkeypatch, raises):
    from tests.unit.test_conversation_service import FakeDaemon

    daemon = FakeDaemon(root)
    service = service_module.ConversationService(daemon)

    def spawn(*args, **kwargs):
        if raises:
            raise OSError(errno.EIO, "test spawn failed")
        return None

    monkeypatch.setattr(catalog_module, "spawn_refresh", spawn)
    try:
        assert service.op_conversation_list({}, None)["catalog"]["refreshing"] is False
        assert service._start_catalog() == {"requested": False, "running": False}
        assert not (root / "catalog.lock").exists()
    finally:
        service.close()
        daemon.store.close()


@pytest.mark.parametrize("closed", [(), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)],
                         ids=lambda closed: "closed-" + ("".join(map(str, closed)) or "none"))
def test_the_fence_stays_above_the_standard_streams_whatever_the_service_has_closed(plain_home, root, tmp_path,
                                                                                    closed):
    """Review of #47: `os.pipe()` returns the lowest free descriptors. In a daemon with
    fd 0 closed the fence's read end was 0; the run's stdin redirect replaced it there,
    and the run read end-of-file, wrote nothing and exited as if its owner had closed.
    A write end at 1 or 2 would also take the daemon's own output. For every set of
    0, 1 and 2 the service's process may have closed, both ends sit above 2 and the
    real run writes the catalog."""
    out = tmp_path / "fence.json"
    done = subprocess.run([sys.executable, "-c", FENCE_CHILD, str(root), str(out), ",".join(map(str, closed))],
                          cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    result = json.loads(out.read_text()) if out.exists() else {}
    assert done.returncode == 0 and result and "error" not in result, (done.returncode, done.stderr, result)
    assert result["started"] == {"requested": True, "running": True}, result
    assert len(result["fence"]) == 2 and min(result["fence"]) > 2, result
    assert result["rc"] == 0 and (root / "catalog.json").is_file(), (result, sorted(os.listdir(root)))


# --- the daemon ------------------------------------------------------------------


@pytest.fixture
def tracked_catalog(monkeypatch):
    """A still-unreaped child and a clock the test advances; waiting is always a bug."""
    clock = SimpleNamespace(now=1000.0)
    process = SimpleNamespace(pid=4_000_001, returncode=None)
    process.poll = lambda: process.returncode

    def no_wait(*args, **kwargs):
        raise AssertionError("the catalog reaper must never wait")

    process.wait = no_wait
    service = object.__new__(service_module.ConversationService)
    service._catalog_lock = threading.RLock()
    service._catalog_revoke_lock = threading.Lock()
    service._catalog_starting = False
    service._catalog_terminating = False
    service._catalog_handoff = threading.Event()
    service._catalog_handoff.set()
    service._catalog_close_started = None
    service._catalog_stop_abandoned = False
    service._catalog_proc = process
    service._catalog_started = clock.now
    service._catalog_terminated_at = None
    service._catalog_killed = False
    service._catalog_fence = None
    service._closed = False
    service.clock = lambda: clock.now
    service.log = logging.getLogger("test-catalog-reaper")
    signals = []
    monkeypatch.setattr(service_module.os, "killpg", lambda pid, sig: signals.append((pid, sig, clock.now)))
    return service, process, clock, signals


def test_a_wedged_run_gets_sigterm_then_a_full_grace_before_sigkill(tracked_catalog, caplog):
    """C-30.1: even a late tick starts a full grace at SIGTERM; no tick ever waits,
    repeats a signal, or sends one after it has reaped the run's group leader."""
    service, process, clock, signals = tracked_catalog
    start = clock.now
    with caplog.at_level(logging.WARNING, logger="test-catalog-reaper"):
        clock.now = start + service_module.CATALOG_KILL_AFTER_S - .001
        service._reap_catalog()
        assert signals == []
        clock.now += 100                 # the first late tick can be arbitrarily late
        terminated_at = clock.now
        service._reap_catalog()
        assert signals == [(process.pid, signal.SIGTERM, terminated_at)]
        service._reap_catalog()          # later invocation, same clock: no immediate kill
        clock.now += service_module.CATALOG_TERM_GRACE_S - .001
        service._reap_catalog()
        assert signals == [(process.pid, signal.SIGTERM, terminated_at)]
        clock.now = terminated_at + service_module.CATALOG_TERM_GRACE_S
        service._reap_catalog()
        assert signals == [(process.pid, signal.SIGTERM, terminated_at),
                           (process.pid, signal.SIGKILL, clock.now)]
        clock.now += 1000
        service._reap_catalog()
        assert len(signals) == 2
        process.returncode = -signal.SIGKILL
        service._reap_catalog()
        service._reap_catalog()
        assert service._catalog_proc is None and len(signals) == 2
    assert len(caplog.records) == 2
    assert "sending SIGTERM" in caplog.records[0].getMessage()
    assert "sending SIGKILL" in caplog.records[1].getMessage()


def test_a_run_that_unwinds_on_sigterm_is_reaped_without_sigkill(tracked_catalog, caplog):
    service, process, clock, signals = tracked_catalog
    clock.now += service_module.CATALOG_KILL_AFTER_S
    with caplog.at_level(logging.WARNING, logger="test-catalog-reaper"):
        service._reap_catalog()
        process.returncode = 128 + signal.SIGTERM
        clock.now += 1000
        service._reap_catalog()
        service._reap_catalog()
    assert signals == [(process.pid, signal.SIGTERM, 1060.0)]
    assert service._catalog_proc is None
    assert len(caplog.records) == 1 and "sending SIGTERM" in caplog.records[0].getMessage()


@pytest.mark.parametrize("term_age", [None, 0.0, 4.0, 10.0, 20.0])
def test_close_honors_the_remaining_sigterm_grace_without_repeating_signals(tracked_catalog, term_age):
    """Closing during a timed stop preserves the original grace; a new stop gets a
    full grace. The fake wait advances time without sleeping or signalling a real pid."""
    service, process, clock, signals = tracked_catalog
    if term_age is not None:
        service._catalog_terminated_at = clock.now - term_age
        signals.append((process.pid, signal.SIGTERM, service._catalog_terminated_at))
    waits = []

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            clock.now += timeout
            raise subprocess.TimeoutExpired("catalog", timeout)
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.wait = wait
    service._stop_catalog()
    expected_wait = max(0.0, service_module.CATALOG_TERM_GRACE_S - (term_age or 0.0))
    assert waits == [expected_wait, service_module.CATALOG_STOP_WAIT_S]
    assert [sig for _, sig, _ in signals] == [signal.SIGTERM, signal.SIGKILL]
    assert signals[1][2] - signals[0][2] >= service_module.CATALOG_TERM_GRACE_S
    assert service._closed and service._catalog_proc is None
    service._stop_catalog()
    assert len(signals) == 2 and len(waits) == 2


def test_close_after_the_reaper_sent_sigkill_never_repeats_it(tracked_catalog):
    service, process, clock, signals = tracked_catalog
    clock.now += service_module.CATALOG_KILL_AFTER_S
    service._reap_catalog()
    clock.now += service_module.CATALOG_TERM_GRACE_S
    service._reap_catalog()
    waits = []

    def wait(timeout):
        waits.append(timeout)
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.wait = wait
    service._stop_catalog()
    assert waits == [service_module.CATALOG_STOP_WAIT_S]
    assert [sig for _, sig, _ in signals] == [signal.SIGTERM, signal.SIGKILL]


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "catalog-close-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "catalog-close-start")


@pytest.fixture
def short_root():
    """A state root under /tmp: a Unix socket path must fit in 104 bytes."""
    root = Path(tempfile.mkdtemp(prefix="sfc-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_closing_the_daemon_stops_its_catalog_run_and_the_removed_root_stays_gone(
        runs, identity, short_root, monkeypatch):
    """The control loop starts a catalog run; the daemon closes while it is in flight.
    `close()` returns only once the run has ended, so no catalog process outlives it,
    and after the root is removed nothing brings it back, even once a surviving run
    could finish its scan and write."""
    root = short_root
    spawn = catalog_module.spawn_refresh

    def tracked_spawn(root, *, fence_fd=None):
        process = spawn(root, fence_fd=fence_fd)
        if process is not None:
            runs.track(process)
        return process

    # Observe every child at creation, including one the service might lose.
    # Cleanup never discovers signal targets through process-name matching.
    monkeypatch.setattr(catalog_module, "spawn_refresh", tracked_spawn)
    try:
        with socket.socket(socket.AF_UNIX) as probe:
            probe.bind(str(root / "probe"))
        (root / "probe").unlink()
    except PermissionError:
        pytest.skip("sandbox denies Unix socket binding")
    daemon = Daemon(root, tick_s=.05)
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    problems: list[str] = []
    try:
        runs.hold()
        until(lambda: daemon.conversations._catalog_proc is not None, what="the service to record its run")
        process = daemon.conversations._catalog_proc
        assert process in runs.processes
        assert process.poll() is None
        daemon.stopping.set()                        # serve_forever's `finally` closes the daemon
        thread.join(30)
        assert not thread.is_alive(), "the daemon did not close"
        stray = [child for child in runs.processes if child.poll() is None]
        if process.poll() is None:
            problems.append("close() returned with its catalog run still running")
        if stray:
            problems.append(f"catalog processes outlived close(): {[child.pid for child in stray]}")
        shutil.rmtree(root)
        runs.let_go()                                # a run that survived now finishes its scan
        for child in stray:
            child.wait(30)
        time.sleep(.5)
        if root.exists():
            problems.append(f"the removed state root came back holding {sorted(os.listdir(root))}")
        assert problems == [], "\n".join(problems)
    finally:
        daemon.stopping.set()
        thread.join(30)
        runs.cleanup()


def test_a_run_that_survives_close_still_writes_nothing(runs, identity, tmp_path, monkeypatch):
    """C-30.1: close() closes the run's fence before it signals, so a run the signals
    never reach (here `killpg` does nothing) publishes nothing once it goes on."""
    monkeypatch.setattr(service_module, "CATALOG_STOP_WAIT_S", 0.2)
    monkeypatch.setattr(service_module, "CATALOG_TERM_GRACE_S", 0.2)
    root = tmp_path / "state"
    daemon = Daemon(root, tick_s=.05)
    try:
        assert daemon.conversations._start_catalog()["requested"] is True
        process = runs.track(daemon.conversations._catalog_proc)
        runs.hold()
        with monkeypatch.context() as patch:
            patch.setattr(service_module.os, "killpg", lambda pid, sig: None)
            daemon.close()
        assert process.poll() is None, "the signals were meant to miss"
        assert runs.let_go(process) == catalog_module.OWNER_GONE
        assert not (root / "catalog.json").exists() and not (root / "catalog-cache.json").exists()
    finally:
        daemon.close()


def _record_fence_closes(monkeypatch, fence):
    close = os.close
    closed = []

    def record(fd):
        if fd in fence:
            closed.append(fd)
        close(fd)

    monkeypatch.setattr(service_module.os, "close", record)
    return closed


@pytest.mark.parametrize("point", ["before-fence", "after-fence"])
def test_close_before_fence_assignment_starts_nothing_and_closes_each_end(root, monkeypatch, point):
    from subfleet import procs

    service = service_module.ConversationService(SimpleNamespace(root=root, log=logging.getLogger("early-catalog")))
    pipe = procs.pipe_above_stdio
    fence = []
    closed = _record_fence_closes(monkeypatch, fence)

    def make_fence():
        if point == "before-fence":
            service.begin_close()
        ends = pipe()
        fence.extend(ends)
        if point == "after-fence":
            service.begin_close()
        return ends

    def unexpected_spawn(*args, **kwargs):
        raise AssertionError("the starter observed close before it could spawn")

    monkeypatch.setattr(procs, "pipe_above_stdio", make_fence)
    monkeypatch.setattr(catalog_module, "spawn_refresh", unexpected_spawn)
    try:
        assert service._start_catalog() == {"requested": False, "running": False}
        service.close()
        assert len(fence) == 2 and sorted(closed) == sorted(fence)
        assert service._catalog_fence is None and service._catalog_proc is None
        for fd in fence:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        service.close()


def test_a_spawn_oserror_during_close_closes_each_fence_end_once(root, monkeypatch):
    service = service_module.ConversationService(SimpleNamespace(root=root, log=logging.getLogger("error-catalog")))
    entered, release = threading.Event(), threading.Event()
    fence, errors, results = [], [], []
    closed = _record_fence_closes(monkeypatch, fence)

    def spawn(*args, **kwargs):
        fence.extend(service._catalog_fence)
        entered.set()
        assert release.wait(30)
        raise OSError(errno.EIO, "test spawn failed during close")

    def start():
        try:
            results.append(service._start_catalog())
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(catalog_module, "spawn_refresh", spawn)
    starter = threading.Thread(target=start)
    try:
        starter.start()
        assert entered.wait(30)
        service.close()
        assert closed == [fence[1]] and service._catalog_fence == (fence[0], None)
        os.fstat(fence[0])
        release.set()
        starter.join(60)
        assert not starter.is_alive() and not errors, errors
        assert results == [{"requested": False, "running": False}]
        assert sorted(closed) == sorted(fence)
        assert service._catalog_fence is None and service._catalog_proc is None
        assert not (root / "catalog.lock").exists()
    finally:
        release.set()
        starter.join(60)
        service.close()


@pytest.mark.parametrize("point", ["before-handoff", "late-handoff", "after-handoff"])
@pytest.mark.parametrize("deliver_term", [True, False], ids=["term-reaches-child", "owner-check-refuses"])
def test_a_real_run_is_reaped_at_each_catalog_handoff(runs, root, monkeypatch, point, deliver_term):
    """A real child already past its first fence read is tracked at TERM and
    reaped, including by a starter that hands it back after full close finished.
    A missed TERM lets the child's later Owner check independently refuse writes."""
    service = service_module.ConversationService(SimpleNamespace(root=root, log=logging.getLogger("handoff-catalog")))
    spawn_refresh, killpg = catalog_module.spawn_refresh, os.killpg
    entered, release = threading.Event(), threading.Event()
    children, errors, signals, reaped = [], [], [], []

    def spawn(root, *, fence_fd):
        process = spawn_refresh(root, fence_fd=fence_fd)
        assert process is not None
        runs.track(process)
        children.append(process)
        poll, wait = process.poll, process.wait

        def observed_poll():
            result = poll()
            if result is not None:
                reaped.append(threading.current_thread())
            return result

        def observed_wait(timeout=None):
            result = wait(timeout)
            reaped.append(threading.current_thread())
            return result

        monkeypatch.setattr(process, "poll", observed_poll)
        monkeypatch.setattr(process, "wait", observed_wait)
        if point != "after-handoff":
            runs.hold()
            entered.set()
            assert release.wait(30)
        return process

    def send(pid, sig):
        assert children and pid == children[0].pid
        assert service._catalog_proc is children[0], "a signal must target the tracked run"
        signals.append(sig)
        if deliver_term:
            killpg(pid, sig)
        runs.let_go()                     # dispatch TERM or reach the later Owner check

    def start():
        try:
            service._start_catalog()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(catalog_module, "spawn_refresh", spawn)
    monkeypatch.setattr(service_module.os, "killpg", send)
    starter = threading.Thread(target=start, name="real-catalog-starter")
    try:
        starter.start()
        if point == "after-handoff":
            starter.join(30)
            assert not starter.is_alive() and not errors, errors
            runs.hold()
            service.close()
        else:
            assert entered.wait(30)
            service.begin_close()
            assert service._catalog_fence[1] is None
            os.fstat(service._catalog_fence[0])
            if point == "late-handoff":
                service.close()
                assert service._catalog_stop_abandoned and starter.is_alive()
            release.set()
            starter.join(30)
            assert not starter.is_alive() and not errors, errors
            service.close()
        assert len(children) == 1 and signals == [signal.SIGTERM]
        assert reaped, "the service must receive the child's exit before cleanup"
        if point == "late-handoff":
            assert starter in reaped, "the late starter must reap its own run"
        assert children[0].returncode == (143 if deliver_term else catalog_module.OWNER_GONE)
        assert service._catalog_proc is None and service._catalog_fence is None
        assert not (root / "catalog.json").exists() and not (root / "catalog-cache.json").exists()
    finally:
        release.set()
        starter.join(60)
        monkeypatch.setattr(service_module.os, "killpg", killpg)
        for process in children:
            if process.poll() is None:
                killpg(process.pid, signal.SIGKILL)
                process.wait(30)
        service.close()


# --- signals during real atomic publishes -------------------------------------------


PUBLISHES = ("catalog.json", "catalog-cache.json")
PUBLISH_PHASES = ("created", "before_write", "partial_write", "full_write", "file_synced",
                  "renamed", "directory_synced")

# sitecustomize runs before the real catalog module. Its pipe descriptors are
# inherited explicitly; there are no polling races, files watched in a loop, or
# sleeps whose duration decides whether a signal interrupts a particular phase.
ATOMIC_PAUSE = r'''
import inspect
import os
import signal
import sys
import threading
from pathlib import Path

from subfleet import guardian

phase = os.environ.get("SF_CATALOG_PAUSE_PHASE")
if phase:
    target = os.environ["SF_CATALOG_PAUSE_TARGET"]
    ready = int(os.environ["SF_CATALOG_PAUSE_READY"])
    release = int(os.environ["SF_CATALOG_PAUSE_RELEASE"])
    paused = False
    thread_sigterm = os.environ.get("SF_CATALOG_THREAD_SIGTERM")
    if thread_sigterm:
        terminate = threading.Event()

        def terminate_from_thread():
            terminate.wait()
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)

        # Start before catalog._atomic: this ordinary thread inherits an unblocked
        # SIGTERM even if the publishing main thread later blocks the signal.
        worker = threading.Thread(target=terminate_from_thread)
        worker.start()

    def pause():
        global paused
        if not paused:
            paused = True
            os.write(ready, b"ready")
            os.read(release, 1)
            if thread_sigterm:
                terminate.set()
                worker.join()             # handle TERM before mkstemp can return

    mkstemp = guardian.tempfile.mkstemp

    def make_temp(*args, **kwargs):
        result = mkstemp(*args, **kwargs)
        if phase == "created" and kwargs.get("prefix") == f".{target}-":
            pause()                       # before guardian's cleanup try starts
        return result

    guardian.tempfile.mkstemp = make_temp

    def publish(path, data):
        def write(stream):
            if phase == "partial_write" and Path(path).name == target:
                middle = max(1, len(data) // 2)
                stream.write(data[:middle])
                stream.flush()
                pause()
                return middle + stream.write(data[middle:])
            return stream.write(data)
        guardian._atomic_write(path, write)

    guardian.atomic_publish = publish
    lines, first = inspect.getsourcelines(guardian._atomic_write)
    source_lines = {
        "before_write": "result = write(stream)",
        "full_write": "stream.flush()",
        "file_synced": "os.rename(temporary, path)",
        "renamed": "dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)",
        "directory_synced": "return result",
    }
    selected = source_lines.get(phase)
    stop_line = next((first + n for n, line in enumerate(lines) if line.strip() == selected), None)
    code = guardian._atomic_write.__code__

    def trace(frame, event, arg):
        if frame.f_code is code:
            if (event == "line" and frame.f_lineno == stop_line
                    and Path(frame.f_locals["path"]).name == target):
                pause()
            return trace
        return None

    sys.settrace(trace)
'''


def _catalog_temps(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir()
                  if re.fullmatch(r"\.(?:catalog\.json|catalog-cache\.json)-[a-z0-9_]{8}", path.name)
                  and stat.S_ISREG(path.lstat().st_mode))


def _entry_snapshot(path: Path):
    """Capture bytes and directory/link structure without following links."""
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return "link", os.readlink(path)
    if stat.S_ISDIR(mode):
        return "directory", sorted((child.name, _entry_snapshot(child)) for child in path.iterdir())
    assert stat.S_ISREG(mode)
    return "file", path.read_bytes()


def _seed_catalog_folder(runs, entries):
    """Seed generated root entries and preserve every object except dead regular temps."""
    targets = runs.base / "link-targets"
    targets.mkdir()
    preserved = []
    for index, (name, kind, data) in enumerate(entries):
        path = runs.root / name
        if kind == "file":
            path.write_bytes(data)
        elif kind == "directory":
            path.mkdir()
            (path / ".catalog.json-nested01").write_bytes(data)
        elif kind == "file_link":
            target = targets / f"file-{index}"
            target.write_bytes(data)
            path.symlink_to(target)
        elif kind == "directory_link":
            target = targets / f"directory-{index}"
            target.mkdir()
            (target / ".catalog-cache.json-nested01").write_bytes(data)
            path.symlink_to(target, target_is_directory=True)
        else:
            assert kind == "dangling_link"
            path.symlink_to(targets / f"absent-{index}")
        if path not in _catalog_temps(runs.root):
            preserved.append((path, _entry_snapshot(path)))
    preserved.append((targets, _entry_snapshot(targets)))
    return preserved


_CATALOG_FOLDER_NAMES = st.one_of(
    st.builds(lambda publish, suffix: f".{publish}-{suffix}", st.sampled_from(PUBLISHES),
              st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=7, max_size=9)),
    st.sampled_from((".catalog.json.tmp", ".CATALOG.JSON-abcdefgh", ".catalog.json-ABCDEF12",
                     ".catalog-cache.json-abc_def!", "catalog.lock.backup", "keep.json")),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12)
      .map(lambda name: f"other-{name}"),
)
_CATALOG_FOLDER_ENTRIES = st.lists(
    st.tuples(_CATALOG_FOLDER_NAMES,
              st.sampled_from(("file", "directory", "file_link", "directory_link", "dangling_link")),
              st.binary(max_size=48)),
    max_size=12, unique_by=lambda entry: entry[0].casefold(),
)
_MIXED_CATALOG_FOLDER = [
    (".catalog.json-previous", "file", b"dead run"),
    (".catalog-cache.json-folder_1", "directory", b"nested bytes"),
    (".catalog.json-linkfile", "file_link", b"linked file bytes"),
    (".catalog-cache.json-link_dir", "directory_link", b"linked directory bytes"),
    (".catalog.json-dangling", "dangling_link", b""),
    (".catalog.json-1234567", "file", b"seven characters"),
    (".catalog.json-123456789", "file", b"nine characters"),
    (".catalog.json-ABCDEF12", "file", b"uppercase suffix"),
]


class PublishingRuns:
    """Isolated homes, a child-only pause seam, and reaping for every child."""

    def __init__(self, base: Path):
        self.base = base
        self.root = base / "state"
        self.root.mkdir()
        home = base / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        (home / ".codex").mkdir()
        seam = base / "seam"
        seam.mkdir()
        (seam / "sitecustomize.py").write_text(ATOMIC_PAUSE)
        self.env = {**os.environ, "HOME": str(home), "SUBFLEET_HOME": str(self.root),
                    "SUBFLEET_CLAUDE_DIR": str(home / ".claude"),
                    "SUBFLEET_CODEX_APP_HOME": str(home / ".codex"),
                    "PYTHONPATH": os.pathsep.join((str(seam), str(REPO)))}

    def command(self) -> list[str]:
        return [sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(self.root)]

    @contextlib.contextmanager
    def paused(self, target: str, phase: str, *, thread_sigterm: bool = False):
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        env = {**self.env, "SF_CATALOG_PAUSE_PHASE": phase, "SF_CATALOG_PAUSE_TARGET": target,
               "SF_CATALOG_PAUSE_READY": str(ready_write), "SF_CATALOG_PAUSE_RELEASE": str(release_read)}
        if thread_sigterm:
            env["SF_CATALOG_THREAD_SIGTERM"] = "1"
        process = None
        try:
            process = subprocess.Popen(self.command(), cwd=REPO, env=env,
                                       pass_fds=(ready_write, release_read), start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.close(ready_write)
            ready_write = None
            os.close(release_read)
            release_read = None
            readable, _, _ = select.select([ready_read], [], [], 30)
            assert readable, f"catalog never reached {target}: {phase}"
            assert os.read(ready_read, 5) == b"ready", process.communicate(timeout=30)[1].decode()
            temps = _catalog_temps(self.root)
            if phase in ("renamed", "directory_synced"):
                assert temps == []
                assert (self.root / target).is_file()
            else:
                assert len(temps) == 1 and temps[0].name.startswith(f".{target}-")
                if phase in ("created", "before_write"):
                    assert temps[0].stat().st_size == 0
                elif phase in ("partial_write", "file_synced"):
                    assert temps[0].stat().st_size > 0
                if phase == "partial_write":
                    with pytest.raises(json.JSONDecodeError):
                        json.loads(temps[0].read_text())
                elif phase == "file_synced":
                    assert isinstance(json.loads(temps[0].read_text()), dict)
            yield process, release_write
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=30)
            for fd in (ready_read, ready_write, release_read, release_write):
                if fd is not None:
                    os.close(fd)

    def complete(self) -> None:
        done = subprocess.run(self.command(), cwd=REPO, env=self.env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=30)
        assert done.returncode == 0, done.stderr.decode()
        assert _catalog_temps(self.root) == []
        assert json.loads((self.root / "catalog.json").read_text())["complete"] is True
        assert isinstance(json.loads((self.root / "catalog-cache.json").read_text()), dict)


@pytest.mark.parametrize("target", PUBLISHES)
@pytest.mark.parametrize("phase", PUBLISH_PHASES)
def test_sigterm_at_each_atomic_publish_phase_leaves_no_temp(tmp_path, target, phase):
    """Invariant 1 includes the tiny creation-before-finally window, both publishes,
    partial/full bytes, each fsync, and rename. Pending TERM may need the pause
    released before it can unwind, because catalog protects the creation window."""
    runs = PublishingRuns(tmp_path)
    with runs.paused(target, phase) as (process, release):
        process.send_signal(signal.SIGTERM)
        try:
            os.write(release, b"go")
        except BrokenPipeError:
            pass                          # an unmasked TERM already unwound
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 128 + signal.SIGTERM, stderr.decode()
        assert _catalog_temps(runs.root) == []


@pytest.mark.parametrize("target", PUBLISHES)
def test_sigterm_from_another_thread_at_temp_creation_leaves_no_temp(tmp_path, target):
    """A second thread receives TERM while mkstemp has made a temp, before the
    publisher's cleanup try starts. Joining it makes Python dispatch the handler
    in that creation seam, even when the main thread has blocked SIGTERM."""
    runs = PublishingRuns(tmp_path)
    with runs.paused(target, "created", thread_sigterm=True) as (process, release):
        os.write(release, b"go")
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 128 + signal.SIGTERM, stderr.decode()
        assert _catalog_temps(runs.root) == []


@settings(max_examples=12, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(interruptions=[("catalog.json", phase) for phase in reversed(PUBLISH_PHASES)],
         entries=_MIXED_CATALOG_FOLDER)
@example(interruptions=[("catalog-cache.json", phase) for phase in reversed(PUBLISH_PHASES)], entries=[])
@given(interruptions=st.lists(st.tuples(st.sampled_from(PUBLISHES), st.sampled_from(PUBLISH_PHASES)),
                              min_size=1, max_size=5), entries=_CATALOG_FOLDER_ENTRIES)
def test_completed_run_sweeps_any_sequence_of_sigkill_interruptions(tmp_path, interruptions, entries):
    """Invariant 2: random killed runs leave empty, partial, or whole writes;
    a completing real catalog run removes all of them from its locked root.
    Generated folders also retain all other bytes, directories, links and targets."""
    with tempfile.TemporaryDirectory(prefix="killed-catalog-", dir=tmp_path) as directory:
        runs = PublishingRuns(Path(directory))
        preserved = _seed_catalog_folder(runs, entries)
        for target, phase in interruptions:
            with runs.paused(target, phase) as (process, _):
                process.kill()
                _, stderr = process.communicate(timeout=30)
                assert process.returncode == -signal.SIGKILL, stderr.decode()
                if phase not in ("renamed", "directory_synced"):
                    assert len(_catalog_temps(runs.root)) == 1
            for path, snapshot in preserved:
                assert _entry_snapshot(path) == snapshot
        runs.complete()
        for path, snapshot in preserved:
            assert _entry_snapshot(path) == snapshot


START_CLOSE_POINTS = (
    "before-allocation", "after-allocation", "before-inheritance",
    "after-inheritance", "before-handoff", "after-handoff",
)


@settings(max_examples=24, deadline=None, database=None)
@given(close_point=st.sampled_from(START_CLOSE_POINTS), close_calls=st.integers(1, 3))
def test_close_at_any_catalog_starter_step_preserves_fence_ownership(close_point, close_calls):
    """C-30.1: every close point revokes future Owner checks, closes each fence
    end once, and keeps the read descriptor reserved until inheritance or no spawn.

    The fake child uses a real Owner and lock identity. Only process creation is
    simulated, so each drawn interleaving needs neither a scheduling race nor a
    child thread. Repeated close claims must preserve the same ownership rules.
    """
    from subfleet import procs

    with tempfile.TemporaryDirectory(prefix="catalog-starter-property-") as directory:
        root = Path(directory)
        lock_fd = os.open(root / "catalog.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        real_close, real_pipe = os.close, procs.pipe_above_stdio
        service = object.__new__(service_module.ConversationService)
        service.root = root
        service.log = logging.getLogger("test-catalog-starter-property")
        service._catalog_lock = threading.RLock()
        service._catalog_revoke_lock = threading.Lock()
        service._catalog_starting = service._catalog_terminating = False
        service._catalog_handoff = threading.Event()
        service._catalog_handoff.set()
        service._catalog_close_started = None
        service._catalog_stop_abandoned = False
        service._catalog_proc = None
        service._catalog_started = 0.0
        service._catalog_terminated_at = None
        service._catalog_killed = False
        service._catalog_last = None
        service._catalog_fence = None
        service._closed = False
        fence, identities, closures, checks, processes = [], {}, {}, [], []
        state = {"closing": False, "close_returned": False, "inherited": False,
                 "spawn_returned": False}

        def close_arrives():
            state["closing"] = True
            for _ in range(close_calls):
                service.begin_close()
                state["close_returned"] = True

        def clock():
            if (close_point == "before-handoff" and state["spawn_returned"]
                    and not state["closing"]):
                close_arrives()
            return 1000.0

        service.clock = clock

        def identity(fd):
            held = os.fstat(fd)
            assert stat.S_ISFIFO(held.st_mode), "the fence number was reused for another file"
            return held.st_dev, held.st_ino

        def pipe():
            if close_point == "before-allocation":
                close_arrives()
            ends = real_pipe()
            fence.extend(ends)
            identities.update((fd, identity(fd)) for fd in ends)
            closures.update((fd, 0) for fd in ends)
            if close_point == "after-allocation":
                close_arrives()
                assert identity(ends[0]) == identities[ends[0]]
            return ends

        def close_fd(fd):
            if fd in closures:
                assert identity(fd) == identities[fd]
                if fd == fence[0]:
                    assert state["inherited"] or not service._catalog_starting, (
                        "the read number was released while a child could still inherit it")
                closures[fd] += 1
                assert closures[fd] == 1, "a fence descriptor closed twice"
            real_close(fd)

        def spawn(_root, *, fence_fd):
            assert _root == root and fence_fd == fence[0]
            if close_point == "before-inheritance":
                close_arrives()
            assert identity(fence_fd) == identities[fence_fd]
            state["inherited"] = True
            owner = catalog_module.Owner(root, lock_fd, fence_fd)
            allowed = owner()
            checks.append((state["close_returned"], allowed))
            if state["close_returned"]:
                assert not allowed, "a child first checked its Owner after close and was admitted"
            if close_point == "after-inheritance":
                close_arrives()
                assert not owner(), "a later Owner check retained write permission"
            process = SimpleNamespace(pid=4_000_100, returncode=None if allowed else catalog_module.OWNER_GONE)
            process.poll = lambda: process.returncode

            def wait(timeout=None):
                assert process.returncode is not None, "an unsignalled fake child was waited on"
                return process.returncode

            process.wait = wait
            processes.append(process)
            state["spawn_returned"] = True
            return process

        def signal_child(pid, sig):
            assert len(processes) == 1 and pid == processes[0].pid
            processes[0].returncode = -sig

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(procs, "pipe_above_stdio", pipe)
                patch.setattr(service_module.os, "close", close_fd)
                patch.setattr(service_module.os, "killpg", signal_child)
                patch.setattr(catalog_module, "spawn_refresh", spawn)
                patch.setattr(catalog_module, "refresh_running", lambda _root: False)
                service._start_catalog()
                if close_point == "after-handoff":
                    close_arrives()
                assert state["close_returned"]
                service._stop_catalog()
                assert len(processes) == (0 if close_point in START_CLOSE_POINTS[:2] else 1)
                assert all(not allowed for after_close, allowed in checks if after_close)
                assert len(fence) == 2 and closures == {fd: 1 for fd in fence}
                assert service._catalog_fence is None and service._catalog_proc is None
                for fd in fence:
                    with pytest.raises(OSError):
                        os.fstat(fd)
        finally:
            # A killed mutant may leave a descriptor or an unfinished fake handoff.
            # Raw descriptor cleanup must preserve the call-phase failure itself.
            for fd in [*fence, lock_fd]:
                try:
                    os.fstat(fd)
                except OSError:
                    continue
                real_close(fd)


def test_concurrent_close_claims_finish_the_same_writer_revocation(plain_home, root, monkeypatch):
    """Every close claimant returns after the writer closes, even when another
    claimant already detached it and is still completing that close."""
    monkeypatch.setenv("SUBFLEET_CODEX_APP_HOME", str(plain_home / ".codex"))
    service = service_module.ConversationService(SimpleNamespace(root=root, log=logging.getLogger("concurrent-catalog")))
    raw_popen, raw_close, raw_killpg = subprocess.Popen, os.close, os.killpg
    at_popen, release_popen = threading.Event(), threading.Event()
    at_writer_close, release_writer = threading.Event(), threading.Event()
    second_started, second_returned = threading.Event(), threading.Event()
    children, errors = [], []
    writer = None

    def held_popen(*args, **kwargs):
        at_popen.set()
        assert release_popen.wait(60), "the test never released Popen"
        process = raw_popen(*args, **kwargs)
        children.append(process)
        process.wait(60)                  # eventual TERM must not conceal a late write
        return process

    def held_close(fd):
        if fd == writer and threading.current_thread() is first_closer:
            at_writer_close.set()
            assert release_writer.wait(60), "the test never released the writer close"
        raw_close(fd)

    def start():
        try:
            service._start_catalog()
        except BaseException as exc:
            errors.append(("starter", exc))

    def close(first):
        try:
            if not first:
                second_started.set()
            service.begin_close()
            if not first:
                second_returned.set()
        except BaseException as exc:
            errors.append(("first close" if first else "second close", exc))

    starter = threading.Thread(target=start, name="concurrent-catalog-starter")
    first_closer = threading.Thread(target=close, args=(True,), name="first-catalog-closer")
    second_closer = threading.Thread(target=close, args=(False,), name="second-catalog-closer")
    with monkeypatch.context() as patch:
        patch.setattr(catalog_module.subprocess, "Popen", held_popen)
        patch.setattr(service_module.os, "close", held_close)
        try:
            starter.start()
            assert at_popen.wait(30), "the starter never reached Popen"
            reader, writer = service._catalog_fence
            first_closer.start()
            assert at_writer_close.wait(30), "the first closer never detached the writer"
            assert service._catalog_fence == (reader, None)
            os.fstat(reader)
            os.fstat(writer)
            second_closer.start()
            assert second_started.wait(30)
            assert not second_returned.wait(.5), "a concurrent close returned with the writer open"
            release_writer.set()
            first_closer.join(30)
            second_closer.join(30)
            assert not first_closer.is_alive() and not second_closer.is_alive()
            assert second_returned.is_set() and not errors, errors
            service.close()              # the original handoff deadline expires before launch
            assert service._catalog_stop_abandoned and starter.is_alive()
            before = _entry_snapshot(root)
            release_popen.set()
            starter.join(90)
            assert not starter.is_alive() and not errors, errors
            assert len(children) == 1 and children[0].returncode == catalog_module.OWNER_GONE
            assert not (root / "catalog.lock").exists()
            assert _entry_snapshot(root) == before
            assert service._catalog_proc is None and service._catalog_fence is None
        finally:
            release_writer.set()
            release_popen.set()
            patch.setattr(service_module.os, "close", raw_close)
            patch.setattr(catalog_module.subprocess, "Popen", raw_popen)
            for thread in (first_closer, second_closer, starter):
                if thread.ident is not None:
                    thread.join(90)
            for process in children:
                if process.poll() is None:
                    try:
                        raw_killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(30)
            service.close()
