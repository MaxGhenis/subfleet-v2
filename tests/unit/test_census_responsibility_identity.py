"""C-5.3, C-5.5: responsibility roots cannot lend a reused pid to strangers."""

import json

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.unit.test_census_procargs import ATTEMPT, ROOT, attempt_row, core_for, kernel_with
from tests.unit.test_procs import BOOT_A, START, census


settings.register_profile("ci", max_examples=40, derandomize=True, database=None, deadline=None,
                          suppress_health_check=[HealthCheck.function_scoped_fixture])


def recycled_root(patch, *, known_unique, after_snapshot, matching_snapshot=False,
                  pid=42, sequence=50, gap=1, children=1, first_child=None):
    """The reviewer's S and C: no attempt markers, and S's recorded start differs.
    Reuse after ps can follow either an empty table or a matching original root."""
    holder = sequence + gap
    first_child = first_child if first_child is not None else pid + 1
    pids = [pid, *range(first_child, first_child + children)]
    rows = f"{pid} 1 {pid} S {START}\n" if matching_snapshot else "" if after_snapshot else "".join(
        f"{current} {1 if current == pid else pid} {pid} S another start\n" for current in pids)
    census(patch, parents=rows, session=BOOT_A)
    if matching_snapshot:
        read_snapshot = procs.snapshot

        def snapshot_before_reuse():
            seen = read_snapshot()
            patch.setattr(procs, "proc_start", lambda current: f"another start {gap}")
            return seen

        patch.setattr(procs, "snapshot", snapshot_before_reuse)
    fake = kernel_with(patch, processes={current: b"" for current in pids},
                       responsible={current: pid for current in pids},
                       unique={current: holder + offset for offset, current in enumerate(pids)})
    recorded = {pid: procs.ProcessIdentity(pid, BOOT_A, START)}
    root = procs.ResponsibleRoot(pid, BOOT_A, sequence if known_unique else None)
    return recorded, root, fake


def root_during_member_reads(patch, *, timing, replace=True, late_start="another start",
                            pid=42, sequence=98, gap=1, children=1, first_child=60):
    """A matching snapshot, with reuse before/between/after the member reads.

    Until replacement, both the start and kernel unique ids belong to R and
    its children. Afterwards S and its children hold those pids, without markers.
    """
    census(patch, parents=f"{pid} 1 {pid} S {START}\n", session=BOOT_A)
    pids = [pid, *range(first_child, first_child + children)]
    fake = kernel_with(patch, processes={current: b"" for current in pids},
                       responsible={current: pid for current in pids},
                       unique={current: sequence + offset for offset, current in enumerate(pids)})
    replaced = False
    reads = []

    def reuse():
        nonlocal replaced
        if replace:
            replaced = True
            holder = sequence + children + gap
            fake.unique.update({current: holder + offset for offset, current in enumerate(pids)})

    read_snapshot, list_pids, read_sources = procs.snapshot, fake.pids, procs._kernel_sources
    read_start = procs.proc_start

    def snapshot():
        seen = read_snapshot()
        reads.append("snapshot")
        if timing == "before-recheck":
            reuse()
        return seen

    def pids_after_recheck():
        if timing == "before-members":
            reuse()
        return list_pids()

    def sources(*args):
        found = read_sources(*args)
        reads.append("members")
        if timing == "after-members":
            reuse()
        return found

    def start(current):
        if current != pid:
            return read_start(current)
        reads.append("root-start")
        value = late_start if replaced else START
        if isinstance(value, Exception):
            raise value
        return value

    patch.setattr(procs, "snapshot", snapshot)
    patch.setattr(fake, "pids", pids_after_recheck)
    patch.setattr(procs, "_kernel_sources", sources)
    patch.setattr(procs, "proc_start", start)
    recorded = {pid: procs.ProcessIdentity(pid, BOOT_A, START)}
    return recorded, procs.ResponsibleRoot(pid, BOOT_A, None), fake, reads


@pytest.mark.parametrize("timing", ["before-members", "after-members"])
@pytest.mark.parametrize("late_start", ["another start", None, procs.InspectionError("start unreadable")],
                         ids=["replaced", "gone", "unreadable"])
def test_a_root_replaced_after_the_recheck_cannot_supply_members(monkeypatch, timing, late_start):
    """Review r2: validate again after member reads, including absent/failed reads."""
    recorded, root, fake, reads = root_during_member_reads(
        monkeypatch, timing=timing, late_start=late_start)
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
                               recorded_identities=recorded, responsible_root=root)
    assert not result.responsible_pids
    assert 60 not in result.live_pids and 60 not in result.identities
    assert result.errors == ("responsibility root identity unavailable for pid 42",)
    assert result.unverifiable and not result.verified_empty
    assert reads == ["snapshot", "root-start", "members", "root-start"]
    assert [call for call in fake.calls if call[0] == "responsible"] == [
        ("responsible", 42), ("responsible", 60)]


@pytest.mark.parametrize("timing", ["before-members", "after-members"])
@pytest.mark.parametrize("late_start", ["another start", None, procs.InspectionError("start unreadable")],
                         ids=["replaced", "gone", "unreadable"])
def test_a_root_replaced_after_the_recheck_cannot_save_walk_seeds(monkeypatch, timing, late_start):
    """Review r2: the provisional members must not become durable evidence."""
    root_during_member_reads(monkeypatch, timing=timing, late_start=late_start)
    row = attempt_row(responsible_root={"pid": 42, "unique_id": None})
    row["child_pid"] = None
    core = core_for(row)
    result = Daemon._contain(core, row)
    assert not result.responsible_pids
    assert 60 not in result.live_pids and 60 not in result.identities
    assert result.errors == ("responsibility root identity unavailable for pid 42",)
    assert result.unverifiable and not result.verified_empty
    assert "responsible_identities" not in json.loads(row["evidence_json"])
    assert not [event for event in core.store.events if event[0] == "attempt.responsible_recorded"]


def test_a_root_without_a_unique_id_cannot_count_a_holder_born_after_ps(monkeypatch):
    """C-5.5, review P3-A: S and C born after ps contribute nothing, with a reason."""
    recorded, root, fake = recycled_root(monkeypatch, known_unique=False, after_snapshot=True)
    result = procs.containment(42, 42, None, ATTEMPT, root=ROOT,
                               recorded_identities=recorded, responsible_root=root)
    assert not result.live_pids and not result.identities
    assert result.errors == ("responsibility root identity unavailable for pid 42",)
    assert result.unverifiable and not result.verified_empty
    assert not [call for call in fake.calls if call[0] == "responsible"]


def test_a_root_without_a_unique_id_cannot_save_a_late_strangers_child(monkeypatch):
    """C-5.5, review P3-A: C must never become a durable descendant-walk seed."""
    recycled_root(monkeypatch, known_unique=False, after_snapshot=True)
    row = attempt_row(responsible_root={"pid": 42, "unique_id": None})
    row["child_pid"] = None
    core = core_for(row)
    result = Daemon._contain(core, row)
    assert not result.responsible_pids
    assert "responsible_identities" not in json.loads(row["evidence_json"])
    assert not [event for event in core.store.events if event[0] == "attempt.responsible_recorded"]


def test_a_matching_snapshot_cannot_lend_a_replaced_roots_pid_to_strangers(monkeypatch):
    """Review r1: ps sees the original 42; current S=42/100 and C=60/101 are strangers."""
    recorded, root, fake = recycled_root(monkeypatch, known_unique=False, after_snapshot=True,
        matching_snapshot=True, first_child=60, sequence=99)
    result = procs.containment(42, 42, None, ATTEMPT, root=ROOT,
                               recorded_identities=recorded, responsible_root=root)
    assert not result.responsible_pids
    assert 60 not in result.live_pids and 60 not in result.identities
    assert result.errors == ("responsibility root identity unavailable for pid 42",)
    assert result.unverifiable and not result.verified_empty
    assert not [call for call in fake.calls if call[0] == "responsible"]


def test_a_matching_snapshot_cannot_save_a_replacement_roots_child(monkeypatch):
    """Review r1: the late stranger C never becomes a durable descendant-walk seed."""
    recycled_root(monkeypatch, known_unique=False, after_snapshot=True,
                  matching_snapshot=True, first_child=60, sequence=99)
    row = attempt_row(responsible_root={"pid": 42, "unique_id": None})
    row["child_pid"] = None
    core = core_for(row)
    result = Daemon._contain(core, row)
    assert not result.responsible_pids
    assert 60 not in result.live_pids and 60 not in result.identities
    assert result.unverifiable and not result.verified_empty
    assert "responsible_identities" not in json.loads(row["evidence_json"])
    assert not [event for event in core.store.events if event[0] == "attempt.responsible_recorded"]


@pytest.mark.parametrize("known_unique", [True, False], ids=["known-id", "missing-id"])
@pytest.mark.parametrize("after_snapshot,matching_snapshot", [(False, False), (True, False), (True, True)],
                         ids=["before-ps", "after-absent-ps", "after-matching-ps"])
@settings.get_profile("ci")
@given(pid=st.integers(100, 10000), sequence=st.integers(1, 1000000),
       gap=st.integers(1, 10000), children=st.integers(1, 8))
def test_a_reused_responsibility_root_never_counts_or_saves_strangers(
        monkeypatch, known_unique, after_snapshot, matching_snapshot, pid, sequence, gap, children):
    """C-5.3, C-5.5: known/unknown unique id × reuse before/after absent/matching ps."""
    with monkeypatch.context() as patch:
        _, root, _ = recycled_root(patch, known_unique=known_unique,
            after_snapshot=after_snapshot, matching_snapshot=matching_snapshot,
            pid=pid, sequence=sequence, gap=gap, children=children)
        row = attempt_row(responsible_root={"pid": pid, "unique_id": root.unique_id})
        row.update(guardian_pid=pid, pgid=pid, child_pid=None)
        core = core_for(row)
        result = Daemon._contain(core, row)
        assert not result.responsible_pids
        assert result.live_pids <= ({pid} if matching_snapshot else set())
        assert set(result.identities) <= ({pid} if matching_snapshot else set())
        assert "responsible_identities" not in json.loads(row["evidence_json"])
        assert not [event for event in core.store.events if event[0] == "attempt.responsible_recorded"]
        if after_snapshot and not known_unique:
            assert result.unverifiable and not result.verified_empty
        elif not matching_snapshot:
            assert result.verified_empty


@pytest.mark.parametrize("timing", ["before-recheck", "before-members", "after-members"])
@settings.get_profile("ci")
@given(pid=st.integers(100, 10000), sequence=st.integers(1, 1000000),
       gap=st.integers(1, 10000), children=st.integers(1, 8))
def test_root_reuse_at_every_read_boundary_excludes_strangers_and_keeps_real_children(
        monkeypatch, timing, pid, sequence, gap, children):
    """C-5.5: all three reuse timings plus a still-holding root for every input."""
    for replace in (True, False):
        with monkeypatch.context() as patch:
            _, root, fake, reads = root_during_member_reads(patch, timing=timing, replace=replace,
                pid=pid, sequence=sequence, gap=gap, children=children, first_child=pid + 1)
            row = attempt_row(responsible_root={"pid": pid, "unique_id": root.unique_id})
            row.update(guardian_pid=pid, pgid=pid, child_pid=None)
            core = core_for(row)
            result = Daemon._contain(core, row)
            evidence = json.loads(row["evidence_json"])
            events = [event for event in core.store.events if event[0] == "attempt.responsible_recorded"]
            if replace:
                assert not result.responsible_pids
                assert result.live_pids <= {pid} and set(result.identities) <= {pid}
                assert "responsible_identities" not in evidence and not events
                assert result.errors == (f"responsibility root identity unavailable for pid {pid}",)
                assert result.unverifiable and not result.verified_empty
                if timing == "before-recheck":
                    assert reads.count("root-start") == 1
                    assert not [call for call in fake.calls if call[0] == "responsible"]
                else:
                    assert reads == ["snapshot", "root-start", "members", "root-start"]
            else:
                real_children = set(range(pid + 1, pid + 1 + children))
                assert result.responsible_pids == {pid, *real_children}
                assert not result.errors and not result.unverifiable
                assert set(evidence["responsible_identities"]) == {str(child) for child in real_children}
                assert events == [("attempt.responsible_recorded", {"pids": sorted(real_children)})]
                assert reads == ["snapshot", "root-start", "members", "root-start"]


@pytest.mark.parametrize("current_start", [START, None, procs.InspectionError("start unreadable")],
                         ids=["still-matches", "gone", "unreadable"])
def test_a_matching_snapshot_needs_a_current_root_start(monkeypatch, current_start):
    """A fresh match keeps all members; an absent or failed start read blocks release."""
    census(monkeypatch, parents=f"42 1 42 S {START}\n", session=BOOT_A)
    fake = kernel_with(monkeypatch, processes={42: b"", 60: b""},
                       responsible={42: 42, 60: 42}, unique={42: 100, 60: 1})
    reads = []
    read_snapshot = procs.snapshot

    def snapshot():
        reads.append("snapshot")
        return read_snapshot()

    def start(pid):
        reads.append(pid)
        if isinstance(current_start, Exception):
            raise current_start
        return current_start

    monkeypatch.setattr(procs, "snapshot", snapshot)
    monkeypatch.setattr(procs, "proc_start", start)
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
        recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, START)},
        responsible_root=procs.ResponsibleRoot(42, BOOT_A, None))
    if current_start == START:
        assert reads == ["snapshot", 42, 42]  # C-5.12: starts bracket the member reads
        assert result.responsible_pids == {42, 60}
        assert not result.errors and not result.unverifiable
    else:
        assert reads == ["snapshot", 42]      # no late read for a root rejected early
        assert not result.responsible_pids
        assert result.errors == ("responsibility root identity unavailable for pid 42",)
        assert result.unverifiable and not result.verified_empty
        assert not [call for call in fake.calls if call[0] == "responsible"]


@pytest.mark.parametrize("shape", ["absent", "zombie", "missing-start", "missing-record"])
def test_an_unknown_sequence_root_needs_a_live_recorded_start(monkeypatch, shape):
    """C-5.5: an unidentifiable root supplies no responsibility members or reads."""
    census(monkeypatch, session=BOOT_A)
    row = {42: (1, 42, "Z" if shape == "zombie" else "S",
                "" if shape == "missing-start" else START)} if shape != "absent" else {}
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(row, BOOT_A))
    fake = kernel_with(monkeypatch, processes={60: b""}, responsible={60: 42})
    recorded = {} if shape == "missing-record" else {42: procs.ProcessIdentity(42, BOOT_A, START)}
    result = procs.containment(None, None, None, ATTEMPT, root=ROOT,
        recorded_identities=recorded, responsible_root=procs.ResponsibleRoot(42, BOOT_A, None))
    assert not result.responsible_pids and not result.identities
    assert "responsibility root identity unavailable for pid 42" in result.errors
    assert result.unverifiable and not result.verified_empty
    assert not [call for call in fake.calls if call[0] == "responsible"]
