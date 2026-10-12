"""C-5.6/C-5.7: overlapping attempt claims hold and never authorize signals."""

import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import procs, protocol
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import BOOT, NEXT_BOOT, script_table
from tests.fake.test_state_contract import state_daemon  # noqa: F401

REAL_SAME_PROCESS = procs.same_process


@pytest.mark.parametrize("foreign_state", ["running", "quarantined"])
def test_late_child_receipt_suppresses_a_cached_kill(state_daemon, monkeypatch, foreign_state):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    other, directory = running(daemon, harness, 100, 101)
    daemon.store.update_attempt(other["attempt_id"], state=foreign_state, evidence_json="{}")
    (directory / "start.json").write_text("{}")
    script_table(monkeypatch, {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201")}, markers="")
    foreign = daemon._foreign_ownership(ours, procs.snapshot())
    target = procs.ProcessIdentity(200, BOOT, "p200")
    census = procs.Containment(identities={200: target}, group_pids=frozenset({200}))
    assert daemon._unambiguous_signal_identity(ours["attempt_id"], target, census, {}, foreign)
    (directory / "start.json").write_text(json.dumps({"child_identity": {
        "pid": 200, "boot_id": BOOT, "proc_start": "p200"}}))
    assert not daemon._unambiguous_signal_identity(ours["attempt_id"], target, census, {}, foreign)
    assert not daemon._unambiguous_signal_group(ours["attempt_id"], target, census, {}, foreign)


@pytest.mark.parametrize("foreign_state", ["running", "finalizing", "quarantined", "succeeded"])
@pytest.mark.parametrize("table_outage", [False, True])
def test_late_child_receipt_blocks_signals_after_startup_import(
        state_daemon, monkeypatch, foreign_state, table_outage):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    other, directory = running(daemon, harness, 100, 101)
    rows = {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201")}
    script_table(monkeypatch, rows, markers="")
    daemon._record_owned(ours, procs.snapshot())
    daemon.store.update_attempt(other["attempt_id"], evidence_json="{}")
    (directory / "start.json").write_text("{}")
    assert not daemon._foreign_ownership(ours, procs.snapshot()).claimed(
        procs.ProcessIdentity(200, BOOT, "p200"))
    child = {"pid": 200, "boot_id": BOOT, "proc_start": "p200"}
    (directory / "start.json").write_text(json.dumps({"child_identity": child}))
    daemon.store.update_attempt(other["attempt_id"], state=foreign_state)
    if table_outage:
        def unavailable():
            raise procs.InspectionError("scripted table outage")
        monkeypatch.setattr(procs, "snapshot", unavailable)
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: rows[pid][1])
    delivered = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: delivered.append(pid))
    monkeypatch.setattr(procs.os, "killpg", lambda pgid, sig:
                        delivered.extend(pid for pid, row in rows.items() if row[1] == pgid))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    assert 200 not in delivered
    evidence = json.loads(daemon.store.get_attempt(other["attempt_id"])["evidence_json"])
    assert evidence["child_identity"] == child
    assert not daemon.store.one("SELECT 1 FROM process_claim_receipt_pending WHERE attempt_id=?",
                                (other["attempt_id"],))
    monkeypatch.setattr(daemon, "_read_json", lambda path: pytest.fail("indexed receipt read again"))
    assert daemon._foreign_ownership(ours, None).claimed(procs.ProcessIdentity(**child))


@pytest.mark.parametrize("foreign_claim", [False, True])
def test_partial_table_keeps_group_protocol_only_without_competing_claims(
        state_daemon, monkeypatch, foreign_claim):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    rows = {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", "p202")}
    script_table(monkeypatch, rows, markers="")
    daemon._record_owned(ours, procs.snapshot())
    if foreign_claim:
        other, _ = running(daemon, harness, 100, 101)
        daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({
            "owned_identity_history": {"202": [{"pid": 202, "boot_id": BOOT, "proc_start": "p202"}]},
        }))
    # Scalar reads remain complete; only the table loses the guardian's start.
    partial = {**rows, 200: (1, 200, "S", "")}
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(partial, boot_id=BOOT))
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: rows[pid][1])
    delivered, groups = [], []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: delivered.append(pid))
    def group_signal(pgid, sig):
        groups.append(int(sig))
        delivered.extend(pid for pid, row in rows.items() if row[1] == pgid)
    monkeypatch.setattr(procs.os, "killpg", group_signal)
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    assert 201 in delivered
    if foreign_claim:
        assert not groups and 202 not in delivered
    else:
        assert groups == [15, 9] and 202 in delivered


def test_ambiguous_guardian_does_not_authenticate_new_ownership(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    running(daemon, harness, 200, 101)
    ours, _ = running(daemon, harness, 200, 201)
    script_table(monkeypatch, {200: (1, 200, "S", "p200"), 202: (200, 200, "S", "p202")})
    daemon._record_owned(ours, procs.snapshot())
    for _ in range(2):
        census = daemon._contain(ours)
        evidence = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
        assert {200, 202} <= census.live_pids
        assert procs.ProcessIdentity(200, BOOT, "p200") in census.ambiguous_identities
        assert not evidence.get("owned_identities")
        assert not evidence.get("provider_identities")
        assert not evidence.get("descendant_identities")


def test_ambiguous_foreign_anchor_cannot_exclude_a_late_local_group(
        state_daemon, monkeypatch):
    daemon, harness = state_daemon
    running(daemon, harness, 202, 203)
    ours, _ = running(daemon, harness, 200, 201)
    # Recycled local writer 202 briefly created group 202, forked 300, and
    # returned to group 200. The stale foreign guardian's identity collides.
    rows = {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", "p202"), 300: (202, 202, "S", "p300")}
    script_table(monkeypatch, rows, markers="")
    table = procs.ProcessTable({pid: row for pid, row in rows.items() if pid != 300}, boot_id=BOOT)
    monkeypatch.setattr(procs, "snapshot", lambda: table)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({300}))
    daemon._record_owned(ours, table)
    census = daemon._contain(ours)
    assert 300 in census.cwd_pids
    assert procs.ProcessIdentity(300, BOOT, "p300") not in census.excluded_identities
    evidence = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    assert "300" not in evidence.get("owned_identities", {})
    assert any(root["pid"] == 300 for root in evidence["lineage_roots"])


def test_own_marker_collision_before_first_record_is_not_hidden(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    other, _ = running(daemon, harness, 100, 101)
    ours, _ = running(daemon, harness, 200, 201)
    known = {"pid": 202, "boot_id": BOOT, "proc_start": "p202"}
    daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({"owned_identity_history": {"202": [known]}}))
    script_table(monkeypatch, {202: (1, 202, "S", "p202")},
                 markers=f"202 writer SUBFLEET_ATTEMPT={ours['attempt_id']} SUBFLEET_ROOT={daemon.root}\n")
    census = daemon._contain(ours)
    current = procs.ProcessIdentity(**known)
    assert census.marker_pids == {202}
    assert current in census.ambiguous_identities
    assert current not in census.excluded_identities
    evidence = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    assert "202" not in evidence.get("owned_identities", {})
    assert known in evidence["ambiguous_identities"]


def test_own_marker_holds_on_fresh_foreign_group_proof_without_a_table_row(
        state_daemon, monkeypatch):
    daemon, harness = state_daemon
    running(daemon, harness, 300, 301)
    ours, _ = running(daemon, harness, 200, 201)
    rows = {300: (1, 300, "S", "p300"), 301: (300, 300, "S", "p301"),
            202: (301, 300, "S", "p202")}
    script_table(monkeypatch, rows, markers=f"202 writer SUBFLEET_ATTEMPT={ours['attempt_id']}\n")
    # The marker and scalar reads see the writer after the empty table read.
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable({}, boot_id=BOOT))
    census = daemon._contain(ours)
    assert 202 in census.marker_pids
    assert procs.ProcessIdentity(202, BOOT, "p202") in census.ambiguous_identities
    evidence = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    assert "202" not in evidence.get("owned_identities", {})


@pytest.mark.parametrize("launch_kind", ["guardian", "child"])
def test_group_collision_before_first_record_holds_after_guardian_exit(
        state_daemon, monkeypatch, launch_kind):
    daemon, harness = state_daemon
    if launch_kind == "guardian":
        running(daemon, harness, 202, 203)
    else:
        running(daemon, harness, 100, 202)
    ours, _ = running(daemon, harness, 200, 201)
    daemon.store.acquire_lease("native:" + ours["attempt_id"], ours["attempt_id"])
    rows = {202: (1, 200, "S", "p202")}
    script_table(monkeypatch, rows, markers="")
    census = daemon._contain(ours)
    assert 202 in census.live_pids
    assert procs.ProcessIdentity(202, BOOT, "p202") in census.ambiguous_identities
    deliveries = []
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "kill", lambda *args: deliveries.append(args))
    monkeypatch.setattr(procs.os, "killpg", lambda *args: deliveries.append(args))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    after = daemon.store.get_attempt(ours["attempt_id"])
    assert after["state"] == "quarantined"
    assert "202" not in json.loads(after["evidence_json"]).get("owned_identities", {})
    assert any(row["lease_key"] == "native:" + ours["attempt_id"] for row in daemon.store.list_leases())
    assert not deliveries


def test_probe_cannot_signal_an_identity_with_an_attempt_claim(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    other, _ = running(daemon, harness, 100, 101)
    ours, _ = running(daemon, harness, 200, 201)
    known = {"pid": 202, "boot_id": BOOT, "proc_start": "p202"}
    daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({"owned_identities": {"202": known}}))
    record = {"holder": "probe:independent", "job_id": ours["job_id"], "lane_id": ours["lane_id"],
              "directory": str(harness.workdir), "guardian_pid": 500, "pgid": 500,
              "boot_id": BOOT, "proc_start": "p500", "state": "running",
              "owned_identities": {"202": known}}
    script_table(monkeypatch, {202: (1, 202, "S", "p202")}, markers="")
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "kill", lambda *args: pytest.fail("probe signalled an ambiguous identity"))
    monkeypatch.setattr(procs.os, "killpg", lambda *args: pytest.fail("absent probe guardian authorized a signal"))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    assert not daemon._contain_probe(record)
    assert record["state"] == "quarantined"
    assert record["owned_identities"]["202"] == known


@pytest.mark.parametrize("legacy_boot", [False, True])
@pytest.mark.parametrize("table_outage", [False, True])
def test_foreign_group_proof_blocks_reuse_before_foreign_paced_record(
        state_daemon, monkeypatch, table_outage, legacy_boot):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    foreign, _ = running(daemon, harness, 300, 301)
    if legacy_boot:
        daemon.store.update_attempt(foreign["attempt_id"], boot_id="1700000000")
        monkeypatch.setattr(procs.boot_identity, "boot_seconds", lambda read: "1700000000")
    rows = {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", "p202")}
    script_table(monkeypatch, rows)
    daemon._record_owned(ours, procs.snapshot())
    rows.clear()
    rows.update({300: (1, 300, "S", "p300"), 301: (300, 300, "S", "p301"),
                 202: (301, 300, "S", "p202")})
    if table_outage:
        def unavailable():
            raise procs.InspectionError("scripted table outage")
        monkeypatch.setattr(procs, "snapshot", unavailable)
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "kill", lambda *args: pytest.fail("recycled foreign writer signalled"))
    monkeypatch.setattr(procs.os, "killpg", lambda *args: pytest.fail("absent local guardian authorized signal"))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    after = daemon.store.get_attempt(ours["attempt_id"])
    assert after["state"] == "quarantined"


def test_foreign_histories_with_matching_legacy_boot_cannot_exclude_a_cwd_writer(
        state_daemon, monkeypatch):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    modern, _ = running(daemon, harness, 300, 301)
    legacy, _ = running(daemon, harness, 400, 401)
    current = {"pid": 202, "boot_id": BOOT, "proc_start": "p202"}
    for attempt, boot in ((modern, BOOT), (legacy, "1700000000")):
        record = {**current, "boot_id": boot}
        daemon.store.update_attempt(attempt["attempt_id"], evidence_json=json.dumps({
            "owned_identity_history": {"202": [record]},
        }))
    monkeypatch.setattr(procs.boot_identity, "boot_seconds", lambda read: "1700000000")
    script_table(monkeypatch, {202: (1, 202, "S", "p202")}, markers="")
    monkeypatch.setattr(procs, "cwd_pids", lambda path: frozenset({202}))
    census = daemon._contain(ours)
    assert 202 in census.live_pids
    assert procs.ProcessIdentity(**current) in census.ambiguous_identities
    assert procs.ProcessIdentity(**current) not in census.excluded_identities
    after = daemon.store.get_attempt(ours["attempt_id"])
    assert "202" not in json.loads(after["evidence_json"]).get("owned_identities", {})
    assert {"pid": 202, "boot_id": BOOT, "proc_start": "p202"} in json.loads(after["evidence_json"])["ambiguous_identities"]


def test_ambiguity_survives_restart_and_removed_foreign_history(state_daemon, monkeypatch):
    from tests.fake.test_review_pr131_round11 import owned_writer

    daemon, ours, _, rows = owned_writer(state_daemon, monkeypatch)
    rows.clear()
    rows[202] = (1, 202, "S", "p202")
    daemon._quarantine(ours, daemon._contain(ours), "identity ambiguity")
    other = daemon.store.one("SELECT * FROM attempts WHERE attempt_id<>?", (ours["attempt_id"],))
    daemon.store.update_attempt(other["attempt_id"], state="succeeded", evidence_json="{}")
    daemon.close()
    restored = type(daemon)(daemon.root, term_grace_s=0, kill_settle_s=0)
    try:
        monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
        monkeypatch.setattr(procs.os, "kill", lambda *args: pytest.fail("restart restored ambiguous signal authority"))
        monkeypatch.setattr(procs.os, "killpg", lambda *args: pytest.fail("absent guardian authorized group signal"))
        held = restored.store.get_attempt(ours["attempt_id"])
        restored._kill_attempt(held)
        after = restored.store.get_attempt(ours["attempt_id"])
        assert after["state"] == "quarantined"
        assert "202" in json.loads(after["evidence_json"])["owned_identities"]
        rows.clear()
        restored._resolve_quarantine(after, protocol.KillArgs(ours["job_id"], confirm_dead=True))
        assert restored.store.get_attempt(ours["attempt_id"])["state"] != "quarantined"
        assert not any(row["lease_key"] == "native:" + ours["attempt_id"] for row in restored.store.list_leases())
    finally:
        restored.close()


@settings(max_examples=800, derandomize=True, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(claims=st.sets(st.integers(200, 205)),
       source=st.sampled_from(["owned_identities", "owned_identity_history",
                              "provider_identities", "descendant_identities",
                              "guardian", "child", "ambiguous_identities"]),
       foreign_state=st.sampled_from(["running", "finalizing", "quarantined", "succeeded"]),
       same_start=st.booleans(), same_boot=st.booleans(),
       guardian_live=st.booleans(), table_outage=st.booleans())
def test_no_signal_to_an_identity_claimed_by_another_attempt(
        state_daemon, monkeypatch, claims, source, foreign_state,
        same_start, same_boot, guardian_live, table_outage):
    daemon, harness = state_daemon
    daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
    attempts = getattr(daemon, "_signal_collision_attempts", None)
    if attempts is None:
        ours, _ = running(daemon, harness, 200, 201)
        other, directory = running(daemon, harness, 100, 101)
        attempts = daemon._signal_collision_attempts = (ours, other, directory)
    ours, other, directory = attempts
    for attempt in (ours, other):
        daemon.store.update_attempt(attempt["attempt_id"], state="running", evidence_json="{}",
                                    killed_by=None, quarantine_reason=None)
    daemon.store.update_attempt(other["attempt_id"], guardian_pid=100, pgid=100,
                                boot_id=BOOT, proc_start="p100")
    (directory / "start.json").write_text("{}")
    rows = {pid: (1 if pid == 200 else 200, 200, "S", f"p{pid}")
            for pid in range(200, 206)}
    script_table(monkeypatch, rows, markers="")
    daemon._record_owned(ours, procs.snapshot())
    before = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    records = [{"pid": pid, "boot_id": BOOT if same_boot else NEXT_BOOT,
                "proc_start": f"p{pid}" if same_start else f"old-p{pid}"}
               for pid in sorted(claims)]
    if source in {"guardian", "child"}:
        records = records[:1]
        if records:
            record = records[0]
            if source == "guardian":
                daemon.store.update_attempt(other["attempt_id"], guardian_pid=record["pid"],
                                            pgid=record["pid"], boot_id=record["boot_id"],
                                            proc_start=record["proc_start"])
            else:
                (directory / "start.json").write_text(json.dumps({"child_identity": record}))
        evidence = {}
    elif source == "owned_identities":
        evidence = {source: {str(record["pid"]): record for record in records}}
    elif source == "owned_identity_history":
        evidence = {source: {str(record["pid"]): [record] for record in records}}
    else:
        evidence = {source: records}
    daemon.store.update_attempt(other["attempt_id"], state=foreign_state,
                                evidence_json=json.dumps(evidence))
    if not guardian_live:
        rows.pop(200)
    if table_outage:
        def unavailable():
            raise procs.InspectionError("scripted table outage")
        monkeypatch.setattr(procs, "snapshot", unavailable)
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: rows[pid][1])
    delivered = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: delivered.append(pid))
    monkeypatch.setattr(procs.os, "killpg", lambda pgid, sig:
                        delivered.extend(pid for pid, row in rows.items() if row[1] == pgid))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    # This oracle comes directly from the generated per-attempt claims,
    # independently of the production ambiguity/exclusion implementation.
    ambiguous = {record["pid"] for record in records
                 if record["boot_id"] == BOOT and record["proc_start"] == f"p{record['pid']}"}
    assert ambiguous.isdisjoint(delivered), (ambiguous, delivered, records)
    assert set(delivered) <= rows.keys()
    after = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    assert all(after["owned_identities"][pid] == value
               for pid, value in before["owned_identities"].items())
    if not table_outage:
        assert set(range(201, 206)) - ambiguous <= set(delivered)
