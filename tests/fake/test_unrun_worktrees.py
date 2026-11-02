"""C-6.20 (with C-6.12 and C-6.17): a tree admission cut for a job no attempt used is given back.

Incident, 2026-11-02: with every Codex lane held for the Microcosm US release, 132
folders under `worktrees/` belonged to jobs that never had an attempt, 8.26 GB in
all: 117 of jobs cancelled while they waited, 15 of jobs still waiting. `_workspace`
cuts a job's tree on its first look, before routing; a job that then waits keeps it,
and one cancelled keeps it until retention retires the job, which can be days later.
Only refused jobs gave theirs back (`_discard_fresh_worktree`).

These tests run the real admission pass and real git against a fake disk.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet.daemon import utcnow
from subfleet.disk import GB
from tests.fake.test_admission_latency import measure
from tests.fake.test_state_contract import state_daemon  # noqa: F401
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
    args = harness.submit_args(sandbox="workspace-write", workdir=str(repo), **changes)
    return daemon.dispatch("submit", args)["job_id"]


def cut(daemon, job_id):
    """What a job's first admission look does before routing (`_admit_pass`)."""
    workspace, _, _, _ = daemon._workspace(daemon.store.get_job(job_id))
    assert workspace == str(daemon.root / "worktrees" / job_id) and os.path.isdir(workspace)
    return workspace


def registered(repo):
    return subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout


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
    assert daemon._sweep_unrun_worktrees() == 1
    assert not os.path.lexists(tree)
    assert job_id not in registered(repo)                 # its registration only (d635: no prune)
    (event,) = given_back(daemon, job_id)
    assert '"why": "ended unrun"' in event["data_json"]
    assert daemon._sweep_unrun_worktrees() == 0           # nothing left to give back


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
    assert daemon._sweep_unrun_worktrees() == 1
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
    assert daemon._sweep_unrun_worktrees() == 0           # an attempt's tree is never given back


def test_a_waiting_job_that_is_not_disk_held_keeps_its_tree(state_daemon, repo, offers):  # noqa: F811
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    daemon._holds_by_kind = {**daemon._holds_by_kind, "detached": {job_id: {"reason": "fleet-full"}}}
    assert daemon._sweep_unrun_worktrees() == 0
    assert os.path.isdir(tree)


def test_a_tree_another_live_job_needs_is_kept_until_that_job_ends(state_daemon, repo, offers):  # noqa: F811
    daemon, harness = state_daemon
    job_id = submit(daemon, harness, repo)
    tree = cut(daemon, job_id)
    daemon.dispatch("kill", {"job_id": job_id})
    guest = daemon.dispatch("submit", harness.submit_args(workdir=tree))["job_id"]   # read-only, in that tree
    assert daemon._sweep_unrun_worktrees() == 0
    assert os.path.isdir(tree)
    daemon.dispatch("kill", {"job_id": guest})
    assert daemon._sweep_unrun_worktrees() == 1
    assert not os.path.lexists(tree)


def test_never_beside_retention_and_retention_sweeps_after_its_pass(state_daemon, repo, offers,  # noqa: F811
                                                                     monkeypatch):
    daemon, harness = state_daemon
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    job_id = submit(daemon, harness, repo)
    daemon.dispatch("kill", {"job_id": job_id})
    seen = {}

    def maintenance(*args, **kwargs):
        # Cut while retention runs, by a look that read the job before its cancel; the
        # sweep offered meanwhile finds retention's lock taken and touches nothing.
        seen["tree"] = cut(daemon, job_id)
        daemon._offer_unrun_sweep()
        seen["during"] = daemon._sweep_unrun_worktrees()
        seen["kept"] = os.path.isdir(seen["tree"])
        return {"pruned": [], "errors": [], "progressed": False}

    monkeypatch.setattr(daemon_module, "maintenance", maintenance)
    daemon._retention()
    assert (seen["during"], seen["kept"]) == (0, True)
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
    assert daemon._sweep_unrun_worktrees() == 1
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
    assert daemon._sweep_unrun_worktrees() == 2
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
    "state": st.sampled_from(["queued", "waiting", "succeeded", "failed", "cancelled", "lost"]),
    "kind": st.sampled_from(["dispatch", "turn"]),
    "sandbox": st.sampled_from(["workspace-write", "read-only"]),
    "in_place": st.booleans(),
    "recorded": st.booleans(),           # jobs.worktree names the tree
    "push": st.booleans(),
    "attempt": st.booleans(),
    "hold": st.sampled_from([None, "disk", "fleet-full", "machine-busy"]),
    "cancel_requested": st.booleans(),
    "retire_lease": st.booleans(),
    "retention_dir": st.booleans(),
    "guest": st.booleans(),              # another live job's workdir is inside the tree
    "link": st.booleans(),               # the allocated path is a symlink
})


def expected(f):
    owned = (f["kind"] != "turn" and f["sandbox"] == "workspace-write" and not f["in_place"]
             and not f["recorded"] and not f["push"] and not f["attempt"] and not f["link"])
    terminal = f["state"] in ("succeeded", "failed", "cancelled", "lost")
    held = (f["state"] in ("queued", "waiting") and not f["cancel_requested"] and f["hold"] == "disk")
    return owned and (terminal or held) and not f["retire_lease"] and not f["retention_dir"] and not f["guest"]


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(f=FIELDS)
def test_property_a_tree_is_given_back_exactly_when_nothing_keeps_it(state_daemon, f, tmp_path_factory):  # noqa: F811
    """Invariants 1, 2 and 6: only a tree no attempt used, recorded nowhere, of a job that ended
    or that the disk hold holds, and nothing another job, retention or a link has a claim on;
    nothing outside `worktrees/<job id>` is removed."""
    daemon, harness = state_daemon
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
    with daemon.store.transaction("test.shape") as tx:
        tx.execute("UPDATE jobs SET state=?,kind=?,sandbox=?,in_place=?,worktree=?,push_branch=?,"
                   "cancel_requested_at=? WHERE job_id=?",
                   (f["state"], f["kind"], f["sandbox"], int(f["in_place"]), str(tree) if f["recorded"] else None,
                    "push/branch" if f["push"] else None, utcnow() if f["cancel_requested"] else None, job_id))
        if f["attempt"]:
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                       "VALUES(?,?,1,'codex-1','astra','succeeded',?)", (f"{job_id}-a1", job_id, utcnow()))
        if f["retire_lease"]:
            tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                       (f"retire:{job_id}", f"retention:{job_id}", utcnow()))
    if f["retention_dir"]:
        (daemon.root / "retention" / job_id).mkdir(parents=True)
    if f["guest"]:
        inside = outside if f["link"] else tree
        daemon.dispatch("submit", harness.submit_args(workdir=str(inside)))
    daemon._holds_by_kind = {**daemon._holds_by_kind,
                             "detached": {job_id: {"reason": f["hold"]}} if f["hold"] else {}}
    want = expected(f)
    assert daemon._give_back_unrun(job_id, {}) is want
    assert os.path.lexists(tree) is (not want)
    assert (outside / "keep.txt").read_text() == "not the daemon's\n"
    assert bool(given_back(daemon, job_id)) is want
