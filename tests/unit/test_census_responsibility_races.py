"""C-5.5, review r3: retain observed children without borrowing reused identities."""

import calendar
from dataclasses import asdict
import json
import struct
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.unit.test_census_procargs import ATTEMPT, ROOT, attempt_row, core_for, kernel_with
from tests.unit.test_census_responsibility_identity import BOOT_A, START
from tests.unit.test_procs import census


TIMINGS = ["before-recheck", "before-members", "mid-scan", "after-members", "after-final"]


def changing_root(patch, *, timing, fate, known_unique=False, pid=42, sequence=98,
                  gap=1, children=1):
    """R, an old listed stranger slot, and post-snapshot escaped children.

    Death removes each child's responsibility link. Replacement also creates C
    in the already-listed slot; C's unique id follows the successor's. The
    snapshot never supplies an escapee's identity or a parent/group link to R.
    """
    stranger = pid + 18
    real = set(range(pid + 19, pid + 19 + children))
    successor = sequence + children + gap
    census(patch, session=BOOT_A)
    fake = kernel_with(patch, processes={p: b"" for p in {pid, stranger, *real}},
                       unique={pid: sequence, stranger: sequence - 1,
                               **{p: sequence + i + 1 for i, p in enumerate(sorted(real))}},
                       responsible={p: pid for p in {pid, *real}})
    starts = {p: START for p in fake.processes}
    starts[stranger] = "old unrelated start"
    state = SimpleNamespace(changed=False, scanning=True, root_reads=0, observed=set(),
                            fake=fake, real=real, stranger=stranger, successor=successor,
                            starts=starts)

    def change():
        if state.changed or fate == "alive":
            return
        state.changed = True
        fake.boss.update({p: p for p in real})
        if fate == "reaped":
            fake.processes.pop(pid, None)
            starts.pop(pid, None)
        else:
            fake.unique[pid] = successor if fate != "successor-no-id" else None
            starts[pid] = "successor start"
            fake.unique[stranger] = successor + 1
            starts[stranger] = "new stranger start"
            fake.boss[stranger] = pid
            if fate == "unreadable":
                fake.fail.add(("process", pid))

    def snapshot():
        rows = {pid: (1, pid, "S", START), stranger: (1, stranger, "S", "old unrelated start")}
        if not state.scanning:
            rows = {p: (1, p, "S", starts[p]) for p in fake.processes}
        seen = procs.ProcessTable(rows, BOOT_A)
        if state.scanning and timing == "before-recheck":
            change()
        return seen

    read_process, list_pids, responsible = fake.process, fake.pids, fake.responsible
    read_sources = procs._kernel_sources

    def process(current):
        found = read_process(current)
        found = (SimpleNamespace(pid=current, exiting=found.exiting,
                                 unique_id=found.unique_id, proc_start=starts.get(current))
                 if found is not None else None)
        if state.scanning and current == pid:
            state.root_reads += 1
            if state.root_reads == 2 and timing == "after-final":
                change()
        return found

    def pids():
        if state.scanning and timing == "before-members":
            change()
        return list_pids()

    def boss(current):
        found = responsible(current)
        if current in real and found == pid:
            state.observed.add(current)
        if state.scanning and timing == "mid-scan" and current == pid:
            change()
        return found

    def sources(*args):
        found = read_sources(*args)
        if state.scanning and timing == "after-members":
            change()
        return found

    patch.setattr(procs, "snapshot", snapshot)
    patch.setattr(procs, "proc_start", starts.get)
    patch.setattr(fake, "process", process)
    patch.setattr(fake, "pids", pids)
    patch.setattr(fake, "responsible", boss)
    patch.setattr(procs, "_kernel_sources", sources)
    row = attempt_row(responsible_root={"pid": pid, "unique_id": sequence if known_unique else None})
    row.update(guardian_pid=pid, pgid=pid, child_pid=None)
    return row, core_for(row), state


@pytest.mark.parametrize("fate", ["reaped", "successor"], ids=["input-A", "input-B"])
def test_a_child_read_before_root_death_is_saved_and_never_released(monkeypatch, fate):
    row, core, state = changing_root(monkeypatch, timing="after-members", fate=fate)
    first = Daemon._contain(core, row)
    assert first.responsible_pids >= {61}
    assert json.loads(row["evidence_json"])["responsible_identities"]["61"]["proc_start"] == START
    assert first.unverifiable and not first.verified_empty
    state.scanning = False
    second = Daemon._contain(core, row)
    assert 61 in second.live_pids and not second.verified_empty
    if fate == "reaped":
        state.fake.processes[42] = b""
        state.fake.unique[42] = 200
        state.starts[42] = "third holder start"
    third = Daemon._contain(core, row)
    assert 42 in third.recycled
    assert 61 in third.live_pids and not third.verified_empty
    assert json.loads(row["evidence_json"])["responsible_identities"]["61"]["proc_start"] == START


def test_a_known_id_root_replaced_mid_scan_cannot_save_a_successors_child(monkeypatch):
    row, core, _ = changing_root(monkeypatch, timing="mid-scan", fate="successor", known_unique=True)
    result = Daemon._contain(core, row)
    assert 60 not in result.responsible_pids
    assert "60" not in json.loads(row["evidence_json"]).get("responsible_identities", {})


@pytest.mark.parametrize("timing", ["before-members", "mid-scan"])
def test_a_late_successor_cannot_supply_responsibility_walk_seeds(monkeypatch, timing):
    """Review r2's exact S=42/100 and C=60/101, for the final-read mutation."""
    row, core, state = changing_root(monkeypatch, timing=timing, fate="successor")
    result = Daemon._contain(core, row)
    assert state.fake.unique[42] == 100 and state.fake.unique[60] == 101
    assert 60 not in result.responsible_pids
    assert "60" not in json.loads(row["evidence_json"]).get("responsible_identities", {})
    assert result.unverifiable and not result.verified_empty


def test_a_known_dead_root_without_members_can_verify_empty(monkeypatch):
    row, core, state = changing_root(monkeypatch, timing="never", fate="alive", known_unique=True)
    state.scanning = False
    for pid in {42, *state.real}:
        state.fake.processes.pop(pid)
        state.starts.pop(pid)
    result = Daemon._contain(core, row)
    assert result.verified_empty and not result.errors


@pytest.mark.parametrize("known_unique", [False, True], ids=["missing-id", "known-id"])
def test_a_member_reused_before_identity_capture_cannot_save_the_new_holder(monkeypatch, known_unique):
    row, core, state = changing_root(monkeypatch, timing="never", fate="alive", known_unique=known_unique)
    sources = procs._kernel_sources

    def replace_member(*args):
        found = sources(*args)
        state.starts[61] = "unrelated Y start"
        state.fake.unique[61] = 500
        state.fake.boss[61] = 61
        return found

    monkeypatch.setattr(procs, "_kernel_sources", replace_member)
    Daemon._contain(core, row)
    saved = json.loads(row["evidence_json"]).get("responsible_identities", {}).get("61")
    assert saved == asdict(procs.ProcessIdentity(61, BOOT_A, START))
    state.scanning = False
    later = Daemon._contain(core, row)
    assert 61 in later.recycled and 61 not in later.live_pids


@pytest.mark.parametrize("fate", ["unreadable", "successor-no-id"])
@pytest.mark.parametrize("known_unique", [False, True])
def test_an_unreadable_final_holder_preserves_members_as_evidence(monkeypatch, fate, known_unique):
    row, core, _ = changing_root(monkeypatch, timing="after-members", fate=fate, known_unique=known_unique)
    result = Daemon._contain(core, row)
    assert 61 in result.responsible_pids
    assert "61" in json.loads(row["evidence_json"])["responsible_identities"]
    assert "responsibility root identity unavailable for pid 42" in result.errors
    assert result.unverifiable and not result.verified_empty


@pytest.mark.parametrize("known_unique", [False, True])
def test_a_member_without_a_unique_id_survives_the_successor_ceiling(monkeypatch, known_unique):
    row, core, state = changing_root(monkeypatch, timing="after-members", fate="successor",
                                     known_unique=known_unique)
    state.fake.unique[61] = None
    result = Daemon._contain(core, row)
    assert 61 in result.responsible_pids
    assert "61" in json.loads(row["evidence_json"])["responsible_identities"]


@pytest.mark.parametrize("known_unique", [False, True], ids=["missing-id", "known-id"])
@pytest.mark.parametrize("timing", TIMINGS)
@settings.get_profile("ci")
@given(pid=st.integers(100, 10000), sequence=st.integers(2, 1000000),
       gap=st.integers(1, 10000), children=st.integers(1, 8))
def test_birth_order_and_observed_children_survive_every_root_transition(
        monkeypatch, known_unique, timing, pid, sequence, gap, children):
    for fate in ("alive", "successor", "reaped"):
        with monkeypatch.context() as patch:
            row, core, state = changing_root(patch, timing=timing, fate=fate, known_unique=known_unique,
                                             pid=pid, sequence=sequence, gap=gap, children=children)
            first = Daemon._contain(core, row)
            assert state.changed == (fate != "alive")
            saved = json.loads(row["evidence_json"]).get("responsible_identities", {})
            assert str(state.stranger) not in saved
            assert state.stranger not in first.responsible_pids
            assert state.observed <= first.responsible_pids
            assert {str(p) for p in state.observed} <= saved.keys()
            if state.observed:
                assert not first.verified_empty
            state.scanning = False
            later = Daemon._contain(core, row)
            assert state.observed <= later.live_pids
            if state.observed:
                assert not later.verified_empty


def test_kernel_identity_start_is_captured_with_the_unique_id(monkeypatch):
    """The BSD record's birth seconds render exactly as C-locale, UTC ps lstart."""
    reader = procs.Darwin()
    reader.readable = True
    reads = []

    def pidinfo(pid, flavor, argument, buffer, size):
        reads.append((pid, flavor))
        struct.pack_into("=IIII", buffer, 0, 0, 2, 0, pid)
        struct.pack_into("=Q", buffer, 120, calendar.timegm((2026, 9, 5, 10, 0, 0)))
        struct.pack_into("=Q", buffer, procs._UNIQUEID_OFFSET, 99)
        return size

    reader._pidinfo = pidinfo
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    found = reader.process(61)
    assert found.proc_start == START
    assert found.unique_id == 99
    assert reads == [(61, procs._PROC_PIDT_BSDINFOWITHUNIQID)]
