"""C-6.8: a workspace preparation failure is recorded, and a transient one waits.

Incident, 2026-09-20: under a load average near 10 a writable job went from
queued to failed in 19 s with "workspace preparation failed", no attempt, and
nothing in daemon.log. The snapshot's git calls were capped at 15 s, the
exception was discarded, and the failure was terminal. Twelve jobs since
2026-09-19 ended that way, read-only ones included.
"""

import errno
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.daemon import after
from subfleet.salvage import SalvageError
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git

REAL_GIT = shutil.which("git")


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


def test_c6_8_a_snapshot_failure_that_quotes_a_name_that_is_not_utf8_fails_the_job_with_it(state_daemon, monkeypatch):
    """Review of cda4c161, N1, at admission: the baseline snapshot's `add -A` quotes such a
    name (faked, as APFS refuses it; every other git call is real). Carried as a surrogate,
    it reached the job's notice, which SQLite could not encode: the admission pass raised
    on every try, holding every job behind it."""
    from tests.unit.test_salvage_unindexable import fake_add
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))["job_id"]
    fake_add(monkeypatch, 128, b'error: open("caf\xe9.txt"): Permission denied\nfatal: adding files failed\n')
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and daemon.store.list_attempts(job_id) == []
    error = 'git add failed: error: open("caf\\xe9.txt"): Permission denied\nfatal: adding files failed'
    assert f"workspace preparation failed: SalvageError: {error}" in daemon.store.list_notices()[0]["text"]
    [failed] = events(daemon, job_id, "job.workspace_failed")
    assert failed["error"] == error and failed["transient"] is False


def test_c6_8_a_worktree_add_that_quotes_a_name_that_is_not_utf8_fails_with_it(state_daemon, monkeypatch):
    """`git worktree add` prints a file it could not check out in its own bytes; read as
    strict UTF-8 that raised `UnicodeDecodeError` out of the admission pass on every try.
    The call is faked (APFS refuses such names) and decodes as `subprocess` would."""
    import subfleet.daemon as daemon_module
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if "worktree" in cmd and "add" in cmd:
            stderr = b"error: unable to create file caf\xe9.txt: Permission denied\nfatal: could not reset\n"
            decoded = stderr.decode("utf-8", kwargs.get("errors") or "strict") if kwargs.get("text") else stderr
            return subprocess.CompletedProcess(cmd, 128, "" if kwargs.get("text") else b"", decoded)
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(daemon_module.subprocess, "run", run)
    daemon._admit()
    assert daemon.store.get_job(job_id)["state"] == "failed"
    assert ("could not allocate worktree: error: unable to create file caf\\xe9.txt: Permission denied"
            in daemon.store.list_notices()[0]["text"])


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
