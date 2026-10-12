"""Suggested conversations (C-25.7, C-29.14): an agent proposes, a person starts.

An agent that finds an out-of-scope follow-up worth a session of its own
proposes it with `subfleet suggest` (op `suggestion.create`): a title, a
one-line summary (`tldr`), a self-contained brief, a folder and provider hints.
The person sees a card in the Subfleet app, in the conversation that proposed
it and in the sidebar's Suggested section, and starts it with one click or
dismisses it; whoever proposed it may withdraw it while it is open.

A suggestion moves `open` -> `starting` -> `started`, or from `open` to
`dismissed` or `withdrawn`, and never back. `starting` is the person's claim:
the one transaction that takes it also writes the new conversation's row and
chooses the id of its first message, so a suggestion that has a conversation can
no longer be dismissed or withdrawn, and one that was dismissed or withdrawn
never gets a conversation. The conversation is bound to the claim only by the
suggestion's own `started_conversation_id`, written in that transaction: it has
no request id an agent could reserve or replay (request ids beginning
`suggestion:` are refused to every create and handoff), and the first message's
id is random and kept private until it is accepted. Until then the conversation
takes no message but the brief (`admit_message`, in every message's own
transaction), and the brief is accepted, in the empty conversation, in the
transaction that records `started`. A retry, a second click or a restart
finishes the same start with the same conversation and the same message.

Where a suggestion starts is decided when it is made (`workspace_kind`: a new
worktree of `repository`, or in place; a suggestion with no folder starts in a
new scratch folder), shown on its card, and what Start does: a worktree start
whose folder is no longer a git checkout is refused, never run in place.

The rows live in `conversations.sqlite3`, beside the conversations they start,
in a table added as the wake tables are (`CREATE TABLE IF NOT EXISTS`, no
numbered step): the store's schema stays 2, so every retained build still opens
the store, and one that predates the table ignores it.

Title, summary and brief are scrubbed when the suggestion is created
(C-25.5, the handoff scrubber C-23.14): what the card shows is what Start sends.
A folder path, caller session or calling job the scrubber would change is refused,
every error a suggestion records, logs or returns is scrubbed whole, including
quoted multiline Unicode values, before whitespace folding; the folded form is
scrubbed again before a marker-safe cut to ERROR_MAX characters (`scrub_error`).
A Start's `decided_by` is scrubbed the
same way (`scrubbed_line`). No cut, of these or of a title, summary or listed
brief, splits what scrubbing put in (`bounded`): no suggestion field or event
holds a value the scrubber recognizes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from ..sessions.handoff import REDACTION_MARKERS, scrub_secrets
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

#: The characters of a start's error a suggestion keeps and shows (`scrub_error`).
ERROR_MAX = 500
#: Request ids no `conversation.create` or handoff may take: a Start once named its
#: conversation so, and an agent could reserve that name first (C-25.7).
RESERVED_REQUEST_PREFIX = "suggestion:"
#: Where a suggestion starts (`workspace_kind`), decided when it is made.
WORKTREE, IN_PLACE = "worktree", "in-place"

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
  workspace_kind          TEXT,
  repository              TEXT,
  provider                TEXT NOT NULL CHECK (provider IN ('claude','codex')),
  model                   TEXT,
  state                   TEXT NOT NULL CHECK (state IN ('open','starting','started','dismissed','withdrawn')),
  revision                INTEGER NOT NULL DEFAULT 0,
  decided_by              TEXT,
  decided_at              TEXT,
  start_settings_json     TEXT,
  start_error             TEXT,
  start_message_id        TEXT,
  started_conversation_id TEXT,
  started_message_id      TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS suggestions_by_state ON suggestions(state, created_at);
CREATE INDEX IF NOT EXISTS suggestions_by_origin ON suggestions(origin_conversation_id, state);
CREATE INDEX IF NOT EXISTS suggestions_by_conversation ON suggestions(started_conversation_id);
"""
#: Columns a table an earlier build of this one made lacks: added as the store adds
#: its own additive columns, never by a numbered step.
_ADDED_COLUMNS = (("workspace_kind", "TEXT"), ("repository", "TEXT"), ("start_message_id", "TEXT"))


def add_columns(db: sqlite3.Connection) -> None:
    """Bring a suggestions table an earlier build made up to SCHEMA's columns."""
    have = {row[1] for row in db.execute("PRAGMA table_info(suggestions)")}
    for name, kind in _ADDED_COLUMNS:
        if name not in have:
            db.execute(f"ALTER TABLE suggestions ADD COLUMN {name} {kind}")


def scratch_name(suggestion_id: str) -> str:
    """The private scratch folder, below `conversations/workspaces`, a suggestion with
    no folder starts in: the same on every retry, and never one `conversation.create`
    makes (theirs are named by 64 hex digits)."""
    return "suggestion-" + hashlib.sha256(suggestion_id.encode()).hexdigest()


def holds_secret(text: str) -> bool:
    """Whether the C-25.5 scrubber would change `text`: a value no suggestion field holds."""
    clean, count = scrub_secrets(text, strip_reminders=True, whole=True)
    return count > 0 or clean != text


def bounded(clean: str, limit: int, mark: str = "") -> str:
    """`clean`, already scrubbed whole, within `limit` characters: its first ones,
    then `mark` when it was cut. Retreat to the beginning of any redaction marker
    crossing the character cut. If that leaves a sensitive assignment fragment,
    retreat a word at a time, preserving whole markers at each new cut (C-25.5)."""
    if limit <= 0:
        return ""
    if len(clean) <= limit:
        return clean
    end = max(0, limit - len(mark))
    while end:
        for marker in REDACTION_MARKERS:
            start = clean.rfind(marker, 0, end + len(marker) - 1)
            if 0 <= start < end < start + len(marker):
                end = start
        cut = clean[:end]
        if not holds_secret(cut + mark):
            break
        words = cut.rstrip()
        end = len(words) - len(words.split()[-1]) if words.split() else 0
    return clean[:end] + mark[:limit]


def _scrub_line(text: str) -> tuple[str, int]:
    """Scrub raw text before folding; scrub any shapes folding creates too."""
    clean, count = scrub_secrets(text, strip_reminders=True, whole=True)
    clean, folded_count = scrub_secrets(" ".join(clean.split()), strip_reminders=True, whole=True)
    return clean, count + folded_count


def scrubbed_line(text: str, limit: int = ERROR_MAX) -> str:
    """`text` scrubbed whole (C-25.5) first, so no secret is cut into a fragment the
    rules no longer know, then folded and scrubbed again, then a marker-safe cut
    to at most `limit` characters (`bounded`)."""
    clean, _ = _scrub_line(text)
    return bounded(clean, limit, "…")


def scrub_error(error: Any, *, prefix: str = "") -> str:
    """A suggestion's diagnostic, scrubbed before exception quoting and folding.

    String exception arguments are the original message: KeyError's repr and a
    multi-argument exception's tuple repr can escape credential delimiters. Keep
    those arguments raw until they are scrubbed, with the diagnostic prefix.
    """
    if isinstance(error, BaseException) and error.args and all(isinstance(arg, str) for arg in error.args):
        text = "\n".join(error.args)
    elif isinstance(error, BaseException) and (isinstance(error, KeyError) or
                                             type(error).__str__ is BaseException.__str__):
        text = "\n".join(str(arg) for arg in error.args)
    else:
        text = str(error)
    if isinstance(error, OSError):
        # OSError uses repr for filenames, which can escape assignment quotes.
        for name in ("filename", "filename2"):
            filename = getattr(error, name, None)
            if isinstance(filename, str):
                text = text.replace(repr(filename), filename)
    return scrubbed_line(prefix + text) or "the start failed"


def admit_message(tx: sqlite3.Connection, conversation_id: str, starts: str | None) -> None:
    """C-25.7, inside the transaction that would accept a message: a conversation a
    suggestion's claim made takes no message until that suggestion's brief, and the
    brief only as its first. `starts` names the suggestion whose Start submits its
    brief (`ConversationService._finish_start`); every other message passes None."""
    claim = tx.execute("SELECT suggestion_id FROM suggestions WHERE started_conversation_id=? AND state=?",
                       (conversation_id, STARTING)).fetchone()
    if starts is None:
        if claim is not None:
            raise ConversationError("conversation-starting", "the conversation is being started from a suggestion: "
                                    "its first message is the brief the person started, and nothing is accepted "
                                    "before it", code=7, fix="send it once the conversation has started")
        return
    if claim is None or claim[0] != starts:
        raise ConversationError("not-starting", f"suggestion {starts} is not starting this conversation", code=1)
    if tx.execute("SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1", (conversation_id,)).fetchone():
        raise ConversationError("conversation-not-empty", "the conversation this start made already holds a "
                                "message, so the brief cannot be its first", code=1)


def _line(value: Any, name: str, limit: int) -> tuple[str, int]:
    """Admit the original length; scrub before folding, then bound the clean line."""
    if not isinstance(value, str) or not value.strip():
        raise ConversationError(f"bad-{name}", f"{name} must be 1 to {limit} characters")
    clean, count = _scrub_line(value)
    original_length = len(" ".join(value.split()))
    if original_length > limit:
        raise ConversationError(f"bad-{name}", f"{name} must be 1 to {limit} characters (it is {original_length})")
    clean = bounded(clean, limit)
    if not clean.strip():
        raise ConversationError(f"bad-{name}", f"nothing of the {name} is left once secrets are removed")
    return clean, count


def normalize(title: Any, tldr: Any, brief: Any) -> tuple[dict, int]:
    """The suggestion's words as they are kept and shown, and how many secret-shaped
    values scrubbing replaced (C-25.5). Lengths are checked before and after."""
    title, title_count = _line(title, "title", TITLE_MAX)
    tldr, tldr_count = _line(tldr, "tldr", TLDR_MAX)
    if not isinstance(brief, str) or not brief.strip():
        raise ConversationError("bad-brief", "the brief must say what the new conversation should do")
    # As a message's text is read back (C-24.3, `message_text`): the brief kept, shown
    # and sent is the same text, so the first message is exactly the brief.
    clean, count = scrub_secrets(brief, strip_reminders=True, whole=True)
    brief = brief.replace("\r\n", "\n").replace("\r", "\n")
    clean = clean.replace("\r\n", "\n").replace("\r", "\n")
    # A bare CR can become a header boundary: scrub shapes normalization creates,
    # just as _scrub_line scrubs the shapes whitespace folding creates.
    clean, normalized_count = scrub_secrets(clean, strip_reminders=True, whole=True)
    count += normalized_count
    if len(brief.encode("utf-8")) > BRIEF_MAX_BYTES:
        raise ConversationError("bad-brief", f"the brief must be at most {BRIEF_MAX_BYTES} bytes of UTF-8 "
                                f"(it is {len(brief.encode('utf-8'))})", fix="shorten it, or point it at a file")
    out: dict[str, str] = {"title": title, "tldr": tldr}
    if not clean.strip():
        raise ConversationError("bad-brief", "nothing of the brief is left once secrets are removed")
    if len(clean.encode("utf-8")) > BRIEF_MAX_BYTES:
        raise ConversationError("bad-brief", f"the brief is over {BRIEF_MAX_BYTES} bytes once secrets are masked",
                                fix="shorten it")
    out["brief"] = clean
    return out, title_count + tldr_count + count


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
    became of it. A listing carries at most a brief's first PREVIEW_CHARS
    characters, never cut inside what scrubbing put in (`bounded`).
    `worktree` is what the agent asked for; `workspace_kind` (and `repository`
    for a worktree) is what Start does, decided when the suggestion was made."""
    brief = row["brief"]
    shown = brief if full_brief else bounded(brief, PREVIEW_CHARS)
    return {"suggestion_id": row["suggestion_id"], "state": row["state"], "title": row["title"],
            "tldr": row["tldr"], "brief": shown, "brief_truncated": len(shown) < len(brief),
            "brief_chars": len(brief), "origin_conversation_id": row["origin_conversation_id"],
            "origin_message_id": row["origin_message_id"], "creator_session": row["creator_session"],
            "workspace": row["workspace"], "worktree": bool(row["worktree"]),
            "workspace_kind": row.get("workspace_kind"), "repository": row.get("repository"),
            "provider": row["provider"],
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


def feed_seq(db: sqlite3.Connection) -> int:
    """The watch feed's high-water mark (C-29.9), read under the lock that read the
    rows beside it: every change up to it is in them. The table's AUTOINCREMENT
    counter, so compaction never lowers it."""
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='changes'").fetchone()
    return int(row[0]) if row else 0


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
            provider: str, model: str | None, workspace_kind: str = IN_PLACE, repository: str | None = None,
            per_origin: int = PER_ORIGIN_OPEN, open_max: int = OPEN_MAX) -> tuple[dict, bool]:
        """Record a suggestion; idempotent by request id and what it stands for. The
        bounds are counted in the transaction that would add one, so concurrent
        creates never pass them together. `workspace_kind` and `repository` are
        where it starts, as the daemon decided it; a retry keeps the first decision."""
        if workspace_kind not in (WORKTREE, IN_PLACE) or (workspace_kind == WORKTREE) != (repository is not None):
            raise ValueError(f"a {workspace_kind!r} start with repository {repository!r}")
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
                "creator_session,creator_job,title,tldr,brief,workspace,worktree,workspace_kind,repository,"
                "provider,model,state,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (suggestion_id, request_id, stands_for, origin, origin_message, creator_session, creator_job,
                 words["title"], words["tldr"], words["brief"], workspace, int(bool(worktree)), workspace_kind,
                 repository, provider, model, OPEN, now, now))
            _changed(tx, _row(tx, suggestion_id))
        return self.get(suggestion_id), True

    def listing(self, *, conversation_id: str | None = None, limit: int = OPEN_MAX,
                closed_limit: int = CLOSED_LISTED) -> tuple[list[dict], int, int]:
        """Open suggestions newest first, then the most recently closed; how many
        are open in all; and the watch feed's sequence they were read at (`feed_seq`)."""
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
            seq = feed_seq(db)
        return opened + ended, total, seq

    def by_ids(self, suggestion_ids: list[str]) -> list[dict]:
        return self.named(suggestion_ids)[0]

    def named(self, suggestion_ids: list[str]) -> tuple[list[dict], int]:
        """These suggestions, and the watch feed's sequence they were read at."""
        marks = ",".join("?" * len(suggestion_ids))
        with self.store.read() as db:
            rows = [decode(r) for r in db.execute(f"SELECT * FROM suggestions WHERE suggestion_id IN ({marks}) "
                                                  "ORDER BY created_at DESC", tuple(suggestion_ids))] \
                if suggestion_ids else []
            return rows, feed_seq(db)

    def claim(self, suggestion_id: str, *, by: str, conversation: dict) -> tuple[dict, bool]:
        """A person's Start: `open` -> `starting`, writing a new conversation's row
        (`conversation`: the `create_conversation` fields) and choosing its first
        message's id in the same transaction. Returns the row and whether this call
        took the claim; a suggestion already starting or started is returned as it
        is, and any other is refused.

        The conversation is always a new one, with no request id: it is bound to the
        claim by `started_conversation_id` alone, and nothing found by a name an
        agent could reserve is ever adopted (C-25.7). Its first message's id is a
        new random UUID, kept off every view until the message is accepted, so no
        caller can take it first anywhere."""
        with self.store.transaction() as tx:
            row = _row(tx, suggestion_id)
            if row["state"] in (STARTING, STARTED):
                return row, False
            if row["state"] != OPEN:
                raise ConversationError("not-open", f"the suggestion was {row['state']}; it cannot start", code=7)
            cid = self.store.insert_conversation(tx, request_id=None, **conversation)
            return _move(tx, row, STARTING, decided_by=scrubbed_line(by), decided_at=utcnow(),
                         started_conversation_id=cid, start_message_id=str(uuid.uuid4()),
                         start_settings_json=json.dumps(conversation["settings"]), start_error=None), True

    def message_id(self, suggestion_id: str) -> str:
        """The first message's id a claim chose; one an earlier build's claim never
        chose is chosen now, once."""
        with self.store.transaction() as tx:
            tx.execute("UPDATE suggestions SET start_message_id=? WHERE suggestion_id=? AND start_message_id IS NULL",
                       (str(uuid.uuid4()), suggestion_id))
            return _row(tx, suggestion_id)["start_message_id"]

    def finish_in(self, tx: sqlite3.Connection, suggestion_id: str, message_id: str) -> None:
        """`starting` -> `started`, in the transaction that accepts the first message
        (`ConversationStore.submit_message`'s `on_accept`): the message the claim chose."""
        row = _row(tx, suggestion_id)
        if row["state"] == STARTED:
            return
        if row["state"] != STARTING:
            raise ConversationError("not-starting", f"the suggestion is {row['state']}", code=1)
        if row["start_message_id"] is not None and message_id != row["start_message_id"]:
            raise ConversationError("not-the-brief", "only the message the claim chose starts the suggestion", code=1)
        _move(tx, row, STARTED, started_message_id=message_id, start_error=None)

    def finish(self, suggestion_id: str, message_id: str) -> dict:
        """`finish_in` on its own, for a start whose message was accepted before (a retry)."""
        with self.store.transaction() as tx:
            self.finish_in(tx, suggestion_id, message_id)
        return self.get(suggestion_id)

    def note_error(self, suggestion_id: str, error: str) -> dict:
        """A start that could not finish keeps its claim and says why; Start again
        (or the daemon's own retry) finishes it. The reason is stored and shown as
        `scrub_error` leaves it, however its caller made it (C-25.5)."""
        error = scrub_error(error)
        with self.store.transaction() as tx:
            row = _row(tx, suggestion_id)
            if row["state"] == STARTING and row["start_error"] != error:
                _move(tx, row, STARTING, start_error=error)
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
