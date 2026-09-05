"""Layer 4 of notice delivery: the best-effort push through v1's session inbox.

C-15.2 ranks the delivery layers most to least reliable: `subfleet wait` in a
background Bash call, the PostToolUse `asyncRewake` hook (`subfleet/hooks.py`),
the SessionStart and UserPromptSubmit hooks, and last "a best-effort socket
push through the v1 mechanism, kept until tickle, muster, and `ping` have
tested replacements". This module is that last layer, ported from v1
`subfleet/notify.py` with its behaviour preserved and its bookkeeping moved
onto the store's `notices` rows.

The mechanism, unchanged from v1: every interactive Claude Code session
registers `~/.claude/sessions/<pid>.json` (`sessionId`, `messagingSocketPath`,
`name`, `pid`, `startedAt`) and publishes its inbox auth key beside it as
`<pid>.<hash>.key` holding `peerToken`. The inbox speaks newline-delimited JSON
on a unix socket: `{"type":"auth","token":...}` then
`{"type":"user","message":{"role":"user","content":...}}`. A body wrapped as
exactly one `<cross-session-message>` envelope is parsed by the recipient, and
`from-mode` declares the sender's permission class.

Two rules this module keeps from v1 and one it adds:

* Resolve the recipient by SESSION ID at delivery time, never by a pid or
  socket captured at dispatch: an account switch restarts the session under a
  new pid and a new socket path, and only the session id is stable.
* Never address a lane session. A headless `claude -p` lane's deliverable is
  its last message, so a pushed notice would become that message.
* The harness gives an address-less sender no acknowledgement, so a delivered
  push records the notice as `offered` with transport `socket` and never as
  `acknowledged` (C-15.3). An unacknowledged notice is surfaced again by the
  session hooks, which is exactly what makes this layer safe to be lossy.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

FROM_NAME = "subfleet"
MODE_CLASSES = ("bypass", "prompting")
TRANSPORT = "socket"

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


def send_to_socket(socket_path: str, token: str | None, content: str, *,
                   timeout: float = DEFAULT_TIMEOUT_S) -> None:
    """Deliver one user message into a session inbox. Raises OSError on failure."""
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(json.dumps({"type": "user",
                             "message": {"role": "user", "content": content}}))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(socket_path)
        client.sendall(payload)
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
    """Best-effort push; never raises.

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
                       timeout=timeout)
    except (OSError, ValueError) as exc:
        result["reason"] = f"send-failed: {exc.__class__.__name__}: {exc}"
        return result
    result["delivered"] = True
    return result


# --- the adapter onto notice rows ---------------------------------------------

def offer(rows: Sequence[dict[str, Any]], *,
          mark: Callable[[int, str], None] | None = None,
          push: Callable[..., dict[str, Any]] = push_to_session,
          lane_sessions: Iterable[str] = (),
          timeout: float = DEFAULT_TIMEOUT_S) -> list[dict[str, Any]]:
    """Push each notice row and record what happened (C-15.2 layer 4, C-15.3).

    `rows` are `notices` rows: `notice_id`, `session_id`, `text`, `state`. Only
    rows in `pending` or `offered` are attempted, because `acknowledged` and
    `surfaced` have already reached their session. `mark(notice_id, transport)`
    is called ONLY for a row whose bytes the inbox accepted, and its contract is
    to move that row to `offered` — never to `acknowledged`, which belongs to
    `notice.ack` or to the session running `runs show <job>` (C-15.3).

    Returns one result dict per row, in order, so a caller can log why a push
    did not land. It never raises: this is the least reliable layer and a
    failure here must not disturb the layers above it.
    """
    lane_sessions = set(lane_sessions)
    results: list[dict[str, Any]] = []
    for row in rows:
        session = row.get("session_id")
        notice_id = row.get("notice_id")
        state = row.get("state")
        if not session or state not in ("pending", "offered"):
            results.append({"delivered": False, "notice_id": notice_id,
                            "session_id": session, "transport": TRANSPORT,
                            "reason": f"state={state!r}: nothing to offer"})
            continue
        outcome = push(session, str(row.get("text") or ""),
                       lane_sessions=lane_sessions, timeout=timeout)
        outcome["notice_id"] = notice_id
        if outcome.get("delivered") and mark is not None and notice_id is not None:
            try:
                mark(int(notice_id), TRANSPORT)
            except Exception as exc:                    # noqa: BLE001 - best effort
                outcome["mark_failed"] = f"{exc.__class__.__name__}: {exc}"
        results.append(outcome)
    return results
