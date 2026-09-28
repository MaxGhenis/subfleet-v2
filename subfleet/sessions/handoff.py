"""Bounded, credential-scrubbed handoffs from a Claude session to a fresh agent.

Handoff is the default recovery of a cold session (plan decision 7): rather than
starting a second writer inside a conversation the desktop app may restart under
us, it builds a brief and dispatches it as an ordinary job. The source transcript
stays the durable record; the brief is a bounded excerpt that points at it
(C-23.36).

What a handoff may carry is C-23.14, and it is a safety rule, not a formatting
one. Three separate mechanisms, in this order:

1. **Suppression by pattern.** The result of any tool call whose *input* matches
   a credential-reading pattern — `agent-secret get`, a keychain read, `env` or
   `printenv`, `auth.json`, `.env`, a credentials file — is omitted wholesale,
   and so is the input. This runs before any redaction, so a secret that no regex
   would recognise never reaches the excerpt at all.
2. **Scrubbing by value.** Private keys, JWTs, prefixed API tokens, `Bearer`
   values, `authorization`/`cookie` headers, URL passwords and
   `key = "value"`-shaped assignments are replaced; encoded binary (data URIs,
   long base64 runs, non-printable output) is omitted.
3. **Retention.** Everything else is kept verbatim. Ordinary code, commands and
   tool output are the entire value of a handoff; a lossy rewrite would destroy
   the continuity the brief exists to carry.

Every section is bounded by an explicit character cap from
`policy.json`'s `sessions.handoff_caps` (C-23.36), and the whole brief is
scrubbed once more after assembly so nothing a section boundary spliced together
escapes.

Dispatch is one `subfleet run` submission (C-23.54), detached, with the caller's
session recorded so the completion notice comes back to the session that asked.
The prompt is the job's own `jobs/<job id>/prompt.md` at mode 0600 inside the
0700 state root — it is written once and retained as the immutable record rather
than unlinked, because it no longer sits in the system temp directory (ledger row
212's replacement).

Ported from v1 `subfleet/handoff.py`; the regexes and the section order are its.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..contracts import SCRUB_MAX_CHARS, Sandbox
from ..protocol import SubmitArgs
from . import registry, transcripts

REDACTED = "[REDACTED]"
OMITTED_BINARY = "[binary/base64 tool result omitted]"
OMITTED_SENSITIVE = "[credential-reading tool result omitted]"
OMITTED_SENSITIVE_INPUT = "[credential-reading input omitted]"
OMITTED_UNMATCHED = "[tool result omitted because its input is outside this excerpt]"

MAX_JSON_LINE_CHARS = 4 * 1024 * 1024
FULL_SCAN_BYTES = 64 * 1024 * 1024
LAST_SCAN_BYTES = 2 * 1024 * 1024
PROGRESS_READ_BYTES = 128 * 1024
# Bound work before any content matching (C-23.14). A longer text is matched as
# two excerpts within the same bound: a head of at most EXCERPT_CHARS, and a tail
# of at most EXCERPT_CHARS - EXCERPT_LOOKBEHIND, before which EXCERPT_LOOKBEHIND
# characters are searched only for private-key and reminder delimiters.
MAX_SCRUB_CHARS = SCRUB_MAX_CHARS
EXCERPT_CHARS = MAX_SCRUB_CHARS // 2
EXCERPT_LOOKBEHIND = 16 * 1024

# --- the scrub list (C-23.14) -------------------------------------------------

_PEM_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)
_PEM_START_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----")
_PEM_END_RE = re.compile(r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_PREFIXED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|"
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"npm_[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{16,}"
    r")(?![A-Za-z0-9_-])"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*")
_DATA_URI_RE = re.compile(
    r"(?i)data:[a-z0-9.+/-]+(?:;[a-z0-9=.+/-]+)*;base64,[A-Za-z0-9+/=\s]{32,}"
)
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{160,}={0,2})(?![A-Za-z0-9+/])"
)
_URL_PASSWORD_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)")
_URL_SCHEME_RE = re.compile(r"[a-z0-9+.-]+", re.IGNORECASE)
_URL_START_RE = re.compile(r"\b[a-z]", re.IGNORECASE)
_HEADER_RE = re.compile(r"(?im)^([^\S\n]*(?:authorization|cookie|set-cookie)\s*:\s*).+$")
_SENSITIVE_WORD = (
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|"
    r"credential|credentials|private[_-]?key|signing[_-]?key|"
    r"secret[_-]?access[_-]?key|access[_-]?key[_-]?id|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|oauth[_-]?token|auth[_-]?token)"
)
_SENSITIVE_KEY = (
    rf"(?:{_SENSITIVE_WORD}|"
    r"(?:[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)[_-](?:api[_-]?key|token|secret|"
    r"password|passwd|private[_-]?key|signing[_-]?key))"
)
# Consume each identifier once, including ordinary long names such as a_a_a_.
# Searching the old optional-prefix expression from every character retried all
# suffixes of such names. The longest sensitive word is only 17 characters.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]+", re.IGNORECASE)
_SENSITIVE_SUFFIX_RE = re.compile(_SENSITIVE_WORD + r"\Z", re.IGNORECASE)
_SENSITIVE_FLAG_RE = re.compile(_SENSITIVE_KEY, re.IGNORECASE)
_ASSIGN_SEPARATOR_RE = re.compile(r"[\"']?\s*[:=]\s*")
_PLAIN_VALUE_RE = re.compile(r"[^\s,;\"']+")
_FLAG_VALUE_RE = re.compile(r"[ \t]+(?P<value>[^\s-][^\s]*)")
_QUOTE_EVENT_RE = re.compile(r"\\[^\n]|[\"'\r\n]")
_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE
)
_REMINDER_START_RE = re.compile(r"<system-reminder>", re.IGNORECASE)
_REMINDER_END_RE = re.compile(r"</system-reminder>", re.IGNORECASE)

#: Tool calls whose RESULT is omitted by pattern rather than redacted (C-23.14).
#: Suppression beats redaction here because the value a keychain read returns has
#: no shape a regex can rely on.
#: `MULTILINE`, the leading `^\s*`, and `_tool_corpus` below are v2's, and they
#: close a hole v1 had. v1 matched these against `json.dumps` of the tool input,
#: which turns a real newline into the two characters `\` and `n` — so an `env`
#: on the SECOND line of a Bash command sat behind neither `^` (the rendering
#: starts with `{`) nor a `;&|` separator, and escaped suppression entirely.
#: A multi-line script that dumps the environment is not an exotic input.
_TOOL_FLAGS = re.IGNORECASE | re.DOTALL | re.MULTILINE
#: `env` itself, bare or by path, where a command word ends.
_ENV_COMMAND = r"(?:[\w.~-]*/)*env(?=[\s;&|)\"'`]|$)"
_SENSITIVE_TOOL_PATTERNS = tuple(
    re.compile(pattern, _TOOL_FLAGS)
    for pattern in (
        r"\bagent-secret\s+(?:get|show)\b",
        r"\bsecurity\s+(?:dump-keychain|find-generic-password|find-internet-password)\b",
        # `printenv` does nothing but print the environment: any mention of the
        # command, bare or by path (`/usr/bin/printenv`), inside an executor's
        # input (`tools.exec_command({cmd: "printenv"})`) or a wrapper's.
        # A slash already supplies the basename's left boundary. Matching the
        # optional path here would retry every suffix of a long ordinary path.
        r"(?<![\w.-])printenv(?![\w.-])",
        # `env` is also a word, so only where a command starts: a line, after a
        # separator or backquote, after a wrapper (`sudo -E`, `nice`, `xargs`:
        # `_env_after_wrapper`), inside `sh -c '...'`, or as a `command`/`cmd`
        # value; bare or by path.
        r"(?:^[ \t]*|[;&|(`]\s*"
        r"|[\"'`](?:command|cmd)[\"'`]?\s*:\s*[\"'`]|\bcmd\s*:\s*[\"'`]|\s-l?c\s+[\"'])"
        + _ENV_COMMAND,
        # A `.env` file, also when a separator follows it (`cat .env; true`).
        r"(?:auth\.json|credentials(?:\.json)?|(?:^|[/\s\"'`=<(])\.env"
        r"(?:\.[A-Za-z0-9_-]+)?(?=[\s\"'`;&|)<>]|$))",
    )
)
_ENV_WRAPPER_RE = re.compile(r"\b(?:sudo|doas|nice|nohup|time|command|exec|xargs)\s+", _TOOL_FLAGS)
_ENV_COMMAND_RE = re.compile(_ENV_COMMAND, _TOOL_FLAGS)
_FLAG_ARG_RE = re.compile(r"-\S+\s+", _TOOL_FLAGS)


def _env_after_wrapper(corpus: str) -> bool:
    """`env` after a wrapper and its flags (`sudo -E -H env`), in one pass.

    This is what `\\b(?:sudo|…)\\s+(?:-\\S+\\s+)*` followed by `_ENV_COMMAND`
    matched as one pattern, which searched a run of flags again from every
    wrapper word inside it (`-time -time …`), quadratic in the run. A run is
    walked once, by the first wrapper before it: a wrapper word within the run
    would walk the rest of the same run to the same end.
    """
    cursor = 0
    for wrapper in _ENV_WRAPPER_RE.finditer(corpus):
        if wrapper.start() < cursor:
            continue
        at = wrapper.end()
        while not _ENV_COMMAND_RE.match(corpus, at):
            flag = _FLAG_ARG_RE.match(corpus, at)
            if flag is None:
                break
            at = flag.end()
        else:
            return True
        cursor = at
    return False


class HandoffError(ValueError):
    """A user-facing handoff selection or source error.

    Exit 2 (invalid input) by default; a refusal — a request naming something
    the contract forbids continuing — carries 7 instead (C-17.3).
    """

    code = 2

    def __init__(self, message: str, code: int | None = None,
                 fix: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.fix = fix


#: What `scrub_secrets` puts in place of what it removes.
_PLACEHOLDERS = (REDACTED, "[PRIVATE KEY REDACTED]", "[BASE64 DATA OMITTED]", "[BASE64 OMITTED]")
#: A run that looks like a secret value rather than a word or a key name: six or
#: more of `[A-Za-z0-9_-]`, with a letter and a digit.
_VALUE_RUN_RE = re.compile(r"[\w-]{6,}")


def _value_runs(text: str) -> set[str]:
    # The old unanchored lookaheads also retried every suffix of ordinary names.
    return {run for run in _VALUE_RUN_RE.findall(text)
            if any(ch.isdecimal() for ch in run)
            and any("a" <= ch <= "z" or "A" <= ch <= "Z" for ch in run)}


def _values_removed(matched: str, out: str) -> int:
    """How many secret-looking values a replacement removed that no earlier rule
    had: the runs of `matched`, placeholders aside, that `out` no longer holds."""
    for mark in _PLACEHOLDERS:
        matched = matched.replace(mark, " ")
    return len(_value_runs(matched) - _value_runs(out))


def _linear_sub(pattern: re.Pattern, replacement, text: str) -> str:
    """Avoid retrying an unmatched delimiter or scheme from every suffix.

    Keep genuine regex matches for the approval masker's grouped replacements.
    Once an end delimiter exists, the original anchored match scans its block
    once; without an end, no later opener can match either.
    """
    if pattern not in (_PEM_RE, _SYSTEM_REMINDER_RE, _URL_PASSWORD_RE):
        return pattern.sub(replacement, text)

    def matches():
        if pattern is _URL_PASSWORD_RE:
            for token in _URL_SCHEME_RE.finditer(text):
                start = _URL_START_RE.search(text, token.start(), token.end())
                if start is not None:
                    match = pattern.match(text, start.start())
                    if match is not None:
                        yield match
            return
        opening, closing = ((_PEM_START_RE, _PEM_END_RE) if pattern is _PEM_RE
                            else (_REMINDER_START_RE, _REMINDER_END_RE))
        cursor = 0
        while (start := opening.search(text, cursor)) is not None:
            end = closing.search(text, start.end())
            if end is None:
                return
            match = pattern.match(text, start.start())
            assert match is not None
            yield match
            cursor = match.end()

    parts: list[str] = []
    cursor = 0
    for match in matches():
        if match.start() < cursor:
            continue
        out = replacement(match) if callable(replacement) else match.expand(replacement)
        parts.extend((text[cursor:match.start()], out))
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _quoted_ends(text: str) -> dict[int, int]:
    """Index matching quotes once, respecting escapes and line boundaries.

    A failed quoted assignment must not rescan the rest of its line for each
    later candidate. Unescaped quotes can close the previous quote of their
    own kind; escaped characters consume both characters, as the old rule did.
    """
    ends: dict[int, int] = {}
    previous: dict[str, int] = {}
    for match in _QUOTE_EVENT_RE.finditer(text):
        char = match.group()
        if len(char) > 1:
            continue
        if char in "\r\n":
            previous.clear()
        else:
            if char in previous:
                ends[previous[char]] = match.start()
            previous[char] = match.start()
    return ends


def _scrub_named_values(text: str, replace: Callable[[str, str], str],
                       *, quoted: bool = False, flags: bool = False) -> str:
    """Replace assignment/flag values in linear scans, preserving rule order."""
    parts: list[str] = []
    cursor = 0
    quote_ends: dict[int, int] | None = None
    for token in _IDENTIFIER_RE.finditer(text):
        if token.start() < cursor:
            continue
        key = token.group()
        if not _SENSITIVE_SUFFIX_RE.search(key[-17:]):
            continue
        start = token.start()
        if flags:
            flag_start = key.rfind("--")
            if flag_start < 0 or not _SENSITIVE_FLAG_RE.fullmatch(key[flag_start + 2:]):
                continue
            start += flag_start
            value = _FLAG_VALUE_RE.match(text, token.end())
            if value is None:
                continue
            value_start, end = value.start("value"), value.end()
            out = text[start:value_start] + REDACTED
        else:
            separator = _ASSIGN_SEPARATOR_RE.match(text, token.end())
            if separator is None:
                continue
            if start > cursor and text[start - 1] in "\"'":
                start -= 1
            value_start = separator.end()
            if quoted:
                if value_start == len(text) or text[value_start] not in "\"'":
                    continue
                if quote_ends is None:
                    quote_ends = _quoted_ends(text)
                closing = quote_ends.get(value_start)
                if closing is None:
                    continue
                end = closing + 1
                out = text[start:value_start + 1] + REDACTED + text[closing]
            else:
                value = _PLAIN_VALUE_RE.match(text, value_start)
                if value is None:
                    continue
                end = value.end()
                out = text[start:value_start] + REDACTED
        parts.extend((text[cursor:start], replace(text[start:end], out)))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def scrub_secrets(text: str, *, strip_reminders: bool = False, whole: bool = False) -> tuple[str, int]:
    """Remove credential values and encoded binary; retain ordinary text. The
    count is of values replaced: one credential two rules match counts once, and
    a header that held several values (`Cookie: a=...; b=...`) counts each.

    A text longer than MAX_SCRUB_CHARS is matched only as a head and a tail
    excerpt with a marker between them (`scrub_bounded`). `whole` matches all of
    it, for a text whose length its caller bounds: the assembled handoff brief,
    whose section caps policy keeps within the same bound (C-23.36)."""
    if len(text) > MAX_SCRUB_CHARS and not whole:
        return scrub_bounded(text, None, strip_reminders=strip_reminders)
    return _scrub(text, strip_reminders)


def scrub_bounded(text: str, limit: int | None, *, strip_reminders: bool = False) -> tuple[str, int]:
    """`truncate(scrub_secrets(text), limit)`: scrubbed, then bounded to `limit`
    characters keeping the head and the tail (C-23.14, C-23.36, C-25.5); with
    `limit` None, not bounded further.

    Within MAX_SCRUB_CHARS that is exactly what it does. A longer text is never
    matched whole: its head (`_head_excerpt`) and its tail (`_tail_excerpt`) are
    scrubbed on their own, within the same work, and joined by a marker giving
    the characters left out. The excerpts are cut at line boundaries and give up
    anything the cut could have separated from what made it a credential, so a
    text whose first or last line is longer than an excerpt keeps nothing of it.
    """
    if len(text) <= MAX_SCRUB_CHARS:
        scrubbed, count = _scrub(text, strip_reminders)
        return (scrubbed if limit is None else truncate(scrubbed, limit)), count
    head, head_count = _head_excerpt(text, strip_reminders)
    tail, tail_count = _tail_excerpt(text, strip_reminders)
    head, tail = head.strip(), tail.strip()
    if limit is not None:
        usable = limit - len(f"\n… [{len(text):,} characters omitted] …\n")   # the longest marker
        if usable <= 0:
            return (ELIDED[:limit] if limit < len(ELIDED) else ELIDED), head_count + tail_count
        head = head[:int(usable * 0.6)].rstrip()
        room = usable - len(head)
        tail = tail[max(0, len(tail) - room):].lstrip() if room > 0 else ""
    marker = f"\n… [{max(0, len(text) - len(head) - len(tail)):,} characters omitted] …\n"
    return (head + marker + tail).strip(), head_count + tail_count


def _unclosed(opening: re.Pattern, closing: re.Pattern, text: str) -> int:
    """Where the first block `text` leaves open starts (`len(text)` if none): the
    first opener after the last closer. Every opener before that closer has a
    closer after it, so the block it opens ends within `text`."""
    last = None
    for last in closing.finditer(text):
        pass
    opener = opening.search(text, last.end() if last is not None else 0)
    return opener.start() if opener is not None else len(text)


def _head_excerpt(text: str, strip_reminders: bool) -> tuple[str, int]:
    """The scrubbed head of a text over MAX_SCRUB_CHARS: its whole lines within
    EXCERPT_CHARS. A line the cut would split is left out, so no value is cut
    from its end; a reminder the cut leaves open is left out from its start, and
    a private key from its armour line (a placeholder in its place)."""
    head = text[:EXCERPT_CHARS]
    head = head[:head.rfind("\n") + 1]
    count = 0
    if strip_reminders:
        head = _linear_sub(_SYSTEM_REMINDER_RE, "", head)
        head = head[:_unclosed(_REMINDER_START_RE, _REMINDER_END_RE, head)]
    key = _unclosed(_PEM_START_RE, _PEM_END_RE, head)
    if key < len(head):
        head, count = head[:key] + "[PRIVATE KEY REDACTED]", 1
    scrubbed, found = _scrub(head, False)     # its reminders are already gone
    return scrubbed, count + found


#: What begins a scrubbed tail excerpt that may be the value of a key, header or
#: `Bearer` above the cut: blank lines, a `:` or `=`, and the rest of that line
#: (`password =` / `  hunter2`, `Authorization:` / `  token`).
_LEADING_VALUE_RE = re.compile(r"\s*(?:[:=]\s*)?[^\n]*\n?")
#: A data URI whose encoded run reaches the end of the lookbehind, and that run.
_OPEN_DATA_URI_RE = re.compile(r"(?i)data:[a-z0-9.+/-]+(?:;[a-z0-9=.+/-]+)*;base64,[A-Za-z0-9+/=\s]*\Z")
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/=\s]*")


def _open_block_end(opening: re.Pattern, closing: re.Pattern, behind: str, tail: str) -> int:
    """Where a block open at the start of `tail` ends in it (0 if none is). The
    lookbehind decides: its last delimiter an opener means open. With neither
    delimiter in it, a closer in `tail` before any opener ends a block opened
    earlier. With no closer in `tail`, nothing there was matched as a block."""
    closer = closing.search(tail)
    if closer is None:
        return 0
    last_open = last_close = -1
    for match in opening.finditer(behind):
        last_open = match.start()
    for match in closing.finditer(behind):
        last_close = match.start()
    if last_open > last_close:
        return closer.end()
    if last_open < 0 and last_close < 0 and opening.search(tail, 0, closer.start()) is None:
        return closer.end()
    return 0


def _tail_excerpt(text: str, strip_reminders: bool) -> tuple[str, int]:
    """The scrubbed tail of a text over MAX_SCRUB_CHARS: its whole lines within
    the last EXCERPT_CHARS - EXCERPT_LOOKBEHIND. What a block open where it
    starts would have removed goes first: a reminder or private key up to its
    closer, a data URI's encoded run. Once scrubbed, it gives up its first
    nonblank line (after a `:` or `=`): a value whose name is above the cut
    (`password =` / `hunter2`) is nothing a scrub of the tail alone can see."""
    start = len(text) - (EXCERPT_CHARS - EXCERPT_LOOKBEHIND)
    newline = text.find("\n", start - 1)
    if newline < 0:
        return "", 0
    start = newline + 1
    behind, tail = text[max(0, start - EXCERPT_LOOKBEHIND):start], text[start:]
    cut = count = 0
    if strip_reminders:
        cut = _open_block_end(_REMINDER_START_RE, _REMINDER_END_RE, behind, tail)
    key = _open_block_end(_PEM_START_RE, _PEM_END_RE, behind, tail)
    if key > cut:
        cut, count = key, 1
    if cut == 0 and _OPEN_DATA_URI_RE.search(behind):
        cut = _BASE64_RUN_RE.match(tail).end()
    scrubbed, found = _scrub(tail[cut:], strip_reminders)
    return scrubbed[_LEADING_VALUE_RE.match(scrubbed).end():], count + found


def _scrub(text: str, strip_reminders: bool) -> tuple[str, int]:
    """`scrub_secrets` on a text within its work bound."""
    if strip_reminders:
        text = _linear_sub(_SYSTEM_REMINDER_RE, "", text)
    total = 0

    def replace_value(matched: str, out: str, *, header: bool = False) -> str:
        nonlocal total
        if out == matched:
            return out
        # Count a credential matched by an earlier rule only once.
        earlier = any(mark in matched for mark in _PLACEHOLDERS)
        if header:
            total += _values_removed(matched, out) or (0 if earlier else 1)
        else:
            total += 1 if not earlier else min(1, _values_removed(matched, out))
        return out

    for pattern, replacement in (
        (_PEM_RE, "[PRIVATE KEY REDACTED]"),
        (_DATA_URI_RE, "[BASE64 DATA OMITTED]"),
        (_JWT_RE, REDACTED),
        (_PREFIXED_TOKEN_RE, REDACTED),
        (_BEARER_RE, "Bearer " + REDACTED),
        (_LONG_BASE64_RE, "[BASE64 OMITTED]"),
        (_HEADER_RE, lambda match: match.group(1) + REDACTED),
        (_URL_PASSWORD_RE, lambda match: match.group(1) + REDACTED + match.group(3)),
    ):
        def replace(match, replacement=replacement, pattern=pattern):
            matched = match.group(0)
            out = replacement(match) if callable(replacement) else match.expand(replacement)
            return replace_value(matched, out, header=pattern is _HEADER_RE)

        text = _linear_sub(pattern, replace, text)
    text = _scrub_named_values(text, replace_value, quoted=True)
    text = _scrub_named_values(text, replace_value)
    text = _scrub_named_values(text, replace_value, flags=True)
    return text, total


#: What a section becomes when the budget left for it is smaller than the marker
#: that would explain the truncation. Carried in full so a reader knows the
#: excerpt stops here rather than that the work did.
ELIDED = "… [omitted] …"


def truncate(text: str, limit: int) -> str:
    """C-23.36: bound a section, keeping its head and its tail and saying so.

    The bound is hard. v1 computed `usable = max(0, limit - len(marker))` and
    then sliced `text[-(usable - head):]`, which for `usable == 0` is
    `text[-0:]` — the WHOLE string. So a section whose remaining allowance was
    smaller than the marker came back complete, and the caller's budget
    accounting then subtracted a number far larger than it had. A tool result
    landing on the last few characters of `recent` could carry the entire file.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    marker = f"\n… [{len(text) - limit:,} characters omitted] …\n"
    usable = limit - len(marker)
    if usable <= 0:
        # No room to explain the truncation: say only that there was one.
        return ELIDED[:limit] if limit < len(ELIDED) else ELIDED
    head = int(usable * 0.6)
    tail = usable - head
    return text[:head].rstrip() + marker + (text[-tail:].lstrip() if tail else "")


def clean(text: str, limit: int) -> tuple[str, int]:
    return scrub_bounded(text, limit, strip_reminders=True)


def _parse(line: str) -> dict[str, Any] | None:
    if len(line) > MAX_JSON_LINE_CHARS:
        return None
    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _synthetic(text: str) -> bool:
    """The app's stubs and subfleet's own nudges are not conversation."""
    stripped = text.strip()
    return bool(
        stripped == transcripts.RESUME_STUB_USER
        or stripped == transcripts.RESUME_STUB_ASSISTANT
        or stripped.startswith("[Request interrupted by user")
        or transcripts.MARKER in stripped[:400]
        or transcripts.MUSTER_MARKER in stripped[:400]
    )


def _tool_corpus(value: Any) -> str:
    """What the credential-reading patterns are matched against.

    Two renderings, because neither alone is enough. The JSON one carries the
    key names (`"command":`) and any shape that is not a string. The strings'
    own text carries the LINE STRUCTURE that JSON escaping destroys — and a
    command whose second line is `env` is invisible in the first and obvious in
    the second.
    """
    strings: list[str] = []

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 8:                       # a self-referential input is a bug,
            return                          # not a reason to recurse forever
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                strings.append(str(key))
                walk(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, depth + 1)

    walk(value)
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return "\n".join([rendered, *strings])


def sensitive_tool_call(name: str, value: Any) -> bool:
    """C-23.14: does this tool call read a credential?"""
    if "agent-secret" in name.casefold() or "keychain" in name.casefold():
        return True
    # Every pattern, and the wrapper walk, is linear in the input, so an input of
    # any size is classified by what it holds: a large `Write` is not a
    # credential read, and a large script that runs `env` is one.
    corpus = _tool_corpus(value)
    return (any(pattern.search(corpus) for pattern in _SENSITIVE_TOOL_PATTERNS)
            or _env_after_wrapper(corpus))


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "") if content.get("type") == "text" else ""
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content
                         if isinstance(item, dict) and item.get("type") == "text").strip()
    return ""


def _tool_input_text(name: str, value: Any) -> str:
    """Readable input context without committing to any provider's tool schema."""
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and (name.casefold() in {"bash", "shell", "shell_command"}
                                         or "exec" in name.casefold()):
            return command
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def looks_binary(text: str) -> bool:
    if "\x00" in text:
        return True
    sample = text[:16_384]
    if not sample:
        return False
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample)
    return printable / len(sample) < 0.85


def first_task(path: Path, cap: int) -> tuple[str, str | None, int]:
    """The session's original instruction: the first real human turn, looked for
    in the transcript's first `FULL_SCAN_BYTES` (it had read a file of any size)."""
    try:
        stream = transcripts.open_regular(path)
    except OSError as exc:
        raise HandoffError(f"cannot read transcript {path}: {exc}") from exc
    with stream:
        for raw in transcripts.capped_lines(stream, FULL_SCAN_BYTES):
            entry = _parse(raw.decode("utf-8", "replace"))
            if not transcripts.is_main(entry) or entry.get("type") != "user":
                continue
            origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
            if origin.get("kind") in {"task-notification", "peer"}:
                continue
            text = transcripts.text_of(transcripts.blocks(entry.get("message")))
            if not text.strip() or _synthetic(text):
                continue
            cleaned, redactions = clean(text, cap)
            return cleaned, entry.get("uuid"), redactions
    raise HandoffError(f"no user task text found in transcript {path}")


def _reverse_main_entries(path: Path, limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in transcripts.lines_reversed(path, max_bytes=FULL_SCAN_BYTES):
        entry = _parse(line)
        if not transcripts.is_main(entry):
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    entries.reverse()
    return entries


def recent_excerpt(path: Path, first_uuid: str | None,
                   caps: dict[str, int]) -> tuple[str, int]:
    """The bounded main-chain tail: text, tool inputs, and tool results.

    Tool inputs and results have their own total budgets on top of the section
    cap, so one enormous file read cannot crowd out the conversation that
    explains it. Segments are selected newest-first and re-ordered, so a brief
    that must drop something drops the oldest.
    """
    entries = _reverse_main_entries(path, caps["recent_records"])
    tools: dict[str, tuple[str, bool]] = {}
    segments: list[tuple[str, int, str | None]] = []

    def add(label: str, body: str, *, tool_kind: str | None = None,
            limit: int = 8_000) -> None:
        if not body or _synthetic(body):
            return
        cleaned, redactions = clean(body, limit)
        if cleaned:
            segments.append((f"{label}\n{cleaned}", redactions, tool_kind))

    for entry in entries:
        role = "Claude user:" if entry.get("type") == "user" else "Claude assistant:"
        blocks = transcripts.blocks(entry.get("message"))
        text = transcripts.text_of(blocks)
        if text and entry.get("uuid") != first_uuid:
            add(role, text)
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "tool")
                sensitive = sensitive_tool_call(name, block.get("input"))
                tools[tool_id] = (name, sensitive)
                if sensitive:
                    segments.append((f"Claude tool call ({name}):\n"
                                     f"{OMITTED_SENSITIVE_INPUT}", 1, "input"))
                else:
                    add(f"Claude tool call ({name}):",
                        _tool_input_text(name, block.get("input")),
                        tool_kind="input", limit=caps["tool_input"])
            elif kind == "tool_result":
                matched = tools.get(str(block.get("tool_use_id") or ""))
                if matched is None:
                    # Its input is outside this excerpt, so its sensitivity is
                    # unknown; unknown means omitted.
                    segments.append((f"Claude tool result:\n{OMITTED_UNMATCHED}", 1, "result"))
                    continue
                name, sensitive = matched
                if sensitive:
                    segments.append((f"Claude tool result ({name}):\n"
                                     f"{OMITTED_SENSITIVE}", 1, "result"))
                    continue
                result = _tool_result_text(block)
                if not result:
                    continue
                if looks_binary(result):
                    segments.append((f"Claude tool result ({name}):\n"
                                     f"{OMITTED_BINARY}", 1, "result"))
                    continue
                add(f"Claude tool result ({name}):", result,
                    tool_kind="result", limit=caps["tool_result"])

    return select_segments(segments, caps)


def select_segments(segments: list[tuple[str, int, str | None]],
                    caps: dict[str, int]) -> tuple[str, int]:
    """C-23.36: fit `(text, redactions, tool kind)` segments into the `recent` cap.

    Newest first, so a brief that must drop something drops the oldest; tool
    inputs and results also draw on their own totals. Shared by the Claude
    transcript reader above and the Codex rollout reader
    (`subfleet/conversations/codex_brief.py`), so both are bounded alike.
    """
    chosen: list[tuple[str, int, str | None]] = []
    remaining = caps["recent"]
    budgets = {"input": caps["tool_inputs_total"], "result": caps["tool_results_total"]}
    for text, redactions, tool_kind in reversed(segments):
        separator = 2 if chosen else 0
        allowance = remaining - separator
        if tool_kind:
            allowance = min(allowance, budgets[tool_kind])
        if allowance <= 0:
            continue
        selected = truncate(text, allowance)
        if not selected:
            continue
        chosen.append((selected, redactions, tool_kind))
        consumed = len(selected)
        remaining -= consumed + separator
        if tool_kind:
            budgets[tool_kind] -= consumed
        if remaining <= 0:
            break
    chosen.reverse()
    return "\n\n".join(item[0] for item in chosen), sum(item[1] for item in chosen)


def _read_bounded(path: Path, max_bytes: int = PROGRESS_READ_BYTES) -> str:
    try:
        with transcripts.open_regular(path) as stream:
            size = os.fstat(stream.fileno()).st_size     # the file read, not the path's
            if size <= max_bytes:
                raw = stream.read(max_bytes)            # never more, however it grows meanwhile
            else:
                half = max_bytes // 2
                raw = stream.read(half)
                stream.seek(max(0, size - half))
                raw += b"\n... [middle omitted] ...\n" + stream.read(half)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                              text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None


def repository_context(cwd: Path, cap: int) -> tuple[str, int]:
    probe = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if probe is None or probe.returncode != 0:
        return truncate("Not a Git worktree.", cap), 0
    sections = []
    for title, args, empty in (
        ("Status", ["status", "--short", "--branch", "--untracked-files=normal"], "Clean."),
        ("Recent commits", ["log", "-5", "--oneline", "--decorate"], "No commits."),
        ("Salvage refs",
         ["for-each-ref", "--sort=-creatordate", "--count=12",
          "--format=%(refname) %(objectname:short)",
          "refs/codex-salvage", "refs/claude-salvage"], "None."),
    ):
        completed = _run_git(cwd, args)
        body = (completed.stdout.strip()
                if completed is not None and completed.returncode == 0 else "")
        sections.append(f"### {title}\n{body or empty}")
    return clean("\n\n".join(sections), cap)


def latest_metadata(path: Path, *,
                    max_bytes: int = LAST_SCAN_BYTES) -> tuple[str | None, str | None]:
    """The last main-chain entry's timestamp and cwd.

    `max_bytes` is 2 MB for ranking `--last`, where a transcript with nothing in
    its tail simply ranks low. Resolving the WORKDIR is different: a session
    whose last 2 MB happen to be sidechain and tool-result rows has a cwd, and
    v1 scanned the whole file (64 MB) to find it rather than telling the caller
    to pass `-C`. `resolve_workdir` asks for that.
    """
    stamp = cwd = None
    for line in transcripts.lines_reversed(path, chunk=64 * 1024, max_bytes=max_bytes):
        entry = _parse(line)
        if not transcripts.is_main(entry):
            continue
        if cwd is None and isinstance(entry.get("cwd"), str) and entry["cwd"]:
            cwd = entry["cwd"]
        if stamp is None and isinstance(entry.get("timestamp"), str):
            stamp = entry["timestamp"]
        if stamp is not None and cwd is not None:
            break
    return stamp, cwd


def canonical_session_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise HandoffError(f"invalid Claude session id: {value!r}") from exc
    canonical = str(parsed)
    if value.casefold() != canonical:
        raise HandoffError(f"Claude session id must be a canonical UUID: {value!r}")
    return canonical


def _candidate_transcripts() -> list[Path]:
    projects = transcripts.projects_dir()
    found: dict[str, Path] = {}
    for pattern in ("*.jsonl", "*/*.jsonl"):
        try:
            for path in projects.glob(pattern):
                if path.is_file():
                    found[str(path)] = path
        except OSError:
            continue
    return list(found.values())


def resolve_source(session_id: str | None, last: bool, *,
                   current: str | None = None) -> tuple[str, Path]:
    """Which transcript this handoff comes from: a named session, or `--last`."""
    if bool(session_id) == bool(last):
        raise HandoffError("provide exactly one of SESSION_ID or --last")
    if session_id:
        canonical = canonical_session_id(session_id)
        path = transcripts.transcript_path(canonical)
        if path is None:
            raise HandoffError(f"transcript not found for Claude session {canonical}")
        return canonical, path
    if current:
        try:
            canonical = canonical_session_id(current)
        except HandoffError:
            canonical = ""
        if canonical and (path := transcripts.transcript_path(canonical)) is not None:
            return canonical, path
    ranked: list[tuple[tuple, str, Path]] = []
    for path in _candidate_transcripts():
        try:
            canonical = canonical_session_id(path.stem)
            stamp, _cwd = latest_metadata(path)
            mtime = path.stat().st_mtime
        except (HandoffError, OSError):
            continue
        ranked.append(((1 if stamp else 0, stamp or "", mtime, str(path)), canonical, path))
    if not ranked:
        raise HandoffError("no Claude session transcript found for --last")
    _key, canonical, path = max(ranked, key=lambda item: item[0])
    return canonical, path


def resolve_workdir(path: Path, override: str | Path | None) -> tuple[Path, str | None]:
    _stamp, source_cwd = latest_metadata(path, max_bytes=FULL_SCAN_BYTES)
    chosen = (Path(override).expanduser() if override is not None
              else Path(source_cwd).expanduser() if source_cwd else None)
    if chosen is None:
        raise HandoffError("source transcript has no cwd; pass -C DIR")
    try:
        chosen = chosen.resolve()
    except OSError as exc:
        raise HandoffError(f"cannot resolve workdir {chosen}: {exc}") from exc
    if not chosen.is_dir():
        raise HandoffError(f"workdir is not a directory: {chosen}")
    return chosen, source_cwd


@dataclass
class Brief:
    """The dispatched text, plus what the dispatcher needs to route it."""

    text: str
    original: str
    session_id: str
    transcript: str
    workdir: str
    source_cwd: str | None
    redactions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "transcript": self.transcript,
                "workdir": self.workdir, "source_cwd": self.source_cwd,
                "redactions": self.redactions, "characters": len(self.text)}


#: The repository section when the caller must not run git (a conversation
#: handoff is answered by a daemon op handler, which never waits on git, C-25.3).
REPOSITORY_NOT_COLLECTED = ("Not collected at handoff. Run `git status` and `git log` in the "
                            "target cwd before acting.")


def workspace_sections(cwd: Path, caps: dict[str, int], *,
                       repository: bool = True) -> tuple[str, str, int]:
    """The brief's PROGRESS.md and repository sections for the target cwd."""
    redactions = 0
    progress_path = cwd / "PROGRESS.md"
    if progress_path.is_file():
        progress, count = clean(_read_bounded(progress_path), caps["progress"])
        redactions += count
    else:
        progress = truncate("Not present.", caps["progress"])
    if repository:
        repo, count = repository_context(cwd, caps["repository"])
        redactions += count
    else:
        repo = truncate(REPOSITORY_NOT_COLLECTED, caps["repository"])
    return progress, repo, redactions


def assemble(*, provider: str, source_label: str, session_id: str, transcript: Path | str,
             source_cwd: str | None, cwd: Path, original: str, recent_title: str, recent: str,
             progress: str, repository: str, redactions: int) -> tuple[str, int]:
    """The brief's text from its bounded sections (C-23.14, C-23.36), scrubbed once
    more as a whole. Both brief readers end here, so they share one header."""
    text = f"""# Cross-agent handoff

Continue the source session's work in the target worktree. Inspect the actual
workspace before acting: this is a bounded excerpt, not an authoritative state
snapshot. Ordinary tool inputs and textual results were bounded and credential-
scrubbed; thinking, binary payloads, and credential-reading inputs/results were
omitted. The full transcript may contain sensitive raw material; consult it only
when necessary and never expose credentials.

- Source provider: {provider}
- Source {source_label}: {session_id}
- Source transcript: {transcript}
- Source cwd: {source_cwd or "unknown"}
- Target cwd: {cwd}
- Credential/binary redactions in this brief: {redactions}

## Original task

{original}

## {recent_title}

{recent}

## PROGRESS.md

{progress}

## Repository state

{repository}
"""
    # One last pass over the assembled brief: a section boundary can splice two
    # halves into a shape no individual section matched. It is matched whole:
    # policy keeps its section caps within the scrubber's bound (C-23.36), and a
    # brief over it must not become one excerpt of itself.
    text, final_count = scrub_secrets(text, whole=True)
    if final_count:
        text = text.replace(
            f"Credential/binary redactions in this brief: {redactions}",
            f"Credential/binary redactions in this brief: {redactions + final_count}")
        redactions += final_count
    return text.rstrip() + "\n", redactions


def build_brief(session_id: str, transcript: Path, cwd: Path, source_cwd: str | None,
                caps: dict[str, int], *, repository: bool = True) -> Brief:
    """The brief itself (C-23.14, C-23.36). Sections in v1's order.

    `repository=False` leaves git out (the repository section says so); a
    conversation handoff (C-30.3) passes it because its handler never waits on
    git (C-25.3).
    """
    original, first_uuid, redactions = first_task(transcript, caps["original_task"])
    recent, recent_redactions = recent_excerpt(transcript, first_uuid, caps)
    if not recent:
        recent = truncate("No additional text or safe tool-result context was available.",
                          caps["recent"])
    redactions += recent_redactions
    progress, repo, count = workspace_sections(cwd, caps, repository=repository)
    redactions += count
    text, redactions = assemble(
        provider="Claude Code", source_label="session", session_id=session_id,
        transcript=transcript, source_cwd=source_cwd, cwd=cwd, original=original,
        recent_title="Recent main-chain excerpt", recent=recent, progress=progress,
        repository=repo, redactions=redactions)
    return Brief(text=text, original=original, session_id=session_id,
                 transcript=str(transcript), workdir=str(cwd),
                 source_cwd=source_cwd, redactions=redactions)


@dataclass
class Dispatched:
    brief: Brief
    job_id: str | None = None
    request_id: str = ""
    model: str | None = None
    task: str | None = None
    tier: str | None = None
    sandbox: str | None = None
    caller_session: str | None = None
    prompt_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**self.brief.to_dict(), "job_id": self.job_id,
                "request_id": self.request_id, "model": self.model,
                "task": self.task, "tier": self.tier, "sandbox": self.sandbox,
                "caller_session": self.caller_session,
                "prompt_path": self.prompt_path}


def sandbox_for(policy: dict[str, Any], task: str | None,
                override: str | None = None) -> str:
    """The handoff's sandbox: the operator's `-s`, else the task's permission.

    v1 derived the task class from the original instruction text and let the
    dispatcher's `permissions` map decide. v2 keeps the map and drops the guess:
    a handoff with no `--task` is read-only, because a writable job carries
    consequences a text classifier should not choose (C-6.5 refuses one on
    `main`, and requires a committed repository).
    """
    if override:
        return override
    permissions = policy.get("permissions", {})
    return permissions.get(task) or permissions.get("*") or Sandbox.READ_ONLY.value


def handoff(sessions, policy: dict[str, Any], *, session_id: str | None, last: bool,
            model: str | None, stage_prompt, workdir: str | Path | None = None,
            task: str | None = None, tier: str | None = None,
            sandbox: str | None = None, caller_session: str | None = None,
            caller_pid: int | None = None, out_path: str | None = None,
            current_session: str | None = None, request_id: str | None = None,
            minted: bool | None = None,
            lane_ids: Any = None, conversation_ids: Any = None,
            dry_run: bool = False) -> Dispatched:
    """Build one brief and submit it through the ordinary path (C-23.54).

    `--to` is a routing pin, so the brief inherits the same model resolution,
    lane picking, guard, salvage, ledger and notices as any other job. The
    caller's session is recorded so the completion notice comes back to it.
    """
    caps = {**policy.get("sessions", {}).get("handoff_caps", {})}
    canonical, transcript = resolve_source(session_id, last, current=current_session)
    if registry.is_conversation_session(canonical, conversation_ids or ()):
        # C-26.13, before the lane check: a conversation's transcript can look
        # like a lane run. Its work continues in its conversation, and a
        # labelled cross-provider handoff of it is the app's (C-30.3).
        raise HandoffError(
            f"{canonical} is {registry.CONVERSATION_REASON}; the kit does not "
            "hand it off", 7, registry.CONVERSATION_FIX)
    if registry.is_lane_run(canonical, lane_ids=lane_ids or (), transcript=transcript):
        # C-23.31: a headless lane run is never continued, and a request naming
        # one is refused with the reason. Its transcript is one brief and one
        # answer; there is no conversation to hand to anybody.
        raise HandoffError(
            f"{canonical} is a headless lane run (claude -p), not a session", 7,
            "`subfleet runs show <job>` for what that lane produced")
    target, source_cwd = resolve_workdir(transcript, workdir)
    brief = build_brief(canonical, transcript, target, source_cwd, caps)
    identity = request_id or str(uuid.uuid4())
    chosen = sandbox_for(policy, task, sandbox)
    if dry_run:
        return Dispatched(brief=brief, request_id=identity, model=model,
                          task=task, tier=tier, sandbox=chosen,
                          caller_session=caller_session)
    prompt_path = str(stage_prompt(brief.text))
    args = SubmitArgs(
        request_id=identity,
        kind="handoff",
        workdir=str(target),
        prompt_path=prompt_path,
        sandbox=chosen,
        task=task,
        tier=tier,
        pinned_model=model,
        out_path=out_path,
        name=f"handoff-{canonical[:8]}",
        caller_session=caller_session,
        caller_pid=caller_pid,
    )
    # C-16.3: whose request id this is. The CLI mints one before calling (the
    # staged prompt is named after it), so it says so; None infers from `request_id`.
    result = sessions.submit(args, minted=not request_id if minted is None else minted)
    return Dispatched(brief=brief, job_id=result.get("job_id"), request_id=identity,
                      model=model, task=task, tier=tier, sandbox=chosen,
                      caller_session=caller_session, prompt_path=prompt_path)


__all__ = ["Brief", "Dispatched", "HandoffError", "assemble", "build_brief",
           "canonical_session_id", "clean", "first_task", "handoff", "latest_metadata",
           "looks_binary", "recent_excerpt", "repository_context", "resolve_source",
           "resolve_workdir", "sandbox_for", "scrub_bounded", "scrub_secrets", "select_segments",
           "sensitive_tool_call", "truncate", "workspace_sections"]
