"""Pages of a conversation's native history (C-25.2 `conversation.history`, C-29.8).

Read from the transcript's tail backwards, at most 4 MiB per call, rendered
as `{role, text, ts, id, kind}` and scrubbed as events are (C-25.5). A tool
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


def _claude_items(path: Path, before: int | None, limit: int) -> tuple[list[dict], int | None]:
    """A page of rows older than `before`, newest first. The cursor is a row's
    byte offset from the start of the file, which later turns never move, so
    "Load earlier" after a turn returns the next older rows (review, 2026-09-25)."""
    items: list[dict] = []
    try:
        size = path.stat().st_size
    except OSError:
        return [], None
    # Newest first: a call's result is read before the call. Rows a previous page
    # returned are still read for results, so a call at a page's edge keeps its own.
    results: dict[str, dict] = {}
    for index, raw in transcripts.lines_reversed_with_offsets(path):
        if size - index > READ_CAP + (size - before if before is not None else 0):
            break
        skipped = before is not None and index >= before
        if skipped and '"tool_result"' not in raw:
            continue
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
        for block in blocks:
            if block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[str(block["tool_use_id"])] = {"text": _result_text(block.get("content")),
                                                      "is_error": bool(block.get("is_error"))}
        if skipped:
            continue
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
            break
    return items[:limit], (items[limit - 1]["cursor"] if len(items) >= limit else None)


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


#: A code-mode or shell output that says the command failed.
_FAILED_OUTPUT = re.compile(r"^(Script failed|Process exited with code [1-9]|Exit code: [1-9])", re.M)


def _codex_outcome(output: Any) -> tuple[str, bool]:
    """An output's text and whether it reports a failure."""
    if isinstance(output, dict):
        text = str(output.get("content") or output.get("output") or "")
        return text, output.get("success") is False or bool(_FAILED_OUTPUT.search(text))
    text = _result_text(output)
    return text, bool(_FAILED_OUTPUT.search(text))


def _codex_items(path: Path, before: int | None, limit: int) -> tuple[list[dict], int | None]:
    """As `_claude_items`: newest first, byte-offset cursors, outputs found across
    a page's edge; a row that cannot be read is skipped, never the page."""
    items: list[dict] = []
    outputs: dict[str, dict] = {}
    for index, raw in transcripts.lines_reversed_with_offsets(path):
        skipped = before is not None and index >= before
        if skipped and "_output" not in raw:
            continue
        try:
            row = json.loads(raw)
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload") or {}
            ptype = payload.get("type")
            if ptype in CODEX_OUTPUTS and payload.get("call_id"):
                text, failed = _codex_outcome(payload.get("output"))
                outputs[str(payload["call_id"])] = {"text": text, "is_error": failed}
                continue
            if skipped:
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
            break
    return items[:limit], (items[limit - 1]["cursor"] if len(items) >= limit else None)
