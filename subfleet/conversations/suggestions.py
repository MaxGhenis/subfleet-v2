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
every error a suggestion records, logs or returns comes from fixed templates
and validated errno numbers, never caller text or exception rendering
(`scrub_error`). Scrubbing and marker-safe bounds remain defence in depth.
A Start's `decided_by` is scrubbed the
same way (`scrubbed_line`). No cut, of these or of a title, summary or listed
brief, splits what scrubbing put in (`bounded`): no suggestion field or event
holds a value the scrubber recognizes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from typing import Any

from ..sessions import scrub_release
from ..sessions.handoff import REDACTION_MARKERS, _head_scrub_secrets, scrub_secrets, shown_by_both
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


def _twice(scrub, text: str, between) -> tuple[str, int]:
    """`text` scrubbed whole by `scrub`, changed by `between` (a fold, a brief's line
    ends), and scrubbed whole again: one scrubber's pipeline for a two-pass mode."""
    clean, count = scrub(text, strip_reminders=True, whole=True)
    clean, again = scrub(between(clean), strip_reminders=True, whole=True)
    return clean, count + again


def _fold(text: str) -> str:
    return " ".join(text.split())


def _line_ends(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _both(text: str, between) -> tuple[str, str, int]:
    """A two-pass mode (`_twice`) as release/217 runs it (`scrub_release`, frozen) and as
    the head does (`_head_scrub_secrets`), each over `text` with its own fold and second
    pass (C-23.14, #239 round 8): release's output, the head's, and the larger count.
    Composing each pass instead loses what release's second pass reads in its own fold
    (review r8, P2-1)."""
    released, count = _twice(scrub_release.scrub_secrets, text, between)
    head, head_count = _twice(_head_scrub_secrets, text, between)
    return released, head, max(count, head_count)


def _released_first(text: str, between) -> tuple[str, int]:
    """A two-pass mode: release's output of it, keeping only the runs the head's output
    of it holds too (`handoff.shown_by_both`), so it shows nothing either hides."""
    released, head, count = _both(text, between)
    return shown_by_both(released, head), count


def _scrub_line(text: str) -> tuple[str, int]:
    """Scrub raw text before folding; scrub any shapes folding creates too: release's
    line, keeping only the runs the head's line holds too (`_released_first`)."""
    return _released_first(text, _fold)


def scrubbed_line(text: str, limit: int = ERROR_MAX) -> str:
    """`text` scrubbed whole (C-25.5) first, so no secret is cut into a fragment the
    rules no longer know, then folded and scrubbed again, then a marker-safe cut
    to at most `limit` characters (`bounded`). Release's line is kept only for the runs
    the head's line holds, then cut (`_line_bounded`)."""
    return _line_bounded(text, limit, "…")


def _line_bounded(text: str, limit: int, mark: str = "") -> str:
    """A folded line cut to `limit` (`bounded`): release's line (`scrub_release`) with each
    run the head's line does not hold replaced (`shown_by_both`), then cut as `bounded`
    cuts a line, so it shows nothing either line hides (C-23.14). Cut first, the part-word
    at the cut would be a run the head's line holds only whole, so it became `[REDACTED]`
    and an ordinary word was lost (#239 review r9b, P2-2); the cut can now keep a
    character release's own cut left out, but none either scrub hides."""
    released, head, _ = _both(text, _fold)
    return bounded(shown_by_both(released, head), limit, mark)


# Public diagnostics are selected from this vocabulary, never rendered from an
# exception's message, args, filenames or repair text. Unknown reasons/classes
# fall back to a fixed diagnostic. Keep scrubbed_line as defence in depth.
ERROR_MESSAGES = {
    "bad-args": "suggestion arguments must have the documented shape",
    "bad-request-id": "request_id must be a canonical lowercase UUID",
    "bad-suggestion-id": "suggestion_id must name a suggestion",
    "bad-session-id": "session_id cannot be recorded safely",
    "bad-calling-job": "calling_job cannot be recorded safely",
    "bad-provider": "provider is claude or codex",
    "bad-title": "title must contain 1 to 60 characters after secrets are removed",
    "bad-tldr": "tldr must contain 1 to 300 characters after secrets are removed",
    "bad-brief": "brief must contain text and fit within 32768 UTF-8 bytes after secrets are removed",
    "bad-model": "model must name a known alias or model id in at most 80 characters",
    "unknown-model": "model must name an active model this fleet routes",
    "no-model": "this fleet routes no model for the suggestion's provider",
    "bad-worktree": "worktree must be true or false",
    "bad-workspace": "workspace must be an existing directory",
    "bad-settings": "start settings must use the documented model and permission values",
    "person-only": "starting or dismissing a suggestion is a person's decision",
    "confirm-widen": "a policy above Ask needs confirm_widen: true",
    "codex-read-only": "Codex conversations are read-only until the never-rules hook is shown firing in a live app-server turn",
    "protected-workspace": "the folder is protected from writable conversations",
    "protected-branch": "a writable conversation cannot start on main or master without permission",
    "git-unavailable": "git could not inspect the suggestion's folder",
    "not-a-repository": "a worktree conversation needs a git repository",
    "worktree-failed": "git worktree add failed",
    "unknown-suggestion": "no such suggestion",
    "unknown-conversation": "no such origin conversation",
    "suggestion-moved": "the suggestion changed meanwhile; read it again",
    "suggestion-id-conflict": "request id already used for a different suggestion",
    "too-many-suggestions": "too many suggestions are open; withdraw or dismiss stale suggestions",
    "not-open": "the suggestion is closed and cannot start",
    "not-starting": "the suggestion is not starting this conversation",
    "not-the-brief": "only the message the claim chose starts the suggestion",
    "conversation-not-empty": "the conversation this start made already holds a message",
    "conversation-starting": "the conversation is waiting for its suggestion's first message",
    "not-creator": "only the conversation or session that suggested it may withdraw it",
    "too-late": "the person already started the suggestion",
    "unknown-op": "the daemon is older than this CLI and has no suggestions",
}
ERROR_FIXES = {
    "unknown-model": "pick a model from models.list",
    "bad-model": "pick a model from models.list",
    "person-only": "answer it in the Subfleet app",
    "confirm-widen": "confirm the permission in the Subfleet app",
    "worktree-failed": "start again to finish the suggestion",
    "git-unavailable": "try again",
    "bad-workspace": "choose a folder or use a new scratch folder",
    "not-a-repository": "choose a git checkout, or use the folder in place",
    "protected-workspace": "choose a narrower directory, or Ask or read-only",
    "protected-branch": "choose another branch",
    "too-many-suggestions": "withdraw or dismiss stale suggestions",
    "not-creator": "dismiss it in the Subfleet app",
    "unknown-op": "subfleet daemon restart",
}
SUGGESTION_OPS = frozenset("suggestion." + action for action in ("create", "list", "start", "dismiss", "withdraw"))
# Only these fixed context templates may precede an error. Caller text is ignored.
_ERROR_PREFIXES = frozenset(("cannot read the brief: ", "cannot resolve the workspace: ",
                             "invalid arguments: "))
PROTECTED_FOLDERS = frozenset((".claude", ".codex", ".subfleet", "state root", "lane home"))
WORKSPACE_REFUSALS = {
    ("bad-workspace", "secret-shaped"): (
        "the folder's path cannot be used because it looks like it holds a secret",
        "choose another folder or use a new scratch folder"),
    ("protected-workspace", "unreadable-spelling"): (
        "the folder cannot be checked because a protected path's spelling cannot be read",
        "make every directory above it searchable, or choose Ask or read-only"),
    ("bad-workspace", "gone"): ("the suggestion's folder is gone", "dismiss it or restore its folder"),
    ("not-a-repository", "no-longer-repository"): (
        "the suggestion's folder is no longer a git checkout, and it was to start in a worktree of its own",
        "dismiss it or restore its checkout"),
}


class SuggestionError(ConversationError):
    """A refusal with structural detail, separate from untrusted exception text."""

    def __init__(self, reason: str, message: str, code: int = 2, fix: str | None = None,
                 *, details: Any = None):
        super().__init__(reason, message, code, fix)
        self.details = details


def error_details(error: Any) -> dict:
    details = (error.details if isinstance(error, SuggestionError) else
               getattr(error, "suggestion_details", None))
    if type(details) is not dict:
        return {}
    reason = error_reason(error)
    status = details.get("workspace_status")
    if type(status) is str and (reason, status) in WORKSPACE_REFUSALS:
        return {"workspace_status": status}
    if reason == "protected-workspace":
        name = details.get("protected_folder")
        if type(name) is str and name in PROTECTED_FOLDERS:
            return {"protected_folder": name}
    if reason == "not-open":
        state = details.get("state")
        if type(state) is str and state in CLOSED_STATES:
            return {"state": state}
    if reason == "too-many-suggestions":
        scope, count, maximum = (details.get(key) for key in ("scope", "count", "maximum"))
        if (type(scope) is str and scope in ("all", "this conversation", "this session", "callers with no session")
                and all(type(value) is int and 0 <= value <= 2 ** 63 - 1 for value in (count, maximum))):
            return {"scope": scope, "count": count, "maximum": maximum}
    return {}


def error_message(reason: str, details: dict) -> str:
    status = details.get("workspace_status")
    if type(status) is str and (reason, status) in WORKSPACE_REFUSALS:
        return WORKSPACE_REFUSALS[reason, status][0]
    if reason == "protected-workspace" and details:
        return f"the folder contains protected {details['protected_folder']}; writable conversations cannot start here"
    if reason == "not-open" and details:
        return f"the suggestion was {details['state']}; it cannot start"
    if reason == "too-many-suggestions" and details:
        count, maximum, scope = (details[key] for key in ("count", "maximum", "scope"))
        if scope == "all":
            return f"{count} suggestions are open; at most {maximum} are kept"
        return f"{scope} has {count} open suggestions; at most {maximum}"
    return ERROR_MESSAGES[reason]


def error_reason(error: Any) -> str | None:
    reason = (error.reason if isinstance(error, ConversationError) else
              getattr(error, "suggestion_reason", None))
    return reason if type(reason) is str and reason in ERROR_MESSAGES else None


def error_code(error: Any) -> int:
    from ..contracts import Exit
    code = getattr(error, "code", None)
    if type(code) in (int, Exit) and code in (1, 2, 7, 69):
        return int(code)
    return 2 if isinstance(error, (ValueError, TypeError, KeyError)) else 1


def error_fix(error: Any) -> str | None:
    reason = error_reason(error)
    status = error_details(error).get("workspace_status")
    if (reason, status) in WORKSPACE_REFUSALS:
        return WORKSPACE_REFUSALS[reason, status][1]
    return ERROR_FIXES.get(reason)


def scrub_error(error: Any, *, prefix: str = "") -> str:
    """C-25.7's structural boundary: templates and checked context only.

    No exception rendering occurs here. Even strings passed by old callers are
    untrusted and receive a fixed fallback. The scrubber remains defence in depth.
    """
    reason = error_reason(error)
    if reason is not None:
        text = reason + ": " + error_message(reason, error_details(error))
    else:
        # Fixed class names also cover subclasses with arbitrary __str__/__repr__.
        name = next((name for kind, name in ((KeyError, "KeyError"), (UnicodeError, "UnicodeError"),
                    (ValueError, "ValueError"), (TypeError, "TypeError"), (OSError, "OSError"),
                    (sqlite3.Error, "sqlite3.Error")) if isinstance(error, kind)), "Exception")
        text = name + ": the suggestion operation failed"
        if isinstance(error, OSError) and type(error.errno) is int and 0 < error.errno < 256:
            text += f" (errno {error.errno}: {os.strerror(error.errno)})"
    context = prefix if type(prefix) is str and prefix in _ERROR_PREFIXES else ""
    return scrubbed_line(context + text)


def response_details(message: Any) -> dict:
    """Extract checked context from complete canonical wire templates."""
    if type(message) is not str:
        return {}
    for (reason, status), (template, _) in WORKSPACE_REFUSALS.items():
        if message == reason + ": " + template:
            return {"workspace_status": status}
    for name in PROTECTED_FOLDERS:
        details = {"protected_folder": name}
        if message == "protected-workspace: " + error_message("protected-workspace", details):
            return details
    for state in CLOSED_STATES:
        if message == "not-open: " + error_message("not-open", {"state": state}):
            return {"state": state}
    # Recognize only complete templates with canonical, bounded integer fields.
    match = re.fullmatch(r"too-many-suggestions: (?:(?P<all>[0-9]{1,19}) suggestions are open; "
                         r"at most (?P<global_max>[0-9]{1,19}) are kept|"
                         r"(?P<scope>this conversation|this session|callers with no session) has "
                         r"(?P<count>[0-9]{1,19}) open suggestions; at most (?P<maximum>[0-9]{1,19}))", message)
    if match:
        details = {"scope": "all" if match["all"] is not None else match["scope"],
                   "count": int(match["all"] if match["all"] is not None else match["count"]),
                   "maximum": int(match["global_max"] if match["all"] is not None else match["maximum"])}
        valid = error_details(SuggestionError("too-many-suggestions", "", details=details))
        if valid and message == "too-many-suggestions: " + error_message("too-many-suggestions", valid):
            return valid
    return {}


def response_reason(message: Any) -> str | None:
    """Validate a daemon wire message against our template vocabulary, for the CLI.

    Return an enum key, never wire text. Exact legacy unknown-op templates keep
    old-daemon discovery working without allowing arbitrary diagnostic prose.
    """
    if type(message) is not str:
        return None
    for reason, template in ERROR_MESSAGES.items():
        if message == reason + ": " + template:
            return reason
    details = response_details(message)
    if details:
        if "workspace_status" in details:
            return next(reason for reason, status in WORKSPACE_REFUSALS
                        if status == details["workspace_status"])
        if "protected_folder" in details:
            return "protected-workspace"
        return "not-open" if "state" in details else "too-many-suggestions"
    if message in tuple("unknown op '" + op + "'" for op in SUGGESTION_OPS):
        return "unknown-op"
    from ..protocol import REFUSAL_MESSAGES
    if message == REFUSAL_MESSAGES["unknown-op"]:
        return "unknown-op"
    return None


# Older daemon builds may have persisted arbitrary diagnostics. Views/events must
# not republish them: reconstruct only a member of the current vocabulary.
_PUBLIC_ERROR_TEXT = {reason + ": " + template: reason + ": " + template
                      for reason, template in ERROR_MESSAGES.items()}
for _name in ("KeyError", "UnicodeError", "ValueError", "TypeError", "OSError", "sqlite3.Error", "Exception"):
    _text = _name + ": the suggestion operation failed"
    _PUBLIC_ERROR_TEXT[_text] = _text
for _errno in range(1, 256):
    _text = f"OSError: the suggestion operation failed (errno {_errno}: {os.strerror(_errno)})"
    _PUBLIC_ERROR_TEXT[_text] = _text


def stored_error(error: Any) -> str | None:
    if error is None:
        return None
    if type(error) is str:
        if error in _PUBLIC_ERROR_TEXT or response_details(error):
            return error
    return "Exception: the suggestion operation failed"


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
        raise ConversationError("not-starting", "the suggestion is not starting this conversation", code=1)
    if tx.execute("SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1", (conversation_id,)).fetchone():
        raise ConversationError("conversation-not-empty", "the conversation this start made already holds a "
                                "message, so the brief cannot be its first", code=1)


def _line(value: Any, name: str, limit: int) -> tuple[str, int]:
    """Admit the original length; scrub before folding, then bound the clean line."""
    if not isinstance(value, str) or not value.strip():
        raise ConversationError(f"bad-{name}", f"{name} must be 1 to {limit} characters")
    _, count = _scrub_line(value)
    original_length = len(" ".join(value.split()))
    if original_length > limit:
        raise ConversationError(f"bad-{name}", f"{name} must be 1 to {limit} characters (it is {original_length})")
    clean = _line_bounded(value, limit)
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
    # A bare CR can become a header boundary: scrub shapes normalization creates,
    # just as _scrub_line scrubs the shapes whitespace folding creates. Release's two
    # passes and the head's both read the brief, and release's output is kept for the
    # runs the head's holds, as the line's is (`_released_first`).
    clean, count = _released_first(brief, _line_ends)
    brief = _line_ends(brief)
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
            "start_error": stored_error(row["start_error"]), "started_conversation_id": row["started_conversation_id"],
            "started_message_id": row["started_message_id"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def _event_body(row: dict) -> str:
    """The `suggestion` event's data: the whole brief when the row cap allows it."""
    body = json.dumps(view(row, full_brief=True), separators=(",", ":"), ensure_ascii=False)
    if len(body.encode()) > EVENT_ROW_MAX:
        body = json.dumps(view(row), separators=(",", ":"), ensure_ascii=False)
    return body


def event_view(data: Any) -> dict:
    """Replay retained events through today's diagnostic vocabulary (C-25.7).

    Keep the recorded state and admitted content, rather than replacing an old
    event with the current row. Unknown diagnostic fields cannot be republished.
    """
    if not isinstance(data, dict):
        return {}
    fields = ("suggestion_id", "state", "title", "tldr", "brief", "brief_truncated", "brief_chars",
              "origin_conversation_id", "origin_message_id", "creator_session", "workspace", "worktree",
              "workspace_kind", "repository", "provider", "model", "decided_by", "decided_at", "start_error",
              "started_conversation_id", "started_message_id", "created_at", "updated_at")
    shown = {key: data[key] for key in fields if key in data}
    if "start_error" in shown:
        shown["start_error"] = stored_error(shown["start_error"])
    return shown


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
        raise ConversationError("unknown-suggestion", "no such suggestion")
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
            raise ConversationError("unknown-suggestion", "no such suggestion")
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
            raise ValueError("invalid suggestion workspace plan")
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
                raise ConversationError("unknown-conversation", "no such origin conversation")
            marks = ",".join("?" * len(OPEN_STATES))
            total = tx.execute(f"SELECT COUNT(*) FROM suggestions WHERE state IN ({marks})", OPEN_STATES).fetchone()[0]
            if total >= open_max:
                raise SuggestionError("too-many-suggestions", "too many suggestions are open", code=7,
                                      details={"scope": "all", "count": total, "maximum": open_max})
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
                raise SuggestionError("too-many-suggestions", "too many suggestions are open", code=7,
                                      details={"scope": whose, "count": mine, "maximum": per_origin})
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
                raise SuggestionError("not-open", "the suggestion cannot start", code=7,
                                      details={"state": row["state"]})
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

    def note_error(self, suggestion_id: str, error: Any) -> dict:
        """A start that could not finish keeps its claim and says why; Start again
        (or the daemon's own retry) finishes it. Only the fixed diagnostic vocabulary is stored and published;
        caller-rendered strings receive a generic fallback (C-25.7)."""
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
