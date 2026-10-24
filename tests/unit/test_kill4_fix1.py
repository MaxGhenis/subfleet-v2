"""D-KILL4 fix round 1 (review r1 of #250): signal authority, the guardian, later joiners,
held parents, parent-chain members, their evidence, taint, and stopped processes.

Each test runs the daemon's census, kill protocol, finalization and kill request
unchanged on the simulated host (`tests/kill4_world.py`), as the reviewer's
executed inputs did with real processes. No real process is inspected or signalled.
"""

from __future__ import annotations

import json
import signal

import pytest

from subfleet import daemon as daemon_module, procs, protocol
from tests.kill4_world import World  # noqa: F401
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, add_running, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_terminal_containment import finalizing, owned_worktree, world  # noqa: F401

TERM, KILL, CONT = int(signal.SIGTERM), int(signal.SIGKILL), int(signal.SIGCONT)


def inline(daemon, monkeypatch) -> None:
    """Run the kill request's scheduled work at once, as its worker would."""
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))


def finished(daemon, state: str = "succeeded") -> None:
    daemon.store.update_attempt(ATTEMPT, state=state)
    daemon.store.update_job(JOB, state=state)


def stranger_group(world) -> procs.ProcessIdentity:
    """Review r1's P1 input: a process called setsid (pid 4242, the attempt's recorded
    guardian and pgid, days later), forked a sleeping member and exited, leaving a
    leaderless group 4242. The attempt recorded 4242 with an older birth."""
    member = world.add(5600, 1, 4242)
    return world.identity(member.pid)


# P1: a process group is a signal source only when it is provably the attempt's.

def test_kill_on_a_finished_job_never_signals_a_strangers_leaderless_group(daemon, world, monkeypatch):
    member = stranger_group(world)
    finished(daemon)
    inline(daemon, monkeypatch)
    daemon.kill(protocol.KillArgs(job_id=JOB))
    assert member.pid not in world.signalled(), world.signals
    assert world.procs[member.pid].alive
    assert attempt(daemon)["state"] == "succeeded"


def test_confirm_dead_then_kill_never_signals_a_strangers_leaderless_group(daemon, world, monkeypatch):
    member = stranger_group(world)
    finished(daemon, "lost")
    inline(daemon, monkeypatch)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
    assert "dead" not in answer["status"]
    daemon.kill(protocol.KillArgs(job_id=JOB))
    assert member.pid not in world.signalled(), world.signals


def test_a_dead_guardians_recycled_group_is_never_signalled_and_the_attempt_is_not_contained(daemon, world):
    member = stranger_group(world)
    daemon.store.update_job(JOB, max_attempts=1)
    daemon._process_attempt(ATTEMPT)                    # running, its guardian dead
    a = attempt(daemon)
    assert member.pid not in world.signalled(), world.signals
    assert a["state"] == "quarantined", a              # held by the group, signalled by nothing
    rounds = json.loads(a["evidence_json"]).get("terminal_containment", [])
    assert all(not round_["contained"] for round_ in rounds)


def test_a_group_recorded_in_another_boot_is_no_source_at_all(daemon, world, monkeypatch):
    member = stranger_group(world)
    daemon.store.update_attempt(ATTEMPT, boot_id="another-boot")
    finished(daemon)
    inline(daemon, monkeypatch)
    census = daemon._contain(attempt(daemon))
    assert member.pid not in census.live_pids and not census.group_pids, census.to_dict()
    assert census.recycled[4242]["reason"] == "group of another boot"
    assert daemon.kill(protocol.KillArgs(job_id=JOB))["status"] == "already finished"
    assert member.pid not in world.signalled()


def test_an_unverifiable_census_never_signals_a_leaderless_group(daemon, world, monkeypatch):
    member = stranger_group(world)
    finished(daemon)
    inline(daemon, monkeypatch)

    def unlisted():
        raise procs.InspectionError("pid listing unavailable")
    monkeypatch.setattr(procs.kernel, "pids", unlisted)
    census = daemon._contain(attempt(daemon))
    assert census.unverifiable and member.pid in census.group_pids and not census.leader_verified
    assert daemon._signal_targets(attempt(daemon), census) == {}
    daemon.kill(protocol.KillArgs(job_id=JOB))
    assert member.pid not in world.signalled(), world.signals


def test_another_attempts_provider_in_a_recycled_group_is_never_signalled(daemon, world, monkeypatch):
    """B's guardian took pid 4242 after A's group was gone, and died; B's provider
    (B's markers, which are not A's) lives on in group 4242."""
    other = add_running(daemon, JOB + "-b", 4242)
    daemon.store.update_attempt(other, proc_start="Mon Oct 19 09:00:00 2026")
    provider = world.add(5601, 1, 4242)
    finished(daemon)
    inline(daemon, monkeypatch)
    daemon.kill(protocol.KillArgs(job_id=JOB))
    assert provider.pid not in world.signalled(), world.signals


def test_a_verified_leaders_group_and_an_owned_member_remain_targets(daemon, world):
    """The rule's two sources: (a) a leader this census verified; (b) an owned identity."""
    world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    member = world.add(4250, 1, 4242)
    census = daemon._contain(attempt(daemon))
    assert census.leader_verified and 4250 in daemon._signal_targets(attempt(daemon), census)
    # That census counted it (its leader verified): a recorded counted identity attributes
    # it from then on. Without that record, a leaderless group alone does not.
    assert str(4250) in json.loads(attempt(daemon)["evidence_json"])["counted_identities"]
    daemon.store.update_attempt(ATTEMPT, evidence_json="{}")
    world.procs[4242].alive = False                          # the guardian has died since
    census = daemon._contain(attempt(daemon))
    assert not census.leader_verified and 4250 in census.group_pids
    assert 4250 not in daemon._signal_targets(attempt(daemon), census)
    daemon.store.update_attempt(ATTEMPT, evidence_json=json.dumps(
        {"owned_identities": {"4250": {"pid": 4250, "boot_id": "boot", "proc_start": member.start}}}))
    assert 4250 in daemon._signal_targets(attempt(daemon), daemon._contain(attempt(daemon)))


# P2: the stop protocol never continues the guardian.

def test_the_guardian_is_never_continued_on_a_cancel(daemon, world):
    guardian = world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True,
                         stopped=True)
    provider = world.add(4243, 4242, 4242, start=STARTED, marked=True, stopped=True, term_handler=True)
    daemon.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
    daemon._process_attempt(ATTEMPT)
    assert ("pid", 4243, CONT) in world.signals                      # its provider is continued after its TERM
    assert not [d for d in world.delivered if d[0] == 4242 and d[2] == CONT], world.delivered
    assert not guardian.alive and guardian.ended_by == KILL
    assert not provider.alive


def test_start_grace_kills_a_guardian_stopped_before_its_setsid_and_never_terms_or_continues_it(daemon, world):
    """The guardian was stopped before its setsid, so it does not lead group 4242 and the
    group's KILL cannot reach it (killpg is refused). It is still unlaunched: it is
    KILLed alone, never TERMed or continued."""
    guardian = world.add(4242, 1, 4100, start=STARTED, marked=True, stopped=True)   # still in its spawner's group
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0
    daemon._process_attempt(ATTEMPT)
    assert not guardian.alive and guardian.ended_by == KILL
    assert [s for s in world.signals if s[1] == 4242] == [("pid", 4242, KILL)], world.signals
    assert attempt(daemon)["state"] == "lost"


# P3-1: the group's TERM is credited only to the census taken before it.

def test_self_stopping_children_born_after_the_group_term_each_get_a_term_before_any_cont(daemon, world):
    """Review r1's P3-1 input: a TERM-ignoring provider starts a child that stops itself
    every census; the attempt is cancelled."""
    world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    world.add(4243, 4242, 4242, start=STARTED, marked=True, ignores_term=True, spawns_stopped=True)
    daemon.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
    daemon.term_grace_s = 2.0
    daemon._process_attempt(ATTEMPT)
    group_term = world.signals.index(("group", 4242, TERM))
    joiners = {pid for pid in world.respawned if world.born_after[pid] > group_term}
    assert len(joiners) >= 5, (joiners, world.signals)
    for pid in joiners:
        born = world.procs[pid].start
        mine = [sig for p, b, sig in world.delivered if (p, b) == (pid, born)]
        assert mine and mine[0] in (TERM, KILL), (pid, mine)
        if CONT in mine:
            assert mine.index(TERM) < mine.index(CONT), (pid, mine)
            assert ("pid", pid, TERM) in world.signals[:world.signals.index(("pid", pid, CONT))]


# P3-2: held parents release, and are named.

def held_parent(daemon, world):
    daemon._salvage = lambda job, a, **kw: ([], None, {})
    parent = world.add(6100, 1, 6100, cwd="/elsewhere")                # outside any worktree
    child = world.add(6101, 6100, 6101, marked=True, ignores_term=True)  # its marked child, ignoring TERM
    return parent, child


def test_a_held_parent_with_no_marked_descendant_left_releases(daemon, world, monkeypatch):
    parent, child = held_parent(daemon, world)
    world.unsignallable.add(child.pid)
    finalizing(daemon)
    daemon._finalize(attempt(daemon))
    assert attempt(daemon)["state"] == "quarantined"
    inline(daemon, monkeypatch)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert f"pid {parent.pid}" in answer["detail"] and "held" in answer["detail"], answer   # named, never signalled
    world.procs[child.pid].alive = False                                 # the marked child exits
    for _ in range(2):
        daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
        if attempt(daemon)["state"] != "quarantined":
            break
    assert attempt(daemon)["state"] in ("lost", "interrupted"), attempt(daemon)
    assert parent.alive and parent.pid not in world.signalled()


def test_a_held_parent_stays_held_while_a_marked_child_is_seen(daemon, world, monkeypatch):
    parent, child = held_parent(daemon, world)
    world.unsignallable.add(child.pid)
    finalizing(daemon)
    daemon._finalize(attempt(daemon))
    inline(daemon, monkeypatch)
    for _ in range(3):
        daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
    a = attempt(daemon)
    assert a["state"] == "quarantined"
    assert parent.pid in json.loads(a["quarantine_reason"])["held_pids"]
    assert parent.pid not in world.signalled()


def test_confirm_dead_on_a_finished_job_names_a_held_parent(daemon, world):
    parent, child = held_parent(daemon, world)
    finished(daemon, "lost")
    answer = daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
    assert answer["status"] == "not quarantined" and f"pid {parent.pid}" in answer["detail"], answer


# P3-3: a parent-chain ancestor is a target, never a walk seed.

def test_a_chain_ancestors_unmarked_child_is_never_signalled(daemon, world, monkeypatch):
    """Review r1's P3-3 input: an operator-style process in the worktree that ignores
    TERM, with one marked child and one unmarked child in another folder."""
    tree = owned_worktree(daemon, monkeypatch, world)
    operator = world.add(6200, 1, 6200, cwd=str(tree), ignores_term=True)
    marked = world.add(6201, 6200, 6200, marked=True, cwd=str(tree))
    unmarked = world.add(6202, 6200, 6200, cwd="/elsewhere")
    finalizing(daemon)
    daemon._finalize(attempt(daemon))
    a = attempt(daemon)
    assert unmarked.pid not in world.signalled(), world.signals
    assert unmarked.alive
    assert not marked.alive and not operator.alive                  # the accepted residual (C-5.5)
    evidence = json.loads(a["evidence_json"])
    assert str(operator.pid) in evidence["chain_identities"]
    assert str(operator.pid) not in evidence.get("counted_identities", {})
    assert a["state"] == "succeeded", a


# P3-4: every added pid and its reason reach durable evidence, on every path.

@pytest.mark.parametrize("path", ["start-grace", "dead-guardian", "exit-receipt", "cancel", "wall"])
def test_every_path_records_the_chain_ancestor_and_its_reason(daemon, world, monkeypatch, path):
    tree = owned_worktree(daemon, monkeypatch, world)
    driver = world.add(5000, 1, 5000, hidden=True, marked=True, cwd=str(tree), respawns=True)
    if path in ("cancel", "wall"):
        world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True,
                  exits_childless=True)
        world.add(4243, 4242, 4242, start=STARTED, marked=True)
        daemon.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
        if path == "wall":
            daemon.store.update_attempt(ATTEMPT, killed_by="max_wall_s")
            monkeypatch.setattr(daemon, "_cancel_info", lambda job_id: {"by": "daemon", "reason": "max_wall_s"})
        daemon._process_attempt(ATTEMPT)
    elif path == "start-grace":
        daemon.store.update_attempt(ATTEMPT, state="starting")
        daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0
        daemon._process_attempt(ATTEMPT)
    elif path == "dead-guardian":
        daemon._process_attempt(ATTEMPT)
    else:
        finalizing(daemon)
        daemon._finalize(attempt(daemon))
    a = attempt(daemon)
    assert not driver.alive, (path, a["state"])
    evidence = json.loads(a["evidence_json"])
    why = evidence.get("chain_reasons", {}).get(str(driver.pid), "")
    assert why.startswith(f"pid {driver.pid}: ancestor of marked pid") and "cwd in worktree" in why, evidence
    last = evidence["terminal_containment"][-1]
    assert why in last.get("chain", []), last
    assert [s for s in last.get("signals", []) if s["pid"] == driver.pid and s["why"] == why], last
    assert "proc" not in json.dumps(evidence["chain_reasons"])     # pids and reasons, never commands


# P3-5: taint is kept by identity.

def test_taint_cannot_be_laundered_through_an_exited_ancestor(daemon, world, monkeypatch):
    """Review r1's P3-5 sequence: a finished attempt whose unrecorded child pid now
    belongs to a stranger; the stranger forks a child, then exits."""
    finished(daemon)
    inline(daemon, monkeypatch)
    stranger = world.add(4243, 1, 4243)                  # the attempt's child pid, now a stranger's
    daemon.kill(protocol.KillArgs(job_id=JOB))           # 1. unverifiable: nothing signalled
    orphan = world.add(5700, 4243, 4243)
    daemon.kill(protocol.KillArgs(job_id=JOB))           # 2. the stranger forks
    world.procs[stranger.pid].alive = False
    world.procs[orphan.pid].ppid = 1                     # 3. the stranger exits
    daemon.kill(protocol.KillArgs(job_id=JOB))
    assert not world.signals, world.signals
    assert orphan.alive
    taint = json.loads(attempt(daemon)["evidence_json"])["unverified_identities"]
    assert taint[str(orphan.pid)]["proc_start"] == orphan.start


# P3-6: a stopped process with TERM's default action ends on the TERM alone.

def test_a_stopped_member_with_the_default_term_action_ends_on_its_term(daemon, world):
    lockf = world.add(4401, 1, 4401, marked=True, hidden=True, stopped=True)        # no TERM handler
    daemon.store.update_attempt(ATTEMPT, evidence_json=json.dumps({"counted_identities": {
        "4401": {"pid": 4401, "boot_id": "boot", "proc_start": lockf.start}}}))
    finalizing(daemon)
    daemon._finalize(attempt(daemon))
    assert not lockf.alive and lockf.ended_by == TERM
    assert [d[2] for d in world.delivered if d[0] == lockf.pid][0] == TERM
    assert attempt(daemon)["state"] == "succeeded"
