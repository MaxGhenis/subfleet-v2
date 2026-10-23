"""D-KILL4, C-5.5, C-5.6, C-5.7, C-5.9: no attempt ends with live census pids.

The daemon's census, kill protocol, finalization and kill request run unchanged
on a simulated host (`tests/kill4_world.py`): each path that finalizes an
attempt runs the kill protocol against the full census first, finalizes as
before on verified empty, and quarantines survivors. Incidents (2026-10-20):
`hub-build-mi-household-members-std/a1` (start grace, live marker pid 26087,
nothing killed, 56 min beside its rerun), `hub-us9981-review-r3b/a1` (writers
after the exit receipt, marker pid 37526, nothing killed) and
`hub-us10299-review-shell-b/a2` (walled, marker pid 80881 born after the kill,
its parent loop 78382 never in the census).
"""

from __future__ import annotations

import dataclasses
import json
import signal
import subprocess
import time

import pytest

from subfleet import daemon as daemon_module, folders, procs, protocol
from subfleet.contracts import attempt_dir
from subfleet.daemon import Daemon
from tests.kill4_world import World
from tests.unit.test_daemon_settle import (
    ATTEMPT, JOB, STARTED, add_running, attempt, daemon, publish_receipt, with_launch,
)  # noqa: F401

TERM, KILL, CONT = int(signal.SIGTERM), int(signal.SIGKILL), int(signal.SIGCONT)


@pytest.fixture
def world(daemon, monkeypatch):
    w = World(ATTEMPT, str(daemon.root)).install(monkeypatch)
    daemon.exit_settle_s = 0
    with_launch(daemon, monkeypatch)
    return w


def counted(daemon, world, pid: int) -> None:
    """`pid` was counted by an earlier inspection (D-KILL3)."""
    daemon.store.update_attempt(ATTEMPT, evidence_json=json.dumps(
        {"counted_identities": {str(pid): {"pid": pid, "boot_id": "boot", "proc_start": world.procs[pid].start}}}))


def owned_worktree(daemon, monkeypatch, world):
    tree = daemon.root / "worktrees" / JOB
    tree.mkdir(parents=True)
    daemon.store.update_job(JOB, sandbox="workspace-write", worktree=str(tree))
    holders = lambda path: frozenset(pid for pid, proc in world.live().items()      # noqa: E731
                                     if proc.cwd and folders.canonical(proc.cwd).startswith(folders.canonical(path)))
    monkeypatch.setattr(procs, "worktree_cwd_pids", holders)
    return tree


def finalizing(daemon) -> None:
    publish_receipt(daemon, rc=0)
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0)


def kinds(daemon) -> list[str]:
    return [row["kind"] for row in daemon.store.list_events(JOB)]


# 1. Writers after the exit receipt (us9981-review-r3b).

def test_writers_left_after_the_exit_receipt_are_contained_then_the_receipt_decides(daemon, world):
    lockf = world.add(4401, 1, 4401, marked=True, hidden=True)      # a setsid'd lockf, counted earlier
    counted(daemon, world, lockf.pid)
    finalizing(daemon)
    daemon._finalize(attempt(daemon))
    a, job = attempt(daemon), daemon.store.get_job(JOB)
    assert a["state"] == "succeeded" and not a["quarantine_reason"], a
    assert job["state"] == "succeeded" and job["accepted_attempt_id"] == ATTEMPT
    assert a["killed_by"] is None                      # the provider ended before any signal
    assert not lockf.alive and lockf.ended_by == TERM
    assert world.signalled() == {lockf.pid}
    rounds = json.loads(a["evidence_json"])["terminal_containment"]
    assert rounds[-1]["signalled"] == [lockf.pid] and rounds[-1]["contained"] is True
    assert kinds(daemon).index("attempt.containment_started") < kinds(daemon).index("attempt.containment")


def test_writers_that_survive_the_protocol_quarantine_after_it_ran(daemon, world):
    lockf = world.add(4401, 1, 4401, marked=True, hidden=True)
    counted(daemon, world, lockf.pid)
    world.unsignallable.add(lockf.pid)
    finalizing(daemon)
    daemon._finalize(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "quarantined"
    assert json.loads(a["quarantine_reason"])["reason"] == "writers remain after exit receipt"
    assert "attempt.containment_started" in kinds(daemon)
    assert json.loads(a["evidence_json"])["terminal_containment"][-1]["contained"] is False
    assert daemon.store.get_job(JOB)["state"] == "lost"


# 2. Lost at start (hub-build-mi-household-members-std): no retry while the census lives.

def test_start_grace_contains_a_stopped_guardian_before_any_retry(daemon, world, monkeypatch):
    guardian = world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, stopped=True)
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.store.update_job(JOB, max_attempts=3)
    daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0
    decided = []
    transaction = daemon.store.transaction

    def watched(kind=None, **kwargs):
        if kind in ("attempt.accepted", "attempt.no_launch", "attempt.quarantined"):
            decided.append((kind, guardian.alive))
        return transaction(kind, **kwargs) if kind is not None else transaction(**kwargs)
    monkeypatch.setattr(daemon.store, "transaction", watched)
    daemon._process_attempt(ATTEMPT)
    a, job = attempt(daemon), daemon.store.get_job(JOB)
    assert not guardian.alive and guardian.ended_by == KILL
    assert decided and all(not alive for _, alive in decided), decided
    assert a["state"] == "lost" and a["killed_by"] == "recovery", a
    assert job["state"] == "waiting"                    # a read-only loss retries (C-4.5), now contained
    assert ("group", 4242, TERM) in world.signals and ("pid", 4242, CONT) in world.signals
    assert world.signals.index(("group", 4242, TERM)) < world.signals.index(("pid", 4242, CONT))
    assert not (attempt_dir(daemon.root, JOB, 1) / "start.json").exists()


def test_start_grace_quarantines_a_guardian_it_cannot_end_and_reserves_no_retry(daemon, world):
    world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True)
    world.unsignallable.add(4242)
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.store.update_job(JOB, max_attempts=3)
    daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert a["state"] == "quarantined"
    assert json.loads(a["quarantine_reason"])["reason"] == "start grace expired without a receipt"
    assert "attempt.kill_started" in kinds(daemon)            # contained first, then quarantined
    assert json.loads(a["evidence_json"])["terminal_containment"][-1]["contained"] is False
    assert daemon.store.get_job(JOB)["state"] == "lost"
    assert len(daemon.store.query("SELECT 1 FROM attempts WHERE job_id=?", (JOB,))) == 1


# 3. A dead guardian's escaped writers.

def test_a_dead_guardians_escaped_marked_runner_is_signalled_not_left(daemon, world):
    runner = world.add(4500, 1, 4500, marked=True)                  # visible markers, own session
    daemon.store.update_job(JOB, max_attempts=1)
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert not runner.alive and runner.ended_by == TERM
    assert a["state"] == "lost" and not a["quarantine_reason"]


# 4. Walled or cancelled, with a hidden shell driver that respawns (us10299-review-shell-b).

@pytest.mark.parametrize("requester", ["operator", "max_wall_s"])
def test_a_cancel_contains_a_hidden_driver_in_the_worktree_with_its_respawning_child(
        daemon, world, monkeypatch, requester):
    tree = owned_worktree(daemon, monkeypatch, world)
    world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, exits_childless=True)
    world.add(4243, 4242, 4242, start=STARTED, marked=True)
    driver = world.add(5000, 1, 5000, marked=True, hidden=True, cwd=str(tree), respawns=True)
    daemon.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
    if requester == "max_wall_s":
        daemon.store.update_attempt(ATTEMPT, killed_by="max_wall_s")
        monkeypatch.setattr(daemon, "_cancel_info", lambda job_id: {"by": "daemon", "reason": "max_wall_s"})
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert not driver.alive, "the respawning driver outlived the attempt"
    assert not [pid for pid, proc in world.live().items() if proc.marked], world.live()
    assert a["state"] in ("finalizing", "interrupted"), a
    chain = json.loads(a["evidence_json"])["terminal_containment"][-1]["chain"]
    assert chain[0].startswith(f"pid {driver.pid}: ancestor of marked pid") and "cwd in worktree" in chain[0]
    assert "proc" not in json.dumps(chain)                 # pids and reasons, never commands


def test_an_in_place_hidden_driver_is_held_never_signalled_and_the_attempt_quarantines(daemon, world):
    world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, exits_childless=True)
    world.add(4243, 4242, 4242, start=STARTED, marked=True)
    driver = world.add(5000, 1, 5000, marked=True, hidden=True, cwd="/elsewhere", respawns=True)
    world.respawn_every = 3                                 # some tables fall between two children
    daemon.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert driver.alive and driver.pid not in world.signalled()
    assert a["state"] == "quarantined", a                   # never interrupted while it lives
    reason = json.loads(a["quarantine_reason"])
    assert driver.pid in reason["held_pids"] and driver.pid not in reason["live_pids"]
    assert str(driver.pid) in json.loads(a["evidence_json"])["held_parents"]


# 5. The cwd rule's negative case: #240's rule still holds.

def test_an_uncounted_operator_process_in_the_worktree_is_never_signalled_and_the_verdict_stands(
        daemon, world, monkeypatch):
    tree = owned_worktree(daemon, monkeypatch, world)
    key = folders.exclusive_key(tree)
    daemon.store.acquire_lease(key, JOB)
    operator = world.add(6000, 1, 6000, cwd=str(tree))                 # an operator's shell
    lockf = world.add(4401, 1, 4401, marked=True, hidden=True)
    counted(daemon, world, lockf.pid)
    daemon._export = lambda job_id: Daemon._export_locked(daemon, job_id)
    finalizing(daemon)
    daemon._finalize(attempt(daemon))                                   # contains lockf, then waits on cwd
    assert attempt(daemon)["state"] == "finalizing"
    daemon._cwd_settle[ATTEMPT] -= daemon_module.CWD_SETTLE_S + 1
    daemon._cwd_wait_next = {}                                          # #240 round 2 paces the wait
    daemon._finalize(attempt(daemon))
    a, job = attempt(daemon), daemon.store.get_job(JOB)
    assert operator.alive and operator.pid not in world.signalled()
    assert not lockf.alive
    assert a["state"] == "succeeded" and job["rc"] == 0 and job["accepted_attempt_id"] == ATTEMPT
    assert json.loads(a["evidence_json"])["cwd_holders"] == [f"pid {operator.pid}: cwd in worktree"]
    assert daemon.store.one("SELECT 1 FROM leases WHERE lease_key=?", (key,))   # the worktree stays held


# 6. Stopped processes (2026-10-22: 13 escaped processes, all ps state T, for over a day).

def test_a_stopped_group_member_is_continued_after_its_term_and_ends_within_the_grace(daemon, world):
    world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, exits_childless=True)
    provider = world.add(4243, 4242, 4242, start=STARTED, marked=True, stopped=True)
    daemon.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
    daemon.term_grace_s = 5
    started = time.monotonic()
    daemon._process_attempt(ATTEMPT)
    assert time.monotonic() - started < 2.5, "the protocol waited out the grace for a stopped member"
    assert not provider.alive and provider.ended_by == TERM
    assert world.signals.index(("group", 4242, TERM)) < world.signals.index(("pid", 4243, CONT))
    assert attempt(daemon)["state"] in ("finalizing", "interrupted")


def test_a_stopped_escaped_member_is_continued_and_contained_and_nothing_else_is(daemon, world):
    lockf = world.add(4401, 1, 4401, marked=True, hidden=True, stopped=True)
    stranger = world.add(7000, 1, 7000, stopped=True)                   # someone else's stopped process
    counted(daemon, world, lockf.pid)
    daemon.term_grace_s = 5
    finalizing(daemon)
    started = time.monotonic()
    daemon._finalize(attempt(daemon))
    assert time.monotonic() - started < 2.5
    assert not lockf.alive and lockf.ended_by == TERM
    assert [s for s in world.signals if s[1] == lockf.pid] == [("pid", lockf.pid, TERM), ("pid", lockf.pid, CONT)]
    assert stranger.alive and stranger.stopped and stranger.pid not in world.signalled()
    assert attempt(daemon)["state"] == "succeeded"


# 7. Never signalled, whatever a census says.

def test_another_attempts_guardian_and_the_daemon_are_never_signalled(daemon, world):
    add_running(daemon, JOB + "-b", 5252)
    other = world.add(5252, 1, 5252, marked=True)            # carries this attempt's markers too
    me = world.add(procs.os.getpid(), 1, procs.os.getpid(), marked=True)
    finalizing(daemon)
    daemon._finalize(attempt(daemon))
    assert "attempt.containment_started" in kinds(daemon)     # the protocol ran over them
    assert other.pid not in world.signalled() and me.pid not in world.signalled()
    assert attempt(daemon)["state"] == "quarantined"


def test_uncertain_attribution_never_becomes_a_signal_target(daemon):
    """An unverifiable census may keep a seed it could not compare or a responsibility
    stranger (C-5.5): there only the group's, marked and parent-chain members are
    targets. What such a census first recorded stays no target later, and so does
    anything below it in a census's shapes."""
    found = {pid: procs.ProcessIdentity(pid, "boot", STARTED) for pid in (4600, 4601, 4602, 4603)}
    shapes = {4600: {"ppid": 1}, 4601: {"ppid": 1}, 4602: {"ppid": 1}, 4603: {"ppid": 4600}}
    census = procs.Containment(frozenset(), frozenset({4600, 4601, 4603}), frozenset({4601}), True, found,
                               ("recorded identity unavailable for pid 4599",), shapes,
                               responsible_pids=frozenset({4602}))
    assert set(daemon._signal_targets(attempt(daemon), census)) == {4601}
    verified = dataclasses.replace(census, unverifiable=False, errors=())
    assert set(daemon._signal_targets(attempt(daemon), verified)) == {4600, 4601, 4602, 4603}
    daemon.store.update_attempt(ATTEMPT, evidence_json=json.dumps({"unverified_identities": {
        "4600": {"pid": 4600, "boot_id": "boot", "proc_start": STARTED}}}))
    assert set(daemon._signal_targets(attempt(daemon), verified)) == {4601, 4602}
    # A recorded identity no longer vouches for a member of an unverifiable census.
    daemon.store.update_attempt(ATTEMPT, evidence_json=json.dumps({"counted_identities": {
        "4600": {"pid": 4600, "boot_id": "boot", "proc_start": STARTED}}}))
    assert set(daemon._signal_targets(attempt(daemon), census)) == {4601}


# 8. Re-containing a finished job (decision 4).

def finished(daemon, world, state="succeeded"):
    lockf = world.add(4401, 1, 4401, marked=True, hidden=True)
    counted(daemon, world, lockf.pid)
    daemon.store.update_attempt(ATTEMPT, state=state)
    daemon.store.update_job(JOB, state=state)
    return lockf


@pytest.mark.parametrize("state", ["succeeded", "lost"])
def test_kill_on_a_finished_job_recontains_its_last_attempt_and_reports(daemon, world, monkeypatch, state):
    lockf = finished(daemon, world, state)
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert answer["status"] == "containment requested" and f"pid {lockf.pid}" in answer["detail"]
    assert not lockf.alive
    a = attempt(daemon)
    assert a["state"] == state                               # a finished verdict is not reopened
    assert json.loads(a["evidence_json"])["recontainment"][-1]["contained"] is True
    again = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert again["status"] == "already finished" and "verified empty" in again["detail"]


def test_confirm_dead_on_a_finished_job_never_reports_dead_while_its_census_lives(daemon, world):
    lockf = finished(daemon, world, "lost")
    answer = daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
    assert answer["status"] == "not quarantined"
    assert f"pid {lockf.pid}" in answer["detail"] and "nothing was released" in answer["detail"]
    assert lockf.alive and not world.signals
    world.procs[lockf.pid].alive = False
    assert daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))["status"] == "already finished"


def test_a_guardian_whose_gate_closed_unopened_is_reaped_before_its_attempt_ends(monkeypatch):
    monkeypatch.setattr(daemon_module, "GATED_REAP_S", .2)
    child = subprocess.Popen(["/bin/sleep", "30"])          # stands in for a guardian that will not exit
    started = time.monotonic()
    Daemon._reap_gated(child)
    assert child.returncode is not None and time.monotonic() - started < 5
