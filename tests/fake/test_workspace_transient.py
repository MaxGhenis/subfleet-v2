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

import subfleet.daemon as daemon_module
from subfleet import procs
from subfleet.adapters.base import AdapterError
from subfleet.daemon import after
from subfleet.procs import Containment
from subfleet.salvage import SalvageError
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git

REAL_GIT = shutil.which("git")
#: Long enough for `git worktree add` to reach its checkout's filter on a
#: loaded machine; the tests below need the kill to land mid-checkout.
ADD_CAP_S = 3
#: The fixture replaces the census with one that is always empty; the tests
#: of a stopped add need the real one, captured before any test runs.
REAL_CONTAINMENT = procs.containment


@pytest.fixture
def slow_git(tmp_path, monkeypatch):
    """A `git` first on PATH that sleeps past any cap while its marker exists."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    marker = tmp_path / "git-is-slow"
    script = bindir / "git"
    # `exec`: the sleep is the process the cap kills, so no orphan outlives a test.
    # `<marker>-add` makes only `git -C <repo> worktree add` slow.
    script.write_text(f'#!/bin/sh\nif [ -e "{marker}" ]; then exec sleep 30; fi\n'
                      f'if [ -e "{marker}-add" ] && [ "$3" = worktree ] && [ "$4" = add ]; then exec sleep 30; fi\n'
                      f'exec "{REAL_GIT}" "$@"\n')
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
    daemon.policy["caps"]["worktree_add_timeout_s"] = 1
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    slow_add = Path(f"{slow_git}-add")
    slow_add.touch()
    try:
        daemon._admit()
    finally:
        slow_add.unlink()
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
# files after it are not, and there is no index yet. Before the fix, the cap
# killed the add alone: its checkout wrote on, git's `initializing` lock
# outlived it and refused the retry, and a half-made tree passed for a
# worktree because `.git` was a link and HEAD answered.


def recorded(pids_file):
    """[filter pid, `git reset --hard` pid] once the checkout reached the filter."""
    if not pids_file.exists():
        return []
    return [int(pid) for pid in pids_file.read_text().split()]


def live(pid):
    """Present in the process table and not a zombie, as the census counts it (C-5.5)."""
    state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


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


def whole_checkout(daemon, workdir, job_id):
    """The job's worktree is registered, unlocked, clean, at its head, and its baseline is that commit."""
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved"
    assert registration(workdir, worktree) == []
    assert git(worktree, "rev-parse", "HEAD") == head
    assert git(worktree, "status", "--porcelain", "--untracked-files=all") == ""
    assert (worktree / "slow.txt").read_text() == "slow\n" and (worktree / "z-after.txt").read_text() == "after\n"
    assert attempt["baseline_tree"] == git(workdir, "rev-parse", head + "^{tree}")


def absent_worktree(workdir, tmp_path):
    """A worktree of the caller's whose directory is away for now (a volume not mounted, a tree moved by hand).

    It holds a commit made only there and a staged file: HEAD, index, and
    reflog live in its registration. Cut from the root commit, so no filter runs.
    """
    theirs = tmp_path / "theirs"
    git(workdir, "worktree", "add", "--detach", str(theirs), git(workdir, "rev-list", "--max-parents=0", "HEAD"))
    (theirs / "committed.txt").write_text("only in their worktree\n")
    git(theirs, "add", "committed.txt")
    git(theirs, "commit", "-m", "made only in their worktree")
    (theirs / "staged.txt").write_text("staged\n")
    git(theirs, "add", "staged.txt")
    absent = SimpleNamespace(path=theirs, away=tmp_path / "theirs-unmounted", commit=git(theirs, "rev-parse", "HEAD"),
                             admin=Path(git(theirs, "rev-parse", "--absolute-git-dir")))
    theirs.rename(absent.away)
    return absent


def assert_intact(workdir, absent):
    """Its registration kept HEAD, index, and reflog: back in place, it is the worktree it was."""
    assert absent.admin.is_dir()
    assert absent.commit in git(workdir, "rev-list", "--all", "--reflog").split()
    absent.away.rename(absent.path)
    assert git(absent.path, "rev-parse", "HEAD") == absent.commit
    assert git(absent.path, "status", "--porcelain") == "A  staged.txt"


@pytest.fixture
def slow_checkout(state_daemon, tmp_path):
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    gate, pids, envs = tmp_path / "checkout-gate", tmp_path / "checkout-pids", tmp_path / "checkout-env"
    (workdir / ".gitattributes").write_text("slow.txt filter=slow\n")
    (workdir / "slow.txt").write_text("slow\n")
    (workdir / "z-after.txt").write_text("after\n")
    git(workdir, "add", "-A")
    git(workdir, "commit", "-m", "a checkout that can be held")
    git(workdir, "config", "filter.slow.smudge",
        f"echo \"$LC_ALL $SUBFLEET_ATTEMPT $SUBFLEET_ROOT\" >> '{envs}'; echo $$ $PPID >> '{pids}'; "
        f"while [ -e '{gate}' ]; do sleep 0.02; done; cat")
    gate.touch()
    try:
        yield SimpleNamespace(workdir=workdir, gate=gate, pids=pids, envs=envs)
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


def test_c6_8_an_add_past_its_cap_stops_its_whole_checkout(state_daemon, slow_checkout, monkeypatch, tmp_path):
    """C-6.8, C-5.4 the cap stops the add's whole group, and what it made goes before the retry.

    Its `git reset --hard` and the filter that one runs no longer outlive it;
    the directory, its registration, and git's `initializing` lock are gone
    when the pass ends, so the retry cuts a whole checkout. No other
    registration in the caller's repository is touched.
    """
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module.procs, "containment", REAL_CONTAINMENT)
    daemon.policy["caps"]["worktree_add_timeout_s"] = ADD_CAP_S
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    absent = absent_worktree(slow_checkout.workdir, tmp_path)
    daemon._admit()

    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert (record["error_type"], record["error"]) == ("TimeoutExpired", f"git worktree timed out after {ADD_CAP_S} s")
    # The add ran with the job's marker and in the C locale, and its checkout inherited both.
    assert slow_checkout.envs.read_text() == f"C worktree-add:{job_id} {daemon.root}\n"
    filter_pid, reset_pid = recorded(slow_checkout.pids)
    assert not live(filter_pid) and not live(reset_pid)
    assert not worktree.exists() and registration(slow_checkout.workdir, worktree) is None
    assert_intact(slow_checkout.workdir, absent)

    slow_checkout.gate.unlink()
    due(daemon, job_id)
    daemon._admit()
    whole_checkout(daemon, slow_checkout.workdir, job_id)


@pytest.mark.parametrize("left", ["killed-mid-checkout", "unlocked-without-index", "locked-whole-checkout",
                                  "registered-without-directory"])
def test_c6_8_a_worktree_that_is_not_a_finished_checkout_is_rebuilt(state_daemon, slow_checkout, monkeypatch, left):
    """C-6.8 a directory is reused only as a finished checkout of `workdir_head`; anything else is rebuilt.

    `killed-mid-checkout` is what a daemon killed mid-add leaves: a `.git`
    link and a HEAD that answers, which passed before, with git's
    `initializing` lock, files missing, and no index. The next two fail one
    test each: no index, and the lock. `registered-without-directory` is what
    the old discard left (and `thesis-tsa-signers` is): the directory
    removed, and the registration kept by its lock.
    """
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module.procs, "containment", REAL_CONTAINMENT)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    if left == "locked-whole-checkout":
        slow_checkout.gate.unlink()
        git(slow_checkout.workdir, "worktree", "add", "--detach", str(worktree), head)
        git(slow_checkout.workdir, "worktree", "lock", "--reason", "initializing", str(worktree))
    else:
        killed_add(slow_checkout, worktree, head)
        if left == "unlocked-without-index":
            git(slow_checkout.workdir, "worktree", "unlock", str(worktree))
    if left == "registered-without-directory":
        shutil.rmtree(worktree)
        assert registration(slow_checkout.workdir, worktree) == ["locked initializing"]
    else:
        (worktree / "left-behind.txt").write_text("not the checkout's\n")

    daemon._admit()
    assert not (worktree / "left-behind.txt").exists()
    whole_checkout(daemon, slow_checkout.workdir, job_id)
    assert f"job {job_id}: {worktree} is not a finished checkout of {head}" in log_text(daemon)


def test_c6_8_a_finished_checkout_is_reused_as_it_is(state_daemon, slow_checkout, monkeypatch):
    """C-6.8 a waiting job's worktree, cut on an earlier pass, is used again, with no census and no new add."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    slow_checkout.gate.unlink()
    git(slow_checkout.workdir, "worktree", "add", "--detach", str(worktree), head)
    inode = (worktree / "z-after.txt").stat().st_ino
    censuses = []
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: censuses.append(args) or Containment())
    daemon._admit()
    whole_checkout(daemon, slow_checkout.workdir, job_id)
    assert (worktree / "z-after.txt").stat().st_ino == inode and censuses == []


def test_c6_8_an_add_that_outlived_its_daemon_is_waited_for_not_raced(state_daemon, slow_checkout, monkeypatch):
    """C-6.8, C-5.4 an add a stopped daemon started is still checking out.

    It carries the job's add marker. The next daemon neither signals it (no
    record names its group) nor removes what it is writing; the job waits,
    and once the add has finished its checkout is the job's worktree.
    """
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module.procs, "containment", REAL_CONTAINMENT)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    env = {**os.environ, "SUBFLEET_ATTEMPT": f"worktree-add:{job_id}", "SUBFLEET_ROOT": str(daemon.root)}
    process = start_add(slow_checkout, worktree, head, env=env)
    try:
        daemon._admit()
        job = daemon.store.get_job(job_id)
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
        [record] = events(daemon, job_id, "job.workspace_deferred")
        assert record["error_type"] == "SalvageError" and "git worktree add for this job still running" in record["error"]
        filter_pid, reset_pid = recorded(slow_checkout.pids)
        assert process.poll() is None and live(filter_pid) and live(reset_pid)
        assert registration(slow_checkout.workdir, worktree) == ["locked initializing"]
        assert (worktree / ".gitattributes").exists() and daemon.store.list_attempts(job_id) == []
    finally:
        slow_checkout.gate.unlink()
        assert process.wait(timeout=30) == 0
    inode = (worktree / "z-after.txt").stat().st_ino
    due(daemon, job_id)
    daemon._admit()
    whole_checkout(daemon, slow_checkout.workdir, job_id)
    assert (worktree / "z-after.txt").stat().st_ino == inode


def test_c6_8_a_census_that_cannot_be_read_removes_nothing(state_daemon, slow_checkout, monkeypatch):
    """C-6.8, C-5.5 an unverifiable census is no evidence that the add is gone: the job waits."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    killed_add(slow_checkout, worktree, head)
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment(
        unverifiable=True, errors=("marker enumeration unavailable",)))
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error"] == ("could not verify that no git worktree add for this job is running: "
                               "marker enumeration unavailable")
    assert (worktree / ".git").is_file() and registration(slow_checkout.workdir, worktree) == ["locked initializing"]


def test_c6_8_an_add_past_its_cap_leaves_its_directory_while_its_census_is_not_empty(state_daemon, slow_checkout,
                                                                                    monkeypatch):
    """C-6.8, C-5.6 the settle window ends without a verified-empty census: nothing is removed.

    The job waits as for any add past its cap; the next pass, with the add's
    group gone, removes the directory and cuts a whole checkout.
    """
    daemon, harness = state_daemon
    daemon.kill_settle_s = .2
    daemon.policy["caps"]["worktree_add_timeout_s"] = ADD_CAP_S
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    monkeypatch.setattr(daemon_module.procs, "containment",
                        lambda *args, **kwargs: Containment(group_pids=frozenset({os.getpid()})))
    daemon._admit()
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error_type"] == "TimeoutExpired"
    assert (worktree / ".git").is_file() and registration(slow_checkout.workdir, worktree) == ["locked initializing"]
    assert f"job {job_id}: git worktree add stopped at its cap, but pids [{os.getpid()}] remain" in log_text(daemon)

    monkeypatch.setattr(daemon_module.procs, "containment", REAL_CONTAINMENT)
    slow_checkout.gate.unlink()
    due(daemon, job_id)
    daemon._admit()
    whole_checkout(daemon, slow_checkout.workdir, job_id)


def test_c6_8_a_worktree_add_that_git_refuses_fails_the_job_with_its_cause(state_daemon, tmp_path):
    """C-6.8 git exiting non-zero fails the job with rc 1 and its stderr; it is not a refusal (C-6.5).

    What the add left is discarded, and nothing else in the caller's repository.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    absent = absent_worktree(workdir, tmp_path)
    worktrees = daemon.root / "worktrees"
    worktrees.chmod(0o500)
    try:
        daemon._admit()
    finally:
        worktrees.chmod(0o700)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and daemon.store.list_attempts(job_id) == []
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error_type"] == "SalvageError" and failed["transient"] is False
    assert failed["error"].startswith("git worktree add failed: ") and "Permission denied" in failed["error"]
    assert not (worktrees / job_id).exists() and registration(workdir, worktrees / job_id) is None
    assert_intact(workdir, absent)


@pytest.mark.parametrize("left", ["locked-for-review", "another-commit"])
def test_c6_8_a_worktree_that_is_someones_is_kept_and_the_job_fails(state_daemon, slow_checkout, left):
    """C-6.8, C-13.4 a lock other than git's `initializing`, or a commit other than the job's, is not an add's leftover.

    Nothing at the path is removed: not the directory, not what is in it, not
    the registration. The job fails with rc 1 and says why and what to do.
    """
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    slow_checkout.gate.unlink()
    cut = head if left == "locked-for-review" else git(slow_checkout.workdir, "rev-parse", head + "~1")
    git(slow_checkout.workdir, "worktree", "add", "--detach", str(worktree), cut)
    if left == "locked-for-review":
        git(slow_checkout.workdir, "worktree", "lock", "--reason", "kept for review", str(worktree))
    (worktree / "theirs.txt").write_text("someone's\n")

    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and daemon.store.list_attempts(job_id) == []
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error_type"] == "SalvageError" and failed["transient"] is False
    reason = "is locked (kept for review)" if left == "locked-for-review" else f"is at commit {cut}, not the job's {head}"
    assert failed["error"].startswith(f"{worktree} {reason}, so it was kept; ")
    assert (worktree / "theirs.txt").read_text() == "someone's\n"
    assert git(worktree, "rev-parse", "HEAD") == cut
    assert registration(slow_checkout.workdir, worktree) == (
        ["locked kept for review"] if left == "locked-for-review" else [])


def test_c6_8_a_rebuild_drops_only_its_own_registration(state_daemon, tmp_path):
    """C-6.8 the discard never runs a repository-wide `git worktree prune`.

    Only the job's path is removed from the caller's repository. A worktree
    of the caller's whose directory is only absent for now keeps its HEAD,
    index, and reflog, and a commit made only there stays reachable.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    absent = absent_worktree(workdir, tmp_path)
    worktree = daemon.root / "worktrees" / job_id
    worktree.mkdir()                                       # what an add killed early leaves

    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert (worktree / "tracked.txt").read_text() == "baseline\n" and registration(workdir, worktree) == []
    assert_intact(workdir, absent)


def test_c6_8_a_link_at_the_worktree_path_is_removed_not_followed(state_daemon, tmp_path):
    """C-6.8 a link where the job's worktree belongs is not that worktree: it is unlinked, never followed.

    The worktree it points to, a checkout of the job's own commit, keeps its
    files and its registration; the job gets its own.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    theirs = tmp_path / "theirs"
    git(workdir, "worktree", "add", "--detach", str(theirs), "HEAD")
    (theirs / "theirs.txt").write_text("someone's\n")
    worktree = daemon.root / "worktrees" / job_id
    worktree.symlink_to(theirs)

    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert not worktree.is_symlink() and (worktree / ".git").is_file()
    assert registration(workdir, worktree) == [] and registration(workdir, theirs) == []
    assert (theirs / "theirs.txt").read_text() == "someone's\n" and (theirs / "tracked.txt").exists()


def test_c6_8_a_file_where_the_worktree_belongs_is_removed(state_daemon):
    """C-6.8 a path that is not a directory at all is not a worktree either: it goes, and the add follows."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    worktree.write_text("not a worktree\n")
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert (worktree / "tracked.txt").read_text() == "baseline\n" and registration(workdir, worktree) == []
