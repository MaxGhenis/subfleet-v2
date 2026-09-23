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
#: `caps.worktree_add_timeout_s` in the tests that fire the cap themselves,
#: once the checkout is held (`cap_at_the_filter`): a watchdog, never reached.
WATCHDOG_S = 60
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
    """C-6.8 what a killed `worktree add` and the removal that raced it left is never handed to a provider.

    git registers the path and locks it `initializing` before it makes the
    directory, so such a leftover is registered. Here the old removal took
    `.git` while the checkout wrote on, stopped mid-file.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    leftover = daemon.root / "worktrees" / job_id
    git(workdir, "worktree", "add", "--lock", "--reason", "initializing", "--detach", str(leftover), head)
    (leftover / ".git").unlink()
    (leftover / "tracked.txt").write_text("base")
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved"
    assert (leftover / ".git").is_file() and registration(workdir, leftover) == []
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
    assert (record["error_type"], record["error"]) == ("TimeoutExpired", "git worktree add timed out after 1 s")
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
    """The lines git lists for `worktree` beyond its path and HEAD, or None if unregistered.

    A path is compared with its directory resolved and its last component
    kept, so a registration whose path is now a link is still found.
    """
    def lexical(path):
        return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    listing = subprocess.run(["git", "-C", str(repository_path), "worktree", "list", "--porcelain", "-z"],
                             check=True, capture_output=True, text=True).stdout
    for stanza in listing.split("\0\0"):
        fields = [field for field in stanza.split("\0") if field]
        if fields and lexical(fields[0].removeprefix("worktree ")) == lexical(str(worktree)):
            return [field for field in fields[1:] if not field.startswith("HEAD ") and field != "detached"]
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


def fingerprint(path, workdir):
    """Everything at `path`, and its registration: equal before and after means nothing there was touched."""
    if path.is_symlink():
        there = ("link", os.readlink(path))
    elif path.is_file():
        there = ("file", path.read_bytes())
    else:
        there = ("directory", sorted((str(item.relative_to(path)), item.is_symlink() or not item.is_file()
                                      or item.read_bytes()) for item in path.rglob("*")))
    return there, registration(workdir, path)


def post_checkout_hook(tmp_path, body):
    """A hooks directory whose post-checkout hook runs `body`; git runs it after it has unlocked the new worktree."""
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return hooks


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


@pytest.fixture
def cap_at_the_filter(state_daemon, slow_checkout, monkeypatch):
    """The add's cap fires once its checkout is held in the filter, however long it took to get there.

    `caps.worktree_add_timeout_s` is only the watchdog here: on a loaded
    machine a fixed cap could fire before the checkout starts, and the tests
    below need the kill to land mid-checkout.
    """
    daemon, _ = state_daemon
    daemon.policy["caps"]["worktree_add_timeout_s"] = WATCHDOG_S
    real_wait = subprocess.Popen.wait
    fired = []

    def wait(process, timeout=None):
        # Only the first add: the retry's must be let through.
        if timeout is not None and list(process.args[3:5]) == ["worktree", "add"] and not fired:
            fired.append(process)

            def held_or_gone():
                return len(recorded(slow_checkout.pids)) == 2 or process.poll() is not None
            wait_for(held_or_gone, timeout_s=timeout)
            raise subprocess.TimeoutExpired(process.args, timeout)
        return real_wait(process, timeout)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)


def start_add(checkout, worktree, head, env=None):
    """A `git worktree add` in a session of its own, held in its checkout's filter."""
    process = subprocess.Popen(["git", "-C", str(checkout.workdir), "worktree", "add", "--detach", str(worktree), head],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=env, start_new_session=True)

    def reached_the_filter():
        return len(recorded(checkout.pids)) == 2
    try:
        wait_for(reached_the_filter)
    except BaseException:
        # Teardown kills only what the filter recorded; an add that never got
        # there would outlive the test.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
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


def test_c6_8_an_add_past_its_cap_stops_its_whole_checkout(state_daemon, slow_checkout, cap_at_the_filter,
                                                          monkeypatch, tmp_path):
    """C-6.8, C-5.4 the cap stops the add's whole group, and what it made goes before the retry.

    Its `git reset --hard` and the filter that one runs no longer outlive it;
    the directory, its registration, and git's `initializing` lock are gone
    when the pass ends, so the retry cuts a whole checkout. The deferral
    carries git's stderr. No other registration in the caller's repository
    is touched.
    """
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module.procs, "containment", REAL_CONTAINMENT)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    absent = absent_worktree(slow_checkout.workdir, tmp_path)
    daemon._admit()

    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error_type"] == "TimeoutExpired"
    assert record["error"].startswith(f"git worktree add timed out after {WATCHDOG_S} s: Preparing worktree (detached")
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


@pytest.mark.parametrize("left", ["killed-mid-checkout", "locked-whole-checkout", "registered-without-directory",
                                  "raced-removal"])
def test_c6_8_what_an_unfinished_add_left_is_rebuilt(state_daemon, slow_checkout, monkeypatch, left):
    """C-6.8 what an add that never finished left is removed and added again, never reused.

    `killed-mid-checkout` is what a daemon killed mid-add leaves: a `.git`
    link and a HEAD that answers, which passed before, with git's
    `initializing` lock, files missing, and no index. `locked-whole-checkout`
    was killed after the index and before the unlock.
    `registered-without-directory` is what the old discard left (and
    `thesis-tsa-signers` is): the directory removed, the registration kept by
    its lock. `raced-removal` is that removal raced by the checkout: files of
    the commit and no `.git`.
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
    if left == "registered-without-directory":
        shutil.rmtree(worktree)
    if left == "raced-removal":
        (worktree / ".git").unlink()
    assert registration(slow_checkout.workdir, worktree) == ["locked initializing"]

    daemon._admit()
    whole_checkout(daemon, slow_checkout.workdir, job_id)
    assert f"job {job_id}: {worktree} is not a finished checkout of {head}" in log_text(daemon)


@pytest.mark.parametrize("left", [
    "locked-for-review", "another-commit", "unlocked-without-index", "untracked-in-an-unfinished-add",
    "staged-after-it-finished", "moved-behind-a-link", "zeros-with-no-lock", "no-registration", "a-file",
])
def test_c6_8_what_is_not_an_unfinished_adds_leftover_is_kept_and_the_job_fails(state_daemon, slow_checkout, tmp_path,
                                                                               left):
    """C-6.8 anything but an unfinished add's leftover is someone's: nothing there is removed, and the job fails with rc 1.

    Someone's lock; another commit; a killed add someone unlocked, or one
    holding a file the commit does not have; a finished checkout changed
    since; a locked registration whose directory was moved and a link left in
    its place (the lock is still found); zeros for HEAD with no add's lock
    (as when git cannot read it); files nothing registers; a file.
    """
    daemon, harness = state_daemon
    workdir = slow_checkout.workdir
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    if left in ("unlocked-without-index", "untracked-in-an-unfinished-add"):
        killed_add(slow_checkout, worktree, head)
    else:
        slow_checkout.gate.unlink()
    if left in ("locked-for-review", "staged-after-it-finished", "moved-behind-a-link", "zeros-with-no-lock"):
        git(workdir, "worktree", "add", "--detach", str(worktree), head)
    reasons = {
        "locked-for-review": "it is locked (kept for review)",
        "another-commit": f"its HEAD is {git(workdir, 'rev-parse', head + '~1')}, not {head}",
        "unlocked-without-index": "it holds files, and no add that never finished",
        "untracked-in-an-unfinished-add": f"it holds notes.txt, which {head} does not",
        "staged-after-it-finished": "it holds files, and no add that never finished",
        "moved-behind-a-link": "it is locked (kept for review)",
        "zeros-with-no-lock": f"its HEAD is {'0' * 40}, not {head}",
        "no-registration": "it holds files, and no add that never finished",
        "a-file": "it is not a directory",
    }
    if left in ("locked-for-review", "moved-behind-a-link"):
        git(workdir, "worktree", "lock", "--reason", "kept for review", str(worktree))
    if left == "another-commit":
        git(workdir, "worktree", "add", "--detach", str(worktree), head + "~1")
    if left == "unlocked-without-index":
        git(workdir, "worktree", "unlock", str(worktree))
    if left == "untracked-in-an-unfinished-add":
        (worktree / "notes.txt").write_text("someone's notes\n")
    if left == "staged-after-it-finished":
        (worktree / "z-after.txt").write_text("someone's edit\n")
        git(worktree, "add", "z-after.txt")
    if left == "moved-behind-a-link":
        moved = tmp_path / "moved"
        worktree.rename(moved)
        worktree.symlink_to(moved)
        moved_before = fingerprint(moved, workdir)
    if left == "zeros-with-no-lock":
        Path(git(worktree, "rev-parse", "--absolute-git-dir"), "HEAD").write_text("0" * 40 + "\n")
    if left == "no-registration":
        worktree.mkdir()
        (worktree / "tracked.txt").write_text("someone's\n")
    if left == "a-file":
        worktree.write_text("someone's\n")
    before = fingerprint(worktree, workdir)

    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and daemon.store.list_attempts(job_id) == []
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error_type"] == "SalvageError" and failed["transient"] is False
    assert failed["error"].startswith(f"{worktree} is not a finished checkout of {head} (")
    assert f") and was kept, because {reasons[left]}" in failed["error"]
    assert failed["error"].endswith("; remove it, then submit the job again")
    assert fingerprint(worktree, workdir) == before
    if left == "moved-behind-a-link":
        assert fingerprint(moved, workdir) == moved_before


def test_c6_8_a_finished_checkout_is_reused_as_it_is(state_daemon, slow_checkout, monkeypatch):
    """C-6.8 a checkout cut before is used again with no new add, after one census on first sight.

    The census is for an add that outlived a restart and is still in its
    post-checkout hook; once none is found, later passes read no census.
    """
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    slow_checkout.gate.unlink()
    git(slow_checkout.workdir, "worktree", "add", "--detach", str(worktree), head)
    inode = (worktree / "z-after.txt").stat().st_ino
    censuses = []
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: censuses.append(args) or Containment())
    job = daemon.store.get_job(job_id)
    daemon._allocate_worktree(job, str(worktree))
    daemon._allocate_worktree(job, str(worktree))
    assert censuses == [(None, None, None, f"worktree-add:{job_id}")]
    daemon._admit()
    whole_checkout(daemon, slow_checkout.workdir, job_id)
    assert (worktree / "z-after.txt").stat().st_ino == inode and len(censuses) == 1


def test_c6_8_a_checkout_whose_post_checkout_hook_still_runs_is_waited_for(state_daemon, slow_checkout, monkeypatch,
                                                                          tmp_path):
    """C-6.8 git unlocks a worktree before its post-checkout hook, so a finished checkout does not prove its add is done.

    An add that outlived its daemon is held in such a hook, with every test
    of a finished checkout passing. The next daemon's first look finds the
    add's marker and waits; once the hook is done, the checkout, with what the
    hook wrote, is the job's.
    """
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module.procs, "containment", REAL_CONTAINMENT)
    workdir = slow_checkout.workdir
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    slow_checkout.gate.unlink()
    started, hook_gate = tmp_path / "hook-started", tmp_path / "hook-gate"
    hook_gate.touch()
    hooks = post_checkout_hook(tmp_path, f": > '{started}'\nwhile [ -e '{hook_gate}' ]; do sleep 0.02; done\n"
                                         ": > generated-by-hook\n")
    env = {**os.environ, "SUBFLEET_ATTEMPT": f"worktree-add:{job_id}", "SUBFLEET_ROOT": str(daemon.root)}
    process = subprocess.Popen(["git", "-c", f"core.hooksPath={hooks}", "-C", str(workdir), "worktree", "add",
                                "--detach", str(worktree), head],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=env, start_new_session=True)
    try:
        wait_for(started.exists)
        assert daemon_module.check_worktree(workdir, worktree, head).unfinished is None
        daemon._admit()
        job = daemon.store.get_job(job_id)
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
        [record] = events(daemon, job_id, "job.workspace_deferred")
        assert "git worktree add for this job still running" in record["error"]
        assert process.poll() is None and daemon.store.list_attempts(job_id) == []
    finally:
        hook_gate.unlink(missing_ok=True)
        assert process.wait(timeout=30) == 0
    due(daemon, job_id)
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert (worktree / "generated-by-hook").exists()


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


@pytest.mark.parametrize("settles", [True, False])
def test_c6_8_an_add_past_its_cap_is_removed_only_on_a_verified_empty_census(state_daemon, slow_checkout,
                                                                            cap_at_the_filter, monkeypatch, settles):
    """C-6.8, C-5.6 the census is re-read within `kill_settle_s`, and only an empty one removes anything.

    `settles`: two censuses still see the add, the third does not, and then
    what it left goes. Otherwise the window ends with the add still seen:
    nothing is removed, the job waits as for any add past its cap, and the
    next pass, with the add gone, removes it and cuts a whole checkout.
    """
    daemon, harness = state_daemon
    daemon.kill_settle_s = 5 if settles else .2
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    seen = Containment(group_pids=frozenset({os.getpid()}))
    script = [seen, seen, Containment()] if settles else []
    calls = []

    def census(*args, **kwargs):
        calls.append(args)
        return script[len(calls) - 1] if len(calls) <= len(script) else seen
    monkeypatch.setattr(daemon_module.procs, "containment", census)
    daemon._admit()
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error_type"] == "TimeoutExpired"
    if settles:
        assert len(calls) == 3 and not worktree.exists() and registration(slow_checkout.workdir, worktree) is None
    else:
        assert len(calls) >= 2
        assert (worktree / ".git").is_file() and registration(slow_checkout.workdir, worktree) == ["locked initializing"]
        assert (f"job {job_id}: git worktree add stopped at its cap, but pids [{os.getpid()}] remain"
                in log_text(daemon))

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


@pytest.mark.parametrize("census", ["empty", "a-process-remains"])
def test_c6_8_a_failed_add_is_removed_only_once_nothing_of_it_runs(state_daemon, tmp_path, monkeypatch, census):
    """C-6.8, C-5.6 git exits 1 when a post-checkout hook fails, and keeps the checkout it made.

    The job fails with rc 1 and git's stderr. The add's group is read after
    git has exited (it is not signalled: its leader is reaped, C-5.4), and
    what the add left goes only on a verified-empty census; a process of it
    still alive leaves it for later, and says so.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    daemon.kill_settle_s = .2
    git(workdir, "config", "core.hooksPath", str(post_checkout_hook(tmp_path, "echo the hook failed >&2\nexit 1\n")))
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    calls = []

    def containment(*args, **kwargs):
        calls.append(args)
        return Containment() if census == "empty" else Containment(group_pids=frozenset({os.getpid()}))
    monkeypatch.setattr(daemon_module.procs, "containment", containment)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and daemon.store.list_attempts(job_id) == []
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error"].startswith("git worktree add failed: ") and "the hook failed" in failed["error"]
    assert calls and calls[0][0] is not None and calls[0][3] == f"worktree-add:{job_id}"
    if census == "empty":
        assert not worktree.exists() and registration(workdir, worktree) is None
    else:
        assert (worktree / "tracked.txt").exists() and registration(workdir, worktree) == []
        assert f"job {job_id}: git worktree add failed, but pids [{os.getpid()}] remain" in log_text(daemon)


def test_c6_8_a_failed_add_never_removes_a_lock_that_is_someones(state_daemon, tmp_path):
    """C-6.8 the discard reads the registration again: a lock put on the new worktree after git's own is kept.

    Here a post-checkout hook locks the checkout for review and fails, so the
    add fails; the checkout and the lock stay.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    hook = 'git worktree lock --reason "operator review" "$PWD"\nexit 1\n'
    git(workdir, "config", "core.hooksPath", str(post_checkout_hook(tmp_path, hook)))
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    worktree = daemon.root / "worktrees" / job_id
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1)
    assert (worktree / "tracked.txt").exists() and registration(workdir, worktree) == ["locked operator review"]
    assert f"job {job_id}: {worktree} was not removed, because it is locked (operator review)" in log_text(daemon)


def test_c6_8_a_repository_that_cannot_be_read_removes_nothing(state_daemon):
    """C-6.8 the registration listing is never optional: a failed one is not "not registered".

    The caller's repository has moved away. The worktree would be rebuilt if
    the listing were read, and holds the only copy of an edit; the job fails
    with git's error and nothing at the path is touched.
    """
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    head = daemon.store.get_job(job_id)["workdir_head"]
    worktree = daemon.root / "worktrees" / job_id
    git(workdir, "worktree", "add", "--lock", "--reason", "initializing", "--detach", str(worktree), head)
    (worktree / "tracked.txt").write_text("the only copy\n")
    moved = workdir.with_name(workdir.name + "-moved")
    workdir.rename(moved)
    try:
        daemon._admit()
    finally:
        moved.rename(workdir)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1)
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error"].startswith("git worktree failed: ") and failed["transient"] is False
    assert (worktree / "tracked.txt").read_text() == "the only copy\n"
    assert registration(workdir, worktree) == ["locked initializing"]


def test_c6_8_a_linked_worktrees_directory_is_never_written_below(state_daemon, tmp_path):
    """C-6.8 `worktrees/` itself a link would turn an outside directory into the daemon's: nothing is made or removed there."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    container, elsewhere = daemon.root / "worktrees", tmp_path / "elsewhere"
    container.rename(elsewhere)
    container.symlink_to(elsewhere)
    (elsewhere / job_id).mkdir()
    (elsewhere / job_id / "unique.txt").write_text("someone's\n")
    try:
        daemon._admit()
    finally:
        container.unlink()
        elsewhere.rename(container)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1)
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error"] == f"{container} is a link, so no worktree is made or removed below it"
    assert (container / job_id / "unique.txt").read_text() == "someone's\n"


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
