"""Review r4 of #276 (fix round r3's head `bf465b34b`): `_may_signal`'s owned term leaves out the
census's proven-recycled pids (review r3's P3-1). Executed on the simulated host
(`tests/kill4_world.py`, as `BootWorld` in `test_kill4_boot_scope.py`) through the daemon's own
census (`_contain`, unchanged).

1. A pid enters `recycled` only when every recorded birth there provably differs from the process
   the table shows (`is_recycled`, or an absent leader's boot), or when the attempt's boot is
   provably over (`exclude`). So a pid that still holds the process its owned record names is
   never recycled: for a record of the attempt's boot, of legacy seconds, of no boot or an empty
   one; an attempt of a UUID, of seconds or of no boot; a current boot read as the UUID, as
   seconds, or not at all; a start the table shows or not; and the pid in a stranger's
   leaderless group, below the live guardian, or the guardian itself. A recycled pid's owned
   record is never what a read of the pid captures, and the census's attempt-boot filter drops
   exactly the records `_may_signal`'s drops.
2. Once the attempt's boot is over, `exclude` records every seed the table shows, an owned pid of
   any boot included, but the group, the walk and the responsibility source are then empty: only
   the marker and parent-chain terms, which count a member on their own, can name such a pid.
3. Review r3's property on the daemon's census, widened: the attempt's boot, each record's boot
   and start, what a boot read returns, and whether the partial read can read the boot at all
   vary. Whenever the whole read gives `_signal_reasons` a target, `_may_signal` runs the
   protocol on the partial one, and no pid the partial census proves recycled is attributed by
   its owned record in the whole read.
4. Outside this PR, documented: review r3's P3-2. Release/217's parent-chain rule asks
   `ProcessTable.is_process` for a recorded ancestor, which raises when the boot cannot be read;
   the rule logs the error but does not add the ancestor. Beside a stranger's leaderless group,
   `kill` then answers `already finished`, naming only the stranger, while the ancestor runs on.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import json
import signal
import textwrap

from hypothesis import HealthCheck, event, example, given, settings, strategies as st
import pytest

from subfleet import procs, protocol
from subfleet.daemon import Daemon
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_boot_scope import (BOOT_A, BOOT_B, SECONDS, finished, inline, kept, store,  # noqa: F401
                                              world)
from tests.unit.test_kill4_terminal_containment import owned_worktree

MEMBER, GUARDIAN, ANCESTOR = 5600, 4242, 5900
TERM = int(signal.SIGTERM)
EARLIER = "Fri Sep  4 08:00:00 2026"
#: `_may_signal` as the module found it, so a control's monkeypatched copy is never the source.
_MAY_SIGNAL = Daemon._may_signal
_MUTATED: dict[str, object] = {}


def mutated(name: str, replacements) -> object:
    """`Daemon._may_signal` with each replacement made, for a differential read beside the real
    one; built on first use, so a changed target fails the test that uses it, not collection."""
    if name not in _MUTATED:
        source = textwrap.dedent(inspect.getsource(_MAY_SIGNAL))
        for before, after in replacements:
            assert before in source, f"mutation target missing: {before}"
            source = source.replace(before, after)
        namespace: dict = {}
        exec(compile(source, f"<mutation:{name}>", "exec"), _MAY_SIGNAL.__globals__, namespace)
        _MUTATED[name] = namespace["_may_signal"]
    return _MUTATED[name]


def keeping_recycled(daemon, a: dict, census: procs.Containment) -> bool:
    """Fix round r2's owned term, before fix round r3: every owned pid not of another boot."""
    return mutated("owned-recycled-kept", [(" - set(census.recycled)", "")])(daemon, a, census)


def without_owned(daemon, a: dict, census: procs.Containment) -> bool:
    """The unread set with no owned term (review r2's P2-1)."""
    return mutated("p2-1-owned-dropped", [(" | (census.live_pids & owned)", "")])(daemon, a, census)


def owned_filter(evidence: dict, boot) -> dict[int, dict]:
    """The owned records `_signal_reasons` and `_may_signal` read: none of another boot."""
    return {int(pid): value for pid, value in (evidence.get("owned_identities") or {}).items()
            if not procs.boot_identity.differ(value.get("boot_id"), boot)}


def captures(record: dict, start: str) -> bool:
    """Is `record` what a read of this host captures for its pid: the live start, and the boot
    read as the UUID or, after a failed UUID read, as `kern.boottime` seconds?"""
    return record["proc_start"] == start and record["boot_id"] in (BOOT_A, SECONDS)


def no_start_for(pid: int):
    """`procs.snapshot`, but showing `pid` with no start."""
    whole = procs.snapshot
    return lambda: procs.ProcessTable({key: (*row[:3], "" if key == pid else row[3])
                                       for key, row in whole().rows.items()})


# 1. A pid holding its owned process is never recycled.

RECORD_BOOTS = {"attempts-uuid": BOOT_A, "other-uuid": BOOT_B, "legacy-seconds": SECONDS, "none": None, "empty": ""}
ATTEMPT_BOOTS = {"uuid": BOOT_A, "legacy-seconds": SECONDS, "other-seconds": "99", "none": None}
CURRENT_BOOTS = {"uuid": BOOT_A, "seconds": SECONDS, "unreadable": None}


@pytest.mark.parametrize("where", ["leaderless-group", "below-the-guardian", "the-guardian"])
def test_a_pid_holding_the_process_its_owned_record_names_is_never_recycled(daemon, world, where):
    """For each record (the process there now or an earlier one, on each boot), attempt boot,
    current boot read and table start (240 cases a test): `is_recycled` proves a pid recycled
    only when every recorded birth there differs from the table's process by start, or by a
    boot `matches` answers False for after a fresh read; a legacy or missing record boot, an
    unreadable boot or a start the table does not show is unknown, and keeps the pid out. So
    the owned term's subtraction never drops a pid a read of the host would attribute by its
    owned record, and changes `_may_signal`'s answer only at a recycled pid."""
    for record_start, record_boot, attempt_boot, now, start in itertools.product(
            ("now", "earlier"), RECORD_BOOTS, ATTEMPT_BOOTS, CURRENT_BOOTS, ("shown", "missing")):
        case = (where, record_start, record_boot, attempt_boot, now, start)
        world.procs.clear()
        if where == "leaderless-group":
            proc = world.add(MEMBER, 1, GUARDIAN)
        else:
            world.add(GUARDIAN, 1, GUARDIAN, start=STARTED)
            proc = world.procs[GUARDIAN] if where == "the-guardian" else world.add(MEMBER, GUARDIAN, GUARDIAN)
        pid = proc.pid
        record = kept(procs.ProcessIdentity(pid, RECORD_BOOTS[record_boot],
                                            proc.start if record_start == "now" else EARLIER))
        daemon.store.update_attempt(ATTEMPT, boot_id=ATTEMPT_BOOTS[attempt_boot],
                                    evidence_json=json.dumps({"owned_identities": {str(pid): record}}))
        world.boot = CURRENT_BOOTS[now]
        a = attempt(daemon)
        with pytest.MonkeyPatch.context() as patch:
            if start == "missing":
                patch.setattr(procs, "snapshot", no_start_for(pid))
            census = daemon._contain(dict(a))
        owned = owned_filter(json.loads(a["evidence_json"]), Daemon._attempt_boot(daemon, a))
        # The census's attempt-boot filter drops exactly the records `_may_signal`'s drops.
        assert (pid in census.other_boot) is (pid not in owned), (case, census.to_dict())
        if captures(record, proc.start):
            assert pid not in census.recycled, (case, census.to_dict())
        if pid in census.recycled:
            assert not captures(record, proc.start), (case, census.recycled)
        assert (Daemon._may_signal(daemon, a, census) is keeping_recycled(daemon, a, census)
                or pid in census.recycled), (case, census.to_dict())


# 2. The attempt's boot over: `exclude` recycles every seed, and only markers and the chain remain.

@pytest.mark.parametrize("marked", [False, True], ids=["unmarked", "marked"])
@pytest.mark.parametrize("start", ["shown", "missing"])
@pytest.mark.parametrize("record_boot", ["attempts-uuid", "legacy-seconds", "none", "empty"])
def test_with_the_attempts_boot_over_an_owned_pid_is_recycled_and_only_its_marker_names_it(
        daemon, world, monkeypatch, record_boot, start, marked):
    """The attempt ran on boot A and the host is on boot B. Every seed the table shows is
    recorded under `recycled` (`exclude`), the owned one included whatever its record's boot, and
    the group, walk and responsibility sources are empty. A marked process is still a member, and
    the marker term counts it, so leaving recycled pids out of the owned term changes no answer."""
    proc = world.add(MEMBER, 1, GUARDIAN, marked=marked)
    record = kept(procs.ProcessIdentity(MEMBER, RECORD_BOOTS[record_boot], proc.start))
    store(daemon, owned_identities={str(MEMBER): record})
    world.boot = BOOT_B
    if start == "missing":
        monkeypatch.setattr(procs, "snapshot", no_start_for(MEMBER))
    a = attempt(daemon)
    census = daemon._contain(dict(a))
    assert census.recycled[MEMBER]["reason"].startswith("different boot identity"), census.to_dict()
    assert not (census.group_pids | census.descendant_pids | census.responsible_pids), census.to_dict()
    assert census.live_pids == ({MEMBER} if marked else set()), census.to_dict()
    assert Daemon._may_signal(daemon, a, census) is keeping_recycled(daemon, a, census) is marked


@pytest.mark.parametrize("now", ["uuid", "unreadable"])
def test_an_absent_leader_of_another_boot_is_recycled_but_no_member(daemon, world, monkeypatch, now):
    """The third route into `recycled`: the recorded group's absent leader, whose recorded boot is
    provably not this one (`group of another boot`). With no attempt boot, the guardian's own
    birth is not recorded, so the leader's record is an owned one of another boot than the
    current one; the attempt-boot filter keeps it (no attempt UUID to differ from). The leader is
    absent from the table, so it is no member, and the subtraction changes no answer. With the
    boot unreadable the comparison is unknown, and the pgid is not recycled."""
    member = world.add(MEMBER, 1, GUARDIAN)
    store(daemon, owned_identities={str(GUARDIAN): kept(procs.ProcessIdentity(GUARDIAN, BOOT_B, STARTED)),
                                    str(MEMBER): kept(procs.ProcessIdentity(MEMBER, BOOT_A, EARLIER))})
    daemon.store.update_attempt(ATTEMPT, boot_id=None)
    world.boot = CURRENT_BOOTS[now]
    a = attempt(daemon)
    census = daemon._contain(dict(a))
    if now == "uuid":
        assert census.recycled[GUARDIAN]["reason"] == "group of another boot", census.to_dict()
        assert not census.group_pids and GUARDIAN not in census.live_pids, census.to_dict()
    else:
        assert GUARDIAN not in census.recycled and member.pid in census.group_pids, census.to_dict()
        assert census.recycled[MEMBER]["reason"] == "different start identity", census.to_dict()
    assert Daemon._may_signal(daemon, a, census) is keeping_recycled(daemon, a, census) or (
        MEMBER in census.recycled and MEMBER in census.live_pids and MEMBER not in census.identities)


# 3. Review r3's property on the daemon's census, every boot varied.

POOL = (5800, 5801, 5802, 5803, 5804)
BOOTS = {"attempts-uuid": BOOT_A, "other-uuid": BOOT_B, "legacy-seconds": SECONDS, "none": None}
#: What a boot read that works returns on the host: the UUID, or `kern.boottime` seconds after a
#: failed UUID read.
READS = {"uuid": BOOT_A, "seconds": SECONDS}


def hosts():
    """Review r3's hosts (`test_pr276_review_r3.worlds`), with the attempt's boot, each owned and
    counted record's boot and start, what a boot read returns, and whether the partial read can
    read the boot, drawn too."""
    record = st.none() | st.tuples(st.sampled_from(("now", "earlier")), st.sampled_from(tuple(BOOTS)))
    process = st.fixed_dictionaries({
        "parent": st.integers(min_value=-2, max_value=len(POOL) - 1),
        "group": st.sampled_from(("recorded", "own", "first")),
        "marked": st.booleans(), "in_worktree": st.booleans(),
        "owned": record, "counted": record, "unverified": st.booleans(), "start_unread": st.booleans()})
    return st.fixed_dictionaries({"processes": st.lists(process, min_size=1, max_size=len(POOL)),
                                  "guardian": st.sampled_from(("alive", "gone")),
                                  "guardian_start_unread": st.booleans(),
                                  "attempt_boot": st.sampled_from(tuple(ATTEMPT_BOOTS)),
                                  "boot_reads": st.sampled_from(tuple(READS)), "boot_unread": st.booleans()})


def on_the_host(daemon, world, spec, tree) -> set[int]:
    """Build `spec`'s host (on boot A) and the attempt's records; the pids whose start the
    partial read's table will not show."""
    world.procs.clear()
    world.tried.clear()
    world.signals.clear()
    unread = set()
    if spec["guardian"] == "alive":
        world.add(GUARDIAN, 1, GUARDIAN, start=STARTED)
        if spec["guardian_start_unread"]:
            unread.add(GUARDIAN)
    pids = POOL[:len(spec["processes"])]
    keys: dict[str, dict] = {"owned_identities": {}, "counted_identities": {}, "unverified_identities": {}}
    for index, (pid, p) in enumerate(zip(pids, spec["processes"])):
        parent = p["parent"]
        ppid = 1 if parent == -2 else GUARDIAN if parent == -1 else pids[parent] if parent < index else 1
        group = {"recorded": GUARDIAN, "own": pid, "first": pids[0]}[p["group"]]
        proc = world.add(pid, ppid, group, marked=p["marked"], cwd=str(tree) if p["in_worktree"] else None)

        def record(drawn, pid=pid, start=proc.start):
            began, boot = drawn
            return kept(procs.ProcessIdentity(pid, BOOTS[boot], start if began == "now" else EARLIER))
        if p["owned"] is not None:
            keys["owned_identities"][str(pid)] = record(p["owned"])
        if p["counted"] is not None:
            keys["counted_identities"][str(pid)] = record(p["counted"])
            if p["unverified"]:
                keys["unverified_identities"][str(pid)] = record(p["counted"])
        if p["start_unread"]:
            unread.add(pid)
    daemon.store.update_attempt(ATTEMPT, state="succeeded", boot_id=ATTEMPT_BOOTS[spec["attempt_boot"]],
                                evidence_json=json.dumps(keys))
    return unread


def process(parent=-2, group="recorded", marked=False, owned=None, counted=None, unverified=False,
            start_unread=False) -> dict:
    return {"parent": parent, "group": group, "marked": marked, "in_worktree": False, "owned": owned,
            "counted": counted, "unverified": unverified, "start_unread": start_unread}


def host(*processes: dict, boot_reads: str = "uuid", boot_unread: bool = True) -> dict:
    return {"processes": list(processes), "guardian": "gone", "guardian_start_unread": False,
            "attempt_boot": "uuid", "boot_reads": boot_reads, "boot_unread": boot_unread}


def read_twice(daemon, world, monkeypatch, spec):
    """The attempt row, its partial census (the starts of `unread` missing, and the boot not
    read when `boot_unread`) and its whole one."""
    tree = daemon.root / "worktrees" / JOB
    if not tree.exists():
        owned_worktree(daemon, monkeypatch, world)
    unread = on_the_host(daemon, world, spec, tree)
    row = attempt(daemon)
    whole = world.snapshot

    def partial():
        table = whole()
        return procs.ProcessTable({pid: (*row_[:3], "" if pid in unread else row_[3])
                                   for pid, row_ in table.rows.items()})
    world.boot = None if spec["boot_unread"] else READS[spec["boot_reads"]]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(procs, "snapshot", partial)
        census = daemon._contain(dict(row))
    daemon.store.update_attempt(ATTEMPT, evidence_json=row["evidence_json"])
    world.boot = READS[spec["boot_reads"]]
    read = daemon._contain(dict(row))
    daemon.store.update_attempt(ATTEMPT, evidence_json=row["evidence_json"])
    return row, census, read


#: Review r3's P3-1 on a host of this generator: a stranger in a leaderless group at the
#: recorded pgid, at a pid owned for an earlier process, the boot unreadable.
A_STRANGER_AT_AN_OWNED_PID = host(process(owned=("earlier", "attempts-uuid")))
#: Review r2's P2-1 with a legacy-seconds owned record, on a host whose boot reads as seconds: the
#: owned member's start is missing from the partial read, below a parent an unverifiable census
#: first recorded, so the walk term drops it and only the owned term runs the protocol.
OWNED_BY_SECONDS_BELOW_A_TAINTED_PARENT = host(
    process(counted=("now", "attempts-uuid"), unverified=True),
    process(parent=0, owned=("now", "legacy-seconds"), start_unread=True), boot_reads="seconds", boot_unread=False)


@example(spec=A_STRANGER_AT_AN_OWNED_PID)
@example(spec=OWNED_BY_SECONDS_BELOW_A_TAINTED_PARENT)
@settings(max_examples=300, derandomize=True, deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(spec=hosts())
def test_the_unread_rule_is_sound_on_the_daemons_census_whatever_the_boots(daemon, world, monkeypatch, spec):
    """One host read twice by the daemon's census from the same attempt row: once with some
    starts missing and maybe the boot unreadable, once whole. Whenever the whole read gives
    `_signal_reasons` a target, `_may_signal` runs the protocol on the partial one; and a pid the
    partial census proves recycled is never attributed by its owned record in the whole read."""
    row, census, read = read_twice(daemon, world, monkeypatch, spec)
    targets = Daemon._signal_reasons(daemon, row, read)
    owned = owned_filter(json.loads(row["evidence_json"]), Daemon._attempt_boot(daemon, row))
    for pid in set(census.recycled) & owned.keys() & census.live_pids:
        assert pid not in read.identities or owned[pid] != dataclasses.asdict(read.identities[pid]), (
            pid, census.to_dict(), read.to_dict())
    runs = Daemon._may_signal(daemon, row, census)
    if runs is not keeping_recycled(daemon, row, census):
        event("the recycled subtraction decides")
    if targets:
        event("the whole read targets")
        assert runs, (sorted(targets), census.to_dict(), read.to_dict(), row["evidence_json"])


def test_the_property_examples_decide_as_described(daemon, world, monkeypatch):
    """The stranger's census is one the subtraction decides, with no target in the whole read;
    the legacy-seconds owned member is a whole-read target that only the owned term keeps."""
    row, census, read = read_twice(daemon, world, monkeypatch, A_STRANGER_AT_AN_OWNED_PID)
    assert census.recycled[5800]["reason"] == "different start identity" and 5800 in census.group_pids
    assert not Daemon._signal_reasons(daemon, row, read)
    assert not Daemon._may_signal(daemon, row, census) and keeping_recycled(daemon, row, census)
    row, census, read = read_twice(daemon, world, monkeypatch, OWNED_BY_SECONDS_BELOW_A_TAINTED_PARENT)
    assert 5801 not in census.recycled and 5801 not in census.identities, census.to_dict()
    assert set(Daemon._signal_reasons(daemon, row, read)) == {5801}, read.to_dict()
    assert Daemon._tainted(json.loads(row["evidence_json"]), census, BOOT_A) == {5800, 5801}
    assert Daemon._may_signal(daemon, row, census) and not without_owned(daemon, row, census)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the property's examples must detect this mutant")
@pytest.mark.parametrize("mutant", ["owned-recycled-kept", "p2-1-owned-dropped"])
def test_an_owned_term_mutant_fails_the_property_examples(daemon, world, monkeypatch, mutant):
    replacements = {"owned-recycled-kept": [(" - set(census.recycled)", "")],
                    "p2-1-owned-dropped": [(" | (census.live_pids & owned)", "")]}[mutant]
    monkeypatch.setattr(Daemon, "_may_signal", mutated(mutant, replacements))
    test_the_property_examples_decide_as_described(daemon, world, monkeypatch)


# 4. Outside this PR (release/217's `procs._parent_chain`): a recorded ancestor when the boot
#    cannot be read (review r3's P3-2).

@pytest.mark.documented_limitation
@pytest.mark.parametrize("boot", ["readable", "unreadable"])
def test_a_recorded_chain_ancestor_drops_out_of_the_census_when_the_boot_cannot_be_read(daemon, world, monkeypatch,
                                                                                         boot):
    """`_parent_chain` keeps a recorded ancestor (`chain_identities`) by `ProcessTable.is_process`,
    which raises when the boot cannot be read; the rule records `parent chain identity inspection
    unavailable` and does not add the ancestor (`procs.py:1396-1400`). Beside a stranger's
    leaderless group the census's one member is the stranger, which no read attributes, so `kill`
    answers `already finished`, naming only the stranger, and the ancestor runs on. With the boot
    readable, `kill` TERMs it. A follow-up (D-KILL4-CHAIN1) keeps the ancestor as a chain member
    with no identity, so `_may_signal`'s chain term runs the protocol."""
    tree = owned_worktree(daemon, monkeypatch, world)
    ancestor = world.add(ANCESTOR, 1, ANCESTOR, cwd=str(tree))
    member = world.add(MEMBER, 1, GUARDIAN)                       # a stranger's leaderless group 4242
    store(daemon, chain_identities={str(ANCESTOR): kept(procs.ProcessIdentity(ANCESTOR, BOOT_A, ancestor.start))},
          chain_reasons={str(ANCESTOR): f"pid {ANCESTOR}: ancestor of marked pid 5901, cwd in worktree"})
    finished(daemon)
    daemon.term_grace_s, daemon.kill_settle_s = 0.2, 0.1
    world.boot = BOOT_A if boot == "readable" else None
    census = daemon._contain(attempt(daemon))
    if boot == "readable":
        assert ANCESTOR in census.chain_pids and not census.unverifiable, census.to_dict()
    else:
        assert census.unverifiable and ANCESTOR not in census.live_pids, census.to_dict()
        assert f"parent chain identity inspection unavailable for pid {ANCESTOR}" in census.errors
    inline(daemon, monkeypatch)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    if boot == "readable":
        assert answer["status"] == "containment requested", answer
        assert not ancestor.alive and ancestor.ended_by == TERM, world.tried
    else:
        assert answer["status"] == "already finished", answer
        assert f"pid {MEMBER}" in answer["detail"] and f"pid {ANCESTOR}" not in answer["detail"], answer
        assert ancestor.alive and not world.tried, world.tried
    assert member.alive
