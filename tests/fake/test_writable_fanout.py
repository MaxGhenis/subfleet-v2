"""C-6.5: the writable hold is the worktree; a session is refused only its second live instance.

Incident, 2026-09-20: a session handing five stalled threads to parallel lanes
got one. Submit refused every later writable job with "caller_session is held by
a live job", whichever worktree it named. The contract only ever refused "a
writable job for a session id that already has one running from another
instance" (the 2026-09-04 twin); the code never compared instances.
"""

import json
import os

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters.base import AdapterError
from subfleet.procs import InspectionError, ProcessIdentity
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git

SESSION = "handoff-session"


def make_repo(path, branch="task/example"):
    path.mkdir(parents=True)
    git(path, "init", "-b", branch)
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("baseline\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")
    return path


@pytest.fixture
def fleet(state_daemon, monkeypatch):
    """A daemon, its harness, and a process table the test controls."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    daemon.policy["caps"].update(max_in_flight_per_lane=8, max_active_attempts=8)
    table = {100: ProcessIdentity(100, "boot-1", "Sun Sep 20 12:00:00 2026"),
             200: ProcessIdentity(200, "boot-1", "Sun Sep 20 13:00:00 2026")}

    def identity(pid):
        found = table.get(pid)
        if isinstance(found, Exception):
            raise found
        return found
    monkeypatch.setattr(daemon_module.procs, "identity", identity)
    return daemon, harness, table


def writable(daemon, harness, workdir, *, pid=100, session=SESSION, in_place=True, **overrides):
    return daemon.dispatch("submit", harness.submit_args(
        sandbox="workspace-write", in_place=in_place, workdir=str(workdir),
        caller_session=session, caller_pid=pid, **overrides))["job_id"]


def refusal(daemon, harness, workdir, **kwargs):
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, workdir, **kwargs)
    assert error.value.code == 7 and error.value.fix
    return str(error.value)


def test_c6_5_one_instance_fans_out_to_distinct_worktrees(fleet):
    """C-6.5 the incident: one session, one instance, K worktrees, K writable jobs admitted together."""
    daemon, harness, _ = fleet
    repos = [make_repo(harness.root / f"thread-{n}") for n in range(4)]
    jobs = [writable(daemon, harness, repo) for repo in repos]
    daemon._admit()
    assert [daemon.store.get_job(job)["state"] for job in jobs] == ["running"] * 4
    keys = sorted(row["lease_key"] for row in daemon.store.list_leases() if row["lease_key"].startswith(("worktree:", "session:")))
    assert keys == sorted(f"worktree:{os.path.realpath(repo)}" for repo in repos)
    recorded = daemon._submitted(jobs[0])
    assert recorded["caller_instance"] == {"pid": 100, "boot_id": "boot-1", "proc_start": "Sun Sep 20 12:00:00 2026"}
    assert recorded["write_target"] == os.path.realpath(repos[0])


def test_c6_5_jobs_with_allocated_worktrees_fan_out_from_one_checkout(fleet):
    """C-6.6 a job that is not in place writes in its own worktree, so one checkout can feed several."""
    daemon, harness, _ = fleet
    jobs = [writable(daemon, harness, harness.workdir, in_place=False) for _ in range(3)]
    daemon._admit()
    assert [daemon.store.get_job(job)["state"] for job in jobs] == ["running"] * 3
    held = {row["lease_key"] for row in daemon.store.list_leases() if row["lease_key"].startswith("worktree:")}
    assert held == {f"worktree:{daemon.root / 'worktrees' / job}" for job in jobs}


def test_c6_5_a_second_live_instance_of_the_session_is_refused(fleet):
    """C-6.5 the 2026-09-04 twin: same session id, another live process, any worktree."""
    daemon, harness, _ = fleet
    first = writable(daemon, harness, make_repo(harness.root / "a"), pid=100)
    message = refusal(daemon, harness, make_repo(harness.root / "b"), pid=200)
    assert first in message and "another live instance (pid 100)" in message
    assert len(daemon.store.list_jobs()) == 1


def test_c6_5_a_resumed_session_is_one_instance_not_two(fleet):
    """C-6.5 the first instance is provably gone: its detached job survives and the session dispatches again."""
    daemon, harness, table = fleet
    writable(daemon, harness, make_repo(harness.root / "a"), pid=100)
    del table[100]
    assert writable(daemon, harness, make_repo(harness.root / "b"), pid=200)
    # A recycled pid is a different process (C-5.3), so it is gone too.
    writable(daemon, harness, make_repo(harness.root / "c"), pid=200)
    table[200] = ProcessIdentity(200, "boot-1", "Sun Sep 20 15:00:00 2026")
    table[300] = ProcessIdentity(300, "boot-1", "Sun Sep 20 15:30:00 2026")
    assert writable(daemon, harness, make_repo(harness.root / "d"), pid=300)


@pytest.mark.parametrize("pid,seconds,expected", [
    (100, "1789915544", "allowed"),
    (200, "1789915544", "another live instance"),
    (100, "1789915542", "cannot be identified"),
    (200, "1789915542", "cannot be identified"),
])
def test_c6_5_legacy_caller_identity_keeps_writer_protection(fleet, monkeypatch, pid, seconds, expected):
    """A UUID upgrade or clock correction must not manufacture a dead caller."""
    daemon, harness, table = fleet
    start = table[100].proc_start
    table[100] = ProcessIdentity(100, "1789915544", start)
    first = writable(daemon, harness, make_repo(harness.root / "legacy"))
    boot = "66355737-51db-46d4-8f31-c928bc955e16"
    table[100] = ProcessIdentity(100, boot, start)
    table[200] = ProcessIdentity(200, boot, table[200].proc_start)
    monkeypatch.setattr(daemon_module.procs, "_read", lambda argv: "{ sec = " + seconds + " }")
    target = make_repo(harness.root / "new")
    if expected == "allowed":
        assert writable(daemon, harness, target, pid=pid)
    else:
        message = refusal(daemon, harness, target, pid=pid)
        assert first in message and expected in message


@pytest.mark.parametrize("case", ["no-pid", "caller-uninspectable", "holder-uninspectable", "holder-unrecorded"])
def test_c6_5_an_instance_that_cannot_be_identified_is_refused(fleet, case):
    """C-6.5 fails closed: what cannot be told apart from the twin is treated as the twin."""
    daemon, harness, table = fleet
    first = writable(daemon, harness, make_repo(harness.root / "a"), pid=100)
    pid = 200
    if case == "no-pid":
        pid = None
    elif case == "caller-uninspectable":
        table[200] = InspectionError("ps inspection unavailable")
    elif case == "holder-uninspectable":
        table[100] = InspectionError("ps inspection unavailable")
    else:
        daemon.store.connection.execute("UPDATE events SET data_json='{}' WHERE job_id=? AND kind='job.submitted'", (first,))
    message = refusal(daemon, harness, make_repo(harness.root / "b"), pid=pid)
    assert first in message and "cannot be identified" in message


def test_c6_5_a_first_writable_job_needs_no_identity(fleet):
    """C-6.5 with nothing live there is no second instance to be."""
    daemon, harness, _ = fleet
    assert writable(daemon, harness, make_repo(harness.root / "a"), pid=None)
    assert "caller_instance" not in daemon._submitted(daemon.store.list_jobs()[0]["job_id"])


@pytest.mark.parametrize("where", ["same", "subdirectory"])
@pytest.mark.parametrize("who", ["same-instance", "another-session", "no-session"])
def test_c6_5_a_second_writer_in_one_checkout_is_refused_for_anyone(fleet, where, who):
    """C-6.5 `/repo` and `/repo/sub` are one place to write, whoever asks."""
    daemon, harness, _ = fleet
    repo = make_repo(harness.root / "a")
    (repo / "sub").mkdir()
    first = writable(daemon, harness, repo)
    session = {"same-instance": SESSION, "another-session": "someone-else", "no-session": None}[who]
    message = refusal(daemon, harness, repo / "sub" if where == "subdirectory" else repo, session=session,
                      pid=100 if session else None)
    assert first in message or "held by a live job" in message
    # A job that is not in place writes elsewhere, so the same checkout may feed it.
    assert writable(daemon, harness, repo, in_place=False, session="a-third", pid=200)


def test_c6_5_the_checkout_stays_held_once_admitted(fleet):
    """C-6.3 the lease is on the checkout, so a subdirectory is refused after admission as before it."""
    daemon, harness, _ = fleet
    repo = make_repo(harness.root / "a")
    (repo / "sub").mkdir()
    job = writable(daemon, harness, repo / "sub")
    daemon._admit()
    assert [row["lease_key"] for row in daemon.store.list_leases(job) if row["lease_key"].startswith("worktree:")] == [f"worktree:{os.path.realpath(repo)}"]
    assert daemon.store.get_job(job)["worktree"] == str(repo / "sub")     # the provider still starts where -C said
    daemon.store.update_job(job, state="succeeded")                      # a job row gone terminal with its lease still held
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, repo, session="someone-else", pid=200)
    assert "worktree has a lease" in str(error.value)


def test_c6_5_linked_worktrees_of_one_repository_are_separate_places(fleet):
    """C-6.5 two linked worktrees share objects, not a working tree."""
    daemon, harness, _ = fleet
    repo = make_repo(harness.root / "a")
    linked = harness.root / "a-linked"
    git(repo, "worktree", "add", "-b", "task/linked", str(linked))
    jobs = [writable(daemon, harness, repo), writable(daemon, harness, linked)]
    daemon._admit()
    assert [daemon.store.get_job(job)["state"] for job in jobs] == ["running"] * 2


def test_c6_5_a_job_submitted_before_targets_were_recorded_still_holds_its_checkout(fleet):
    """C-6.5 a live job from an older release has no recorded target; git answers for it."""
    daemon, harness, _ = fleet
    repo = make_repo(harness.root / "a")
    (repo / "sub").mkdir()
    first = writable(daemon, harness, repo / "sub", session="older")
    daemon.store.connection.execute("UPDATE events SET data_json='{}' WHERE job_id=? AND kind='job.submitted'", (first,))
    assert first in refusal(daemon, harness, repo, session="newer", pid=200)


@pytest.mark.parametrize("revive_first", [True, False])
def test_c23_54_a_revive_is_always_another_instance(fleet, revive_first):
    """C-23.54 a revive continues the session headless, so it never coexists with the session's writable jobs."""
    daemon, harness, _ = fleet
    a, b = make_repo(harness.root / "a"), make_repo(harness.root / "b")
    kinds = ("revive", "dispatch") if revive_first else ("dispatch", "revive")
    first = writable(daemon, harness, a, kind=kinds[0], pinned_model="astra", pid=None if kinds[0] == "revive" else 100)
    message = refusal(daemon, harness, b, kind=kinds[1], pinned_model="astra", pid=None if kinds[1] == "revive" else 100)
    assert first in message and "revive" in message


def test_c6_5_the_per_session_cap_is_a_backstop(fleet):
    """C-6.4 `max_writable_per_session` bounds a runaway caller and names itself."""
    daemon, harness, _ = fleet
    daemon.policy["caps"]["max_writable_per_session"] = 2
    for name in "ab":
        writable(daemon, harness, make_repo(harness.root / name))
    assert "caps.max_writable_per_session 2" in refusal(daemon, harness, make_repo(harness.root / "c"))
    assert writable(daemon, harness, make_repo(harness.root / "d"), session="another-session")


def test_c6_5_the_inserting_transaction_refuses_a_job_it_was_not_cleared_against(fleet):
    """C-3.3 the transaction re-checks with SQL alone and never trusts a stale clearance."""
    daemon, harness, _ = fleet
    first = writable(daemon, harness, make_repo(harness.root / "a"))
    job = {"job_id": "new", "sandbox": "workspace-write", "in_place": False, "workdir": "/nowhere",
           "caller_session": SESSION}
    daemon._validate_conflicts(job, frozenset({first}))
    with pytest.raises(AdapterError) as error:
        daemon._validate_conflicts(job, frozenset())
    assert first in str(error.value)


def test_c6_5_read_only_jobs_are_never_held(fleet):
    """C-6.5 the rule is about writers: a session's read-only jobs are untouched by it."""
    daemon, harness, _ = fleet
    writable(daemon, harness, make_repo(harness.root / "a"), pid=100)
    for _ in range(3):
        daemon.dispatch("submit", harness.submit_args(caller_session=SESSION, caller_pid=200))
    assert len(daemon.store.list_jobs()) == 4
