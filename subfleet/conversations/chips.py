"""Durable task suggestions and their person-started children (C-31).

Suggestions use the conversation store's transaction and notification boundary.
Starting publishes the exact first-message payload before committing the chip,
child and message together; competing Starts and Dismisses cannot split them.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid

from .store import ConversationError, message_digest, new_id, utcnow, validate_settings

PROMPT_BYTES = 32 * 1024
PUBLIC_FIELDS = (
    "chip_id", "parent_conversation_id", "message_id", "title", "tldr", "prompt", "cwd", "state",
    "child_conversation_id", "created_at", "updated_at", "dismissal_reason",
)


def initialize(db: sqlite3.Connection) -> None:
    """Additive schema: older stores and builds continue to read conversations."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chips (
          chip_id TEXT PRIMARY KEY,
          parent_conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          message_id TEXT NOT NULL REFERENCES messages(message_id),
          request_id TEXT NOT NULL, digest TEXT NOT NULL,
          title TEXT NOT NULL, tldr TEXT NOT NULL, prompt TEXT NOT NULL, cwd TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('pending','started','dismissed')),
          child_conversation_id TEXT REFERENCES conversations(conversation_id),
          child_message_id TEXT REFERENCES messages(message_id), dismissal_reason TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(parent_conversation_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS chips_by_parent ON chips(parent_conversation_id, created_at, chip_id);
        CREATE TABLE IF NOT EXISTS conversation_chip_hosts (
          token_sha256 TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
          message_id TEXT NOT NULL REFERENCES messages(message_id),
          created_at TEXT NOT NULL
        );
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
    for column in ("parent_conversation_id", "source_chip_id"):
        if column not in columns:
            db.execute(f"ALTER TABLE conversations ADD COLUMN {column} TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS conversations_by_chip ON conversations(source_chip_id)")
    if "preserve_newlines" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
        db.execute("ALTER TABLE messages ADD COLUMN preserve_newlines INTEGER NOT NULL DEFAULT 0")


def public(row: dict, *, prompt: bool = True) -> dict:
    return {key: row.get(key) for key in PUBLIC_FIELDS if prompt or key != "prompt"}


def _string(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ConversationError("bad-chip", f"{field} must be nonempty text of at most {limit} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ConversationError("bad-chip", f"{field} must be valid UTF-8 text") from None
    return value


class ChipService:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def host_credentials(self, conversation_id: str, message_id: str) -> dict:
        """Register one launch's capability, stored hashed and never sent to the app."""
        conversation = self.store.conversation(conversation_id)
        message = self.store.message(message_id)
        if (message["conversation_id"] != conversation_id or conversation["provider"] != "claude"
                or message["settings"]["permission"] == "read-only"):
            raise ConversationError("chip-host-refused", "chip tools require a writable Claude turn", code=7)
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as tx:
            tx.execute("INSERT INTO conversation_chip_hosts VALUES (?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), conversation_id, message_id, utcnow()))
        return {"root": str(self.store.root), "conversation_id": conversation_id,
                "message_id": message_id, "token": token}

    def _host(self, args: dict) -> dict:
        token = args.get("host_token")
        if not isinstance(token, str) or not token or len(token) > 256 or not token.isascii():
            raise ConversationError("chip-host-refused", "invalid chip host capability", code=7)
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.store.one("SELECT * FROM conversation_chip_hosts WHERE token_sha256=?", (digest,))
        if (row is None or not hmac.compare_digest(row["token_sha256"], digest)
                or row["conversation_id"] != args.get("conversation_id")
                or row["message_id"] != args.get("message_id")):
            raise ConversationError("chip-host-refused", "chip host capability does not match this turn", code=7)
        message = self.store.message(row["message_id"])
        if message["conversation_id"] != row["conversation_id"] or message["settings"]["permission"] == "read-only":
            raise ConversationError("chip-host-refused", "invalid chip host turn", code=7)
        return self.store.conversation(row["conversation_id"])

    def _get(self, chip_id: str) -> dict:
        chip = self.store.one("SELECT * FROM chips WHERE chip_id=?", (chip_id,))
        if chip is None:
            raise ConversationError("unknown-chip", f"no chip {chip_id}")
        return chip

    def list(self, conversation_id: str) -> list[dict]:
        self.store.conversation(conversation_id)
        return [public(row) for row in self.store.query(
            "SELECT * FROM chips WHERE parent_conversation_id=? ORDER BY created_at,chip_id", (conversation_id,))]

    def _event(self, tx: sqlite3.Connection, chip: dict, kind: str) -> None:
        # The large prompt remains in the durable snapshot, outside the 64 KiB event cap.
        tx.execute("INSERT INTO events(conversation_id,message_id,attempt_id,source,position,ordinal,kind,data_json,ts) "
                   "VALUES (?,?,?,'chip',?,0,?,?,?)",
                   (chip["parent_conversation_id"], chip["message_id"], f"chip:{chip['chip_id']}", kind,
                    kind, json.dumps({"chip": public(chip, prompt=False)}, ensure_ascii=False), chip["updated_at"]))
        tx.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                   (chip["updated_at"], chip["parent_conversation_id"]))
        self.store._change(tx, chip["parent_conversation_id"], None, None)

    def spawn(self, args: dict) -> dict:
        parent = self._host(args)
        request_id = _string(args.get("request_id"), "request_id", 128)
        title = _string(args.get("title"), "title", 200)
        tldr = _string(args.get("tldr"), "tldr", 2000)
        prompt = _string(args.get("prompt"), "prompt", PROMPT_BYTES)
        if len(prompt.encode("utf-8")) > PROMPT_BYTES:
            raise ConversationError("bad-chip", "prompt must be at most 32 KiB UTF-8")
        cwd = args.get("cwd", parent["workspace"])
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or len(cwd) > 4096 or "\x00" in cwd:
            raise ConversationError("bad-workspace", "chip cwd must be an absolute directory")
        _string(cwd, "cwd", 4096)
        body = {"message_id": args["message_id"], "title": title, "tldr": tldr, "prompt": prompt,
                "cwd": args.get("cwd")}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

        def replay(row):
            if row["digest"] != digest:
                raise ConversationError("chip-request-conflict", "request_id already used for a different chip")
            return {"chip": public(dict(row))}

        existing = self.store.one("SELECT * FROM chips WHERE parent_conversation_id=? AND request_id=?",
                                  (parent["conversation_id"], request_id))
        if existing:
            return replay(existing)
        if parent.get("archived_at"):
            raise ConversationError("archived", "the parent conversation is archived")
        cwd = os.path.realpath(cwd)
        if len(cwd) > 4096 or not os.path.isdir(cwd):
            raise ConversationError("bad-workspace", "chip cwd must be an existing directory")
        now = utcnow()
        with self.store.transaction() as tx:
            row = tx.execute("SELECT * FROM chips WHERE parent_conversation_id=? AND request_id=?",
                             (parent["conversation_id"], request_id)).fetchone()
            if row:
                return replay(row)
            chip_id = new_id("chip")
            tx.execute("INSERT INTO chips(chip_id,parent_conversation_id,message_id,request_id,digest,title,tldr,prompt,"
                       "cwd,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                       (chip_id, parent["conversation_id"], args["message_id"], request_id, digest, title, tldr,
                        prompt, cwd, now, now))
            chip = dict(tx.execute("SELECT * FROM chips WHERE chip_id=?", (chip_id,)).fetchone())
            self._event(tx, chip, "chip.created")
        return {"chip": public(chip)}

    def dismiss(self, args: dict, peer) -> dict:
        parent = self._host(args) if "host_token" in args else None
        if parent is None:
            self.service._person(peer, "dismissing a suggested task")
        reason = args.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
            raise ConversationError("bad-chip", "reason must be text of at most 2000 characters")
        if reason is not None:
            try:
                reason.encode("utf-8")
            except UnicodeEncodeError:
                raise ConversationError("bad-chip", "reason must be valid UTF-8 text") from None
        with self.store.transaction() as tx:
            chip = self._get(args["chip_id"])
            if parent is not None and chip["parent_conversation_id"] != parent["conversation_id"]:
                raise ConversationError("chip-host-refused", "a host can dismiss only its parent's chips", code=7)
            if chip["state"] == "started":
                raise ConversationError("chip-started", "this task has already started", code=7)
            if chip["state"] == "pending":
                chip.update(state="dismissed", dismissal_reason=reason, updated_at=utcnow())
                tx.execute("UPDATE chips SET state='dismissed',dismissal_reason=?,updated_at=? WHERE chip_id=?",
                           (reason, chip["updated_at"], chip["chip_id"]))
                self._event(tx, chip, "chip.dismissed")
        return {"chip": public(chip)}

    def _started(self, chip: dict) -> dict:
        return {"chip": public(chip), "conversation": self.service._view(
            self.store.conversation(chip["child_conversation_id"])), "message": self.service._receipt(
                self.store.message(chip["child_message_id"]), text=True)}

    def start(self, args: dict, peer) -> dict:
        self.service._person(peer, "starting a suggested task")
        chip = self._get(args["chip_id"])
        if chip["state"] == "started":
            return self._started(chip)
        if chip["state"] != "pending":
            raise ConversationError("chip-dismissed", "this task was dismissed", code=7)
        parent = self.store.conversation(chip["parent_conversation_id"])
        settings = validate_settings(parent["provider"], parent["settings"])
        if not os.path.isdir(chip["cwd"]):
            raise ConversationError("bad-workspace", "the proposed directory no longer exists")
        self.service._check_codex_policy(parent["provider"], settings)
        self.service._check_workspace(parent["provider"], chip["cwd"], settings)
        allow_main = parent["allow_main"] and os.path.realpath(parent["workspace"]) == chip["cwd"]
        cid, mid, now = new_id("cv"), str(uuid.uuid4()), utcnow()
        digest = message_digest(cid, chip["prompt"], [], settings)
        path = self.store.dir / cid / "messages" / f"{mid}.{digest[:16]}.md"
        # The write guard keeps close() from removing the root before cleanup ends.
        with self.store.writing():
            try:
                self.store._publish(path, chip["prompt"].encode("utf-8"))
                with self.store.transaction() as tx:
                    chip = self._get(chip["chip_id"])
                    if chip["state"] == "dismissed":
                        raise ConversationError("chip-dismissed", "this task was dismissed", code=7)
                    if chip["state"] == "pending":
                        tx.execute("INSERT INTO conversations(conversation_id,provider,title,workspace,workspace_kind,"
                                   "allow_main,settings_json,origin,parent_conversation_id,source_chip_id,created_at,updated_at) "
                                   "VALUES (?,?,?,?,'in-place',?,?,'new',?,?,?,?)",
                                   (cid, parent["provider"], chip["title"], chip["cwd"], int(allow_main),
                                    json.dumps(settings), parent["conversation_id"], chip["chip_id"], now, now))
                        tx.execute("INSERT INTO messages(message_id,conversation_id,seq,origin,digest,text_path,"
                                   "attachments_json,settings_json,state,created_at,updated_at,preserve_newlines) "
                                   "VALUES (?,?,1,'person',?,?,'[]',?,'queued',?,?,1)",
                                   (mid, cid, digest, str(path), json.dumps(settings), now, now))
                        chip.update(state="started", child_conversation_id=cid, child_message_id=mid, updated_at=now)
                        tx.execute("UPDATE chips SET state='started',child_conversation_id=?,child_message_id=?,updated_at=? "
                                   "WHERE chip_id=?", (cid, mid, now, chip["chip_id"]))
                        self.store._change(tx, cid, mid, "queued")
                        self._event(tx, chip, "chip.started")
            finally:
                # A failed commit or a losing Start owns only its unreferenced payload.
                if self.store.one("SELECT 1 FROM messages WHERE message_id=?", (mid,)) is None:
                    with contextlib.suppress(OSError):
                        path.unlink()
                    for directory in (path.parent, path.parent.parent):
                        with contextlib.suppress(OSError):
                            directory.rmdir()
        self.service.daemon._notify()
        return self._started(chip)
