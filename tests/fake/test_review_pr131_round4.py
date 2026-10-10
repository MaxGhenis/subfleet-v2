"""Round-four safety probes. Scripted OS interleavings, real census/resolvers."""
import json

import pytest

from subfleet import procs
from tests.fake.test_quarantine_detached_writers import running
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_review_pr131_probes import BOOT, ORIGINAL_CENSUS, ident, quarantine, script_table
from tests.fake.test_review_pr131_round3 import resolve
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["cwd", "marker"])
@pytest.mark.parametrize("old_state", ["S", "Z"])
def test_later_source_must_not_use_a_reused_pids_old_table_incarnation(
        state_daemon, monkeypatch, operator, source, old_state):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    # The read-only fixture otherwise has only a lane lease, which quarantine
    # releases. Give the lease-retention assertion an actual retained lease.
    daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
    phase = [0]
    # An unrelated old incarnation exists only in the first, earlier table.
    # By the later cwd/marker scan it was reaped, and PID 400 is a new writer.
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(
        {400: (1, 700 if phase[0] == 0 else 800,
               old_state if phase[0] == 0 else "S",
               "old-unrelated" if phase[0] == 0 else "new-writer")}, boot_id=BOOT))
    monkeypatch.setattr(procs, "containment", ORIGINAL_CENSUS)
    monkeypatch.setattr(procs, "_read", lambda argv, **kwargs:
        f"400 writer SUBFLEET_ATTEMPT={a['attempt_id']}\n"
        if source == "marker" and phase[0] == 0 else "")
    monkeypatch.setattr(procs, "_stat", lambda pid: "S")
    monkeypatch.setattr(procs, "identity", lambda pid: procs.ProcessIdentity(pid, BOOT, "new-writer"))
    monkeypatch.setattr(procs, "process_group", lambda pid: 800)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir:
        frozenset({400}) if source == "cwd" and phase[0] == 0 else frozenset())
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    evidence = json.loads(actual["evidence_json"])
    print("FIRST", source, old_state, operator, actual["state"], evidence.get("lineage_roots"))
    assert actual["state"] == "quarantined" and leases, "a later scan observed a live writer"
    roots = [root for root in evidence["lineage_roots"] if root["pid"] == 400]
    assert roots == [{"pid": 400, "boot_id": BOOT, "proc_start": "new-writer", "pgid": 800}], roots
    # The observed writer then leaves matching cwd and hides its markers.
    # Its real identity/group stay live, so observed-once retention must hold.
    phase[0] = 1
    clock.advance()
    actual, leases = resolve(daemon, actual, operator)
    print("SECOND", source, old_state, operator, actual["state"], bool(leases))
    assert actual["state"] == "quarantined" and leases, "an observed writer must remain a root"


@pytest.mark.parametrize("operator", [False, True])
def test_paced_inspection_keeps_a_listed_descendant_with_missing_start(
        state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = running(daemon, harness)
    adir = daemon.root / "jobs" / a["job_id"] / "a1"
    start = json.loads((adir / "start.json").read_text())
    start.update(child_pid=150, child_identity=ident(150, "provider"))
    (adir / "start.json").write_text(json.dumps(start))
    table = procs.ProcessTable({100: (1, 100, "Ss", "guardian"),
                               150: (100, 100, "S", "provider"),
                               200: (150, 200, "Ss", "")}, boot_id=BOOT)
    daemon._record_owned(a, table)
    saved = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    print("INSPECTION", saved.get("lineage_roots"))
    remaining = {200: (1, 201, "S", "shell")}
    script_table(monkeypatch, remaining, markers_fail=True)
    census = daemon._contain(a)
    assert not census.verified_empty
    daemon._quarantine(a, census, "review: marker inspection failed")
    script_table(monkeypatch, remaining)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    print("RESOLUTION", operator, actual["state"], bool(leases))
    assert actual["state"] == "quarantined" and leases, "a failed identity capture must retain the listed writer"
