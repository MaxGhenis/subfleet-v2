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

import bisect
import hashlib
import json
import math
import re
from typing import Any

from ..sessions.handoff import (
    _URL_SCHEME_RE, looks_binary, scrub_bounded, scrub_secrets, sensitive_tool_call,
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
#: A mask a pass wrote in place (`[masked url-password, 1 chars]`), with the number of
#: characters it stands for.
_MASK_RE = re.compile(r"\[masked [a-z-]+, (\d{1,12}) chars\]")
#: The shell's operators: unquoted, each ends a URL's authority in an approval.
_SHELL_OPERATORS = frozenset(";&|<>()\n")
#: What never goes inside a mask the head writes over a password: quote marks and backslashes.
_NEVER_MASKED = frozenset("'\"\\")
#: Where `_quoting` stops outside quotes, and inside double quotes.
_UNQUOTED_SPECIAL_RE = re.compile(r"['\"\\#$`<]")
_DOUBLE_QUOTED_SPECIAL_RE = re.compile(r'["\\$`]')
#: A parameter expansion that holds no quoting of its own (`${IFS}`).
_PLAIN_PARAMETER_RE = re.compile(r"\$\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[#?$!@*-])\}")
#: What a word follows: a `#` after one of these starts a comment.
_WORD_BREAK = frozenset(" \t\n;&|()<>")
#: `_quoting`'s kinds: unquoted, inside single quotes, inside double quotes, a quote mark that
#: opens or closes them, a comment, and unknown (from where the scanner stops following).
_U, _S, _D, _Q, _C, _X = b"usdqcx"


def _quoting(text: str) -> bytes:
    """How the shell quotes each character of `text`, one kind (`_U` to `_X`) per character
    (C-27.1, #308 fix r4). From where this scanner can't follow the shell, every character
    is `_X`, unknown: a quote left open, a heredoc or here-string (`<<`), a backtick, or a
    `$(`, `$'`, `$"` or `${…}` other than a plain name (`${IFS}`), each of which can hold
    quoting of its own. A backslash outside single quotes escapes the next character, and
    a `#` that starts a word starts a comment, to the line's end."""
    kinds = bytearray(b"u" * len(text))
    at = 0
    while (special := _UNQUOTED_SPECIAL_RE.search(text, at)) is not None:
        i, char = special.start(), special.group()
        if char == "'":
            close = text.find("'", i + 1)
            if close < 0:
                kinds[i:] = b"x" * (len(text) - i)
                break
            kinds[i + 1:close] = b"s" * (close - i - 1)
            kinds[i] = kinds[close] = _Q
            at = close + 1
        elif char == '"':
            close, stop = _double_quoted_end(text, i + 1)
            if stop > i:                               # inside the quotes up to `stop`
                kinds[i + 1:stop] = b"d" * (stop - i - 1)
                kinds[i] = _Q
            if close is None:
                kinds[stop:] = b"x" * (len(text) - stop)
                break
            kinds[close] = _Q
            at = close + 1
        elif char == "\\":
            at = i + 2
        elif char == "#" and (i == 0 or text[i - 1] in _WORD_BREAK):
            stop = text.find("\n", i)
            stop = len(text) if stop < 0 else stop
            kinds[i:stop] = b"c" * (stop - i)
            at = stop
        elif char == "$" and text.startswith(("$(", "${", "$'", '$"'), i):
            plain = _PLAIN_PARAMETER_RE.match(text, i)
            if plain is None:
                kinds[i:] = b"x" * (len(text) - i)
                break
            at = plain.end()
        elif char == "`" or text.startswith("<<", i):
            kinds[i:] = b"x" * (len(text) - i)
            break
        else:
            at = i + 1
    return bytes(kinds)


def _double_quoted_end(text: str, at: int) -> tuple[int | None, int]:
    """The closing quote of a double-quoted string whose text starts at `at`, and where the
    string is known to end: the closing quote; or None, and where the scanner stopped
    following it (a backtick, or a `$(` or `${…}` other than a plain name), or its opening
    quote if it is never closed."""
    opening = at - 1
    while (special := _DOUBLE_QUOTED_SPECIAL_RE.search(text, at)) is not None:
        i, char = special.start(), special.group()
        if char == '"':
            return i, i
        if char == "\\":
            at = i + 2
        elif char == "`":
            return None, i
        elif text.startswith(("$(", "${"), i):
            plain = _PLAIN_PARAMETER_RE.match(text, i)
            if plain is None:
                return None, i
            at = plain.end()
        else:
            at = i + 1
    return None, opening


def _url_password(text: str, start: int, kinds: bytes) -> list[int]:
    """The positions in `text` of the password of the URL whose authority starts at `start`
    (just after `://`), as a client is sent it (C-27.1): the authority ends at the first `/`,
    `?`, `#` or whitespace, an unquoted shell operator, or a `$`, backtick or backslash
    outside single quotes, and leaves out the quote marks; where the quoting is unknown
    (`_X`) every operator ends it and every quote mark is left out. The userinfo ends at
    the authority's last `@`, and the password is its text after the user's first `:`;
    with no `@` or `:` there is none."""
    authority = []
    for at in range(start, len(text)):
        char, kind = text[at], kinds[at]
        if char.isspace() or char in "/?#":
            break
        if kind == _Q or (kind == _X and char in "'\""):
            continue
        if char in "$`\\" and kind != _S:
            break
        if char in _SHELL_OPERATORS and kind not in (_S, _D):
            break
        authority.append(at)
    ats = [k for k, at in enumerate(authority) if text[at] == "@"]
    if not ats:
        return []
    userinfo = authority[:ats[-1]]
    colon = next((k for k, at in enumerate(userinfo) if text[at] == ":"), None)
    return [] if colon is None else userinfo[colon + 1:]


def _password_positions(text: str) -> list[int]:
    """The positions of every URL's password in `text` (`_url_password`), each URL tried once
    from each run of scheme characters that `://` follows, as `handoff._linear_sub` tries
    the head's URL rule."""
    if "://" not in text:
        return []
    kinds = _quoting(text)
    found: set[int] = set()
    for token in _URL_SCHEME_RE.finditer(text):
        if text.startswith("://", token.end()):
            found.update(_url_password(text, token.end() + 3, kinds))
    return sorted(found)


def _aligned(source: str, shown: str) -> list[tuple[int, int, int]] | None:
    """`shown` read as `source` with masks in place: its literal stretches, each (source
    start, shown start, length), where a mask (`_MASK_RE`) stands for its count of
    `source`'s characters and every other character for itself; None where `shown` is not
    that. A mask-shaped text that `source` holds at that point is `source`'s own text: no
    pass masks a run that starts with `[` and then reads as a mask (#308 fix r4)."""
    stretches, i, j = [], 0, 0
    for mask in _MASK_RE.finditer(shown):
        start, end = mask.span()
        at = j + start - i
        if source.startswith(mask.group(), at):
            continue
        stretches.append((j, i, start - i))
        i, j = end, at + int(mask.group(1))
    stretches.append((j, i, len(shown) - i))
    if j + len(shown) - i != len(source) or not all(
            source.startswith(shown[at:at + length], origin) for origin, at, length in stretches):
        return None
    return [stretch for stretch in stretches if stretch[2]]


def _through(stretches: list[tuple[int, int, int]], position: int) -> int | None:
    """Where `position` of the source shows (`_aligned`), or None inside a mask."""
    k = bisect.bisect_right(stretches, (position, math.inf, math.inf)) - 1
    if k < 0:
        return None
    origin, at, length = stretches[k]
    return at + position - origin if position < origin + length else None


def _shown_at(original: str, released: str, shown: str):
    """A function from a position of `original` to where `shown` (release's output
    `released` after the head's token rules) shows it, or None inside a mask; None where
    either text can't be aligned with the one before it."""
    first, second = _aligned(original, released), _aligned(released, shown)
    if first is None or second is None:
        return None
    return lambda position: (None if (middle := _through(first, position)) is None
                             else _through(second, middle))


def mask_approval(request: Any) -> tuple[Any, list[dict]]:
    """`_head_mask_approval` after release/217's masking (`scrub_release.mask_approval`,
    frozen at `2c981cba5`), on what release leaves (C-23.14, C-27.1, #239 round 8). The
    head's pass only replaces a span with its mask, so it shows nothing release masks,
    and it reads every token shape release leaves unmasked as it would without release's
    pass. It finds each URL's password on `request` itself, never on release's output,
    whose frozen rule can mask a character that ends the authority (#308 fix r4). The
    spans are release's, then the head's."""
    from ..sessions import scrub_release
    released, spans = scrub_release.mask_approval(request)
    masked, more = _head_mask_approval(request, released)
    return masked, spans + more


def _head_mask_approval(request: Any, released: Any = None) -> tuple[Any, list[dict]]:
    """Mask value-shaped secrets in an approval request, and nothing else.

    A person must see what they are allowing (C-27.1). Only token-shaped values (PEM
    blocks, JWTs, prefixed tokens, Bearer tokens, URL passwords) are masked, never by key
    name and never long base64. A PEM block, JWT, token or Bearer token that contains
    command substitution, a pipe, a separator, a redirection or a newline is not masked.

    A URL's password is the one a client is sent (`_url_password`), found on `request` as
    submitted: shell operators inside quotes are password characters, and only the
    password is masked, so its user, host, port, path and query, and every command and
    operator around it, show. Where C-27.1 and secrecy conflict, C-27.1 wins (#308 fix
    r4). `released` is release's output of `request` (`mask_approval`); with it, the head
    masks each run of the password's characters that release's masks and its own token
    rules leave in view, and no quote mark or backslash. Alone (`released` None) the head
    masks `request` itself. Each masked span is reported so the app can show it and offer
    a reveal; its length and SHA-256 describe the request's characters.
    """
    from ..sessions.handoff import (
        _BEARER_RE, _JWT_RE, _PEM_RE, _PREFIXED_TOKEN_RE, _bearer_refusal, _linear_sub,
    )
    spans: list[dict] = []

    def masked(value: str, rule: str, path: str) -> str:
        spans.append({"path": path, "rule": rule, "length": len(value),
                      "sha256": hashlib.sha256(value.encode()).hexdigest()})
        return f"[masked {rule}, {len(value)} chars]"

    def url_passwords(original: str, released: str, shown: str, path: str) -> str:
        """`shown` with each run of `original`'s URL passwords that it shows masked."""
        found = [at for at in _password_positions(original) if original[at] not in _NEVER_MASKED]
        if not found:
            return shown
        where = _shown_at(original, released, shown)
        if where is None:
            # Never expected (`_aligned`): fail closed, the whole string masked.
            return masked(original, "url-password", path)
        runs: list[list[int]] = []                     # [shown start, shown end, original start]
        for at in found:
            to = where(at)
            if to is None:
                continue
            if runs and runs[-1][1] == to and runs[-1][2] + to - runs[-1][0] == at:
                runs[-1][1] = to + 1
            else:
                runs.append([to, to + 1, at])
        parts, cursor = [], 0
        for start, end, origin in runs:
            parts += [shown[cursor:start], masked(original[origin:origin + end - start], "url-password", path)]
            cursor = end
        parts.append(shown[cursor:])
        return "".join(parts)

    def mask_text(text: str, original: str | None, path: str) -> str:
        if original is None:
            # `released` isn't `request`'s shape (never expected): fail closed.
            return masked(text, "url-password", path)
        released_text = text
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
        return url_passwords(original, released_text, text, path)

    def walk(value: Any, original: Any, path: str) -> Any:
        if isinstance(value, str):
            return mask_text(value, original if isinstance(original, str) else None, path)
        if isinstance(value, dict):
            return {k: walk(v, original.get(k) if isinstance(original, dict) else None, f"{path}.{k}")
                    for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, original[i] if isinstance(original, list) and i < len(original) else None,
                         f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    return walk(request if released is None else released, request, "$"), spans
