"""Pages of a conversation's native history (C-25.2 `conversation.history`, C-29.8).

Read from the transcript's tail backwards, at most 4 MiB per call, rendered
as `{role, text, ts, id, kind}` and scrubbed as events are (C-25.5). Tool
calls appear as their redacted summaries; credential-reading calls as hidden.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..sessions import transcripts
from . import redact

READ_CAP = 4 * 1024 * 1024


def _claude_items(path: Path, before: int | None, limit: int) -> tuple[list[dict], int | None]:
    items: list[dict] = []
    read = 0
    tools: dict[str, bool] = {}
    for index, raw in enumerate(transcripts.lines_reversed(path)):
        read += len(raw) + 1
        if read > READ_CAP:
            break
        if before is not None and index <= before:
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
        for block in reversed(content or []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                items.append({"role": "assistant" if kind == "assistant" else "user", "kind": "text",
                              "text": redact.bounded_text(block["text"]), "ts": row.get("timestamp"),
                              "id": row.get("uuid"), "cursor": index})
            elif btype == "tool_use":
                started = redact.tool_started(str(block.get("name")), block.get("input"), tool_id=block.get("id"))
                tools[str(block.get("id"))] = started["hidden"]
                items.append({"role": "assistant", "kind": "tool", "text": started["summary"], "tool": started["name"],
                              "ts": row.get("timestamp"), "id": block.get("id"), "cursor": index})
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
    from .catalog import native_session  # Codex: newest-first user and agent messages
    homes = [Path(r["home"]) for r in lanes if r.get("provider") == "codex" and r.get("home")]
    for home in homes:
        matches = list((home / "sessions").rglob(f"rollout-*{sid}.jsonl")) if (home / "sessions").is_dir() else []
        if len(matches) == 1:
            return {"items": _codex_items(matches[0], before, limit), "next_before": None}
    return {"items": [], "next_before": None, "missing": True}


def _codex_items(path: Path, before, limit: int) -> list[dict]:
    items = []
    for index, raw in enumerate(transcripts.lines_reversed(path)):
        if before is not None and index <= before:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        if row.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
            text = " ".join(c.get("text", "") for c in payload.get("content", []) if isinstance(c, dict)).strip()
            if text and not text.startswith("<"):
                items.append({"role": payload["role"], "kind": "text", "text": redact.bounded_text(text),
                              "ts": row.get("timestamp"), "id": None, "cursor": index})
        if len(items) >= limit:
            break
    return items
