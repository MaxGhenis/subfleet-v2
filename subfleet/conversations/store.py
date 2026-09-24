"""The conversation store: `<state root>/conversations.sqlite3` (C-24, design D-4, §3).

Written only by the daemon, through its own connection and lock, so streamed
events never contend for the job store's lock and `state.sqlite3` keeps the
schema every retained release can open. The job store stays the source of
truth for jobs and attempts; a message's `job_id` is a binding repaired from
request ids (`turn:<message id>:<n>`) whenever it is missing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .turn import LIVE_STATES, MESSAGE_STATES, PERMISSIONS, QUEUED, TERMINAL_STATES

SCHEMA_VERSION = 2
PROVIDERS = ("claude", "codex")
EVENT_ROW_MAX = 64 * 1024
PAGE_BYTES = 256 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id   TEXT PRIMARY KEY,
  provider          TEXT NOT NULL CHECK (provider IN ('claude','codex')),
  native_session_id TEXT,
  title             TEXT,
  workspace         TEXT NOT NULL,
  workspace_kind    TEXT NOT NULL CHECK (workspace_kind IN ('in-place','worktree')),
  allow_main        INTEGER NOT NULL DEFAULT 0,
  lane_id           TEXT,
  settings_json     TEXT NOT NULL,
  origin            TEXT NOT NULL CHECK (origin IN ('new','native','handoff','legacy')),
  handoff_from_json TEXT,
  request_id        TEXT UNIQUE,
  blocked_by        TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT,
  UNIQUE (provider, native_session_id)
);
CREATE TABLE IF NOT EXISTS messages (
  message_id        TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES conversations(conversation_id),
  seq               INTEGER NOT NULL,
  after_message_id  TEXT,
  origin            TEXT NOT NULL,
  continues         TEXT,
  digest            TEXT NOT NULL,
  text_path         TEXT NOT NULL,
  attachments_json  TEXT NOT NULL,
  settings_json     TEXT NOT NULL,
  state             TEXT NOT NULL,
  state_reason      TEXT,
  turn_seq          INTEGER NOT NULL DEFAULT 0,
  job_id            TEXT,
  turn_ref          TEXT,
  served_json       TEXT,
  stop_requested_at TEXT,
  resolution_json   TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS messages_by_state ON messages(conversation_id, state, seq);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id         TEXT PRIMARY KEY,
  message_id          TEXT NOT NULL REFERENCES messages(message_id),
  conversation_id     TEXT NOT NULL,
  attempt_id          TEXT NOT NULL,
  provider_request_id TEXT NOT NULL,
  kind                TEXT NOT NULL,
  request_path        TEXT NOT NULL,
  request_sha256      TEXT NOT NULL,
  display_json        TEXT NOT NULL,
  options_json        TEXT NOT NULL,
  nonce               TEXT NOT NULL,
  state               TEXT NOT NULL CHECK (state IN ('pending','answered','withdrawn')),
  decision_json       TEXT,
  created_at TEXT NOT NULL, answered_at TEXT,
  UNIQUE (attempt_id, provider_request_id)
);
CREATE TABLE IF NOT EXISTS attachments (
  sha256 TEXT PRIMARY KEY, media_type TEXT NOT NULL, bytes INTEGER NOT NULL,
  path TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt_marks (
  attempt_id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
  stdout_offset INTEGER NOT NULL, stdin_seq INTEGER NOT NULL,
  compacted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL, message_id TEXT, attempt_id TEXT NOT NULL,
  source TEXT NOT NULL, position TEXT NOT NULL, ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL, data_json TEXT NOT NULL, ts TEXT NOT NULL,
  UNIQUE (attempt_id, source, position, ordinal)
);
CREATE INDEX IF NOT EXISTS events_by_conversation ON events(conversation_id, seq);
CREATE TABLE IF NOT EXISTS floors (conversation_id TEXT PRIMARY KEY, compacted_through INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL, message_id TEXT, state TEXT, pending_approvals INTEGER NOT NULL,
  ts TEXT NOT NULL
);
"""

# Schema 2 (C-26.13, design D-25): a turn's working-tree snapshots, one row per
# attempt, keyed by the job store's attempt id as `attempt_marks` is. The start
# is the attempt's `baseline_tree` from the job store; the end is written at
# finalization.
TURN_TREES = """
CREATE TABLE IF NOT EXISTS turn_trees (
  attempt_id      TEXT PRIMARY KEY,
  message_id      TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  workspace       TEXT NOT NULL,
  writable        INTEGER NOT NULL,
  head_before     TEXT,
  start_tree      TEXT,
  head_after      TEXT,
  end_tree        TEXT,
  error           TEXT,
  started_at      TEXT NOT NULL,
  ended_at        TEXT
);
CREATE INDEX IF NOT EXISTS turn_trees_by_message ON turn_trees(message_id, started_at);
CREATE INDEX IF NOT EXISTS turn_trees_by_conversation ON turn_trees(conversation_id, started_at);
"""
SCHEMA += TURN_TREES

#: Each step carries a store of the version before it forward (the main store's
#: rule, C-3.1). Every statement is idempotent, so an interrupted upgrade re-runs
#: safely; the whole upgrade is one transaction, so a store is either migrated or
#: untouched.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: tuple(statement.strip() for statement in TURN_TREES.split(";") if statement.strip()),
}


class ConversationError(Exception):
    """A refusal with an exit code (2 invalid input, 7 refused) and a short reason."""

    def __init__(self, reason: str, message: str, code: int = 2, fix: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.code = code
        self.fix = fix


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """A sortable id: prefix, millisecond time, random tail."""
    return f"{prefix}-{int(time.time() * 1000):013d}-{secrets.token_hex(6)}"


def canonical_uuid(value: Any) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ConversationError("bad-message-id", "message_id must be a UUID") from exc
    if str(parsed) != str(value).lower():
        raise ConversationError("bad-message-id", "message_id must be a canonical lowercase UUID")
    return str(parsed)


def validate_settings(provider: str, settings: Any) -> dict:
    """The shape every message's settings must have (C-24.2); catalog checks are
    `models.list`'s and the driver's (C-26.8)."""
    if not isinstance(settings, dict):
        raise ConversationError("bad-settings", "settings must be an object")
    model = settings.get("model")
    if not isinstance(model, str) or not model or len(model) > 80:
        raise ConversationError("bad-settings", "settings.model must name a model")
    effort = settings.get("effort")
    if effort is not None and (not isinstance(effort, str) or not effort or len(effort) > 20):
        raise ConversationError("bad-settings", "settings.effort must be a string or null")
    if not isinstance(settings.get("fast", False), bool):
        raise ConversationError("bad-settings", "settings.fast must be true or false")
    permission = settings.get("permission")
    if permission not in PERMISSIONS:
        raise ConversationError("bad-settings", f"settings.permission must be one of {', '.join(PERMISSIONS)}")
    auto = settings.get("auto_continue", True)
    if not isinstance(auto, bool):
        raise ConversationError("bad-settings", "settings.auto_continue must be true or false")
    return {"model": model, "effort": effort, "fast": bool(settings.get("fast", False)),
            "permission": permission, "auto_continue": auto}


# Messages Subfleet writes to repair a session; they go ahead of queued person messages.
REPAIR_ORIGINS = ("unblock-note", "failover")

PERMISSION_ORDER = {"read-only": 0, "ask": 1, "accept-edits": 2, "bypass": 3}


def widens(before: dict, after: dict) -> bool:
    """A settings change a person must confirm (C-25.6, design D-9)."""
    return PERMISSION_ORDER[after["permission"]] > PERMISSION_ORDER[before["permission"]]


def message_digest(conversation_id: str, text: str, attachments: list[str], settings: dict) -> str:
    body = {"conversation_id": conversation_id, "text": text, "attachments": list(attachments),
            "settings": settings}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _publish(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class ConversationStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "conversations.sqlite3"
        self.dir = self.root / "conversations"
        self._lock = threading.RLock()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5)
        self._db.row_factory = sqlite3.Row
        if fresh:
            os.chmod(self.path, 0o600)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            version = None
            try:
                row = self._db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
                version = row["v"] if row else None
            except sqlite3.OperationalError:
                pass
            if version is not None and version > SCHEMA_VERSION:
                raise ConversationError("schema", f"conversations.sqlite3 is schema {version}; this build knows {SCHEMA_VERSION}",
                                        code=1, fix="install the release that created it")
            if version is not None and version < SCHEMA_VERSION:
                self._migrate(version)
            self._db.executescript(SCHEMA)
            if version is None:
                self._db.execute("INSERT INTO schema_version VALUES (?,?)", (SCHEMA_VERSION, utcnow()))
        self.changed = threading.Condition()

    def _migrate(self, version: int) -> None:
        """Carry an older store forward one numbered step at a time, in one
        transaction, recording each step (the main store's rule, C-3.1)."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for step in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS.get(step, ()):
                    self._db.execute(statement)
                self._db.execute("INSERT INTO schema_version VALUES (?,?)", (step, utcnow()))
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- plumbing --------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        self.notify()

    def notify(self) -> None:
        with self.changed:
            self.changed.notify_all()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- conversations ---------------------------------------------------------

    def conversation(self, conversation_id: str) -> dict:
        row = self.one("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,))
        if row is None:
            raise ConversationError("unknown-conversation", f"no conversation {conversation_id}")
        return _decode_conversation(row)

    def by_native(self, provider: str, native_session_id: str) -> dict | None:
        row = self.one("SELECT * FROM conversations WHERE provider=? AND native_session_id=?",
                       (provider, native_session_id))
        return _decode_conversation(row) if row else None

    def create_conversation(self, *, provider: str, workspace: str, workspace_kind: str, settings: dict,
                            origin: str, native_session_id: str | None = None, title: str | None = None,
                            allow_main: bool = False, lane_id: str | None = None,
                            request_id: str | None = None, handoff_from: dict | None = None) -> tuple[dict, bool]:
        if provider not in PROVIDERS:
            raise ConversationError("bad-provider", "provider must be claude or codex")
        settings = validate_settings(provider, settings)
        now = utcnow()
        with self.transaction() as tx:
            if request_id:
                existing = tx.execute("SELECT * FROM conversations WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    return _decode_conversation(dict(existing)), False
            if native_session_id:
                existing = tx.execute("SELECT * FROM conversations WHERE provider=? AND native_session_id=?",
                                      (provider, native_session_id)).fetchone()
                if existing:
                    return _decode_conversation(dict(existing)), False
            cid = new_id("cv")
            tx.execute(
                "INSERT INTO conversations(conversation_id,provider,native_session_id,title,workspace,workspace_kind,"
                "allow_main,lane_id,settings_json,origin,handoff_from_json,request_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, provider, native_session_id, title, workspace, workspace_kind, int(allow_main), lane_id,
                 json.dumps(settings), origin, json.dumps(handoff_from) if handoff_from else None, request_id,
                 now, now))
            self._change(tx, cid, None, None)
        return self.conversation(cid), True

    def update_conversation(self, conversation_id: str, **fields: Any) -> dict:
        allowed = {"native_session_id", "title", "lane_id", "settings", "blocked_by", "allow_main", "archived_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown conversation fields {sorted(unknown)}")
        sets, params = [], []
        for key, value in fields.items():
            if key == "settings":
                sets.append("settings_json=?")
                params.append(json.dumps(value))
            elif key == "allow_main":
                sets.append("allow_main=?")
                params.append(int(bool(value)))
            else:
                sets.append(f"{key}=?")
                params.append(value)
        sets.append("updated_at=?")
        params.append(utcnow())
        with self.transaction() as tx:
            tx.execute(f"UPDATE conversations SET {','.join(sets)} WHERE conversation_id=?", (*params, conversation_id))
            self._change(tx, conversation_id, None, None)
        return self.conversation(conversation_id)

    def list_conversations(self, *, provider: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM conversations WHERE archived_at IS NULL"
        params: list[Any] = []
        if provider:
            sql += " AND provider=?"
            params.append(provider)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [_decode_conversation(r) for r in self.query(sql, tuple(params))]

    # --- messages --------------------------------------------------------------

    def submit_message(self, *, conversation_id: str, message_id: str, after_message_id: str | None,
                       text: str, attachments: list[str], settings: dict, origin: str = "person",
                       continues: str | None = None) -> tuple[dict, bool]:
        """Durably accept a message (C-24.2, C-24.3). Idempotent by id and digest;
        in order by the client's predecessor."""
        message_id = canonical_uuid(message_id)
        if after_message_id is not None:
            after_message_id = canonical_uuid(after_message_id)
        conversation = self.conversation(conversation_id)
        settings = validate_settings(conversation["provider"], settings)
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1_048_576:
            raise ConversationError("bad-text", "text must be a string of at most 1 MiB")
        if not text.strip() and not attachments:
            raise ConversationError("empty", "a message needs text or an attachment")
        if len(attachments) > 8 or len(set(attachments)) != len(attachments):
            raise ConversationError("bad-attachments", "at most 8 distinct attachments")
        digest = message_digest(conversation_id, text, attachments, settings)
        now = utcnow()
        # Named by content too, so a racing submit of the same id with other text
        # can never overwrite the text of the one that was accepted.
        text_path = self.dir / conversation_id / "messages" / f"{message_id}.{digest[:16]}.md"
        existing = self.one("SELECT * FROM messages WHERE message_id=?", (message_id,))
        if existing:
            if existing["digest"] != digest or existing["conversation_id"] != conversation_id:
                raise ConversationError("message-id-conflict", "message id already used with different content")
            return _decode_message(existing), False
        _publish(text_path, text.encode("utf-8"))           # C-24.3: text before the row
        with self.transaction() as tx:
            again = tx.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if again:
                if again["digest"] != digest:
                    raise ConversationError("message-id-conflict", "message id already used with different content")
                return _decode_message(dict(again)), False
            last = tx.execute("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                              "ORDER BY seq DESC LIMIT 1", (conversation_id,)).fetchone()
            if origin == "person" and (last["message_id"] if last else None) != after_message_id:
                raise ConversationError("out-of-order", "the message's predecessor has not been accepted yet",
                                        fix="send the earlier message first; this one changed nothing")
            for sha in attachments:
                if not tx.execute("SELECT 1 FROM attachments WHERE sha256=?", (sha,)).fetchone():
                    raise ConversationError("unknown-attachment", f"no attachment {sha}")
                tx.execute("UPDATE attachments SET last_used_at=? WHERE sha256=?", (now, sha))
            seq = (tx.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE conversation_id=?",
                              (conversation_id,)).fetchone()[0]) + 1
            tx.execute(
                "INSERT INTO messages(message_id,conversation_id,seq,after_message_id,origin,continues,digest,text_path,"
                "attachments_json,settings_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, conversation_id, seq, after_message_id, origin, continues, digest, str(text_path),
                 json.dumps(list(attachments)), json.dumps(settings), QUEUED, now, now))
            tx.execute("UPDATE conversations SET updated_at=?, settings_json=? WHERE conversation_id=?",
                       (now, json.dumps(settings), conversation_id))
            self._change(tx, conversation_id, message_id, QUEUED)
        return self.message(message_id), True

    def message(self, message_id: str) -> dict:
        row = self.one("SELECT * FROM messages WHERE message_id=?", (message_id,))
        if row is None:
            raise ConversationError("unknown-message", f"no message {message_id}")
        return _decode_message(row)

    def messages(self, conversation_id: str, *, limit: int = 50) -> list[dict]:
        rows = self.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?",
                          (conversation_id, max(1, min(limit, 500))))
        return [_decode_message(r) for r in reversed(rows)]

    def message_text(self, message: dict) -> str:
        return Path(message["text_path"]).read_text(encoding="utf-8")

    def set_state(self, message_id: str, state: str, *, reason: str | None = None,
                  expect: tuple[str, ...] | None = None, **fields: Any) -> bool:
        """Move a message; with `expect`, only from those states. Returns whether it moved."""
        if state not in MESSAGE_STATES:
            raise ValueError(f"unknown state {state}")
        allowed = {"job_id", "turn_seq", "turn_ref", "served", "stop_requested_at", "resolution"}
        sets, params = ["state=?", "state_reason=?", "updated_at=?"], [state, reason, utcnow()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown message field {key}")
            column = {"served": "served_json", "resolution": "resolution_json"}.get(key, key)
            sets.append(f"{column}=?")
            params.append(json.dumps(value) if key in ("served", "resolution") else value)
        where, wparams = "message_id=?", [message_id]
        if expect:
            where += f" AND state IN ({','.join('?' * len(expect))})"
            wparams += list(expect)
        with self.transaction() as tx:
            cur = tx.execute(f"UPDATE messages SET {','.join(sets)} WHERE {where}", (*params, *wparams))
            if cur.rowcount:
                row = tx.execute("SELECT conversation_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
                self._change(tx, row["conversation_id"], message_id, state)
            return bool(cur.rowcount)

    def update_message(self, message_id: str, **fields: Any) -> None:
        allowed = {"job_id", "turn_seq", "turn_ref", "served", "stop_requested_at", "resolution"}
        sets, params = ["updated_at=?"], [utcnow()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown message field {key}")
            column = {"served": "served_json", "resolution": "resolution_json"}.get(key, key)
            sets.append(f"{column}=?")
            params.append(json.dumps(value) if key in ("served", "resolution") else value)
        with self.transaction() as tx:
            tx.execute(f"UPDATE messages SET {','.join(sets)} WHERE message_id=?", (*params, message_id))

    def next_dispatchable(self) -> list[dict]:
        """For each unblocked conversation with no live message, its next queued one
        (C-24.5): a repair message first (an unblock note, a failover continuation;
        C-24.8, C-26.7), since the person's queued messages were written expecting
        it; otherwise the lowest sequence."""
        repair = ",".join(f"'{origin}'" for origin in REPAIR_ORIGINS)
        rows = self.query(
            "SELECT m.* FROM messages m JOIN conversations c USING(conversation_id) "
            "WHERE m.state='queued' AND c.blocked_by IS NULL AND c.archived_at IS NULL "
            "AND m.message_id = (SELECT q.message_id FROM messages q WHERE q.conversation_id=m.conversation_id "
            f"AND q.state='queued' ORDER BY q.origin IN ({repair}) DESC, q.seq LIMIT 1) "
            f"AND NOT EXISTS (SELECT 1 FROM messages l WHERE l.conversation_id=m.conversation_id AND l.state IN ({','.join('?' * len(LIVE_STATES))})) "
            "ORDER BY m.created_at", LIVE_STATES)
        return [_decode_message(r) for r in rows]

    def live_messages(self) -> list[dict]:
        rows = self.query(f"SELECT * FROM messages WHERE state IN ({','.join('?' * len(LIVE_STATES))})", LIVE_STATES)
        return [_decode_message(r) for r in rows]

    # --- approvals -------------------------------------------------------------

    def add_approval(self, *, message_id: str, conversation_id: str, attempt_id: str, provider_request_id: str,
                     kind: str, request: dict, display: dict, options: tuple[str, ...]) -> tuple[dict, bool]:
        existing = self.one("SELECT * FROM approvals WHERE attempt_id=? AND provider_request_id=?",
                            (attempt_id, provider_request_id))
        if existing:
            return _decode_approval(existing), False
        approval_id = new_id("ap")
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        path = self.dir / conversation_id / "approvals" / f"{approval_id}.json"
        _publish(path, raw)
        with self.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO approvals(approval_id,message_id,conversation_id,attempt_id,provider_request_id,kind,"
                "request_path,request_sha256,display_json,options_json,nonce,state,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (approval_id, message_id, conversation_id, attempt_id, provider_request_id, kind, str(path),
                 hashlib.sha256(raw).hexdigest(), json.dumps(display), json.dumps(list(options)),
                 secrets.token_hex(16), utcnow()))
            pending = tx.execute("SELECT COUNT(*) FROM approvals WHERE message_id=? AND state='pending'",
                                 (message_id,)).fetchone()[0]
            self._change(tx, conversation_id, message_id, None, pending=pending)
        row = self.one("SELECT * FROM approvals WHERE attempt_id=? AND provider_request_id=?",
                       (attempt_id, provider_request_id))
        return _decode_approval(row), True

    def approval(self, approval_id: str) -> dict:
        row = self.one("SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
        if row is None:
            raise ConversationError("unknown-approval", f"no approval {approval_id}")
        return _decode_approval(row)

    def approvals(self, *, conversation_id: str | None = None, message_id: str | None = None,
                  state: str | None = "pending") -> list[dict]:
        sql, params = "SELECT * FROM approvals WHERE 1=1", []
        for column, value in (("conversation_id", conversation_id), ("message_id", message_id), ("state", state)):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        return [_decode_approval(r) for r in self.query(sql + " ORDER BY created_at", tuple(params))]

    def answer_approval(self, approval_id: str, decision: dict) -> bool:
        with self.transaction() as tx:
            cur = tx.execute("UPDATE approvals SET state='answered', decision_json=?, answered_at=? "
                             "WHERE approval_id=? AND state='pending'",
                             (json.dumps(decision), utcnow(), approval_id))
            if cur.rowcount:
                row = tx.execute("SELECT conversation_id, message_id FROM approvals WHERE approval_id=?",
                                 (approval_id,)).fetchone()
                pending = tx.execute("SELECT COUNT(*) FROM approvals WHERE message_id=? AND state='pending'",
                                     (row["message_id"],)).fetchone()[0]
                self._change(tx, row["conversation_id"], row["message_id"], None, pending=pending)
            return bool(cur.rowcount)

    def withdraw_approvals(self, *, attempt_id: str, provider_request_ids: list[str] | None = None) -> int:
        sql = "UPDATE approvals SET state='withdrawn', answered_at=? WHERE attempt_id=? AND state='pending'"
        params: list[Any] = [utcnow(), attempt_id]
        if provider_request_ids is not None:
            if not provider_request_ids:
                return 0
            sql += f" AND provider_request_id IN ({','.join('?' * len(provider_request_ids))})"
            params += provider_request_ids
        with self.transaction() as tx:
            return tx.execute(sql, tuple(params)).rowcount

    # --- attachments -----------------------------------------------------------

    def add_attachment(self, sha256: str, media_type: str, size: int, path: str) -> dict:
        now = utcnow()
        with self.transaction() as tx:
            tx.execute("INSERT INTO attachments(sha256,media_type,bytes,path,created_at,last_used_at) VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT(sha256) DO UPDATE SET last_used_at=excluded.last_used_at",
                       (sha256, media_type, size, path, now, now))
        return self.one("SELECT * FROM attachments WHERE sha256=?", (sha256,))

    def attachment(self, sha256: str) -> dict | None:
        return self.one("SELECT * FROM attachments WHERE sha256=?", (sha256,))

    # --- events ----------------------------------------------------------------

    def mark(self, attempt_id: str) -> dict:
        return self.one("SELECT * FROM attempt_marks WHERE attempt_id=?", (attempt_id,)) or {
            "attempt_id": attempt_id, "stdout_offset": 0, "stdin_seq": 0, "compacted": 0}

    def append_events(self, *, conversation_id: str, message_id: str, attempt_id: str,
                      events: list[tuple[str, str, int, str, dict]], stdout_offset: int, stdin_seq: int) -> int:
        """One batch (C-25.4): events `(source, position, ordinal, kind, data)` and the
        attempt's watermark, in one transaction. Duplicates are ignored (C-26.6)."""
        now = utcnow()
        written = 0
        with self.transaction() as tx:
            for source, position, ordinal, kind, data in events:
                body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
                if len(body.encode()) > EVENT_ROW_MAX:
                    body = json.dumps({"truncated": True, "kind": kind})
                cur = tx.execute(
                    "INSERT OR IGNORE INTO events(conversation_id,message_id,attempt_id,source,position,ordinal,kind,data_json,ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (conversation_id, message_id, attempt_id, source, position, ordinal, kind, body, now))
                written += cur.rowcount
            tx.execute("INSERT INTO attempt_marks(attempt_id,message_id,stdout_offset,stdin_seq) VALUES (?,?,?,?) "
                       "ON CONFLICT(attempt_id) DO UPDATE SET stdout_offset=MAX(stdout_offset,excluded.stdout_offset), "
                       "stdin_seq=MAX(stdin_seq,excluded.stdin_seq)",
                       (attempt_id, message_id, stdout_offset, stdin_seq))
        return written

    def events_after(self, conversation_id: str, after: int, *, limit: int = 500,
                     max_bytes: int = PAGE_BYTES) -> dict:
        floor = self.one("SELECT compacted_through FROM floors WHERE conversation_id=?", (conversation_id,))
        # A client that has seen something, but not everything up to the floor,
        # missed compacted rows: it reloads the conversation (C-25.4).
        reset = bool(floor) and 0 < after < floor["compacted_through"]
        rows = self.query("SELECT seq,message_id,kind,data_json,ts FROM events WHERE conversation_id=? AND seq>? "
                          "ORDER BY seq LIMIT ?", (conversation_id, after, max(1, min(limit, 1000))))
        out, size, nxt = [], 0, after
        for row in rows:
            size += len(row["data_json"]) + 64
            if out and size > max_bytes:
                break
            out.append({"seq": row["seq"], "message_id": row["message_id"], "kind": row["kind"], "ts": row["ts"],
                        "data": json.loads(row["data_json"])})
            nxt = row["seq"]
        return {"events": out, "next": nxt, "reset": reset}

    def compact(self, attempt_id: str) -> int:
        """Remove an ended attempt's delta rows; record the floor (design §3)."""
        with self.transaction() as tx:
            row = tx.execute("SELECT conversation_id, MAX(seq) AS top FROM events WHERE attempt_id=?",
                             (attempt_id,)).fetchone()
            if not row or row["conversation_id"] is None:
                return 0
            deleted = tx.execute("DELETE FROM events WHERE attempt_id=? AND kind IN ('text.delta','thinking.delta')",
                                 (attempt_id,)).rowcount
            tx.execute("INSERT INTO floors(conversation_id,compacted_through) VALUES (?,?) ON CONFLICT(conversation_id) "
                       "DO UPDATE SET compacted_through=MAX(compacted_through,excluded.compacted_through)",
                       (row["conversation_id"], row["top"]))
            tx.execute("UPDATE attempt_marks SET compacted=1 WHERE attempt_id=?", (attempt_id,))
            return deleted

    # --- a turn's snapshots (C-26.13, design D-25) ------------------------------

    def record_trees(self, *, attempt_id: str, message_id: str, conversation_id: str, workspace: str,
                     writable: bool, started_at: str, head_before: str | None = None, start_tree: str | None = None,
                     head_after: str | None = None, end_tree: str | None = None, error: str | None = None,
                     ended: bool = False) -> None:
        """Record a turn attempt's start, its end, or both. Idempotent: the start is
        written once, the end fills in; a replayed record changes nothing."""
        with self.transaction() as tx:
            before = tx.execute("SELECT ended_at FROM turn_trees WHERE attempt_id=?", (attempt_id,)).fetchone()
            tx.execute(
                "INSERT INTO turn_trees(attempt_id,message_id,conversation_id,workspace,writable,head_before,start_tree,"
                "head_after,end_tree,error,started_at,ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(attempt_id) DO UPDATE SET "
                "head_before=COALESCE(turn_trees.head_before,excluded.head_before), "
                "start_tree=COALESCE(turn_trees.start_tree,excluded.start_tree), "
                "head_after=COALESCE(turn_trees.head_after,excluded.head_after), "
                "end_tree=COALESCE(turn_trees.end_tree,excluded.end_tree), "
                "error=COALESCE(turn_trees.error,excluded.error), "
                "ended_at=COALESCE(turn_trees.ended_at,excluded.ended_at)",
                (attempt_id, message_id, conversation_id, workspace, int(bool(writable)), head_before, start_tree,
                 head_after, end_tree, error, started_at, utcnow() if ended else None))
            if ended and not (before and before["ended_at"]):
                # The change feed says this message's changes are final (C-29.9).
                self._change(tx, conversation_id, message_id, None)

    def turn_trees(self, message_id: str) -> dict | None:
        """The message's latest turn attempt's snapshots: a re-admitted message's
        earlier attempts never delivered it (C-26.7), so the latest is the turn."""
        row = self.one("SELECT * FROM turn_trees WHERE message_id=? ORDER BY started_at DESC, attempt_id DESC LIMIT 1",
                       (message_id,))
        return _decode_trees(row) if row else None

    def first_trees(self, conversation_id: str) -> dict | None:
        """The conversation's base: the start snapshot of its first writable turn."""
        row = self.one("SELECT * FROM turn_trees WHERE conversation_id=? AND start_tree IS NOT NULL "
                       "ORDER BY started_at, attempt_id LIMIT 1", (conversation_id,))
        return _decode_trees(row) if row else None

    # --- the change feed (C-29.9) ----------------------------------------------

    def _change(self, tx: sqlite3.Connection, conversation_id: str, message_id: str | None, state: str | None,
                *, pending: int | None = None) -> None:
        if pending is None:
            pending = tx.execute("SELECT COUNT(*) FROM approvals WHERE conversation_id=? AND state='pending'",
                                 (conversation_id,)).fetchone()[0]
        tx.execute("INSERT INTO changes(conversation_id,message_id,state,pending_approvals,ts) VALUES (?,?,?,?,?)",
                   (conversation_id, message_id, state, pending, utcnow()))

    def changes_after(self, after: int, *, limit: int = 500) -> dict:
        rows = self.query("SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (after, max(1, min(limit, 1000))))
        return {"changes": rows, "next": rows[-1]["seq"] if rows else after}

    def wait(self, predicate, timeout_s: float) -> bool:
        """Wake when the store changes (or on timeout), for long polls."""
        deadline = time.monotonic() + timeout_s
        with self.changed:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.changed.wait(min(remaining, 0.5))
        return True


def _decode_conversation(row: dict) -> dict:
    out = dict(row)
    out["settings"] = json.loads(out.pop("settings_json"))
    out["handoff_from"] = json.loads(out.pop("handoff_from_json")) if out.get("handoff_from_json") else None
    out["allow_main"] = bool(out["allow_main"])
    return out


def _decode_message(row: dict) -> dict:
    out = dict(row)
    out["settings"] = json.loads(out.pop("settings_json"))
    out["attachments"] = json.loads(out.pop("attachments_json"))
    out["served"] = json.loads(out.pop("served_json")) if out.get("served_json") else None
    out["resolution"] = json.loads(out.pop("resolution_json")) if out.get("resolution_json") else None
    return out


def _decode_trees(row: dict) -> dict:
    out = dict(row)
    out["writable"] = bool(out["writable"])
    return out


def _decode_approval(row: dict) -> dict:
    out = dict(row)
    out["display"] = json.loads(out.pop("display_json"))
    out["options"] = json.loads(out.pop("options_json"))
    out["decision"] = json.loads(out.pop("decision_json")) if out.get("decision_json") else None
    return out
