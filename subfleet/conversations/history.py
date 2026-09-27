"""Pages of a conversation's native history (C-25.2 `conversation.history`, C-29.8).

Read from the transcript's tail backwards and rendered as `{role, text, ts,
id, kind}`, scrubbed as events are (C-25.5). A page reads at most `READ_CAP`
(4 MiB) of rows below its cursor, or the one row there when that row is larger
(up to `READ_BUDGET`, 68 MiB: the cap plus 64 MiB), and up to `RESULT_WINDOW`
(8 MiB) past the cursor for the
results of calls at the page's edge. A tool
call is `kind: "tool"` with its redacted summary, its result preview and
whether it failed, as a live `tool.completed` carries them; a credential-reading
call is hidden, with no preview. Thinking the transcript kept is
`kind: "thinking"`. So a session continued from another app reads the way its
live turns do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..sessions import transcripts
from . import redact

READ_CAP = 4 * 1024 * 1024


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "") if content.get("type") in ("text", "input_text", "output_text") else ""
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content
                         if isinstance(item, dict) and item.get("type") in ("text", "input_text", "output_text")).strip()
    return ""


def _tool_item(name: str, value: Any, tool_id: str | None, result: dict | None, *, ts, cursor: int) -> dict:
    started = redact.tool_started(name, value, tool_id=tool_id)
    item = {"role": "assistant", "kind": "tool", "text": started["summary"], "tool": started["name"],
            "tool_id": tool_id, "hidden": started["hidden"], "ts": ts, "id": tool_id, "cursor": cursor,
            "preview": None, "is_error": None}
    if result is not None:
        done = redact.tool_completed(tool_id, result["text"], is_error=result["is_error"], hidden=started["hidden"])
        item.update(preview=done["preview"], is_error=done["is_error"])
    return item


#: How far past a page's cursor its calls' results are looked for: a result is
#: written right after its call, so this is generous.
RESULT_WINDOW = 8 * 1024 * 1024
#: The reader's own bound, beyond the cap: room for one large row.
READ_BUDGET = READ_CAP + 64 * 1024 * 1024
READ_CHUNK = 512 * 1024


def _after_cursor(path: Path, before: int | None, marker: str):
    """The raw rows just past the cursor that may hold a result for a call below
    it, read forward, whole however long."""
    if before is None:
        return
    for _, raw in transcripts.lines_forward_with_offsets(path, before, RESULT_WINDOW):
        if marker in raw:
            yield raw


def _below_cursor(path: Path, top: int):
    """The rows below the cursor, newest first. The cursor is a row's start, so no
    row is cut; the reader stops after `READ_BUDGET` bytes."""
    return transcripts.lines_reversed_with_offsets(path, end=top, max_bytes=READ_BUDGET, chunk=READ_CHUNK)


#: Leading blank space longer than this is not looked through for a row.
BLANK_PROBE = 64 * 1024


def _earlier(path: Path, start: int) -> int | None:
    """The cursor for what lies before the row at `start`: None when only blank
    space does, so the page that reached the file's first row ends the history
    rather than leaving one more, empty page to load."""
    if start <= 0:
        return None
    if start > BLANK_PROBE:
        return start
    try:
        with transcripts.open_regular(path) as handle:
            return start if handle.read(start).strip() else None
    except OSError:
        return start


def _next_cursor(path: Path, top: int, last: int | None) -> int | None:
    """Where the next page starts once the rows ran out: None at the file's start;
    past a row too large for the reader when nothing below the cursor was read."""
    if last is None:
        if top <= READ_BUDGET:
            # The reader covered everything back to the file's start and found no
            # row (only blank space): the history ends (review of 5aa2718).
            return None
        # No further back than the reader's own budget: a row whose start lies
        # further back is stepped over in budget-sized steps (review of aa41312).
        start = transcripts.line_start(path, top - 1, max_bytes=READ_BUDGET) if top > 0 else 0
        return _earlier(path, start)
    return _earlier(path, last)


def _note_blocks(blocks: list[dict], results: dict[str, dict]) -> None:
    for block in blocks:
        if block.get("type") == "tool_result" and block.get("tool_use_id"):
            results[str(block["tool_use_id"])] = {"text": _result_text(block.get("content")),
                                                  "is_error": bool(block.get("is_error"))}


def _note_results(raw: str, results: dict[str, dict]) -> None:
    try:
        row = json.loads(raw)
    except ValueError:
        return
    content = (row.get("message") or {}).get("content") if isinstance(row, dict) else None
    if transcripts.is_main(row) and isinstance(content, list):
        _note_blocks([b for b in content if isinstance(b, dict)], results)


def _claude_items(path: Path, before: int | None, limit: int) -> tuple[list[dict], int | None]:
    """A page of rows older than `before`, newest first. The cursor is a row's
    byte offset from the start of the file, which later turns never move, so
    "Load earlier" after a turn returns the next older rows (review, 2026-09-25).

    A page ends after a whole row, so a row's blocks are never split across two
    pages; it holds at least `limit` items unless the file's start or the read cap
    comes first. The next cursor is the last row read, and is None only at the
    file's start: a cap reached before any item still hands on a cursor, and a
    row too large to read is stepped over rather than ending the history."""
    items: list[dict] = []
    try:
        size = path.stat().st_size
    except OSError:
        return [], None
    top = size if before is None else min(before, size)
    # Newest first: a call's result is read before the call. Rows a previous page
    # returned are still read for results, so a call at a page's edge keeps its own.
    results: dict[str, dict] = {}
    for raw in _after_cursor(path, before, '"tool_result"'):
        _note_results(raw, results)
    last: int | None = None
    for index, raw in _below_cursor(path, top):
        if last is not None and top - index > READ_CAP:
            return items, _earlier(path, last)
        last = index
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not transcripts.is_main(row):
            continue
        message = row.get("message") or {}
        kind = row.get("type")
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        blocks = [b for b in content or [] if isinstance(b, dict)]
        _note_blocks(blocks, results)
        for block in reversed(blocks):
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                items.append({"role": "assistant" if kind == "assistant" else "user", "kind": "text",
                              "text": redact.bounded_text(block["text"]), "ts": row.get("timestamp"),
                              "id": row.get("uuid"), "cursor": index})
            elif btype == "thinking" and block.get("thinking"):
                items.append({"role": "assistant", "kind": "thinking", "text": redact.bounded_text(block["thinking"]),
                              "ts": row.get("timestamp"), "id": row.get("uuid"), "cursor": index})
            elif btype == "tool_use":
                tool_id = str(block.get("id")) if block.get("id") else None
                items.append(_tool_item(str(block.get("name")), block.get("input"), tool_id,
                                        results.get(tool_id or ""), ts=row.get("timestamp"), cursor=index))
        if len(items) >= limit:
            return items, _earlier(path, index)
    return items, _next_cursor(path, top, last)


def page(conversation: dict, *, root: Path, before=None, limit: int = 50, lanes: list[dict]) -> dict:
    limit = max(1, min(int(limit), 200))
    before = int(before) if before is not None else None
    sid = conversation.get("native_session_id")
    if not sid:
        return {"items": [], "next_before": None}
    if conversation["provider"] == "claude":
        path = transcripts.transcript_path(sid)
        if path is None:
            return {"items": [], "next_before": None, "missing": True}
        items, nxt = _claude_items(path, before, limit)
        return {"items": items, "next_before": nxt}
    from .catalog import native_session  # noqa: F401  (Codex: newest first)
    homes = [Path(r["home"]) for r in lanes if r.get("provider") == "codex" and r.get("home")]
    for home in homes:
        matches = list((home / "sessions").rglob(f"rollout-*{sid}.jsonl")) if (home / "sessions").is_dir() else []
        if len(matches) == 1:
            items, nxt = _codex_items(matches[0], before, limit)
            return {"items": items, "next_before": nxt}
    return {"items": [], "next_before": None, "missing": True}


#: Rollout items that are a tool call, and the ones that carry its output.
CODEX_CALLS = ("function_call", "custom_tool_call", "local_shell_call")
CODEX_OUTPUTS = ("function_call_output", "custom_tool_call_output")


#: A code-mode `exec` call's shell commands: `tools.exec_command({cmd:"…"})`.
_EXEC_CMD = re.compile(r'exec_command\(\s*\{\s*cmd\s*:\s*"((?:[^"\\]|\\.)*)"')


def _js_string(literal: str) -> str:
    """A double-quoted JS literal's text; an escape JSON does not know is kept as written."""
    try:
        return json.loads(f'"{literal}"')
    except ValueError:
        return literal


def _codex_call(payload: dict) -> tuple[str, Any]:
    if payload.get("name") == "exec" and isinstance(payload.get("input"), str):
        script = payload["input"]
        if redact.sensitive_tool_call("exec", script):
            # Judged on the whole script, not the commands shown: a credential
            # read outside a `cmd:"…"` literal must still hide the call.
            return "exec", script
        # Code mode wraps shell commands in a script; show them as a live turn
        # shows a commandExecution item.
        commands = [_js_string(c) for c in _EXEC_CMD.findall(script)]
        if commands:
            return "command", {"command": "; ".join(commands)}
    name = str(payload.get("name") or payload.get("type") or "tool")
    if payload.get("namespace"):
        name = f"{payload['namespace']}/{name}"
    if "arguments" in payload:
        try:
            value = json.loads(payload["arguments"])
        except (TypeError, ValueError):
            return name, payload.get("arguments")
        if payload.get("namespace") == "collaboration" and isinstance(value, dict):
            # Agent-to-agent messages travel encrypted; who it went to is the news.
            peer = value.get("target") or value.get("recipient")
            return name, {"description": f"to {peer}"} if peer else {}
        return name, value
    if "input" in payload:
        return name, payload.get("input")
    return name, payload.get("action")


#: A status line that says the command failed. Only an output's header is
#: judged: its lines before the `Output:` line, or its first line when it has
#: none. Code mode puts `Script failed` first; an `exec_command` output has
#: `Chunk ID`, `Wall time`, then `Process exited with code N` (seen in lane
#: rollouts, 2026-09-25). The command's own output (a log it printed) may hold
#: any of these words.
_FAILED_STATUS = re.compile(r"\s*(Script failed\b|Process exited with code -?[1-9]|Exit code: -?[1-9])")
#: How much of an output's start holds its header.
STATUS_HEAD = 4096


def _failed_status(text: str) -> bool:
    lines = text[:STATUS_HEAD].splitlines()
    header = next((lines[:at] for at, line in enumerate(lines) if line.strip() == "Output:"), lines[:1])
    return any(_FAILED_STATUS.match(line) for line in header)


def _codex_outcome(output: Any) -> tuple[str, bool]:
    """An output's text and whether it reports a failure: `success: false`, a
    nonzero `metadata.exit_code`, or a failing status line in its header."""
    if isinstance(output, dict):
        text = str(output.get("content") or output.get("output") or "")
        code = (output.get("metadata") or {}).get("exit_code") if isinstance(output.get("metadata"), dict) else None
        failed = output.get("success") is False or (isinstance(code, int) and code != 0)
        return text, failed or _failed_status(text)
    text = _result_text(output)
    return text, _failed_status(text)


def _note_output(raw: str, outputs: dict[str, dict], row: dict | None = None) -> None:
    try:
        row = json.loads(raw) if row is None else row
        payload = row.get("payload") or {}
        if row.get("type") == "response_item" and payload.get("type") in CODEX_OUTPUTS and payload.get("call_id"):
            text, failed = _codex_outcome(payload.get("output"))
            outputs[str(payload["call_id"])] = {"text": text, "is_error": failed}
    except (ValueError, TypeError, AttributeError):
        return


def _codex_items(path: Path, before: int | None, limit: int) -> tuple[list[dict], int | None]:
    """As `_claude_items`: newest first, byte-offset cursors, outputs found across
    a page's edge, the same read cap; a row that cannot be read is skipped, never
    the page."""
    items: list[dict] = []
    outputs: dict[str, dict] = {}
    try:
        size = path.stat().st_size
    except OSError:
        return [], None
    top = size if before is None else min(before, size)
    for raw in _after_cursor(path, before, "_output"):
        _note_output(raw, outputs)
    last: int | None = None
    for index, raw in _below_cursor(path, top):
        if last is not None and top - index > READ_CAP:
            return items, _earlier(path, last)
        last = index
        try:
            row = json.loads(raw)
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload") or {}
            ptype = payload.get("type")
            if ptype in CODEX_OUTPUTS and payload.get("call_id"):
                _note_output(raw, outputs, row)
                continue
            if ptype in CODEX_CALLS:
                call_id = str(payload.get("call_id")) if payload.get("call_id") else None
                name, value = _codex_call(payload)
                items.append(_tool_item(name, value, call_id, outputs.get(call_id or ""), ts=row.get("timestamp"),
                                        cursor=index))
            elif ptype == "reasoning":
                summary = "\n".join(str(s.get("text") or "") for s in payload.get("summary") or []
                                    if isinstance(s, dict)).strip()
                if summary:
                    items.append({"role": "assistant", "kind": "thinking", "text": redact.bounded_text(summary),
                                  "ts": row.get("timestamp"), "id": None, "cursor": index})
            elif ptype == "message" and payload.get("role") in ("user", "assistant"):
                text = " ".join(c.get("text", "") for c in payload.get("content", []) if isinstance(c, dict)).strip()
                if text and not text.startswith("<"):
                    items.append({"role": payload["role"], "kind": "text", "text": redact.bounded_text(text),
                                  "ts": row.get("timestamp"), "id": None, "cursor": index})
        except (ValueError, TypeError, AttributeError):
            continue
        if len(items) >= limit:
            return items, _earlier(path, index)
    return items, _next_cursor(path, top, last)
