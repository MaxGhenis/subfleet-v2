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

import hashlib
import json
import re
from typing import Any

from ..sessions.handoff import (
    looks_binary, scrub_bounded, scrub_secrets, sensitive_tool_call,
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


def bounded(text: str, limit: int) -> str:
    """`scrub`, bounded to `limit` characters keeping the head and the tail. A
    text over the scrubber's budget is excerpted before matching (C-23.14)."""
    return scrub_bounded(text, limit, strip_reminders=True)[0]


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
    return {"id": tool_id, "name": name, "hidden": False, "summary": bounded(_summary_text(name, value), INPUT_MAX)}


def tool_completed(tool_id: str | None, text: str, *, is_error: bool | None, hidden: bool) -> dict:
    """The `tool.completed` payload. A hidden call's output is never kept."""
    if hidden:
        return {"id": tool_id, "is_error": bool(is_error), "hidden": True, "preview": ""}
    text = text or ""
    if looks_binary(text):
        preview = "[binary output omitted]"
    else:
        preview = bounded(text, RESULT_MAX)
    return {"id": tool_id, "is_error": bool(is_error), "hidden": False, "preview": preview}


def bounded_text(text: str) -> str:
    """A whole text or thinking block, scrubbed and bounded for one event row."""
    return bounded(text, TEXT_EVENT_MAX)


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
#: A mask an earlier pass wrote in place (`[masked url-password, 1 chars]`).
_MASK = r"\[masked [a-z-]+, \d+ chars\]"
#: A URL's password in an approval: `handoff._URL_PASSWORD_RE`, reading a mask an earlier
#: pass wrote as one character of the password. Release's masking ends a password at its
#: first `@` (`https://alice:p@ss/…@host` masks `p`), and its mask holds spaces, which
#: `\S` cannot cross; read so, the head's pass still reaches the password's last `@`
#: (review r3 of #308, P1).
_APPROVAL_URL_PASSWORD_RE = re.compile(
    r"(?i)([a-z0-9+._-]+://[^\s/:]*:)"
    r"((?:" + _MASK + r"|(?!://)\S)*(?=@))(@)")
#: What a URL password's masking leaves in place: a mask an earlier pass wrote, and a
#: shell metacharacter, which an approval never hides (IR-20).
_PASSWORD_KEPT_RE = re.compile(_MASK + "|" + "|".join(re.escape(meta) for meta in _SHELL_META))


def mask_approval(request: Any) -> tuple[Any, list[dict]]:
    """`_head_mask_approval` after release/217's masking (`scrub_release.mask_approval`,
    frozen at `2c981cba5`), on what release leaves (C-23.14, C-27.1, #239 round 8). The
    head's pass only replaces a span with its mask, so it shows nothing release masks,
    and it reads every token shape release leaves unmasked as it would without release's
    pass; the spans are release's, then the head's."""
    from ..sessions import scrub_release
    released, spans = scrub_release.mask_approval(request)
    masked, more = _head_mask_approval(released)
    return masked, spans + more


def _head_mask_approval(request: Any) -> tuple[Any, list[dict]]:
    """Mask value-shaped secrets in an approval request, and nothing else.

    A person must see what they are allowing. Only token-shaped values (PEM
    blocks, JWTs, prefixed tokens, Bearer tokens, URL passwords) are masked,
    never by key name, never long base64, and never a span that contains
    command substitution, a pipe, a separator, a redirection or a newline.
    Each masked span is reported so the app can show it and offer a reveal.
    """
    from ..sessions.handoff import (
        _BEARER_RE, _JWT_RE, _PEM_RE, _PREFIXED_TOKEN_RE, _URL_SCHEME_RE, _bearer_refusal, _linear_sub,
    )
    spans: list[dict] = []

    def masked(value: str, rule: str, path: str) -> str:
        spans.append({"path": path, "rule": rule, "length": len(value),
                      "sha256": hashlib.sha256(value.encode()).hexdigest()})
        return f"[masked {rule}, {len(value)} chars]"

    def _masked_password(value: str, path: str) -> str:
        """A URL's password, to its last `@` (C-23.14): every piece between the masks an
        earlier pass wrote and the shell metacharacters masked, those kept. So every
        character of the password is masked but a shell metacharacter, which stays in
        view (IR-20), and text after the password up to that `@` (a host, a path, a
        query) is masked with it (review r3 of #308, P1)."""
        pieces, cursor = [], 0
        for kept in _PASSWORD_KEPT_RE.finditer(value):
            if kept.start() > cursor:
                pieces.append(masked(value[cursor:kept.start()], "url-password", path))
            pieces.append(kept.group())
            cursor = kept.end()
        if cursor < len(value):
            pieces.append(masked(value[cursor:], "url-password", path))
        return "".join(pieces)

    def url_passwords(text: str, path: str) -> str:
        """`text` with each URL's password masked (`_masked_password`): tried once from the
        start of each run of scheme characters that `://` follows, as `handoff._linear_sub`
        tries the head's URL rule, an empty password skipped."""
        parts, cursor = [], 0
        for token in _URL_SCHEME_RE.finditer(text):
            if token.start() < cursor or not text.startswith("://", token.end()):
                continue
            match = _APPROVAL_URL_PASSWORD_RE.match(text, token.start())
            if match is None or not match.group(2):
                continue
            parts.extend((text[cursor:match.start()],
                          match.group(1) + _masked_password(match.group(2), path) + match.group(3)))
            cursor = match.end()
        parts.append(text[cursor:])
        return "".join(parts)

    def mask_text(text: str, path: str) -> str:
        for rule, pattern in (("pem", _PEM_RE), ("jwt", _JWT_RE), ("token", _PREFIXED_TOKEN_RE),
                              ("bearer", _BEARER_RE)):
            def repl(match):
                value, rest = match.group(0), ""
                if rule == "bearer" and (cut := _bearer_refusal(match)) is not None:
                    # A credential key, as the scrubber reads one (C-23.14): masked
                    # only for the token before its credential word, if any.
                    if cut == match.start(1):
                        return match.group(0)
                    value, rest = match.string[match.start():cut], match.string[cut:match.end()]
                if any(meta in value for meta in _SHELL_META):
                    return match.group(0)
                return masked(value, rule, path) + rest
            text = _linear_sub(pattern, repl, text)
        return url_passwords(text, path)

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, str):
            return mask_text(value, path)
        if isinstance(value, dict):
            return {k: walk(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    return walk(request, "$"), spans
