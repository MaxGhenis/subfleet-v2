"""D-QUAR-REUSE1, C-5.5, C-5.12: a recorded pid from an earlier boot never keeps an attempt quarantined.

Incident 2026-10-24: `20261014-071003-trackb-d1408-review5/a1` stayed quarantined after
`kill --confirm-dead`. Its guardian was recorded on boot A (`start.json`: pid 76526, started
"Wed Oct 14 12:05:49 2026"); its exit receipt named `child_pid 83026` with no identity; on
the current boot B, pid 83026 was an unrelated `tail -40` born at 15:07:47. Release/217's
census (15bd8c172, copied in `tests/reference_census_15bd8c172.py`) seeded its walk with
every recorded pid it could not prove recycled, and an identity-less live pid cannot be
proved recycled, so the stranger was counted, the census was unverifiable ("recorded
identity unavailable for pid 83026"), and `Daemon._contain` then kept the stranger's
current-boot identity in `counted_identities` (D-KILL2), from where every later census
seeded it as a recorded survivor whose identity matched.

A reboot ends every process. The child a receipt names was born on the attempt's boot,
the boot its guardian identity records, so when that boot is known and is not this one,
no process now alive can be the child or descend from it:

1. A seed of such an attempt (the guardian, the receipt's or row's child, every stored
   identity) and the group of its recorded pgid count nothing; an identity-less seed is
   recorded under `recycled` as "different boot identity (attempt's boot)", with no error.
2. `_contain` never keeps an identity whose boot differs from the attempt's recorded boot.
3. A stored identity recorded on another boot than the attempt's is not the attempt's:
   never a seed, never counted, and named in the census evidence (`other_boot`).
4. The attempt's responsibility root is its guardian, so a check of the root's identity (one
   recorded without a kernel unique id, or the final holder read) cannot hold it either.

An unknown attempt boot (none recorded, legacy `kern.boottime` seconds, or a current boot
that cannot be read) keeps release/217's conservative answer, and on the attempt's own
boot every source counts what it counted at 15bd8c172.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import re
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, event, given, settings, strategies as st
import pytest

from subfleet import boot_identity, procs
from subfleet.daemon import Daemon
from tests import reference_census_15bd8c172 as base
from tests.fake_kernel import procargs
from tests.unit.test_census_procargs import Store, captured
from tests.unit.test_procs import BOOT_A, BOOT_B, census

#: Bounded, derandomized and without a database: the ci profile's terms.
PROPERTY = settings(max_examples=150, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])

JOB = "20261014-071003-trackb-d1408-review5"
ATTEMPT = f"{JOB}/a1"
ROOT = "/fixture/state"
GUARDIAN, CHILD, SHELL = 76526, 83026, 83000
GUARDIAN_START = "Wed Oct 14 12:05:49 2026"
STRANGER_START = "Sat Oct 24 15:07:47 2026"
SHELL_START = "Sat Oct 24 15:01:02 2026"
REASON = "different boot identity (attempt's boot)"
LEGACY = "99"                  # `kern.boottime` seconds the fixture's boot (100) does not match
BOOT_C = "33333333-3333-4333-8333-333333333333"
#: The errors a seed's comparison adds; a cross-boot attempt's seeds never add one.
SEED_ERRORS = re.compile(r"^(recorded identity unavailable|start identity unavailable for root|"
                         r"boot identity (unknown|unavailable) for root) pid \d+$")


def stranger_table(*extra: str) -> str:
    """The current boot as the incident found it: a shell and its `tail -40` at 83026."""
    return "".join([f"1 0 1 Ss {SHELL_START}\n",
                    f"{SHELL} 1 {SHELL} Ss {SHELL_START}\n",
                    f"{CHILD} {SHELL} {SHELL} S {STRANGER_START}\n", *extra])


def trackb_row(*, child_in_row: bool = False, boot: str | None = BOOT_A, **evidence) -> dict:
    return {"attempt_id": ATTEMPT, "job_id": JOB, "seq": 1, "guardian_pid": GUARDIAN, "pgid": GUARDIAN,
            "child_pid": CHILD if child_in_row else None, "boot_id": boot,
            "proc_start": GUARDIAN_START if boot else None, "started_at": "2026-10-14T12:05:49Z",
            "evidence_json": json.dumps({"responsible_root": {"pid": GUARDIAN, "unique_id": 9000}, **evidence})}


def trackb_core(row: dict, *, receipt_child: int | None = CHILD, start_boot: str | None = BOOT_A):
    """`Daemon._contain`'s view: the row, `start.json` and the exit receipt."""
    files = {"start.json": {"guardian_pid": GUARDIAN, "pgid": GUARDIAN, "boot_id": start_boot,
                            "proc_start": GUARDIAN_START} if start_boot else None,
             "exit.json": {"rc": 0, "signal": None, "child_pid": receipt_child,
                           "finished_at": "2026-10-14T12:28:32Z"}}
    return SimpleNamespace(root=Path(ROOT), store=Store(row), _read_json=lambda path: files.get(path.name))


def install(patch, parents: str, *, session: str | None = BOOT_B, markers: str = "", fail: str | None = None):
    procs.forget_boot_id()
    census(patch, parents=parents, markers=markers, session=session, fail=fail)


def recording(patch) -> list[dict]:
    """Wrap the real census, keeping what `_contain` asked of it."""
    calls, real = [], procs.containment

    def containment(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)
    patch.setattr(procs, "containment", containment)
    return calls


def at_base(call: dict) -> procs.Containment:
    """15bd8c172's census of the same request (it had no `attempt_boot`)."""
    procs.forget_boot_id()
    kwargs = {key: value for key, value in call["kwargs"].items() if key != "attempt_boot"}
    return base.containment(*call["args"], **kwargs)


def ident(pid: int, boot: str, start: str) -> procs.ProcessIdentity:
    return procs.ProcessIdentity(pid, boot, start)


# Regressions ------------------------------------------------------------------------------

@pytest.mark.parametrize("child_in_row", [False, True], ids=["receipt-child", "row-child"])
def test_trackb_a_child_pid_reused_on_a_later_boot_is_recycled_not_counted(monkeypatch, child_in_row):
    """The incident: the receipt's (or row's) identity-less child pid, now a stranger on a later
    boot, is recorded under `recycled` with the attempt's boot as its reason, and the census is
    verified empty. Release/217 counted it and could not verify (its census is the incident's)."""
    row = trackb_row(child_in_row=child_in_row)
    install(monkeypatch, stranger_table())
    calls = recording(monkeypatch)
    result = Daemon._contain(trackb_core(row), dict(row))
    assert result.verified_empty, result.to_dict()
    assert result.errors == () and result.live_pids == frozenset()
    assert result.recycled[CHILD] == {"recorded": {"pid": CHILD, "boot_id": BOOT_A, "proc_start": None},
                                      "now": {"pid": CHILD, "boot_id": BOOT_B, "proc_start": STRANGER_START},
                                      "reason": REASON}
    assert calls[-1]["kwargs"]["attempt_boot"] == BOOT_A
    before = at_base(calls[-1]).to_dict()                         # the incident's census
    assert (before["descendant_pids"], before["live_pids"], before["unverifiable"]) == ([CHILD], [CHILD], True)
    assert before["errors"] == [f"recorded identity unavailable for pid {CHILD}"]
    evidence = json.loads(row["evidence_json"])
    assert evidence["recycled"][str(CHILD)]["reason"] == REASON    # kept for `runs show`


def test_trackb_the_census_adopts_no_current_boot_identity(monkeypatch):
    """After the trackb census, `counted_identities` holds no identity from the current boot:
    release/217 kept the stranger's (event `attempt.counted_recorded {"pids": [83026]}`)."""
    row = trackb_row()
    install(monkeypatch, stranger_table())
    core = trackb_core(row)
    for _ in range(3):
        Daemon._contain(core, dict(row))
    evidence = json.loads(row["evidence_json"])
    assert all(value["boot_id"] != BOOT_B for key in ("counted_identities", "responsible_identities")
               for value in evidence.get(key, {}).values())
    assert [kind for kind, _ in core.store.events] == ["attempt.processes_recycled"]


def test_decision_2_an_identity_from_another_boot_than_the_attempts_is_never_kept(monkeypatch):
    """Whatever a census counts (here a marker process and a member naming the root, each on
    the current boot), `_contain` keeps no identity whose boot is not the attempt's recorded one."""
    seen = procs.Containment(marker_pids=frozenset({61}), responsible_pids=frozenset({62}),
                             identities={61: ident(61, BOOT_B, STRANGER_START), 62: ident(62, BOOT_B, STRANGER_START)})
    row = trackb_row()
    core = SimpleNamespace(root=Path(ROOT), store=Store(row), _read_json=lambda path: None)
    captured(monkeypatch, seen)
    Daemon._contain(core, dict(row))
    evidence = json.loads(row["evidence_json"])
    assert "counted_identities" not in evidence and "responsible_identities" not in evidence
    assert core.store.events == []


def test_decision_2_on_the_attempts_own_boot_every_member_is_still_kept(monkeypatch):
    """D-KILL2 and D-CEN1 unchanged: on the attempt's boot, or with an attempt boot that is
    unknown, each counted member is kept exactly as before."""
    for attempt_boot, member_boot in [(BOOT_A, BOOT_A), (None, BOOT_B), (LEGACY, BOOT_B), (BOOT_A, "100")]:
        seen = procs.Containment(descendant_pids=frozenset({61}), responsible_pids=frozenset({62}),
                                 identities={61: ident(61, member_boot, STRANGER_START),
                                             62: ident(62, member_boot, STRANGER_START)})
        row = trackb_row(boot=attempt_boot)
        core = SimpleNamespace(root=Path(ROOT), store=Store(row), _read_json=lambda path: None)
        with monkeypatch.context() as patch:
            captured(patch, seen)
            Daemon._contain(core, dict(row))
        evidence = json.loads(row["evidence_json"])
        assert evidence["counted_identities"] == {"61": asdict(seen.identities[61])}, attempt_boot
        assert evidence["responsible_identities"] == {"62": asdict(seen.identities[62])}, attempt_boot


@pytest.mark.parametrize("child", [True, False], ids=["also-the-receipt-child", "counted-only"])
def test_repair_an_adopted_later_boot_identity_is_not_the_attempts(monkeypatch, child):
    """trackb's row as release/217 left it: `counted_identities` holds the stranger's current-boot
    identity, which matches the live `tail`. That entry is not the attempt's: never a seed, never
    counted, named in the census evidence, and left in the attempt's history unchanged. With
    nothing else live the census verifies empty; release/217's counted the stranger."""
    adopted = asdict(ident(CHILD, BOOT_B, STRANGER_START))
    row = trackb_row(counted_identities={str(CHILD): adopted})
    install(monkeypatch, stranger_table())
    calls = recording(monkeypatch)
    result = Daemon._contain(trackb_core(row, receipt_child=CHILD if child else None), dict(row))
    assert result.verified_empty, result.to_dict()
    assert result.other_boot == {CHILD: [adopted]}
    assert result.to_dict()["other_boot"] == {str(CHILD): [adopted]}
    assert json.loads(row["evidence_json"])["counted_identities"] == {str(CHILD): adopted}   # history kept
    before = at_base(calls[-1])
    assert CHILD in before.descendant_pids and not before.verified_empty


def test_repair_still_holds_for_whatever_else_is_live(monkeypatch):
    """"Once nothing else is live": a process carrying the attempt's exact marker pair is still
    counted by its marker (an environment match, not a pid), and is not kept from another boot."""
    adopted = asdict(ident(CHILD, BOOT_B, STRANGER_START))
    row = trackb_row(counted_identities={str(CHILD): adopted})
    install(monkeypatch, stranger_table(f"90 1 90 S {STRANGER_START}\n"),
            markers=f"90 pm2 SUBFLEET_ATTEMPT={ATTEMPT} SUBFLEET_ROOT={ROOT}\n")
    result = Daemon._contain(trackb_core(row), dict(row))
    assert result.marker_pids == {90} and result.descendant_pids == frozenset()
    assert not result.verified_empty and CHILD in result.recycled
    assert "90" not in json.loads(row["evidence_json"])["counted_identities"]


def test_same_boot_an_identity_less_live_seed_is_still_unverifiable(monkeypatch):
    """On the attempt's own boot nothing changes: the identity-less child is counted and the
    census cannot verify, exactly as at 15bd8c172. (Guard: passes at the base by design.)"""
    row = trackb_row()
    install(monkeypatch, stranger_table(), session=BOOT_A)
    calls = recording(monkeypatch)
    result = Daemon._contain(trackb_core(row), dict(row))
    assert result.descendant_pids == {CHILD} and result.unverifiable
    assert result.errors == (f"recorded identity unavailable for pid {CHILD}",)
    assert result.to_dict() == at_base(calls[-1]).to_dict()


@pytest.mark.parametrize("attempt_boot,session,fail", [
    (None, BOOT_B, None),            # no boot recorded in the row or start.json
    (LEGACY, BOOT_B, None),          # legacy seconds: C-5.3 can only say unknown
    (BOOT_A, None, "sysctl"),        # the current boot cannot be read
    (BOOT_A, None, None),            # the current boot is legacy seconds (its UUID read failed)
], ids=["unrecorded", "legacy-seconds", "unreadable", "current-legacy"])
def test_unknown_boot_the_result_is_unchanged(monkeypatch, attempt_boot, session, fail):
    """With no attempt boot known to be over, the census is 15bd8c172's: the live identity-less
    child keeps the attempt unverifiable. (Guard: passes at the base by design.)"""
    row = trackb_row(boot=attempt_boot)
    install(monkeypatch, stranger_table(), session=session, fail=fail)
    calls = recording(monkeypatch)
    result = Daemon._contain(trackb_core(row, start_boot=attempt_boot), dict(row))
    assert CHILD in result.descendant_pids and result.unverifiable and not result.recycled
    assert result.to_dict() == at_base(calls[-1]).to_dict()


def test_an_orphaned_group_and_an_absent_seeds_children_on_a_later_boot_are_not_the_attempts(monkeypatch):
    """The other ways a recorded pid reaches the current boot: a stranger's group whose number is
    the old pgid (its leader gone), and live rows whose parent is an absent seed (the snapshot is
    not atomic). Release/217 counted both."""
    table = (f"1 0 1 Ss {SHELL_START}\n"
             f"500 1 {GUARDIAN} S {STRANGER_START}\n"         # group 76526, its leader gone
             f"501 {GUARDIAN} 501 S {STRANGER_START}\n")      # parent 76526, absent
    row = trackb_row()
    install(monkeypatch, table)
    calls = recording(monkeypatch)
    result = Daemon._contain(trackb_core(row, receipt_child=None), dict(row))
    assert result.verified_empty and result.group_pids == result.descendant_pids == frozenset()
    assert result.recycled[GUARDIAN]["reason"] == REASON
    assert result.recycled[GUARDIAN]["now"] == {"pid": GUARDIAN, "boot_id": BOOT_B, "proc_start": None}
    before = at_base(calls[-1])
    assert before.group_pids == {500} and before.descendant_pids == {501}


def test_a_seed_with_a_full_birth_keeps_its_own_reason(monkeypatch):
    """A guardian pid now held by a stranger is excluded for the reason release/217 gave
    ("different start identity"); only seeds it could not decide take the attempt's boot."""
    row = trackb_row()
    install(monkeypatch, stranger_table(f"{GUARDIAN} 1 {GUARDIAN} Ss {STRANGER_START}\n"))
    result = Daemon._contain(trackb_core(row), dict(row))
    assert result.verified_empty
    assert result.recycled[GUARDIAN]["reason"] == "different start identity"
    assert result.recycled[CHILD]["reason"] == REASON


ROOT_ERROR = f"responsibility root identity unavailable for pid {GUARDIAN}"
ROOT_SHAPES = ["unique-id-unrecorded", "final-holder-unreadable"]


def root_shape(patch, row: dict, shape: str) -> dict:
    """The guardian as a responsibility root whose own identity check fails once its pid is gone:
    recorded without a kernel unique id (the launch's metadata read failed, `_spawn_guardian`), or
    with one, its final holder read failing."""
    if shape == "unique-id-unrecorded":
        evidence = json.loads(row["evidence_json"])
        evidence["responsible_root"] = {"pid": GUARDIAN, "unique_id": None}
        row["evidence_json"] = json.dumps(evidence)
    else:
        procs.kernel.fail.add(("process", GUARDIAN))
    return row


@pytest.mark.parametrize("shape", ROOT_SHAPES)
def test_the_attempts_responsibility_root_never_holds_it_on_a_later_boot(monkeypatch, shape):
    """The root is the guardian, a recorded pid of the attempt's boot, and no process of a later
    boot names it: a root recorded without a unique id needs no live matching start there, and a
    known one no final holder read ("a proven other boot needs no final holder check", C-5.5).
    At 993a28ef3 and at 15bd8c172 each held trackb's shape with `responsibility root identity
    unavailable for pid 76526`; the first for as long as pid 76526 stayed free."""
    row = trackb_row()
    install(monkeypatch, stranger_table())
    root_shape(monkeypatch, row, shape)
    calls = recording(monkeypatch)
    result = Daemon._contain(trackb_core(row), dict(row))
    assert result.verified_empty, result.to_dict()
    assert result.errors == () and result.recycled[CHILD]["reason"] == REASON
    assert ROOT_ERROR in at_base(calls[-1]).errors


@pytest.mark.parametrize("attempt_boot,session", [(BOOT_A, BOOT_A), (None, BOOT_B), (LEGACY, BOOT_B)],
                         ids=["own-boot", "unrecorded", "legacy-seconds"])
@pytest.mark.parametrize("shape", ROOT_SHAPES)
def test_the_roots_identity_checks_are_unchanged_without_a_boot_known_over(monkeypatch, shape,
                                                                         attempt_boot, session):
    """On the attempt's own boot, or with its boot unknown, the root's failed identity check still
    prevents verification, exactly as at 15bd8c172. (Guard: passes at the base by design.)"""
    row = trackb_row(boot=attempt_boot)
    install(monkeypatch, stranger_table(), session=session)
    root_shape(monkeypatch, row, shape)
    calls = recording(monkeypatch)
    result = Daemon._contain(trackb_core(row, receipt_child=None, start_boot=attempt_boot), dict(row))
    assert ROOT_ERROR in result.errors and result.unverifiable
    assert result.to_dict() == at_base(calls[-1]).to_dict()


MEMBER = 83100
#: C-5.5's boot-scope sentence in trackb's shape (the attempt on boot A, the census on boot B), by
#: root shape: the root's recorded unique id, its pid on boot B ("free"; "unreadable", free with
#: the kernel's read of it failing; "stranger", a row of boot B) and whether a process names it as
#: responsible, then `kernel.process(76526)` calls, of those the final holder read after the member
#: scan, and whether the scan was given the root, so read its members. Review r6 of #240 executed
#: the first four rows. The last two are a root the census proves recycled, which disables the
#: source.
BOOT_SCOPE = {
    "known-id-holder-read-fails": ((9000, "unreadable", False), (1, 1, True)),
    "known-id-holder-gone": ((9000, "free", False), (1, 1, True)),
    "unique-id-unrecorded": ((None, "free", False), (0, 0, False)),
    "a-member-names-the-root": ((9000, "free", True), (0, 0, True)),
    "known-id-pid-held-by-a-stranger": ((9000, "stranger", False), (1, 0, False)),
    "unique-id-unrecorded-pid-held-by-a-stranger": ((None, "stranger", False), (1, 0, False)),
}


@pytest.mark.parametrize("shape", list(BOOT_SCOPE))
def test_c5_5_boot_scope_the_final_holder_read_by_root_shape(monkeypatch, shape):
    """C-5.5 once the attempt's boot is over (review r6 of #240, P3-1). A root with a recorded unique
    id takes its final holder read unless the census proved the root recycled or a member's read
    proved the other boot, and that read's failure is dropped. A root recorded without one, whose
    start is absent or changed, drops the responsibility source without an error, so neither its
    members nor its holder are read. Every shape verifies empty with no error. A stranger's pid is
    a listed pid, so the marker source reads it once in the scan; that read is not a holder read."""
    (unique_id, holder, member), (process_calls, final_reads, source) = BOOT_SCOPE[shape]
    row = trackb_row()
    evidence = json.loads(row["evidence_json"])
    evidence["responsible_root"] = {"pid": GUARDIAN, "unique_id": unique_id}
    row["evidence_json"] = json.dumps(evidence)
    extra = (f"{GUARDIAN} 1 {GUARDIAN} Ss {STRANGER_START}\n",) if holder == "stranger" else ()
    install(monkeypatch, stranger_table(*extra))
    kernel = procs.kernel
    if holder == "unreadable":
        kernel.fail.add(("process", GUARDIAN))
    if member:
        kernel.processes[MEMBER] = procargs(["tail"])
        kernel.boss[MEMBER] = GUARDIAN
    scans, real = [], procs._kernel_sources

    def scan(kernel_reader, candidates, marker, root, ours):
        try:
            return real(kernel_reader, candidates, marker, root, ours)
        finally:
            scans.append((len(kernel.calls), root is not None))
    monkeypatch.setattr(procs, "_kernel_sources", scan)
    result = Daemon._contain(trackb_core(row), dict(row))
    assert result.verified_empty and result.errors == (), result.to_dict()
    assert [rooted for _, rooted in scans] == [source], scans
    assert kernel.calls.count(("process", GUARDIAN)) == process_calls, kernel.calls
    assert kernel.calls[scans[0][0]:].count(("process", GUARDIAN)) == final_reads, kernel.calls
    if holder == "stranger":
        assert result.recycled[GUARDIAN]["reason"] == "different start identity"
    if member:
        assert ("responsible", MEMBER) in kernel.calls and MEMBER not in result.responsible_pids


def test_c5_12_the_attempts_boot_is_read_only_when_a_seed_depends_on_it(monkeypatch):
    """No seed in the table, no parent row and no group: the census reads no boot identity,
    as at 15bd8c172 (C-5.12). One that matters is one read, and a mismatch is read afresh."""
    row = trackb_row()
    reads = []
    install(monkeypatch, f"1 0 1 Ss {SHELL_START}\n7 1 7 S {SHELL_START}\n")
    inner = procs._read
    monkeypatch.setattr(procs, "_read", lambda argv, **kw: reads.append(argv[-1]) or inner(argv, **kw))
    assert Daemon._contain(trackb_core(row), dict(row)).verified_empty
    assert "kern.bootsessionuuid" not in reads
    install(monkeypatch, stranger_table())
    inner = procs._read
    reads.clear()
    monkeypatch.setattr(procs, "_read", lambda argv, **kw: reads.append(argv[-1]) or inner(argv, **kw))
    assert Daemon._contain(trackb_core(row), dict(row)).verified_empty
    assert reads.count("kern.bootsessionuuid") == 2               # the table's, then the fresh one


def test_boot_identity_differ_needs_two_session_uuids():
    """Two recorded boots provably differ only as two different boot-session UUIDs (C-5.3)."""
    assert boot_identity.differ(BOOT_A, BOOT_B) and boot_identity.differ(BOOT_B, BOOT_A.upper())
    assert not boot_identity.differ(BOOT_A, BOOT_A) and not boot_identity.differ(BOOT_A, BOOT_A.upper())
    for other in (None, "", "100", LEGACY, "unit-test-boot"):
        assert not boot_identity.differ(BOOT_A, other) and not boot_identity.differ(other, BOOT_A)
    assert not boot_identity.differ("100", "99")                  # legacy seconds shift within a boot
    for first in RECORDED_BOOTS + [None, BOOT_A.upper()]:
        for second in RECORDED_BOOTS + [None]:
            assert boot_identity.differ(first, second) == provably_other(first, second), (first, second)


# Generated worlds -------------------------------------------------------------------------

PIDS = list(range(100, 110))
NOW_STARTS = [f"Sat Oct 24 15:0{n}:00 2026" for n in range(5)]
OLD_START = GUARDIAN_START
#: Recorded boots: the attempt's, the current one's, another UUID, legacy seconds that match
#: the fixture's (100) and that do not (99), and a missing one.
RECORDED_BOOTS = [BOOT_A, BOOT_B, BOOT_C, "100", LEGACY, ""]


@st.composite
def worlds(draw, *, attempt_boots=(BOOT_A, None, LEGACY), sessions=(BOOT_A, BOOT_B, None, "fail"),
           recorded_boots=tuple(RECORDED_BOOTS)):
    rows = {1: (0, 1, "Ss", SHELL_START)}
    for pid in sorted(draw(st.sets(st.sampled_from(PIDS), max_size=8))):
        rows[pid] = (draw(st.sampled_from([0, 1, *PIDS])), draw(st.sampled_from([pid, 100, 101, 102])),
                     draw(st.sampled_from(["S", "Ss", "R", "Z"])), draw(st.sampled_from([*NOW_STARTS, OLD_START])))
    births = st.builds(lambda boot, start: (boot, start), st.sampled_from(recorded_boots),
                       st.sampled_from([*NOW_STARTS, OLD_START, ""]))
    stored = st.dictionaries(st.sampled_from(PIDS), births, max_size=4)
    world = {
        "rows": rows,
        "attempt_boot": draw(st.sampled_from(attempt_boots)),
        "session": draw(st.sampled_from(sessions)),
        "guardian": draw(st.sampled_from(PIDS)),
        "child": draw(st.none() | st.sampled_from(PIDS)),
        "guardian_start": draw(st.sampled_from([OLD_START, *NOW_STARTS])),
        "owned": draw(stored), "named": draw(stored), "counted": draw(stored),
        "marked": draw(st.sets(st.sampled_from(sorted(rows)), max_size=3)),
        # A responsibility root: none, one with a kernel unique id, or one recorded without.
        "rooted": draw(st.sampled_from([False, 9000, None])),
        "holder_fails": draw(st.booleans()),     # the kernel read of the guardian's pid fails
    }
    return world


def world_row(world: dict) -> dict:
    def kept(entries):
        return {str(pid): {"pid": pid, "boot_id": boot, "proc_start": start} for pid, (boot, start) in entries.items()}
    evidence = {"owned_identities": kept(world["owned"]), "responsible_identities": kept(world["named"]),
                "counted_identities": kept(world["counted"])}
    if world["rooted"] is not False:
        evidence["responsible_root"] = {"pid": world["guardian"], "unique_id": world["rooted"]}
    return {"attempt_id": ATTEMPT, "job_id": JOB, "seq": 1, "guardian_pid": world["guardian"],
            "pgid": world["guardian"], "child_pid": world["child"], "boot_id": world["attempt_boot"],
            "proc_start": world["guardian_start"] if world["attempt_boot"] else None,
            "started_at": "2026-10-14T12:05:49Z", "evidence_json": json.dumps(evidence)}


def run_world(patch, world: dict):
    """`_contain` on the world, with the real census; the census, its request and the row after."""
    session = world["session"]
    install(patch, "".join(f"{pid} {ppid} {pgid} {stat} {began}\n" for pid, (ppid, pgid, stat, began)
                           in world["rows"].items()),
            session=None if session == "fail" else session, fail="sysctl" if session == "fail" else None,
            markers="".join(f"{pid} fake SUBFLEET_ATTEMPT={ATTEMPT} SUBFLEET_ROOT={ROOT}\n"
                            for pid in sorted(world["marked"])))
    if world["holder_fails"]:
        procs.kernel.fail.add(("process", world["guardian"]))
    row = world_row(world)
    core = SimpleNamespace(root=Path(ROOT), store=Store(row), _read_json=lambda path: None)
    calls = recording(patch)
    result = Daemon._contain(core, dict(row))
    return result, calls[-1], row


def provably_other(recorded: str | None, attempt: str | None) -> bool:
    """The tests' own statement of "recorded on another boot" (C-5.3): two different
    boot-session UUIDs, and nothing else. `boot_identity.differ` is checked against it."""
    first, second = (boot_identity.session_uuid(value or "") for value in (recorded, attempt))
    return bool(first and second and first != second)


def cross_boot(world: dict) -> bool:
    """The attempt's recorded boot is known and is not the current one (case 1)."""
    attempt, session = world["attempt_boot"], world["session"]
    return bool(boot_identity.session_uuid(attempt or "") and boot_identity.session_uuid(session or "")
                and attempt != session)


def stores_another_boot(world: dict) -> bool:
    """A stored identity names a boot other than the attempt's (case 3)."""
    return any(provably_other(boot, world["attempt_boot"])
               for key in ("owned", "named", "counted") for boot, _ in world[key].values())


@PROPERTY
@given(worlds(attempt_boots=(BOOT_A,), sessions=(BOOT_B,)))
def test_invariant_1_cross_boot_no_current_process_is_counted_seeded_or_kept(monkeypatch, world):
    """For an attempt whose recorded boot is known and over, no process on the current boot is
    counted through a recorded pid (a seed, the walk, the recorded group, the root's members),
    no seed comparison or check of the root's identity holds it, and `_contain` keeps none of their
    identities. The marker source counts only processes carrying the attempt's exact marker pair,
    and those are not kept either."""
    with monkeypatch.context() as patch:
        result, _, row = run_world(patch, world)
    assert result.group_pids == result.descendant_pids == result.responsible_pids == frozenset()
    assert result.live_pids == result.marker_pids
    assert not [error for error in result.errors if SEED_ERRORS.match(error)]
    assert f"responsibility root identity unavailable for pid {world['guardian']}" not in result.errors
    seeds = {world["guardian"], *([world["child"]] if world["child"] else []), *world["owned"],
             *world["named"], *world["counted"]}
    present = {pid for pid in seeds if pid in world["rows"]}
    stored_foreign = {pid for key in ("owned", "named", "counted") for pid, (boot, _) in world[key].items()
                      if provably_other(boot, BOOT_A)}
    attempts_own = {world["guardian"], world["child"]} | {
        pid for key in ("owned", "named", "counted") for pid, (boot, _) in world[key].items()
        if not provably_other(boot, BOOT_A)}
    assert present & attempts_own <= set(result.recycled)
    assert set(result.other_boot) == stored_foreign
    evidence, stored = json.loads(row["evidence_json"]), json.loads(world_row(world)["evidence_json"])
    for key in ("counted_identities", "responsible_identities"):
        written = {pid: value for pid, value in evidence.get(key, {}).items() if stored[key].get(pid) != value}
        assert written == {}, (key, written)                    # every census identity is the current boot's


@PROPERTY
@given(worlds(recorded_boots=(BOOT_A, "100", LEGACY, "")))
def test_invariant_2_on_the_attempts_own_boot_every_source_counts_as_at_15bd8c172(monkeypatch, world):
    """Without a cross-boot attempt or a stored identity from another boot, today's census is
    15bd8c172's, whole: sources, identities, errors (identity-less live seeds stay
    unverifiable), shapes and recycled evidence."""
    if cross_boot(world):
        world = {**world, "session": world["attempt_boot"]}
    assert not stores_another_boot(world)
    with monkeypatch.context() as patch:
        result, call, _ = run_world(patch, world)
        before = at_base(call)
    assert result.to_dict() == before.to_dict()


@PROPERTY
@given(worlds())
def test_invariant_3_any_difference_from_15bd8c172_is_a_cross_boot_case(monkeypatch, world):
    """Differential against the base census over generated tables, boots and stored identities:
    where the two disagree, the attempt's boot is over (case 1) or a stored identity names another
    boot (case 3). In particular nothing moves from unverifiable to verified-empty otherwise."""
    with monkeypatch.context() as patch:
        result, call, _ = run_world(patch, world)
        before = at_base(call)
    case = "cross-boot" if cross_boot(world) else "another boot stored" if stores_another_boot(world) else "same"
    differs = result.to_dict() != before.to_dict()
    event(f"{case}: {'differs' if differs else 'equal'}"
          + (", released" if before.unverifiable and result.verified_empty else ""))
    if case == "same":
        assert not differs, (result.to_dict(), before.to_dict())
    if before.unverifiable and result.verified_empty:
        assert case != "same"
    if case == "cross-boot":
        # Only seeds and the recorded group are dropped: the census never gains a pid or an error.
        assert result.live_pids <= before.live_pids and set(result.errors) <= set(before.errors)
        assert result.marker_pids == before.marker_pids


@PROPERTY
@given(worlds())
def test_invariant_4_contain_never_writes_signal_authority(monkeypatch, world):
    """Counted and responsible identities are evidence only: `_contain` never writes
    `owned_identities`, the only key the kill protocol signals by (C-5.4, C-5.6)."""
    before = json.loads(world_row(world)["evidence_json"])["owned_identities"]
    with monkeypatch.context() as patch:
        _, _, row = run_world(patch, world)
    assert json.loads(row["evidence_json"])["owned_identities"] == before


IDENTITIES = st.builds(ident, st.sampled_from(PIDS), st.sampled_from(RECORDED_BOOTS),
                       st.sampled_from([*NOW_STARTS, OLD_START]))


@PROPERTY
@given(attempt_boot=st.sampled_from([BOOT_A, BOOT_B, None, LEGACY, "100"]),
       members=st.lists(IDENTITIES, max_size=6, unique_by=lambda value: value.pid),
       named=st.sets(st.sampled_from(PIDS)))
def test_decision_2_property_kept_identities_are_exactly_the_attempts_boot(monkeypatch, attempt_boot,
                                                                         members, named):
    """For any census result: `_contain` keeps every counted member it kept at 15bd8c172 (D-KILL2,
    D-CEN1) except one whose boot provably differs from the attempt's recorded boot, and only
    the attempt's recorded boot decides that, never the current one."""
    identities = {value.pid: value for value in members}
    seen = procs.Containment(descendant_pids=frozenset(identities) - named,
                             responsible_pids=frozenset(identities) & named, identities=identities)
    row = trackb_row(boot=attempt_boot)
    core = SimpleNamespace(root=Path(ROOT), store=Store(row), _read_json=lambda path: None)
    with monkeypatch.context() as patch:
        captured(patch, seen)
        Daemon._contain(core, dict(row))
    evidence = json.loads(row["evidence_json"])
    kept = {**evidence.get("counted_identities", {}), **evidence.get("responsible_identities", {})}
    expected = {str(pid) for pid, value in identities.items() if pid != GUARDIAN
                and not provably_other(value.boot_id, attempt_boot)}
    assert set(kept) == expected
    assert "owned_identities" not in evidence
