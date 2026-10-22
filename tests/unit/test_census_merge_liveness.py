"""D-KILL3 / D-WALL1: one durable observation per distinct shared table."""

import json

from subfleet import daemon as daemon_module, procs
from tests.unit.test_daemon_settle import daemon, attempt, ATTEMPT, STARTED  # noqa: F401


def table(*, command=True):
    rows = {4242: (1, 4242, "S", STARTED), 4243: (4242, 4242, "S", STARTED)}
    if command:
        rows[4400] = (4243, 4400, "S", STARTED)
    return procs.ProcessTable(rows, "boot")


def test_repeated_snapshot_does_not_advance_liveness_or_write_after_clock_moves(daemon, monkeypatch):
    now = ["2026-10-22T11:00:00.000Z"]
    monkeypatch.setattr(daemon_module, "utcnow_ms", lambda: now[0])
    first = table()
    daemon._record_owned(attempt(daemon), first)
    evidence = json.loads(attempt(daemon)["evidence_json"])
    assert evidence["last_observed_alive_at"] == now[0]
    assert "4400" in evidence["counted_identities"]
    generation = daemon.store.generation
    now[0] = "2026-10-22T11:00:01.000Z"
    daemon._record_owned(attempt(daemon), first)
    assert daemon.store.generation == generation
    assert json.loads(attempt(daemon)["evidence_json"]) == evidence
    daemon._record_owned(attempt(daemon), table())
    assert json.loads(attempt(daemon)["evidence_json"])["last_observed_alive_at"] == now[0]
    assert daemon.store.generation == generation + 1


def test_liveness_updates_preserve_two_table_expiry_across_restart(daemon, monkeypatch):
    now = ["2026-10-22T11:00:00.000Z"]
    monkeypatch.setattr(daemon_module, "utcnow_ms", lambda: now[0])
    daemon._record_owned(attempt(daemon), table())
    now[0] = "2026-10-22T11:00:01.000Z"
    first_absence = table(command=False)
    daemon._record_owned(attempt(daemon), first_absence)
    evidence = json.loads(attempt(daemon)["evidence_json"])
    assert "4400" in evidence["counted_identities"]
    assert evidence["counted_absent"]["4400"] == evidence["counted_identities"]["4400"]
    now[0] = "2026-10-22T11:00:02.000Z"
    daemon._counted_tables.clear()  # A restarted daemon receives a fresh table.
    daemon._record_owned(attempt(daemon), table(command=False))
    evidence = json.loads(attempt(daemon)["evidence_json"])
    assert not evidence["counted_identities"]
    assert "counted_absent" not in evidence
    assert evidence["last_observed_alive_at"] == now[0]
