"""Per-job authorization crosses the wire and store without gaining defaults."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

import pytest

from subfleet import protocol, store as store_module
from subfleet.contracts import JobSpec, Sandbox
from subfleet.store import SCHEMA_VERSION, Store


REASON = "Operator checked account usage at 09:00; authorize a same-model probe."
WIRE_BASE = dict(request_id="request", kind="dispatch", workdir="/work",
                 prompt_path="/prompt", sandbox="read-only")


def test_submit_reason_survives_wire_round_trip_and_old_clients_default_to_none():
    for reason in (None, REASON):
        args = protocol.SubmitArgs(**WIRE_BASE, unmeasured_reserve_reason=reason)
        request = protocol.Request("submit", asdict(args), id="request")
        decoded = protocol.decode_request(protocol.encode(request))
        rebuilt = protocol.coerce_args(protocol.SubmitArgs, decoded.args)
        assert rebuilt.unmeasured_reserve_reason == reason
    assert protocol.coerce_args(protocol.SubmitArgs, WIRE_BASE).unmeasured_reserve_reason is None


def test_job_spec_requires_no_authorization_by_default():
    base = {**WIRE_BASE, "sandbox": Sandbox.READ_ONLY, "task": None, "tier": None,
            "pinned_model": "fable", "pinned_lane": "claude-1", "out_path": None,
            "name": None}
    assert JobSpec(**base).unmeasured_reserve_reason is None
    assert JobSpec(**base, unmeasured_reserve_reason=REASON).unmeasured_reserve_reason == REASON


@pytest.fixture
def version_4_store(tmp_path):
    path = tmp_path / "state.sqlite3"
    schema = Path(store_module.__file__).with_name("store_schema.sql").read_text()
    old_jobs = (Path(__file__).parents[1] / "fixtures/store/version-4-jobs.sql").read_text()
    # Create jobs first from a frozen historical declaration. The current
    # schema's CREATE IF NOT EXISTS leaves it untouched and supplies other tables.
    with sqlite3.connect(path) as conn:
        conn.executescript(old_jobs + schema)
        conn.execute("INSERT INTO schema_version VALUES (4, '2026-09-19T00:00:00Z')")
        conn.execute("""INSERT INTO jobs
            (job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,
             created_at,pinned_model,pinned_lane,wait_reason)
            VALUES ('old','old-request','digest','dispatch','waiting','/work','/prompt',
                    'read-only','2026-09-19T00:00:00Z','fable','claude-1','reserve unknown')""")
    return path


def test_migration_preserves_old_jobs_without_authorizing_them(version_4_store):
    with sqlite3.connect(version_4_store) as conn:
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute("SELECT * FROM jobs WHERE job_id='old'").fetchone())
    with Store(version_4_store) as store:
        after = store.get_job("old")
        assert {key: after[key] for key in before} == before
        assert after["unmeasured_reserve_reason"] is None
        assert after["mcp_servers"] == "[]"
        assert set(after) - set(before) == {"unmeasured_reserve_reason", "mcp_servers"}
        store.add_job(job_id="authorized", request_id="new-request", payload_digest="new",
                      kind="dispatch", workdir="/work", prompt_path="/prompt", sandbox="read-only",
                      pinned_model="fable", pinned_lane="claude-1", unmeasured_reserve_reason=REASON)
    with Store(version_4_store) as store:
        assert store.get_job("authorized")["unmeasured_reserve_reason"] == REASON
        assert store.get_job("old")["unmeasured_reserve_reason"] is None
        assert [row["version"] for row in store.query("SELECT * FROM schema_version ORDER BY version")] == list(range(4, SCHEMA_VERSION + 1))
        assert [json.loads(row["data_json"]) for row in store.list_events()
                if row["kind"] == "schema.migrated"] == [{"version": step} for step in range(5, SCHEMA_VERSION + 1)]


def test_readonly_old_store_does_not_migrate_or_invent_authorization(version_4_store):
    with Store(version_4_store, read_only=True) as store:
        assert "unmeasured_reserve_reason" not in store.get_job("old")
        assert store.one("SELECT MAX(version) AS version FROM schema_version")["version"] == 4
    with sqlite3.connect(version_4_store) as conn:
        assert "unmeasured_reserve_reason" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}


def test_fresh_store_defaults_authorization_to_null_and_matches_migrated_shape(tmp_path, version_4_store):
    with Store(tmp_path / "fresh.sqlite3") as fresh, Store(version_4_store) as migrated:
        fresh.add_job(job_id="default", request_id="default", payload_digest="default", kind="dispatch",
                      workdir="/work", prompt_path="/prompt", sandbox="read-only")
        assert fresh.get_job("default")["unmeasured_reserve_reason"] is None
        assert fresh.one("SELECT MAX(version) AS version FROM schema_version")["version"] == SCHEMA_VERSION
        assert {row["name"] for row in fresh.query("PRAGMA table_info(jobs)")} == {
            row["name"] for row in migrated.query("PRAGMA table_info(jobs)")}


def test_quarantine_migration_keeps_old_jobs_unauthorized(version_4_store):
    """C-3.1: the quarantine schema step changes attempts, never job consent."""
    with Store(version_4_store) as store:
        old = store.get_job("old")
        assert old["unmeasured_reserve_reason"] is None
        assert old["mcp_servers"] == "[]"
        assert old["state"] == "waiting" and old["wait_reason"] == "reserve unknown"
        assert store.one("SELECT MAX(version) v FROM schema_version")["v"] == SCHEMA_VERSION
