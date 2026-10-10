"""D-ST2: retained attempt evidence must not grow operator reads or replies."""

from datetime import datetime, timezone

from subfleet import cli, protocol, render
from subfleet.daemon import STATUS_ATTEMPTS, STATUS_JOBS, after
from tools.measure_status_payload import seed_store
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


def test_status_and_lane_hold_stay_small_and_indexed_with_large_history(routing_state, monkeypatch):
    service, _ = routing_state
    seed_store(service.store)
    evidence = service.store.one("SELECT sum(length(evidence_json)) n FROM attempts")["n"]
    assert evidence > 64 * 1024 * 1024, "the fixture must exceed the client's line limit"
    def reject_full_read(*args, **kwargs):
        raise AssertionError("read the full attempt ledger")
    monkeypatch.setattr(service.store, "list_attempts", reject_full_read)
    issued = []
    query = service.store.query

    def watched(sql, params=()):
        issued.append((sql, params))
        return query(sql, params)
    monkeypatch.setattr(service.store, "query", watched)

    for op, args in (("daemon.status", {}),
                     ("lanes", {"action": "hold", "lane_id": "codex-1", "until": after(3600)})):
        issued.clear()
        result = service.dispatch(op, args)
        assert len(protocol.encode(protocol.Response("guard", True, result=result))) < 4 * 1024 * 1024
        statements = [sql for sql, _ in issued]
        assert STATUS_ATTEMPTS in statements and STATUS_JOBS in statements
        assert not any(sql.startswith("SELECT * FROM attempts") or
                       sql == "SELECT * FROM jobs ORDER BY created_at,rowid" for sql in statements)
        for sql in (STATUS_ATTEMPTS, STATUS_JOBS):
            plan = " / ".join(row["detail"] for row in query("EXPLAIN QUERY PLAN " + sql))
            assert "attempts_live" in plan and "attempts_quarantined" in plan, plan
            assert "SCAN attempts" not in plan and "SCAN jobs" not in plan, plan
            if sql == STATUS_JOBS:
                assert "jobs_state" in plan and "sqlite_autoindex_jobs_1" in plan, plan
        lane = result["lanes"][0]
        assert lane["in_flight"] == 31 and lane["in_flight_turns"] == 1
        if op == "daemon.status":
            assert result["active_attempts"] == 32
            assert len(result["attempts"]) == 33
            assert {row["state"] for row in result["attempts"]} == {
                "reserved", "starting", "running", "finalizing", "quarantined"}
            assert all(set(row) == {"attempt_id", "job_id", "seq", "lane_id", "model_requested",
                                    "state", "reserved_at"} for row in result["attempts"])
            assert len(result["jobs"]) == 35
            assert {"queued", "waiting", "quarantine"} <= {row["job_id"] for row in result["jobs"]}
            assert "Running jobs" in result["status"] and "Waiting jobs" in result["status"]
            assert "Conversation turns" in result["status"]
            assert "running jobs: 33" in cli.format_status(result)
        else:
            assert result["held"] == "codex-1"
            assert any(row["reason"] == "operator-hold" for row in result["closures"])


def test_bounded_operator_rows_preserve_both_status_renderers(routing_state):
    service, _ = routing_state
    seed_store(service.store, finished=8, evidence_bytes=100, live=4)
    with service.store.snapshot():
        rows = service._capacity_rows()
        legacy = {**rows, "view": {**rows["view"], "attempts": service.store.list_attempts(),
                                   "jobs": service.store.query("SELECT * FROM jobs ORDER BY created_at,rowid")}}
    now = datetime.now(timezone.utc)
    before = service._capacity_view(rows=legacy, now=now, desktop_in_use=False)
    after_view = service._capacity_view(rows=rows, now=now, desktop_in_use=False)
    assert render.status(after_view) == render.status(before)
    assert cli.format_status(after_view) == cli.format_status(before)
    assert after_view["in_flight"] == before["in_flight"]
    assert after_view["in_flight_turns"] == before["in_flight_turns"]
    # Every carried job keeps its original fields, including the terminal job
    # whose quarantined attempt must remain visible.
    old_jobs = {row["job_id"]: row for row in before["jobs"]}
    assert all(row == old_jobs[row["job_id"]] for row in after_view["jobs"])
