"""Continuity briefs from Codex rollouts (C-30.3; C-23.14, C-23.36; design D-18).

A labelled handoff out of a Codex thread starts a new conversation whose first
message is a brief of that thread. The brief is built exactly as the Claude
reader builds one (`subfleet/sessions/handoff.py`): the same three rules of
C-23.14, in the same order, and the same caps.

1. **Suppression by pattern.** A tool call whose input matches a
   credential-reading pattern (`handoff.sensitive_tool_call`) is shown only as
   omitted, and so is its output.
2. **Scrubbing by value.** Every retained text goes through `handoff.clean`
   (system reminders removed, `handoff.scrub_secrets`, a per-section cap); a
   binary-looking output or an image is omitted.
3. **Retention.** Ordinary text, commands and tool output are kept verbatim.

The whole brief is assembled and scrubbed once more by `handoff.assemble`.

What a rollout holds, as far as this module relies on it: one JSON object per
line with `type` and `payload`. `session_meta` comes first and carries the
thread `id` and `cwd` (the fake app-server writes it so, `tests/fake/
interactive_codex.py`, and the v2 adapter map records the same keys observed on
0.153.3, `docs/desktop/maps/v2-codex-adapter.md`). `response_item` payloads are
read by their `type` as the pinned 0.153.3 schema defines `ResponseItem`
(`tests/fixtures/codex/app-server-0.153.3/ClientRequest.json`): `message`
(role and `input_text`/`output_text` content), `function_call` (`name`,
`arguments` as a JSON string, `call_id`), `custom_tool_call` (`name`, `input`,
`call_id`), `local_shell_call` (`action.command`, `call_id`),
`function_call_output` and `custom_tool_call_output` (`call_id`, `output` as a
string or content items). A `reasoning` item is never read (design D-11 keeps
Codex reasoning out of everything a person or another agent is shown), and
every other record type (`turn_context`, `event_msg`, compaction, web search,
image generation) is skipped. The fake app-server writes only `message` items;
the tool-call shapes above come from the schema, not from an observed rollout.
User messages whose text begins with `<` are context the client injected, not
the person's words, as the catalog and history readers already treat them
(`catalog.py`, `history.py`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..sessions import handoff, transcripts
from ..sessions.handoff import (
    OMITTED_BINARY, OMITTED_SENSITIVE, OMITTED_SENSITIVE_INPUT, OMITTED_UNMATCHED, Brief, HandoffError,
    clean, looks_binary, sensitive_tool_call, truncate,
)

#: How much of a rollout's head `session_meta` must appear in.
META_BYTES = 64 * 1024
#: Tool-call item types, and the output types answering them.
CALLS = ("function_call", "custom_tool_call", "local_shell_call")
OUTPUTS = ("function_call_output", "custom_tool_call_output")
READ_TYPES = ("message", *CALLS, *OUTPUTS)
OMITTED_MEDIA = "[image or audio tool result omitted]"


def canonical_thread_id(value: Any) -> str:
    """A Codex thread id is a UUID; anything else never reaches a path pattern."""
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HandoffError(f"invalid Codex thread id: {value!r}") from exc
    if str(parsed) != str(value).lower():
        raise HandoffError(f"Codex thread id must be a canonical UUID: {value!r}")
    return str(parsed)


def find_rollout(thread_id: str, homes: Iterable[Path | str]) -> Path | None:
    """The one `rollout-*-<thread id>.jsonl` under the first home that has any.

    Walks only the homes given (a conversation's own lane home, or the enrolled
    homes and `~/.codex` for a thread no conversation holds); the caller runs on
    the daemon's bounded file pool (C-25.3). Two rollouts for one thread in one
    home are ambiguous and refused.
    """
    thread_id = canonical_thread_id(thread_id)
    suffix = f"-{thread_id}.jsonl"
    for home in homes:
        found: list[Path] = []
        for sub in ("sessions", "archived_sessions"):
            base = Path(home) / sub
            if not base.is_dir():
                continue
            for directory, _dirs, files in os.walk(base):
                found.extend(Path(directory) / name for name in files
                             if name.startswith("rollout-") and name.endswith(suffix))
        if len(found) > 1:
            raise HandoffError(f"{len(found)} rollouts name Codex thread {thread_id} under {home}")
        if found:
            return found[0]
    return None


def session_meta(path: Path) -> dict[str, Any]:
    """The rollout's `session_meta` payload (its first record), or {}."""
    try:
        with transcripts.open_regular(path) as stream:
            head = stream.read(META_BYTES)
    except OSError as exc:
        raise HandoffError(f"cannot read rollout {path}: {exc}") from exc
    for raw in head.splitlines():
        record = handoff._parse(raw.decode("utf-8", "replace"))
        if record is None:
            continue
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return record["payload"]
        break
    return {}


def headless_run(path: Path) -> bool:
    """A Codex `exec` run or a subagent's thread: a lane run, not a conversation.

    The catalog's own exclusion (C-30.1), read through the catalog's reader so
    the two can never disagree.
    """
    from .catalog import _codex_record
    return bool(_codex_record(path).get("excluded"))


def _records(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """(raw line, record) in file order, in the rollout's first
    `handoff.FULL_SCAN_BYTES`; unreadable or oversized lines are skipped."""
    try:
        stream = transcripts.open_regular(path)
    except OSError as exc:
        raise HandoffError(f"cannot read rollout {path}: {exc}") from exc
    with stream:
        for raw in transcripts.capped_lines(stream, handoff.FULL_SCAN_BYTES):
            line = raw.decode("utf-8", "replace")
            record = handoff._parse(line)
            if record is not None:
                yield line.rstrip("\n"), record


def _item(record: dict[str, Any]) -> dict[str, Any] | None:
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return None
    return payload if payload.get("type") in READ_TYPES else None


def _message_text(item: dict[str, Any]) -> str:
    parts = []
    for content in item.get("content") or []:
        if isinstance(content, dict) and content.get("type") in ("input_text", "output_text", "text"):
            parts.append(str(content.get("text") or ""))
    return "\n".join(parts).strip()


def _person_text(item: dict[str, Any]) -> str:
    """A user message's text, or '' when it is injected context rather than the person's."""
    if item.get("type") != "message" or item.get("role") != "user":
        return ""
    text = _message_text(item)
    return "" if text.startswith("<") else text


def _key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def first_task(path: Path, cap: int) -> tuple[str, str, int]:
    """The thread's first message from the person, cleaned and capped."""
    for raw, record in _records(path):
        item = _item(record)
        text = _person_text(item) if item else ""
        if text:
            cleaned, redactions = clean(text, cap)
            return cleaned, _key(raw), redactions
    raise HandoffError(f"no user task text found in rollout {path}")


def _arguments(item: dict[str, Any]) -> Any:
    """A call's input: `arguments` parsed when it is JSON, else the text as written."""
    kind = item.get("type")
    if kind == "custom_tool_call":
        return item.get("input")
    if kind == "local_shell_call":
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        # The action's environment map is neither shown nor matched: only the command is.
        return {"command": action.get("command"), "working_directory": action.get("working_directory")}
    raw = item.get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _command_text(command: Any) -> str | None:
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        if len(command) == 3 and command[1] in ("-c", "-lc"):
            return command[2]                 # `bash -lc <script>`: the script is the command
        return shlex.join(command)
    return None


def _input_text(value: Any) -> str:
    if isinstance(value, dict):
        for field in ("cmd", "command"):
            text = _command_text(value.get(field))
            if text:
                return text
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _output_text(item: dict[str, Any]) -> tuple[str, bool]:
    """(text, carried an image or audio) of a call's output; encrypted parts are dropped."""
    output = item.get("output")
    if isinstance(output, str):
        return output, False
    parts, media = [], False
    for content in output if isinstance(output, list) else []:
        if not isinstance(content, dict):
            continue
        if content.get("type") in ("input_text", "output_text", "text"):
            parts.append(str(content.get("text") or ""))
        elif content.get("type") in ("input_image", "input_audio"):
            media = True
    return "\n".join(parts).strip(), media


def _recent_items(path: Path, limit: int) -> list[tuple[str, dict[str, Any]]]:
    """The last `limit` items this reader keeps, newest last."""
    items: list[tuple[str, dict[str, Any]]] = []
    for raw in transcripts.lines_reversed(path, max_bytes=handoff.FULL_SCAN_BYTES):
        record = handoff._parse(raw)
        item = _item(record) if record else None
        if item is None:
            continue
        items.append((raw, item))
        if len(items) >= limit:
            break
    items.reverse()
    return items


def recent_excerpt(path: Path, first_key: str | None, caps: dict[str, int]) -> tuple[str, int]:
    """The bounded tail of the thread: messages, tool inputs and tool outputs."""
    calls: dict[str, tuple[str, bool]] = {}
    segments: list[tuple[str, int, str | None]] = []

    def add(label: str, body: str, *, tool_kind: str | None = None, limit: int = 8_000) -> None:
        if not body:
            return
        cleaned, redactions = clean(body, limit)
        if cleaned:
            segments.append((f"{label}\n{cleaned}", redactions, tool_kind))

    for raw, item in _recent_items(path, caps["recent_records"]):
        kind = item.get("type")
        if kind == "message":
            if item.get("role") == "user":
                if _key(raw) != first_key:
                    add("Codex user:", _person_text(item))
            elif item.get("role") == "assistant":
                add("Codex assistant:", _message_text(item))
        elif kind in CALLS:
            name = str(item.get("name") or ("shell" if kind == "local_shell_call" else "tool"))
            call_id = str(item.get("call_id") or item.get("id") or "")
            value = _arguments(item)
            sensitive = sensitive_tool_call(name, value)
            calls[call_id] = (name, sensitive)
            if sensitive:
                segments.append((f"Codex tool call ({name}):\n{OMITTED_SENSITIVE_INPUT}", 1, "input"))
            else:
                add(f"Codex tool call ({name}):", _input_text(value), tool_kind="input",
                    limit=caps["tool_input"])
        elif kind in OUTPUTS:
            matched = calls.get(str(item.get("call_id") or ""))
            if matched is None:
                # Its call is outside this excerpt, so its sensitivity is unknown;
                # unknown means omitted (as the Claude reader does).
                segments.append((f"Codex tool result:\n{OMITTED_UNMATCHED}", 1, "result"))
                continue
            name, sensitive = matched
            if sensitive:
                segments.append((f"Codex tool result ({name}):\n{OMITTED_SENSITIVE}", 1, "result"))
                continue
            text, media = _output_text(item)
            if text and looks_binary(text):
                segments.append((f"Codex tool result ({name}):\n{OMITTED_BINARY}", 1, "result"))
                continue
            if media and not text:
                segments.append((f"Codex tool result ({name}):\n{OMITTED_MEDIA}", 1, "result"))
                continue
            add(f"Codex tool result ({name}):", text, tool_kind="result", limit=caps["tool_result"])
    return handoff.select_segments(segments, caps)


def build_brief(thread_id: str, rollout: Path, cwd: Path, source_cwd: str | None,
                caps: dict[str, int], *, repository: bool = True) -> Brief:
    """A Codex thread's brief, section for section as `handoff.build_brief`."""
    thread_id = canonical_thread_id(thread_id)
    meta = session_meta(rollout)
    if meta.get("id") and str(meta["id"]).lower() != thread_id:
        raise HandoffError(f"rollout {rollout} belongs to thread {meta['id']}, not {thread_id}")
    original, first_key, redactions = first_task(rollout, caps["original_task"])
    recent, recent_redactions = recent_excerpt(rollout, first_key, caps)
    if not recent:
        recent = truncate("No additional text or safe tool-result context was available.",
                          caps["recent"])
    redactions += recent_redactions
    progress, repo, count = handoff.workspace_sections(cwd, caps, repository=repository)
    redactions += count
    text, redactions = handoff.assemble(
        provider="Codex", source_label="thread", session_id=thread_id, transcript=rollout,
        source_cwd=source_cwd or meta.get("cwd"), cwd=cwd, original=original,
        recent_title="Recent rollout excerpt", recent=recent, progress=progress, repository=repo,
        redactions=redactions)
    return Brief(text=text, original=original, session_id=thread_id, transcript=str(rollout),
                 workdir=str(cwd), source_cwd=source_cwd or meta.get("cwd"), redactions=redactions)


__all__ = ["build_brief", "canonical_thread_id", "find_rollout", "first_task", "headless_run",
           "recent_excerpt", "session_meta"]
