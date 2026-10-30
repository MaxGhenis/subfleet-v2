"""Review r1 of #276 (D-KILL4's follow-ups, with #274's D4 merged): inputs the branch's own
tests did not execute, on the simulated host (`tests/kill4_world.py`).

1. P3-1 (`Daemon._may_signal`): a census member `_signal_reasons` skips only because this
   census could not read its identity is one a verifiable read could attribute. A finished
   job whose escaped, marked runner is such a member answered `already finished` to `kill`
   ("no source attributes it", while the marker source does) and no protocol ran; the rule
   before P3-1 ran one, whose own census read the runner and TERMed it (P2-1). The fix
   counts a marked or parent-chain member, or a group member under a verified leader, whose
   identity this census could not read; a leaderless group's member still never counts.
   The rule's terms, responsibility members and never-signalled members included, are
   pinned on constructed censuses: a mutant that dropped either passed every test of the
   branch.
2. Check 5 at the abandon's last read inside `_kill_attempt`: past the bound, a guardian
   whose `start.json` lands after the abandon is written is recorded once, and contained as
   launched (its group TERMed first). The branch's tests call `_abandon_start` directly, so
   a `_kill_attempt` that inlined the handshake recorded nothing, and no test saw it.
3. P3-3 for D4: #274's extension property (`test_quar_start1_census.py`) left a ±0.05 s band
   at the bound unasserted on a real clock, so a `>` for the `>=` passed it. On a fake clock
   with examples at the bound, both of round 3's P3-3 mutants fail it.
"""

from __future__ import annotations

import os

import pytest

from subfleet import procs, protocol
from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import ATTEMPT, JOB, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_fix2b import MAX_S, START, TERM, publish
from tests.unit.test_kill4_p3_mutations import mutate as p3_mutate
from tests.unit.test_kill4_review_r3 import due, running_guardian
from tests.unit.test_quar_start1_census import (
    test_extension_is_granted_exactly_while_every_condition_holds_whatever_the_census_could_not_read
    as d4_extension_property,
)
from tests.unit.test_kill4_terminal_containment import world  # noqa: F401
from tests.unit.test_quar_start1_landed import (D4_SHAPES, LAUNCH, STRANGER, clock, d4_shape,  # noqa: F401
                                                extended, extended_by_d4, landed, launched, record)
from tests.unit.test_quar_start1_landed import (
    test_a_start_d4_extended_found_by_the_abandons_last_read_is_recorded_once as d4_last_read_once,
    test_a_start_that_lands_after_an_extension_is_recorded_once_with_its_seconds as landed_once,
)

RUNNER = 4500
MUTANTS = {
    # P3-1's fallback without responsibility members.
    "p3-1-responsibility-dropped": (Daemon, "_may_signal", [
        ("(census.descendant_pids | census.responsible_pids)", "(census.descendant_pids)")]),
    # P3-1's fallback counting never-signalled members.
    "p3-1-never-ignored": (Daemon, "_may_signal", [(" - census.held_pids - never - tainted)",
                                                    " - census.held_pids - tainted)")]),
    # P2-1's fix without parent-chain members.
    "p2-1-chain-dropped": (Daemon, "_may_signal", [
        ("(census.marker_pids | census.chain_pids", "(census.marker_pids")]),
    # P2-1's fix counting the group's members without the leader check.
    "p2-1-group-unchecked": (Daemon, "_may_signal", [
        ("(census.group_pids if census.leader_verified else frozenset())", "census.group_pids")]),
    # Check 5: `_kill_attempt`'s handshake inlined, so its last read records no landing.
    "kill-attempt-handshake-unrecorded": (Daemon, "_kill_attempt", [
        ("unlaunched = self._abandon_start(a, unlaunched_detail())",
         'unlaunched = not self._read_json(self._abandon(a, unlaunched_detail()) / "start.json")')]),
    # Check 5: `_start_landed` without its once-only check (review r1's unrun mutant).
    "start-landed-not-once": (Daemon, "_start_landed", [
        ("kind='attempt.start_landed'\"", "kind='attempt.start_landed' AND 0\"")]),
}


def mutate(monkeypatch, mutant: str) -> None:
    owner, name, replacements = MUTANTS[mutant]
    replace_function(monkeypatch, owner, name, replacements)


# 1. P3-1: a member this census could not identify.

def unidentified_on_the_first_table(world, monkeypatch, pid: int) -> None:
    """The first process table read shows `pid` with no start (an `lstart` `ps` did not
    give), so that census cannot capture its identity (C-5.3); every later table shows it."""
    read: list[bool] = []

    def snapshot():
        table = world.snapshot()
        if not read:
            read.append(True)
            ppid, pgid, stat, _ = table.rows[pid]
            table = procs.ProcessTable({**table.rows, pid: (ppid, pgid, stat, "")}, table.boot_id)
        return table
    monkeypatch.setattr(procs, "snapshot", snapshot)


@pytest.mark.parametrize("rule", ["p3-1", "before-p3-1"])
def test_a_marked_runner_the_kills_census_could_not_identify_is_contained(daemon, world, monkeypatch, rule):
    """A finished job whose runner escaped (its own session, the attempt's markers) and
    whose kill's census read no start for it: unverifiable, the runner a marker member with
    no identity, so `_signal_reasons` skips it. A verifiable read attributes it by its
    markers in any census, so `kill` must run the protocol."""
    runner = world.add(RUNNER, 1, RUNNER, marked=True)
    daemon.store.update_attempt(ATTEMPT, state="succeeded")
    daemon.store.update_job(JOB, state="succeeded")
    daemon.term_grace_s, daemon.kill_settle_s = 2.0, 1.0
    if rule == "before-p3-1":
        replace_function(monkeypatch, Daemon, "_may_signal", [
            ("if Daemon._signal_reasons(self, a, census):",
             "if census.unverifiable or Daemon._signal_reasons(self, a, census):")])
    unidentified_on_the_first_table(world, monkeypatch, RUNNER)
    censuses: list[procs.Containment] = []
    terminal = daemon._terminal_census

    def recorded(job_id):
        found = terminal(job_id)
        censuses.append(found[1] if found else None)
        return found
    monkeypatch.setattr(daemon, "_terminal_census", recorded)
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    census = censuses[0]
    assert census is not None and census.unverifiable and RUNNER in census.marker_pids, census
    assert RUNNER not in census.identities and not Daemon._signal_reasons(daemon, attempt(daemon), census)
    assert answer["status"] == "containment requested", answer
    assert not runner.alive and runner.ended_by == TERM, world.signals


def test_confirm_dead_points_a_marked_runner_the_census_could_not_identify_to_kill(daemon, world, monkeypatch):
    """C-5.7's `--confirm-dead` on the same finished job: its census holds the marked
    runner with no identity, so the answer must say `kill` contains it, not that none of
    them may be signalled; and the `kill` that follows does."""
    runner = world.add(RUNNER, 1, RUNNER, marked=True)
    daemon.store.update_attempt(ATTEMPT, state="succeeded")
    daemon.store.update_job(JOB, state="succeeded")
    daemon.term_grace_s, daemon.kill_settle_s = 2.0, 1.0
    unidentified_on_the_first_table(world, monkeypatch, RUNNER)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
    assert answer["status"] == "not quarantined", answer
    assert f"pid {RUNNER}" in answer["detail"] and f"`subfleet kill {JOB}` contains them" in answer["detail"], answer
    assert "none of them may be signalled" not in answer["detail"], answer
    assert runner.alive and not world.signals, world.signals
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))
    assert daemon.kill(protocol.KillArgs(job_id=JOB))["status"] == "containment requested"
    assert not runner.alive and runner.ended_by == TERM, world.signals


def ident(pid: int) -> procs.ProcessIdentity:
    return procs.ProcessIdentity(pid, "boot", "Sat Sep  5 11:00:00 2026")


CENSUSES = {
    # Only the responsibility source names it, and only unverifiability keeps it from a signal.
    "responsibility-member": (procs.Containment(
        responsible_pids=frozenset({5900}), identities={5900: ident(5900)}, unverifiable=True,
        errors=("responsibility read unavailable for pid 5901",)), True),
    # The walk names it, but it is the daemon's parent, which nothing ever signals.
    "never-signalled-walk-member": (procs.Containment(
        descendant_pids=frozenset({os.getppid()}), identities={os.getppid(): ident(os.getppid())},
        unverifiable=True, errors=("marker enumeration unavailable",)), False),
    # Marked, but its identity could not be read: a verifiable read signals it.
    "unidentified-marker-member": (procs.Containment(
        marker_pids=frozenset({RUNNER}), unverifiable=True,
        errors=(f"identity inspection unavailable for pid {RUNNER}",)), True),
    # A parent-chain ancestor whose identity could not be read: attributed in any census.
    "unidentified-chain-member": (procs.Containment(
        chain_pids=frozenset({RUNNER}), chain_reasons={RUNNER: f"pid {RUNNER}: ancestor of marked pid 4501"},
        unverifiable=True, errors=(f"identity inspection unavailable for pid {RUNNER}",)), True),
    # A member of the recorded group under a verified leader, its own identity unread (the
    # term alone: a verified leader is itself a member `_signal_reasons` allows).
    "unidentified-verified-group-member": (procs.Containment(
        group_pids=frozenset({RUNNER}), leader_verified=True, unverifiable=True,
        errors=(f"identity inspection unavailable for pid {RUNNER}",)), True),
    # Only a leaderless group at the recorded pgid names it: no read attributes it, read or not.
    "unidentified-leaderless-group-member": (procs.Containment(
        group_pids=frozenset({RUNNER}), unverifiable=True,
        errors=(f"identity inspection unavailable for pid {RUNNER}",)), False),
    # Marked and unidentified, but the daemon's parent, which nothing ever signals.
    "unidentified-never-signalled-marker-member": (procs.Containment(
        marker_pids=frozenset({os.getppid()}), unverifiable=True,
        errors=(f"identity inspection unavailable for pid {os.getppid()}",)), False),
}


@pytest.mark.parametrize("shape", list(CENSUSES))
def test_an_unverifiable_census_runs_the_protocol_exactly_when_a_verifiable_read_could_attribute_a_member(
        daemon, shape):
    census, runs = CENSUSES[shape]
    assert not Daemon._signal_reasons(daemon, attempt(daemon), census)
    assert Daemon._may_signal(daemon, attempt(daemon), census) is runs


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the rule's own case must detect this mutant")
@pytest.mark.parametrize(("mutant", "shape"), [("p3-1-responsibility-dropped", "responsibility-member"),
                                               ("p3-1-never-ignored", "never-signalled-walk-member"),
                                               ("p2-1-chain-dropped", "unidentified-chain-member"),
                                               ("p2-1-group-unchecked", "unidentified-leaderless-group-member")])
def test_a_p3_1_term_mutant_fails_its_case(daemon, monkeypatch, mutant, shape):
    mutate(monkeypatch, mutant)
    test_an_unverifiable_census_runs_the_protocol_exactly_when_a_verifiable_read_could_attribute_a_member(
        daemon, shape)


# 2. Check 5 at `_kill_attempt`'s last read.

@pytest.mark.parametrize("shape", ["guardian-only", *D4_SHAPES])
def test_a_start_json_kill_attempts_last_read_finds_at_the_bound_is_recorded_once_and_contained_as_launched(
        daemon, world, clock, shape):
    """At exactly `START_GRACE_MAX_S` the extension ends and the census still holds the
    guardian (and D4's shape), so start grace goes to `_kill_attempt`, which writes the
    abandon and reads `start.json` for the last time. The guardian published it in between:
    it may have launched. The landing is recorded once, 60 s from the launch, and the group
    is TERMed first, as a launched attempt's (C-5.6)."""
    guardian = running_guardian(world)
    if shape == "guardian-only":
        launched(daemon)
        extended(daemon, clock)
    else:
        d4_shape(world, shape)
        launched(daemon)
        extended_by_d4(daemon, clock, shape)
    contain, met = daemon._contain, []

    def at_the_bound(a):
        census = contain(a)
        if not met:
            met.append(census)
            world.fail_reads.clear()                 # the protocol's censuses read the stranger whole
        return census
    daemon._contain = at_the_bound
    abandon = daemon._abandon

    def guardian_publishes(a, detail):
        adir = abandon(a, detail)
        publish(daemon, "start.json", START)         # after the abandon, before the daemon's last read
        return adir
    daemon._abandon = guardian_publishes
    daemon.term_grace_s = daemon.kill_settle_s = 0.0     # the fake clock stands still
    clock[0] = LAUNCH + MAX_S
    due(daemon)
    daemon._process_attempt(ATTEMPT)
    assert met and guardian.pid in met[0].live_pids, met
    assert landed(daemon) == [record(MAX_S)], landed(daemon)
    assert world.signals and world.signals[0] == ("group", 4242, TERM), world.signals
    assert STRANGER not in world.signalled()
    assert attempt(daemon)["outcome_detail"] != "starting-no-receipt"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the last-read landing must detect this mutant")
def test_a_kill_attempt_that_inlines_the_handshake_fails_the_last_read_landing(daemon, world, clock, monkeypatch):
    mutate(monkeypatch, "kill-attempt-handshake-unrecorded")
    test_a_start_json_kill_attempts_last_read_finds_at_the_bound_is_recorded_once_and_contained_as_launched(
        daemon, world, clock, "guardian-only")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the recorded-once landings must detect this mutant")
@pytest.mark.parametrize("read", ["after-the-census", "abandons-last-read"])
def test_a_start_landed_without_its_once_only_check_fails_the_recorded_once_landings(
        daemon, world, clock, monkeypatch, read):
    """`after-the-census`: the read after start grace's census and the next pass's first
    read both find `start.json`. `abandons-last-read`: the abandon's last read runs twice.
    Either records the landing twice without the check."""
    mutate(monkeypatch, "start-landed-not-once")
    if read == "after-the-census":
        landed_once(daemon, world, clock, "after-the-census")
    else:
        d4_last_read_once(daemon, world, clock, "stranger-unread")


# 3. P3-3 for D4's extension property (`test_quar_start1_census.py`), now on a fake clock.

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="D4's extension property must detect this mutant")
@pytest.mark.parametrize("mutant", ["p3-3-extend-at-the-bound", "p3-3-bound-from-the-deadline"])
def test_a_p3_3_mutant_fails_d4s_extension_property(mutant, daemon, monkeypatch):
    p3_mutate(monkeypatch, mutant)
    d4_extension_property(daemon)
