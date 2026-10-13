"""Round-18 combined census rules preserve holds without granting authority."""
from dataclasses import asdict

import pytest

from subfleet import procs
from subfleet.daemon import _retain_lineage
from tests.unit.test_census_recycled_pid import install
from tests.unit.test_procs import BOOT_A


@pytest.mark.parametrize("current_start", ["old-start", "new-start", ""])
@pytest.mark.parametrize("group_live", [False, True])
def test_unknown_boot_start_comparison_preserves_independent_group_hold(
        monkeypatch, current_start, group_live):
    root = procs.CensusRoot(42, "", "old-start", 200)
    rows = {42: (1, 300, "S", current_start)}
    if group_live:
        rows.update({43: (1, 200, "S", "member"),
                     44: (43, 400, "S", "detached-child")})
    install(monkeypatch, rows)
    census = procs.containment(None, None, None, "a1", recorded={42: root.identity},
                               lineage_roots=(root,))
    reused = current_start == "new-start"
    assert census.verified_empty is (reused and not group_live), census.to_dict()
    assert (root.identity in census.resolved_identities) is reused
    assert (root in census.resolved_lineage_roots) is (reused and not group_live)
    if group_live:
        assert {43, 44} <= census.live_pids, census.to_dict()
    if reused:
        assert 42 not in census.live_pids
        assert census.recycled[42]["reason"] == "different start identity"
    else:
        assert 42 in census.live_pids and census.unverifiable
        assert not census.recycled
    # Census death proof does not make an incomplete identity signallable.
    assert not procs.same_process(42, root.boot_id, root.proc_start)
    assert not census.group_identities


def test_persisting_sampled_groups_keeps_an_unchanged_census_stable(monkeypatch):
    install(monkeypatch, {43: (1, 200, "S", "writer"),
                         44: (43, 400, "S", "detached-child")})
    recorded = {43: procs.ProcessIdentity(43, BOOT_A, "writer")}
    first = procs.containment(None, None, None, "a1", recorded=recorded)
    second = procs.containment(None, None, None, "a1", recorded=recorded,
                                lineage_roots=first.lineage_roots)
    assert first.live_pids == second.live_pids == {43, 44}
    assert not first.verified_empty and not second.verified_empty
    assert first.to_dict() == second.to_dict()
    assert not first.group_identities and not second.group_identities


def test_sparse_quarantine_census_keeps_existing_lineage():
    root = asdict(procs.CensusRoot(42, BOOT_A, "writer", 200))
    evidence = {"lineage_roots": [root], "lineage_boot_ids": [BOOT_A]}
    retained = _retain_lineage(evidence, {"verified_empty": False})
    assert retained == evidence
    assert evidence["lineage_roots"] == [root]
