-- Derived, transactionally maintained ownership projection. Only recorded
-- authority/ancestry and ambiguous observations are claims; census roots are not.
-- The PID/start key keeps reads independent of the retained attempt ledger.
CREATE TABLE IF NOT EXISTS attempt_process_claims (
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
  pid INTEGER NOT NULL,
  proc_start TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (pid,proc_start,attempt_id,boot_id,source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS process_claims_attempt ON attempt_process_claims(attempt_id);

CREATE TABLE IF NOT EXISTS process_claim_clock (singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL);
INSERT OR IGNORE INTO process_claim_clock VALUES(1,0);
-- Keep deletion tombstones too: a new foreign claim can only suppress a kill.
CREATE TABLE IF NOT EXISTS process_claim_revisions (attempt_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS process_claim_revision_generation ON process_claim_revisions(generation,attempt_id);

CREATE TRIGGER IF NOT EXISTS process_claims_update
AFTER UPDATE OF guardian_pid,boot_id,proc_start,evidence_json ON attempts
BEGIN
  UPDATE process_claim_clock SET generation=generation+1 WHERE singleton=1;
  INSERT OR REPLACE INTO process_claim_revisions
    SELECT NEW.attempt_id,generation FROM process_claim_clock WHERE singleton=1;
  DELETE FROM attempt_process_claims WHERE attempt_id=NEW.attempt_id;
  INSERT OR IGNORE INTO attempt_process_claims
    SELECT NEW.attempt_id,NEW.guardian_pid,COALESCE(NEW.proc_start,''),COALESCE(NEW.boot_id,''),'guardian'
    WHERE NEW.guardian_pid>0;
  INSERT OR IGNORE INTO attempt_process_claims
    SELECT NEW.attempt_id,json_extract(CASE WHEN n.type='object' THEN n.value ELSE '{}' END,'$.pid'),
           COALESCE(json_extract(CASE WHEN n.type='object' THEN n.value ELSE '{}' END,'$.proc_start'),''),
           COALESCE(json_extract(CASE WHEN n.type='object' THEN n.value ELSE '{}' END,'$.boot_id'),''),s.key
    FROM json_each(CASE WHEN json_valid(NEW.evidence_json) THEN NEW.evidence_json ELSE '{}' END) s,
         json_tree(CASE WHEN s.type IN ('object','array') THEN s.value ELSE '{}' END) n
    WHERE s.key IN ('owned_identities','owned_identity_history','provider_identities',
                    'descendant_identities','ambiguous_identities','child_identity')
      AND n.type='object' AND json_extract(CASE WHEN n.type='object' THEN n.value ELSE '{}' END,'$.pid')>0;
END;
CREATE TRIGGER IF NOT EXISTS process_claims_insert AFTER INSERT ON attempts
BEGIN
  UPDATE attempts SET evidence_json=evidence_json WHERE attempt_id=NEW.attempt_id;
END;

CREATE TRIGGER IF NOT EXISTS process_claims_delete AFTER DELETE ON attempts
BEGIN
  UPDATE process_claim_clock SET generation=generation+1 WHERE singleton=1;
  INSERT OR REPLACE INTO process_claim_revisions
    SELECT OLD.attempt_id,generation FROM process_claim_clock WHERE singleton=1;
END;

-- One restart-safe import of legacy child launch receipts into attempt rows.
CREATE TABLE IF NOT EXISTS process_claim_receipts (singleton INTEGER PRIMARY KEY CHECK(singleton=1), seeded INTEGER NOT NULL, enumerated INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO process_claim_receipts(singleton,seeded) VALUES(1,0);

-- A receipt can arrive after the startup import. Remember changed attempts
-- whose child publication still needs importing, including a just-finished
-- legacy attempt. This queue avoids rescanning retained terminal history.
CREATE TABLE IF NOT EXISTS process_claim_receipt_pending (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id) ON DELETE CASCADE,
  failures INTEGER NOT NULL DEFAULT 0,
  retry_at REAL NOT NULL DEFAULT 0,
  last_error TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS process_claim_receipt_due ON process_claim_receipt_pending(retry_at,attempt_id);
CREATE TRIGGER IF NOT EXISTS process_claim_receipt_added AFTER INSERT ON attempts
BEGIN
  INSERT OR IGNORE INTO process_claim_receipt_pending(attempt_id) VALUES(NEW.attempt_id);
END;
CREATE TRIGGER IF NOT EXISTS process_claim_receipt_resolved AFTER UPDATE OF evidence_json ON attempts
WHEN CASE WHEN json_valid(NEW.evidence_json) THEN json_extract(NEW.evidence_json,'$.child_identity') END IS NOT NULL
BEGIN
  DELETE FROM process_claim_receipt_pending WHERE attempt_id=NEW.attempt_id;
END;
CREATE TRIGGER IF NOT EXISTS process_claim_receipt_changed
AFTER UPDATE OF evidence_json,state,child_pid ON attempts
WHEN CASE WHEN json_valid(NEW.evidence_json) THEN json_extract(NEW.evidence_json,'$.child_identity') END IS NULL
  AND (CASE WHEN json_valid(OLD.evidence_json) THEN json_extract(OLD.evidence_json,'$.child_identity') END IS NOT NULL
       OR (OLD.state IN ('reserved','starting','running','finalizing','quarantined')
           AND NEW.state NOT IN ('reserved','starting','running','finalizing','quarantined')))
BEGIN
  INSERT OR IGNORE INTO process_claim_receipt_pending(attempt_id) VALUES(NEW.attempt_id);
END;
