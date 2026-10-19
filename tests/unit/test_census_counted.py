"""D-KILL2, C-5.5: every process a census counts is recorded and seeds the next census.

A walk member is reparented to init when its parent dies, and a process whose argument
buffer shows no environment (an Apple platform binary running with the hardened runtime,
such as `/usr/bin/lockf`, on macOS 26) has no marker to be found by. The installed daemon
(c69757256) recorded neither, so the lockf a cancelled attempt's provider had started in a
session of its own (incidents 2026-10-16 and 2026-10-18) could be counted while its parent
lived and lost when it died. Release/217's `Daemon._contain` records the processes a
census saw naming the root (`responsible_identities`); it now records every other member
too (`counted_identities`), as evidence that seeds later walks and never as authority.
"""

from __future__ import annotations

from dataclasses import asdict
import json

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.unit.test_census_procargs import attempt_row, captured, core_for
from tests.unit.test_procs import BOOT_A, START

GUARDIAN = 42                                # attempt_row's guardian and pgid
REBORN = "Sat Sep  5 10:00:09 2026"


def ident(pid: int, start: str = START) -> procs.ProcessIdentity:
    return procs.ProcessIdentity(pid, BOOT_A, start)


def test_d_kill2_every_member_any_source_counts_is_recorded_and_seeds_the_next(monkeypatch):
    """The group's and walk's members and a marked process are kept beside the member
    seen naming the root; the guardian is not (its own identity is the attempt's). The
    next census gets them all as recorded survivors, and nothing becomes owned."""
    walk, marked, member = ident(61), ident(62), ident(60)
    seen = procs.Containment(group_pids=frozenset({GUARDIAN}), descendant_pids=frozenset({GUARDIAN, 61}),
                             marker_pids=frozenset({62}), responsible_pids=frozenset({60}),
                             identities={GUARDIAN: ident(GUARDIAN), 60: member, 61: walk, 62: marked})
    row = attempt_row(responsible_root={"pid": GUARDIAN, "unique_id": 77})
    core = core_for(row)
    calls = captured(monkeypatch, seen)
    a = dict(row)
    Daemon._contain(core, a)
    evidence = json.loads(row["evidence_json"])
    assert evidence["counted_identities"] == {"61": asdict(walk), "62": asdict(marked)}
    assert evidence["responsible_identities"] == {"60": asdict(member)}
    assert "owned_identities" not in evidence                     # evidence, never authority
    assert json.loads(a["evidence_json"]) == evidence              # the caller's row too
    assert core.store.events == [("attempt.responsible_recorded", {"pids": [60]}),
                                 ("attempt.counted_recorded", {"pids": [61, 62]})]
    Daemon._contain(core, dict(row))
    assert {pid: calls[-1]["recorded_identities"][pid] for pid in (60, 61, 62)} == {
        60: member, 61: walk, 62: marked}
    assert len(core.store.events) == 2                            # nothing new: nothing written


def test_d_kill2_an_owned_member_is_not_recorded_again_but_a_new_birth_is(monkeypatch):
    """A group member the attempt already owns under the same identity costs no write. A
    new process later counted at that pid is recorded, and both births seed the walk
    (C-5.5's alternatives): the owned one keeps its place, the new one is the alternative."""
    owned, reborn = ident(61), ident(61, REBORN)
    row = attempt_row(owned_identities={"61": asdict(owned)})
    core = core_for(row)
    captured(monkeypatch, procs.Containment(group_pids=frozenset({61}), identities={61: owned}))
    Daemon._contain(core, dict(row))
    assert core.store.events == [] and "counted_identities" not in json.loads(row["evidence_json"])
    captured(monkeypatch, procs.Containment(descendant_pids=frozenset({61}), identities={61: reborn}))
    Daemon._contain(core, dict(row))
    assert json.loads(row["evidence_json"])["counted_identities"] == {"61": asdict(reborn)}
    calls = captured(monkeypatch)
    Daemon._contain(core, dict(row))
    assert calls[-1]["recorded_identities"][61] == owned
    assert calls[-1]["recorded_alternatives"][61] == [reborn]


def test_d_kill2_a_member_without_an_identity_is_not_recorded(monkeypatch):
    """Only a captured identity can be recorded; the census that could not capture one
    is unverifiable and holds the attempt itself (C-5.5)."""
    row = attempt_row()
    core = core_for(row)
    captured(monkeypatch, procs.Containment(descendant_pids=frozenset({61}), unverifiable=True,
                                            errors=("identity inspection unavailable for pid 61",)))
    Daemon._contain(core, dict(row))
    assert core.store.events == [] and "counted_identities" not in json.loads(row["evidence_json"])


def test_d_kill2_a_probe_record_keeps_its_counted_members_in_the_record(monkeypatch):
    walk = ident(61)
    captured(monkeypatch, procs.Containment(descendant_pids=frozenset({61}), identities={61: walk}))
    record = {"holder": "probe:x", "job_id": "job", "lane_id": "codex-1", "directory": "/fixture/probe",
              "guardian_pid": GUARDIAN, "pgid": GUARDIAN, "boot_id": BOOT_A, "proc_start": START,
              "owned_identities": {}}
    core = core_for(attempt_row())
    Daemon._contain(core, record)
    assert record["counted_identities"] == {"61": asdict(walk)}
    assert core.store.events == []                                 # saved with the record by its caller
    calls = captured(monkeypatch)
    Daemon._contain(core, record)
    assert calls[-1]["recorded_identities"][61] == walk


PIDS = st.sets(st.integers(50, 70), max_size=8)


@settings(max_examples=40, derandomize=True, deadline=None, database=None)
@given(groups=PIDS, walk=PIDS, marked=PIDS, named=PIDS, missing=PIDS)
def test_d_kill2_property_every_counted_member_is_kept_once_and_seeds_the_next(groups, walk, marked,
                                                                              named, missing):
    """For every census result: after `_contain`, each live member other than the guardian
    whose identity was captured is in exactly one of `responsible_identities` (seen naming
    the root) and `counted_identities` (every other source), under that identity; the
    next census receives each as a recorded survivor; nothing is added to
    `owned_identities`; and a second identical census writes nothing."""
    identities = {pid: ident(pid) for pid in groups | walk | marked | named | {GUARDIAN} if pid not in missing}
    seen = procs.Containment(group_pids=frozenset(groups | {GUARDIAN}), descendant_pids=frozenset(walk),
                             marker_pids=frozenset(marked), responsible_pids=frozenset(named),
                             identities=identities)
    row = attempt_row(responsible_root={"pid": GUARDIAN, "unique_id": 77})
    core = core_for(row)
    with pytest.MonkeyPatch.context() as patch:
        calls = captured(patch, seen)
        Daemon._contain(core, dict(row))
        written = len(core.store.events)
        Daemon._contain(core, dict(row))
    evidence = json.loads(row["evidence_json"])
    responsible = evidence.get("responsible_identities", {})
    counted = evidence.get("counted_identities", {})
    expected = {str(pid) for pid in seen.live_pids if pid != GUARDIAN and pid in identities}
    assert set(responsible) | set(counted) == expected
    assert set(responsible).isdisjoint(counted)
    assert set(responsible) == {str(pid) for pid in named if pid in identities}
    assert all(value == asdict(identities[int(pid)]) for pid, value in {**responsible, **counted}.items())
    assert "owned_identities" not in evidence
    assert all(calls[-1]["recorded_identities"][int(pid)] == identities[int(pid)] for pid in expected)
    assert len(core.store.events) == written
