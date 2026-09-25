"""The legacy cockpit import (C-30.4, design §13).

What the v1 state directory holds, read on 2026-09-24 from the cockpit's source
(`~/chief-of-staff-worktrees/subfleet-traycer-port`, whose `outbox.py`,
`broker.py`, `native_dispatch.py` and `claude_transport.py` are untracked
working-tree files) and from a private copy of the real outbox, whose rows have
exactly the shapes that code writes:

* `outbox.sqlite3` is the desktop cockpit's message outbox, not a notice
  outbox. One row per message the cockpit's composer sent into a native
  session: `messages(sequence, message_id UNIQUE, session_id "claude:<uuid>" or
  "codex:<id>", request_digest, payload_digest, payload, status, created_at,
  updated_at REAL, receipt)`. The row's `status` column is the message's state:
  the cockpit's `Outbox._receipt` overlays it on the stored receipt, whose own
  `status` stays the dispatch-time `"dispatched"` (`outbox.py:151-169`).
* The cockpit's terminal statuses are `finished`, `error` and `cancelled`
  (`TERMINAL`, `outbox.py:31`); `queued`, `starting`, `dispatched`,
  `failover-dispatched`, `delivered-live` and `delivery-unknown` are not
  (`BLOCKING`, `outbox.py:33-34`). `finished` is written when the provider run
  finished with rc 0 and `error` when it did not (`broker.py:63-64`, applied by
  `Outbox.update_active`, `outbox.py:365-381`); `cancelled` is either a queued
  message withdrawn before dispatch (`Outbox.cancel`, receipt "Cancelled before
  dispatch.", `outbox.py:291-302`) or an unknown delivery a person marked
  handled (`Outbox.resolve_handled`, receipt `resolution: "handled"`,
  `outbox.py:312-327`).
* A Claude message went to the provider as a user record whose `uuid` is the
  message id (`claude_transport.py:86`), and the dispatch receipt names it as
  `native_message_id` (`native_dispatch.py:780`).
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
and so does its whole session (`held_sessions`); a journal that cannot be read
holds every session. A conversation an earlier pass bound whose session is held
again is blocked `legacy-owner` until a pass finds the session settled
(`fence_bound_sessions`). Nothing here ever queues, dispatches or sends a
message. Nothing here writes under the v1 state directory; the caller reads the
outbox from a copy.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..sessions import transcripts
from .catalog import claude_session
from .store import ConversationError, ConversationStore, canonical_uuid
from .turn import CANCELLED, COMPLETE, FAILED

#: The cockpit's own terminal set (`outbox.py:31`).
TERMINAL = frozenset({"finished", "error", "cancelled"})
#: The cockpit's other statuses (`outbox.py:33-35`): the legacy writer still owns these.
NON_TERMINAL = frozenset({"queued", "starting", "dispatched", "failover-dispatched", "delivered-live",
                          "delivery-unknown"})

JOURNAL = Path("cockpit-client") / "pending-messages.json"

#: The block a pass puts on a conversation it bound earlier while the legacy
#: writer may be using that session again (C-30.4, design D-17). A blocked
#: conversation gets no turn (C-24.5, `ConversationStore.next_dispatchable`).
LEGACY_HOLD = "legacy-owner"


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
        return self.session_id.partition(":")[2]


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


def _iso(epoch: float | None) -> str:
    moment = datetime.fromtimestamp(epoch, UTC) if epoch is not None else datetime.now(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Result:
    """What one pass did with each legacy message and journal entry."""

    items: list[dict[str, Any]] = field(default_factory=list)
    imported: int = 0
    conversations_created: int = 0
    #: The reason every session is held, when the client journal could not be read.
    journal_problem: str | None = None

    def add(self, **item: Any) -> None:
        self.items.append(item)


def _item(message: LegacyMessage, disposition: str, **extra: Any) -> dict[str, Any]:
    return {"source": "outbox", "sequence": message.sequence, "message_id": message.message_id,
            "session_id": message.session_id, "status": message.status, "disposition": disposition, **extra}


def held_sessions(messages: Iterable[LegacyMessage], journal: Iterable[Mapping[str, Any]] = ()) -> dict[str, str]:
    """Sessions the legacy writer may still be using, each with the reason.

    A session with a message that is not terminal, or with any journal entry,
    stays with its legacy owner as a whole: a legacy-history conversation is
    continuable (C-30.2), and binding one to a session whose legacy turn may
    still be running or undelivered would make Subfleet a second writer there
    (C-26.3, design D-17). A journal entry holds its session whatever the outbox
    says about its id: the entry is a send the cockpit app has not seen
    acknowledged, and nothing read here shows what the app does with it next.
    """
    messages = list(messages)
    held: dict[str, str] = {}
    for message in messages:
        if message.status not in TERMINAL:
            held.setdefault(message.session_id, f"message {message.message_id} is {message.status or 'blank'}")
    for entry in journal:
        held.setdefault(entry["session_id"], f"the cockpit journal holds an unacknowledged send "
                                             f"{entry.get('message_id')}")
    return held


def journal_hold(problem: str | None) -> str | None:
    """Why every session is held when the client journal could not be read (C-30.4).

    The journal names the sessions with an unacknowledged send; unread, it could
    name any of them, so none is bound and every bound one is fenced.
    """
    return f"the cockpit journal could not be read ({problem}), so any session may have an unacknowledged send" \
        if problem else None


def import_outbox(store: ConversationStore, messages: Iterable[LegacyMessage], *,
                  projects: Path | None = None, journal: Iterable[Mapping[str, Any]] = (),
                  journal_problem: str | None = None) -> Result:
    """Classify every message by itself and write terminal Claude ones as history.

    Idempotent: a message already in the store is reported `already-imported`
    and nothing is written for it. A terminal message whose session cannot be
    placed this pass (held by the legacy writer, no transcript, a session that
    cannot continue here, a conversation that already has messages of its own)
    is reported with that disposition and written by a later pass once that
    changes. A session's history is written in legacy sequence order.
    `journal_problem` is `read_journal`'s reason the journal could not be read:
    then every session is held (`journal_hold`). Last, `fence_bound_sessions`
    blocks or releases the conversations earlier passes bound.
    """
    messages = sorted(messages, key=lambda message: message.sequence)
    held = held_sessions(messages, journal)
    everyone = journal_hold(journal_problem)
    result = Result(journal_problem=journal_problem)
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
        reason = held.get(message.session_id) or everyone
        if reason:
            result.add(**_item(message, "session-held-by-legacy-owner", state=state[0], detail=reason))
            continue
        if message.native_id not in sessions:
            sessions[message.native_id] = _place(store, message.native_id, projects, result)
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
                created_at=_iso(message.created_at), updated_at=_iso(message.updated_at),
                turn_ref=native_message if isinstance(native_message, str) and native_message else None)
        except ConversationError as exc:
            result.add(**_item(message, exc.reason, conversation_id=place["conversation_id"], detail=str(exc)))
            continue
        result.imported += int(created)
        images = len(message.payload.get("image_paths") or [])
        result.add(**_item(message, "history" if created else "already-imported", state=row["state"],
                           conversation_id=row["conversation_id"], seq=row["seq"],
                           **({"images_left_in_v1": images} if images else {})))
    fence_bound_sessions(store, lambda session_id: held.get(session_id) or everyone, result)
    return result


def fence_bound_sessions(store: ConversationStore, hold: Callable[[str], str | None], result: Result) -> None:
    """Block each conversation the import bound whose session is held; release it once not (C-30.4).

    A conversation the import bound is one that holds legacy history, or that the
    import created (origin `legacy`); a later pass can find its session held again
    (a new cockpit message there that is not terminal, a journal entry naming
    it, a journal that cannot be read). Holding it means `blocked_by:
    "legacy-owner"`, which keeps every turn off it (C-24.5), and a
    `bound-session-held` item. The first pass whose `hold` names no reason lifts
    that block and reports `bound-session-released`. A pass never replaces
    another block (`unfinished-turn`, `delivery-unknown`, `quarantined-turn`
    each wait for their own resolution) and never lifts one it did not set; a
    conversation blocked for another reason is reported with that block. A
    conversation of a held session that holds no legacy history was never bound
    by the import and is not fenced here.
    """
    bound = store.query(
        "SELECT * FROM conversations c WHERE provider='claude' AND native_session_id IS NOT NULL "
        "AND (origin='legacy' OR blocked_by=? OR EXISTS "
        "(SELECT 1 FROM messages m WHERE m.conversation_id=c.conversation_id AND m.origin='legacy')) "
        "ORDER BY created_at, conversation_id", (LEGACY_HOLD,))
    for row in bound:
        session_id = f"claude:{row['native_session_id']}"
        reason = hold(session_id)
        blocked_by = row["blocked_by"]
        if reason:
            if blocked_by is None:
                store.update_conversation(row["conversation_id"], blocked_by=LEGACY_HOLD)
                blocked_by = LEGACY_HOLD
            elif blocked_by != LEGACY_HOLD:
                reason += f"; it stays blocked {blocked_by!r}, which the legacy hold does not replace"
            result.add(**_conversation_item(row, session_id, "bound-session-held", blocked_by, reason))
        elif blocked_by == LEGACY_HOLD:
            store.update_conversation(row["conversation_id"], blocked_by=None)
            result.add(**_conversation_item(row, session_id, "bound-session-released", None,
                                            "no message of this session is unsettled and no journal entry names it"))


def _conversation_item(row: Mapping[str, Any], session_id: str, disposition: str, blocked_by: str | None,
                       detail: str) -> dict[str, Any]:
    return {"source": "conversation", "conversation_id": row["conversation_id"], "session_id": session_id,
            "disposition": disposition, "blocked_by": blocked_by, "detail": detail}


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


def read_journal(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """The cockpit client's pending-send journal: its entries, or why it could not be read."""
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
        request = entry.get("request") if isinstance(entry, dict) else None
        request = request if isinstance(request, dict) else {}
        session_id = request.get("session_id") if isinstance(request.get("session_id"), str) else str(key)
        entries.append({"session_id": session_id, "message_id": request.get("message_id"),
                        "images": len(request.get("image_paths") or [])})
    return entries, None


def journal_items(entries: Iterable[Mapping[str, Any]], outbox: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every journal entry keeps its legacy owner, and holds its session, whatever
    the outbox status (C-30.4); that status, if the broker took the message, is
    reported beside it."""
    return [{"source": "client-journal", "message_id": entry.get("message_id"), "session_id": entry["session_id"],
             "status": outbox.get(str(entry.get("message_id"))), "disposition": "legacy-owned",
             "detail": "an unacknowledged cockpit send; the import never sends it"}
            for entry in entries]
