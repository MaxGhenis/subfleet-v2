"""A catalog run never outlives the service that started it, and never writes into a
state root after that service closed (C-30.1, design D-23).

2026-09-25: `ConversationService.close()` left the detached catalog process running.
It finished after its owner's teardown had removed the state root, recreated the
root and wrote `catalog.json` and `catalog-cache.json` into it (69 `/tmp/sfd-*`
directories at the review), or wrote while `rmtree` was still emptying it
(`OSError: [Errno 66] Directory not empty`, about one teardown in 30).

Every run here is the real `python -m subfleet.conversations.catalog`. The only
Codex rollout under the isolated HOME is a FIFO the test holds open read-write
from the start, with one blank line in it. A run opens it at once, reads the line
and waits for more: once the line is gone (`Runs.hold`) the run is past its lock
and every check before its scan, and publishes nothing until `Runs.let_go` closes
the test's end. Neither step races: a writer is there before the run opens the
FIFO, and the test lets go only after the run has read from it.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
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


def catalog_pids(root: Path) -> list[int]:
    """Every live catalog run for this state root, whoever started it."""
    out = subprocess.run(["/bin/ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    spelled = "|".join(re.escape(str(r)) for r in {root, root.resolve()})
    pattern = re.compile(rf"subfleet\.conversations\.catalog --state-root ({spelled})(\s|$)")
    return [int(line.split(None, 1)[0]) for line in out.splitlines() if pattern.search(line)]


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
    monkeypatch.delenv("SUBFLEET_CLAUDE_DIR", raising=False)
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
    reads; the run finishes reading and publishes nothing."""
    process = runs.spawn(root, fence[0])
    runs.hold()
    close_write_end(fence)
    assert runs.let_go(process) == 0
    assert sorted(os.listdir(root)) == ["catalog.lock"]


def test_a_run_started_after_its_owner_closed_touches_nothing(runs, root, fence):
    """C-30.1: a run whose owner had already closed ends at once, before it takes the
    lock, opens `state.sqlite3` or reads a session: it cannot write into the root, not
    even its lock file. (Started directly: `spawn_refresh`'s own probe makes the lock.)"""
    close_write_end(fence)
    process = runs.track(subprocess.Popen(
        [sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(root),
         "--fence-fd", str(fence[0])], cwd=REPO, pass_fds=(fence[0],), start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    assert process.wait(30) == 0
    assert os.listdir(root) == [] and runs.unread()           # it read no session


def test_a_run_whose_root_is_removed_never_brings_it_back(runs, root):
    """2026-09-25: a run that outlived its owner recreated the removed root. Now it
    finds its lock gone, writes nothing, and never makes a directory."""
    process = runs.spawn(root)
    runs.hold()
    shutil.rmtree(root)
    assert runs.let_go(process) == 0
    assert not root.exists()


def test_a_run_whose_root_is_replaced_writes_nothing_into_the_new_one(runs, root):
    """A new state root at the same path is not the one the run locked, even once a
    new daemon's probe has made a `catalog.lock` there."""
    process = runs.spawn(root)
    runs.hold()
    shutil.rmtree(root)
    root.mkdir()
    assert catalog_module.refresh_running(root) is False     # the old run holds only the old lock
    assert runs.let_go(process) == 0
    assert os.listdir(root) == ["catalog.lock"]


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


def test_a_run_without_a_state_root_makes_none(home, tmp_path):
    absent = tmp_path / "absent"
    done = subprocess.run([sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(absent)],
                          capture_output=True, text=True, timeout=60, cwd=REPO)
    assert done.returncode == 0, done.stderr
    assert not absent.exists()


# --- the daemon ------------------------------------------------------------------


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
        runs, identity, short_root):
    """The control loop starts a catalog run; the daemon closes while it is in flight.
    `close()` returns only once the run has ended, so no catalog process outlives it,
    and after the root is removed nothing brings it back, even once a surviving run
    could finish its scan and write."""
    root = short_root
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
        process = runs.track(daemon.conversations._catalog_proc)
        assert process.poll() is None
        daemon.stopping.set()                        # serve_forever's `finally` closes the daemon
        thread.join(30)
        assert not thread.is_alive(), "the daemon did not close"
        stray = catalog_pids(root)
        if process.poll() is None:
            problems.append("close() returned with its catalog run still running")
        if stray:
            problems.append(f"catalog processes outlived close(): {stray}")
        shutil.rmtree(root)
        runs.let_go()                                # a run that survived now finishes its scan
        for pid in stray:
            until(lambda: pid not in catalog_pids(root), what=f"catalog run {pid} to end")
        time.sleep(.5)
        if root.exists():
            problems.append(f"the removed state root came back holding {sorted(os.listdir(root))}")
        assert problems == [], "\n".join(problems)
    finally:
        daemon.stopping.set()
        thread.join(30)
        for pid in catalog_pids(root):
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass


def test_a_run_that_survives_close_still_writes_nothing(runs, identity, tmp_path, monkeypatch):
    """C-30.1: close() closes the run's fence before it signals, so a run the signals
    never reach (here `killpg` does nothing) publishes nothing once it goes on."""
    monkeypatch.setattr(service_module, "CATALOG_STOP_WAIT_S", 0.2)
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
        assert runs.let_go(process) == 0
        assert not (root / "catalog.json").exists() and not (root / "catalog-cache.json").exists()
    finally:
        daemon.close()
