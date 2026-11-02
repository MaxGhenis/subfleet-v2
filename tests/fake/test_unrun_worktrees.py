"""C-6.20 (with C-6.12 and C-6.17): a tree admission cut for a job no attempt used is given back.

Incident, 2026-11-02: with every Codex lane held for the Microcosm US release, 164
folders under `worktrees/` belonged to jobs that never had an attempt at 18:10:58Z,
8.54 GB in all: 125 of jobs that had ended, 39 of jobs still waiting. `_workspace`
cuts a job's tree on its first look, before routing; a job that then waits keeps it,
and one cancelled keeps it until retention retires the job, which can be days later.
Only refused jobs gave theirs back (`_discard_fresh_worktree`).

These tests run the real admission pass and real git against a fake disk.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as daemon_module, dependencies, folders
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import utcnow
from subfleet.disk import GB
from tests.fake.test_admission_latency import measure
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture of `fleet`)
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_unadmittable_pin_admission import fleet  # noqa: F401
from tests.unit.test_salvage import git


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "task/example")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("baseline\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")
    return path


@pytest.fixture
def offers(state_daemon, monkeypatch):  # noqa: F811
    """`_schedule` calls, recorded rather than run, so each test runs the sweep itself."""
    daemon, _ = state_daemon
    calls = []
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kwargs: calls.append(key))
    return calls


def disk(daemon, monkeypatch, free):
    reading = {"gb": free}
    daemon.policy["admission"]["disk"]["enabled"] = True
    monkeypatch.setattr("subfleet.disk.free_bytes", lambda path: int(reading["gb"] * GB))
    return reading


def submit(daemon, harness, repo, **changes):
    args = harness.submit_args(**{"sandbox": "workspace-write", "workdir": str(repo), **changes})
    return daemon.dispatch("submit", args)["job_id"]


def cut(daemon, job_id):
    """What a job's first admission look does before routing (`_admit_pass`)."""
    workspace, _, _, _ = daemon._workspace(daemon.store.get_job(job_id))
    assert workspace == str(daemon.root / "worktrees" / job_id) and os.path.isdir(workspace)
    return workspace


def registered(repo):
    return subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout


def swept(daemon):
    """One sweep that ran to its end, and how many trees it gave back. A failure, a busy pass
    lock or a stop leaves the sweep owed (`_unrun_dirty`), and a test that expects a tree kept
    must not pass because the sweep never got to it."""
    given = daemon._sweep_unrun_worktrees()
    assert not daemon._unrun_dirty, "the sweep did not run to its end"
    return given


def given_back(daemon, job_id):
    return daemon.store.query("SELECT data_json FROM events WHERE job_id=? AND kind='job.worktree_given_back'",
                              (job_id,))


def test_a_job_cancelled_before_any_attempt_gives_back_its_tree(state_daemon, repo, offers):  # noqa: F811
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    assert job_id in registered(repo)
    offers.clear()
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    assert daemon.store.get_job(job_id)["state"] == "cancelled"
    assert offers == ["unrun-worktrees"]
    assert swept(daemon) == 1
    assert not os.path.lexists(tree)
    assert job_id not in registered(repo)                 # its registration only (d635: no prune)
    (event,) = given_back(daemon, job_id)
    data = json.loads(event["data_json"])
    assert data["why"] == "ended unrun" and data["common_dir"] == os.path.realpath(repo / ".git")
    assert "registration_left" not in data
    assert swept(daemon) == 0           # nothing left to give back


def test_a_link_in_place_of_a_tree_leaves_the_tree_it_names_registered(state_daemon, repo, offers,  # noqa: F811
                                                                        tmp_path):
    """Invariant 1: a link where a job's tree should be is never followed. `_discard_worktree`
    removes nothing through it, but `discard_registration` resolves `<tree>/.git`, so a sweep
    that let the link through would drop the registration of the worktree it names, with the
    HEAD and reflogs only that registration holds (mutant `rule-link-followed`)."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "-b", "other", str(other))
    (daemon.root / "worktrees").mkdir(exist_ok=True)
    link = daemon.root / "worktrees" / job_id
    link.symlink_to(other, target_is_directory=True)
    assert swept(daemon) == 0
    assert link.is_symlink() and (other / "tracked.txt").read_text() == "baseline\n"
    assert (repo / ".git" / "worktrees" / "other" / "gitdir").is_file()
    assert not given_back(daemon, job_id)


def test_a_give_back_leaves_a_stale_registration_elsewhere_in_the_repository(state_daemon, repo, offers,  # noqa: F811
                                                                            tmp_path):
    """d635: the sweep drops its own tree's registration and never runs a repository-wide
    `git worktree prune`, which would also drop this one, whose tree is gone, with the HEAD and
    reflogs only it holds."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    cut(daemon, job_id)
    stale = tmp_path / "stale"
    git(repo, "worktree", "add", "-q", "-b", "stale", str(stale))
    (stale / "tracked.txt").unlink()
    stale.joinpath(".git").unlink()
    stale.rmdir()
    prunable = subprocess.run(["git", "-C", str(repo), "worktree", "prune", "--dry-run", "-v"],
                              capture_output=True, text=True, check=True)
    assert "worktrees/stale" in prunable.stdout + prunable.stderr      # a prune would drop it
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    assert swept(daemon) == 1
    assert job_id not in registered(repo)
    assert (repo / ".git" / "worktrees" / "stale" / "gitdir").is_file()


@pytest.mark.parametrize("shape", ["read-only", "in-place", "push", "recorded", "turn"])
def test_a_cancel_outside_the_sweeps_scope_offers_no_sweep(state_daemon, repo, offers, shape):  # noqa: F811
    """A cancel offers the sweep only for a job `UNRUN_TREES` could hold: conversation stops
    cancel turn jobs, which run in place, many times an hour."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo, **({"sandbox": "read-only"} if shape == "read-only" else {}))
    column = {"in-place": ("in_place", 1), "push": ("push_branch", "push/branch"),
              "recorded": ("worktree", str(repo)), "turn": ("kind", "turn")}.get(shape)
    if column:
        with daemon.store.transaction("test.shape") as tx:
            tx.execute(f"UPDATE jobs SET {column[0]}=? WHERE job_id=?", (column[1], job_id))
    offers.clear()
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    assert daemon.store.get_job(job_id)["state"] == "cancelled"
    assert offers == []


def test_the_cancels_scope_check_is_the_sweeps_query(state_daemon):  # noqa: F811
    """Differential: `_unrun_scope`, which gates a cancel's offer, holds for a job's columns
    exactly when `UNRUN_TREES` would select the job, over every combination of those columns."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    seen = set()
    for sandbox in ("workspace-write", "read-only"):
        for in_place in (0, 1):
            for worktree in (None, "/tmp/recorded"):
                for push in (None, "push/branch"):
                    for kind in ("dispatch", "turn"):
                        with daemon.store.transaction("test.shape") as tx:
                            tx.execute("UPDATE jobs SET sandbox=?,in_place=?,worktree=?,push_branch=?,kind=? "
                                       "WHERE job_id=?", (sandbox, in_place, worktree, push, kind, job_id))
                        selected = job_id in {row["job_id"] for row in daemon.store.query(daemon_module.UNRUN_TREES)}
                        assert daemon_module._unrun_scope(daemon.store.get_job(job_id)) is selected
                        seen.add(selected)
    assert seen == {True, False}


def test_the_disk_hold_takes_back_a_waiting_tree_and_placement_cuts_it_again(state_daemon, repo, offers,  # noqa: F811
                                                                            monkeypatch):
    daemon, harness = state_daemon
    reading = disk(daemon, monkeypatch, 39)
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    head = daemon.store.get_job(job_id)["workdir_head"]
    offers.clear()
    daemon._admit()
    assert daemon._holds[job_id]["reason"] == "disk"
    assert offers == ["unrun-worktrees"]                  # once for the job's hold ...
    daemon._admit()
    assert offers == ["unrun-worktrees"]                  # ... not on every pass that holds it
    assert swept(daemon) == 1
    assert not os.path.lexists(tree) and job_id not in registered(repo)
    assert daemon.store.get_job(job_id)["state"] in ("queued", "waiting")
    (event,) = given_back(daemon, job_id)
    assert '"why": "disk hold"' in event["data_json"]
    reading["gb"] = 46                                    # the hold lets go: placement cuts the tree again
    measure(daemon, "codex-1")
    daemon._admit()
    (attempt,) = daemon.store.list_attempts(job_id)
    job = daemon.store.get_job(job_id)
    assert job["worktree"] == tree and os.path.isdir(tree)
    assert git(tree, "rev-parse", "HEAD").strip() == head
    assert attempt["state"] in ("reserved", "starting", "running")
    assert swept(daemon) == 0           # an attempt's tree is never given back


def test_a_waiting_job_that_is_not_disk_held_keeps_its_tree(state_daemon, repo, offers):  # noqa: F811
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    daemon._holds_by_kind = {**daemon._holds_by_kind, "detached": {job_id: {"reason": "fleet-full"}}}
    assert swept(daemon) == 0
    assert os.path.isdir(tree)


def test_a_tree_another_live_job_needs_is_kept_until_that_job_ends(state_daemon, repo, offers):  # noqa: F811
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    daemon.dispatch("kill", {"job_id": job_id})
    guest = daemon.dispatch("submit", harness.submit_args(workdir=tree))["job_id"]   # read-only, in that tree
    assert swept(daemon) == 0
    assert os.path.isdir(tree)
    daemon.dispatch("kill", {"job_id": guest})
    assert swept(daemon) == 1
    assert not os.path.lexists(tree)


def test_never_beside_retention_and_retention_sweeps_after_its_pass(state_daemon, repo, offers,  # noqa: F811
                                                                     monkeypatch):
    daemon, harness = state_daemon
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    early = submit(daemon, harness, repo, caller_session="early")
    early_tree = cut(daemon, early)
    daemon.dispatch("kill", {"job_id": early})            # its offer is recorded, not run (`offers`)
    job_id = submit(daemon, harness, repo)
    daemon.dispatch("kill", {"job_id": job_id})
    seen = {}

    def maintenance(*args, **kwargs):
        seen["early kept"] = os.path.lexists(early_tree)  # given back before retention looks
        # Cut while retention runs, by a look that read the job before its cancel; the
        # sweep offered meanwhile finds retention's lock taken and touches nothing.
        seen["tree"] = cut(daemon, job_id)
        daemon._offer_unrun_sweep()
        seen["during"] = daemon._sweep_unrun_worktrees()
        seen["kept"] = os.path.isdir(seen["tree"])
        return {"pruned": [], "errors": [], "progressed": False}

    monkeypatch.setattr(daemon_module, "maintenance", maintenance)
    daemon._retention()
    assert (seen["early kept"], seen["during"], seen["kept"]) == (False, 0, True)
    assert not os.path.lexists(seen["tree"])              # given back once the pass ended
    assert not daemon._unrun_dirty


def test_never_while_a_detached_pass_may_be_cutting_a_tree(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "UNRUN_PASS_WAIT_S", 0.05)
    job_id = submit(daemon, harness, repo)
    daemon.dispatch("kill", {"job_id": job_id})
    tree = cut(daemon, job_id)
    with daemon._pass_locks["detached"]:
        assert daemon._sweep_unrun_worktrees() == 0
        assert os.path.isdir(tree) and daemon._unrun_dirty
    assert swept(daemon) == 1
    assert not os.path.lexists(tree) and not daemon._unrun_dirty


def test_an_offer_while_a_sweep_runs_is_read_again(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    daemon, harness = state_daemon
    first, second = (submit(daemon, harness, repo, caller_session=session) for session in ("one", "two"))
    daemon.dispatch("kill", {"job_id": first})
    trees = [cut(daemon, first), cut(daemon, second)]
    original = daemon._give_back_unrun

    def give_back(job_id, context):
        result = original(job_id, context)
        if job_id == first and daemon.store.get_job(second)["state"] not in daemon_module.TERMINAL:
            daemon.dispatch("kill", {"job_id": second})   # cancelled after this run read its rows
        return result

    monkeypatch.setattr(daemon, "_give_back_unrun", give_back)
    assert swept(daemon) == 2
    assert not any(os.path.lexists(tree) for tree in trees)


def test_the_daemon_offers_a_sweep_at_start(state_daemon, monkeypatch):  # noqa: F811
    daemon, _ = state_daemon
    keys = []
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kwargs: keys.append(key))
    monkeypatch.setattr(daemon.timers, "start", lambda: None)
    daemon._recover_then_start_timers()
    assert "unrun-worktrees" in keys and daemon._unrun_dirty


def test_the_offer_runs_on_a_worker(state_daemon, repo):  # noqa: F811
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    daemon.dispatch("kill", {"job_id": job_id})
    deadline = time.monotonic() + 30
    while os.path.lexists(tree) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.lexists(tree)


# --- the rule, over every combination of what keeps a tree -------------------------------------

FIELDS = st.fixed_dictionaries({
    "state": st.sampled_from(["queued", "waiting", "running", "succeeded", "failed", "cancelled", "lost"]),
    "kind": st.sampled_from(["dispatch", "turn"]),
    "sandbox": st.sampled_from(["workspace-write", "read-only"]),
    "in_place": st.booleans(),
    "recorded": st.booleans(),           # jobs.worktree names the tree
    "push": st.booleans(),
    "attempt": st.booleans(),
    "hold": st.sampled_from([None, "disk", "unprepared", "fleet-full", "machine-busy"]),
    "cancel_requested": st.booleans(),
    "lease": st.sampled_from([None, "retire", "own"]),   # retention's fence on the job, or a lease it holds
    "retention_dir": st.booleans(),
    "guest": st.booleans(),              # another live job's workdir is inside the tree
    "link": st.booleans(),               # the allocated path is a symlink
    "locked": st.booleans(),             # `git worktree lock` on the tree's registration
})


#: The detached pass's hold for each `hold` field: `unprepared` is the one no lane takes now.
HOLDS = {"disk": {"reason": "disk"}, "fleet-full": {"reason": "fleet-full"}, "machine-busy": {"reason": "machine-busy"},
         "unprepared": {"reason": "closed:account", "refused_now": ["closed:account:2099-01-01T00:00:00Z"],
                        "unprepared": True}}


def expected(f):
    """Whether the tree goes back, and the `why` its event names."""
    owned = (f["kind"] != "turn" and f["sandbox"] == "workspace-write" and not f["in_place"]
             and not f["recorded"] and not f["push"] and not f["attempt"] and not f["link"])
    terminal = f["state"] in ("succeeded", "failed", "cancelled", "lost")
    held = (f["state"] in ("queued", "waiting") and not f["cancel_requested"] and f["hold"] in ("disk", "unprepared"))
    if owned and (terminal or held) and not f["lease"] and not f["retention_dir"] and not f["guest"] and not f["locked"]:
        return "ended unrun" if terminal else "disk hold" if f["hold"] == "disk" else "no lane"
    return None


def check_rule(daemon, harness, f, tmp_path_factory):
    """A job and its tree shaped as `f` says: `_give_back_unrun` gives the tree back exactly
    when `expected(f)` says, removes nothing else, and records one event naming why."""
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    root = daemon.root / "worktrees"
    root.mkdir(exist_ok=True)
    tree = root / job_id
    outside = tmp_path_factory.mktemp("outside")
    (outside / "keep.txt").write_text("not the daemon's\n")
    if f["link"]:
        tree.symlink_to(outside, target_is_directory=True)
    else:
        tree.mkdir()
        (tree / "file.txt").write_text("cut\n")
        if f["locked"]:                         # a lock is a file beside what the gitfile names
            admin = outside / "worktrees" / job_id
            admin.mkdir(parents=True)
            (admin / "locked").write_text("kept by a person\n")
            (tree / ".git").write_text(f"gitdir: {admin}\n")
    with daemon.store.transaction("test.shape") as tx:
        tx.execute("UPDATE jobs SET state=?,kind=?,sandbox=?,in_place=?,worktree=?,push_branch=?,"
                   "cancel_requested_at=? WHERE job_id=?",
                   (f["state"], f["kind"], f["sandbox"], int(f["in_place"]), str(tree) if f["recorded"] else None,
                    "push/branch" if f["push"] else None, utcnow() if f["cancel_requested"] else None, job_id))
        if f["attempt"]:
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                       "VALUES(?,?,1,'codex-1','astra','succeeded',?)", (f"{job_id}-a1", job_id, utcnow()))
        if f["lease"]:
            tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                       (f"retire:{job_id}", f"retention:{job_id}", utcnow()) if f["lease"] == "retire"
                       else (f"test-lease:{job_id}", job_id, utcnow()))
    if f["retention_dir"]:
        (daemon.root / "retention" / job_id).mkdir(parents=True)
    if f["guest"]:
        inside = outside if f["link"] else tree
        daemon.dispatch("submit", harness.submit_args(workdir=str(inside)))
    daemon._holds_by_kind = {**daemon._holds_by_kind,
                             "detached": {job_id: dict(HOLDS[f["hold"]])} if f["hold"] else {}}
    why = expected(f)
    assert daemon._give_back_unrun(job_id) is (why is not None), f
    assert os.path.lexists(tree) is (why is None), f
    assert (outside / "keep.txt").read_text() == "not the daemon's\n", f
    assert [json.loads(row["data_json"])["why"] for row in given_back(daemon, job_id)] == ([why] if why else []), f


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(f=FIELDS)
def test_property_a_tree_is_given_back_exactly_when_nothing_keeps_it(state_daemon, f, tmp_path_factory):  # noqa: F811
    """Invariants 1, 2 and 6: only a tree no attempt used, recorded nowhere, of a job that ended
    or that the last pass held at the disk hold or `unprepared`, and nothing another job,
    retention, a lease or a link has a claim on; nothing outside `worktrees/<job id>` is
    removed; its one event names why."""
    check_rule(*state_daemon, f, tmp_path_factory)


#: Each shape that gives its tree back, by the `why` its event names.
GIVEN = {"ended unrun": {"state": "cancelled"}, "disk hold": {"state": "queued", "hold": "disk"},
         "no lane": {"state": "waiting", "hold": "unprepared"}}
BARE = {"state": "queued", "kind": "dispatch", "sandbox": "workspace-write", "in_place": False, "recorded": False,
        "push": False, "attempt": False, "hold": None, "cancel_requested": False, "lease": None,
        "retention_dir": False, "guest": False, "link": False, "locked": False}
#: Any one of these keeps the tree of every shape in GIVEN.
CLAIMS = [{"kind": "turn"}, {"sandbox": "read-only"}, {"in_place": True}, {"recorded": True}, {"push": True},
          {"attempt": True}, {"link": True}, {"lease": "retire"}, {"lease": "own"}, {"retention_dir": True},
          {"guest": True}, {"state": "running"}, {"locked": True}]
#: These keep the tree of a job that has not ended, and leave an ended job's to go.
LIVE_CLAIMS = [{"cancel_requested": True}, {"hold": None}, {"hold": "fleet-full"}, {"hold": "machine-busy"}]


def test_each_claim_alone_keeps_a_tree_that_would_go_back(state_daemon, tmp_path_factory):  # noqa: F811
    """Invariants 1 and 2, one claim at a time from each shape that gives its tree back: the
    property above draws a tree that a single claim keeps too seldom to catch a rule that
    forgets that claim (the `rule-*` mutants survived it)."""
    daemon, harness = state_daemon
    for why, shape in GIVEN.items():
        assert expected({**BARE, **shape}) == why
        check_rule(daemon, harness, {**BARE, **shape}, tmp_path_factory)
        for claim in CLAIMS + LIVE_CLAIMS:
            f = {**BARE, **shape, **claim}
            assert (expected(f) is None) is (claim in CLAIMS or why != "ended unrun"), f
            check_rule(daemon, harness, f, tmp_path_factory)


# --- no lane now: no tree is cut, and one cut before goes back ---------------------------------

def close_every_lane(daemon):
    from subfleet.contracts import ClockSource, Closure, ClosureReason
    from subfleet.daemon import after
    lanes = [row["lane_id"] for row in daemon.store.query("SELECT lane_id FROM lanes")]
    assert lanes
    for lane_id in lanes:                     # an operator's hold for the release: hours, not days
        daemon.store.put_closure(Closure(lane_id, "account", after(3 * 3600), ClosureReason.PROVIDER_LIMIT,
                                         ClockSource.REPORTED, "test"))
    return lanes


def reopen(daemon, job_id):
    with daemon.store.transaction("test.reopen") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE released_at IS NULL", (utcnow(),))
        tx.execute("UPDATE jobs SET next_check_at=? WHERE job_id=?", ("2000-01-01T00:00:00Z", job_id))


def test_no_tree_is_cut_for_a_fresh_job_that_no_lane_takes_now(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    daemon, harness = state_daemon
    close_every_lane(daemon)
    job_id = submit(daemon, harness, repo)
    real = daemon._workspace

    def forbidden(job):
        raise AssertionError("no tree is cut for a job no lane takes now")

    monkeypatch.setattr(daemon, "_workspace", forbidden)
    daemon._admit()
    hold = daemon._holds[job_id]
    assert hold["unprepared"] and hold["reason"] == "closed:account" and hold["refused_now"]
    assert "for_good" not in hold                       # three hours is not for good (C-11.8)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity") and job["next_check_at"]
    assert not os.path.lexists(daemon.root / "worktrees" / job_id)
    assert not daemon.store.query("SELECT 1 FROM decisions WHERE job_id=?", (job_id,))
    assert daemon.dispatch("why", {"job_id": job_id})["text"]
    daemon._admit()                                     # its clock runs: held as it was, still unprepared
    assert daemon._holds[job_id]["unprepared"]
    monkeypatch.setattr(daemon, "_workspace", real)
    measure(daemon, "codex-1")
    reopen(daemon, job_id)
    daemon._admit()
    (attempt,) = daemon.store.list_attempts(job_id)
    assert daemon.store.get_job(job_id)["worktree"] == str(daemon.root / "worktrees" / job_id)
    assert os.path.isdir(daemon.root / "worktrees" / job_id)


def test_a_tree_cut_before_every_lane_closed_goes_back_and_is_cut_again(state_daemon, repo, offers):  # noqa: F811
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    head = daemon.store.get_job(job_id)["workdir_head"]
    close_every_lane(daemon)
    offers.clear()
    daemon._admit()
    assert daemon._holds[job_id]["unprepared"] and offers == ["unrun-worktrees"]
    assert swept(daemon) == 1
    assert not os.path.lexists(tree) and job_id not in registered(repo)
    (event,) = given_back(daemon, job_id)
    assert '"why": "no lane"' in event["data_json"]
    measure(daemon, "codex-1")
    reopen(daemon, job_id)
    daemon._admit()
    assert len(daemon.store.list_attempts(job_id)) == 1
    assert daemon.store.get_job(job_id)["worktree"] == tree
    assert git(tree, "rev-parse", "HEAD").strip() == head


@pytest.mark.parametrize("recorded", [True, False])
def test_a_retry_keeps_its_tree_and_its_look_whatever_the_lanes(state_daemon, repo, offers, recorded):  # noqa: F811
    """An attempt's tree holds its work: never unprepared, never given back. Reservation records
    the tree (`recorded`); the attempt alone keeps it too."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    with daemon.store.transaction("test.attempt") as tx:
        tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,outcome_class,"
                   "reserved_at) VALUES(?,?,1,'codex-1','astra','failed','transient',?)",
                   (f"{job_id}-a1", job_id, utcnow()))
        tx.execute("UPDATE jobs SET worktree=?,state='queued' WHERE job_id=?", (tree if recorded else None, job_id))
    close_every_lane(daemon)
    daemon._admit()
    assert not (daemon._holds.get(job_id) or {}).get("unprepared")
    assert swept(daemon) == 0
    assert os.path.isdir(tree)


@pytest.mark.parametrize("shape", ["read-only", "in-place", "push"])
def test_a_job_that_cuts_no_tree_gets_its_look_whatever_the_lanes(state_daemon, repo, offers, shape):  # noqa: F811
    """The `unprepared` hold is only for a job a look would cut a tree for: any other job no lane
    takes now gets its look and its decision, as before C-6.20."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo, **({"sandbox": "read-only"} if shape == "read-only" else {}))
    if shape != "read-only":
        with daemon.store.transaction("test.shape") as tx:
            tx.execute(f"UPDATE jobs SET {'in_place=1' if shape == 'in-place' else 'push_branch=?'} WHERE job_id=?",
                       (job_id,) if shape == "in-place" else ("push/branch", job_id))
    close_every_lane(daemon)
    daemon._admit()
    hold = daemon._holds.get(job_id) or {}
    assert not hold.get("unprepared")
    if shape == "push":                     # its look starts preparing its checkout first (C-6.8)
        assert (hold.get("reason"), hold.get("preparing")) == ("workspace", True), hold
    else:
        assert daemon.store.query("SELECT 1 FROM decisions WHERE job_id=?", (job_id,)), hold


def test_a_task_hold_that_commits_while_the_look_is_under_way_takes_the_job(state_daemon, repo, offers,  # noqa: F811
                                                                           monkeypatch):
    """C-11.10 (R3-1): the `unprepared` wait reads the task's hold in its own transaction, as every
    admission write does: a hold that commits while the look is under way keeps the job."""
    from subfleet.daemon import after
    from tests.fake.test_task_hold_clocks import look_under_way
    from tests.task_hold_support import hold
    daemon, harness = state_daemon
    close_every_lane(daemon)
    job_id = submit(daemon, harness, repo, task="build", tier="standard")
    with look_under_way(daemon, monkeypatch, "_capacity_wait"):
        hold(daemon, "build", after(3600))
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "task-held"), dict(job)
    assert daemon._holds[job_id]["reason"] == "task-held"
    assert not os.path.lexists(daemon.root / "worktrees" / job_id)


def test_a_tree_that_did_not_go_records_nothing(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    """The event says a tree went back only once it is gone."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    daemon.dispatch("kill", {"job_id": job_id})
    monkeypatch.setattr(daemon, "_discard_worktree", lambda *args, **kwargs: None)
    assert swept(daemon) == 0
    assert os.path.isdir(tree) and not given_back(daemon, job_id)


def test_a_sweep_that_fails_is_asked_again(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    """A failure is logged, never raised, and leaves the sweep owed: the next one gives the tree back."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    daemon.dispatch("kill", {"job_id": job_id})

    def broken(job_id, context):
        raise RuntimeError("store read failed")

    with monkeypatch.context() as patch:
        patch.setattr(daemon, "_give_back_unrun", broken)
        assert daemon._sweep_unrun_worktrees() == 0
    assert daemon._unrun_dirty and os.path.isdir(tree)
    assert swept(daemon) == 1
    assert not os.path.lexists(tree) and not daemon._unrun_dirty


# --- C-6.9: an unprepared job holds back whom its look held back --------------------------------

@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
@pytest.mark.parametrize("closure", ["hours", "for good"])
def test_c6_9_the_younger_job_fares_as_it_did_before_c6_20(fleet, repo, sandbox, closure):  # noqa: F811
    """Differential, through the shared expectation: an older Opus job whose Claude lanes are
    closed for hours, or disabled, is looked at and decided (read-only, the path before C-6.20) or
    held `unprepared` with no look (writable). A younger job that may promote to Astra is held
    behind it in the first case and placed on codex-1 in the second (C-11.8), either way."""
    from subfleet.contracts import ClockSource, Closure, ClosureReason
    from subfleet.daemon import after
    from tests.caps import capped
    from tests.fake.test_unadmittable_pin_admission import submit as pin_submit
    service, harness = fleet
    capped(service.policy)
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    older = pin_submit(service, harness, pinned_lane=None, sandbox=sandbox, workdir=str(repo))
    for lane_id in ("claude-9", "claude-7"):
        if closure == "hours":
            service.store.put_closure(Closure(lane_id, "account", after(3 * 3600), ClosureReason.PROVIDER_LIMIT,
                                              ClockSource.REPORTED, "test"))
        else:
            service.store.update_lane(lane_id, enabled=0)
    younger = pin_submit(service, harness, pinned_lane=None, pinned_model=None, task="review", tier="standard")
    service._admit()
    hold = service._holds[older]
    assert bool(hold.get("unprepared")) is (sandbox == "workspace-write")
    assert not os.path.lexists(service.root / "worktrees" / older)
    placed = [attempt["lane_id"] for attempt in service.store.list_attempts(younger)]
    if closure == "hours":
        assert "for_good" not in hold
        assert placed == [] and service._holds[younger]["reason"] == "behind-older-job"
        assert service._holds[younger]["behind"] == older
    else:
        assert hold["for_good"] == ["disabled"]
        assert placed == ["codex-1"]
        assert "holds no other job back" in service.dispatch("why", {"job_id": older})["text"]


# --- what else keeps a tree, read for each tree --------------------------------------------------

def cancelled_tree(daemon, harness, repo, session="fake-session"):
    job_id = submit(daemon, harness, repo, caller_session=session)
    tree = cut(daemon, job_id)
    daemon.dispatch("kill", {"job_id": job_id})
    return job_id, tree


@pytest.mark.parametrize("whose", ["its own", "another job's"])
def test_incomplete_evidence_keeps_a_tree_unless_it_is_the_jobs_own(state_daemon, repo, offers,  # noqa: F811
                                                                    monkeypatch, whose):
    """As retention's `dependents`: a job whose places could not all be read may need any tree,
    except its own job's."""
    daemon, harness = state_daemon
    job_id, tree = cancelled_tree(daemon, harness, repo)
    real = dependencies.live

    def live(read, legacy_places=None):
        found = real(read, legacy_places)
        found.incomplete[job_id if whose == "its own" else "20261102-000000-other"] = [
            {"file": "/unreadable", "error": "PermissionError"}]
        return found

    monkeypatch.setattr(dependencies, "live", live)
    assert swept(daemon) == (1 if whose == "its own" else 0)
    assert os.path.lexists(tree) is (whose != "its own")


@pytest.mark.parametrize("holder", ["writer", "turn"])
def test_a_folder_a_lease_holds_in_the_tree_keeps_it(state_daemon, repo, offers, holder):  # noqa: F811
    """A detached writer's folder or retention's fence (`worktree:`), or a live turn's folder."""
    daemon, harness = state_daemon
    job_id, tree = cancelled_tree(daemon, harness, repo)
    inside = os.path.join(tree, "sub")
    key = (folders.exclusive_key(inside) if holder == "writer"
           else folders.turn_key(inside, "20261102-000000-turn", writable=False))
    with daemon.store.transaction("test.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (key, "20261102-000000-other", utcnow()))
    assert swept(daemon) == 0
    assert os.path.isdir(tree)


def test_a_job_placed_in_a_tree_between_two_give_backs_keeps_it(state_daemon, repo, offers,  # noqa: F811
                                                                monkeypatch):
    """The sweep lets go of the detached pass lock between trees, so what keeps a tree is read
    for each tree under it, never once for the sweep."""
    daemon, harness = state_daemon
    first, first_tree = cancelled_tree(daemon, harness, repo, "one")
    second, second_tree = cancelled_tree(daemon, harness, repo, "two")
    real = daemon._discard_worktree

    def discard(workdir, tree, timeout):
        real(workdir, tree, timeout)
        if tree == first_tree:              # a job submitted and placed between the two trees
            daemon.dispatch("submit", harness.submit_args(workdir=second_tree))

    monkeypatch.setattr(daemon, "_discard_worktree", discard)
    assert swept(daemon) == 1
    assert not os.path.lexists(first_tree) and os.path.isdir(second_tree)


def test_a_daemon_that_is_stopping_starts_no_give_back_and_owes_the_sweep(state_daemon, repo, offers):  # noqa: F811
    daemon, harness = state_daemon
    job_id, tree = cancelled_tree(daemon, harness, repo)
    daemon.stopping.set()
    try:
        assert daemon._sweep_unrun_worktrees() == 0
        assert os.path.isdir(tree) and daemon._unrun_dirty
    finally:
        daemon.stopping.clear()
    assert swept(daemon) == 1


def test_a_place_only_the_legacy_read_finds_keeps_the_tree(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    """`dependencies.legacy`: a job queued before submit recorded `depends` needs places only a
    filesystem read finds. The sweep reads them once a pass and passes them to each tree's check."""
    daemon, harness = state_daemon
    job_id, tree = cancelled_tree(daemon, harness, repo)
    guest = daemon.dispatch("submit", harness.submit_args())["job_id"]        # live, its workdir elsewhere
    found = dependencies.Places()
    found[guest] = [os.path.join(tree, "objects")]
    monkeypatch.setattr(dependencies, "legacy", lambda read: found)
    assert swept(daemon) == 0
    assert os.path.isdir(tree)


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_c23_16_an_unprepared_job_is_reset_demand_as_its_look_would_have_made_it(state_daemon, repo, offers,  # noqa: F811
                                                                                 sandbox):
    """Differential, through the shared expectation: a job no lane takes now is C-23.16's demand
    whether its look ran (read-only) or was skipped (`unprepared`)."""
    daemon, harness = state_daemon
    close_every_lane(daemon)
    job_id = submit(daemon, harness, repo, sandbox=sandbox)
    daemon._admit()
    assert bool(daemon._holds[job_id].get("unprepared")) is (sandbox == "workspace-write")
    assert [entry["job_id"] for entry in daemon._reset_demand()] == [job_id]


# --- a lock on a tree's registration is someone's claim ----------------------------------------

def test_a_tree_someone_locked_keeps_its_files_until_it_is_unlocked(state_daemon, repo, offers):  # noqa: F811
    """`git worktree lock` is a claim: git refuses to remove such a tree, retention defers it and
    `discard_registration` keeps its registration. Submit's reply names the tree, so a person can
    hold one before its job runs, and nothing archives what a give-back removes."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    git(repo, "worktree", "lock", "--reason", "kept by a person", tree)
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    assert swept(daemon) == 0
    assert (Path(tree) / "tracked.txt").is_file() and job_id in registered(repo)
    assert not given_back(daemon, job_id)
    git(repo, "worktree", "unlock", tree)
    assert swept(daemon) == 1
    assert not os.path.lexists(tree) and job_id not in registered(repo)


def test_a_waiting_job_whose_tree_someone_locked_runs_in_it(state_daemon, repo, offers):  # noqa: F811
    """Had the tree gone, the look that reopens could cut nothing: git refuses a missing but locked
    worktree, which is no transient error, so admission would fail the job."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    git(repo, "worktree", "lock", tree)
    close_every_lane(daemon)
    daemon._admit()
    assert daemon._holds[job_id]["unprepared"]
    assert swept(daemon) == 0
    assert runs(daemon, job_id)["worktree"] == tree


# --- a job needs its repository, not its caller's checkout --------------------------------------

@pytest.fixture
def linked(repo, tmp_path):
    """The caller's checkout: a linked worktree, as a session's isolated worktree is."""
    path = tmp_path / "session-worktree"
    git(repo, "worktree", "add", "-q", "-b", "session/work", str(path))
    return path


def end_session(repo, linked):
    """The session archives and its worktree goes."""
    git(repo, "worktree", "remove", "--force", str(linked))
    assert not linked.exists()


def forget_the_record(daemon, job_id):
    """A job submitted before submit recorded its repository."""
    path = daemon.root / "jobs" / job_id / "manifest.json"
    manifest = json.loads(path.read_text())
    assert manifest["workspace"].pop("common_dir")
    path.unlink()
    path.write_text(json.dumps(manifest))


def runs(daemon, job_id):
    """The look that follows a lane opening: it reserves an attempt in the job's tree."""
    measure(daemon, "codex-1")
    reopen(daemon, job_id)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert job["state"] != "failed" and daemon.store.list_attempts(job_id), dict(job)
    return job


def leaves(repo, linked, how):
    """What can become of the caller's checkout while its job waits."""
    if how == "goes":
        end_session(repo, linked)
    elif how == "loses its registration":
        shutil.rmtree(repo / ".git" / "worktrees" / linked.name)
        assert (linked / ".git").is_file()              # git finds no repository from here now
    elif how == "becomes another repository":
        end_session(repo, linked)
        linked.mkdir()
        git(linked, "init", "-q", "-b", "other")
        git(linked, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")


@pytest.mark.parametrize("how", ["stays", "goes", "loses its registration", "becomes another repository"])
@pytest.mark.parametrize("before", ["held from submit", "given back", "kept its tree"])
def test_a_job_runs_whatever_becomes_of_its_callers_checkout(state_daemon, repo, linked, offers,  # noqa: F811
                                                             before, how):
    """Invariant 14, every way a job can come to its first attempt by every fate of its checkout:
    it runs at the HEAD submit recorded, in a tree registered in the repository submit read. A
    session in an isolated worktree submits while every lane is held and archives: before the
    record the job failed at the look that cut its tree, unless an earlier look had cut one."""
    daemon, harness = state_daemon
    head = git(linked, "rev-parse", "HEAD")
    if before == "held from submit":
        close_every_lane(daemon)
    job_id = submit(daemon, harness, linked)
    if before != "held from submit":
        cut(daemon, job_id)
        close_every_lane(daemon)
    daemon._admit()
    assert daemon._holds[job_id]["unprepared"]
    if before == "given back":
        assert swept(daemon) == 1
    leaves(repo, linked, how)
    job = runs(daemon, job_id)
    tree = daemon.root / "worktrees" / job_id
    assert job["worktree"] == str(tree) and (tree / "tracked.txt").read_text() == "baseline\n"
    assert git(tree, "rev-parse", "HEAD") == head and not git(tree, "status", "--porcelain")
    assert job_id in registered(repo)


def test_a_give_back_after_the_checkout_went_takes_its_registration_too(state_daemon, repo, linked,  # noqa: F811
                                                                        offers):
    """The tree's gitfile names its registration: `jobs.workdir` has gone, and git run there
    answers nothing."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, linked)
    cut(daemon, job_id)
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    end_session(repo, linked)
    assert swept(daemon) == 1
    assert job_id not in registered(repo)
    (event,) = given_back(daemon, job_id)
    data = json.loads(event["data_json"])
    assert data["common_dir"] == os.path.realpath(repo / ".git") and "registration_left" not in data


def test_a_job_older_than_the_record_is_cut_from_what_its_give_back_recorded(state_daemon, repo, linked,  # noqa: F811
                                                                             offers):
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, linked)
    forget_the_record(daemon, job_id)
    cut(daemon, job_id)
    close_every_lane(daemon)
    daemon._admit()
    assert swept(daemon) == 1
    end_session(repo, linked)
    assert runs(daemon, job_id)["worktree"] == str(daemon.root / "worktrees" / job_id)


def test_a_job_with_no_record_still_needs_its_checkout(state_daemon, repo, linked, offers):  # noqa: F811
    """The limit: older than the record, and no look ever cut a tree it could read one from."""
    daemon, harness = state_daemon
    close_every_lane(daemon)
    job_id = submit(daemon, harness, linked)
    forget_the_record(daemon, job_id)
    daemon._admit()
    end_session(repo, linked)
    measure(daemon, "codex-1")
    reopen(daemon, job_id)
    daemon._admit()
    assert daemon.store.get_job(job_id)["state"] == "failed" and not daemon.store.list_attempts(job_id)


def test_a_broken_tree_is_cut_again_after_the_checkout_went(state_daemon, repo, linked, offers):  # noqa: F811
    """Its registration has to go first, from the repository: git refuses a missing but registered
    worktree."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, linked)
    tree = cut(daemon, job_id)
    (Path(tree) / ".git").unlink()
    end_session(repo, linked)
    assert runs(daemon, job_id)["worktree"] == tree
    assert (Path(tree) / "tracked.txt").is_file() and not git(tree, "status", "--porcelain")


def test_a_refused_jobs_tree_takes_its_registration_after_the_checkout_went(state_daemon, repo, linked,  # noqa: F811
                                                                            offers):
    """C-6.12's discard, which reads the same source."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, linked)
    tree = cut(daemon, job_id)
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    end_session(repo, linked)
    daemon._discard_fresh_worktree(job_id)
    assert not os.path.lexists(tree) and job_id not in registered(repo)


def test_a_job_from_a_subfolder_starts_there_after_the_checkout_goes(state_daemon, repo, linked,  # noqa: F811
                                                                     offers):
    daemon, harness = state_daemon
    (linked / "pkg").mkdir()
    (linked / "pkg" / "module.txt").write_text("in a subfolder\n")
    git(linked, "add", ".")
    git(linked, "commit", "-q", "-m", "a subfolder")
    close_every_lane(daemon)
    job_id = submit(daemon, harness, linked / "pkg")
    workspace = json.loads((daemon.root / "jobs" / job_id / "manifest.json").read_text())["workspace"]
    assert workspace["prefix"] == "pkg" and workspace["common_dir"] == os.path.realpath(repo / ".git")
    daemon._admit()
    end_session(repo, linked)
    runs(daemon, job_id)
    assert (daemon.root / "worktrees" / job_id / "pkg" / "module.txt").is_file()


def test_a_job_from_a_bare_repositorys_checkout_runs_after_the_checkout_goes(state_daemon, repo, tmp_path,  # noqa: F811
                                                                             offers):
    daemon, harness = state_daemon
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    checkout = tmp_path / "bare-checkout"
    git(bare, "worktree", "add", "-q", "-b", "session/bare", str(checkout))
    close_every_lane(daemon)
    job_id = submit(daemon, harness, checkout)
    daemon._admit()
    git(bare, "worktree", "remove", "--force", str(checkout))
    runs(daemon, job_id)
    assert job_id in registered(bare)


def test_a_tree_cut_from_the_repository_is_the_tree_the_checkout_would_cut(state_daemon, repo, linked,  # noqa: F811
                                                                           offers):
    """Differential: the two places `_tree_source` can send the cut agree on HEAD, a detached
    HEAD, what is tracked and a clean status."""
    daemon, harness = state_daemon
    (linked / "pkg").mkdir()
    (linked / "pkg" / "module.txt").write_text("tracked\n")
    (linked / "script.sh").write_text("#!/bin/sh\n")
    (linked / "script.sh").chmod(0o755)
    (linked / "a-link").symlink_to("script.sh")
    git(linked, "add", ".")
    git(linked, "commit", "-q", "-m", "a few shapes")
    first = submit(daemon, harness, linked, caller_session="first")
    second = submit(daemon, harness, linked, caller_session="second")
    from_checkout = cut(daemon, first)
    assert daemon._tree_source(daemon.store.get_job(second), 5) == str(linked)
    end_session(repo, linked)
    assert daemon._tree_source(daemon.store.get_job(second), 5) == os.path.realpath(repo / ".git")
    from_repository = cut(daemon, second)
    for args in (["rev-parse", "HEAD"], ["ls-files", "-s"], ["status", "--porcelain"], ["symbolic-ref", "-q", "HEAD", "--short"]):
        read = [subprocess.run(["git", "-C", tree, *args], capture_output=True, text=True)
                for tree in (from_checkout, from_repository)]
        assert (read[0].returncode, read[0].stdout) == (read[1].returncode, read[1].stdout), args
    assert os.access(Path(from_repository) / "script.sh", os.X_OK)
    assert os.path.islink(Path(from_repository) / "a-link")


def test_a_sparse_callers_patterns_pass_on_while_its_checkout_lasts(state_daemon, repo, linked, offers):  # noqa: F811
    """`worktree add` copies the per-worktree settings of the checkout it runs in, so the tree comes
    from there while it lasts. Once it has gone the settings have too, and the tree is whole."""
    daemon, harness = state_daemon
    for folder in ("kept", "left"):
        (linked / folder).mkdir()
        (linked / folder / "file.txt").write_text(folder + "\n")
    git(linked, "add", ".")
    git(linked, "commit", "-q", "-m", "two folders")
    git(linked, "sparse-checkout", "set", "kept")
    assert not (linked / "left").exists()
    first = submit(daemon, harness, linked, caller_session="first")
    second = submit(daemon, harness, linked, caller_session="second")
    sparse = Path(cut(daemon, first))
    assert (sparse / "kept" / "file.txt").is_file() and not (sparse / "left").exists()
    end_session(repo, linked)
    whole = Path(cut(daemon, second))
    assert (whole / "kept" / "file.txt").is_file() and (whole / "left" / "file.txt").is_file()


@pytest.mark.parametrize("name", ["repo", "name that ends in a space ", "caf\u00e9"])
def test_the_common_directory_answers_from_any_folder_of_a_checkout(tmp_path, name):
    from subfleet.salvage import git_common_dir
    main = tmp_path / name
    (main / "sub").mkdir(parents=True)
    git(main, "init", "-q", "-b", "task/example")
    git(main, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
    other = tmp_path / "linked"
    git(main, "worktree", "add", "-q", "-b", "other", str(other))
    (other / "deep" / "er").mkdir(parents=True)
    common = os.path.realpath(main / ".git")
    for folder in (main, main / "sub", other, other / "deep" / "er", main / ".git"):
        assert git_common_dir(folder) == common, folder
    assert git_common_dir(tmp_path / "gone") is None
    shutil.rmtree(main / ".git" / "worktrees" / "linked")
    assert git_common_dir(other) is None                  # a gitfile that names nothing


# --- a removal cut short -------------------------------------------------------------------------

def test_a_removal_cut_short_leaves_no_tree_a_look_reuses(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    """`_workspace` reuses a tree on its `.git`, HEAD and (for a copy) marker, never its files, and
    C-6.20 removes the trees of jobs that run later. A daemon stopped part way through had left a
    tree with `.git` and some files gone, and the job's attempt ran in it."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    close_every_lane(daemon)
    daemon._admit()
    real = shutil.rmtree

    def stopped_part_way(path, **kwargs):
        if str(path) != tree:
            return real(path, **kwargs)
        assert not os.path.lexists(os.path.join(tree, ".git"))
        os.unlink(os.path.join(tree, "tracked.txt"))
        return None

    with monkeypatch.context() as patched:
        patched.setattr(daemon_module.shutil, "rmtree", stopped_part_way)
        assert swept(daemon) == 0
    assert os.path.isdir(tree) and not given_back(daemon, job_id)
    assert runs(daemon, job_id)["worktree"] == tree
    assert (Path(tree) / "tracked.txt").read_text() == "baseline\n" and not git(tree, "status", "--porcelain")


def test_discarding_through_a_link_takes_no_file_of_the_tree_it_names(state_daemon, repo, tmp_path):  # noqa: F811
    daemon, _ = state_daemon
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "-b", "other", str(other))
    link = tmp_path / "link"
    link.symlink_to(other, target_is_directory=True)
    daemon._discard_worktree(str(repo), str(link), 5)
    assert (other / ".git").is_file() and (other / "tracked.txt").is_file()


def test_a_registration_that_stayed_is_named(state_daemon, repo, offers, monkeypatch):  # noqa: F811
    """The event says the tree went, and says so when its registration did not."""
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    cut(daemon, job_id)
    assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    monkeypatch.setattr(daemon, "_discard_worktree", lambda repository, tree, cap: shutil.rmtree(tree))
    assert swept(daemon) == 1
    (event,) = given_back(daemon, job_id)
    assert json.loads(event["data_json"])["registration_left"] == os.path.realpath(repo / ".git" / "worktrees" / job_id)
