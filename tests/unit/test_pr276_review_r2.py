"""Review r2 of #276 (D-KILL4's follow-ups, fix round r1's head): inputs fix round r1's tests
did not execute, on the simulated host (`tests/kill4_world.py`, as `BootWorld` in
`test_kill4_boot_scope.py`).

1. P2-1's unread set (`Daemon._may_signal`) named three of the four sources C-5.4 lets
   attribute a member in any census: a marked member, a parent-chain member, and a member of
   the recorded group under a verified leader. The fourth is an owned identity
   (`owned_identities`, recorded while the guardian led its group), which `_signal_reasons`
   reads before taint and before the census's unverifiability. An owned member whose identity
   the census could not read reached `_may_signal` only through the walk term, and that term
   drops it when a tainted census parent taints it by its link. A finished job with such a
   member answered `kill` with `already finished` and left it running; the rule before P3-1
   ran the protocol, whose next census read the member and TERMed it as owned. Fix round r2
   counts an owned member in the unread set (its `owned_identities` record not of another
   boot than the attempt's). Its mutants: the owned term dropped fails the constructed case
   and the kill; owned records counted whatever their boot fail the other-boot case.
2. The verified-group term of the unread set: its constructed case, as fix round r1 built it,
   fails when the term is dropped; and the never-signalled marked case fails when the unread
   set keeps `_never_signalled` pids (review r2's scratch mutant, now a control).
3. `_terminal_census` returns the attempt row it read before its census (review r1's
   observation, left as it is). A member that census first records as unverified is not yet
   tainted for `_may_signal`, so that one `kill` runs a protocol that signals no member, and
   the next `kill` answers `already finished`. With the row read after the census, the first
   `kill` answers `already finished` too: the gap errs toward running only. `--confirm-dead`
   words its answer from the same row, so it says `kill` contains such a member, and the
   `kill` that follows runs no protocol.
4. Fix round r2: the unread rule's soundness, as a property over constructed unverifiable
   censuses. Whenever the verifiable read of the same members (every identity captured)
   gives `_signal_reasons` a target, `_may_signal` runs the protocol: `kill` never skips it
   only because this census could not read a member's identity. Dropping any of the chain,
   verified-group or owned terms fails it. The converse is not a property: a walk member
   with no identity counts though its read may show it tainted (its own record), which
   errs toward running only.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import procs, protocol
from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import ATTEMPT, JOB, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_boot_scope import (BOOT_A, BOOT_B, evidence, finished, inline, kept, kinds,  # noqa: F401
                                              store, world)
from tests.unit.test_pr276_review_r1 import MUTANTS as R1_MUTANTS
from tests.unit.test_pr276_review_r1 import (
    test_an_unverifiable_census_runs_the_protocol_exactly_when_a_verifiable_read_could_attribute_a_member
    as unread_case,
    unidentified_on_the_first_table,
)

TERM = int(signal.SIGTERM)
PARENT, MEMBER, PROVIDER = 5800, 5801, 4243
MUTANTS = {
    # P2-1's fix without the verified group's members.
    "p2-1-group-dropped": (Daemon, "_may_signal", [
        ("| (census.group_pids if census.leader_verified else frozenset())", "")]),
    # P2-1's fix counting never-signalled members of the unread set (review r2's scratch mutant).
    "p2-1-never-kept": (Daemon, "_may_signal", [("if unread - census.held_pids - never:",
                                                 "if unread - census.held_pids:")]),
    # Fix round r2: the unread set without owned members (review r2's P2-1 again).
    "p2-1-owned-dropped": (Daemon, "_may_signal", [(" | (census.live_pids & owned)", "")]),
    # Fix round r2: owned records counted whatever their boot.
    "p2-1-owned-boot-unchecked": (Daemon, "_may_signal", [
        ('if not procs.boot_identity.differ(value.get("boot_id"), boot)}', "if True}")]),
}


def mutate(monkeypatch, mutant: str) -> None:
    owner, name, replacements = MUTANTS[mutant]
    replace_function(monkeypatch, owner, name, replacements)


def before_p3_1(monkeypatch) -> None:
    """The rule before P3-1: every unverifiable census runs the protocol."""
    replace_function(monkeypatch, Daemon, "_may_signal", [
        ("if Daemon._signal_reasons(self, a, census):",
         "if census.unverifiable or Daemon._signal_reasons(self, a, census):")])


# 1. An owned member whose identity the census could not read, below a tainted parent.

def owned_below_a_tainted_parent(daemon, world) -> tuple:
    """The recorded group 4242 of a finished job, its guardian gone: `PARENT`, a member an
    unverifiable census first recorded (counted, and so in `unverified_identities`), and its
    child `MEMBER`, whose identity an inspection recorded as owned while the guardian led the
    group (`_record_owned`)."""
    parent = world.add(PARENT, 1, 4242)
    member = world.add(MEMBER, PARENT, 4242)
    first = kept(world.identity(PARENT))
    store(daemon, counted_identities={str(PARENT): first}, unverified_identities={str(PARENT): first},
          owned_identities={str(MEMBER): kept(world.identity(MEMBER))})
    finished(daemon)
    return parent, member


def test_an_owned_member_is_a_target_below_a_tainted_parent_in_any_census(daemon, world):
    """C-5.4 and `_signal_reasons`: an owned identity attributes its member in any census,
    whatever taints it; the walk and responsibility sources do not."""
    parent, member = owned_below_a_tainted_parent(daemon, world)
    census = daemon._contain(attempt(daemon))
    assert not census.unverifiable and {PARENT, MEMBER} <= census.descendant_pids, census.to_dict()
    assert Daemon._tainted(evidence(daemon), census) == {PARENT, MEMBER}
    assert set(daemon._signal_targets(attempt(daemon), census)) == {MEMBER}


@pytest.mark.parametrize("rule", ["head", "before-p3-1"])
def test_an_owned_member_the_kills_census_could_not_identify_is_contained(daemon, world, monkeypatch, rule):
    """The kill's first process table shows `MEMBER` with no start: unverifiable, and
    `MEMBER` a walk and group member with no identity, tainted by its parent's link. A read
    that captures its identity attributes it by `owned_identities`, so `kill` must run the
    protocol. Before fix round r2 it answered `already finished`, naming it `held, never
    signalled`."""
    parent, member = owned_below_a_tainted_parent(daemon, world)
    daemon.term_grace_s, daemon.kill_settle_s = 0.2, 0.1
    if rule == "before-p3-1":
        before_p3_1(monkeypatch)
    unidentified_on_the_first_table(world, monkeypatch, MEMBER)
    censuses: list[procs.Containment] = []
    terminal = daemon._terminal_census

    def recorded(job_id):
        found = terminal(job_id)
        censuses.append(found[1] if found else None)
        return found
    monkeypatch.setattr(daemon, "_terminal_census", recorded)
    inline(daemon, monkeypatch)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    census = censuses[0]
    assert census is not None and census.unverifiable and MEMBER in census.descendant_pids, census
    assert MEMBER not in census.identities and not Daemon._signal_reasons(daemon, attempt(daemon), census)
    assert MEMBER in Daemon._tainted(evidence(daemon), census), census.to_dict()
    assert answer["status"] == "containment requested", answer
    assert not member.alive and member.ended_by == TERM, world.tried
    assert parent.alive and not [sig for kind, pid, sig in world.tried if kind == "pid" and pid == PARENT]


@pytest.mark.parametrize(("taint", "runs"), [("below-a-tainted-parent", True), ("untainted", True),
                                             ("owned-on-another-boot", False)])
def test_an_unverifiable_census_runs_the_protocol_for_an_owned_member_it_could_not_identify(daemon, taint, runs):
    """The rule on constructed censuses: an owned group member with no identity in this
    census, its parent a member with one. `untainted`: the walk term counts it.
    `below-a-tainted-parent`: an unverifiable census first recorded the parent, so the walk
    term drops both, and only the owned record attributes the member.
    `owned-on-another-boot` (fix round r2): the same, but the owned record names another
    boot than the attempt's, so no read attributes the member (D-QUAR-REUSE1), and the
    unread set must not count it either."""
    other = taint == "owned-on-another-boot"
    if other:
        daemon.store.update_attempt(ATTEMPT, boot_id=BOOT_A)
    ident = {pid: procs.ProcessIdentity(pid, BOOT_B if other and pid == MEMBER else "boot",
                                        f"Sat Sep  5 11:00:0{pid % 10} 2026")
             for pid in (PARENT, MEMBER)}
    keys = {"owned_identities": {str(MEMBER): kept(ident[MEMBER])},
            "counted_identities": {str(PARENT): kept(ident[PARENT])}}
    if taint != "untainted":
        keys["unverified_identities"] = {str(PARENT): kept(ident[PARENT])}
    store(daemon, **keys)
    census = procs.Containment(
        group_pids=frozenset({PARENT, MEMBER}), descendant_pids=frozenset({PARENT, MEMBER}),
        identities={PARENT: ident[PARENT]}, unverifiable=True,
        errors=(f"identity inspection unavailable for pid {MEMBER}",),
        shapes={PARENT: {"ppid": 1, "pgid": 4242, "stat": "S"}, MEMBER: {"ppid": PARENT, "pgid": 4242, "stat": "S"}})
    assert not Daemon._signal_reasons(daemon, attempt(daemon), census)
    read = dataclasses.replace(census, identities=ident, unverifiable=False, errors=())
    assert set(Daemon._signal_reasons(daemon, attempt(daemon), read)) == {
        "below-a-tainted-parent": {MEMBER}, "untainted": {PARENT, MEMBER}, "owned-on-another-boot": set()}[taint]
    assert Daemon._may_signal(daemon, attempt(daemon), census) is runs


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the owned member's constructed case must detect this mutant")
@pytest.mark.parametrize(("mutant", "taint"), [("p2-1-owned-dropped", "below-a-tainted-parent"),
                                               ("p2-1-owned-boot-unchecked", "owned-on-another-boot")])
def test_an_owned_term_mutant_fails_its_constructed_case(daemon, monkeypatch, mutant, taint):
    mutate(monkeypatch, mutant)
    test_an_unverifiable_census_runs_the_protocol_for_an_owned_member_it_could_not_identify(
        daemon, taint, taint == "below-a-tainted-parent")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the owned member's kill must detect this mutant")
def test_an_unread_set_without_owned_members_fails_the_owned_members_kill(daemon, world, monkeypatch):
    mutate(monkeypatch, "p2-1-owned-dropped")
    test_an_owned_member_the_kills_census_could_not_identify_is_contained(daemon, world, monkeypatch, "head")


# 2. The verified group's term, and never-signalled members.

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the verified group's case must detect this mutant")
def test_an_unread_set_without_the_verified_group_fails_its_case(daemon, monkeypatch):
    owner, name, replacements = MUTANTS["p2-1-group-dropped"]
    replace_function(monkeypatch, owner, name, replacements)
    unread_case(daemon, "unidentified-verified-group-member")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the never-signalled marked case must detect this mutant")
def test_an_unread_set_keeping_never_signalled_members_fails_its_case(daemon, monkeypatch):
    mutate(monkeypatch, "p2-1-never-kept")
    unread_case(daemon, "unidentified-never-signalled-marker-member")


# 3. `_terminal_census`'s row from before its census.

@pytest.mark.documented_limitation
@pytest.mark.parametrize("row", ["before-the-census", "after-the-census"])
def test_a_member_the_kills_own_census_first_records_as_unverified_costs_one_protocol_at_most(
        daemon, world, monkeypatch, row):
    """A finished job's provider (the receipt's child pid) outlived its guardian in the
    leaderless group 4242, with no identity recorded. The kill's census cannot compare a
    seed it has no birth for, so it is unverifiable, and it records the provider as counted
    and unverified: tainted, a target in no census. `_may_signal` reads the row from before
    that census (`before-the-census`, head), so this `kill` runs a protocol, which signals no
    member, and the next one answers `already finished`. With the row read after the census
    (`after-the-census`) the first `kill` answers `already finished` as well."""
    provider = world.add(PROVIDER, 1, 4242)
    finished(daemon)
    daemon.term_grace_s, daemon.kill_settle_s = 0.2, 0.1
    if row == "after-the-census":
        terminal = daemon._terminal_census

        def fresh(job_id):
            found = terminal(job_id)
            return found and (dict(daemon.store.get_attempt(found[0]["attempt_id"])), found[1])
        monkeypatch.setattr(daemon, "_terminal_census", fresh)
    inline(daemon, monkeypatch)
    first = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert evidence(daemon)["unverified_identities"] == {str(PROVIDER): kept(world.identity(PROVIDER))}
    second = daemon.kill(protocol.KillArgs(job_id=JOB))
    expected = "containment requested" if row == "before-the-census" else "already finished"
    assert first["status"] == expected, first
    assert ("attempt.recontainment_started" in kinds(daemon)) is (row == "before-the-census")
    assert second["status"] == "already finished" and "held, never signalled" in second["detail"], second
    assert provider.alive and not [pid for kind, pid, _ in world.tried if kind == "pid"], world.tried


@pytest.mark.documented_limitation
def test_confirm_dead_on_that_member_points_to_a_kill_that_runs_no_protocol(daemon, world, monkeypatch):
    """The same gap in `--confirm-dead`'s wording: its census first records the provider as
    unverified, but `_may_signal` reads the row from before it, so the answer says `kill`
    contains them; the `kill` that follows reads the taint and runs no protocol."""
    provider = world.add(PROVIDER, 1, 4242)
    finished(daemon)
    inline(daemon, monkeypatch)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
    assert answer["status"] == "not quarantined" and f"`subfleet kill {JOB}` contains them" in answer["detail"], answer
    after = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert after["status"] == "already finished" and "no kill protocol ran" in after["detail"], after
    assert "attempt.recontainment_started" not in kinds(daemon)
    assert provider.alive and not world.tried, world.tried


# 4. The unread rule's soundness (fix round r2).

SOURCES = ("group", "walk", "marker", "responsibility", "chain")
POOL = (5800, 5801, 5802, 5803)
HELD = 5900
#: A record of the process at the pid now (`now`), of an earlier one there (`earlier`), or,
#: for an owned record only, of the process now but on another boot than the attempt's.
RECORDS = ("none", "now", "earlier")


def member(sources, parent=-1, read=True, owned="none", unverified="none") -> dict:
    return {"sources": frozenset(sources), "parent": parent, "read": read, "owned": owned, "unverified": unverified}


def specs():
    members = st.lists(st.fixed_dictionaries({
        "sources": st.frozensets(st.sampled_from(SOURCES), min_size=1),
        "parent": st.integers(min_value=-1, max_value=len(POOL) - 1),
        "read": st.booleans(),
        "owned": st.sampled_from(RECORDS + ("now-other-boot",)),
        "unverified": st.sampled_from(RECORDS),
    }), min_size=1, max_size=len(POOL))
    return st.fixed_dictionaries({"members": members, "leader_verified": st.booleans(), "held": st.booleans(),
                                  "never_first": st.booleans()})


def build(daemon, spec: dict) -> tuple[dict, procs.Containment, procs.Containment]:
    """The attempt (on `BOOT_A`), an unverifiable census of `spec`'s members, and the
    verifiable read of the same members, every identity captured. A member's parent is an
    earlier member, else launchd; the first member may be the daemon's parent, which
    nothing ever signals."""
    pids = [os.getppid() if index == 0 and spec["never_first"] else POOL[index]
            for index in range(len(spec["members"]))]
    now = {pid: procs.ProcessIdentity(pid, BOOT_A, f"Sat Sep  5 11:00:0{index} 2026") for index, pid in enumerate(pids)}
    record = {"now": lambda pid: kept(now[pid]),
              "earlier": lambda pid: kept(dataclasses.replace(now[pid], proc_start="Fri Sep  4 08:00:00 2026")),
              "now-other-boot": lambda pid: kept(dataclasses.replace(now[pid], boot_id=BOOT_B))}
    shapes, sources = {}, {source: set() for source in SOURCES}
    owned, unverified = {}, {}
    for index, (pid, m) in enumerate(zip(pids, spec["members"])):
        parent = m["parent"] if 0 <= m["parent"] < index else -1
        shapes[pid] = {"ppid": pids[parent] if parent >= 0 else 1, "pgid": 4242, "stat": "S"}
        for source in m["sources"]:
            sources[source].add(pid)
        if m["owned"] != "none":
            owned[str(pid)] = record[m["owned"]](pid)
        if m["unverified"] != "none":
            unverified[str(pid)] = record[m["unverified"]](pid)
    a = {**attempt(daemon), "boot_id": BOOT_A,
         "evidence_json": json.dumps({"owned_identities": owned, "unverified_identities": unverified})}
    census = procs.Containment(
        group_pids=frozenset(sources["group"]), descendant_pids=frozenset(sources["walk"]),
        marker_pids=frozenset(sources["marker"]), responsible_pids=frozenset(sources["responsibility"]),
        chain_pids=frozenset(sources["chain"]), leader_verified=spec["leader_verified"],
        held_pids=frozenset({HELD} if spec["held"] else ()), shapes=shapes, unverifiable=True,
        identities={pid: now[pid] for pid, m in zip(pids, spec["members"]) if m["read"]},
        errors=("identity inspection unavailable",))
    return a, census, dataclasses.replace(census, identities=now, unverifiable=False, errors=())


#: Review r2's P2-1: an owned member with no identity, below a parent an unverifiable census
#: first recorded.
OWNED_BELOW_A_TAINTED_PARENT = {"members": [member({"group", "walk"}, unverified="now"),
                                            member({"group", "walk"}, parent=0, read=False, owned="now")],
                                "leader_verified": False, "held": False, "never_first": False}
UNREAD_CHAIN_MEMBER = {"members": [member({"chain"}, read=False)], "leader_verified": False, "held": False,
                       "never_first": False}
UNREAD_VERIFIED_GROUP_MEMBER = {"members": [member({"group"}, read=False)], "leader_verified": True,
                                "held": False, "never_first": False}


@example(spec=OWNED_BELOW_A_TAINTED_PARENT)
@example(spec=UNREAD_CHAIN_MEMBER)
@example(spec=UNREAD_VERIFIED_GROUP_MEMBER)
@settings(max_examples=300, derandomize=True, deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(spec=specs())
def test_an_unverifiable_census_any_verifiable_read_would_target_runs_the_protocol(daemon, spec):
    a, census, read = build(daemon, spec)
    targets = Daemon._signal_reasons(daemon, a, read)
    if targets:
        assert Daemon._may_signal(daemon, a, census), (sorted(targets), census.to_dict(), a["evidence_json"])


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the soundness property must detect this mutant")
@pytest.mark.parametrize("mutant", ["p2-1-chain-dropped", "p2-1-group-dropped", "p2-1-owned-dropped"])
def test_a_dropped_unread_term_fails_the_soundness_property(daemon, monkeypatch, mutant):
    owner, name, replacements = {**R1_MUTANTS, **MUTANTS}[mutant]
    replace_function(monkeypatch, owner, name, replacements)
    test_an_unverifiable_census_any_verifiable_read_would_target_runs_the_protocol(daemon)
