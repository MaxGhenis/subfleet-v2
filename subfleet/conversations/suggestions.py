"""Suggested conversations (C-25.7, C-29.14): an agent proposes, a person starts.

An agent that finds an out-of-scope follow-up worth a session of its own
proposes it with `subfleet suggest` (op `suggestion.create`): a title, a
one-line summary (`tldr`), a self-contained brief, a folder and provider hints.
The person sees a card in the Subfleet app, in the conversation that proposed
it and in the sidebar's Suggested section, and starts it with one click or
dismisses it; whoever proposed it may withdraw it while it is open.

A suggestion moves `open` -> `starting` -> `started`, or from `open` to
`dismissed` or `withdrawn`, and never back. `starting` is the person's claim:
the one transaction that takes it also writes the new conversation's row, so a
suggestion that has a conversation can no longer be dismissed or withdrawn, and
one that was dismissed or withdrawn never gets a conversation. The brief is
then submitted as that conversation's first message under an id derived from
the suggestion's own, in the transaction that records `started`; a retry, a
second click or a restart finishes the same start with the same conversation
and the same message (`start_request_id`, `start_message_id`).

The rows live in `conversations.sqlite3`, beside the conversations they start,
in a table added as the wake tables are (`CREATE TABLE IF NOT EXISTS`, no
numbered step): the store's schema stays 2, so every retained build still opens
the store, and one that predates the table ignores it.

Title, summary and brief are scrubbed once, when the suggestion is created
(C-25.5, the handoff scrubber C-23.14): what the card shows is what Start sends.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from ..sessions.handoff import scrub_secrets
from .store import EVENT_ROW_MAX, ConversationError, new_id, utcnow

#: C-25.7: the shapes `suggestion.create` takes, in characters (title, summary)
#: and UTF-8 bytes (brief), and the open suggestions it keeps (per origin, and in all).
TITLE_MAX = 60
TLDR_MAX = 300
BRIEF_MAX_BYTES = 32 * 1024
PER_ORIGIN_OPEN = 20
OPEN_MAX = 200
#: `suggestion.list`: the closed suggestions listed after the open ones, and the
#: characters of a brief a listing carries unless the request names the suggestion.
CLOSED_LISTED = 50
PREVIEW_CHARS = 2_000

OPEN, STARTING, STARTED, DISMISSED, WITHDRAWN = "open", "starting", "started", "dismissed", "withdrawn"
STATES = (OPEN, STARTING, STARTED, DISMISSED, WITHDRAWN)
#: Counted against the bounds, and listed first: what a person may still act on.
OPEN_STATES = (OPEN, STARTING)
CLOSED_STATES = (STARTED, DISMISSED, WITHDRAWN)

CAPABILITY = "suggestions.v1"
LIMITS = {"suggestion_title_chars": TITLE_MAX, "suggestion_tldr_chars": TLDR_MAX,
          "suggestion_brief_bytes": BRIEF_MAX_BYTES, "suggestions_per_origin": PER_ORIGIN_OPEN,
          "suggestions_open": OPEN_MAX}

#: The namespace of the first message's id (`start_message_id`).
_NAMESPACE = uuid.UUID("5c2d3f0e-8a4b-4c1e-9f6a-2b7d0e1c4a91")

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
  suggestion_id           TEXT PRIMARY KEY,
  request_id              TEXT NOT NULL UNIQUE,
  digest                  TEXT NOT NULL,
  origin_conversation_id  TEXT,
  origin_message_id       TEXT,
  creator_session         TEXT,
  creator_job             TEXT,
  title                   TEXT NOT NULL,
  tldr                    TEXT NOT NULL,
  brief                   TEXT NOT NULL,
  workspace               TEXT,
  worktree                INTEGER NOT NULL DEFAULT 0,
  provider                TEXT NOT NULL CHECK (provider IN ('claude','codex')),
  model                   TEXT,
  state                   TEXT NOT NULL CHECK (state IN ('open','starting','started','dismissed','withdrawn')),
  revision                INTEGER NOT NULL DEFAULT 0,
  decided_by              TEXT,
  decided_at              TEXT,
  start_settings_json     TEXT,
  start_error             TEXT,
  started_conversation_id TEXT,
  started_message_id      TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS suggestions_by_state ON suggestions(state, created_at);
CREATE INDEX IF NOT EXISTS suggestions_by_origin ON suggestions(origin_conversation_id, state);
"""


def start_request_id(suggestion_id: str) -> str:
    """The `conversation.create` request id of the conversation a suggestion starts."""
    return f"suggestion:{suggestion_id}"


def start_message_id(suggestion_id: str) -> str:
    """The id of a started conversation's first message: the same on every retry (C-24.2)."""
    return str(uuid.uuid5(_NAMESPACE, suggestion_id))


def _line(value: Any, name: str, limit: int) -> str:
    """One line of text: white space collapsed, 1 to `limit` characters."""
    if not isinstance(value, str) or not value.strip():
        raise ConversationError(f"bad-{name}", f"{name} must be 1 to {limit} characters")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ConversationError(f"bad-{name}", f"{name} must be 1 to {limit} characters (it is {len(text)})")
    return text


def normalize(title: Any, tldr: Any, brief: Any) -> tuple[dict, int]:
    """The suggestion's words as they are kept and shown, and how many secret-shaped
    values scrubbing replaced (C-25.5). Lengths are checked before and after."""
    title, tldr = _line(title, "title", TITLE_MAX), _line(tldr, "tldr", TLDR_MAX)
    if not isinstance(brief, str) or not brief.strip():
        raise ConversationError("bad-brief", "the brief must say what the new conversation should do")
    # As a message's text is read back (C-24.3, `message_text`): the brief kept, shown
    # and sent is the same text, so the first message is exactly the brief.
    brief = brief.replace("\r\n", "\n").replace("\r", "\n")
    if len(brief.encode("utf-8")) > BRIEF_MAX_BYTES:
        raise ConversationError("bad-brief", f"the brief must be at most {BRIEF_MAX_BYTES} bytes of UTF-8 "
                                f"(it is {len(brief.encode('utf-8'))})", fix="shorten it, or point it at a file")
    scrubbed = 0
    out: dict[str, str] = {}
    for name, text, limit in (("title", title, TITLE_MAX), ("tldr", tldr, TLDR_MAX)):
        clean, count = scrub_secrets(text, strip_reminders=True)
        clean = " ".join(clean.split())
        if not clean:
            raise ConversationError(f"bad-{name}", f"nothing of the {name} is left once secrets are removed")
        out[name] = clean[:limit]
        scrubbed += count
    clean, count = scrub_secrets(brief, strip_reminders=True, whole=True)
    if not clean.strip():
        raise ConversationError("bad-brief", "nothing of the brief is left once secrets are removed")
    if len(clean.encode("utf-8")) > BRIEF_MAX_BYTES:
        raise ConversationError("bad-brief", f"the brief is over {BRIEF_MAX_BYTES} bytes once secrets are masked",
                                fix="shorten it")
    out["brief"] = clean
    return out, scrubbed + count


def digest(*, words: dict, origin: str | None, creator_session: str | None, workspace: str | None,
           worktree: bool, provider: str, model: str | None) -> str:
    """What a request id stands for: a retry with the same id must say the same."""
    body = {"words": words, "origin": origin, "creator_session": creator_session, "workspace": workspace,
            "worktree": bool(worktree), "provider": provider, "model": model}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def decode(row: Any) -> dict:
    row = dict(row)
    row["worktree"] = bool(row.get("worktree"))
    settings = row.pop("start_settings_json", None)
    row["start_settings"] = json.loads(settings) if settings else None
    return row


def view(row: dict, *, full_brief: bool = False) -> dict:
    """What `suggestion.list`, the watch feed and the events carry (C-25.5): the
    agent's words (already scrubbed), where and how it would start, and what
    became of it. A listing carries a brief's first PREVIEW_CHARS characters."""
    brief = row["brief"]
    shown = brief if full_brief or len(brief) <= PREVIEW_CHARS else brief[:PREVIEW_CHARS]
    return {"suggestion_id": row["suggestion_id"], "state": row["state"], "title": row["title"],
            "tldr": row["tldr"], "brief": shown, "brief_truncated": len(shown) < len(brief),
            "brief_chars": len(brief), "origin_conversation_id": row["origin_conversation_id"],
            "origin_message_id": row["origin_message_id"], "creator_session": row["creator_session"],
            "workspace": row["workspace"], "worktree": bool(row["worktree"]), "provider": row["provider"],
            "model": row["model"], "decided_by": row["decided_by"], "decided_at": row["decided_at"],
            "start_error": row["start_error"], "started_conversation_id": row["started_conversation_id"],
            "started_message_id": row["started_message_id"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def _event_body(row: dict) -> str:
    """The `suggestion` event's data: the whole brief when the row cap allows it."""
    body = json.dumps(view(row, full_brief=True), separators=(",", ":"), ensure_ascii=False)
    if len(body.encode()) > EVENT_ROW_MAX:
        body = json.dumps(view(row), separators=(",", ":"), ensure_ascii=False)
    return body


def _changed(tx: sqlite3.Connection, row: dict) -> None:
    """Every state a suggestion takes reaches the watch feed (C-29.9) and, for one
    with an origin, that conversation's events (C-25.4), in the transaction that
    took it. A suggestion with no origin has a feed row under conversation id "":
    a client that predates suggestions reads it as a list to refresh."""
    now = utcnow()
    origin = row["origin_conversation_id"]
    pending = tx.execute("SELECT COUNT(*) FROM approvals WHERE conversation_id=? AND state='pending'",
                         (origin,)).fetchone()[0] if origin else 0
    tx.execute("INSERT INTO changes(conversation_id,message_id,state,pending_approvals,ts,state_reason,suggestion_id) "
               "VALUES (?,NULL,NULL,?,?,NULL,?)", (origin or "", pending, now, row["suggestion_id"]))
    if origin:
        tx.execute("INSERT OR IGNORE INTO events(conversation_id,message_id,attempt_id,source,position,ordinal,"
                   "kind,data_json,ts) VALUES (?,?,?,'suggestion',?,0,'suggestion',?,?)",
                   (origin, row["origin_message_id"], f"suggestion:{row['suggestion_id']}", str(row["revision"]),
                    _event_body(row), now))


def _row(tx: sqlite3.Connection, suggestion_id: str) -> dict:
    found = tx.execute("SELECT * FROM suggestions WHERE suggestion_id=?", (suggestion_id,)).fetchone()
    if found is None:
        raise ConversationError("unknown-suggestion", f"no suggestion {suggestion_id}")
    return decode(found)


def _move(tx: sqlite3.Connection, row: dict, state: str, **fields: Any) -> dict:
    """One transition, from the state `row` was read in; its feed rows with it."""
    now = utcnow()
    sets = {"state": state, "revision": row["revision"] + 1, "updated_at": now, **fields}
    assignments = ",".join(f"{name}=?" for name in sets)
    moved = tx.execute(f"UPDATE suggestions SET {assignments} WHERE suggestion_id=? AND state=?",
                       (*sets.values(), row["suggestion_id"], row["state"])).rowcount
    if not moved:
        raise ConversationError("suggestion-moved", "the suggestion changed meanwhile; read it again", code=1)
    after = _row(tx, row["suggestion_id"])
    _changed(tx, after)
    return after


class Suggestions:
    """The suggestions table, through the conversation store's connection and lock."""

    def __init__(self, store):
        self.store = store

    def get(self, suggestion_id: str) -> dict:
        found = self.store.one("SELECT * FROM suggestions WHERE suggestion_id=?", (suggestion_id,))
        if found is None:
            raise ConversationError("unknown-suggestion", f"no suggestion {suggestion_id}")
        return decode(found)

    def add(self, *, request_id: str, words: dict, origin: str | None, origin_message: str | None,
            creator_session: str | None, creator_job: str | None, workspace: str | None, worktree: bool,
            provider: str, model: str | None, per_origin: int = PER_ORIGIN_OPEN,
            open_max: int = OPEN_MAX) -> tuple[dict, bool]:
        """Record a suggestion; idempotent by request id and what it stands for. The
        bounds are counted in the transaction that would add one, so concurrent
        creates never pass them together."""
        stands_for = digest(words=words, origin=origin, creator_session=creator_session, workspace=workspace,
                            worktree=worktree, provider=provider, model=model)
        with self.store.transaction() as tx:
            existing = tx.execute("SELECT * FROM suggestions WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["digest"] != stands_for:
                    raise ConversationError("suggestion-id-conflict",
                                            "request id already used for a different suggestion")
                return decode(existing), False
            if origin is not None and not tx.execute("SELECT 1 FROM conversations WHERE conversation_id=?",
                                                     (origin,)).fetchone():
                raise ConversationError("unknown-conversation", f"no conversation {origin}")
            marks = ",".join("?" * len(OPEN_STATES))
            total = tx.execute(f"SELECT COUNT(*) FROM suggestions WHERE state IN ({marks})", OPEN_STATES).fetchone()[0]
            if total >= open_max:
                raise ConversationError("too-many-suggestions", f"{total} suggestions are open; at most {open_max} are kept",
                                        code=7, fix="wait until the owner starts or dismisses some")
            if origin is not None:
                where, params = "origin_conversation_id=?", (origin,)
                whose = "this conversation"
            elif creator_session is not None:
                where, params = "origin_conversation_id IS NULL AND creator_session=?", (creator_session,)
                whose = "this session"
            else:
                where, params = "origin_conversation_id IS NULL AND creator_session IS NULL", ()
                whose = "callers with no session"
            mine = tx.execute(f"SELECT COUNT(*) FROM suggestions WHERE {where} AND state IN ({marks})",
                              (*params, *OPEN_STATES)).fetchone()[0]
            if mine >= per_origin:
                raise ConversationError("too-many-suggestions", f"{whose} has {mine} open suggestions; at most "
                                        f"{per_origin}", code=7,
                                        fix="withdraw a stale one (subfleet suggest --withdraw ID) first")
            now = utcnow()
            suggestion_id = new_id("sg")
            tx.execute(
                "INSERT INTO suggestions(suggestion_id,request_id,digest,origin_conversation_id,origin_message_id,"
                "creator_session,creator_job,title,tldr,brief,workspace,worktree,provider,model,state,revision,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (suggestion_id, request_id, stands_for, origin, origin_message, creator_session, creator_job,
                 words["title"], words["tldr"], words["brief"], workspace, int(bool(worktree)), provider, model,
                 OPEN, now, now))
            _changed(tx, _row(tx, suggestion_id))
        return self.get(suggestion_id), True

    def listing(self, *, conversation_id: str | None = None, limit: int = OPEN_MAX,
                closed_limit: int = CLOSED_LISTED) -> tuple[list[dict], int]:
        """Open suggestions newest first, then the most recently closed; and how
        many are open in all."""
        where, params = ("", ()) if conversation_id is None else (" AND origin_conversation_id=?", (conversation_id,))
        marks, closed = ",".join("?" * len(OPEN_STATES)), ",".join("?" * len(CLOSED_STATES))
        with self.store.read() as db:
            opened = [decode(r) for r in db.execute(
                f"SELECT * FROM suggestions WHERE state IN ({marks}){where} "
                "ORDER BY created_at DESC, suggestion_id DESC LIMIT ?", (*OPEN_STATES, *params, max(0, limit)))]
            ended = [decode(r) for r in db.execute(
                f"SELECT * FROM suggestions WHERE state IN ({closed}){where} "
                "ORDER BY updated_at DESC, suggestion_id DESC LIMIT ?", (*CLOSED_STATES, *params, max(0, closed_limit)))]
            total = db.execute(f"SELECT COUNT(*) FROM suggestions WHERE state IN ({marks}){where}",
                               (*OPEN_STATES, *params)).fetchone()[0]
        return opened + ended, total

    def by_ids(self, suggestion_ids: list[str]) -> list[dict]:
        if not suggestion_ids:
            return []
        marks = ",".join("?" * len(suggestion_ids))
        return [decode(r) for r in self.store.query(f"SELECT * FROM suggestions WHERE suggestion_id IN ({marks}) "
                                                    "ORDER BY created_at DESC", tuple(suggestion_ids))]

    def claim(self, suggestion_id: str, *, by: str, conversation: dict) -> tuple[dict, bool]:
        """A person's Start: `open` -> `starting`, writing the new conversation's row
        (`conversation`: the `create_conversation` fields) in the same transaction.
        Returns the row and whether this call took the claim; a suggestion already
        starting or started is returned as it is, and any other is refused."""
        with self.store.transaction() as tx:
            row = _row(tx, suggestion_id)
            if row["state"] in (STARTING, STARTED):
                return row, False
            if row["state"] != OPEN:
                raise ConversationError("not-open", f"the suggestion was {row['state']}; it cannot start", code=7)
            existing = tx.execute("SELECT conversation_id FROM conversations WHERE request_id=?",
                                  (start_request_id(suggestion_id),)).fetchone()
            cid = existing["conversation_id"] if existing else self.store.insert_conversation(
                tx, request_id=start_request_id(suggestion_id), **conversation)
            settings = conversation["settings"]
            return _move(tx, row, STARTING, decided_by=by, decided_at=utcnow(), started_conversation_id=cid,
                         start_settings_json=json.dumps(settings), start_error=None), True

    def finish_in(self, tx: sqlite3.Connection, suggestion_id: str, message_id: str) -> None:
        """`starting` -> `started`, in the transaction that accepts the first message
        (`ConversationStore.submit_message`'s `on_accept`)."""
        row = _row(tx, suggestion_id)
        if row["state"] == STARTED:
            return
        if row["state"] != STARTING:
            raise ConversationError("not-starting", f"the suggestion is {row['state']}", code=1)
        _move(tx, row, STARTED, started_message_id=message_id, start_error=None)

    def finish(self, suggestion_id: str, message_id: str) -> dict:
        """`finish_in` on its own, for a start whose message was accepted before (a retry)."""
        with self.store.transaction() as tx:
            self.finish_in(tx, suggestion_id, message_id)
        return self.get(suggestion_id)

    def note_error(self, suggestion_id: str, error: str) -> dict:
        """A start that could not finish keeps its claim and says why; Start again
        (or the daemon's own retry) finishes it."""
        with self.store.transaction() as tx:
            row = _row(tx, suggestion_id)
            if row["state"] == STARTING and row["start_error"] != error:
                _move(tx, row, STARTING, start_error=error[:500])
        return self.get(suggestion_id)

    def close(self, suggestion_id: str, state: str, *, by: str) -> tuple[dict, bool]:
        """`open` -> `dismissed` or `withdrawn`. Closing a closed one changes nothing;
        one being started, or started, is too late."""
        if state not in (DISMISSED, WITHDRAWN):
            raise ValueError(state)
        with self.store.transaction() as tx:
            row = _row(tx, suggestion_id)
            if row["state"] in (DISMISSED, WITHDRAWN):
                return row, False
            if row["state"] != OPEN:
                raise ConversationError("too-late", f"the suggestion is {row['state']}: the person started it",
                                        code=7)
            return _move(tx, row, state, decided_by=by, decided_at=utcnow()), True

    def resumable(self, *, older_than: str) -> list[dict]:
        """Claims a start took and did not finish, the oldest first."""
        return [decode(r) for r in self.store.query(
            "SELECT * FROM suggestions WHERE state=? AND decided_at<=? ORDER BY decided_at", (STARTING, older_than))]

    def views_for_changes(self, suggestion_ids: list[str]) -> dict[str, dict]:
        return {row["suggestion_id"]: view(row) for row in self.by_ids(sorted(set(suggestion_ids)))}
