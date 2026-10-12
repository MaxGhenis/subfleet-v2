"""Legacy receipt work survives both upgrade and an interrupted upgrade."""
import json
import sqlite3

import pytest

from subfleet.daemon import Daemon
from subfleet.store import Store


@pytest.mark.parametrize("interrupted", [False, True])
def test_legacy_receipt_queue_upgrade_recovers_its_claim(tmp_path, monkeypatch, interrupted):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        with store.transaction("fixture.legacy") as tx:
            for row in tx.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                  "AND name LIKE 'process_claim_receipt_%'").fetchall():
                tx.execute('DROP TRIGGER "' + row["name"] + '"')
            tx.execute("DROP TABLE process_claim_receipt_pending")
            tx.execute("DROP TABLE process_claim_receipts")
            tx.execute("CREATE TABLE process_claim_receipt_pending (attempt_id TEXT PRIMARY KEY "
                       "REFERENCES attempts(attempt_id) ON DELETE CASCADE) WITHOUT ROWID")
            tx.execute("CREATE TABLE process_claim_receipts (singleton INTEGER PRIMARY KEY, seeded INTEGER NOT NULL)")
            tx.execute("INSERT INTO process_claim_receipts VALUES(1,1)")
            tx.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,created_at,updated_at) "
                       "VALUES('codex-1','codex','fixture','fixture','home','old','old')")
            tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,created_at) "
                       "VALUES('old','old','fixture','dispatch','succeeded','/fixture','fixture','read-only','old')")
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                       "VALUES('old/a1','old',1,'codex-1','astra','succeeded','old')")
            tx.execute("INSERT INTO process_claim_receipt_pending VALUES('old/a1')")
            tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('native:legacy','old/a1','old')")
    receipt = tmp_path / "jobs/old/a1/start.json"
    receipt.parent.mkdir(parents=True)
    child = {"pid": 202, "boot_id": "legacy-boot", "proc_start": "legacy-start"}
    receipt.write_text(json.dumps({"child_identity": child}))
    if interrupted:
        real_connect = sqlite3.connect
        class InterruptedUpgrade(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("ALTER TABLE process_claim_receipts ADD COLUMN"):
                    raise sqlite3.OperationalError("interrupted receipt upgrade")
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            return real_connect(*args, **kwargs, factory=InterruptedUpgrade)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            with pytest.raises(sqlite3.OperationalError, match="interrupted receipt upgrade"):
                Store(path)
        with real_connect(path) as db:
            assert {row[1] for row in db.execute("PRAGMA table_info(process_claim_receipt_pending)")} == {"attempt_id"}
            assert db.execute("SELECT seeded FROM process_claim_receipts").fetchone() == (1,)
            assert db.execute("SELECT attempt_id FROM process_claim_receipt_pending").fetchone() == ("old/a1",)
    with Store(path) as store:
        # Run exactly startup's importer against the upgraded fixture store.
        daemon = object.__new__(Daemon)
        daemon.root, daemon.store = tmp_path, store
        assert store.one("SELECT seeded FROM process_claim_receipts")["seeded"] == 0
        daemon._seed_process_claim_receipts()
        assert json.loads(store.get_attempt("old/a1")["evidence_json"])["child_identity"] == child
        assert store.one("SELECT pid FROM attempt_process_claims WHERE attempt_id='old/a1'")["pid"] == 202
        assert store.query("SELECT * FROM process_claim_receipt_pending") == []
        assert store.one("SELECT seeded FROM process_claim_receipts")["seeded"] == 1
        assert store.list_leases()[0]["holder"] == "old/a1"
