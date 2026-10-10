"""Review 4: live turn cwd guards use the live-state index."""
from subfleet import retention
from tests.unit.retention_world import World, trust_temporary_directories


def test_live_turn_workdir_guard_uses_an_index(tmp_path, monkeypatch):
    """Explain the actual census and fence SQL, rather than a copied query."""
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    try:
        w.job("retired")
        for state in ("queued", "running", "waiting"):
            w.store.add_job(job_id=state, request_id=state, payload_digest="d", kind="turn",
                            state=state, workdir=str(w.repo), prompt_path="/prompt", sandbox="read-only")
        statements = []

        def trace(sql):
            if sql.startswith("SELECT workdir FROM jobs WHERE kind='turn'"):
                statements.append((sql, w.store._holds_writer()))

        w.store.conn.set_trace_callback(trace)
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        w.store.conn.set_trace_callback(None)
        assert result["pruned"] == ["retired"], result
        assert len(statements) >= 2 and any(writer for _, writer in statements), statements
        for sql, _ in statements:
            plan = w.store.query("EXPLAIN QUERY PLAN " + sql)
            assert all("SCAN jobs" not in row["detail"] for row in plan), (sql, plan)
            assert any("jobs_state" in row["detail"] for row in plan), (sql, plan)
    finally:
        w.close()

