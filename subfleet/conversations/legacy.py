"""The legacy cockpit import (C-30.4, design §13).

What the v1 state directory holds, read on 2026-09-24 from the cockpit's source
(`~/chief-of-staff-worktrees/subfleet-traycer-port`, whose `outbox.py`,
`broker.py`, `native_dispatch.py` and `claude_transport.py` are untracked
working-tree files) and from a private copy of the real outbox, whose rows have
exactly the shapes that code writes:

* `outbox.sqlite3` is the desktop cockpit's message outbox, not a notice
  outbox. One row per message the cockpit's composer sent into a native
  session: `messages(sequence, message_id UNIQUE, session_id "claude:<uuid>" or
  "codex:<app|lane-name>:<id>", request_digest, payload_digest, payload, status,
  created_at, updated_at REAL, receipt)`. The row's `status` column is the
  message's state: the cockpit's `Outbox._receipt` overlays it on the stored
  receipt (`outbox.py:151-169`), whose own `status` is what the dispatch
  returned: `"dispatched"` for a streamed dispatch (`native_dispatch.py:778`),
  `"dispatched"` or `"delivery-unknown"` when delivery was unconfirmed (`:796`),
  `"delivered-live"` for a message handed to a live session
  (`session_catalog.py:2241`).
* The cockpit's terminal statuses are `finished`, `error` and `cancelled`
  (`TERMINAL`, `outbox.py:31`). `starting`, `dispatched`, `failover-dispatched`,
  `delivered-live` and `delivery-unknown` are `BLOCKING` (`outbox.py:34-35`),
  and `STATUSES` adds `queued` (`:36`); none of them is terminal. `finished` is written when the provider run
  finished with rc 0 and `error` when it did not (`broker.py:63-64`, applied by
  `Outbox.update_active`, `outbox.py:365-381`); `cancelled` is either a queued
  message withdrawn before dispatch (`Outbox.cancel`, receipt "Cancelled before
  dispatch.", `outbox.py:291-302`) or an unknown delivery a person marked
  handled (`Outbox.resolve_handled`, receipt `resolution: "handled"`,
  `outbox.py:312-327`).
* A Claude message went to the provider as a user record whose `uuid` is the
  message id (`claude_transport.py:86`). Only a streamed dispatch's receipt
  names it, as `native_message_id` (`native_dispatch.py:780`); a
  `delivered-live` receipt has none, so its history row's `turn_ref` is null.
* `cockpit-client/pending-messages.json` is the app's journal of sends whose
  broker acknowledgement it had not seen: `{session id: {"request": {"op":
  "enqueue", "message_id", "session_id", "prompt", "image_paths",
  "service_tier"}, "sourceImagePaths": [...]}}` (`CockpitStore.swift:867-870,
  1041-1064`, `SubfleetCLI.swift:7-24`).

What the import does with them: a terminal message of a Claude session whose
transcript is found becomes a read-only history row
(`ConversationStore.insert_legacy_history`) of that session's one conversation
(C-24.1). When the session has none, the import creates it with origin `legacy`
from the transcript, exactly as `conversation.open` creates a native one
(`catalog.claude_session`); a conversation the session already has keeps its
own origin. Every other message, and every journal entry whatever the outbox
says about its id, keeps its legacy owner and is reported with its disposition,
and so does its whole session (`held_sessions`), as does a session a live
cockpit worker is in (`cockpit_activity`). A journal, outbox or workers file
that exists and cannot be read, or a running broker, holds every session. Every
conversation bound to a held session, whatever its origin, is held
(`legacy_hold`) until a pass finds the session settled (`fence_bound_sessions`),
and so is one bound to it after the pass (`record_legacy_sessions`).
Nothing here ever queues, dispatches or sends a message. Nothing here writes
under the v1 state directory; the caller reads the outbox from a copy, and the
broker probe takes a shared lock on a read-only handle.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..sessions import transcripts
from .catalog import claude_session
from .store import LEGACY_OWNER, ConversationError, ConversationStore, canonical_native, canonical_uuid
from .turn import CANCELLED, COMPLETE, FAILED, TERMINAL_STATES

#: The cockpit's own terminal set (`outbox.py:31`).
TERMINAL = frozenset({"finished", "error", "cancelled"})
#: The cockpit's other statuses (`BLOCKING`, `outbox.py:34-35`, and `queued` from
#: `STATUSES`, `:36`): the legacy writer still owns these.
NON_TERMINAL = frozenset({"queued", "starting", "dispatched", "failover-dispatched", "delivered-live",
                          "delivery-unknown"})

JOURNAL = Path("cockpit-client") / "pending-messages.json"

#: The hold a pass puts on a conversation while the legacy writer may be using
#: its session (C-30.4, design D-17): the conversation's `legacy_hold` column,
#: beside the service's `blocked_by`. A held conversation gets no turn (C-24.5).
LEGACY_HOLD = LEGACY_OWNER


@dataclass(frozen=True)
class LegacyMessage:
    """One `messages` row of the cockpit outbox, as read."""

    sequence: int
    message_id: str
    session_id: str
    status: str
    created_at: float | None
    updated_at: float | None
    payload: dict[str, Any] | None          # None: the payload did not parse
    receipt: dict[str, Any] | None          # None: the receipt did not parse

    @property
    def provider(self) -> str:
        return self.session_id.partition(":")[0]

    @property
    def native_id(self) -> str:
        return canonical_native(self.session_id.partition(":")[2])

    @property
    def key(self) -> str:
        return session_key(self.session_id)


def _json_object(text: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(text) if isinstance(text, (str, bytes)) and text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_outbox(connection: sqlite3.Connection) -> list[LegacyMessage]:
    """Every row of the outbox, oldest first. The connection is to a copy."""
    connection.row_factory = sqlite3.Row
    rows = connection.execute("SELECT * FROM messages ORDER BY sequence").fetchall()
    return [LegacyMessage(sequence=int(row["sequence"]), message_id=str(row["message_id"] or ""),
                          session_id=str(row["session_id"] or ""), status=str(row["status"] or ""),
                          created_at=_number(row["created_at"]), updated_at=_number(row["updated_at"]),
                          payload=_json_object(row["payload"]), receipt=_json_object(row["receipt"]))
            for row in rows]


def history_state(message: LegacyMessage) -> tuple[str, str] | None:
    """The v2 state and reason of a terminal legacy message (design D-12), else None.

    `finished` is `complete`. `error` is `failed`, with the receipt's error code.
    A `cancelled` row a person marked handled after an unknown delivery is
    `failed` with reason `legacy-handled`, as v2 records a person's resolution of
    an ambiguous delivery (`message.resolve` settles `failed`); any other
    `cancelled` row was withdrawn before dispatch and is `cancelled`.
    """
    receipt = message.receipt or {}
    if message.status == "finished":
        return COMPLETE, "legacy-finished"
    if message.status == "error":
        code = receipt.get("error")
        return FAILED, (f"legacy-error: {code}"[:200] if isinstance(code, str) and code else "legacy-error")
    if message.status == "cancelled":
        if receipt.get("resolution") == "handled":
            return FAILED, "legacy-handled"
        return CANCELLED, "legacy-cancelled"
    return None


def history_settings(message: LegacyMessage) -> dict[str, Any]:
    """What the outbox recorded about a message's settings: only a service tier.

    The cockpit took the model and permission from its own session row at
    dispatch (`claude_transport.py:50-61`, `native_dispatch.py:764-767`) and
    never wrote them into the outbox, so they are null here rather than
    borrowed from the conversation.
    """
    payload = message.payload or {}
    return {"model": None, "effort": None, "fast": None, "permission": None, "auto_continue": None,
            "service_tier": payload.get("service_tier")}


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def session_key(session_id: str) -> str:
    """One spelling per session: `<provider>:<native id>`, a UUID in lower case.

    The v1 CLI stores a session id as typed (`cli.py:1077-1080`), and the
    cockpit resolves a Claude id in any case (`session_catalog.py:1307-1320`),
    while Claude Code names a transcript by the lower-case id and the store's
    unique binding compares ids exactly; so one session is always keyed by one
    spelling (review L1). A Codex id (`codex:<app|lane-name>:<uuid>`) is keyed by
    its thread id, the part a conversation binds. Anything that is not
    provider-qualified is returned as it is.
    """
    provider, _, rest = session_id.partition(":")
    native = rest.rpartition(":")[2] if provider == "codex" else rest
    if provider not in ("claude", "codex") or not native:
        return session_id
    return f"{provider}:{canonical_native(native)}"


def _iso(epoch: float | None) -> str:
    moment = datetime.fromtimestamp(epoch, UTC) if epoch is not None else datetime.now(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Activity:
    """What a pass read of the legacy writer besides its outbox and journal (review M1)."""

    #: Session key to why the cockpit is using it now: held, and every conversation of it fenced.
    sessions: dict[str, str] = field(default_factory=dict)
    #: Session key to why a live Claude process outside Subfleet is in it: no history is
    #: placed there this pass, but nothing is fenced, since dispatch and launch already
    #: make a turn there wait while the process lives (`external-writer`, C-26.3) and a
    #: hold set now would outlast it.
    live: dict[str, str] = field(default_factory=dict)
    #: Why any session may be in use (a running broker, a signal that could not be read).
    problem: str | None = None


@dataclass
class Result:
    """What one pass did with each legacy message and journal entry."""

    items: list[dict[str, Any]] = field(default_factory=list)
    imported: int = 0
    conversations_created: int = 0
    #: The reason every session is held, when the client journal could not be read.
    journal_problem: str | None = None
    #: The reason every session is held, when the outbox could not be read.
    outbox_problem: str | None = None
    #: The reason every session is held, when the cockpit may be using any of them.
    activity_problem: str | None = None

    def add(self, **item: Any) -> None:
        self.items.append(item)


def _item(message: LegacyMessage, disposition: str, **extra: Any) -> dict[str, Any]:
    return {"source": "outbox", "sequence": message.sequence, "message_id": message.message_id,
            "session_id": message.session_id, "status": message.status, "disposition": disposition, **extra}


def held_sessions(messages: Iterable[LegacyMessage], journal: Iterable[Mapping[str, Any]] = (),
                  activity: Activity | None = None) -> dict[str, str]:
    """Sessions the legacy writer may still be using, by `session_key`, each with the reason.

    A session with a message that is not terminal, with any journal entry, or
    that the cockpit is using now (`cockpit_activity`) stays with its legacy
    owner as a whole: a legacy-history conversation is continuable (C-30.2), and
    binding one to a session whose legacy turn may still be running or
    undelivered would make Subfleet a second writer there (C-26.3, design D-17).
    A journal entry holds its session whatever the outbox says about its id: the
    entry is a send the cockpit app has not seen acknowledged, and nothing read
    here shows what the app does with it next.
    """
    held: dict[str, str] = {}
    for message in messages:
        if message.status not in TERMINAL:
            held.setdefault(message.key, f"message {message.message_id} is {message.status or 'blank'}")
    for entry in journal:
        reason = f"the cockpit journal holds an unacknowledged send {entry.get('message_id')}"
        for named in (entry["session_id"], entry.get("key") or entry["session_id"]):
            held.setdefault(session_key(named), reason)
    for key, reason in (activity.sessions if activity else {}).items():
        held.setdefault(key, reason)
    return held


def journal_hold(problem: str | None) -> str | None:
    """Why every session is held when the client journal could not be read (C-30.4).

    The journal names the sessions with an unacknowledged send; unread, it could
    name any of them, so none is bound and every bound one is fenced.
    """
    return f"the cockpit journal could not be read ({problem}), so any session may have an unacknowledged send" \
        if problem else None


def outbox_hold(problem: str | None) -> str | None:
    """Why every session is held when the outbox could not be read (C-30.4, review M2).

    An outbox that exists and cannot be read could hold a message in flight in
    any session, exactly as an unreadable journal could; a missing one holds none.
    """
    return f"the cockpit outbox could not be read ({problem}), so any session may have a message in flight" \
        if problem else None


def import_outbox(store: ConversationStore, messages: Iterable[LegacyMessage], *,
                  projects: Path | None = None, journal: Iterable[Mapping[str, Any]] = (),
                  journal_problem: str | None = None, outbox_problem: str | None = None,
                  activity: Activity | None = None) -> Result:
    """Classify every message by itself and write terminal Claude ones as history.

    First, `fence_bound_sessions` holds or releases every conversation bound to
    a session, so nothing that goes wrong placing one session's history can stop
    it (review L2). Idempotent: a message already in the store is reported
    `already-imported` and nothing is written for it. A terminal message whose
    session cannot be placed this pass (held by the legacy writer, no
    transcript, a session that cannot continue here or whose transcript could
    not be read) is reported with that disposition and written by a later pass
    once that changes; one whose conversation already has messages of its own,
    whose id the store holds as its own message, whose row cannot be read, or
    whose session is not Claude's is reported and never placed. A session's
    history is written in legacy sequence order. `journal_problem` (the journal
    could not be read), `outbox_problem` (the outbox could not be read; then
    `messages` is empty) and `activity.problem` (the cockpit may be using any
    session) each hold every session.
    """
    messages = sorted(messages, key=lambda message: message.sequence)
    held = held_sessions(messages, journal, activity)
    activity_problem = activity.problem if activity else None
    everyone = "; ".join(reason for reason in (journal_hold(journal_problem), outbox_hold(outbox_problem),
                                                activity_problem) if reason) or None
    result = Result(journal_problem=journal_problem, outbox_problem=outbox_problem,
                    activity_problem=activity_problem)

    def hold(key: str) -> str | None:
        return held.get(key) or everyone

    store.record_legacy_sessions({**held, **({"*": everyone} if everyone else {})})
    fence_bound_sessions(store, hold, result)
    live = activity.live if activity else {}
    sessions: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.status not in TERMINAL:
            known = message.status in NON_TERMINAL
            result.add(**_item(message, "legacy-owned",
                               detail=None if known else "a status the cockpit never wrote"))
            continue
        state = history_state(message)
        try:
            message_id = canonical_uuid(message.message_id)
        except ConversationError:
            result.add(**_item(message, "unreadable-row", detail="the message id is not a canonical UUID"))
            continue
        if message.payload is None or not isinstance(message.payload.get("prompt"), str):
            result.add(**_item(message, "unreadable-row", detail="the payload has no prompt"))
            continue
        # Every field the row's history uses is checked here, before anything is
        # written for it: one malformed row is reported and the pass goes on
        # (review of 3c1a34e, finding 6).
        if not isinstance(message.payload.get("image_paths"), (list, type(None))):
            result.add(**_item(message, "unreadable-row", detail="the payload's image_paths is not a list"))
            continue
        if not isinstance(message.payload.get("service_tier"), (str, type(None))):
            result.add(**_item(message, "unreadable-row", detail="the payload's service_tier is not a string"))
            continue
        stored = store.one("SELECT conversation_id, origin FROM messages WHERE message_id=?", (message_id,))
        if stored is not None:
            if stored["origin"] == "legacy":
                result.add(**_item(message, "already-imported", state=state[0],
                                   conversation_id=stored["conversation_id"]))
            else:
                result.add(**_item(message, "message-id-conflict", conversation_id=stored["conversation_id"],
                                   detail="the store holds this id as a message of its own"))
            continue
        if message.provider != "claude" or not _is_uuid(message.native_id):
            # A Claude session id is a UUID; nothing else is looked up as a file name.
            result.add(**_item(message, "not-a-claude-session",
                               detail="this release places legacy history only in Claude sessions"))
            continue
        reason = hold(message.key) or live.get(message.key)
        if reason:
            result.add(**_item(message, "session-held-by-legacy-owner", state=state[0], detail=reason))
            continue
        try:
            created_at, updated_at = _iso(message.created_at), _iso(message.updated_at)
        except (OverflowError, OSError, ValueError):
            result.add(**_item(message, "unreadable-row", detail="a timestamp is out of range"))
            continue
        if message.native_id not in sessions:
            try:
                sessions[message.native_id] = _place(store, message.native_id, projects, result)
            except Exception as exc:          # one session's transcript never ends the pass (review L2)
                sessions[message.native_id] = {"disposition": "transcript-unreadable",
                                               "detail": f"reading its transcript raised {type(exc).__name__}"}
        place = sessions[message.native_id]
        if place.get("disposition"):
            result.add(**_item(message, place["disposition"], detail=place.get("detail"),
                               conversation_id=place.get("conversation_id")))
            continue
        receipt = message.receipt or {}
        native_message = receipt.get("native_message_id")
        try:
            row, created = store.insert_legacy_history(
                conversation_id=place["conversation_id"], message_id=message_id, text=message.payload["prompt"],
                state=state[0], state_reason=state[1], settings=history_settings(message),
                created_at=created_at, updated_at=updated_at,
                turn_ref=native_message if isinstance(native_message, str) and native_message else None)
        except ConversationError as exc:
            result.add(**_item(message, exc.reason, conversation_id=place["conversation_id"], detail=str(exc)))
            continue
        result.imported += int(created)
        images = len(message.payload.get("image_paths") or [])
        result.add(**_item(message, "history" if created else "already-imported", state=row["state"],
                           conversation_id=row["conversation_id"], seq=row["seq"],
                           **({"images_left_in_v1": images} if images else {})))
    return result


#: Why a pass lifts a hold: nothing it read says the legacy writer is using the session.
RELEASED = "no message of this session is unsettled and no journal entry names it"


def fence_bound_sessions(store: ConversationStore, hold: Callable[[str], str | None], result: Result) -> None:
    """Hold every conversation whose session is held; release it once not (C-30.4).

    Every conversation bound to a native session is looked at, whatever its
    origin (review M4): one the import made or put history in, one
    `conversation.open` made of a session the cockpit also continues, one a
    Subfleet turn started. `hold` takes a `session_key` and names why the legacy
    writer may be using that session (a cockpit message there that is not
    terminal, a journal entry naming it, a live cockpit worker in it) or, for
    every session, why any may be in use (a journal or outbox
    that cannot be read, a running broker). Holding a conversation sets its
    `legacy_hold` to the reason, which keeps every turn off it (C-24.5: no
    dispatch, no re-admission, no admission of a turn job already queued, and a
    running turn is stopped when the daemon adopts it), and reports
    `bound-session-held` with the conversation's own `blocked_by` and every
    message of it that is not settled, which runs once the hold lifts unless it
    is cancelled first. The first pass whose `hold` names no reason lifts the
    hold and reports `bound-session-released`. The caller records the held
    sessions first (`record_legacy_sessions`), so a conversation bound to one
    after the pass is bound held. The hold is the import's alone:
    `blocked_by` (`unfinished-turn`, `delivery-unknown`, `quarantined-turn`) is
    never read or written here, and no outcome, `conversation.unblock` or
    `message.resolve` touches the hold.
    """
    bound = store.query("SELECT * FROM conversations WHERE native_session_id IS NOT NULL "
                        "ORDER BY created_at, conversation_id")
    for row in bound:
        key = f"{row['provider']}:{canonical_native(row['native_session_id'])}"
        reason = hold(key)
        if reason:
            if row["legacy_hold"] != reason:
                store.set_legacy_hold(row["conversation_id"], reason)
            result.add(**_conversation_item(store, row, key, "bound-session-held", reason))
        elif row["legacy_hold"] is not None:
            store.set_legacy_hold(row["conversation_id"], None)
            result.add(**_conversation_item(store, row, key, "bound-session-released", None))


#: Why `retire` lifts a hold.
RETIRED = "the legacy cockpit is retired (--cockpit-retired)"


def retire(store: ConversationStore) -> Result:
    """Lift every legacy hold, forget every held session, and record the
    retirement so no later pass reads the cockpit again: the operator says it
    will never run again (C-30.4)."""
    result = Result()
    for row in store.retire_legacy():                     # one transaction: all of it, or nothing
        key = f"{row['provider']}:{canonical_native(row['native_session_id'])}"
        result.add(**{**_conversation_item(store, row, key, "bound-session-released", None), "detail": RETIRED})
    return result


def _conversation_item(store: ConversationStore, row: Mapping[str, Any], session_id: str, disposition: str,
                       reason: str | None) -> dict[str, Any]:
    unsettled = [{"message_id": message["message_id"], "state": message["state"]} for message in store.query(
        f"SELECT message_id, state FROM messages WHERE conversation_id=? "
        f"AND state NOT IN ({','.join('?' * len(TERMINAL_STATES))}) ORDER BY seq",
        (row["conversation_id"], *TERMINAL_STATES))]
    return {"source": "conversation", "conversation_id": row["conversation_id"], "session_id": session_id,
            "disposition": disposition, "legacy_hold": reason, "blocked_by": row["blocked_by"],
            "unsettled": unsettled, "detail": reason or RELEASED}


def _place(store: ConversationStore, native_id: str, projects: Path | None, result: Result) -> dict[str, Any]:
    """The conversation a session's history goes into, or why there is none yet.

    That is the session's one conversation (C-24.1: one per native session),
    whatever its origin, when it has no message of its own yet; otherwise a new
    conversation with origin `legacy`, created from the transcript.
    """
    existing = store.by_native("claude", native_id)
    if existing is not None:
        if store.one("SELECT 1 FROM messages WHERE conversation_id=? AND origin<>'legacy' LIMIT 1",
                     (existing["conversation_id"],)):
            return {"disposition": "conversation-has-own-messages", "conversation_id": existing["conversation_id"],
                    "detail": "history goes ahead of every message; this session's conversation already has some"}
        return {"conversation_id": existing["conversation_id"]}
    path = transcripts.transcript_path(native_id, projects)
    if path is None:
        return {"disposition": "transcript-not-found",
                "detail": f"no {native_id}.jsonl under {projects or transcripts.projects_dir()}"}
    facts = claude_session(path)
    if not facts.get("continuable"):
        return {"disposition": "session-not-continuable", "detail": facts.get("continue_blocker")}
    settings = {"model": facts["model_value"], "effort": None, "fast": False, "permission": facts["permission"],
                "auto_continue": True}
    conversation, created = store.create_conversation(
        provider="claude", workspace=facts["cwd"], workspace_kind="in-place", settings=settings, origin="legacy",
        native_session_id=native_id, title=facts.get("title"))
    result.conversations_created += int(created)
    return {"conversation_id": conversation["conversation_id"]}


def _qualified(value: Any) -> bool:
    """A provider-qualified session id, as the cockpit's own check has it (`outbox.py:83-88`)."""
    return isinstance(value, str) and value.startswith(("claude:", "codex:")) and bool(value.partition(":")[2])


def read_journal(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """The cockpit client's pending-send journal: its entries, or why it could not be read.

    The journal is an object keyed by provider-qualified session id, each entry's
    request naming the same session (`CockpitStore.swift:1031`, `:1059`). Any
    other shape is not read as a journal (review L3): `{"version": 2, "entries":
    {...}}` would otherwise hold sessions named `version` and `entries` and no
    real one. Nor is an entry with no request, or a request whose message id is
    not a string or whose image paths are not a list: each is checked before
    anything is counted, so the caller holds every session for it rather than
    the pass ending on it (review of 3c1a34e, finding 6).
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        return [], f"unreadable: {type(exc).__name__}"
    if not isinstance(value, dict):
        return [], "not an object keyed by session"
    entries = []
    for key, entry in value.items():
        if not _qualified(key):
            return [], "not an object keyed by session"
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict):
            return [], "an entry with no request"
        session_id = request.get("session_id")
        if not _qualified(session_id):
            return [], "not an object keyed by session"
        message_id, images = request.get("message_id"), request.get("image_paths")
        if not isinstance(message_id, (str, type(None))) or not isinstance(images, (list, type(None))):
            return [], "an entry whose request is not the cockpit's (its message_id or image_paths)"
        entries.append({"session_id": session_id, "key": key, "message_id": message_id,
                        "images": len(images or [])})
    return entries, None


def journal_items(entries: Iterable[Mapping[str, Any]], outbox: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every journal entry keeps its legacy owner, and holds its session, whatever
    the outbox status (C-30.4); that status, if the broker took the message, is
    reported beside it."""
    return [{"source": "client-journal", "message_id": entry.get("message_id"), "session_id": entry["session_id"],
             "status": outbox.get(str(entry.get("message_id"))), "disposition": "legacy-owned",
             "detail": "an unacknowledged cockpit send; the import never sends it"}
            for entry in entries]


# --- whether the cockpit is using a session now (review M1) ---------------------

#: The broker's lock and the workers' registry, under the v1 state (manifest rows
#: `locks` and `sessions-kit`, `importer.MANIFEST`).
BROKER_LOCK = "broker.lock"
WORKERS = "native-workers.json"


def cockpit_activity(v1_state: Path, *, claude_dir: Path | None = None,
                     owned: Callable[[int | None], bool] | None = None) -> Activity:
    """Whether the legacy cockpit is using sessions now, read without writing anything.

    Nothing pending in the outbox or the journal does not mean the cockpit is
    not in a session: selecting one in the app starts an idle `claude -p
    --resume` worker in it (`native_dispatch.py:161-171`), reaped only when a
    later worker is made (`:127-135`), and the broker never kills a provider
    process when it stops (`broker.py:339`). Three signals, each read only:

    * The broker holds an exclusive `flock` on `S/broker.lock` for its lifetime
      (`broker.py:296-305`). A shared, non-blocking `flock` on a read-only handle
      is refused while it does; one that is granted is released at once. The
      file is never created. A running broker may dispatch into any session, so
      every session is held.
    * `S/native-workers.json` names each live worker's session with its pid
      (`native_dispatch.py:85-91`); a pid that is alive holds that session.
    * A live Claude process outside Subfleet registered for a session in
      `<claude dir>/sessions` (`catalog._live_claude_sessions`'s rule: a live
      pid without `SUBFLEET_ATTEMPT` in its environment, so a Subfleet turn that
      kept running across the restart is not one) keeps history out of that
      session this pass (`Activity.live`). It fences nothing: dispatch and launch
      make a turn there wait while the process lives (C-26.3), and a hold would
      outlast it.

    A signal that exists and cannot be read holds every session, as an
    unreadable journal does.
    """
    from ..sessions import registry
    from .catalog import _subfleet_owned
    owned = owned or _subfleet_owned
    activity = Activity()
    problems: list[str] = []
    broker = _broker_running(Path(v1_state) / BROKER_LOCK)
    if broker is True:
        problems.append(f"the cockpit broker holds {BROKER_LOCK}, so it may dispatch into any session")
    elif broker is not False:
        problems.append(f"{BROKER_LOCK} could not be probed ({broker}), so the cockpit broker may be running")
    path = Path(v1_state) / WORKERS
    try:
        workers = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        workers = {}
    except (OSError, ValueError) as exc:
        workers = None
        problems.append(f"{WORKERS} could not be read ({type(exc).__name__}), so a cockpit worker may be live "
                        "in any session")
    if workers is not None and not isinstance(workers, dict):
        problems.append(f"{WORKERS} is not an object keyed by session, so a cockpit worker may be live "
                        "in any session")
        workers = {}
    for key, entry in (workers or {}).items():
        pid = entry.get("pid") if isinstance(entry, dict) else None
        if isinstance(pid, int) and not isinstance(pid, bool) and registry._pid_alive(pid):
            activity.sessions.setdefault(session_key(str(key)), f"the cockpit's worker pid {pid} is live in it")
    sessions = (Path(claude_dir) if claude_dir is not None else transcripts.claude_dir()) / "sessions"
    for row in registry.rows(sessions):
        if row.alive and not owned(row.pid):
            activity.live.setdefault(session_key(f"claude:{row.session_id}"),
                                     f"a live Claude process outside Subfleet (pid {row.pid}) holds it")
    activity.problem = "; ".join(problems) or None
    return activity


def _broker_running(lock: Path) -> bool | str:
    """True while another process holds `lock` exclusively, False when none does
    or it does not exist, otherwise why it could not be told."""
    try:
        handle = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return False
    except OSError as exc:
        return type(exc).__name__
    try:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as exc:
        return type(exc).__name__
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        os.close(handle)
