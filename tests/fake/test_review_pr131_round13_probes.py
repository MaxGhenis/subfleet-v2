"""Round-13 ownership and dead-root budgets also apply to probe kills."""
import dataclasses
import json

from subfleet import procs
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.fake.test_review_pr131_round13 import _record_signals


def record_for(daemon, harness):
    job_id, attempt, _ = reserve(daemon, harness)
    return attempt, {"holder": "probe:" + job_id, "job_id": job_id,
                     "lane_id": attempt["lane_id"], "directory": str(harness.workdir),
                     "guardian_pid": None, "pgid": None, "boot_id": BOOT,
                     "proc_start": "guardian", "state": "running"}


def test_r13_finding_probe_persists_removal_of_dead_empty_group_roots(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, record = record_for(daemon, harness)
    record["lineage_roots"] = [dataclasses.asdict(procs.CensusRoot(202, BOOT, "writer", 202))]
    script_table(monkeypatch, {})
    assert daemon._probe_census(record).verified_empty
    assert not record.get("lineage_roots")
    assert not daemon._probe_record(record["holder"]).get("lineage_roots")


def test_r13_finding_probe_outage_preserves_terminal_collision_suppression(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a, record = record_for(daemon, harness)
    known = {"pid": 202, "boot_id": BOOT, "proc_start": "writer"}
    daemon.store.update_attempt(a["attempt_id"], state="succeeded",
                                evidence_json=json.dumps({"owned_identities": {"202": known}}))
    record["owned_identities"] = {"202": known}
    script_table(monkeypatch, {202: (1, 202, "S", "writer")}, table_fails=True)
    monkeypatch.setattr(procs, "same_process", lambda *args: True)
    sent = _record_signals(monkeypatch)
    daemon.term_grace_s = 0
    assert not daemon._contain_probe(record)
    assert not sent


def test_r13_finding_probe_kill_evaluates_foreign_ownership_once(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a, record = record_for(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], state="succeeded")
    record.update(guardian_pid=100, pgid=100)
    script_table(monkeypatch, {100: (1, 100, "Ss", "guardian"), 101: (100, 100, "S", "provider")})
    monkeypatch.setattr(procs, "same_process", lambda *args: True)
    _record_signals(monkeypatch)
    original = daemon._foreign_ownership
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(daemon, "_foreign_ownership", counted)
    daemon.term_grace_s = 0
    assert not daemon._contain_probe(record)
    assert len(calls) == 1
