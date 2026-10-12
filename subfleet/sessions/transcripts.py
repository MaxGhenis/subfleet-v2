"""How a Claude Code transcript ends, and whether it is a session at all.

Ported from v1 `subfleet/tickle.py:turn_state` and `subfleet/lanes.py`, whose
classification is the thing C-23.33 and C-23.34 are written about. The rules it
keeps, verbatim in effect:

* **Interrupted** is one of three shapes at the end of the main chain: an
  assistant message carrying a `tool_use` block whose result never arrived; a
  user message made of `tool_result` blocks the model never continued from; or a
  user message with real text that has no assistant reply. An assistant message
  ending in plain text is a *completed* turn — a session that finished its work
  is never nudged.
* **The app's synthetic resume stub.** The desktop app writes a hidden user line
  "Continue from where you left off." and a fake assistant reply "No response
  requested." into every restarted session (observed four times in one session
  on 2026-08-23, ~0.7 s after the new process started). It would make an
  interrupted transcript look completed, so the classifier skips it and judges
  the turn beneath it (C-23.34). Each stub carries its own uuid, which is why
  every restart earns exactly one nudge (C-23.33's "once per interruption
  point"): `dedupe_key` prefers the stub's uuid over the real turn's.
* **Provider-limit banners** are written as assistant entries and are not model
  turns either. Trailing assistant text above one is treated as interrupted:
  a genuinely finished session answers a nudge with one cheap "nothing pending"
  turn, while a missed resume strands real work.
* **A headless lane run is not a session** (C-23.31). Claude Code stamps its
  entries with the `entrypoint` of the process that wrote them: `sdk-cli` for a
  `claude -p` run whose environment names no entrypoint, `claude-desktop` for
  the desktop app, `cli` for a terminal. A transcript is a lane run when some
  entry names a headless entrypoint (`HEADLESS_ENTRYPOINTS`) and none names
  another; one entry written by any other process makes it a session. Only
  prompts that name no `entrypoint` (older Claude Code) are read by v1's prompt
  rule, under which a transcript of such prompts is a lane run with one or two
  text prompts, all `promptSource: sdk`. That rule cannot read a current
  transcript, because the desktop app sends its prompts as `sdk` too.

`fingerprint` is the C-23.34 re-check: the identity of the real last turn. The
resume stub and the app's bookkeeping rows change the file without changing it;
a session that is actually working changes it within seconds.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

#: The desktop app's synthetic resume pair (v1 `tickle.RESUME_STUB_*`).
RESUME_STUB_USER = "Continue from where you left off."
RESUME_STUB_ASSISTANT = "No response requested."

#: The first line of every nudge subfleet sends, so a nudge is never mistaken
#: for an unanswered human prompt on the next pass (v1 `tickle.MARKER`).
MARKER = "subfleet: this session restarted"
MUSTER_MARKER = "subfleet muster: roll call"

#: C-23.31: the `entrypoint` values of a headless SDK process: `sdk-cli` for
#: `claude -p`, `sdk-ts` and `sdk-py` for the Agent SDKs. Claude Code 2.1.286
#: keeps an entrypoint its environment already names (the desktop app sets
#: `claude-desktop`, and a `claude -p` started from a desktop session's shell
#: inherits it), and otherwise sets `sdk-cli` for a non-interactive run and `cli`
#: for an interactive one. Its own transcript reader checks a transcript's
#: `entrypoint` against this same set. A person can drive an SDK host (a Subfleet
#: conversation is `sdk-cli`), which is why a conversation is identified by the
#: daemon's records, never by this shape. Every other value (`claude-desktop`,
#: `cli`, and the rest of Claude Code's list) is a session. Measured on
#: 2026-10-03 across one machine's 18,575 transcripts: `sdk-cli` (all 2,042
#: Claude lane runs in the ledger), `claude-desktop` and `cli`, nothing else.
HEADLESS_ENTRYPOINTS = frozenset({"sdk-cli", "sdk-ts", "sdk-py"})

#: v1 `lanes.HEADLESS_PROMPT_SOURCE`; a `claude -p` prompt arrives via the SDK.
#: Read only in a transcript that names no `entrypoint` (C-23.31).
HEADLESS_PROMPT_SOURCE = "sdk"
HEADLESS_PROMPT_LIMIT = 2

_TAIL_BYTES = 512 * 1024
_SCAN_MAX = 64 * 1024 * 1024
_MAIN_ENTRY_LIMIT = 12
_MODE_RE = re.compile(rb'"permissionMode"\s*:\s*"([A-Za-z]+)"')

STATES = ("interrupted", "completed", "tickled", "stopped", "empty")


def claude_dir() -> Path:
    """`~/.claude`, overridden by `SUBFLEET_CLAUDE_DIR` exactly as v1 overrides it.

    Every test points this at its own tree; nothing here ever writes.
    """
    override = os.environ.get("SUBFLEET_CLAUDE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def projects_dir() -> Path:
    return claude_dir() / "projects"


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def transcript_path(session_id: str) -> Path | None:
    """`~/.claude/projects/<encoded cwd>/<session id>.jsonl`, newest if several.

    A session that moved worktrees leaves a transcript under each project
    directory; the most recently written one is the live conversation.
    """
    if not session_id:
        return None
    projects = projects_dir()
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


def lines_reversed(path: Path, *, chunk: int = _TAIL_BYTES,
                   max_bytes: int = _SCAN_MAX) -> Iterator[str]:
    """Lines from the end of the file backwards, in chunks.

    A long run of subagent entries at the tail must not hide the last main turn,
    and a 400 MB transcript must not be read whole to classify its last line.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            end, carry, scanned = size, b"", 0
            while end > 0 and scanned < max_bytes:
                start = max(0, end - chunk)
                stream.seek(start)
                block = stream.read(end - start) + carry
                scanned += end - start
                parts = block.split(b"\n")
                carry = parts[0] if start > 0 else b""
                for raw in reversed(parts[1:] if start > 0 else parts):
                    if raw.strip():
                        yield raw.decode("utf-8", "replace")
                end = start
            if carry.strip() and scanned < max_bytes:
                yield carry.decode("utf-8", "replace")
    except OSError:
        return


def blocks(message: Any) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def text_of(items: list[dict[str, Any]]) -> str:
    return "\n".join(str(item.get("text") or "") for item in items
                     if item.get("type") == "text").strip()


def is_main(entry: Any) -> bool:
    """A main-chain user or assistant entry: not a sidechain, not bookkeeping."""
    return bool(isinstance(entry, dict) and entry.get("type") in {"user", "assistant"}
                and not entry.get("isSidechain") and not entry.get("isMeta"))


def _limit_banner(entry: dict[str, Any]) -> bool:
    """A provider-limit banner ("You've reached your Fable 5 limit…").

    Written as an assistant entry by the harness, but not a model turn.
    """
    return bool(entry.get("error") or entry.get("isApiErrorMessage") is True
                or (entry.get("quotaLimits") or {}).get("status") == "rejected")


@dataclass(frozen=True)
class TurnState:
    """How a transcript ends. `age_s` is measured against the caller's clock."""

    state: str = "empty"
    detail: str = "no transcript"
    path: str | None = None
    last_uuid: str | None = None
    stub_uuid: str | None = None
    timestamp: str | None = None
    age_s: int | None = None
    main_entries: int = 0
    assistant_turns: int = 0
    restart_stubs: int = 0
    limit_banner: bool = False

    @property
    def dedupe_key(self) -> str | None:
        """One nudge per interruption point — and per restart of it (C-23.33).

        The app's resume stub carries a fresh uuid on every restart, so a second
        restart of the same stuck turn earns a second nudge; without a stub the
        stuck turn's own uuid holds the dedupe.
        """
        return self.stub_uuid or self.last_uuid

    @property
    def fingerprint(self) -> tuple[Any, Any] | None:
        """Identity of the real last turn, for the C-23.34 re-check."""
        return None if self.state == "empty" else (self.last_uuid, self.timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "path": self.path,
                "last_uuid": self.last_uuid, "stub_uuid": self.stub_uuid,
                "timestamp": self.timestamp, "age_s": self.age_s,
                "main_entries": self.main_entries,
                "assistant_turns": self.assistant_turns,
                "restart_stubs": self.restart_stubs,
                "limit_banner": self.limit_banner,
                "dedupe_key": self.dedupe_key}


def turn_state(transcript: str | Path | None, *,
               now: datetime | None = None) -> TurnState:
    """Classify how `transcript` ends (C-23.33, C-23.34).

    Reads backwards and stops at the first assistant turn, so the cost is the
    tail of the file rather than its length.
    """
    if not transcript:
        return TurnState()
    path = Path(transcript).expanduser()
    if not path.is_file():
        return TurnState(path=str(path))
    now = now or datetime.now(timezone.utc)

    last: dict[str, Any] | None = None
    stubs = 0
    stub_uuid: str | None = None
    banner = False
    main_entries = 0
    assistant_turns = 0
    for line in lines_reversed(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not is_main(entry):
            continue
        main_entries += 1
        if entry["type"] == "assistant":
            if text_of(blocks(entry.get("message"))) == RESUME_STUB_ASSISTANT:
                stubs += 1
                if stub_uuid is None and isinstance(entry.get("uuid"), str):
                    stub_uuid = entry["uuid"]
                continue                        # judge the turn underneath it
            if _limit_banner(entry):
                banner = True
                continue                        # a banner is not a model turn
            assistant_turns += 1
        if last is None:
            last = entry
        if main_entries >= _MAIN_ENTRY_LIMIT or (last is not None and assistant_turns > 0):
            break

    common = {"path": str(path), "main_entries": main_entries,
              "assistant_turns": assistant_turns, "restart_stubs": stubs,
              "stub_uuid": stub_uuid, "limit_banner": banner}
    if last is None:
        return TurnState(state="empty", detail="no user/assistant turns", **common)

    kinds = {block.get("type") for block in blocks(last.get("message"))}
    stamp = _instant(last.get("timestamp"))
    common.update({"last_uuid": last.get("uuid"),
                   "timestamp": _iso(stamp) if stamp else None,
                   "age_s": round((now - stamp).total_seconds()) if stamp else None})
    marks = []
    if banner:
        marks.append("hit a usage limit")
    if stubs:
        marks.append("behind the app's resume stub")
    suffix = f" ({', '.join(marks)})" if marks else ""

    if last["type"] == "assistant":
        if "tool_use" in kinds:
            names = [str(block.get("name")) for block in blocks(last.get("message"))
                     if block.get("type") == "tool_use"]
            return TurnState(state="interrupted",
                             detail=f"a tool call never got its result "
                                    f"({', '.join(names[:3])}){suffix}", **common)
        if banner:
            # Trailing assistant text with a limit banner above it: the session
            # was still making requests when the limit hit. Err toward resuming
            # — a finished session answers with one cheap "nothing pending".
            return TurnState(state="interrupted",
                             detail=f"the model was cut off by a usage limit "
                                    f"after its last text{suffix}", **common)
        return TurnState(state="completed",
                         detail=f"last turn ended in assistant text{suffix}", **common)

    if "tool_result" in kinds:
        return TurnState(state="interrupted",
                         detail=f"a tool result arrived but the model never "
                                f"continued{suffix}", **common)
    text = text_of(blocks(last.get("message")))
    if any(mark in text[:400] for mark in (MARKER, MUSTER_MARKER)):
        return TurnState(state="tickled",
                         detail="the last message is already a subfleet nudge", **common)
    if text.startswith("[Request interrupted by user"):
        return TurnState(state="stopped",
                         detail="the user interrupted the last turn (Esc)", **common)
    preview = text[:80].replace("\n", " ")
    detail = ("an unanswered prompt: “" + preview + "”" if preview
              else "an unanswered message")
    return TurnState(state="interrupted", detail=detail + suffix, **common)


def fingerprint(transcript: str | Path | None) -> tuple[Any, Any] | None:
    """The C-23.34 re-check value: read once before the wait, once after."""
    return turn_state(transcript).fingerprint


def headless_transcript(transcript: str | Path | None, *,
                        max_lines: int = 5000) -> bool:
    """True for a `claude -p` (SDK) run — a lane run, a probe, or a one-shot.

    C-23.31: a headless lane run is not a session. Claude Code stamps its
    entries with their writer's `entrypoint`, so the process says what it is: a
    transcript is headless when some entry names an entrypoint in
    `HEADLESS_ENTRYPOINTS` and none names another. One entry from the desktop
    app or a terminal makes it a session, and a headless process that later
    continues one (a revive, a Subfleet turn) does not make it a lane run.

    A transcript in which no entry names an entrypoint (older Claude Code) is
    judged by the rule v1 measured on 2026-09-04: a lane's transcript holds one
    text prompt (its brief), arrived through the SDK, or two if the lane was
    itself notified; a typed prompt, an absent `promptSource`, or a third sdk
    prompt (inbox notices arrive as `sdk`) is a session. Tool results are user
    entries too but are not prompts. That rule now misreads the desktop app,
    which sends its prompts as `sdk` too (2026-10-03), so it reads only prompts
    that name no entrypoint. Only the first `max_lines` lines are read.
    """
    if not transcript:
        return False
    path = Path(transcript).expanduser()
    headless_writer = False         # an entry a headless process wrote
    legacy_prompts = 0              # sdk text prompts that name no entrypoint
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= max_lines:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue                    # a valid line that is not an object is no entry
                entrypoint = entry.get("entrypoint")
                named = isinstance(entrypoint, str) and bool(entrypoint)
                if named:
                    if entrypoint not in HEADLESS_ENTRYPOINTS:
                        return False            # the desktop app, a terminal: a session
                    headless_writer = True
                if entry.get("type") != "user" or entry.get("isMeta") or named:
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list) and content and all(
                        isinstance(item, dict) and item.get("type") == "tool_result"
                        for item in content):
                    continue                    # a tool result is not a prompt
                if entry.get("promptSource") != HEADLESS_PROMPT_SOURCE:
                    return False                # typed, or the desktop app before entrypoints
                legacy_prompts += 1
                if legacy_prompts > HEADLESS_PROMPT_LIMIT:
                    return False                # an inbox-driven interactive session
    except OSError:
        return False
    return headless_writer or legacy_prompts > 0


def last_permission_mode(transcript: str | Path | None) -> str | None:
    """The last `permissionMode` stamped on a turn, scanning from the end.

    C-23.35 admits a revive only for `bypassPermissions`; a headless `-p` run of
    any other mode would auto-deny its own tools. The scan is over raw bytes
    rather than parsed entries, exactly as v1 `notify._last_permission_mode`
    does it, because the harness has stamped the key at more than one depth and
    a reader that knows the nesting would silently stop finding it if that
    changed again.
    """
    if not transcript:
        return None
    path = Path(transcript).expanduser()
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            end, carry, scanned = size, b"", 0
            while end > 0 and scanned < _SCAN_MAX:
                start = max(0, end - _TAIL_BYTES)
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


def last_assistant_model(transcript: str | Path | None) -> str | None:
    """The model that served the last real assistant turn (C-23.39's fallback).

    Skips the app's synthetic entries: limit banners and resume stubs carry the
    model `<synthetic>`.
    """
    if not transcript:
        return None
    for line in lines_reversed(Path(transcript).expanduser()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain"):
            continue
        model = (entry.get("message") or {}).get("model")
        if isinstance(model, str) and model and not model.startswith("<"):
            return model
    return None


def last_cwd(transcript: str | Path | None) -> str | None:
    """The working directory of the last main-chain entry."""
    if not transcript:
        return None
    for line in lines_reversed(Path(transcript).expanduser(), chunk=256 * 1024):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if is_main(entry) and isinstance(entry.get("cwd"), str) and entry["cwd"]:
            return entry["cwd"]
    return None


@dataclass(frozen=True)
class ColdSession:
    """A transcript whose session has no live process (a `continue --scope cold`
    candidate). Nothing outside can wake one — the inbox needs a process — so a
    cold session is recovered by an explicit handoff, or by an opted-in revive.
    """

    session_id: str
    project: str
    transcript: str
    state: TurnState = field(default_factory=TurnState)

    @property
    def age_s(self) -> int | None:
        return self.state.age_s


def cold_sessions(*, live_ids: set[str], lane_ids: set[str], max_age_s: float,
                  now: datetime | None = None) -> list[ColdSession]:
    """Recently-active interrupted transcripts with no live registry row.

    A morning account switch restarts only what was running at switch time, so
    sessions the overnight idle reaper already killed come back cold. Headless
    lane runs are excluded here (C-23.31) and retirement is applied by the
    caller, which is the side that can ask the daemon.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - max_age_s
    rows: list[ColdSession] = []
    try:
        directories = [item for item in projects_dir().iterdir() if item.is_dir()]
    except OSError:
        return rows
    for directory in directories:
        try:
            entries = sorted(directory.glob("*.jsonl"))
        except OSError:
            continue
        for entry in entries:
            session_id = entry.name[:-len(".jsonl")]
            if session_id in live_ids or session_id in lane_ids:
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            if headless_transcript(entry):
                continue
            state = turn_state(entry, now=now)
            if state.state != "interrupted":
                continue
            if state.age_s is not None and state.age_s > max_age_s:
                continue
            rows.append(ColdSession(session_id=session_id, project=directory.name,
                                    transcript=str(entry), state=state))
    rows.sort(key=lambda row: (row.age_s if row.age_s is not None else 0, row.session_id))
    return rows
