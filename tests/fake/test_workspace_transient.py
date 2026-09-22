"""C-6.8: a workspace preparation failure is recorded, and a transient one waits.

Incident, 2026-09-20: under a load average near 10 a writable job went from
queued to failed in 19 s with "workspace preparation failed", no attempt, and
nothing in daemon.log. The snapshot's git calls were capped at 15 s, the
exception was discarded, and the failure was terminal. Twelve jobs since
2026-09-19 ended that way, read-only ones included.
"""

import contextlib
import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.daemon import after
from subfleet.salvage import SalvageError
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git

REAL_GIT = shutil.which("git")
#: Long enough for `git worktree add` to reach its checkout's filter on a
#: loaded machine; the tests below need the kill to land mid-checkout.
ADD_CAP_S = 3


@pytest.fixture
def slow_git(tmp_path, monkeypatch):
    """A `git` first on PATH that sleeps past any cap while its marker exists."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    marker = tmp_path / "git-is-slow"
    script = bindir / "git"
    # `exec`: the sleep is the process the cap kills, so no orphan outlives a test.
    script.write_text(f'#!/bin/sh\nif [ -e "{marker}" ]; then exec sleep 30; fi\nexec "{REAL_GIT}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return marker


def events(daemon, job_id, kind):
    return [json.loads(row["data_json"]) for row in daemon.store.list_events(job_id) if row["kind"] == kind]


def due(daemon, job_id):
    """Make the job's next check due without sleeping through its backoff."""
    daemon.store.update_job(job_id, next_check_at=after(-1))


def log_text(daemon):
    daemon._log_handler.flush()
    return (daemon.root / "daemon.log").read_text()


@pytest.mark.parametrize("sandbox,in_place", [("workspace-write", True), ("read-only", False)])
def test_c6_8_git_past_its_cap_requeues_and_the_next_pass_admits(state_daemon, slow_git, sandbox, in_place):
    """C-6.8 the incident itself: git runs past the cap, the job waits, then it is admitted."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    daemon.policy["caps"]["workspace_git_timeout_s"] = 1
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox=sandbox, in_place=in_place))["job_id"]

    slow_git.touch()
    daemon._admit()

    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"], job["rc"]) == ("waiting", "workspace", None)
    assert job["next_check_at"] > after(0) and job["finished_at"] is None
    assert daemon.store.list_attempts(job_id) == []
    assert daemon.store.list_notices() == [] and daemon.store.list_leases() == []
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error_type"] == "TimeoutExpired"
    assert "timed out after 1 s" in record["error"] and record["deferrals"] == 1
    shown = daemon.dispatch("show", {"job_id": job_id})["workspace"]
    assert shown["event"] == "job.workspace_deferred" and shown["error_type"] == "TimeoutExpired"
    text = log_text(daemon)
    assert job_id in text and "TimeoutExpired" in text and "deferred 1/" in text

    # Waiting on a workspace is this job's wait alone: it holds nothing back.
    slow_git.unlink()
    other = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(other)] == ["reserved"]
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    daemon._pending_launches.clear()
    daemon.store.update_attempt(daemon.store.list_attempts(other)[0]["attempt_id"], state="failed")
    daemon.store.update_job(other, state="failed")
    daemon.store.release_leases(other)

    due(daemon, job_id)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"], job["next_check_at"]) == ("running", None, None)
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved" and attempt["baseline_tree"]
    assert events(daemon, job_id, "job.workspace_ready") == [{}]
    assert daemon._workspace_deferrals == {}


def failing_workspace(monkeypatch, daemon, exc):
    calls = []

    def raise_it(job):
        calls.append(job["job_id"])
        raise exc
    monkeypatch.setattr(daemon, "_workspace", raise_it)
    return calls


def test_c6_8_a_transient_failure_past_the_retry_limit_fails_with_its_cause(state_daemon, monkeypatch):
    """C-6.8 the wait is bounded, and the terminal notice names what kept failing."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    daemon.policy["caps"]["workspace_retry_max"] = 2
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    failing_workspace(monkeypatch, daemon, subprocess.TimeoutExpired(
        ["git", "-C", str(harness.workdir), "add", "-A"], 15))

    delays = []
    for _ in range(2):
        daemon._admit()
        assert daemon.store.get_job(job_id)["state"] == "waiting"
        due(daemon, job_id)
    daemon._admit()

    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and job["started_at"] is None
    [notice] = daemon.store.list_notices()
    assert "workspace preparation failed after 2 retries: TimeoutExpired: git add timed out after 15 s" in notice["text"]
    assert [r["deferrals"] for r in events(daemon, job_id, "job.workspace_deferred")] == [1, 2]
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error_type"] == "TimeoutExpired" and failed["transient"] is True
    assert daemon.dispatch("show", {"job_id": job_id})["workspace"]["event"] == "job.workspace_failed"
    assert "workspace preparation failed: TimeoutExpired" in log_text(daemon)
    assert daemon._workspace_deferrals == {} and daemon.store.list_leases() == []


def test_c6_8_backoff_doubles_from_five_seconds_to_the_ceiling(state_daemon, monkeypatch):
    """C-6.8 5 s, doubling, never beyond 300 s."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    daemon.policy["caps"]["workspace_retry_max"] = 9
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    failing_workspace(monkeypatch, daemon, OSError(errno.EAGAIN, "Resource temporarily unavailable"))
    observed = []
    real_after = after
    monkeypatch.setattr("subfleet.daemon.after", lambda seconds: observed.append(seconds) or real_after(seconds))
    for _ in range(9):
        daemon._admit()
        due(daemon, job_id)
    assert observed == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert {r["error_type"] for r in events(daemon, job_id, "job.workspace_deferred")} == {"BlockingIOError"}


@pytest.mark.parametrize("exc,expected", [
    (SalvageError("git read-tree failed: fatal: bad object deadbeef"), "SalvageError: git read-tree failed: fatal: bad object deadbeef"),
    (OSError(errno.EACCES, "Permission denied"), "PermissionError: [Errno 13] Permission denied"),
    (subprocess.CalledProcessError(128, ["git"]), "CalledProcessError"),
])
def test_c6_8_a_failure_that_says_something_about_the_repository_fails_at_once(state_daemon, monkeypatch, exc, expected):
    """C-6.8 only timeouts and transient OS errors wait; the rest fail now, with the cause."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    calls = failing_workspace(monkeypatch, daemon, exc)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and calls == [job_id]
    [notice] = daemon.store.list_notices()
    assert "workspace preparation failed: " + expected in notice["text"]
    assert events(daemon, job_id, "job.workspace_deferred") == []
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["transient"] is False and failed["deferrals"] == 0


def test_c6_8_a_wrapped_timeout_is_transient_and_names_the_underlying_type(state_daemon, monkeypatch):
    """C-6.8 salvage wraps git's timeout; the record still says TimeoutExpired."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    try:
        try:
            raise subprocess.TimeoutExpired(["git"], 60)
        except subprocess.TimeoutExpired as cause:
            raise SalvageError("git add timed out after 60 s", transient=True) from cause
    except SalvageError as exc:
        failing_workspace(monkeypatch, daemon, exc)
    daemon._admit()
    assert daemon.store.get_job(job_id)["wait_reason"] == "workspace"
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error_type"] == "TimeoutExpired" and record["error"] == "git add timed out after 60 s"


def test_c6_8_a_refusal_is_still_a_refusal(state_daemon, monkeypatch):
    """C-6.5 an AdapterError from preparation keeps its exit code and fix, and never waits."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    failing_workspace(monkeypatch, daemon, AdapterError("writable job refused on main", code=7, fix="branch first"))
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 7)
    assert "fix: branch first" in daemon.store.list_notices()[0]["text"]


def test_c6_8_a_cancelled_job_is_not_put_back_to_waiting(state_daemon, monkeypatch):
    """C-6.8 the deferral never overwrites a cancellation that raced it."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    job = daemon.store.get_job(job_id)
    daemon.store.update_job(job_id, cancel_requested_at=after(0))
    daemon._workspace_failed(job, subprocess.TimeoutExpired(["git", "-C", "x", "add"], 1))
    assert daemon.store.get_job(job_id)["state"] == "queued"
    assert events(daemon, job_id, "job.workspace_deferred") == []


def test_c6_8_submit_reports_a_git_timeout_instead_of_misreading_it(state_daemon, slow_git):
    """C-6.8 a timed-out rev-parse is not "not a repository" and not "not on main"."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    git(workdir, "branch", "-m", "main")
    daemon.policy["caps"]["workspace_git_timeout_s"] = 1
    slow_git.touch()
    with pytest.raises(AdapterError) as error:
        daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))
    assert error.value.code == 1 and "timed out after 1 s" in str(error.value)
    assert "caps.workspace_git_timeout_s" in error.value.fix
    assert daemon.store.list_jobs() == []
    slow_git.unlink()
    with pytest.raises(AdapterError) as refused:
        daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))
    assert refused.value.code == 7 and "main" in str(refused.value)


def test_c6_8_a_half_made_worktree_is_rebuilt_before_admission(state_daemon):
    """C-6.8 a directory a killed `worktree add` left behind is never handed to a provider."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    leftover = daemon.root / "worktrees" / job_id
    leftover.mkdir()
    (leftover / "partial.txt").write_text("from a killed checkout\n")
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved"
    assert (leftover / ".git").is_file() and not (leftover / "partial.txt").exists()
    assert git(leftover, "rev-parse", "HEAD") == git(workdir, "rev-parse", "HEAD")
    assert (leftover / "tracked.txt").read_text() == "baseline\n"


def test_c6_8_a_killed_worktree_add_waits_and_leaves_nothing_behind(state_daemon, slow_git):
    """C-6.8 `git worktree add` past its cap is transient, and its directory is removed."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    daemon.policy["caps"].update(workspace_git_timeout_s=1, worktree_add_timeout_s=1)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    daemon.policy["caps"]["workspace_git_timeout_s"] = 60   # only the add is slow
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if "worktree" in cmd and "add" in cmd:
            slow_git.touch()
            try:
                return real_run(cmd, *args, **kwargs)
            finally:
                slow_git.unlink()
        return real_run(cmd, *args, **kwargs)
    import subfleet.daemon as daemon_module
    original = daemon_module.subprocess.run
    daemon_module.subprocess.run = run
    try:
        daemon._admit()
    finally:
        daemon_module.subprocess.run = original
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    assert not (daemon.root / "worktrees" / job_id).exists()
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error"] == "git worktree timed out after 1 s"
    due(daemon, job_id)
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]


# --- A `git worktree add` stopped partway (2026-09-22 design review) ---------
#
# `git worktree add` does its checkout in a child, `git reset --hard`, which
# runs any filter as a child of its own. The fixture's filter records itself
# and that `git reset` and then blocks while the gate exists, so a test can
# stop an add mid-checkout: `.gitattributes` is written, `slow.txt` and the
# files after it are not, and there is no index yet.


def recorded(pids_file):
    """[filter pid, `git reset --hard` pid] once the checkout reached the filter."""
    if not pids_file.exists():
        return []
    return [int(pid) for pid in pids_file.read_text().split()]


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(condition, timeout_s=15):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for " + condition.__name__
        time.sleep(.02)


def registration(repository_path, worktree):
    """The lines git lists for `worktree` beyond its path and HEAD, or None if unregistered."""
    listing = git(repository_path, "worktree", "list", "--porcelain")
    for stanza in listing.split("\n\n"):
        lines = stanza.splitlines()
        if lines and os.path.realpath(lines[0].removeprefix("worktree ")) == os.path.realpath(worktree):
            return [line for line in lines[1:] if not line.startswith("HEAD ") and line != "detached"]
    return None


@pytest.fixture
def slow_checkout(state_daemon, tmp_path):
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    gate, pids = tmp_path / "checkout-gate", tmp_path / "checkout-pids"
    (workdir / ".gitattributes").write_text("slow.txt filter=slow\n")
    (workdir / "slow.txt").write_text("slow\n")
    (workdir / "z-after.txt").write_text("after\n")
    git(workdir, "add", "-A")
    git(workdir, "commit", "-m", "a checkout that can be held")
    git(workdir, "config", "filter.slow.smudge",
        f"echo $$ $PPID >> '{pids}'; while [ -e '{gate}' ]; do sleep 0.02; done; cat")
    gate.touch()
    try:
        yield SimpleNamespace(workdir=workdir, gate=gate, pids=pids)
    finally:
        gate.unlink(missing_ok=True)
        for pid in recorded(pids):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


def start_add(checkout, worktree, head, env=None):
    """A `git worktree add` in a session of its own, held in its checkout's filter."""
    process = subprocess.Popen(["git", "-C", str(checkout.workdir), "worktree", "add", "--detach", str(worktree), head],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=env, start_new_session=True)

    def reached_the_filter():
        return len(recorded(checkout.pids)) == 2
    wait_for(reached_the_filter)
    return process


def killed_add(checkout, worktree, head):
    """What a `git worktree add` leaves when it dies mid-checkout with all its children."""
    process = start_add(checkout, worktree, head)
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()

    def group_gone():
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass                               # macOS: only unreaped zombies are left
        return False
    wait_for(group_gone)
    checkout.gate.unlink()


def test_c6_8_on_main_a_killed_worktree_add_orphans_its_checkout(state_daemon, slow_checkout):
    """C-6.8 as it stands on main: the cap kills `git worktree add` alone.

    Its `git reset --hard` and the filter it runs keep going after the add is
    gone and the directory removed. The registration keeps git's
    `initializing` lock, which `git worktree prune` skips, so the retry's add
    is refused for it and the job fails at once, and as a refusal (rc 7, a
    plain `job.failed`), not the rc 1 `job.workspace_failed` C-6.8 names for
    git exiting non-zero.
    """
    daemon, harness = state_daemon
    daemon.policy["caps"]["worktree_add_timeout_s"] = ADD_CAP_S
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    daemon._admit()

    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error"] == f"git worktree timed out after {ADD_CAP_S} s"
    filter_pid, reset_pid = recorded(slow_checkout.pids)
    assert running(filter_pid) and running(reset_pid)
    assert not worktree.exists()
    assert registration(slow_checkout.workdir, worktree) == ["locked initializing"]

    slow_checkout.gate.unlink()
    due(daemon, job_id)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 7) and daemon.store.list_attempts(job_id) == []
    assert events(daemon, job_id, "job.workspace_failed") == []
    [notice] = daemon.store.list_notices()
    assert "could not allocate worktree" in notice["text"] and "missing but locked worktree" in notice["text"]


def test_c6_8_on_main_a_half_made_checkout_is_handed_to_a_provider(state_daemon, slow_checkout):
    """C-6.8 as it stands on main: `.git` is a link and HEAD answers, so it is reused.

    The attempt is reserved in a tree that is missing files, locked
    `initializing`, with no index, and its baseline records the missing files
    as deleted.
    """
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    killed_add(slow_checkout, worktree, head)
    assert registration(slow_checkout.workdir, worktree) == ["locked initializing"]

    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved"
    assert not (worktree / "slow.txt").exists() and not (worktree / "z-after.txt").exists()
    assert registration(slow_checkout.workdir, worktree) == ["locked initializing"]
    assert attempt["baseline_tree"] != git(slow_checkout.workdir, "rev-parse", head + "^{tree}")


def test_c6_8_on_main_an_add_still_running_is_raced(state_daemon, slow_checkout):
    """C-6.8 as it stands on main: an add a stopped daemon started is still checking out.

    The next daemon reuses its half-made tree while it is still being written.
    """
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    process = start_add(slow_checkout, worktree, head)
    try:
        daemon._admit()
        [attempt] = daemon.store.list_attempts(job_id)
        assert attempt["state"] == "reserved" and process.poll() is None
        assert not (worktree / "z-after.txt").exists()
    finally:
        slow_checkout.gate.unlink()
        process.wait(timeout=30)


def test_c6_8_on_main_a_discard_prunes_the_callers_other_worktrees(state_daemon, tmp_path):
    """C-6.8 as it stands on main: the discard runs `git worktree prune` in the caller's repository.

    `prune` with no path drops every stale, unlocked registration there, not
    only this job's. A worktree of the caller's whose directory is absent for
    now (a volume not mounted, a tree moved by hand) loses its HEAD, its index,
    and its reflog, and a commit made only there is no longer reachable.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    theirs = tmp_path / "theirs"
    git(workdir, "worktree", "add", "--detach", str(theirs), "HEAD")
    (theirs / "committed.txt").write_text("only in their worktree\n")
    git(theirs, "add", "committed.txt")
    git(theirs, "commit", "-m", "made only in their worktree")
    only_theirs = git(theirs, "rev-parse", "HEAD")
    (theirs / "staged.txt").write_text("staged\n")
    git(theirs, "add", "staged.txt")
    admin = Path(git(theirs, "rev-parse", "--absolute-git-dir"))
    away = tmp_path / "theirs-unmounted"
    theirs.rename(away)
    (daemon.root / "worktrees" / job_id).mkdir()          # what an add killed early leaves

    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert not admin.exists()
    assert only_theirs not in git(workdir, "rev-list", "--all", "--reflog").split()
    away.rename(theirs)
    status = subprocess.run(["git", "-C", str(theirs), "status"], capture_output=True, text=True)
    assert status.returncode != 0 and "not a git repository" in status.stderr
