"""C-5.3, C-5.5: missing identities retain protection at every census seam."""

import pytest

from subfleet import procs
from tests.unit.test_census_recycled_pid import install, start
from tests.unit.test_procs import BOOT_A


@pytest.mark.parametrize("boot,began", [(None, None), (BOOT_A, None), (None, start())])
def test_incomplete_recorded_identity_is_unknown(monkeypatch, boot, began):
    install(monkeypatch, {42: (1, 42, "Ss", start())})
    assert procs.liveness(42, boot, began) == "unknown"
    assert not procs.same_process(42, boot, began)
    # Observed absence proves death independently of the missing record.
    monkeypatch.setattr(procs, "identity", lambda _: None)
    assert procs.liveness(42, boot, began) == "dead"


def test_live_pid_without_readable_start_is_unknown(monkeypatch):
    monkeypatch.setattr(procs, "proc_start", lambda _: None)
    monkeypatch.setattr(procs, "_stat", lambda _: "Ss")
    with pytest.raises(procs.InspectionError):
        procs.identity(42)
    assert procs.liveness(42, BOOT_A, start()) == "unknown"


def test_missing_descendant_start_keeps_snapshot_unverifiable(monkeypatch):
    install(monkeypatch, {42: (1, 42, "Ss", start()), 43: (42, 43, "Ss", "")})
    result = procs.containment(42, 42, None, "job/a1", recorded_identities={
        42: procs.ProcessIdentity(42, BOOT_A, start())})
    assert result.live_pids == {42, 43}
    assert result.unverifiable and not result.verified_empty
    assert 43 not in result.identities  # a later read cannot repair this scan


def test_incomparable_orphan_record_keeps_writer_protection(monkeypatch):
    install(monkeypatch, {43: (1, 43, "Ss", start(1))})
    result = procs.containment(42, 42, None, "job/a1", recorded_identities={
        43: procs.ProcessIdentity(43, "", start(1))})
    assert result.descendant_pids == {43}
    assert result.unverifiable and not result.verified_empty and not result.recycled


def test_clock_step_forward_cannot_prove_unrecorded_root_reuse(monkeypatch):
    install(monkeypatch, {43: (1, 43, "Ss", start(1000))})
    result = procs.containment(42, 42, 43, "job/a1",
                               started_at="2026-10-11T01:00:00Z",
                               finished_at="2026-10-11T01:01:00Z")
    assert result.descendant_pids == {43}
    assert result.unverifiable and not result.verified_empty and not result.recycled
