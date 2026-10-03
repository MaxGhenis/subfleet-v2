-- subfleet v2 store schema, version 4 (2: jobless operator notices; 3: gate job fields; 4: lane identity columns). See docs/acceptance-contract.md section 3.
-- Applied by subfleet/store.py with journal_mode=WAL, synchronous=FULL, foreign_keys=ON.
-- Version 2 (C-3.1, additive and numbered) adds identity, label, and
-- identity_status to `lanes`; store.py migrates a version-1 database in place.

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);

-- C-10.1
CREATE TABLE IF NOT EXISTS lanes (
  lane_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('codex','claude')),
  account_key TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  credential_kind TEXT NOT NULL CHECK (credential_kind IN ('keychain-token','home','env')),
  credential_epoch INTEGER NOT NULL DEFAULT 1,
  home TEXT,
  owner TEXT NOT NULL DEFAULT 'v2' CHECK (owner IN ('v1','v2')),
  desktop INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  plan TEXT,
  -- C-10.6 (schema version 2): the identity the profile endpoint returned for
  -- this lane's own credential, "<account_uuid>:<org_uuid>"; the email label
  -- C-1.4 calls a display name and never a key; and the last identity check.
  identity TEXT,
  label TEXT,
  identity_status TEXT CHECK (identity_status IS NULL OR identity_status IN ('verified','enrolled','mismatch','unverified')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lanes_account ON lanes(account_key);
CREATE INDEX IF NOT EXISTS lanes_identity ON lanes(identity) WHERE identity IS NOT NULL;

-- C-9.1, C-9.8
CREATE TABLE IF NOT EXISTS readings (
  reading_id INTEGER PRIMARY KEY,
  lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
  scope TEXT NOT NULL,
  window TEXT NOT NULL,
  utilization REAL CHECK (utilization IS NULL OR (utilization >= 0.0 AND utilization <= 1.0)),
  resets_at TEXT,
  label TEXT NOT NULL CHECK (label IN ('provider','stale-provider','admission-observed','local-backoff','unknown')),
  source TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  attempt_id TEXT
);
CREATE INDEX IF NOT EXISTS readings_latest ON readings(lane_id, scope, window, observed_at DESC);

-- C-9.6
CREATE TABLE IF NOT EXISTS closures (
  closure_id INTEGER PRIMARY KEY,
  lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
  scope TEXT NOT NULL,
  until_at TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('provider-limit','credits','auth-dead','operator-hold','cooldown')),
  clock_source TEXT NOT NULL CHECK (clock_source IN ('reported','guessed')),
  source_event TEXT,
  created_at TEXT NOT NULL,
  released_at TEXT
);
CREATE INDEX IF NOT EXISTS closures_active ON closures(lane_id, scope, until_at) WHERE released_at IS NULL;

-- C-4.1, C-6, C-7
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  payload_digest TEXT NOT NULL,
  kind TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued','running','waiting','succeeded','failed','cancelled','lost')),
  wait_reason TEXT,
  next_check_at TEXT,
  task TEXT,
  tier TEXT,
  pinned_model TEXT,
  pinned_lane TEXT,
  unmeasured_reserve_reason TEXT,
  workdir TEXT NOT NULL,
  workdir_head TEXT,
  worktree TEXT,
  prompt_path TEXT NOT NULL,
  out_path TEXT,
  sandbox TEXT NOT NULL CHECK (sandbox IN ('read-only','workspace-write')),
  exclusions TEXT NOT NULL DEFAULT '[]',
  allow_desktop INTEGER NOT NULL DEFAULT 0,
  in_place INTEGER NOT NULL DEFAULT 0,
  independent INTEGER NOT NULL DEFAULT 0,
  isolated_review INTEGER NOT NULL DEFAULT 0,
  review_root TEXT,
  round_lease TEXT,
  mcp_servers TEXT NOT NULL DEFAULT '[]',
  parent_job_id TEXT REFERENCES jobs(job_id),
  caller_session TEXT,
  caller_pid INTEGER,
  name TEXT,
  policy_hash TEXT,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  max_wall_s INTEGER NOT NULL DEFAULT 21600,
  max_tokens_observed INTEGER,
  accepted_attempt_id TEXT,
  rc INTEGER,
  export_error TEXT,
  cancel_requested_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_caller ON jobs(caller_session, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_job_id);

-- C-4.2, C-5
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  seq INTEGER NOT NULL,
  lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
  model_requested TEXT NOT NULL,
  model_served TEXT,
  attestation TEXT NOT NULL DEFAULT 'unattested' CHECK (attestation IN ('attested','mismatch','unattested')),
  state TEXT NOT NULL CHECK (state IN ('reserved','starting','running','finalizing','succeeded','failed','interrupted','lost','cancelled','quarantined')),
  guardian_pid INTEGER,
  child_pid INTEGER,
  pgid INTEGER,
  boot_id TEXT,
  proc_start TEXT,
  native_session_id TEXT,
  transcript_path TEXT,
  transcript_offset INTEGER,
  baseline_tree TEXT,
  rc INTEGER,
  signal INTEGER,
  outcome_class TEXT CHECK (outcome_class IS NULL OR outcome_class IN ('ok','limited','auth-dead','cli-too-old','content-filter','transient','unknown')),
  outcome_detail TEXT,
  evidence_json TEXT,
  killed_by TEXT,
  quarantine_reason TEXT,
  reserved_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE (job_id, seq)
);
CREATE INDEX IF NOT EXISTS attempts_live ON attempts(state) WHERE state IN ('reserved','starting','running','finalizing');
CREATE INDEX IF NOT EXISTS attempts_lane ON attempts(lane_id, state);

-- C-8.2, C-13.1
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id INTEGER PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  role TEXT NOT NULL,                -- deliverable | stdout | stderr | raw-stream | lane-log | salvage | export | manifest
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_attempt ON artifacts(attempt_id, role);

-- C-15. `job_id` is nullable because the v1 outbox the migration manifest
-- imports (docs/migration.md, `S/outbox.sqlite3`) holds session messages that
-- name a session and no run; every notice v2 itself writes names a job (C-15.1).
CREATE TABLE IF NOT EXISTS notices (
  notice_id INTEGER PRIMARY KEY,
  job_id TEXT REFERENCES jobs(job_id),
  session_id TEXT,
  text TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','offered','acknowledged','surfaced')),
  transport TEXT,
  created_at TEXT NOT NULL,
  offered_at TEXT,
  acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS notices_session ON notices(session_id, state);

-- C-11.5
CREATE TABLE IF NOT EXISTS decisions (
  decision_id INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  attempt_id TEXT,
  evaluated_at TEXT NOT NULL,
  policy_hash TEXT NOT NULL,
  decision_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_job ON decisions(job_id, evaluated_at DESC);

-- C-6.3 leases: lane:<lane id>:slot:<n> (a turn's: slot:turn-<n>, C-26.9) | out:<path> | worktree:<realpath> | session:<id>
CREATE TABLE IF NOT EXISTS leases (
  lease_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,              -- attempt id or job id
  acquired_at TEXT NOT NULL,
  expires_at TEXT
);

-- C-19.1
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  op_key TEXT NOT NULL UNIQUE,
  subject TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','executing','confirmed','failed','unknown')),
  request_json TEXT,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- C-3.2 append-only
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  job_id TEXT,
  attempt_id TEXT,
  lane_id TEXT,
  data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, event_id);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, event_id DESC);
-- C-3.7: the newest probe record of a holder in one index step, not a walk of every
-- probe.state event (6.5k on 2026-09-25, 8-14 ms per lookup, one lookup per probe
-- lease per capacity view). Only payloads SQLite reads as JSON are indexed: the
-- index evaluates json_extract, which raises on anything else, and one such row
-- must not stop the store opening or a probe record being written.
CREATE INDEX IF NOT EXISTS events_probe_holder ON events(json_extract(data_json,'$.holder'), event_id DESC)
  WHERE kind='probe.state' AND json_valid(data_json);
-- C-3.7: every event whose payload json_valid refuses: a NaN or an Infinity, which
-- json.dumps writes and json.loads reads, or a row that is not JSON at all. A query
-- that filters on JSON in SQL also reads these few rows and parses them in Python,
-- as every such read did before it moved into SQL.
CREATE INDEX IF NOT EXISTS events_not_json ON events(kind, event_id DESC) WHERE NOT json_valid(data_json);
-- C-11.8, C-15.8: the pin notice a `job.pin_noticed` event names, so naming a
-- listed notice's job is one index step per notice, however many pins had no one
-- to tell (their events name none) and however long the history. The CASE keeps
-- json_extract off a payload json_valid refuses, in the index as in the query
-- (`store.pin_notice_jobs`). `kind` leads so that, with no statistics (nothing
-- here runs ANALYZE), the planner prefers it to `events_kind`: two equality
-- terms to one.
CREATE INDEX IF NOT EXISTS events_pin_notice ON events(kind,
  (CASE WHEN json_valid(data_json) THEN json_extract(data_json,'$.service_notice_id') END), event_id DESC)
  WHERE kind='job.pin_noticed';

-- Jobless operator messages use ping and the same notice polling/ack path.
-- A separate table is additive: v1's notices.job_id remains a required FK.
CREATE TABLE IF NOT EXISTS service_notices (
  notice_id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  text TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','offered','acknowledged','surfaced')),
  transport TEXT,
  created_at TEXT NOT NULL,
  offered_at TEXT,
  acknowledged_at TEXT
);
