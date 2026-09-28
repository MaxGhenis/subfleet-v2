"""What a conversation event may carry (C-25.5, design D-8).

Events hold what a person may see: the assistant's text, the reasoning
summaries the provider's own app displays, and tool activity reduced to a
name, a scrubbed input summary of at most 500 characters and a scrubbed
result preview of at most 2 KiB. A tool call that reads credentials is shown
only as hidden, with neither input nor output. Scrubbing is the handoff
scrubber (C-23.14), so a value it removes from a handoff brief is removed here
too.
"""

from __future__ import annotations

import json
from typing import Any

from ..sessions.handoff import (
    looks_binary, scrub_secrets, sensitive_tool_call, truncate,
)

INPUT_MAX = 500
RESULT_MAX = 2048
TEXT_EVENT_MAX = 60_000          # an event's text; the 64 KiB row cap leaves room for keys
HIDDEN = "credential access (hidden)"

# Claude and Codex tool inputs name the field a person recognises first.
_PREFERRED_FIELDS = (
    "command", "cmd", "file_path", "path", "notebook_path", "pattern", "url",
    "query", "description", "prompt", "skill",
)


def scrub(text: str) -> str:
    """Remove credentials, reminders and encoded blobs from displayable text."""
    return scrub_secrets(text, strip_reminders=True)[0]


def _summary_text(name: str, value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        for key in _PREFERRED_FIELDS:
            field = value.get(key)
            if isinstance(field, str) and field:
                parts.append(field if key in ("command", "cmd") else f"{key}: {field}")
            elif isinstance(field, list) and field and all(isinstance(x, str) for x in field):
                parts.append(" ".join(field) if key in ("command", "cmd") else f"{key}: {' '.join(field)}")
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def tool_started(name: str, value: Any, *, tool_id: str | None) -> dict:
    """The `tool.started` payload for one call."""
    name = str(name or "tool")[:80]
    if sensitive_tool_call(name, value):
        return {"id": tool_id, "name": name, "hidden": True, "summary": HIDDEN}
    summary = scrub(_summary_text(name, value))
    return {"id": tool_id, "name": name, "hidden": False, "summary": truncate(summary, INPUT_MAX)}


def tool_completed(tool_id: str | None, text: str, *, is_error: bool | None, hidden: bool) -> dict:
    """The `tool.completed` payload. A hidden call's output is never kept."""
    if hidden:
        return {"id": tool_id, "is_error": bool(is_error), "hidden": True, "preview": ""}
    text = text or ""
    if looks_binary(text):
        preview = "[binary output omitted]"
    else:
        preview = truncate(scrub(text), RESULT_MAX)
    return {"id": tool_id, "is_error": bool(is_error), "hidden": False, "preview": preview}


def bounded_text(text: str) -> str:
    """A whole text or thinking block, scrubbed and bounded for one event row."""
    return truncate(scrub(text), TEXT_EVENT_MAX)


class DeltaBuffer:
    """Streamed text is scrubbed a line at a time.

    A credential is one token and never spans a line, but it can span two
    deltas; holding text until a newline (or 2 KiB, or an explicit flush when a
    non-text event arrives) means the scrubber always sees whole tokens.
    """

    LIMIT = 2048

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, delta: str) -> str:
        self._pending += delta
        cut = self._pending.rfind("\n")
        if cut < 0 and len(self._pending) < self.LIMIT:
            return ""
        if cut < 0:
            # No newline in 2 KiB: cut at the last space so a token stays whole.
            cut = self._pending.rfind(" ")
            if cut < 0:
                cut = len(self._pending) - 1
        ready, self._pending = self._pending[:cut + 1], self._pending[cut + 1:]
        return scrub(ready)

    def flush(self) -> str:
        ready, self._pending = self._pending, ""
        return scrub(ready) if ready else ""


# --- approvals (C-27.1, review IR-20) -------------------------------------------

_SHELL_META = ("$(", "`", "|", ";", "&", ">", "<", "\n")


def mask_approval(request: Any) -> tuple[Any, list[dict]]:
    """Mask value-shaped secrets in an approval request, and nothing else.

    A person must see what they are allowing. Only token-shaped values (PEM
    blocks, JWTs, prefixed tokens, Bearer tokens, URL passwords) are masked,
    never by key name, never long base64, and never a span that contains
    command substitution, a pipe, a separator, a redirection or a newline.
    Each masked span is reported so the app can show it and offer a reveal.
    """
    import hashlib
    from ..sessions.handoff import _BEARER_RE, _JWT_RE, _PEM_RE, _PREFIXED_TOKEN_RE, _URL_PASSWORD_RE, _linear_sub
    spans: list[dict] = []

    def mask_text(text: str, path: str) -> str:
        for rule, pattern in (("pem", _PEM_RE), ("jwt", _JWT_RE), ("token", _PREFIXED_TOKEN_RE),
                              ("bearer", _BEARER_RE), ("url-password", _URL_PASSWORD_RE)):
            def repl(match):
                value = match.group(0) if rule != "url-password" else match.group(2)
                if any(meta in value for meta in _SHELL_META):
                    return match.group(0)
                spans.append({"path": path, "rule": rule, "length": len(value),
                              "sha256": hashlib.sha256(value.encode()).hexdigest()})
                hidden = f"[masked {rule}, {len(value)} chars]"
                return hidden if rule != "url-password" else match.group(1) + hidden + match.group(3)
            text = _linear_sub(pattern, repl, text)
        return text

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, str):
            return mask_text(value, path)
        if isinstance(value, dict):
            return {k: walk(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    return walk(request, "$"), spans
