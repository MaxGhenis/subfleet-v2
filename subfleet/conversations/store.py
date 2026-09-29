"""The conversation store: `<state root>/conversations.sqlite3` (C-24, design D-4, §3).

Written only by the daemon, through its own connection and lock, so streamed
events never contend for the job store's lock and `state.sqlite3` keeps the
schema every retained release can open. The job store stays the source of
truth for jobs and attempts; a message's `job_id` is a binding repaired from
request ids (`turn:<message id>:<n>`) whenever it is missing.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from ..lockwatch import WatchedLock
from ..sessions.transcripts import NotRegularFile
from ..state_files import read_state
from .turn import CANCELLED, LIVE_STATES, MESSAGE_STATES, PERMISSIONS, QUEUED, TERMINAL_STATES, WAITING

SCHEMA_VERSION = 2
PROVIDERS = ("claude", "codex")
EVENT_ROW_MAX = 64 * 1024
#: A message's text, in UTF-8 bytes (C-24.3, `LIMITS['message_bytes']`).
TEXT_MAX = 1_048_576
PAGE_BYTES = 256 * 1024
# Streamed fragments a settled turn no longer needs: its `text` and `thinking`
# events carry the whole of each (design §3).
DELTA_KINDS = ("text.delta", "thinking.delta")

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
  worktree_json     TEXT,
  request_id        TEXT UNIQUE,
  blocked_by        TEXT,
  legacy_hold       TEXT,
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
  ts TEXT NOT NULL, state_reason TEXT
);
CREATE TABLE IF NOT EXISTS legacy_sessions (
  session_key TEXT PRIMARY KEY,   -- "<provider>:<native id>", or "*" for every session
  reason      TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_retirement (retired_at TEXT NOT NULL);
"""

# Schema 2 (C-26.14, design D-25): a turn's working-tree snapshots, one row per
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


def canonical_native(session_id: Any) -> Any:
    """A native session id as a conversation binds it: a UUID in lower case,
    anything else as given (review L1). Claude Code names a transcript by the
    lower-case id, and `UNIQUE (provider, native_session_id)` and the `native:`
    lease compare ids exactly, so one session has one spelling here."""
    try:
        parsed = str(uuid.UUID(session_id))
    except (ValueError, AttributeError, TypeError):
        return session_id
    return parsed if parsed == session_id.lower() else session_id


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

#: The legacy import's hold (C-30.4, design D-17): the legacy cockpit may be using
#: the conversation's session. It lives in its own column, `legacy_hold`, beside
#: the service's single `blocked_by`, so no outcome replaces it and no
#: `conversation.unblock` or `message.resolve` lifts it; only the import sets or
#: clears it (`set_legacy_hold`).
LEGACY_OWNER = "legacy-owner"

#: C-24.5: a conversation gets no turn while either block is set.
UNBLOCKED = "c.blocked_by IS NULL AND c.legacy_hold IS NULL"

PERMISSION_ORDER = {"read-only": 0, "ask": 1, "accept-edits": 2, "bypass": 3}


def widens(before: dict, after: dict) -> bool:
    """A settings change a person must confirm (C-25.6, design D-9)."""
    return PERMISSION_ORDER[after["permission"]] > PERMISSION_ORDER[before["permission"]]


def message_digest(conversation_id: str, text: str, attachments: list[str], settings: dict) -> str:
    body = {"conversation_id": conversation_id, "text": text, "attachments": list(attachments),
            "settings": settings}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _publish(path: Path, data: bytes) -> None:
    """Write `path` by temp, fsync, rename, directory fsync (C-8.1). Its directory
    must exist: `ConversationStore._publish` makes it, and never the state root."""
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
        self._lock = WatchedLock("conversations")      # C-3.6
        # Held by a file write under the state root (`writing`) and by close(), taken
        # before `_lock`: close() waits for a write under way, and none starts after it.
        # Watched like `_lock` (C-3.6): a write can hold it across two fsyncs.
        self._writes = WatchedLock("conversation-files")
        self._closed = False
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
            # Additive columns need no numbered step: a build without them still reads the
            # rows (it selects by name and ignores what it does not know).
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(conversations)")}
            if "worktree_json" not in columns:
                self._db.execute("ALTER TABLE conversations ADD COLUMN worktree_json TEXT")
            # A waiting message's reason (another writer, a deferral) reaches the
            # app with its state (design §12).
            if "state_reason" not in {row["name"] for row in self._db.execute("PRAGMA table_info(changes)")}:
                self._db.execute("ALTER TABLE changes ADD COLUMN state_reason TEXT")
            self._add_turn_windows()
            if version is None:
                self._db.execute("INSERT INTO schema_version VALUES (?,?)", (SCHEMA_VERSION, utcnow()))
            self._add_legacy_hold()
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

    def _add_turn_windows(self) -> None:
        """C-26.14 (2026-09-29): who else wrote in a turn's folder during it. Additive
        columns, as `worktree_json`: `target` (the folder, C-6.5's write target),
        `window_start` (when admission began the turn's start snapshot) and
        `shared_json` (the attempt ids of the other writable turns whose windows
        overlapped it). A row written before them keeps its workspace as its folder."""
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(turn_trees)")}
        for column in ("target", "window_start", "shared_json"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE turn_trees ADD COLUMN {column} TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS turn_trees_by_target ON turn_trees(target, ended_at)")
        # Every open, not only the one that added the column: an open cut short
        # between the two statements leaves no row without its folder for long.
        self._db.execute("UPDATE turn_trees SET target=workspace WHERE target IS NULL")

    def _add_legacy_hold(self) -> None:
        """A schema 1 store written before `legacy_hold` gains the column (C-30.4).

        The column is nullable and additive: a build that predates it reads rows
        by name and ignores it. A store written then kept the legacy hold in
        `blocked_by`; that value moves to the column, since `blocked_by` is now
        the service's alone and nothing there would ever lift it.
        """
        self._db.execute("BEGIN IMMEDIATE")            # checked inside, so two openers cannot both add it
        try:
            if "legacy_hold" in {row["name"] for row in self._db.execute("PRAGMA table_info(conversations)")}:
                self._db.execute("COMMIT")
                return
            self._db.execute("ALTER TABLE conversations ADD COLUMN legacy_hold TEXT")
            self._db.execute("UPDATE conversations SET legacy_hold=?, blocked_by=NULL WHERE blocked_by=?",
                             (f"held {LEGACY_OWNER} by an earlier import", LEGACY_OWNER))
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def close(self) -> None:
        """A file write under way (`writing`) finishes first. Every read, write and
        file write after this is refused (`store-closed`): a turn runner still going
        when its service closed wrote into the state root after its owner had removed
        it, and made the root again (C-25.3)."""
        with self._writes, self._lock:
            self._closed = True
            self._db.close()

    def _open(self) -> None:
        if self._closed:
            raise ConversationError("store-closed", "the conversation store is closed", code=1)

    def check_open(self) -> None:
        """Raise `store-closed` once the store has closed: for a write elsewhere
        (the main store) that must end with this store, as a runner's own do."""
        self._open()

    @contextlib.contextmanager
    def writing(self) -> Iterator[None]:
        """Hold the store open across a write of files under the state root: close()
        waits for one under way and none starts after it, so none lands in a root its
        owner removes once close() has returned. The write makes its directories with
        `subdirectory`, never the root itself."""
        with self._writes:
            self._open()
            yield

    def subdirectory(self, name: str | Path) -> Path:
        """`<root>/<name>`, each missing level made in turn, for the files an op writes
        there. Never the root itself: a write that outlived its owner's close() made
        the removed state root again (review of #47)."""
        parts = Path(name).parts
        if not parts or Path(name).is_absolute() or ".." in parts:
            raise ValueError(f"{name!r} is not a directory below the state root")
        path = self.root
        for part in parts:
            path = path / part
            try:
                path.mkdir(mode=0o700, exist_ok=True)
            except FileNotFoundError:
                raise ConversationError("state-root-gone", f"the state root {self.root} is gone", code=1) from None
        return path

    def _publish(self, path: Path, data: bytes) -> None:
        """Publish one of the store's files (a message's text, an approval's request)
        while the store is open (`writing`), making its directory below the root."""
        with self.writing():
            self.subdirectory(path.parent.relative_to(self.root))
            _publish(path, data)

    # --- plumbing --------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        self.notify()

    def notify(self) -> None:
        with self.changed:
            self.changed.notify_all()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            self._open()
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

    def bound_sessions(self) -> set[str]:
        """C-26.13: every native session id a conversation binds, archived or not.

        Archiving stops a conversation's dispatch but keeps its row and its
        session, so an archived conversation still owns the session.
        """
        return {row["native_session_id"] for row in self.query(
            "SELECT native_session_id FROM conversations WHERE native_session_id IS NOT NULL")
                if row["native_session_id"]}

    def binding(self, native_session_id: str) -> str | None:
        """The conversation that binds `native_session_id`, if one does (C-26.13), in
        whatever case its UUID was spelled when bound or asked about (review L1)."""
        match, params = native_any_case("native_session_id", native_session_id)
        row = self.one(f"SELECT conversation_id FROM conversations WHERE provider IN ('claude','codex') "
                       f"AND {match} ORDER BY created_at LIMIT 1", params)
        return row["conversation_id"] if row else None

    def by_native(self, provider: str, native_session_id: str) -> dict | None:
        """The conversation bound to a native session, in whatever case its UUID
        was spelled when it was bound (a store written before ids were canonical)."""
        row = self.one(f"SELECT * FROM conversations WHERE {_NATIVE_MATCH}",
                       _native_params(provider, native_session_id))
        return _decode_conversation(row) if row else None

    def create_conversation(self, *, provider: str, workspace: str, workspace_kind: str, settings: dict,
                            origin: str, native_session_id: str | None = None, title: str | None = None,
                            allow_main: bool = False, lane_id: str | None = None,
                            request_id: str | None = None, handoff_from: dict | None = None) -> tuple[dict, bool]:
        if provider not in PROVIDERS:
            raise ConversationError("bad-provider", "provider must be claude or codex")
        settings = validate_settings(provider, settings)
        native_session_id = canonical_native(native_session_id)
        now = utcnow()
        held = None
        with self.transaction() as tx:
            if request_id:
                existing = tx.execute("SELECT * FROM conversations WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    return _decode_conversation(dict(existing)), False
            if native_session_id:
                existing = tx.execute(f"SELECT * FROM conversations WHERE {_NATIVE_MATCH}",
                                      _native_params(provider, native_session_id)).fetchone()
                if existing:
                    return _decode_conversation(dict(existing)), False
                # C-30.4: a session the last legacy pass found held is bound held.
                row = tx.execute("SELECT reason FROM legacy_sessions WHERE session_key IN (?, '*') "
                                 "ORDER BY session_key='*' LIMIT 1", (f"{provider}:{native_session_id}",)).fetchone()
                held = row["reason"] if row else None
            cid = new_id("cv")
            tx.execute(
                "INSERT INTO conversations(conversation_id,provider,native_session_id,title,workspace,workspace_kind,"
                "allow_main,lane_id,settings_json,origin,handoff_from_json,request_id,legacy_hold,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, provider, native_session_id, title, workspace, workspace_kind, int(allow_main), lane_id,
                 json.dumps(settings), origin, json.dumps(handoff_from) if handoff_from else None, request_id,
                 held, now, now))
            self._change(tx, cid, None, None)
        return self.conversation(cid), True

    def by_request(self, request_id: str) -> dict | None:
        row = self.one("SELECT * FROM conversations WHERE request_id=?", (request_id,))
        return _decode_conversation(row) if row else None

    def create_handoff(self, *, request_id: str, provider: str, workspace: str, settings: dict,
                       title: str | None, allow_main: bool, handoff_from: dict, brief: dict,
                       moves: list[dict], withdrawals: list[dict]) -> tuple[dict, bool]:
        """C-30.3, IR-28: a labelled handoff with nothing to fence, prepared and
        committed in one call (`prepare_handoff`, then `commit_handoff`)."""
        prepared = self.prepare_handoff(request_id=request_id, provider=provider, workspace=workspace,
                                        settings=settings, title=title, allow_main=allow_main,
                                        handoff_from=handoff_from, brief=brief, moves=moves)
        if prepared.get("existing"):
            return prepared["existing"], False
        return self.commit_handoff(prepared, withdrawals=withdrawals)

    def prepare_handoff(self, *, request_id: str, provider: str, workspace: str, settings: dict,
                        title: str | None, allow_main: bool, handoff_from: dict, brief: dict,
                        moves: list[dict]) -> dict:
        """C-30.3, IR-28: everything about a handoff that can fail before its commit.

        Checks each text and each moved attachment and publishes the texts
        (C-24.3), so a handoff that must cancel a waiting message's job does so
        only once nothing but the commit is left (design D-18). `brief` is
        `{message_id, text}`; `moves` are `{message_id, text, attachments}` in
        order. A request id already used returns `{"existing": conversation}`.
        The result goes to `commit_handoff`, or to `discard_handoff` when the
        handoff stops before its commit.
        """
        if provider not in PROVIDERS:
            raise ConversationError("bad-provider", "provider must be claude or codex")
        settings = validate_settings(provider, settings)
        existing = self.by_request(request_id)
        if existing:
            return {"existing": existing}
        from .attachments import check as check_attachment
        for item in moves:
            for sha in item["attachments"]:
                if self.attachment(sha) is None:
                    raise ConversationError("unknown-attachment", f"no attachment {sha}")
                # The stored copy must be there and hash right now, before the
                # source's job is cancelled: a damaged one would fail the target's
                # turn after the source had been withdrawn (review of 6290a51).
                check_attachment(self, sha)
        cid = new_id("cv")
        now = utcnow()
        prepared = {"request_id": request_id, "cid": cid, "provider": provider, "workspace": workspace,
                    "settings": settings, "title": title, "allow_main": allow_main, "handoff_from": handoff_from,
                    "moves": moves, "rows": [], "published": [], "now": now}
        after: str | None = None
        try:
            for index, item in enumerate([{**brief, "origin": "handoff", "attachments": []},
                                          *({**m, "origin": "person"} for m in moves)]):
                message_id = canonical_uuid(item["message_id"])
                text = item["text"]
                if not isinstance(text, str) or len(text.encode("utf-8")) > TEXT_MAX:
                    raise ConversationError("bad-text", "a handed-off message must be text of at most 1 MiB")
                digest = message_digest(cid, text, list(item["attachments"]), settings)
                path = self.dir / cid / "messages" / f"{message_id}.{digest[:16]}.md"
                self._publish(path, text.encode("utf-8"))     # C-24.3: text before the row
                prepared["published"].append(path)
                prepared["rows"].append((message_id, cid, index + 1, after if item["origin"] == "person" else None,
                                         item["origin"], digest, str(path), json.dumps(list(item["attachments"])),
                                         json.dumps(settings), QUEUED, now, now))
                if item["origin"] == "person":
                    after = message_id
        except BaseException:
            self.discard_handoff(prepared)
            raise
        return prepared

    def discard_handoff(self, prepared: dict) -> None:
        """Remove the texts a handoff published and never committed: nothing refers to them."""
        if prepared.get("committed"):
            return
        # COMMIT may have succeeded before a notification or result read failed.
        # The durable row wins even if the caller never recorded its success.
        if prepared.get("cid") and self.one("SELECT 1 FROM conversations WHERE conversation_id=?", (prepared["cid"],)):
            return
        for path in prepared.get("published", []):
            with contextlib.suppress(OSError):
                path.unlink()
        if prepared.get("cid"):
            for directory in (self.dir / prepared["cid"] / "messages", self.dir / prepared["cid"]):
                with contextlib.suppress(OSError):
                    directory.rmdir()

    def commit_handoff(self, prepared: dict, *, withdrawals: list[dict],
                       fence: tuple[str, str] | None = None) -> tuple[dict, bool]:
        """C-30.3, IR-28: a prepared handoff, committed in one transaction.

        The new conversation (origin `handoff`), its first message (the brief,
        origin `handoff`), the source's moved messages re-queued behind it in
        their order, each source message's withdrawal (`cancelled`,
        `handed-off:<new conversation>`), and, with `fence` (`(source id,
        blocked_by value)`), the lifting of the source's handoff fence.
        `withdrawals` are `{message_id, expect}`, where `expect` is the states
        the message may still be in. A withdrawal that no longer matches (the
        dispatcher claimed the message, or a person withdrew it, meanwhile), or a
        fence no longer in place, rolls this transaction back: exit 2
        `source-changed`. A job the handoff already cancelled is not this
        transaction's to roll back; the caller puts its message back
        (`restore_after_handoff`). A request id already used returns that
        conversation. The published texts are removed unless the commit lands.
        """
        cid, now, request_id = prepared["cid"], prepared["now"], prepared["request_id"]
        created = False
        existing = None
        try:
            with self.transaction() as tx:
                again = tx.execute("SELECT * FROM conversations WHERE request_id=?", (request_id,)).fetchone()
                if again:
                    existing = _decode_conversation(dict(again))
                else:
                    reason = f"handed-off:{cid}"
                    for withdrawal in withdrawals:
                        expect = tuple(withdrawal["expect"])
                        # A message with no job leaves only while still unbound and not
                        # claimed by the dispatcher (C-24.7).
                        guard = " AND job_id IS NULL" if withdrawal.get("unbound") else ""
                        guard_params: tuple = ()
                        if withdrawal.get("not_reason"):
                            guard += " AND COALESCE(state_reason,'')<>?"
                            guard_params = (withdrawal["not_reason"],)
                        cur = tx.execute(
                            f"UPDATE messages SET state='cancelled', state_reason=?, updated_at=? WHERE message_id=? "
                            f"AND state IN ({','.join('?' * len(expect))}){guard}",
                            (reason, now, withdrawal["message_id"], *expect, *guard_params))
                        if not cur.rowcount:
                            raise ConversationError(
                                "source-changed", f"message {withdrawal['message_id']} changed state during the handoff",
                                fix="send the same handoff request again")
                        source = tx.execute("SELECT conversation_id FROM messages WHERE message_id=?",
                                            (withdrawal["message_id"],)).fetchone()
                        self._change(tx, source["conversation_id"], withdrawal["message_id"], "cancelled")
                    if fence is not None:
                        lifted = tx.execute("UPDATE conversations SET blocked_by=NULL, updated_at=? "
                                            "WHERE conversation_id=? AND blocked_by=?", (now, *fence)).rowcount
                        if not lifted:
                            raise ConversationError("source-changed", "the source's handoff fence was lifted meanwhile",
                                                    fix="send the same handoff request again")
                        self._change(tx, fence[0], None, None)
                    tx.execute(
                        "INSERT INTO conversations(conversation_id,provider,native_session_id,title,workspace,"
                        "workspace_kind,allow_main,lane_id,settings_json,origin,handoff_from_json,request_id,"
                        "created_at,updated_at) VALUES (?,?,NULL,?,?,'in-place',?,NULL,?,'handoff',?,?,?,?)",
                        (cid, prepared["provider"], prepared["title"], prepared["workspace"],
                         int(prepared["allow_main"]), json.dumps(prepared["settings"]),
                         json.dumps(prepared["handoff_from"]), request_id, now, now))
                    for item in prepared["moves"]:
                        for sha in item["attachments"]:
                            if not tx.execute("SELECT 1 FROM attachments WHERE sha256=?", (sha,)).fetchone():
                                raise ConversationError("unknown-attachment", f"no attachment {sha}")
                            tx.execute("UPDATE attachments SET last_used_at=? WHERE sha256=?", (now, sha))
                    tx.executemany(
                        "INSERT INTO messages(message_id,conversation_id,seq,after_message_id,origin,digest,text_path,"
                        "attachments_json,settings_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        prepared["rows"])
                    self._change(tx, cid, None, None)
                    for row in prepared["rows"]:
                        self._change(tx, cid, row[0], QUEUED)
                    created = True
            prepared["committed"] = created
        finally:
            self.discard_handoff(prepared)
        if not created:
            return existing, False
        return self.conversation(cid), True

    def fence(self, conversation_id: str, value: str) -> bool:
        """Block an unblocked conversation with `value` (a handoff's `handoff:<request
        id>`), durably: the dispatcher skips a blocked conversation (C-24.5). False
        when it is already blocked, and then nothing changed."""
        with self.transaction() as tx:
            done = tx.execute("UPDATE conversations SET blocked_by=?, updated_at=? WHERE conversation_id=? "
                              "AND blocked_by IS NULL", (value, utcnow(), conversation_id)).rowcount
            if done:
                self._change(tx, conversation_id, None, None)
        return bool(done)

    def restore_after_handoff(self, conversation_id: str, fence: str, restores: list[dict]) -> list[str]:
        """Undo what a handoff that never committed did to its source (C-30.3, D-18).

        Each `{message_id, job_id, turn_seq}` in `restores` names a message whose turn job
        the handoff cancelled: it goes back to `queued` in its place, under the
        next turn sequence (so its next turn job is a new one) and bound to no
        job, unless a handoff did move it. Then the fence is lifted. One
        transaction; returns the ids put back.
        """
        now = utcnow()
        restored: list[str] = []
        with self.transaction() as tx:
            source = tx.execute("SELECT blocked_by FROM conversations WHERE conversation_id=?",
                                (conversation_id,)).fetchone()
            if source is None or source["blocked_by"] not in (None, fence):
                return restored
            for item in restores:
                done = tx.execute(
                    "UPDATE messages SET state='queued', state_reason='handoff-rolled-back', turn_seq=turn_seq+1, "
                    "job_id=NULL, updated_at=? WHERE message_id=? AND conversation_id=? AND turn_seq=? "
                    "AND (job_id IS NULL OR job_id=?) AND state IN ('queued','waiting','cancelled') "
                    "AND COALESCE(state_reason,'') NOT LIKE 'handed-off:%'",
                    (now, item["message_id"], conversation_id, item["turn_seq"], item["job_id"])).rowcount
                if done:
                    restored.append(item["message_id"])
                    self._change(tx, conversation_id, item["message_id"], QUEUED)
            if tx.execute("UPDATE conversations SET blocked_by=NULL, updated_at=? WHERE conversation_id=? "
                          "AND blocked_by=?", (now, conversation_id, fence)).rowcount:
                self._change(tx, conversation_id, None, None)
        return restored

    def update_conversation(self, conversation_id: str, **fields: Any) -> dict:
        allowed = {"native_session_id", "title", "lane_id", "settings", "blocked_by", "allow_main", "archived_at",
                   "workspace", "worktree"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown conversation fields {sorted(unknown)}")
        if "workspace" in fields and (not isinstance(fields["workspace"], str) or not fields["workspace"]):
            raise ValueError("a conversation's workspace is a directory path")
        sets, params = [], []
        for key, value in fields.items():
            if key == "native_session_id":
                value = canonical_native(value)
            if key == "settings":
                sets.append("settings_json=?")
                params.append(json.dumps(value))
            elif key == "worktree":
                sets.append("worktree_json=?")
                params.append(json.dumps(value, sort_keys=True) if value is not None else None)
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

    def set_legacy_hold(self, conversation_id: str, reason: str | None) -> dict:
        """Set (a reason) or lift (None) the legacy import's hold (C-30.4).

        Only `legacy.fence_bound_sessions` calls this; `update_conversation`
        cannot name the column, so no service path can clear it.
        """
        with self.transaction() as tx:
            tx.execute("UPDATE conversations SET legacy_hold=?, updated_at=? WHERE conversation_id=?",
                       (reason, utcnow(), conversation_id))
            self._change(tx, conversation_id, None, None)
        return self.conversation(conversation_id)

    def record_legacy_sessions(self, holds: dict[str, str]) -> None:
        """The sessions the last legacy pass found held, by `legacy.session_key`
        (`*`: every session), replacing what an earlier pass recorded (C-30.4).
        A conversation bound later to one of them is bound held
        (`create_conversation`), so opening a held session after the pass never
        starts a turn in it."""
        now = utcnow()
        with self.transaction() as tx:
            tx.execute("DELETE FROM legacy_sessions")
            tx.executemany("INSERT INTO legacy_sessions(session_key,reason,recorded_at) VALUES (?,?,?)",
                           [(key, reason, now) for key, reason in sorted(holds.items())])

    def retire_legacy(self) -> list[dict]:
        """The legacy cockpit will never run again (C-30.4): in one transaction,
        lift every legacy hold, forget every held session and record the
        retirement, so later passes read nothing from it and a run that dies
        midway changes nothing. Returns the conversations it released, as they
        were."""
        now = utcnow()
        with self.transaction() as tx:
            released = [dict(row) for row in tx.execute(
                "SELECT * FROM conversations WHERE legacy_hold IS NOT NULL ORDER BY created_at, conversation_id")]
            tx.execute("UPDATE conversations SET legacy_hold=NULL, updated_at=? WHERE legacy_hold IS NOT NULL", (now,))
            for row in released:
                self._change(tx, row["conversation_id"], None, None)
            tx.execute("DELETE FROM legacy_sessions")
            if tx.execute("SELECT 1 FROM legacy_retirement").fetchone() is None:
                tx.execute("INSERT INTO legacy_retirement(retired_at) VALUES (?)", (now,))
        return released

    def legacy_retired_at(self) -> str | None:
        row = self.one("SELECT retired_at FROM legacy_retirement")
        return row["retired_at"] if row else None

    def turn_hold(self, conversation_id: str) -> dict | None:
        """Why no turn may run in a conversation now (C-24.5): its `blocked_by`
        and `legacy_hold`, or None when neither is set."""
        row = self.one("SELECT blocked_by, legacy_hold FROM conversations WHERE conversation_id=?",
                       (conversation_id,))
        if row is None or (row["blocked_by"] is None and row["legacy_hold"] is None):
            return None
        return {"blocked_by": row["blocked_by"], "legacy_hold": row["legacy_hold"]}

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
                       continues: str | None = None, state: str = QUEUED,
                       state_reason: str | None = None) -> tuple[dict, bool]:
        """Durably accept a message (C-24.2, C-24.3). Idempotent by id and digest;
        in order by the client's predecessor. A row Subfleet writes already
        settled (a withdrawal tombstone, IR-7) is born in its final `state`, so
        the dispatcher never sees it queued."""
        if state not in (QUEUED, *TERMINAL_STATES):
            raise ValueError(f"a message is accepted queued or terminal, not {state}")
        message_id = canonical_uuid(message_id)
        if after_message_id is not None:
            after_message_id = canonical_uuid(after_message_id)
        conversation = self.conversation(conversation_id)
        settings = validate_settings(conversation["provider"], settings)
        if not isinstance(text, str) or len(text.encode("utf-8")) > TEXT_MAX:
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
        self._publish(text_path, text.encode("utf-8"))      # C-24.3: text before the row
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
                "attachments_json,settings_json,state,state_reason,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, conversation_id, seq, after_message_id, origin, continues, digest, str(text_path),
                 json.dumps(list(attachments)), json.dumps(settings), state, state_reason, now, now))
            tx.execute("UPDATE conversations SET updated_at=?, settings_json=? WHERE conversation_id=?",
                       (now, json.dumps(settings), conversation_id))
            self._change(tx, conversation_id, message_id, state)
        return self.message(message_id), True

    def insert_legacy_history(self, *, conversation_id: str, message_id: str, text: str, state: str,
                              state_reason: str | None, settings: dict, created_at: str, updated_at: str,
                              turn_ref: str | None = None) -> tuple[dict, bool]:
        """One legacy cockpit message as read-only history (C-30.4, design §13).

        The row keeps the legacy message id, has origin `legacy`, a terminal state,
        no job, no predecessor and turn sequence 0. A state that is not terminal is
        refused, so a history row is never `queued`: the dispatcher, the
        re-admission pass and the settle pass select only queued, waiting or
        starting rows, and every `set_state` that moves a message names the live
        states it expects. History goes ahead of every other message: a
        conversation holding any message of another origin refuses it, so a
        history row never becomes a conversation's latest message after a turn
        Subfleet ran. Idempotent by id: the same id, conversation and content
        returns the stored row with `created: false`; anything else is
        `message-id-conflict`. The text is published before the row, as
        `submit_message` publishes it (C-24.3).
        """
        message_id = canonical_uuid(message_id)
        if state not in TERMINAL_STATES:
            raise ConversationError("not-terminal", f"legacy history is terminal; {state!r} is not")
        if not isinstance(text, str) or len(text.encode("utf-8")) > TEXT_MAX:
            raise ConversationError("bad-text", "text must be a string of at most 1 MiB")
        if not isinstance(settings, dict):
            raise ConversationError("bad-settings", "settings must be an object")
        self.conversation(conversation_id)
        digest = message_digest(conversation_id, text, [], settings)

        def same(row) -> dict:
            if row["origin"] != "legacy" or row["conversation_id"] != conversation_id or row["digest"] != digest:
                raise ConversationError("message-id-conflict",
                                        "message id already used with different content or conversation")
            return _decode_message(dict(row))

        existing = self.one("SELECT * FROM messages WHERE message_id=?", (message_id,))
        if existing:
            return same(existing), False
        if self.one("SELECT 1 FROM messages WHERE conversation_id=? AND origin<>'legacy' LIMIT 1", (conversation_id,)):
            raise ConversationError("history-after-messages",
                                    "the conversation already has messages of its own; history goes first")
        text_path = self.dir / conversation_id / "messages" / f"{message_id}.{digest[:16]}.md"
        self._publish(text_path, text.encode("utf-8"))      # C-24.3: text before the row
        with self.transaction() as tx:
            again = tx.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if again:
                return same(again), False
            if tx.execute("SELECT 1 FROM messages WHERE conversation_id=? AND origin<>'legacy' LIMIT 1",
                          (conversation_id,)).fetchone():
                raise ConversationError("history-after-messages",
                                        "the conversation already has messages of its own; history goes first")
            seq = (tx.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE conversation_id=?",
                              (conversation_id,)).fetchone()[0]) + 1
            tx.execute(
                "INSERT INTO messages(message_id,conversation_id,seq,after_message_id,origin,continues,digest,text_path,"
                "attachments_json,settings_json,state,state_reason,turn_seq,job_id,turn_ref,created_at,updated_at) "
                "VALUES (?,?,?,NULL,'legacy',NULL,?,?,'[]',?,?,?,0,NULL,?,?,?)",
                (message_id, conversation_id, seq, digest, str(text_path), json.dumps(settings), state, state_reason,
                 turn_ref, created_at, updated_at))
            self._change(tx, conversation_id, message_id, state)
        return self.message(message_id), True

    def message(self, message_id: str) -> dict:
        message = self.find_message(message_id)
        if message is None:
            raise ConversationError("unknown-message", f"no message {message_id}")
        return message

    def find_message(self, message_id: str) -> dict | None:
        """The message, or None when the store has none by that id. Any other
        refusal (`store-closed`) is raised: it says nothing of the message."""
        row = self.one("SELECT * FROM messages WHERE message_id=?", (message_id,))
        return None if row is None else _decode_message(row)

    def messages(self, conversation_id: str, *, limit: int = 50) -> list[dict]:
        rows = self.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?",
                          (conversation_id, max(1, min(limit, 500))))
        return [_decode_message(r) for r in reversed(rows)]

    def message_text(self, message: dict) -> str:
        """The text accepted with this message (C-24.3), read through a private,
        regular, no-follow descriptor and checked against the message digest.
        Check its original bytes before preserving read_text's newline handling.
        """
        text = read_state(message["text_path"], limit=TEXT_MAX, private=True).decode("utf-8")
        digest = message_digest(message["conversation_id"], text, message["attachments"], message["settings"])
        if digest != message["digest"]:
            raise OSError(errno.EIO, "message text does not match its accepted digest", message["text_path"])
        return text.replace("\r\n", "\n").replace("\r", "\n")      # as `read_text` gave it

    def set_state(self, message_id: str, state: str, *, reason: str | None = None,
                  expect: tuple[str, ...] | None = None, unbound: bool = False,
                  expect_turn_seq: int | None = None, **fields: Any) -> bool:
        """Move a message; with `expect`, only from those states, with `unbound`,
        only while no job is bound to it, and with `expect_turn_seq`, only at that
        turn sequence. Returns whether it moved."""
        if state not in MESSAGE_STATES:
            raise ValueError(f"unknown state {state}")
        if state == WAITING and not reason:
            # C-24.4, I3: a waiting message always says why; the app never guesses
            # (it had shown "Waiting for capacity" for a lease another turn held).
            raise ValueError("a waiting message needs its reason")
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
        if unbound:
            where += " AND job_id IS NULL"
        if expect_turn_seq is not None:
            where += " AND turn_seq=?"
            wparams.append(expect_turn_seq)
        with self.transaction() as tx:
            cur = tx.execute(f"UPDATE messages SET {','.join(sets)} WHERE {where}", (*params, *wparams))
            if cur.rowcount:
                row = tx.execute("SELECT conversation_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
                self._change(tx, row["conversation_id"], message_id, state, reason=reason)
            return bool(cur.rowcount)

    def note_wait(self, message_id: str, job_id: str, reason: str) -> bool:
        """C-24.4, I3: why a waiting message's turn job is not placed yet, written while
        that job still carries it and only when it changes, with a change-feed row so
        the app shows it. Returns whether it changed."""
        if not reason:
            raise ValueError("a waiting message needs its reason")
        with self.transaction() as tx:
            cur = tx.execute("UPDATE messages SET state_reason=?, updated_at=? WHERE message_id=? AND state=? "
                             "AND job_id=? AND COALESCE(state_reason,'')<>?",
                             (reason, utcnow(), message_id, WAITING, job_id, reason))
            if cur.rowcount:
                row = tx.execute("SELECT conversation_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
                self._change(tx, row["conversation_id"], message_id, WAITING, reason=reason)
            return bool(cur.rowcount)

    def withdraw(self, message_id: str, *, expect: tuple[str, ...], stop_at: str, unbound: bool = False) -> bool:
        """A person's withdrawal of a message (C-24.7): `cancelled`, reason
        `withdrawn`, only from `expect` and, with `unbound`, only while no job is
        bound to it, with the person's stop recorded in the same statement (an
        earlier one kept). Returns whether it moved; when it did not, nothing
        changed, so no stop was recorded that a runner could act on."""
        where, params = f"message_id=? AND state IN ({','.join('?' * len(expect))})", [message_id, *expect]
        if unbound:
            where += " AND job_id IS NULL"
        with self.transaction() as tx:
            cur = tx.execute(f"UPDATE messages SET state=?, state_reason=?, updated_at=?, "
                             f"stop_requested_at=COALESCE(stop_requested_at, ?) WHERE {where}",
                             (CANCELLED, "withdrawn", utcnow(), stop_at, *params))
            if cur.rowcount:
                row = tx.execute("SELECT conversation_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
                self._change(tx, row["conversation_id"], message_id, CANCELLED, reason="withdrawn")
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

    def next_dispatchable(self, conversation_id: str | None = None) -> list[dict]:
        """For each unblocked conversation with no live message, its next queued one
        (C-24.5): a repair message first (an unblock note, a failover continuation;
        C-24.8, C-26.7), since the person's queued messages were written expecting
        it; otherwise the lowest sequence."""
        repair = ",".join(f"'{origin}'" for origin in REPAIR_ORIGINS)
        source = "AND m.conversation_id=? " if conversation_id is not None else ""
        rows = self.query(
            "SELECT m.* FROM messages m JOIN conversations c USING(conversation_id) "
            f"WHERE m.state='queued' AND {UNBLOCKED} AND c.archived_at IS NULL "
            f"{source}"
            "AND m.message_id = (SELECT q.message_id FROM messages q WHERE q.conversation_id=m.conversation_id "
            f"AND q.state='queued' ORDER BY q.origin IN ({repair}) DESC, q.seq LIMIT 1) "
            f"AND NOT EXISTS (SELECT 1 FROM messages l WHERE l.conversation_id=m.conversation_id AND l.state IN ({','.join('?' * len(LIVE_STATES))})) "
            "ORDER BY m.created_at", ((conversation_id,) if conversation_id is not None else ()) + LIVE_STATES)
        return [_decode_message(r) for r in rows]

    def readmittable(self) -> list[dict]:
        """Waiting messages whose turn is re-admitted (`readmit:*`, design D-12),
        of conversations that are not blocked (C-24.5)."""
        rows = self.query("SELECT m.* FROM messages m JOIN conversations c USING(conversation_id) "
                          f"WHERE m.state='waiting' AND m.state_reason LIKE 'readmit:%' AND {UNBLOCKED} "
                          "ORDER BY m.created_at")
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
        self._publish(path, raw)
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
                       "ON CONFLICT(sha256) DO UPDATE SET path=excluded.path, last_used_at=excluded.last_used_at",
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
            mark = tx.execute("SELECT compacted FROM attempt_marks WHERE attempt_id=?", (attempt_id,)).fetchone()
            compacted = bool(mark and mark["compacted"])
            for source, position, ordinal, kind, data in events:
                if compacted and kind in DELTA_KINDS:
                    continue            # removed by compaction; a replay never brings them back
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

    def floor(self, conversation_id: str) -> int:
        """The highest event sequence number compaction removed, or 0 (C-25.4)."""
        row = self.one("SELECT compacted_through FROM floors WHERE conversation_id=?", (conversation_id,))
        return int(row["compacted_through"]) if row else 0

    def events_after(self, conversation_id: str, after: int, *, limit: int = 500,
                     max_bytes: int = PAGE_BYTES) -> dict:
        floor = self.floor(conversation_id)
        # C-25.4, IR-6: reset exactly when the cursor is below the floor. The row at
        # the floor was removed, so a cursor below it missed at least that row; a
        # cursor at or above it had read every removed row before it was removed.
        reset = after < floor
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
        return {"events": out, "next": nxt, "reset": reset, "floor": floor}

    def compact(self, attempt_id: str) -> int:
        """Remove an attempt's delta rows and raise its conversation's floor to the
        highest sequence number removed (design §3, C-25.4). Only for an attempt
        that has ended and whose message is settled: the caller checks (IR-6). The
        attempt is marked compacted even when it had no deltas, so it is visited once."""
        kinds = ",".join("?" * len(DELTA_KINDS))
        with self.transaction() as tx:
            row = tx.execute(f"SELECT conversation_id, MAX(seq) AS top FROM events WHERE attempt_id=? AND kind IN ({kinds})",
                             (attempt_id, *DELTA_KINDS)).fetchone()
            deleted = 0
            if row and row["top"] is not None:
                deleted = tx.execute(f"DELETE FROM events WHERE attempt_id=? AND kind IN ({kinds})",
                                     (attempt_id, *DELTA_KINDS)).rowcount
                tx.execute("INSERT INTO floors(conversation_id,compacted_through) VALUES (?,?) ON CONFLICT(conversation_id) "
                           "DO UPDATE SET compacted_through=MAX(compacted_through,excluded.compacted_through)",
                           (row["conversation_id"], row["top"]))
            tx.execute("UPDATE attempt_marks SET compacted=1 WHERE attempt_id=?", (attempt_id,))
            return deleted

    def compactable(self, *, settled_before: str, limit: int) -> list[dict]:
        """Attempts not yet compacted whose message reached a terminal state no later
        than `settled_before` (UTC ISO), oldest first. Whether the attempt itself has
        ended is the job store's to say; the caller asks it (IR-6)."""
        terminal = ",".join("?" * len(TERMINAL_STATES))
        return self.query(
            "SELECT k.attempt_id, k.message_id, m.conversation_id, m.state FROM attempt_marks k "
            f"JOIN messages m USING(message_id) WHERE k.compacted=0 AND m.state IN ({terminal}) "
            "AND m.updated_at<=? ORDER BY m.updated_at LIMIT ?", (*TERMINAL_STATES, settled_before, max(1, int(limit))))

    # --- a turn's snapshots (C-26.14, design D-25) ------------------------------

    def record_trees(self, *, attempt_id: str, message_id: str, conversation_id: str, workspace: str,
                     writable: bool, started_at: str, head_before: str | None = None, start_tree: str | None = None,
                     head_after: str | None = None, end_tree: str | None = None, error: str | None = None,
                     ended: bool = False, target: str | None = None, window_start: str | None = None) -> None:
        """Record a turn attempt's start, its end, or both. Idempotent: the start is
        written once, the end fills in; a replayed record changes nothing.

        `target` is the folder the turn writes in (C-6.5's write target; the
        workspace when not given) and `window_start` when its start snapshot began
        (`started_at` when not given). Every record also notes, in the same
        transaction, which other writable turns in that folder overlapped this one
        in time (`_note_overlaps`, C-26.14)."""
        with self.transaction() as tx:
            before = tx.execute("SELECT ended_at FROM turn_trees WHERE attempt_id=?", (attempt_id,)).fetchone()
            tx.execute(
                "INSERT INTO turn_trees(attempt_id,message_id,conversation_id,workspace,writable,head_before,start_tree,"
                "head_after,end_tree,error,started_at,ended_at,target,window_start) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(attempt_id) DO UPDATE SET "
                "head_before=COALESCE(turn_trees.head_before,excluded.head_before), "
                "start_tree=COALESCE(turn_trees.start_tree,excluded.start_tree), "
                "head_after=COALESCE(turn_trees.head_after,excluded.head_after), "
                "end_tree=COALESCE(turn_trees.end_tree,excluded.end_tree), "
                "error=COALESCE(turn_trees.error,excluded.error), "
                "ended_at=COALESCE(turn_trees.ended_at,excluded.ended_at), "
                "target=COALESCE(turn_trees.target,excluded.target), "
                "window_start=COALESCE(turn_trees.window_start,excluded.window_start)",
                (attempt_id, message_id, conversation_id, workspace, int(bool(writable)), head_before, start_tree,
                 head_after, end_tree, error, started_at, utcnow() if ended else None, target or workspace,
                 _stamp(window_start)))
            grown = self._note_overlaps(tx, attempt_id)
            if ended and not (before and before["ended_at"]):
                # One change-feed row with `state` null, as an approval writes: it
                # carries no kind, and tells a watcher to fetch this message again,
                # its `turn.diff` included (C-26.14, design D-24, §5).
                self._change(tx, conversation_id, message_id, None)
                grown.discard((conversation_id, message_id))
            for other_conversation, other_message in sorted(grown):
                # A turn found to share its folder: its changes now say so.
                self._change(tx, other_conversation, other_message, None)

    def _note_overlaps(self, tx: sqlite3.Connection, attempt_id: str) -> set[tuple[str, str]]:
        """C-26.14, I4: mark this writable turn and every writable turn of another
        conversation in its folder whose time window overlaps its own as sharing
        the folder, each in
        the other's `shared_json`. A window runs from `window_start` to `ended_at`,
        and is open while `ended_at` is null. Checked on every record, in the
        recording transaction, so whichever of two turns is recorded second sees
        the first: a pair is marked when its later row is written, whatever order
        the starts and ends arrive in (a start is recorded when its runner is
        adopted, possibly after another turn has ended). Marks are only ever
        added, since a window only ever gains its end. Returns the messages whose
        marks grew."""
        row = tx.execute("SELECT * FROM turn_trees WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or not row["writable"]:
            return set()
        start, end = _window(row)
        # `ended_at` is always this store's millisecond stamp, so the prefilter compares
        # like with like; the exact test is on parsed instants below.
        # Another conversation's: two turns of one conversation never run at once (C-24.5,
        # I2), each starting after the one before it has ended and been recorded.
        others = tx.execute("SELECT * FROM turn_trees WHERE target=? AND conversation_id<>? AND writable=1 "
                            "AND (ended_at IS NULL OR ended_at>=?)",
                            (row["target"], row["conversation_id"], _stamp(start))).fetchall()
        mine = set(json.loads(row["shared_json"] or "[]"))
        grown: set[tuple[str, str]] = set()
        for other in others:
            other_start, other_end = _window(other)
            if not overlaps(start, end, other_start, other_end):
                continue
            theirs = set(json.loads(other["shared_json"] or "[]"))
            if attempt_id not in theirs:
                tx.execute("UPDATE turn_trees SET shared_json=? WHERE attempt_id=?",
                           (json.dumps(sorted(theirs | {attempt_id})), other["attempt_id"]))
                grown.add((other["conversation_id"], other["message_id"]))
            if other["attempt_id"] not in mine:
                mine.add(other["attempt_id"])
                grown.add((row["conversation_id"], row["message_id"]))
        if (row["conversation_id"], row["message_id"]) in grown:
            tx.execute("UPDATE turn_trees SET shared_json=? WHERE attempt_id=?", (json.dumps(sorted(mine)), attempt_id))
        return grown

    def overlapping(self, target: str, start: str, *, besides_conversation: str) -> list[dict]:
        """The writable turns of other conversations in `target` whose windows reach
        into the time since `start` (C-26.14's `conversation.diff`, to now)."""
        since = _instant(start)
        rows = self.query("SELECT * FROM turn_trees WHERE target=? AND writable=1 AND conversation_id<>? "
                          "AND (ended_at IS NULL OR ended_at>=?) ORDER BY rowid",
                          (target, besides_conversation, _stamp(since)))
        return [_decode_trees(row) for row in rows if overlaps(since, None, *_window(row))]

    def trees_by_attempt(self, attempt_ids: list[str]) -> list[dict]:
        """The snapshot rows of these attempts, in the order they were first written."""
        if not attempt_ids:
            return []
        marks = ",".join("?" * len(attempt_ids))
        return [_decode_trees(row) for row in
                self.query(f"SELECT * FROM turn_trees WHERE attempt_id IN ({marks}) ORDER BY rowid", tuple(attempt_ids))]

    # Attempts are ordered by `rowid`, the order their rows were first written: a
    # row is written when its turn's runner starts (`service._record_start`) or, at
    # the latest, by `daemon._turn_trees` when it finalizes or its quarantine is
    # released, and a conversation's next turn job is submitted only once the
    # previous one is terminal, holds no lease, and no turn of the conversation is
    # quarantined (C-24.5, `service._previous_released`), so that is the order the
    # attempts ran. `started_at` (`reserved_at`, whole seconds) and the
    # attempt id cannot say it: two attempts of one message can be reserved in the
    # same second, and the later job's id (`<stamp>-<slug>-1`) sorts below the
    # earlier one's (`<stamp>-<slug>`) once `/a1` follows. Rows are never deleted,
    # and the store never runs VACUUM, which may renumber an implicit rowid.

    def turn_trees(self, message_id: str) -> dict | None:
        """The message's latest turn attempt's snapshots: a re-admitted message's
        earlier attempts never delivered it (C-26.7), so the latest is the turn."""
        row = self.one("SELECT * FROM turn_trees WHERE message_id=? ORDER BY rowid DESC LIMIT 1", (message_id,))
        return _decode_trees(row) if row else None

    def first_trees(self, conversation_id: str) -> dict | None:
        """The conversation's base: the start snapshot of its first writable turn."""
        row = self.one("SELECT * FROM turn_trees WHERE conversation_id=? AND start_tree IS NOT NULL "
                       "ORDER BY rowid LIMIT 1", (conversation_id,))
        return _decode_trees(row) if row else None

    # --- the change feed (C-29.9) ----------------------------------------------

    def _change(self, tx: sqlite3.Connection, conversation_id: str, message_id: str | None, state: str | None,
                *, pending: int | None = None, reason: str | None = None) -> None:
        if pending is None:
            pending = tx.execute("SELECT COUNT(*) FROM approvals WHERE conversation_id=? AND state='pending'",
                                 (conversation_id,)).fetchone()[0]
        tx.execute("INSERT INTO changes(conversation_id,message_id,state,pending_approvals,ts,state_reason) "
                   "VALUES (?,?,?,?,?,?)", (conversation_id, message_id, state, pending, utcnow(), reason))

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


#: C-29.6: how many conversations `status.json` lists; its counts cover every one.
STATUS_ITEMS = 20
STATUS_TITLE_CHARS = 200
_OPEN_STATES = (QUEUED, *LIVE_STATES)


def status_summary(root: str | Path, *, limit: int = STATUS_ITEMS, timeout_s: float = 0.5) -> dict:
    """C-29.6, D-26: conversation counts and a bounded list for `status.json`.

    Read through a connection of its own, opened read-only (`mode=ro`,
    `query_only`), never the `ConversationStore`'s, so the timer thread that
    publishes `status.json` takes neither that store's lock nor a write lock, and
    the control loop and request threads never wait for it. All reads are one
    read transaction, so the counts and the list are one snapshot. A file that
    is not there is no conversations; one that cannot be read, or has a newer
    schema, is `available: false` with the error's type (a SQLite or an OS
    exception's name, or `schema`), never zeros. "Not there" is only what `stat`
    reports as `FileNotFoundError`: a root this reader may not search raises
    `PermissionError`, which `Path.exists` would have read as no file and so as
    zeros nobody observed.

    Counted over conversations not archived: `active` has a message queued or in
    a live state (C-24.4), `needs_approval` has a pending approval, `blocked`
    has `blocked_by` or the legacy import's hold set (C-30.4; listed with
    `blocked_by` "legacy-owner" when only the hold is). Listed: those three kinds only, those needing
    approval first, then blocked, then the rest, newest update first and,
    between equal updates, the later-created conversation first (an id begins
    with its creation millisecond, `new_id`), at most `limit`. A listed
    conversation's `state` is `approval-needed` or `blocked` when it is either,
    else its live message's state, else `queued`.
    """
    counts = {"active": 0, "needs_approval": 0, "blocked": 0}
    path = Path(root) / "conversations.sqlite3"
    open_marks, live_marks = ",".join("?" * len(_OPEN_STATES)), ",".join("?" * len(LIVE_STATES))
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            # SQLite opens it by name and would wait in open() on a FIFO, holding the
            # timers' worker that `Timers.stop()` waits for.
            raise NotRegularFile(errno.EINVAL, "not a regular file", str(path))
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_s,
                             isolation_level=None)
    except FileNotFoundError:
        return {"available": True, "counts": counts, "items": [], "truncated": False}
    except (sqlite3.Error, OSError) as exc:
        return {"available": False, "error": type(exc).__name__}
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        version = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version is not None and version > SCHEMA_VERSION:
            return {"available": False, "error": "schema"}
        row = db.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM conversations c WHERE c.archived_at IS NULL AND EXISTS "
            f" (SELECT 1 FROM messages m WHERE m.conversation_id=c.conversation_id AND m.state IN ({open_marks}))),"
            "(SELECT COUNT(DISTINCT a.conversation_id) FROM approvals a JOIN conversations c "
            " USING(conversation_id) WHERE a.state='pending' AND c.archived_at IS NULL),"
            "(SELECT COUNT(*) FROM conversations WHERE archived_at IS NULL "
            " AND (blocked_by IS NOT NULL OR legacy_hold IS NOT NULL))",
            _OPEN_STATES).fetchone()
        counts = {"active": row[0], "needs_approval": row[1], "blocked": row[2]}
        rows = db.execute(
            # C-30.4: the legacy import's hold is its own column; to a reader it is a block.
            "SELECT c.conversation_id, c.provider, c.title, "
            f"COALESCE(c.blocked_by, CASE WHEN c.legacy_hold IS NOT NULL THEN '{LEGACY_OWNER}' END) AS blocked_by, "
            "c.updated_at, "
            "(SELECT COUNT(*) FROM approvals a WHERE a.conversation_id=c.conversation_id "
            " AND a.state='pending') AS pending_approvals, "
            "(SELECT m.state FROM messages m WHERE m.conversation_id=c.conversation_id "
            f" AND m.state IN ({live_marks}) ORDER BY m.seq LIMIT 1) AS live_state, "
            "EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.conversation_id "
            " AND m.state='queued') AS queued "
            "FROM conversations c WHERE c.archived_at IS NULL AND (c.blocked_by IS NOT NULL OR c.legacy_hold IS NOT NULL "
            f" OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.conversation_id AND m.state IN ({open_marks})) "
            " OR EXISTS (SELECT 1 FROM approvals a WHERE a.conversation_id=c.conversation_id AND a.state='pending')) "
            "ORDER BY CASE WHEN pending_approvals > 0 THEN 0 "
            "WHEN c.blocked_by IS NOT NULL OR c.legacy_hold IS NOT NULL THEN 1 ELSE 2 END, "
            "c.updated_at DESC, c.conversation_id DESC LIMIT ?",
            (*LIVE_STATES, *_OPEN_STATES, max(0, int(limit)) + 1)).fetchall()
        db.execute("COMMIT")
    except (sqlite3.Error, OSError) as exc:
        return {"available": False, "error": type(exc).__name__}
    finally:
        db.close()
    items = []
    for row in rows[:max(0, int(limit))]:
        state = ("approval-needed" if row["pending_approvals"] else "blocked" if row["blocked_by"]
                 else row["live_state"] or (QUEUED if row["queued"] else "idle"))
        title = row["title"]
        items.append({"conversation_id": row["conversation_id"], "provider": row["provider"],
                      "title": title[:STATUS_TITLE_CHARS] if isinstance(title, str) else None,
                      "state": state, "blocked_by": row["blocked_by"],
                      "pending_approvals": row["pending_approvals"], "updated_at": row["updated_at"]})
    return {"available": True, "counts": counts, "items": items, "truncated": len(rows) > len(items)}


#: A binding by native id, matched without regard to the case of a UUID's hex digits.
_NATIVE_MATCH = "provider=? AND (native_session_id=? OR (? AND lower(native_session_id)=?))"


def native_any_case(column: str, native_session_id: str) -> tuple[str, tuple]:
    """A SQL condition that `column` names `native_session_id`, and its parameters:
    the id as given or in one spelling, and a UUID whatever the case of its hex
    digits on either side (C-26.3, review L1). An id that is not a UUID is matched
    exactly. Any store's column: the conversation store's bindings and the main
    store's turn attempts (`Daemon._conversation_binding`) alike."""
    _, native, is_uuid, lowered = _native_params("claude", native_session_id)
    return (f"({column} IN (?, ?) OR (? AND lower({column})=?))",
            (native_session_id, native, is_uuid, lowered))


def _native_params(provider: str, native_session_id: str) -> tuple:
    native = canonical_native(native_session_id)
    try:
        is_uuid = str(uuid.UUID(native)) == native
    except (ValueError, AttributeError, TypeError):
        is_uuid = False
    return provider, native, int(is_uuid), native


def _decode_conversation(row: dict) -> dict:
    out = dict(row)
    out["settings"] = json.loads(out.pop("settings_json"))
    out["handoff_from"] = json.loads(out.pop("handoff_from_json")) if out.get("handoff_from_json") else None
    worktree = out.pop("worktree_json", None)
    out["worktree"] = json.loads(worktree) if worktree else None
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
    out["shared"] = json.loads(out.pop("shared_json", None) or "[]")
    return out


def _instant(value: str | datetime) -> datetime:
    """An ISO instant, either store's stamp (seconds or milliseconds, `Z` or an offset)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(value: str | datetime | None) -> str | None:
    """This store's millisecond stamp for an instant; None for none or one that does
    not parse (the window then starts at `started_at`)."""
    if value is None:
        return None
    try:
        return _instant(value).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _window(row) -> tuple[datetime, datetime | None]:
    """C-26.14: a turn's time window, from before its start snapshot to its recorded
    end; open (None) while it has none."""
    start = _instant(row["window_start"] or row["started_at"])
    return start, (_instant(row["ended_at"]) if row["ended_at"] else None)


def overlaps(a_start: datetime, a_end: datetime | None, b_start: datetime, b_end: datetime | None) -> bool:
    """Two closed windows meet; an open end reaches to any later time (I4)."""
    return (b_end is None or a_start <= b_end) and (a_end is None or b_start <= a_end)


def _decode_approval(row: dict) -> dict:
    out = dict(row)
    out["display"] = json.loads(out.pop("display_json"))
    out["options"] = json.loads(out.pop("options_json"))
    out["decision"] = json.loads(out.pop("decision_json")) if out.get("decision_json") else None
    return out
