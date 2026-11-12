"""C-6.8, C-6.12, C-6.20: a `git worktree add` stopped at its cap, and what it leaves.

Four reproducers, one for each defect of the C-6.8 port (MaxGhenis/subfleet-v2#30's
fixes, ported to release/217), each with real git: the cap killed git alone and its
checkout kept running; a half-written checkout was reused; a dead add's
`initializing` lock failed the job for good; and a removal proved nothing about what
it removed. Then the review of #30's nine findings (F1 to F9), each with a test.

Every test that holds a process releases its gate, waits with a bound, and kills
and reaps whatever is left, inside `finally` (F9).
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module, procs
from subfleet.procs import Containment
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git

#: The process reads the census needs, as they are before `state_daemon` fakes them.
REAL = {name: getattr(procs, name) for name in
        ("containment", "snapshot", "identity", "boot_id", "proc_start", "same_process", "group_members")}


@contextlib.contextmanager
def real_procs():
    """The real process reads, for the length of one call (`state_daemon` fakes them)."""
    saved = {name: getattr(procs, name) for name in REAL}
    for name, function in REAL.items():
        setattr(procs, name, function)
    try:
        yield
    finally:
        for name, function in saved.items():
            setattr(procs, name, function)


@pytest.fixture
def real_census(monkeypatch):
    """The daemon's add census and identity read go to the real process table."""
    def containment(*args, **kwargs):
        with real_procs():
            return REAL["containment"](*args, **kwargs)

    def identity(pid):
        with real_procs():
            return REAL["identity"](pid)
    monkeypatch.setattr(daemon_module.procs, "containment", containment)
    monkeypatch.setattr(daemon_module.procs, "identity", identity)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class HeldFilter:
    """A smudge filter on `*.dat` that blocks while `hold` exists: a checkout git cannot finish."""

    def __init__(self, workdir: Path, scratch: Path):
        self.workdir, self.scratch = workdir, scratch
        self.gate = scratch / "hold"
        self.pids = scratch / "filter-pids"
        script = scratch / "hold-filter.sh"
        script.write_text(f'#!/bin/sh\necho $$ >> "{self.pids}"\n'
                          f'while [ -e "{self.gate}" ]; do sleep 0.05; done\nexec cat\n')
        script.chmod(0o755)
        git(workdir, "config", "filter.hold.smudge", str(script))
        git(workdir, "config", "filter.hold.clean", "cat")
        (workdir / ".gitattributes").write_text("*.dat filter=hold\n")
        (workdir / "a.dat").write_text("held content\n")
        (workdir / "dir").mkdir()
        (workdir / "dir" / "b.txt").write_text("b\n")
        git(workdir, "add", ".")
        git(workdir, "commit", "-m", "with a held file")
        self.head = git(workdir, "rev-parse", "HEAD")
        self.processes: list[subprocess.Popen] = []

    def hold(self):
        self.gate.touch()

    def filter_pids(self) -> list[int]:
        try:
            return [int(line) for line in self.pids.read_text().split()]
        except FileNotFoundError:
            return []

    def wait_for_filter(self, bound: float = 20) -> None:
        deadline = time.monotonic() + bound
        while not self.filter_pids():
            assert time.monotonic() < deadline, "the add never reached its filter"
            time.sleep(0.02)

    def killed_add(self, tree: Path) -> None:
        """What an add killed in its checkout leaves at `tree`: registered, locked `initializing`."""
        process = subprocess.Popen(["git", "-C", str(self.workdir), "worktree", "add", "--detach", str(tree), self.head],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={**os.environ, "LC_ALL": "C"}, start_new_session=True)
        self.processes.append(process)
        self.hold()
        self.wait_for_filter()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        self.release()

    def release(self, bound: float = 10) -> None:
        """Open the gate, wait with a bound, then kill and reap what is left (F9)."""
        self.gate.unlink(missing_ok=True)
        deadline = time.monotonic() + bound
        for process in self.processes:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for pid in self.filter_pids():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        while any(alive(pid) for pid in self.filter_pids()):
            assert time.monotonic() < deadline + 5, "a filter outlived its kill"
            time.sleep(0.05)


@pytest.fixture
def held(state_daemon, tmp_path):  # noqa: F811
    daemon, harness = state_daemon
    repository(daemon, harness)
    scratch = tmp_path / "held"
    scratch.mkdir()
    filtered = HeldFilter(harness.workdir, scratch)
    try:
        yield filtered
    finally:
        filtered.release()


def submit(daemon, harness):
    return daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]


def events(daemon, job_id, kind):
    return [json.loads(row["data_json"]) for row in daemon.store.list_events(job_id) if row["kind"] == kind]


def due(daemon, job_id):
    daemon.store.update_job(job_id, next_check_at=daemon_module.after(-1))


def log_text(daemon):
    daemon._log_handler.flush()
    return (daemon.root / "daemon.log").read_text()


def admin_of(workdir: Path, job_id: str) -> Path:
    return workdir / ".git" / "worktrees" / job_id


def assert_finished(tree: Path, head: str) -> None:
    assert git(tree, "rev-parse", "HEAD") == head
    assert (tree / "a.dat").read_text() == "held content\n"
    assert git(tree, "status", "--porcelain", "--untracked-files=all") == ""


# --- the four reproducers ---------------------------------------------------------------------------

def test_reproducer_a_the_cap_stops_the_whole_checkout(state_daemon, held, real_census):  # noqa: F811
    """Defect 1: at the cap the add's whole group goes, its filter with it; the job waits."""
    daemon, harness = state_daemon
    daemon.policy["caps"]["worktree_add_timeout_s"] = 1
    job_id = submit(daemon, harness)
    held.hold()
    try:
        daemon._admit()
        filters = held.filter_pids()
        assert filters, "the add reached its filter"
        assert [pid for pid in filters if alive(pid)] == []
        job = daemon.store.get_job(job_id)
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    finally:
        held.release()
    due(daemon, job_id)
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert_finished(daemon.root / "worktrees" / job_id, held.head)


def test_reproducer_b_a_half_written_checkout_is_never_reused(state_daemon, held):  # noqa: F811
    """Defect 2: a killed add's tree has a gitfile and a HEAD that answers; it is cut again."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness)
    tree = daemon.root / "worktrees" / job_id
    tree.parent.mkdir(exist_ok=True)
    held.killed_add(tree)
    assert (admin_of(harness.workdir, job_id) / "locked").read_bytes() == b"initializing\n"
    assert (tree / ".git").is_file() and not (tree / "a.dat").exists()
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert_finished(tree, held.head)


@pytest.mark.parametrize("shape", ["no-gitfile", "no-tree"])
def test_reproducer_c_a_dead_adds_lock_does_not_fail_the_job(state_daemon, held, shape):  # noqa: F811
    """Defect 3: git's `initializing` lock that no live add holds is no one's claim."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness)
    tree = daemon.root / "worktrees" / job_id
    tree.parent.mkdir(exist_ok=True)
    held.killed_add(tree)
    if shape == "no-gitfile":
        (tree / ".git").unlink()            # git writes the backlink before the gitfile (Step 0, item 4)
    else:
        shutil.rmtree(tree)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert job["state"] == "running", (job["state"], daemon.store.list_notices())
    assert_finished(tree, held.head)


def test_reproducer_d_an_unregistered_directory_with_someones_file_stays(state_daemon):  # noqa: F811
    """Defect 4: a path the commit does not have is never removed; the job fails, saying so."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = submit(daemon, harness)
    tree = daemon.root / "worktrees" / job_id
    tree.mkdir(parents=True)
    (tree / "notes.txt").write_text("someone's work\n")
    daemon._admit()
    assert (tree / "notes.txt").read_text() == "someone's work\n"
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 7)
    notice = daemon.store.list_notices()[0]["text"]
    assert "worktree" in notice and str(tree) not in notice and "notes.txt" not in notice
    assert "notes.txt" in log_text(daemon)


def test_reproducer_d_f1_a_staged_index_stays_when_the_tree_has_gone(state_daemon):  # noqa: F811
    """Defect 4 with F1: the path is missing, and its registration's index stages an edit."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = submit(daemon, harness)
    tree = daemon.root / "worktrees" / job_id
    tree.parent.mkdir(exist_ok=True)
    git(workdir, "worktree", "add", "--detach", str(tree), "HEAD")
    (tree / "tracked.txt").write_text("a staged edit\n")
    git(tree, "add", "tracked.txt")
    shutil.rmtree(tree)
    admin = admin_of(workdir, job_id)
    index = (admin / "index").read_bytes()
    daemon._admit()
    assert (admin / "index").read_bytes() == index
    assert daemon.store.get_job(job_id)["state"] == "failed"
