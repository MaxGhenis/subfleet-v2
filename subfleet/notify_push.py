"""Layer 4 of notice delivery: the push that wakes an idle session (C-15.2, C-15.7).

C-15.2 ranks the delivery layers most to least reliable: `subfleet wait` in a
background Bash call, the PostToolUse `asyncRewake` hook (`subfleet/hooks.py`),
the SessionStart and UserPromptSubmit hooks, and last a best-effort socket push
into the session's own inbox. The first three need the session to act: to have
armed a waiter, to make a Bash call, or to start a turn. A session that
dispatched a job and then went idle does none of them, so before the daemon ran
this layer it learned of the job's end only when someone next typed into it
(2026-10-02: four reviews finished between 17:34Z and 22:10Z, and their
sessions sat idle until a person messaged them at 02:47Z the next day). This
layer is the one that starts a turn. It is ported from v1 `subfleet/notify.py`,
with its bookkeeping on the store's `notices` rows.

The mechanism, unchanged from v1: every interactive Claude Code session
registers `~/.claude/sessions/<pid>.json` (`sessionId`, `messagingSocketPath`,
`name`, `pid`, `startedAt`) and publishes its inbox auth key beside it as
`<pid>.<hash>.key` holding `peerToken`. The inbox speaks newline-delimited JSON
on a unix socket: `{"type":"auth","token":...}` then
`{"type":"user","message":{"role":"user","content":...}}`. A body wrapped as
exactly one `<cross-session-message>` envelope is parsed by the recipient, and
`from-mode` declares the sender's permission class.

Rules kept from v1:

* Resolve the recipient by SESSION ID at delivery time, never by a pid or
  socket captured at dispatch: an account switch restarts the session under a
  new pid and a new socket path, and only the session id is stable.
* Never address a lane session. A headless `claude -p` lane's deliverable is
  its last message, so a pushed notice would become that message.
* The harness gives an address-less sender no acknowledgement, so a delivered
  push records the notice as `offered` with transport `socket` and never as
  `acknowledged` (C-15.3). An unacknowledged notice is surfaced again by the
  session hooks, which is exactly what makes this layer safe to be lossy.

What the frame adds, from what the installed Claude Code's inbox does with it
(2.1.286; read from its inbox handler and command queue, not assumed):

* `priority: "later"`. The inbox queues a user frame at the priority it names
  (`now`, `next` or `later`; `next` when none). `now` aborts the running turn.
  `next` is folded into the running turn between tool calls, and one that lands
  as a turn opens restarts that turn (`rapid_followup`). `later` waits until the
  running turn ends, and an idle session starts a turn for it at once. So a
  push never interleaves with a turn, whatever the session is doing when the
  bytes land.
* `session_id`. The inbox drops a frame that names a session other than its
  own, so a socket path or pid that now belongs to another session cannot
  misdeliver a notice.
* The envelope declares the recipient's own permission class (C-23.42). The
  inbox holds, rather than runs, a frame whose declared class is not the
  recipient's, and a bypass session holds one that declares none. It reads the
  declared class while its remote flag `tengu_harbor_kite_mode_emit` (default
  on) is on, and a `crossSessionInbound` setting overrides all of it (`hold`
  holds, `refuse` drops). A held frame a person approves is queued without its
  priority. None of this reaches the sender: an address-less sender gets no
  receipt, so `delivered` here means the inbox took the bytes.

The daemon's pass (C-15.7) is `plan` over the pending job notices and the
registry as read, then `deliver` for each session it picked: reserve the rows
(`pending` to `offered`, transport `socket`), write the frame, and settle. A
connect failure, or a missing token, wrote nothing: the rows go back to
`pending` and may be tried again. A failure after bytes may have reached the
inbox leaves them `offered`, so a notice is pushed at most once. The frame's
last line names its notices by id (`render.push_trailer`), so the
UserPromptSubmit hook of the turn it starts marks exactly those as shown. The
planner is a pure function of what it is handed, so the rules are tested
without a daemon (`tests/unit/test_notify_push.py`,
`tests/unit/test_notice_push_properties.py`).
"""

from __future__ import annotations

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import render
from .sessions import registry

FROM_NAME = "subfleet"
MODE_CLASSES = ("bypass", "prompting")
TRANSPORT = "socket"
#: C-15.7: the inbox runs a `later` frame when the recipient's current turn ends,
#: and at once when it is idle; it never interrupts a turn or folds into one.
PRIORITY = "later"

#: How far back into a transcript to look for the recipient's permission mode.
_TRANSCRIPT_TAIL = 1024 * 1024
_TRANSCRIPT_MAX = 64 * 1024 * 1024
_MODE_RE = re.compile(rb'"permissionMode"\s*:\s*"([A-Za-z]+)"')
_ENVELOPE_CLOSE = "</cross-session-message>"

#: v1's lane-session markers. `notify.push_to_session` refuses a lane session by
#: asking `lanes.is_lane_session`, which reads the v1 run ledger; v2 has no such
#: reader here, so the same refusal is made from what the registry itself shows:
#: a headless lane has no inbox socket, and the daemon knows its own lane
#: sessions. `refuse_session` lets the caller supply that set.
DEFAULT_TIMEOUT_S = 5.0


class PushError(OSError):
    """A push that did not land, and whether any of its bytes may have.

    `written` is False only when nothing reached the socket: the connection was
    never made. Once `sendall` has started, a failure may have left a complete
    frame in the inbox (it parses a final line without its newline), so the
    push counts as made and is never repeated (C-15.7: at most once).
    """

    def __init__(self, message: str, *, written: bool):
        super().__init__(message)
        self.written = written


def claude_dir() -> Path:
    """`~/.claude`, overridable exactly as v1 overrides it (`paths.claude_dir`)."""
    override = os.environ.get("SUBFLEET_CLAUDE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def sessions_dir() -> Path:
    return claude_dir() / "sessions"


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # someone else's process, but a process
    except OSError:
        return False
    return True


def _is_socket(path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_socket()
    except OSError:
        return False


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_session(session_id: str) -> dict[str, Any] | None:
    """The registry row for `session_id` — the LIVE one when several exist.

    Registry files are keyed by pid, so a restarted session leaves an old row
    behind for a while (v1 `notify.find_session`). Rank live pid, then a present
    socket, then the newest start; this module never kills or deregisters the
    loser, because killing a process it did not launch is a far larger blast
    radius than a missed notice (`docs/reports/D-surface.md` section 4).
    """
    if not session_id:
        return None
    best: tuple[tuple, dict[str, Any]] | None = None
    try:
        entries = list(sessions_dir().glob("*.json"))
    except OSError:
        return None
    for entry in entries:
        data = _load_json(entry)
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        pid = data.get("pid") if isinstance(data.get("pid"), int) else None
        sock = data.get("messagingSocketPath")
        started = data.get("startedAt")
        candidate = {
            "session_id": session_id,
            "pid": pid,
            "socket": sock if isinstance(sock, str) else None,
            "name": data.get("name") if isinstance(data.get("name"), str) else None,
            "cwd": data.get("cwd") if isinstance(data.get("cwd"), str) else None,
            "started_at": started if isinstance(started, (int, float)) else None,
            "alive": _pid_alive(pid),
            "socket_present": _is_socket(sock if isinstance(sock, str) else None),
            "registry_path": str(entry),
        }
        key = (candidate["alive"], candidate["socket_present"],
               candidate["started_at"] or 0)
        if best is None or key > best[0]:
            best = (key, candidate)
    return best[1] if best else None


def peer_token(pid: int | None) -> str | None:
    """The inbox auth key the session published for peers (newest if several)."""
    if not isinstance(pid, int):
        return None
    try:
        files = sorted(sessions_dir().glob(f"{pid}.*.key"),
                       key=lambda item: item.stat().st_mtime)
    except OSError:
        return None
    for path in reversed(files):
        data = _load_json(path)
        token = data.get("peerToken") if isinstance(data, dict) else None
        if isinstance(token, str) and token:
            return token
    return None


# --- permission-mode attestation ---------------------------------------------

def transcript_path(session_id: str) -> Path | None:
    projects = claude_dir() / "projects"
    candidates: list[Path] = []
    direct = projects / f"{session_id}.jsonl"
    if direct.is_file():
        candidates.append(direct)
    try:
        candidates.extend(item for item in projects.glob(f"*/{session_id}.jsonl")
                          if item.is_file())
    except OSError:
        pass
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda item: item.stat().st_mtime)
    except OSError:
        return candidates[-1]


def _last_permission_mode(path: Path) -> str | None:
    """Last `permissionMode` stamped on a user turn, scanning from the end."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            scanned, end, carry = 0, size, b""
            while end > 0 and scanned < _TRANSCRIPT_MAX:
                start = max(0, end - _TRANSCRIPT_TAIL)
                stream.seek(start)
                chunk = stream.read(end - start) + carry
                matches = list(_MODE_RE.finditer(chunk))
                if matches:
                    return matches[-1].group(1).decode("ascii", "replace")
                carry = chunk[:64]
                scanned += end - start
                end = start
    except OSError:
        return None
    return None


def mode_class_of(permission_mode: str | None) -> str | None:
    """Map a harness permission mode to the inbox's two attestation classes."""
    if not permission_mode:
        return None
    return "bypass" if permission_mode == "bypassPermissions" else "prompting"


def session_mode_class(session_id: str) -> str | None:
    path = transcript_path(session_id)
    return None if path is None else mode_class_of(_last_permission_mode(path))


def resolve_mode_class(session_id: str, requested: str | None = None) -> str | None:
    """Which class to declare: explicit > `SUBFLEET_NOTIFY_MODE` > the recipient's.

    subfleet is not a session, so it has no mode of its own to attest. A
    completion notice carries no instructions beyond "your run finished, here is
    the file", so v1 declares the RECIPIENT's current class — the treatment the
    harness gives a session's own background-task completions — and v2 keeps it.
    """
    if requested in MODE_CLASSES:
        return requested
    if requested == "none":
        return None
    override = (os.environ.get("SUBFLEET_NOTIFY_MODE") or "").strip().lower()
    if override in MODE_CLASSES:
        return override
    if override == "none":
        return None
    return session_mode_class(session_id)


# --- the wire -----------------------------------------------------------------

def _clean_name(name: str) -> str:
    cleaned = re.sub(r'["<>\r\n]+', " ", name or "").strip()
    return cleaned[:64] or FROM_NAME


def envelope(body: str, *, from_name: str = FROM_NAME,
             mode_class: str | None = None) -> str:
    """Exactly one harness-formed envelope around `body`.

    The recipient parses `from-name`/`from-mode` only when the whole message is
    one envelope, so a closing tag inside the body is defanged rather than
    escaped (v1 `notify.envelope`).
    """
    safe_body = body.replace(_ENVELOPE_CLOSE, "</cross-session-message >")
    attrs = f' from-name="{_clean_name(from_name)}"'
    if mode_class in MODE_CLASSES:
        attrs += f' from-mode="{mode_class}"'
    return f"<cross-session-message{attrs}>\n{safe_body.strip()}\n{_ENVELOPE_CLOSE}"


def frame(content: str, *, priority: str | None = PRIORITY, session_id: str | None = None,
          message_uuid: str | None = None) -> dict[str, Any]:
    """The inbox's `user` frame (C-15.7): the message, the priority it is queued
    at, the session it is for, and the uuid it is recorded under."""
    item: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": content}}
    if priority:
        item["priority"] = priority
    if session_id:
        item["session_id"] = session_id
    if message_uuid:
        item["uuid"] = message_uuid
    return item


def send_to_socket(socket_path: str, token: str | None, content: str, *,
                   timeout: float = DEFAULT_TIMEOUT_S, priority: str | None = PRIORITY,
                   session_id: str | None = None, message_uuid: str | None = None) -> None:
    """Deliver one user message into a session inbox.

    Raises `PushError` (an `OSError`) when it did not land, with `written` False
    only when no byte can have reached the inbox.
    """
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(json.dumps(frame(content, priority=priority, session_id=session_id,
                                  message_uuid=message_uuid)))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(socket_path)
        except (OSError, ValueError) as exc:
            raise PushError(f"connect: {exc.__class__.__name__}: {exc}", written=False) from exc
        try:
            client.sendall(payload)
        except OSError as exc:
            raise PushError(f"send: {exc.__class__.__name__}: {exc}", written=True) from exc
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        # The inbox only answers senders that gave a reply address; drain
        # briefly so a receipt never lands as ECONNRESET on the server side.
        client.settimeout(min(timeout, 1.0))
        try:
            while client.recv(65536):
                pass
        except (TimeoutError, OSError):
            pass
    finally:
        client.close()


def push_to_session(session_id: str, body: str, *, from_name: str = FROM_NAME,
                    mode_class: str | None = None,
                    timeout: float = DEFAULT_TIMEOUT_S,
                    lane_sessions: Iterable[str] = (),
                    force: bool = False) -> dict[str, Any]:
    """Best-effort push of one message, outside the daemon's pass; never raises.

    `delivered` means the inbox accepted the bytes. It is NOT an
    acknowledgement: the harness gives none to an address-less sender, which is
    why C-15.3 stops this layer at `offered`.
    """
    result: dict[str, Any] = {"delivered": False, "session_id": session_id,
                              "transport": TRANSPORT}
    entry = find_session(session_id)
    if entry is None:
        result["reason"] = "session-not-registered"
        return result
    result.update({"pid": entry["pid"], "socket": entry["socket"],
                   "name": entry["name"]})
    if not entry["alive"]:
        result["reason"] = "session-not-running"
        return result
    if not force and session_id in set(lane_sessions):
        # A lane's deliverable is its last message; a pushed notice would become
        # that message. Relay to the lane's orchestrator instead (v1 rule).
        result["reason"] = ("lane-session: headless run, a notice would overwrite "
                            "its deliverable (relay to its orchestrator)")
        return result
    if not entry["socket_present"]:
        result["reason"] = "no-inbox-socket"
        return result
    token = peer_token(entry["pid"])
    if token is None:
        result["reason"] = "no-peer-token"
        return result
    declared = resolve_mode_class(session_id, mode_class)
    result["mode_class"] = declared
    try:
        send_to_socket(entry["socket"], token,
                       envelope(body, from_name=from_name, mode_class=declared),
                       timeout=timeout, session_id=session_id)
    except (OSError, ValueError) as exc:
        result["reason"] = f"send-failed: {exc.__class__.__name__}: {exc}"
        return result
    result["delivered"] = True
    return result


# --- the daemon's pass (C-15.7) -----------------------------------------------

#: The statuses Claude Code records for a live session (`~/.claude/sessions`):
#: `idle` between turns, `busy` in one, `waiting` on a person's answer. A push
#: waits for `idle`; a row with no status (an older Claude Code) is pushed to,
#: because a `later` frame is safe whatever the session is doing.
IDLE_STATUSES = frozenset({"idle"})
#: The registry's kind for a session a person or the desktop app drives.
INTERACTIVE = "interactive"
#: At most this many notices are spelled out in one push; the rest are counted.
BODY_MAX_NOTICES = 10
#: A notice's text longer than this is cut in the push (the store keeps it whole).
BODY_MAX_TEXT = 4000


@dataclass(frozen=True)
class PushSettings:
    """`notices.*` (C-15.7), as `policy.load_policy` validated them."""

    enabled: bool = True
    interval_s: float = 2.0
    delay_s: float = 10.0
    max_age_s: float = 7200.0
    session_gap_s: float = 60.0
    per_minute: int = 10
    after_wait_s: float = 120.0
    retry_s: float = 60.0
    max_tries: int = 3
    timeout_s: float = 2.0

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any] | None) -> "PushSettings":
        from .policy import NOTICE_DEFAULTS
        supplied = (policy or {}).get("notices")
        values = {**NOTICE_DEFAULTS, **(supplied if isinstance(supplied, Mapping) else {})}
        return cls(enabled=bool(values["push"]), interval_s=float(values["push_interval_s"]),
                   delay_s=float(values["push_delay_s"]),
                   max_age_s=float(values["push_max_age_min"]) * 60,
                   session_gap_s=float(values["push_session_gap_s"]),
                   per_minute=int(values["push_per_minute"]),
                   after_wait_s=float(values["push_after_wait_s"]),
                   retry_s=float(values["push_retry_s"]),
                   max_tries=int(values["push_max_tries"]),
                   timeout_s=float(values["push_timeout_s"]))


@dataclass(frozen=True)
class Pending:
    """One `pending` notice that names a job and the session that dispatched it."""

    notice_id: int
    job_id: str
    session_id: str
    text: str
    created_at: float                   # epoch seconds

    @property
    def key(self) -> str:
        return self.session_id.lower()

    @property
    def ident(self) -> tuple[int, float]:
        """The notice as the pass remembers it: its id with its creation time,
        because SQLite reuses the largest rowid once retention has deleted it."""
        return (self.notice_id, self.created_at)


def epoch(stamp: Any) -> float | None:
    """A store time (`2026-10-02T22:10:05Z`) as epoch seconds, or None."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def pending_rows(rows: Iterable[Mapping[str, Any]]) -> list[Pending]:
    """The store's `notices` rows the pass may push: `pending`, naming a job and a
    session, with a creation time it can read. Anything else is not this layer's."""
    found = []
    for row in rows:
        notice_id, job_id, session = row.get("notice_id"), row.get("job_id"), row.get("session_id")
        created = epoch(row.get("created_at"))
        if (not isinstance(notice_id, int) or isinstance(notice_id, bool) or notice_id <= 0
                or not isinstance(job_id, str) or not job_id.strip()
                or not isinstance(session, str) or not session.strip()
                or row.get("state", "pending") != "pending" or created is None):
            continue
        found.append(Pending(notice_id, job_id, session.strip(), str(row.get("text") or ""), created))
    return found


@dataclass(frozen=True)
class Push:
    """One inbox message: every pending notice of one session, to the row that
    speaks for that session now (C-23.30)."""

    session_id: str                     # as the registry row spells it
    row: registry.SessionRow
    notices: tuple[Pending, ...]
    #: The uuid the recipient records the message under, fresh for every push.
    #: Claude Code writes a message to the transcript once per uuid: a second
    #: message under a uuid it has recorded still starts a turn but is never
    #: written, so a resumed session would not have it (2026-10-03, two pushes
    #: whose uuid was derived from the session and a notice id that two stores
    #: shared). A notice id is not unique for good either: SQLite reuses the
    #: largest rowid once retention has deleted it.
    message_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def notice_ids(self) -> tuple[int, ...]:
        return tuple(item.notice_id for item in self.notices)


@dataclass
class History:
    """What the pass remembers between passes, in memory (C-15.7).

    A restart forgets it, which costs at most one more try of a push that wrote
    nothing; a push that may have landed is remembered by its notices' state,
    which is the store's.
    """

    last_push: dict[str, float] = field(default_factory=dict)       # session (lower case) -> when
    recent: list[float] = field(default_factory=list)               # pushes made, when
    #: `Pending.ident` -> (tries that wrote nothing, when the last one was).
    failures: dict[tuple[int, float], tuple[int, float]] = field(default_factory=dict)

    def prune(self, now: float, settings: PushSettings) -> None:
        self.recent = [at for at in self.recent if now - at < 60]
        horizon = max(settings.session_gap_s, 60.0)
        self.last_push = {key: at for key, at in self.last_push.items() if now - at < horizon}
        # A notice past the max age is never pushed, so its tries need no keeping.
        self.failures = {ident: value for ident, value in self.failures.items()
                         if now - ident[1] <= settings.max_age_s}


@dataclass
class Plan:
    """The pushes one pass makes, and why every other session waits or is passed over."""

    pushes: list[Push] = field(default_factory=list)
    held: dict[str, str] = field(default_factory=dict)       # session (lower case) -> reason


def target_row(rows: Iterable[registry.SessionRow], session_key: str) -> registry.SessionRow | None:
    """The row that speaks for a session id now (C-23.30), whichever case either
    side spells the id in."""
    return registry.speaker(row for row in rows if row.session_id.lower() == session_key)


def row_refusal(row: registry.SessionRow | None) -> str | None:
    """Why a registry row may not receive a push now, or None when it may.

    Every reason here is a fact about the recipient at delivery time: it has to
    be a live, interactive session with an inbox, between turns.
    """
    if row is None:
        return "not registered"
    if not row.alive:
        return "not running"
    if (row.entrypoint or "").startswith(registry.HEADLESS_ENTRYPOINT_PREFIX):
        return "headless"
    if row.kind is not None and row.kind != INTERACTIVE:
        return "not interactive"
    if not row.socket or not row.socket_present:
        return "no inbox"
    if row.status is not None and row.status not in IDLE_STATUSES:
        return row.status
    return None


def reachable(pending: Iterable[Pending], rows: Iterable[registry.SessionRow]) -> bool:
    """Whether any session with a pending notice has a row a push could reach
    now, before the daemon reads its own lane and conversation records."""
    rows = list(rows)
    return any(row_refusal(target_row(rows, key)) is None for key in {item.key for item in pending})


def registry_fingerprint() -> tuple:
    """Each registry row file's name, size and modification time: a status, a
    restart or a new session changes it. Empty when the directory cannot be
    listed, which is also a change from a listing that worked."""
    try:
        with os.scandir(registry.sessions_dir()) as entries:
            found = []
            for entry in entries:
                if entry.name.endswith(".json"):
                    try:
                        info = entry.stat()
                    except OSError:
                        continue
                    found.append((entry.name, info.st_size, info.st_mtime_ns))
    except OSError:
        return ()
    return tuple(sorted(found))


def plan(pending: Iterable[Pending], rows: Iterable[registry.SessionRow] | None, *,
         now: float, settings: PushSettings, lane_ids: Iterable[str] = (),
         conversation_ids: Iterable[str] = (), watched: Iterable[str] = (),
         waited: Mapping[str, float] | None = None,
         history: History | None = None) -> Plan:
    """Which sessions get a push this pass (C-15.7, C-23.50). Pure: reads nothing.

    `rows` is the registry as read now, or None when it could not be read (then
    nothing is pushed). `lane_ids` and `conversation_ids` are the daemon's own
    record of the sessions it ran as lanes and conversations. `watched` is the
    jobs a waiter is registered for now; `waited` maps a job to when a `wait`
    last reported it ended. `history` is the pass's memory.
    """
    history = history or History()
    waited = waited or {}
    out = Plan()
    if not settings.enabled:
        return out
    lanes = registry.folded(lane_ids)
    conversations = registry.folded(conversation_ids)
    watching = frozenset(watched)
    sessions: dict[str, list[Pending]] = {}
    for item in pending:
        sessions.setdefault(item.key, []).append(item)
    recent = [at for at in history.recent if now - at < 60]
    # The session whose oldest notice has waited longest goes first, so the
    # per-minute cap delays the newest finishes, never the oldest.
    order = sorted(sessions, key=lambda key: (min(item.created_at for item in sessions[key]), key))
    for key in order:
        items = sorted(sessions[key], key=lambda item: item.notice_id)
        fresh = [item for item in items if now - item.created_at <= settings.max_age_s]
        if not fresh:
            out.held[key] = "too old"
            continue
        usable = []
        waiting_retry = False
        for item in fresh:
            tries, last = history.failures.get(item.ident, (0, 0.0))
            if tries >= settings.max_tries:
                continue
            if tries and now - last < settings.retry_s:
                waiting_retry = True
                continue
            usable.append(item)
        if not usable:
            out.held[key] = "retry wait" if waiting_retry else "gave up"
            continue
        # C-23.50, per notice: a job with a live waiter, or one a `wait` has just
        # reported, is that waiter's to tell. The session's other notices go
        # on without it, so one long wait never holds them until they age out.
        live = [item for item in usable if item.job_id in watching]
        reported = [item for item in usable if item.job_id not in watching and item.job_id in waited
                    and now - waited[item.job_id] < settings.after_wait_s]
        usable = [item for item in usable if item not in live and item not in reported]
        if not usable:
            out.held[key] = "waiter live" if live else "waiter reported"
            continue
        if max(now - item.created_at for item in usable) < settings.delay_s:
            out.held[key] = "settling"
            continue
        if key in lanes:
            out.held[key] = "lane session"
            continue
        if key in conversations:
            out.held[key] = "conversation session"
            continue
        if rows is None:
            out.held[key] = "registry unreadable"
            continue
        row = target_row(rows, key)
        refusal = row_refusal(row)
        if refusal is not None:
            out.held[key] = refusal
            continue
        last_push = history.last_push.get(key)
        if last_push is not None and now - last_push < settings.session_gap_s:
            out.held[key] = "session gap"
            continue
        if len(recent) >= settings.per_minute:
            out.held[key] = "rate"
            continue
        recent.append(now)
        out.pushes.append(Push(session_id=row.session_id, row=row, notices=tuple(usable)))
    return out


def render_body(notices: Sequence[Pending]) -> str:
    """The push's text: the notices' own text (C-15.1: metadata only), in order."""
    count = len(notices)
    blocks = [f"subfleet: {count} detached run{'s' if count != 1 else ''} this session "
              f"dispatched {'have' if count != 1 else 'has'} finished:"]
    for item in notices[:BODY_MAX_NOTICES]:
        text = item.text.strip() or f"{item.job_id} finished"
        if len(text) > BODY_MAX_TEXT:
            text = text[:BODY_MAX_TEXT].rstrip() + " ..."
        blocks.append(text)
    if count > BODY_MAX_NOTICES:
        blocks.append(f"... and {count - BODY_MAX_NOTICES} more: subfleet runs --mine")
    blocks.append("Read one with `subfleet runs show <id>`, which also marks its notice read; "
                  "list them with `subfleet runs --mine`.\n"
                  + render.push_trailer(item.notice_id for item in notices))
    return "\n\n".join(blocks)


def recheck(push: Push) -> str | None:
    """Read the target's registry row again just before the push, and say why
    not to push now, or None. The pass read the registry up to a couple of
    seconds ago; a session that started a turn since is left for later."""
    fresh = registry.read_row(Path(push.row.registry_path))
    if fresh is None:
        return "registry row gone"
    if fresh.session_id.lower() != push.session_id.lower() or fresh.pid != push.row.pid:
        return "registry row changed"
    if not fresh.alive:
        return "not running"
    if fresh.socket != push.row.socket:
        return "inbox moved"
    if fresh.status is not None and fresh.status not in IDLE_STATUSES:
        return fresh.status
    return None


@dataclass
class Outcome:
    """What one push did. `result` is `delivered`, `failed` (nothing written;
    the notices are `pending` again), `uncertain` (bytes may have landed; the
    notices stay `offered`), `raced` (another layer reached every notice first)
    or `held` (the recheck found the session busy or gone; nothing reserved)."""

    session_id: str
    pid: int | None
    result: str
    notice_ids: tuple[int, ...] = ()
    reason: str = ""
    mode_class: str | None = None
    message_uuid: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "pid": self.pid, "result": self.result,
                "notice_ids": list(self.notice_ids), "reason": self.reason,
                "mode_class": self.mode_class, "uuid": self.message_uuid}


def deliver(push: Push, *, reserve: Callable[[Push, dict[str, Any]], list[int]],
            release: Callable[[Push, list[int], dict[str, Any]], None],
            record: Callable[[str, dict[str, Any]], None],
            check: Callable[[Push], str | None] = recheck,
            send: Callable[..., None] = send_to_socket,
            token_of: Callable[[int | None], str | None] = peer_token,
            mode_of: Callable[[str], str | None] = resolve_mode_class,
            timeout: float = 2.0) -> Outcome:
    """Reserve, write and settle one push (C-15.7). Never raises.

    `reserve(push, data)` moves the push's notices that are still `pending` to
    `offered` with transport `socket`, in one transaction, and returns their
    ids; only those are written. `release(push, ids, data)` puts them back to
    `pending` when nothing was written. `record(kind, data)` writes an event.
    """
    base = {"session_id": push.session_id, "pid": push.row.pid, "socket": push.row.socket,
            "uuid": push.message_uuid}
    outcome = Outcome(push.session_id, push.row.pid, "held", message_uuid=push.message_uuid)
    try:
        reason = check(push)
        if reason is not None:
            outcome.reason = reason
            return outcome
        token = token_of(push.row.pid)
        if token is None:
            outcome.result, outcome.reason = "failed", "no peer token"
            return outcome
        mode = mode_of(push.session_id)
        outcome.mode_class = mode
        data = {**base, "mode_class": mode}
        reserved = list(reserve(push, data))
    except Exception as exc:                            # noqa: BLE001 - never raises
        outcome.result, outcome.reason = "failed", f"{exc.__class__.__name__}: {exc}"
        return outcome
    outcome.notice_ids = tuple(reserved)
    if not reserved:
        outcome.result, outcome.reason = "raced", "another layer reached every notice first"
        return outcome
    chosen = [item for item in push.notices if item.notice_id in set(reserved)]
    data = {**base, "mode_class": mode, "notice_ids": reserved,
            "job_ids": [item.job_id for item in chosen]}
    try:
        send(push.row.socket, token, envelope(render_body(chosen), mode_class=mode),
             timeout=timeout, priority=PRIORITY, session_id=push.session_id,
             message_uuid=push.message_uuid)
    except Exception as exc:                            # noqa: BLE001 - never raises
        written = getattr(exc, "written", True) if isinstance(exc, OSError) else True
        outcome.reason = f"{exc.__class__.__name__}: {exc}"
        if written:
            outcome.result = "uncertain"
            _quietly(record, "notice.push_uncertain", {**data, "reason": outcome.reason})
        else:
            outcome.result = "failed"
            _quietly(release, push, reserved, {**data, "reason": outcome.reason})
        return outcome
    outcome.result = "delivered"
    _quietly(record, "notice.pushed", data)
    return outcome


def _quietly(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except Exception:                                   # noqa: BLE001 - bookkeeping only
        pass


def settle(history: History, push: Push, outcome: Outcome, now: float) -> None:
    """Fold one push's outcome into the pass's memory (C-15.7)."""
    if outcome.result in ("delivered", "uncertain"):
        history.last_push[push.session_id.lower()] = now
        history.recent.append(now)
    elif outcome.result == "failed":
        for item in push.notices:
            if not outcome.notice_ids or item.notice_id in outcome.notice_ids:
                tries, _ = history.failures.get(item.ident, (0, 0.0))
                history.failures[item.ident] = (tries + 1, now)
