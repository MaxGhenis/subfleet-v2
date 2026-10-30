"""Review r3 of #276 (D-KILL4's follow-ups, fix round r2's head `0eec8c92f`): inputs fix round
r2's tests did not execute, on constructed censuses and the simulated host
(`tests/kill4_world.py`, as `BootWorld` in `test_kill4_boot_scope.py`).

1. The owned term's boot filter is `_signal_reasons`'s own, legacy `kern.boottime` seconds and a
   missing boot included. `procs.boot_identity.differ` is True only for two different boot
   UUIDs, so an owned record of seconds, or of no boot, passes both filters, and a read that
   captures the recorded identity attributes it. Fix round r2's `[owned-on-another-boot]` case
   pins the UUID half only: a filter that asked for the attempt's boot exactly (review r2's
   "of the attempt's boot") passed every kill4 and #276 unit test of the branch.
2. The owned term counts only live census members. An owned record of a process that has
   exited names no member, so case 3's world with one still answers `already finished` to
   every `kill`. An unread set that took every owned pid passed every kill4 and #276 unit test
   of the branch.
3. A stranger at an owned pid, with the current boot unreadable (case 3's
   `[unreadable-current-boot]`): the census proves the stranger is not the recorded process (its
   start differs, `recycled`), yet cannot read its identity, so the owned term counts it, and
   each `kill` runs a protocol that signals nothing, the loop P3-1 removed. Fix round r1's rule
   answered `already finished`. Fix round r3 of #276 leaves the census's proven-recycled pids
   out of the owned term, so every `kill` answers `already finished` again.
4. The unread rule's soundness on the daemon's own census (`_contain`, unchanged), not a
   constructed one: one simulated host read twice, once with some starts missing from the
   process table and once whole. Whenever the whole read gives `_signal_reasons` a target,
   `_may_signal` runs the protocol on the partial one. Random hosts rarely isolate a term:
   every recorded identity seeds the walk, so an untainted walk member decides most of them.
   Two examples put review r1's and review r2's P2-1 on the daemon's census, and dropping the
   marked or the owned term fails the property there.
5. Outside this PR, documented: release/217's parent-chain rule compares a recorded ancestor's
   or a held parent's start with the table's, and a table that shows no start drops it with no
   error. The census then verifies without it: `kill` answers `already finished` with a
   recorded ancestor alive, and `--confirm-dead` releases a quarantine its held parent still
   holds.
"""

from __future__ import annotations

import dataclasses
import json
import signal

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import procs, protocol
from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_boot_scope import (BOOT_A, BOOT_B, SECONDS, evidence, finished, inline, kept,  # noqa: F401
                                              kill_force_release_kill_kill, kinds, quarantined, store,
                                              stranger_group, world)
from tests.unit.test_kill4_terminal_containment import owned_worktree
from tests.unit.test_pr276_review_r2 import MUTANTS as R2_MUTANTS

PARENT, MEMBER, STRANGER, GONE = 5800, 5801, 5600, 5650
TERM = int(signal.SIGTERM)
EARLIER = "Fri Sep  4 08:00:00 2026"
MUTANTS = {
    # The owned filter asking for the attempt's boot exactly (review r2's wording, not C-5.4's).
    "owned-boot-exact": (Daemon, "_may_signal", [
        ('if not procs.boot_identity.differ(value.get("boot_id"), boot)}', 'if value.get("boot_id") == boot}')]),
    # The owned term taking every owned pid, live census member or not.
    "owned-not-live": (Daemon, "_may_signal", [(" | (census.live_pids & owned)", " | owned")]),
    # The unread set without marked members (fix round r1's first term).
    "marker-dropped": (Daemon, "_may_signal", [("(census.marker_pids | census.chain_pids", "(census.chain_pids")]),
    # Fix round r3 (P3-1): the owned term keeping the pids the census proved recycled.
    "owned-recycled-kept": (Daemon, "_may_signal", [(" - set(census.recycled)", "")]),
}


def mutate(monkeypatch, mutant: str) -> None:
    owner, name, replacements = MUTANTS[mutant]
    replace_function(monkeypatch, owner, name, replacements)


# 1. The owned filter, boot by boot.

ATTEMPT_BOOTS = {"uuid": BOOT_A, "legacy-seconds": "99", "none": None}
RECORD_BOOTS = {"attempts-uuid": BOOT_A, "other-uuid": BOOT_B, "legacy-seconds": SECONDS, "none": None}


@pytest.mark.parametrize("record_boot", list(RECORD_BOOTS))
@pytest.mark.parametrize("attempt_boot", list(ATTEMPT_BOOTS))
def test_the_owned_term_counts_exactly_the_records_signal_reasons_reads(daemon, attempt_boot, record_boot):
    """An owned member with no identity in this census, below a parent an unverifiable census
    first recorded (so the walk term drops both): `_may_signal` runs the protocol exactly when
    the read that captures the member's recorded identity gives `_signal_reasons` a target,
    and that is exactly when the record is not on another boot than the attempt's (C-5.4)."""
    boot, recorded = ATTEMPT_BOOTS[attempt_boot], RECORD_BOOTS[record_boot]
    a = {**attempt(daemon), "boot_id": boot}
    assert Daemon._attempt_boot(daemon, a) == boot          # no start.json to fall back on
    parent = procs.ProcessIdentity(PARENT, "boot", "Sat Sep  5 11:00:00 2026")
    member = procs.ProcessIdentity(MEMBER, recorded, "Sat Sep  5 11:00:01 2026")
    a["evidence_json"] = json.dumps({"owned_identities": {str(MEMBER): kept(member)},
                                     "counted_identities": {str(PARENT): kept(parent)},
                                     "unverified_identities": {str(PARENT): kept(parent)}})
    census = procs.Containment(
        group_pids=frozenset({PARENT, MEMBER}), descendant_pids=frozenset({PARENT, MEMBER}),
        identities={PARENT: parent}, unverifiable=True, errors=(f"identity inspection unavailable for pid {MEMBER}",),
        shapes={PARENT: {"ppid": 1, "pgid": 4242, "stat": "S"}, MEMBER: {"ppid": PARENT, "pgid": 4242, "stat": "S"}})
    read = dataclasses.replace(census, identities={PARENT: parent, MEMBER: member}, unverifiable=False, errors=())
    counted = not procs.boot_identity.differ(recorded, boot)
    assert Daemon._tainted(json.loads(a["evidence_json"]), census, boot) == {PARENT, MEMBER}
    assert not Daemon._signal_reasons(daemon, a, census)
    assert set(Daemon._signal_reasons(daemon, a, read)) == ({MEMBER} if counted else set())
    assert Daemon._may_signal(daemon, a, census) is counted


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the owned filter's boot cases must detect this mutant")
@pytest.mark.parametrize("record_boot", ["legacy-seconds", "none"])
def test_an_owned_filter_asking_for_the_attempts_boot_fails_a_legacy_or_missing_record(daemon, monkeypatch,
                                                                                        record_boot):
    mutate(monkeypatch, "owned-boot-exact")
    test_the_owned_term_counts_exactly_the_records_signal_reasons_reads(daemon, "uuid", record_boot)


# 2. An owned record of a process that has exited.

@pytest.mark.parametrize(("attempt_boot", "now"), [("99", BOOT_A), (BOOT_A, None)],
                         ids=["legacy-attempt-boot", "unreadable-current-boot"])
def test_an_owned_record_of_an_exited_process_adds_no_protocol_to_case_3(daemon, world, monkeypatch, attempt_boot,
                                                                          now):
    """Case 3's world (a stranger's leaderless group at the recorded pgid, a boot that cannot be
    compared), and an owned record of the attempt's process that has since exited: no census
    member is owned, so every `kill` of review r2's sequence still answers `already finished`."""
    member = stranger_group(world)
    gone = world.add(GONE, 1, 4242)
    store(daemon, owned_identities={str(GONE): kept(procs.ProcessIdentity(GONE, BOOT_A, gone.start))})
    gone.alive = False
    daemon.store.update_attempt(ATTEMPT, boot_id=attempt_boot)
    world.boot = now
    finished(daemon)
    census = daemon._contain(attempt(daemon))
    assert census.unverifiable and GONE not in census.live_pids and member.pid in census.group_pids, census.to_dict()
    answers, scheduled = kill_force_release_kill_kill(daemon, monkeypatch)
    assert [answer["status"] for answer in answers] == ["already finished"] * 4, answers
    assert not scheduled and not world.tried, (scheduled, world.tried)
    assert "recontainment" not in evidence(daemon) and member.alive


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the exited owned record's case must detect this mutant")
def test_an_owned_term_taking_every_owned_pid_fails_the_exited_record(daemon, world, monkeypatch):
    mutate(monkeypatch, "owned-not-live")
    test_an_owned_record_of_an_exited_process_adds_no_protocol_to_case_3(daemon, world, monkeypatch, "99", BOOT_A)


# 3. A stranger at an owned pid, the current boot unreadable.

def stranger_at_an_owned_pid(daemon, world):
    """Case 3's `[unreadable-current-boot]` world, where the stranger's member took a pid an
    inspection once recorded as owned, for the attempt's process born `EARLIER`."""
    member = stranger_group(world, STRANGER)
    store(daemon, owned_identities={str(STRANGER): kept(procs.ProcessIdentity(STRANGER, BOOT_A, EARLIER))})
    world.boot = None
    finished(daemon)
    return member


def test_the_census_proves_the_stranger_is_not_the_owned_process_but_cannot_read_it(daemon, world):
    """The census's own evidence: the stranger's start differs from the owned record's, so the
    pid is `recycled` (no read can match the record), and the unreadable boot leaves it with
    no identity. No read attributes it, as fix round r1's rule answered."""
    member = stranger_at_an_owned_pid(daemon, world)
    census = daemon._contain(attempt(daemon))
    assert census.unverifiable and STRANGER in census.group_pids and not census.leader_verified, census.to_dict()
    assert census.recycled[STRANGER]["reason"] == "different start identity", census.recycled
    assert STRANGER not in census.identities and STRANGER not in census.descendant_pids
    read = dataclasses.replace(census, identities={STRANGER: procs.ProcessIdentity(STRANGER, BOOT_A, member.start)},
                               unverifiable=False, errors=())
    assert not Daemon._signal_reasons(daemon, attempt(daemon), read)
    assert not Daemon._may_signal(daemon, attempt(daemon), census)


def test_a_stranger_at_an_owned_pid_runs_no_protocol_with_the_boot_unreadable(daemon, world, monkeypatch):
    """Every census of this world is unverifiable (the absent leader's boot cannot be read),
    and its one member is a stranger the census proved is not the owned process. Each `kill`
    must answer `already finished`, as case 3 does without the owned record."""
    member = stranger_at_an_owned_pid(daemon, world)
    answers, scheduled = kill_force_release_kill_kill(daemon, monkeypatch)
    assert [answer["status"] for answer in answers] == ["already finished"] * 4, (answers, world.tried)
    assert not scheduled and not world.tried and "attempt.recontainment_started" not in kinds(daemon)
    assert member.alive


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the stranger's cases must detect this mutant")
@pytest.mark.parametrize("case", ["census", "kills"])
def test_an_owned_term_keeping_a_proven_recycled_pid_fails_the_strangers_cases(daemon, world, monkeypatch, case):
    mutate(monkeypatch, "owned-recycled-kept")
    if case == "census":
        test_the_census_proves_the_stranger_is_not_the_owned_process_but_cannot_read_it(daemon, world)
    else:
        test_a_stranger_at_an_owned_pid_runs_no_protocol_with_the_boot_unreadable(daemon, world, monkeypatch)


# 4. The unread rule's soundness on the daemon's own census.

POOL = (5800, 5801, 5802, 5803, 5804)
RECORDS = ("none", "now", "earlier")


def worlds():
    """A finished attempt's host: its guardian 4242 alive with its recorded birth, or gone (its
    pgid then a leaderless group), and up to five other processes. Each has a parent (launchd,
    the guardian, or an earlier one), a group (the recorded one, its own, or the first
    process's), the attempt's markers or none, a cwd in the job's worktree or none, an owned
    record (of the process there now, an earlier one at that pid, or the process now on
    another boot) and a counted one (now or earlier, and maybe first recorded by an
    unverifiable census), and a start the census's process table may show or not."""
    process = st.fixed_dictionaries({
        "parent": st.integers(min_value=-2, max_value=len(POOL) - 1),
        "group": st.sampled_from(("recorded", "own", "first")),
        "marked": st.booleans(), "in_worktree": st.booleans(),
        "owned": st.sampled_from(RECORDS + ("other-boot",)), "counted": st.sampled_from(RECORDS),
        "unverified": st.booleans(), "start_unread": st.booleans()})
    return st.fixed_dictionaries({"processes": st.lists(process, min_size=1, max_size=len(POOL)),
                                  "guardian": st.sampled_from(("alive", "gone")),
                                  "guardian_start_unread": st.booleans()})


def on_the_host(daemon, world, spec, tree) -> set[int]:
    """Build `spec`'s host and the attempt's records; the pids whose start the census's table
    will not show."""
    world.procs.clear()
    world.tried.clear()
    world.signals.clear()
    world.boot = BOOT_A
    unread = set()
    if spec["guardian"] == "alive":
        world.add(4242, 1, 4242, start=STARTED)
        if spec["guardian_start_unread"]:
            unread.add(4242)
    pids = POOL[:len(spec["processes"])]
    keys: dict[str, dict] = {"owned_identities": {}, "counted_identities": {}, "unverified_identities": {}}
    for index, (pid, p) in enumerate(zip(pids, spec["processes"])):
        parent = p["parent"]
        ppid = 1 if parent == -2 else 4242 if parent == -1 else pids[parent] if parent < index else 1
        group = {"recorded": 4242, "own": pid, "first": pids[0]}[p["group"]]
        proc = world.add(pid, ppid, group, marked=p["marked"], cwd=str(tree) if p["in_worktree"] else None)
        now = procs.ProcessIdentity(pid, BOOT_A, proc.start)
        record = {"now": now, "earlier": dataclasses.replace(now, proc_start=EARLIER),
                  "other-boot": dataclasses.replace(now, boot_id=BOOT_B)}
        if p["owned"] != "none":
            keys["owned_identities"][str(pid)] = kept(record[p["owned"]])
        if p["counted"] != "none":
            keys["counted_identities"][str(pid)] = kept(record[p["counted"]])
            if p["unverified"]:
                keys["unverified_identities"][str(pid)] = kept(record[p["counted"]])
        if p["start_unread"]:
            unread.add(pid)
    daemon.store.update_attempt(ATTEMPT, state="succeeded", boot_id=BOOT_A, evidence_json=json.dumps(keys))
    return unread


def process(parent=-2, group="recorded", marked=False, in_worktree=False, owned="none", counted="none",
            unverified=False, start_unread=False) -> dict:
    return {"parent": parent, "group": group, "marked": marked, "in_worktree": in_worktree, "owned": owned,
            "counted": counted, "unverified": unverified, "start_unread": start_unread}


def host(*processes: dict, guardian: str = "gone") -> dict:
    return {"processes": list(processes), "guardian": guardian, "guardian_start_unread": False}


#: Review r2's P2-1 on the daemon's census: an owned member the table shows with no start, below
#: a member an unverifiable census first recorded, in a leaderless group at the recorded pgid.
OWNED_BELOW_A_TAINTED_PARENT = host(process(counted="now", unverified=True),
                                    process(parent=0, owned="now", start_unread=True))
#: Review r1's P2-1 on the daemon's census: an escaped marked runner (no record, its own group,
#: below launchd) the table shows with no start.
AN_ESCAPED_MARKED_RUNNER = host(process(group="own", marked=True, start_unread=True))


@example(spec=OWNED_BELOW_A_TAINTED_PARENT)
@example(spec=AN_ESCAPED_MARKED_RUNNER)
@settings(max_examples=200, derandomize=True, deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(spec=worlds())
def test_a_census_that_could_not_read_some_starts_runs_the_protocol_whenever_the_whole_read_targets(
        daemon, world, monkeypatch, spec):
    """The daemon's census (`_contain`, unchanged) of one host twice, from the same attempt
    row: once with some processes' starts missing from its process table (unverifiable, those
    members with no identity), once whole. Whenever the whole read gives `_signal_reasons` a
    target, `_may_signal` runs the protocol on the partial one: `kill` never skips it only
    because its census could not read an identity. The census, not a constructed one,
    decides every source: the walk from the attempt's records, taint by record and by parent
    link, the parent-chain rule and its held parents, the group under a verified leader or
    none, and the proven-recycled records."""
    tree = daemon.root / "worktrees" / JOB
    if not tree.exists():
        owned_worktree(daemon, monkeypatch, world)
    unread = on_the_host(daemon, world, spec, tree)
    row = attempt(daemon)
    whole = world.snapshot

    def partial():
        table = whole()
        return procs.ProcessTable({pid: (*row_[:3], "" if pid in unread else row_[3])
                                   for pid, row_ in table.rows.items()}, table.boot_id)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(procs, "snapshot", partial)
        census = daemon._contain(dict(row))
    daemon.store.update_attempt(ATTEMPT, evidence_json=row["evidence_json"])
    read = daemon._contain(dict(row))
    daemon.store.update_attempt(ATTEMPT, evidence_json=row["evidence_json"])
    targets = Daemon._signal_reasons(daemon, row, read)
    if targets:
        assert Daemon._may_signal(daemon, row, census), (sorted(targets), census.to_dict(), read.to_dict(),
                                                          row["evidence_json"])


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the property on the daemon's census must detect this mutant")
@pytest.mark.parametrize("mutant", ["p2-1-owned-dropped", "marker-dropped"])
def test_a_dropped_unread_term_fails_the_property_on_the_daemons_census(daemon, world, monkeypatch, mutant):
    owner, name, replacements = {**R2_MUTANTS, **MUTANTS}[mutant]
    replace_function(monkeypatch, owner, name, replacements)
    test_a_census_that_could_not_read_some_starts_runs_the_protocol_whenever_the_whole_read_targets(
        daemon, world, monkeypatch)


# 5. Outside this PR (release/217's `procs._parent_chain`): a recorded ancestor or held parent
#    whose start the census's process table does not show.

ANCESTOR, HELD = 5900, 5950


def no_start_for(world, monkeypatch, pid: int) -> None:
    """Every process table shows `pid` with no start (an `lstart` `ps` did not give)."""
    whole = world.snapshot

    def snapshot():
        table = whole()
        if pid not in table.rows:
            return table
        return procs.ProcessTable({**table.rows, pid: (*table.rows[pid][:3], "")}, table.boot_id)
    monkeypatch.setattr(procs, "snapshot", snapshot)


@pytest.mark.documented_limitation
@pytest.mark.parametrize("stranger", [False, True], ids=["alone", "beside-a-stranger-group"])
@pytest.mark.parametrize("start", ["shown", "missing"])
def test_a_recorded_chain_ancestor_with_no_start_in_the_table_drops_out_of_the_census(daemon, world, monkeypatch,
                                                                                      start, stranger):
    """`_parent_chain` keeps a recorded ancestor (`chain_identities`) by `ProcessTable.is_process`,
    which reads a start the table did not show as another process's, and records no error; a
    walk seed's (`is_recycled`) is unknown and an error (C-5.3). So a census whose table shows
    the ancestor with no start is verifiable without it: `kill` answers `already finished`
    (beside a stranger's leaderless group, naming only the stranger) and the ancestor runs on.
    With its start shown, `kill` TERMs it. A follow-up keeps it as a member with no identity, so
    the census is unverifiable and `_may_signal`'s chain term runs the protocol."""
    tree = owned_worktree(daemon, monkeypatch, world)
    ancestor = world.add(ANCESTOR, 1, ANCESTOR, cwd=str(tree))
    if stranger:
        stranger_group(world)
    store(daemon, chain_identities={str(ANCESTOR): kept(world.identity(ANCESTOR))},
          chain_reasons={str(ANCESTOR): f"pid {ANCESTOR}: ancestor of marked pid 5901, cwd in worktree"})
    finished(daemon)
    daemon.term_grace_s, daemon.kill_settle_s = 0.2, 0.1
    if start == "missing":
        no_start_for(world, monkeypatch, ANCESTOR)
    census = daemon._contain(attempt(daemon))
    assert (ANCESTOR in census.chain_pids) is (start == "shown") and not census.unverifiable, census.to_dict()
    inline(daemon, monkeypatch)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    if start == "shown":
        assert answer["status"] == "containment requested", answer
        assert not ancestor.alive and ancestor.ended_by == TERM, world.tried
    else:
        assert answer["status"] == "already finished" and f"pid {ANCESTOR}" not in answer.get("detail", ""), answer
        assert ancestor.alive and not world.tried, world.tried


@pytest.mark.documented_limitation
@pytest.mark.parametrize("start", ["shown", "missing"])
def test_a_held_parent_with_no_start_in_the_table_no_longer_holds_a_quarantine(daemon, world, monkeypatch, start):
    """The same comparison for a held parent (`held_marked`): a table that shows it with no start
    releases it silently, so `--confirm-dead` finds the census verified empty and releases the
    quarantine while the held parent lives. With its start shown, the quarantine stays."""
    held = world.add(HELD, 1, HELD)                          # outside the worktree: held, never signalled
    record = kept(world.identity(HELD))
    store(daemon, held_parents={str(HELD): record}, held_marked={str(HELD): record})
    quarantined(daemon, procs.Containment(held_pids=frozenset({HELD}), held_identities={HELD: world.identity(HELD)}))
    if start == "missing":
        no_start_for(world, monkeypatch, HELD)
    inline(daemon, monkeypatch)
    assert daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))["status"] == "resolution requested"
    assert attempt(daemon)["state"] == ("quarantined" if start == "shown" else "lost"), attempt(daemon)
    assert held.alive and not world.tried, world.tried
