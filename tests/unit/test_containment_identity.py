"""C-5.3/C-5.5: ownership survives exits, never PID reuse (I1-I5).

All process tables and environment reads are synthetic. No test examines or
signals a host process. The property oracle follows each candidate's ancestry
individually, independently of containment's descendant traversal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import procs

BOOT = "11111111-1111-4111-8111-111111111111"
OTHER_BOOT = "22222222-2222-4222-8222-222222222222"
START = "Sun Sep 27 23:26:33 2026"
REUSED_START = "Mon Sep 28 22:11:29 2026"
ATTEMPT = "20260927-151250-r218-conv-opus/a2"
ROOT = "/synthetic/subfleet"


def recorded(pid, *, boot=BOOT, started=START):
    return procs.ProcessIdentity(pid, boot, started)


def install_census(monkeypatch, rows, *, marked=(), wrong_root=(), boot=BOOT,
                   failure=None, reverse=False):
    """Patch both inspection sources; an unexpected per-pid read is a failure."""
    ordered = list(rows.items())
    if reverse:
        ordered.reverse()

    def snapshot():
        if failure == "snapshot":
            raise procs.InspectionError("synthetic process table failure")
        return procs.ProcessTable(dict(ordered), None if failure == "boot" else boot)

    def read(argv, **kwargs):
        if argv == ["/bin/ps", "-axEww", "-o", "pid=,command="]:
            if failure == "markers":
                raise procs.InspectionError("synthetic marker failure")
            marker_rows = [f"{pid} provider SUBFLEET_ATTEMPT={ATTEMPT} SUBFLEET_ROOT={ROOT}"
                           for pid in sorted(marked)]
            marker_rows += [f"{pid} provider SUBFLEET_ATTEMPT={ATTEMPT} SUBFLEET_ROOT=/other"
                            for pid in sorted(wrong_root)]
            return "\n".join(reversed(marker_rows) if reverse else marker_rows)
        pytest.fail(f"unexpected host inspection: {argv}")

    def boot_id():
        raise procs.InspectionError("synthetic boot inspection failure")

    monkeypatch.setattr(procs, "snapshot", snapshot)
    monkeypatch.setattr(procs, "_read", read)
    monkeypatch.setattr(procs, "boot_id", boot_id)


def contain(identities, *, pgid=10, guardian=10, child=20):
    return procs.containment(pgid, guardian, child, ATTEMPT, root=ROOT,
                             recorded_identities=identities)


def test_i1_r218_a2_reused_child_in_foreign_group_is_exited(monkeypatch):
    """The live incident: child 80817 was reused almost a day after its launch."""
    install_census(monkeypatch, {80817: (76443, 76443, "S", REUSED_START)})
    result = contain({80807: recorded(80807), 80817: recorded(80817)},
                     pgid=80807, guardian=80807, child=80817)
    assert result.verified_empty
    assert result.reused_pids == {80817}
    assert result.to_dict()["reused_pids"] == [80817]
    assert not result.identities and not result.shapes


@pytest.mark.parametrize("attempt_id,guardian,child,guardian_start,child_start,reused_pid,current_start", [
    ("20260925-074431-inv-eggnest/a1", 45308, 45548,
     "Fri Sep 25 12:08:03 2026", "Fri Sep 25 12:08:04 2026", 45548, "Fri Sep 25 12:35:39 2026"),
    ("20260925-110251-review-chronicle-institute-4/a1", 28242, 28360,
     "Fri Sep 25 15:09:11 2026", "Fri Sep 25 15:09:12 2026", 28242, "Fri Sep 25 15:15:02 2026"),
], ids=["inv-eggnest-a1", "review-chronicle-institute-4-a1"])
def test_i1_confirmed_historical_incidents_reused_root_is_exited(
        monkeypatch, attempt_id, guardian, child, guardian_start, child_start, reused_pid, current_start):
    """Two other live incidents: reused child/self-group and reused guardian/group."""
    install_census(monkeypatch, {reused_pid: (1, reused_pid, "S", current_start)})
    result = procs.containment(guardian, guardian, child, attempt_id, root=ROOT,
                               recorded_identities={guardian: recorded(guardian, started=guardian_start),
                                                    child: recorded(child, started=child_start)})
    assert result.verified_empty
    assert result.reused_pids == {reused_pid}
    assert not result.group_pids and not result.descendant_pids and not result.marker_pids


def test_i1_reused_guardian_cannot_attribute_its_forked_children(monkeypatch):
    install_census(monkeypatch, {10: (1, 900, "S", REUSED_START),
                                30: (10, 900, "S", REUSED_START),
                                31: (30, 31, "S", REUSED_START)})
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.verified_empty
    assert result.reused_pids == {10}


def test_i1_mixed_genuine_and_reused_pids_keeps_only_genuine_writers(monkeypatch):
    install_census(monkeypatch, {20: (900, 900, "S", REUSED_START),
                                30: (1, 10, "S", START)})
    result = contain({10: recorded(10), 20: recorded(20), 30: recorded(30)})
    assert result.live_pids == {30}
    assert result.group_pids == {30}
    assert result.reused_pids == {20}
    assert not result.unverifiable


def test_i1_reused_group_leader_rejects_entire_new_group(monkeypatch):
    install_census(monkeypatch, {10: (1, 10, "S", REUSED_START),
                                30: (10, 10, "S", REUSED_START),
                                31: (1, 10, "S", REUSED_START)})
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.verified_empty
    assert result.reused_pids == {10}


@pytest.mark.parametrize("leader", ["absent", "zombie"])
def test_group_survives_leader_exit_on_same_boot(monkeypatch, leader):
    rows = {30: (1, 10, "S", START), 31: (30, 10, "S", START)}
    if leader == "zombie":
        rows[10] = (1, 10, "Z", START)
    install_census(monkeypatch, rows)
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.group_pids == result.live_pids == {30, 31}
    assert not result.unverifiable


@pytest.mark.parametrize("leader_present", [False, True])
def test_i1_group_from_another_boot_is_not_owned(monkeypatch, leader_present):
    rows = {30: (1, 10, "S", START)}
    if leader_present:
        rows[10] = (1, 10, "S", START)
    install_census(monkeypatch, rows, boot=OTHER_BOOT)
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.verified_empty
    assert not result.group_pids
    if leader_present:
        assert result.reused_pids == {10}


def test_i2_attempt_markers_override_reused_identity_and_group(monkeypatch):
    install_census(monkeypatch, {10: (1, 10, "S", REUSED_START),
                                20: (900, 900, "S", REUSED_START),
                                30: (1, 30, "S", REUSED_START),
                                31: (1, 31, "S", REUSED_START)},
                   marked={10, 20, 30}, wrong_root={31})
    result = contain({pid: recorded(pid) for pid in (10, 20, 30, 31)})
    assert result.live_pids == result.marker_pids == {10, 20, 30}
    assert not result.group_pids and not result.descendant_pids
    assert {10, 20, 30} <= result.reused_pids
    assert all(identity.proc_start == REUSED_START for identity in result.identities.values())


def test_i3_verified_root_counts_generations_in_escaped_groups(monkeypatch):
    install_census(monkeypatch, {20: (1, 20, "S", START),
                                30: (20, 30, "S", START),
                                31: (30, 31, "S", START),
                                32: (31, 32, "S", START)})
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.descendant_pids == result.live_pids == {20, 30, 31, 32}
    assert not result.group_pids and not result.unverifiable


def test_i3_verified_zombie_root_still_attributes_its_live_descendants(monkeypatch):
    install_census(monkeypatch, {20: (1, 20, "Z", START),
                                30: (20, 30, "S", START),
                                31: (30, 31, "S", START)})
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.descendant_pids == result.live_pids == {30, 31}
    assert not result.unverifiable


def test_i1_reused_known_descendant_breaks_ancestry_chain(monkeypatch):
    install_census(monkeypatch, {20: (1, 20, "S", START),
                                30: (20, 30, "S", REUSED_START),
                                31: (30, 31, "S", REUSED_START)})
    result = contain({10: recorded(10), 20: recorded(20), 30: recorded(30)})
    assert result.live_pids == {20}
    assert result.reused_pids == {30}


@pytest.mark.parametrize("rows,identities", [
    ({20: (1, 20, "S", START)}, {10: recorded(10)}),
    ({30: (1, 10, "S", START)}, {20: recorded(20)}),
    ({10: (1, 10, "S", START)}, {}),
])
def test_i4_missing_live_root_or_group_leader_identity_prevents_release(monkeypatch, rows, identities):
    install_census(monkeypatch, rows)
    result = contain(identities)
    assert result.unverifiable and result.errors
    assert not result.verified_empty
    assert not result.live_pids


def test_absent_roots_and_empty_group_need_no_recorded_identities(monkeypatch):
    install_census(monkeypatch, {99: (1, 99, "S", START)})
    assert contain({}).verified_empty


@pytest.mark.parametrize("failure", ["snapshot", "markers", "boot"])
def test_i4_failed_inspection_never_proves_release(monkeypatch, failure):
    install_census(monkeypatch, {10: (1, 10, "S", START)}, failure=failure)
    result = contain({10: recorded(10)})
    assert result.unverifiable and result.errors
    assert not result.verified_empty


@pytest.mark.parametrize("source", ["root", "leader-absent"])
@pytest.mark.parametrize("legacy_answer", ["100", "101", "error"])
def test_legacy_boot_identity_matches_or_remains_unverified(monkeypatch, source, legacy_answer):
    rows = {20: (1, 20, "S", START)} if source == "root" else {30: (1, 10, "S", START)}
    install_census(monkeypatch, rows)

    def legacy_seconds(table):
        if legacy_answer == "error":
            raise procs.InspectionError("synthetic legacy boot inspection failure")
        return legacy_answer

    monkeypatch.setattr(procs.ProcessTable, "legacy_seconds", legacy_seconds)
    result = contain({10: recorded(10, boot="100"), 20: recorded(20, boot="100")})
    if legacy_answer == "100":
        assert result.live_pids == set(rows)
        assert not result.unverifiable and not result.reused_pids
    else:
        # Shifted wall-clock boot seconds are uncertain, not proof of reuse.
        assert result.unverifiable and result.errors
        assert not result.verified_empty and not result.reused_pids


@pytest.mark.parametrize("leader_present", [False, True])
def test_i4_uuid_record_against_legacy_boot_fallback_cannot_prove_exit(monkeypatch, leader_present):
    rows = {30: (1, 10, "S", START)}
    if leader_present:
        rows[10] = (1, 10, "S", START)
    install_census(monkeypatch, rows, boot="100")
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.unverifiable and result.errors
    assert not result.verified_empty and not result.reused_pids


@pytest.mark.parametrize("pid,group", [(20, 20), (10, 10)])
def test_i4_snapshot_without_root_or_leader_start_cannot_prove_exit(monkeypatch, pid, group):
    install_census(monkeypatch, {pid: (1, group, "S", "")})
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.unverifiable and result.errors
    assert not result.verified_empty and not result.reused_pids


@pytest.mark.parametrize("failed_read", ["stat", "identity"])
def test_i4_late_marked_process_with_failed_single_pid_inspection_prevents_release(monkeypatch, failed_read):
    install_census(monkeypatch, {}, marked={99})

    def fail(pid):
        raise procs.InspectionError("synthetic late marker inspection failure")

    monkeypatch.setattr(procs, "_stat", fail if failed_read == "stat" else lambda pid: "S")
    monkeypatch.setattr(procs, "identity", fail)
    result = contain({10: recorded(10), 20: recorded(20)})
    assert result.unverifiable and result.errors
    assert not result.verified_empty
    if failed_read == "identity":
        assert result.marker_pids == {99}


@pytest.mark.parametrize("source", ["marker", "descendant"])
def test_i2_i4_incomplete_snapshot_identity_keeps_writer_without_fresh_read(monkeypatch, source):
    rows = {30: (20 if source == "descendant" else 1, 30, "S", "")}
    if source == "descendant":
        rows[20] = (1, 20, "S", START)
    install_census(monkeypatch, rows, marked={30} if source == "marker" else set())
    monkeypatch.setattr(procs, "identity", lambda pid: pytest.fail("snapshot writer must not be erased by a later read"))
    result = contain({10: recorded(10), 20: recorded(20)})
    assert 30 in result.live_pids
    assert result.unverifiable and result.errors
    assert not result.verified_empty
    assert 30 not in result.identities
    if source == "marker":
        assert result.marker_pids == {30}


@dataclass
class Scenario:
    rows: dict
    identities: dict
    marked: set
    wrong_root: set
    boot: str


@st.composite
def process_forests(draw):
    """Generate process trees, reincarnated roots/groups, and marked escapes."""
    pids = [10, 20, *range(30, 30 + draw(st.integers(0, 15)))]
    boot = draw(st.sampled_from([BOOT, OTHER_BOOT]))
    identities = {10: recorded(10), 20: recorded(20)}
    rows = {}
    for index, pid in enumerate(pids):
        if draw(st.booleans()):
            identities[pid] = recorded(pid, boot=draw(st.sampled_from([BOOT, OTHER_BOOT])))
        incarnation = draw(st.sampled_from(["absent", "original", "original", "reused", "zombie"]))
        if incarnation == "absent":
            continue
        parent = draw(st.sampled_from([1, *pids[:index]]))
        group = draw(st.sampled_from([10, pid, 900]))
        rows[pid] = (parent, group, "Z" if incarnation == "zombie" else "S",
                     REUSED_START if incarnation == "reused" else START)
    live = {pid for pid, row in rows.items() if not row[2].startswith("Z")}
    marked = draw(st.sets(st.sampled_from(sorted(live)), max_size=len(live))) if live else set()
    unmarked = live - marked
    wrong_root = draw(st.sets(st.sampled_from(sorted(unmarked)), max_size=len(unmarked))) if unmarked else set()
    return Scenario(rows, identities, marked, wrong_root, boot)


def expected_sources(case):
    """Ownership oracle expressed as each pid's path to an authenticated root."""
    live = {pid for pid, row in case.rows.items() if not row[2].startswith("Z")}
    mismatched = {pid for pid in case.rows.keys() & case.identities.keys()
                  if (case.identities[pid].boot_id, case.identities[pid].proc_start)
                  != (case.boot, case.rows[pid][3])}
    roots = {pid for pid in (10, 20) if pid in case.rows and pid not in mismatched}
    descendants = set()
    for pid in live - mismatched:
        ancestor, visited = pid, set()
        while ancestor in case.rows and ancestor not in visited:
            if ancestor in mismatched:
                break
            if ancestor in roots:
                descendants.add(pid)
                break
            visited.add(ancestor)
            ancestor = case.rows[ancestor][0]
    group_valid = (case.identities[10].boot_id == case.boot
                   and (10 not in case.rows or 10 not in mismatched))
    groups = {pid for pid in live - mismatched if case.rows[pid][1] == 10} if group_valid else set()
    return groups, descendants, mismatched


PROPERTY_SETTINGS = settings(max_examples=250, deadline=None,
                             suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                    HealthCheck.too_slow])


@PROPERTY_SETTINGS
@given(case=process_forests())
def test_i1_i2_i3_i5_generated_census_obeys_identity_ancestry_and_markers(monkeypatch, case):
    groups, descendants, mismatched = expected_sources(case)
    install_census(monkeypatch, case.rows, marked=case.marked, wrong_root=case.wrong_root, boot=case.boot)
    result = contain(case.identities)
    assert not result.unverifiable
    assert not ((result.live_pids - result.marker_pids) & mismatched)  # I1
    assert result.marker_pids == case.marked                          # I2
    assert result.descendant_pids == descendants                      # I3
    assert result.group_pids == groups
    assert result.live_pids == groups | descendants | case.marked
    # I5 includes evidence ordering: reverse table and marker input order and
    # require byte-for-byte equal serialized evidence, not merely set equality.
    install_census(monkeypatch, case.rows, marked=case.marked, wrong_root=case.wrong_root,
                   boot=case.boot, reverse=True)
    repeated = contain(dict(reversed(list(case.identities.items()))))
    assert json.dumps(result.to_dict()) == json.dumps(repeated.to_dict())


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(case=process_forests(), failure=st.sampled_from(["snapshot", "markers", "boot"]))
def test_i4_i5_generated_inspection_failures_stay_unverified_and_deterministic(monkeypatch, case, failure):
    # Ensure boot inspection is necessary regardless of the generated forest;
    # the existing inspection can fail without erasing this marked writer (I2).
    case.rows[999] = (1, 999, "S", START)
    case.marked.add(999)
    install_census(monkeypatch, case.rows, marked=case.marked, boot=case.boot, failure=failure)
    if failure == "snapshot":
        monkeypatch.setattr(procs, "_stat", lambda pid: "S")
        monkeypatch.setattr(procs, "identity", lambda pid: recorded(pid, boot=case.boot,
                                                                    started=case.rows[pid][3]))
    result = contain(case.identities)
    assert result.unverifiable and result.errors                       # I4
    assert not result.verified_empty
    if failure != "markers":
        assert case.marked <= result.marker_pids                       # I2
    install_census(monkeypatch, case.rows, marked=case.marked, boot=case.boot,
                   failure=failure, reverse=True)
    assert json.dumps(result.to_dict()) == json.dumps(contain(case.identities).to_dict())  # I5
