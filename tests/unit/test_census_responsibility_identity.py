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


def recycled_root(patch, *, known_unique, after_snapshot, pid=42, sequence=50, gap=1, children=1):
    """The reviewer's S and C: no attempt markers, and S's recorded start differs.
    Omit both from ps to put the reuse between the snapshot and the kernel listing."""
    holder = sequence + gap
    pids = range(pid, pid + children + 1)
    rows = "" if after_snapshot else "".join(
        f"{current} {1 if current == pid else pid} {pid} S another start\n" for current in pids)
    census(patch, parents=rows, session=BOOT_A)
    fake = kernel_with(patch, processes={current: b"" for current in pids},
                       responsible={current: pid for current in pids},
                       unique={current: holder + current - pid for current in pids})
    recorded = {pid: procs.ProcessIdentity(pid, BOOT_A, START)}
    root = procs.ResponsibleRoot(pid, BOOT_A, sequence if known_unique else None)
    return recorded, root, fake


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


@pytest.mark.parametrize("known_unique", [True, False], ids=["known-id", "missing-id"])
@pytest.mark.parametrize("after_snapshot", [False, True], ids=["before-ps", "after-ps"])
@settings.get_profile("ci")
@given(pid=st.integers(100, 10000), sequence=st.integers(1, 1000000),
       gap=st.integers(1, 10000), children=st.integers(1, 8))
def test_a_reused_responsibility_root_never_counts_or_saves_strangers(
        monkeypatch, known_unique, after_snapshot, pid, sequence, gap, children):
    """C-5.3, C-5.5: recorded unique id present/None × reuse before/after ps."""
    with monkeypatch.context() as patch:
        recorded, root, _ = recycled_root(patch, known_unique=known_unique,
            after_snapshot=after_snapshot, pid=pid, sequence=sequence, gap=gap, children=children)
        row = attempt_row(responsible_root={"pid": pid, "unique_id": root.unique_id})
        row.update(guardian_pid=pid, pgid=pid, child_pid=None)
        core = core_for(row)
        result = Daemon._contain(core, row)
        assert not result.live_pids and not result.identities
        assert "responsible_identities" not in json.loads(row["evidence_json"])
        assert not [event for event in core.store.events if event[0] == "attempt.responsible_recorded"]
        if after_snapshot and not known_unique:
            assert result.unverifiable and not result.verified_empty
        else:
            assert result.verified_empty


@pytest.mark.parametrize("shape", ["absent", "zombie", "missing-start", "missing-record"])
def test_an_unknown_sequence_root_needs_a_live_recorded_start(monkeypatch, shape):
    """C-5.5: an unidentifiable root supplies no responsibility members or reads."""
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
