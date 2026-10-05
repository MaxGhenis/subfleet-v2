"""C-3.1/C-5.7: schema 6 quarantine rows become due without losing leases."""
import sqlite3
from pathlib import Path

from subfleet.store import Store


def test_schema_6_quarantine_pace_migrates_additively_and_reopens(tmp_path):
    path = tmp_path / "state.sqlite3"
    schema = Path("subfleet/store_schema.sql").read_text()
    schema = "\n".join(line for line in schema.splitlines()
                       if "quarantine_recheck_at" not in line and "quarantine_notice_pending" not in line
                       and "attempts_quarantine_notice" not in line)
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute("INSERT INTO schema_version VALUES (6,'2026-10-04T00:00:00Z')")
        db.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,created_at,updated_at) "
                   "VALUES('codex-1','codex','fixture','fixture','home','old','old')")
        db.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,created_at) "
                   "VALUES('old','old','fixture','dispatch','lost','/fixture','fixture','read-only','old')")
        db.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,quarantine_reason,reserved_at) "
                   "VALUES('old/a1','old',1,'codex-1','astra','quarantined','held','old')")
        db.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('worktree:/fixture','old/a1','old')")
    with Store(path) as store:
        a = store.get_attempt("old/a1")
        assert a["quarantine_recheck_at"] == "" and a["quarantine_notice_pending"] == 0
        assert a["quarantine_reason"] == "held" and a["state"] == "quarantined"
        assert len(store.list_leases()) == 1
        store.update_attempt("old/a1", quarantine_recheck_at="2026-10-05T12:10:00.000000Z", quarantine_notice_pending=1)
    with Store(path) as store:
        assert store.get_attempt("old/a1")["quarantine_recheck_at"] == "2026-10-05T12:10:00.000000Z"
        assert store.get_attempt("old/a1")["quarantine_notice_pending"] == 1
        assert len(store.list_leases()) == 1
