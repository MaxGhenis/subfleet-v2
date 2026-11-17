-- Created by the unmodified Store from git archive 48dab4abee725c2b10287b00d85b3c92eda3072a (upgrade failed: OperationalError: no such column: wake_state; versions [11, 12])
BEGIN TRANSACTION;
CREATE TABLE actions (
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
CREATE TABLE artifacts (
  artifact_id INTEGER PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  role TEXT NOT NULL,                -- deliverable | stdout | stderr | raw-stream | lane-log | salvage | export | manifest
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE attempts (
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  seq INTEGER NOT NULL,
  lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
  model_requested TEXT NOT NULL,
  has_shell INTEGER CHECK (has_shell IN (0,1)),
  capability TEXT CHECK (capability IN ('none','commands','writable')),
  publish_denied INTEGER CHECK (publish_denied IN (0,1)),
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
CREATE TABLE closures (
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
CREATE TABLE decisions (
  decision_id INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  attempt_id TEXT,
  evaluated_at TEXT NOT NULL,
  policy_hash TEXT NOT NULL,
  decision_json TEXT NOT NULL
);
CREATE TABLE events (
  event_id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  job_id TEXT,
  attempt_id TEXT,
  lane_id TEXT,
  data_json TEXT NOT NULL
);
INSERT INTO "events" VALUES(1,'2026-11-01T00:00:00Z','schema.applied',NULL,NULL,NULL,'{"version":11}');
INSERT INTO "events" VALUES(2,'2026-11-01T00:00:00Z','jobs.insert','historic-review',NULL,NULL,'{}');
INSERT INTO "events" VALUES(3,'2026-11-01T00:00:00Z','notices.insert','historic-review',NULL,NULL,'{}');
INSERT INTO "events" VALUES(4,'2026-11-01T00:00:00Z','jobs.update','historic-review',NULL,NULL,'{}');
INSERT INTO "events" VALUES(5,'2026-11-17T05:43:23Z','schema.migrated',NULL,NULL,NULL,'{"version":12}');
CREATE TABLE job_owned_branches (
  remote_key TEXT NOT NULL,
  branch_key TEXT NOT NULL,
  branch TEXT NOT NULL,
  remote TEXT NOT NULL,
  family_job_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  sha TEXT,
  claimed_at TEXT NOT NULL,
  PRIMARY KEY (remote_key, branch_key)
);
CREATE TABLE job_pushes (
  job_id TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  remote TEXT NOT NULL,
  sha TEXT,
  started_at TEXT NOT NULL,
  intakes INTEGER NOT NULL DEFAULT 1,
  pushing_at TEXT,
  finished_at TEXT,
  result TEXT NOT NULL CHECK (result IN ('pending','succeeded','failed')),
  error TEXT
);
CREATE TABLE jobs (
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
  push_branch TEXT,
  push_remote TEXT,
  push_default_branch TEXT,
  push_sha TEXT,
  push_error TEXT,
  worktree TEXT,
  prompt_path TEXT NOT NULL,
  out_path TEXT,
  needs_shell INTEGER NOT NULL DEFAULT 0 CHECK (needs_shell IN (0,1)),
  -- C-5.1, D-QOS-BOOKED1 (schema version 10): `run --qos`; NULL is `utility`, the clamp.
  provider_qos TEXT CHECK (provider_qos IS NULL OR provider_qos IN ('utility','default')),
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
  caller_family TEXT CHECK (caller_family IS NULL OR caller_family IN ('claude','codex')),
  caller_pid INTEGER,
  review_subject TEXT,
  review_round INTEGER CHECK (review_round IS NULL OR review_round BETWEEN 1 AND 4),
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
, allow_same_family INTEGER NOT NULL DEFAULT 0 CHECK (allow_same_family IN (0,1)));
INSERT INTO "jobs" VALUES('historic-review','historic-request','synthetic-digest','dispatch','queued',NULL,NULL,'review',NULL,NULL,NULL,NULL,'/synthetic/work',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'/synthetic/prompt',NULL,0,NULL,'read-only','[]',0,0,0,0,NULL,NULL,'[]',NULL,NULL,'codex',NULL,'https://github.com/example/project/pull/17',2,NULL,NULL,3,21600,NULL,NULL,NULL,NULL,NULL,'2026-11-01T00:00:00Z',NULL,NULL,0);
CREATE TABLE lanes (
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
CREATE TABLE leases (
  lease_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,              -- attempt id or job id
  acquired_at TEXT NOT NULL,
  expires_at TEXT
);
CREATE TABLE notices (
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
INSERT INTO "notices" VALUES(1,'historic-review','synthetic-session','synthetic notice','pending',NULL,'2026-11-01T00:00:00Z',NULL,NULL);
CREATE TABLE readings (
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
CREATE TABLE schema_version (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);
INSERT INTO "schema_version" VALUES(11,'2026-11-01T00:00:00Z');
INSERT INTO "schema_version" VALUES(12,'2026-11-17T05:43:23Z');
CREATE TABLE service_notices (
  notice_id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  text TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','offered','acknowledged','surfaced')),
  transport TEXT,
  created_at TEXT NOT NULL,
  offered_at TEXT,
  acknowledged_at TEXT
);
CREATE INDEX lanes_account ON lanes(account_key);
CREATE INDEX lanes_identity ON lanes(identity) WHERE identity IS NOT NULL;
CREATE INDEX readings_latest ON readings(lane_id, scope, window, observed_at DESC);
CREATE INDEX readings_weekly_history ON readings(observed_at)
  WHERE window='seven_day' AND label IN ('provider','stale-provider');
CREATE INDEX readings_weekly_history_parsed ON readings(julianday(observed_at))
  WHERE window='seven_day' AND label IN ('provider','stale-provider');
CREATE INDEX closures_active ON closures(lane_id, scope, until_at) WHERE released_at IS NULL;
CREATE INDEX jobs_state ON jobs(state, created_at DESC);
CREATE INDEX jobs_caller ON jobs(caller_session, created_at DESC);
CREATE INDEX jobs_review_subject ON jobs(review_subject) WHERE review_subject IS NOT NULL;
CREATE INDEX jobs_parent ON jobs(parent_job_id);
CREATE INDEX jobs_created ON jobs(created_at);
CREATE INDEX attempts_live ON attempts(state) WHERE state IN ('reserved','starting','running','finalizing');
CREATE INDEX attempts_quarantined ON attempts(state) WHERE state='quarantined';
CREATE INDEX attempts_lane ON attempts(lane_id, state);
CREATE INDEX attempts_reserved ON attempts(reserved_at, seq);
CREATE INDEX artifacts_attempt ON artifacts(attempt_id, role);
CREATE INDEX notices_session ON notices(session_id, state);
CREATE INDEX notices_job ON notices(job_id, state);
CREATE INDEX decisions_job ON decisions(job_id, evaluated_at DESC);
CREATE INDEX events_job ON events(job_id, event_id);
CREATE INDEX events_kind ON events(kind, event_id DESC);
CREATE INDEX events_probe_holder ON events(json_extract(data_json,'$.holder'), event_id DESC)
  WHERE kind='probe.state' AND json_valid(data_json);
CREATE INDEX events_not_json ON events(kind, event_id DESC) WHERE NOT json_valid(data_json);
CREATE INDEX events_pin_notice ON events(kind,
  (CASE WHEN json_valid(data_json) THEN json_extract(data_json,'$.service_notice_id') END), event_id DESC)
  WHERE kind='job.pin_noticed';
COMMIT;
