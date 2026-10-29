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

import bisect
import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..contracts import DEFAULT_CAPS, SCRUB_MAX_CHARS, Exit
from ..launch_env import build_environment
from ..policy import resolve_sandbox
from ..protocol import POLICY_SANDBOX, SubmitArgs
from ..salvage import SalvageError, git_head
from . import registry, scrub_release, transcripts

REDACTED = "[REDACTED]"
OMITTED_BINARY = "[binary/base64 tool result omitted]"
OMITTED_SENSITIVE = "[credential-reading tool result omitted]"
OMITTED_SENSITIVE_INPUT = "[credential-reading input omitted]"
OMITTED_UNMATCHED = "[tool result omitted because its input is outside this excerpt]"

MAX_JSON_LINE_CHARS = 4 * 1024 * 1024
FULL_SCAN_BYTES = 64 * 1024 * 1024
LAST_SCAN_BYTES = 2 * 1024 * 1024
PROGRESS_READ_BYTES = 128 * 1024
# Bound the rules' work (C-23.14). In a longer text only the blocks that can span
# lines (reminders, private keys, data URIs) are resolved whole, in linear scans;
# every other rule runs on a head and a tail excerpt of at most EXCERPT_CHARS.
MAX_SCRUB_CHARS = SCRUB_MAX_CHARS
EXCERPT_CHARS = MAX_SCRUB_CHARS // 2

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
_DATA_URI_RE = re.compile(
    r"(?i)data:[a-z0-9.+/-]+(?:;[a-z0-9=.+/-]+)*;base64,[A-Za-z0-9+/=\s]{32,}"
)
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{160,}={0,2})(?![A-Za-z0-9+/])"
)
_URL_PASSWORD_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)")
_URL_SCHEME_RE = re.compile(r"[a-z0-9+.-]+", re.IGNORECASE)
_URL_START_RE = re.compile(r"\b[a-z]", re.IGNORECASE)
#: A header's name: an identifier ending in `authorization` or `cookie` (`Authorization`,
#: `Proxy-Authorization`, `X-Authorization`, `Cookie`, `Set-Cookie`). One that starts its
#: line is `_HEADER_RE`'s; any other, before `[ \t]*:`, is an inline header's
#: (`_scrub_inline_headers`). Either takes a header's value, not a plain one (C-23.14).
_HEADER_NAME = r"[A-Za-z0-9_-]*(?:authorization|cookie)"
_HEADER_RE = re.compile(r"(?im)^([^\S\n]*" + _HEADER_NAME + r"\s*:\s*).+$")
#: The word that ends an inline header's name, and its colon, read on the reversed
#: text: each match starts at a `:`, which the regex engine finds by a plain search,
#: where the same words read forwards were tried, case folded, at every character
#: (about a tenth of the cost per MiB). The name's identifier starts where
#: `_IDENTIFIER_RE` reads back from the word, on the same reversed text.
_INLINE_HEADER_REVERSED_RE = re.compile(r":[ \t]*(?:noitazirohtua|eikooc)", re.IGNORECASE)
_VISIBLE_RE = re.compile(r"\S")
#: A key ending in a header word with this after it is a header, not a key with a
#: plain value.
_HEADER_WORD_RE = re.compile(r"(?:authorization|cookie)\Z", re.IGNORECASE)
_HEADER_COLON_RE = re.compile(r"[ \t]*:")
#: A run of backslashes, matched whole (so no run is searched again from inside it).
_BACKSLASH_RUN_RE = re.compile(r"\\+")
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
# A key's closing quote may be escaped, by a repr (`\'password\': …`) or by JSON
# encoded more than once (`\"password\": …`, `\\\"password\\\": …`).
_ASSIGN_SEPARATOR_RE = re.compile(r"(?:\\*[\"'])?\s*[:=]\s*")
# A value that opens with a quote, escaped or not, is the quoted rule's.
_PLAIN_VALUE_RE = re.compile(r"(?!\\*[\"'])[^\s,;\"']+")
_FLAG_VALUE_RE = re.compile(r"[ \t]+(?P<value>[^\s-][^\s]*)")
# A `Bearer` token: the run of token characters after the scheme word. Whether the
# run is a token or a credential key a named-value rule reads is `_bearer_refusal`'s
# decision, made once per match in Python: the rule's old lookahead tried every
# prefix of the run, which at the work bound held the GIL some 25 times as long.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/-]{8,})=*")
#: What may join a credential word to the token run before it (`…_token`, `….token`).
_TOKEN_JOINERS = "._~+/-"
#: `Bearer` ending a value whose token may be on the next line (`_header_redaction`).
_BEARER_END_RE = re.compile(r"(?i)\bBearer\Z")
_QUOTE_EVENT_RE = re.compile(r"\\[^\n]|[\"'\r\n]")
#: A run of backslashes, a quote or a line break: each run is one match.
_ESCAPE_EVENT_RE = re.compile(r"\\+|[\"'\r\n]")
_VALUE_QUOTE_RE = re.compile(r"\\*[\"']")
_LINE_END_RE = re.compile(r"[\r\n]|\Z")
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
#: Public marker vocabulary for consumers that cut scrubbed text.
REDACTION_MARKERS = _PLACEHOLDERS
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


def _quoted_ends(text: str, *, multiline: bool = False) -> dict[int, int]:
    """Index matching quotes once, respecting escapes and, for excerpts, line boundaries.

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
            if not multiline:
                previous.clear()
        else:
            if char in previous:
                ends[previous[char]] = match.start()
            previous[char] = match.start()
    return ends


def _escaped_quote_ends(text: str, *, multiline: bool = False) -> dict[int, int]:
    """`_quoted_ends` for quotes a run of backslashes escapes (a repr's `\\'`, JSON
    encoded twice's `\\"`, three times' `\\\\\\"`): from the start of each such run,
    the start of the next run of the same length before the same quote, which is
    where a value that opens with it closes. One scan, where each run is one match;
    except in whole-text mode, a line break ends every open quote."""
    ends: dict[int, int] = {}
    previous: dict[tuple[int, str], int] = {}
    run_start = run_end = -1
    for match in _ESCAPE_EVENT_RE.finditer(text):
        char = match.group()
        if char[0] == "\\":
            run_start, run_end = match.start(), match.end()
        elif char in "\r\n":
            if not multiline:
                previous.clear()
        elif run_end == match.start():
            kind = (run_end - run_start, char)
            if kind in previous:
                ends[previous[kind]] = run_start
            previous[kind] = run_start
    return ends


def _flag_start(key: str) -> int:
    """Where the credential flag in `key`, one identifier as `_IDENTIFIER_RE` reads it,
    starts (its last `--`) when the flags rule masks the value after it, else -1:
    `--password`, `--db-token`, `x--client-secret`. The cut's break rule asks the same
    (`_flag_ends_at`), so a flag the rule masks is one no cut parts from its value."""
    flag_start = key.rfind("--")
    if flag_start < 0 or not _SENSITIVE_SUFFIX_RE.search(key[-17:]) \
            or not _SENSITIVE_FLAG_RE.fullmatch(key[flag_start + 2:]):
        return -1
    return flag_start


def _scrub_named_values(text: str, replace: Callable[[str, str], str],
                       *, quoted: bool = False, flags: bool = False, multiline: bool = False,
                       spans: list[tuple[int, int, str]] | None = None) -> str:
    """Replace assignment/flag values in linear scans, preserving rule order. With
    `spans`, the quoted pass only records what it would replace (`quoted_value_spans`)
    and returns `text` as it is."""
    parts: list[str] = []
    cursor = 0
    quote_ends: dict[int, int] | None = None
    escaped_ends: dict[int, int] | None = None
    line_ends = None
    line_end = -1
    ranges: list[tuple[int, int, str]] = []
    visible: dict[int, int] = {}
    owned: dict[int, tuple[int | None, int, tuple[int, int] | None]] = {}
    keyed: dict[int, int | None] = {}

    def visible_end(at: int) -> int:
        # The end of the last visible character before `at`, walked back once per
        # position: candidates that share a line's end, or an item, share the walk.
        if at not in visible:
            visible[at] = _last_visible(text, at) + 1
        return visible[at]

    def owned_before(start: int, last: int) -> tuple[int | None, tuple[int, int] | None]:
        # What ends the text before `last`, a line's last visible character, read once
        # per line end however many unclosed values, or header names a quoted match
        # swallows, share it (#239 review r2, P3-1):
        # the item that owns a value past the line (`_owned_item`), if it starts at or
        # after `start`, else the value a secret-named flag there is given
        # (`_secret_flag`), if the flag does.
        if last not in owned:
            item = _owned_item(text, 0, last)
            flag = _ending_flag(text, 0, last) if item is None else None
            owned[last] = (item, -1 if flag is None else flag,
                           None if flag is None else _secret_flag(text, flag, last))
        item, flag, fold = owned[last]
        return (item if item is not None and item >= start else None), (fold if flag >= start else None)

    for token in _IDENTIFIER_RE.finditer(text):
        fold = None
        # Inside the previous plain replacement (the quoted rule keeps no cursor).
        nested = not flags and token.start() < cursor
        key = token.group()
        if not _SENSITIVE_SUFFIX_RE.search(key[-17:]):
            continue
        start = token.start()
        if flags:
            flag_start = _flag_start(key)
            if flag_start < 0:
                continue
            start += flag_start
            if start < cursor:
                continue                        # the flag is inside the previous flag's value
            if _value_runs(key[flag_start + 2:]):
                # A flag whose name reads as a secret (`--SECRET7…_token`) is one: it
                # goes itself, with the value daemon.log's folded line gives it, on a
                # later line too (#239 review r2: fail closed).
                end, fold = token.end(), _fold_value(text, token.end())
                parts.extend((text[cursor:start], replace(text[start:end], REDACTED)))
                cursor = end
                if fold is not None:
                    parts.extend((text[cursor:fold[0]], replace(text[fold[0]:fold[1]], REDACTED)))
                    cursor = fold[1]
                continue
            value = _FLAG_VALUE_RE.match(text, token.end())
            if value is None:
                continue
            value_start, end = value.start("value"), value.end()
            # A credential flag glued to the value's end (`--password x--token …`) keeps
            # its own value: the value ends before it, as a plain value's does (C-23.14).
            glued = _owning_flag(text, value_start, end)
            if glued is not None:
                end = glued
            else:
                fold = _secret_flag(text, value_start, end)   # one glued to it is taken whole
            out = text[start:value_start] + REDACTED
        else:
            separator = _ASSIGN_SEPARATOR_RE.match(text, token.end())
            if separator is None:
                continue
            if start > cursor and text[start - 1] in "\"'":
                start -= 1
            value_start = separator.end()
            if nested and value_start < cursor:
                continue                        # its value is in the previous replacement
            opening = _VALUE_QUOTE_RE.match(text, value_start) if quoted else None
            if quoted and opening is None and (not (ranges and start < ranges[-1][1] <= value_start) or (
                    token.start() >= ranges[-1][1] and _HEADER_WORD_RE.search(key[-13:])
                    and _HEADER_COLON_RE.match(text, token.end()))):
                # A plain value is the plain rule's, after this pass, unless its key
                # is in a quoted match that ends before its value starts. A header's
                # name and colon that the match leaves visible (`…"Cookie: Basic …`,
                # the match ending at the quote before the name) take a header's value,
                # the header rules', never a plain one (C-23.14).
                continue
            if opening is not None:
                if opening.end() - value_start == 1:
                    if quote_ends is None:
                        quote_ends = _quoted_ends(text, multiline=multiline)
                    closing = quote_ends.get(value_start)
                else:
                    # Escaped (`\'`, `\"`, `\\\"`): it closes at the next quote of
                    # its kind escaped by a run of the same length.
                    if escaped_ends is None:
                        escaped_ends = _escaped_quote_ends(text, multiline=multiline)
                    closing = escaped_ends.get(value_start)
                if closing is None:
                    # An unterminated quote must never expose its value. Without
                    # a closing delimiter, redact to the line/record boundary.
                    # Advance one shared iterator so nested candidates cannot
                    # repeatedly scan the same suffix.
                    if line_ends is None:
                        line_ends = _LINE_END_RE.finditer(text)
                    while line_end < value_start:
                        line_end = next(line_ends).start()
                    end = line_end
                    # A credential flag or `Bearer` that ends the line keeps its own
                    # value, which may be past the line, exactly as it would with no
                    # quote: the redaction ends before it (`_owned_item`, C-23.14).
                    item, fold = owned_before(opening.end(), visible_end(end))
                    # But a flag whose name is a key with its separator on the next line
                    # (`--password` and `=r` there) ends nothing: the union takes it with
                    # the key's value, as for any other such key (#239 review r6, P2-1).
                    if (last := visible_end(end)) not in keyed:
                        keyed[last] = _key_past_line(text, 0, last)
                    if keyed[last] is not None and keyed[last] >= opening.end():
                        item = fold = None
                    if item is not None:
                        if item == opening.end():
                            continue                # the value is the item alone: its rule's
                        end = visible_end(item)
                    out = text[start:value_start] + REDACTED
                else:
                    end = closing + opening.end() - value_start
                    out = text[start:opening.end()] + REDACTED + text[closing:end]
            else:
                value = _PLAIN_VALUE_RE.match(text, value_start)
                if value is None:
                    continue
                end = value.end()
                if quoted and _HEADER_WORD_RE.search(key[-13:]) and _HEADER_COLON_RE.match(text, token.end()):
                    # A header's name inside the quoted match, its value past the
                    # match: a header's value, to its line's end (C-23.14). A flag or
                    # `Bearer` that ends the line keeps its value, read at the line's
                    # last visible character, as `_header_redaction` reads it.
                    end = text.find("\n", value_start)
                    end = len(text) if end < 0 else end
                    item, fold = owned_before(value_start, visible_end(end))
                    # A flag there whose name is a key with its separator on the next line
                    # ends nothing, as for an unclosed value (#239 review r7, P2-1).
                    if item is not None or fold is not None:
                        if (last := visible_end(end)) not in keyed:
                            keyed[last] = _key_past_line(text, 0, last)
                        if keyed[last] is not None and keyed[last] >= value_start:
                            item = fold = None
                elif quoted:
                    # A key in a quoted match whose plain value is past the match: a
                    # `Bearer` ending the value is an item too, whose token the union
                    # would otherwise take from the `Bearer` rule (#239 review r2, P2-2).
                    item = _owned_item(text, value_start, end)
                    fold = None if item is not None else _secret_flag(text, value_start, end)
                    # And a flag there whose name is such a key ends nothing either (#239
                    # review r7, P2-1).
                    if item is not None or fold is not None:
                        if (last := visible_end(end)) not in keyed:
                            keyed[last] = _key_past_line(text, 0, last)
                        if keyed[last] is not None and keyed[last] >= value_start:
                            item = fold = None
                else:
                    # A credential flag that ends the value (`api_key=x--password …`)
                    # keeps its own value: the value ends before it (C-23.14).
                    item = _owning_flag(text, value_start, end)
                    fold = None if item is not None else _secret_flag(text, value_start, end)
                if item is not None:
                    if item == value_start:
                        continue
                    end = visible_end(item)
                out = text[start:value_start] + REDACTED
        if quoted:
            # A surrounding assignment can end at an inner value's opening
            # quote, and an unterminated value at the next one of its kind (`a='x
            # b='y'`) or at its line's end, past a key whose value is on the next
            # line (`a='x b:\n y`, plain or quoted). Mask the union of overlapping
            # matches, including the inner value beyond, instead of skipping its
            # candidate key or leaving its value to a rule that no longer sees
            # the key. Outside whole-text mode a match spans a line break only
            # between its key and its value, which `_neutral_break` never cuts,
            # so neither does a union.
            for start, end, out in ((start, end, out), *(() if fold is None else ((*fold, REDACTED),))):
                if ranges and start < ranges[-1][1]:
                    previous_start, previous_end, previous_out = ranges[-1]
                    ranges[-1] = (previous_start, max(previous_end, end), previous_out)
                else:
                    ranges.append((start, end, out))
        elif nested:
            # A plain value that ends in a key whose own value follows (`a=xtoken
            # = y`): that value goes too, though its key went with the first.
            parts.extend((text[cursor:value_start], replace(text[value_start:end], REDACTED)))
            cursor = end
        else:
            parts.extend((text[cursor:start], replace(text[start:end], out)))
            cursor = end
        if fold is not None and not quoted:
            # The value daemon.log's folded line gives a secret-named flag the match
            # took (`_secret_flag`): redacted too, on its own, the text between kept.
            parts.extend((text[cursor:fold[0]], replace(text[fold[0]:fold[1]], REDACTED)))
            cursor = fold[1]
    if spans is not None:
        spans.extend(ranges)
        return text
    for start, end, out in ranges:
        parts.extend((text[cursor:start], replace(text[start:end], out)))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def scrub_secrets(text: str, *, strip_reminders: bool = False, whole: bool = False) -> tuple[str, int]:
    """Remove credential values and encoded binary; retain ordinary text (C-23.14). The
    count is of values replaced: release's or the head's, whichever replaced more.

    Release/217's scrub for the same mode runs first (`scrub_release`, frozen at
    `2c981cba5`), and its output is what this returns, but for each whitespace-free run
    of it that the head's own scrub of the text (`_head_scrub_secrets`) does not hold,
    which becomes `[REDACTED]` (`shown_by_both`, #239 round 8). That only replaces runs of
    release's output, so no character release hides comes back; and a run shows only if
    the head's output holds it too, so no value the head hides shows. A text longer than
    MAX_SCRUB_CHARS keeps a scrubbed head and tail with a marker between them, as
    release cuts it (`scrub_bounded`). `whole` scrubs all of it, for a text whose length
    its caller bounds: the assembled handoff brief, whose section caps policy keeps
    within the same bound (C-23.36)."""
    if len(text) > scrub_release.MAX_SCRUB_CHARS and not whole:
        return scrub_bounded(text, None, strip_reminders=strip_reminders)
    released, count = scrub_release.scrub_secrets(text, strip_reminders=strip_reminders, whole=whole)
    head, head_count = _head_scrub_secrets(text, strip_reminders=strip_reminders, whole=whole)
    return shown_by_both(released, head), max(count, head_count)


def scrub_bounded(text: str, limit: int | None, *, strip_reminders: bool = False) -> tuple[str, int]:
    """`truncate(scrub_secrets(text), limit)`: scrubbed, then bounded to `limit`
    characters keeping the head and the tail (C-23.14, C-23.36, C-25.5); with `limit`
    None, not bounded further.

    Release/217's pipeline for the mode makes the pieces (`_released_pieces`: its block
    resolution, excerpts, scrub and bound, as `scrub_release.scrub_bounded` joins them,
    each excerpt cut where both scrubbers allow a cut), and each scrubbed piece keeps
    only the runs that the head's own scrub of the same text holds (`shown_by_both`): of
    the text release scrubbed whole, or of the same excerpt, before release scrubbed it;
    each piece is kept so before it is cut. A text release scrubs whole (within the
    budget, or once its blocks are resolved) is one piece, release's scrub before its
    `truncate`, and `truncate` bounds what is written of it, as release bounds its own
    scrub: so within the budget this is exactly `truncate(scrub_secrets(text), limit)`
    (#239 round 9). Past it, each excerpt is kept so, then cut as release cuts its own,
    with release's marker between them, the characters release's own cut of the same
    excerpts leaves out; those pieces fit `limit`, so nothing cuts them again (#239 review
    r9b, P2-1). Either way the cut,
    made in the written text, can fall elsewhere than release's cut of its own: it can
    keep a character release's cut left out, but none either scrub hides. The count is
    release's or the head's, whichever replaced more."""
    pieces, outer_strip, count, heads, head_count = _released_pieces(text, limit, strip_reminders)
    out = "".join(piece if kept else shown_by_both(piece, head) for (piece, kept), head in zip(pieces, heads))
    out = out.strip() if outer_strip else out
    return (truncate(out, limit) if limit is not None and len(out) > limit else out), max(count, head_count)


#: A whitespace-free run, as `shown_by_both` keeps or replaces one.
_SHOWN_RUN_RE = re.compile(r"\S+")


def shown_by_both(released: str, head: str) -> str:
    """`released` (release/217's output of a mode) with each whitespace-free run that
    `head` (the head's output of the same mode, of the same text) does not hold
    replaced by `[REDACTED]` (C-23.14, #239 round 8). A run of `released` is kept only if
    it is one of `head`'s runs, so neither scrubber's hidden values show: what release
    hides is not in `released`, and the replacement writes no character but the
    placeholder's; what the head hides is in no run of `head`. The lines, and the
    whitespace between runs, are release's."""
    shown = set(_SHOWN_RUN_RE.findall(head))
    return _SHOWN_RUN_RE.sub(lambda run: run.group() if run.group() in shown else REDACTED, released)


def _released_pieces(text: str, limit: int | None, strip_reminders: bool):
    """Release/217's `scrub_bounded` (`scrub_release`), as the pieces it joins: each
    scrubbed piece with False and each marker with True, whether the join is stripped,
    release's count; then for each piece the head's own scrub of the same text (`_scrub`
    of the text release scrubbed, whole or the same excerpt of it; "" for a marker), and
    the head's count. The steps are release's, in its order, by its helpers, and the
    blocks it resolves first are the head's too (`_resolve_blocks`); a text that needs no
    excerpt is one piece, release's scrub before the `truncate` that `scrub_bounded` then
    applies to the written text. With a limit, each excerpt is kept for the runs the
    head's scrub of it holds (`shown_by_both`) before release's cut, so its pieces come
    with True and "" (#239 review r9b). The pieces of a text whose cuts both scrubbers
    make alike, and whose excerpts they scrub alike, join to release's output exactly,
    which a test checks. Where the head's cut is the more cautious, each excerpt is the
    head's, and its pieces the start and end of release's own."""
    release = scrub_release
    original, count = len(text), 0
    if original > release.MAX_SCRUB_CHARS:
        tally = release._Tally()
        text, strip_reminders = release._resolve_blocks(text, strip_reminders, tally), False
        count = tally.total
    if len(text) <= release.MAX_SCRUB_CHARS:
        scrubbed, found = release._scrub(text, strip_reminders)
        head, head_found = _scrub(text, strip_reminders)
        # Release bounds this scrub with `truncate`; `scrub_bounded` bounds the composed scrub
        # with it instead, so within the budget the bounded mode is the whole-text mode, cut
        # (C-23.36). A cut piece of release's would hold the start or end of a run that the
        # head's scrub holds only whole (`p` of `password`), which `shown_by_both` would hide
        # (#239 round 9). `truncate` strips first, so the piece is stripped here.
        return ([(scrubbed if limit is None else scrubbed.strip(), False)], False, count + found, [head],
                count + head_found)
    # Each excerpt is cut where both scrubbers allow a cut: the head refuses every break
    # release refuses, and more (a credential flag's, whose value the head's fold reads on
    # the next line), so its head cut is at or before release's and its tail cut at or
    # after. Release's scrub of the shorter excerpt is the start (or end) of its scrub of
    # its own, as its excerpt cuts rest on, and the head's is its own excerpt's.
    head_end = min(release._head_cut(text), _head_cut(text))
    tail_start = max(release._tail_cut(text), _tail_cut(text))
    head, head_count = release._scrub(text[:head_end], False)
    tail, tail_count = release._scrub(text[tail_start:], False)
    (own_head, own_head_count), (own_tail, own_tail_count) = _scrub(text[:head_end], False), _scrub(text[tail_start:], False)
    heads, own = [own_head, "", own_tail], count + own_head_count + own_tail_count
    head, tail, count = head.strip(), tail.strip(), count + head_count + tail_count
    if limit is not None:
        usable = limit - len(f"\n… [{original:,} characters omitted] …\n")   # the longest marker
        if usable <= 0:
            return [(release.ELIDED[:limit] if limit < len(release.ELIDED) else release.ELIDED, True)], False, \
                count, [""], own
        # Each excerpt is kept for the runs the head's scrub of it holds first, then cut as release
        # cuts its own, so the pieces are written as they are and fit the limit. Cut first, the
        # part-word at each cut would become `[REDACTED]`, which the head's scrub holds only
        # whole, and `truncate` would cut the longer result again, through the marker (#239
        # review r9b, P2-1). The marker is release's, as without a limit: what its own cut of
        # the same excerpts leaves out (#239 round 10).
        omitted = original - sum(map(len, _cut_excerpts(head, tail, usable)))
        head, tail = _cut_excerpts(shown_by_both(head, own_head).strip(), shown_by_both(tail, own_tail).strip(),
                                   usable)
        marker = f"\n… [{max(0, omitted):,} characters omitted] …\n"
        return [(head, True), (marker, True), (tail, True)], True, count, ["", "", ""], own
    marker = f"\n… [{max(0, original - len(head) - len(tail)):,} characters omitted] …\n"
    return [(head, False), (marker, True), (tail, False)], True, count, heads, own


def _cut_excerpts(head: str, tail: str, usable: int) -> tuple[str, str]:
    """Release's cut of a bounded text's two scrubbed excerpts (`scrub_release.scrub_bounded`):
    the start of the head within 60% of `usable`, then as much of the end of the tail as
    the rest of it holds."""
    head = head[:int(usable * 0.6)].rstrip()
    room = usable - len(head)
    return head, (tail[max(0, len(tail) - room):].lstrip() if room > 0 else "")


def _head_scrub_secrets(text: str, *, strip_reminders: bool = False, whole: bool = False) -> tuple[str, int]:
    """The head's own scrub, which `scrub_secrets` runs beside release's: remove credential values and encoded binary; retain ordinary text. The
    count is of values replaced: one credential two rules match counts once, and
    a header that held several values (`Cookie: a=...; b=...`) counts each.

    A text longer than MAX_SCRUB_CHARS keeps a scrubbed head and tail with a
    marker between them (`scrub_bounded`). `whole` scrubs all of it, for a text
    whose length its caller bounds: the assembled handoff brief, whose section
    caps policy keeps within the same bound (C-23.36). Whole-text callers also
    mask quoted assignment values across line breaks, before any folding or cut;
    excerpts retain their within-line rules so no match crosses an excerpt cut."""
    if len(text) > MAX_SCRUB_CHARS and not whole:
        return _head_scrub_bounded(text, None, strip_reminders=strip_reminders)
    return _scrub(text, strip_reminders, multiline=whole)


def _head_scrub_bounded(text: str, limit: int | None, *, strip_reminders: bool = False) -> tuple[str, int]:
    """`truncate(_head_scrub_secrets(text), limit)`: scrubbed, then bounded to `limit`
    characters keeping the head and the tail (C-23.14, C-23.36, C-25.5); with
    `limit` None, not bounded further.

    Within MAX_SCRUB_CHARS that is exactly what it does. A longer text first has
    the blocks that can span lines resolved over all of it, exactly as the scrub
    of the whole text resolves them (`_resolve_blocks`: linear scans). If it is
    still too long, the other rules run only on its head and its tail, each the
    whole lines within EXCERPT_CHARS, joined by a marker giving the characters
    left out. Those rules match within a line, except where a key, `Bearer` or
    separator (`:`, `=`, a key's quote) ends the text before a line break: an
    excerpt is cut only at a break no such text precedes (`_neutral_break`), so
    no match spans a cut and each excerpt scrubs exactly as the whole text does
    there. An excerpt with no such break in reach keeps nothing.
    """
    original, count = len(text), 0
    if original > MAX_SCRUB_CHARS:
        tally = _Tally()
        text, strip_reminders = _resolve_blocks(text, strip_reminders, tally), False
        count = tally.total
    if len(text) <= MAX_SCRUB_CHARS:
        scrubbed, found = _scrub(text, strip_reminders)
        return (scrubbed if limit is None else truncate(scrubbed, limit)), count + found
    head, head_count = _scrub(text[:_head_cut(text)], False)
    tail, tail_count = _scrub(text[_tail_cut(text):], False)
    head, tail, count = head.strip(), tail.strip(), count + head_count + tail_count
    if limit is not None:
        usable = limit - len(f"\n… [{original:,} characters omitted] …\n")   # the longest marker
        if usable <= 0:
            return (ELIDED[:limit] if limit < len(ELIDED) else ELIDED), count
        head = head[:int(usable * 0.6)].rstrip()
        room = usable - len(head)
        tail = tail[max(0, len(tail) - room):].lstrip() if room > 0 else ""
    marker = f"\n… [{max(0, original - len(head) - len(tail)):,} characters omitted] …\n"
    return (head + marker + tail).strip(), count


def _identifier_char(char: str) -> bool:
    """A character of a key as the scrubber reads one (`_IDENTIFIER_RE`, which
    ignores case, so `ſ` and the Kelvin sign are letters too)."""
    return _IDENTIFIER_RE.fullmatch(char) is not None


#: Every character `str.isspace` holds, which `\s` in these patterns matches and
#: `str.strip` strips (a test checks the list against both).
_SPACES = ("\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
           "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")
#: How far `_run_start` and `_run_end` read in growing steps before one scan of the rest.
_STEPS_MAX = 8192


def _run_start(text: str, end: int, chars: str | None = None) -> int:
    """Where the run of `chars` (whitespace when None) that ends at `end` starts: C
    scans of 64, 128, ... characters back, then at most one of the rest before `end`;
    nine scans in all, whatever the run's length."""
    step = 64
    while step <= _STEPS_MAX:
        near = max(0, end - step)
        kept = len(text[near:end].rstrip(chars))
        if kept or not near:
            return near + kept
        end, step = near, step * 2
    return len(text[:end].rstrip(chars))


def _run_end(text: str, start: int, chars: str | None = None) -> int:
    """Where the run of `chars` (whitespace when None) that starts at `start` ends: as
    `_run_start`, forward. `_WHITESPACE_RUN_RE.match(text, start).end()`, in C scans."""
    step = 64
    while step <= _STEPS_MAX:
        near = min(len(text), start + step)
        dropped = (near - start) - len(text[start:near].lstrip(chars))
        if start + dropped < near or near == len(text):
            return start + dropped
        start, step = near, step * 2
    return len(text) - len(text[start:].lstrip(chars))


def _last_visible(text: str, end: int) -> int:
    """The position of the last non-whitespace character before `end`, or -1."""
    return _run_start(text, end) - 1


def _neutral_break(text: str, visible: int, after: int,
                   before: Callable[[int], int] | None = None) -> bool:
    """Whether a line break can be a cut: no rule could continue a match past
    it. `visible` is the last visible character before it (-1: none), `after`
    the first after it (`len(text)`: none); `before` finds the last visible
    character before a position (`_last_visible` when None; `cut_whole` reads a
    group's margins as spaces, `_Reading.before`). Only these continue past a break,
    through whitespace that may include it:
    - a `:` or `=` after a key, its quote and whitespace, to the value;
    - a key (an identifier with a sensitive ending, which `Authorization`,
      `Cookie` and `Set-Cookie` headers have), or a key's closing quote
      (`"password"`, and escaped as `\\'password\\'` or `\\"password\\"`), to
      a `:` or `=` after the break;
    - `Bearer`, to a token character after the break;
    - a credential flag (`_flag_ends_at`), to any visible character but `-` after the
      break: a rule that takes the flag (one whose name reads as a secret, or one that
      is a header's whole value) takes the value daemon.log's folded line gives it
      too (`_secret_flag`, `_header_redaction`).
    `_bearer_refusal`, which refuses a token that is a key, and the header rules'
    choice to leave the key that ends a value (`_key_past_line`) read past a break
    only between a key or its closing quote and its `:` or `=`, or between those and
    the value, which the first two refuse; a refused `Bearer` still refuses the break
    after it, which is only more cautious, and is where a header that a refused
    `Bearer` ends takes the `Bearer`'s next line (`_refused_bearer`). Whether a flag
    owns a value past a match (`_owns_past`) is read from the one character after the
    flag, on its own line.
    An inline header's value (`_scrub_inline_headers`) crosses a break only between
    its colon and the value's first visible character, which the first refuses (its
    name is a key); otherwise it ends on its line, at a closing quote or the break.
    A quote or separator after anything else, a key followed by anything else,
    and a word ending the text are ordinary. No rule's replacement puts a `:`,
    `=` or token character where the first visible character after a break was
    anything else, so the text before the rules run decides."""
    if visible < 0:
        return True
    following = text[after:after + 1]
    last = text[visible]
    if last in ":=":
        # The separator may follow blank lines.
        key = before(visible) if before is not None else _last_visible(text, visible)
        if key >= 0 and text[key] in "\"'":
            return not _key_ends_at(text, key, quoted=True)
        return not _key_ends_at(text, key)
    separator = following in (":", "=")
    if last in "\"'":
        return not (separator and _key_ends_at(text, visible, quoted=True))
    if _key_ends_at(text, visible):
        # A credential flag's value, which the header or other rule that takes the
        # flag redacts with it, may be on a later line (`_secret_flag`,
        # `_header_redaction`), as daemon.log's folded line reads it.
        return not separator and not (following not in ("", "-") and _flag_ends_at(text, visible))
    if _last_word(text, visible).lower().endswith("bearer"):
        return not (following and _BEARER_TOKEN_CHAR_RE.fullmatch(following))
    return True


#: A character `_BEARER_RE` reads as part of a token (it ignores case too).
_BEARER_TOKEN_CHAR_RE = re.compile(r"(?i)[A-Za-z0-9._~+/-]")
_WHITESPACE_RUN_RE = re.compile(r"\s*")


def _last_word(text: str, end: int) -> str:
    """The last 17 characters (at most) of the identifier ending at `end`, as
    `_scrub_named_values` reads a key; "" if none ends there."""
    if end < 0 or not _identifier_char(text[end]):
        return ""
    start = end
    while start > 0 and end - start < 16 and _identifier_char(text[start - 1]):
        start -= 1
    return text[start:end + 1]


def _key_ends_at(text: str, end: int, *, quoted: bool = False) -> bool:
    """Whether an identifier the scrubber reads as a key ends at `end`; `quoted`:
    whether one ends before the quote at `end` and the backslashes escaping it, as
    `_ASSIGN_SEPARATOR_RE` reads a key's closing quote (`"password"`, `\\'password\\'`)."""
    if quoted:
        end -= 1
        while end >= 0 and text[end] == "\\":
            end -= 1
    return bool(_SENSITIVE_SUFFIX_RE.search(_last_word(text, end)))


def _head_cut(text: str) -> int:
    """The end of the head excerpt: the last neutral line break within
    EXCERPT_CHARS (0 if none). Breaks between the same two visible characters
    share one verdict, so a refused one is passed over with the rest of them."""
    end = EXCERPT_CHARS
    while (newline := text.rfind("\n", 0, end)) >= 0:
        visible = _last_visible(text, newline + 1)
        if _neutral_break(text, visible, _WHITESPACE_RUN_RE.match(text, newline + 1).end()):
            return newline + 1
        end = visible
    return 0


def _tail_cut(text: str) -> int:
    """The start of the tail excerpt: the first neutral line break in the last
    EXCERPT_CHARS (the end of the text if none)."""
    at = text.find("\n", len(text) - EXCERPT_CHARS - 1)
    while at >= 0:
        after = _WHITESPACE_RUN_RE.match(text, at + 1).end()
        if _neutral_break(text, _last_visible(text, at + 1), after):
            return at + 1
        at = text.find("\n", after)
    return len(text)


#: A data URI's prefix, as `_DATA_URI_RE` reads it; its body is the run of
#: `_DATA_BODY_RE` after it, line breaks included.
_DATA_URI_PREFIX_RE = re.compile(r"(?i)data:[a-z0-9.+/-]+(?:;[a-z0-9=.+/-]+)*;base64,")
_DATA_BODY_RE = re.compile(r"[A-Za-z0-9+/=\s]+")
#: A quote and the run of backslashes right before it. `_quoted_ends` pairs a quote
#: after an even run (none included) with the next such quote of its kind;
#: `_escaped_quote_ends`, one after a run of one or more with the next after a run
#: of the same length. A match starts only where a run starts (the lookbehind): without
#: it, `finditer` read a run no quote follows again from each of its backslashes, so a
#: run of 8,000 cost the cut 0.7 s (review r5 of #233, P3-1). Each character is then
#: read a fixed number of times, and a run is matched whole, as before.
_QUOTE_RUN_RE = re.compile(r"(?<!\\)(\\*)([\"'])")
#: Either reminder tag (`_REMINDER_START_RE`, `_REMINDER_END_RE`). `cut_whole` runs it
#: only where C scans found a tag (`_holds_reminder_tag`), never over the whole text.
_REMINDER_TAG_RE = re.compile(r"</?system-reminder>", re.IGNORECASE)
#: The characters other than ASCII letters' cases that `_REMINDER_TAG_RE`, which ignores
#: case, takes for one of the tag's letters. With these replaced, `str.lower` keeps every
#: position and lowers a text to the tag exactly where the regex matches one (a test
#: checks every code point).
_REMINDER_FOLDS = (("İ", "i"), ("ı", "i"), ("ſ", "s"))
#: The characters of a header's name, as `_HEADER_RE` reads one (`_HEADER_NAME`, which
#: ignores case, so `ſ`, `ı`, `İ` and the Kelvin sign are among them; a test checks every
#: code point): `_leads_with_header` finds the name's end by a C scan of these, then
#: matches its word (`_HEADER_WORD_RE`) in at most 13 characters before that end.
_HEADER_NAME_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz\u0130\u0131\u017f\u212a"
#: A private key's first and last lines as `_PEM_RE` reads them once the text is
#: folded to one line: each of their spaces may be any run of whitespace, line
#: breaks included (`-----BEGIN RSA\nPRIVATE KEY-----`). A test checks them against
#: `_PEM_START_RE` and `_PEM_END_RE` on folded text.
_PEM_START_FOLDED_RE = re.compile(r"-----BEGIN(?:\s+[A-Z0-9]+)*\s+PRIVATE\s+KEY(?:\s+BLOCK)?-----")
_PEM_END_FOLDED_RE = re.compile(r"-----END(?:\s+[A-Z0-9]+)*\s+PRIVATE\s+KEY(?:\s+BLOCK)?-----")
#: What may still become a private key's first line (`_PEM_START_FOLDED_RE`) when more is
#: written: `-----BEGIN` and uppercase words, with whitespace (line breaks too) between and
#: after them. `settled_end` reads it from a text's last `-----BEGIN` to the text's end.
_PEM_START_PREFIX_RE = re.compile(r"-----BEGIN(?:\s++[A-Z0-9]++)*+\s*+")
#: The margin Python's traceback writes before each line of an exception group and
#: its members (`daemon_log._GROUP_MARGIN`): two spaces a level, then `|` or `+`, then
#: a space or the line's end. The daemon's block form also reads a group's text with
#: the margins removed, so the cut reads each margin's `|` or `+` as a space.
_MARGIN_MARK_RE = re.compile(r"(?m)^((?:  )+)[|+](?= |$)")
#: C-25.7: the most distinct escaped quote kinds (a quote and the length of the run of
#: backslashes before it) one side of `cut_whole` reads. A side whose window holds
#: more keeps only the marker: each kind open at a candidate cut costs a look past
#: the window, and this is what bounds them.
CUT_KINDS_MAX = 8
#: How many quotes of its character (for an escaped kind, runs at least its length)
#: `_event_from` and `_event_before` read past a window, and how long a run of
#: backslashes they count, before they call what lies there unknown. No cut crosses
#: what is unknown.
_LOOK = 64
#: The longest identifier `_flag_ends_at` reads back to find a flag's `--`.
_WORD_LOOK = 256


def cut_marker(omitted: int) -> str:
    """The line `cut_whole` puts where it left `omitted` characters out."""
    return f"… [{omitted:,} characters omitted] …"


def cut_whole(text: str, limit: int) -> str:
    """`text` cut to whole lines from its head and its tail, each at most `limit // 2`
    characters, joined by a line saying how many characters were left out, for a
    caller that scrubs the result whole (`scrub_secrets(..., whole=True)`), folded
    and not, and bounds that work (C-25.7: the daemon's handler, a record at a time).

    No cut falls inside a match the whole-text scrub of `text` could make, folded
    to one line or not, so the cut text, scrubbed either way, shows nothing of its
    head or tail that `text` scrubbed the same way hides. A cut is at a line break:
    - that `_neutral_break` allows, as `scrub_bounded`'s are: no key, `Bearer` or
      separator before it whose match continues after it;
    - that folding would not join into a match no line break allows (`_folds_to_value`):
      a credential flag before it, as the flags rule reads one (`_flag_start`), and
      the start of a value after it, as its `_FLAG_VALUE_RE` reads one: folded,
      `--password\\nSECRET` is `--password SECRET`;
    - outside any quoted value, which a whole-text scrub masks across lines to the
      next quote of its kind (`_quoted_ends` and `_escaped_quote_ends` with
      `multiline`). Read loosely: a quote after a `:` or `=` may open one;
    - outside any private key, which the head never starts and the tail never
      ends, its first and last lines read as folding lets `_PEM_RE` read them,
      and outside any data URI's body;
    - with each exception group's margin read as spaces (`_Reading`), as the block
      form's margin-free reading of a group reads it;
    - and a text holding a reminder tag is not cut at all: stripping a reminder
      joins what was on either side of it, which can make a quote open a value. (A
      record keeps this rule; what a person reads of daemon.log is read with its
      reminders resolved before it is cut where that leaves the scrub's first pass as
      it was, `daemon_log.scrubbed_tail`.)
      Nor is one that opens with a header when the head would keep none of it:
      folded to one line, as the line form folds it, all of it is the header's.
    `scrub_bounded` needs only the first: it resolves the blocks over the whole
    text first and scrubs its excerpts within lines, where no quoted value
    crosses a line break, and it never folds.

    The work is bounded whatever the text's length (reviews r2 and r3 of #233).
    Every regex runs within the two windows (`limit // 2` characters each, and the
    lines that hold their edges) or at a fixed position; counted by what each call
    reads, they read at most 16 times the bound in all (review r5: `_QUOTE_RUN_RE`
    starts only where a run of backslashes starts). Past the windows the cut
    reads only in C scans of `str` and `bytes`, each linear in what it reads: a
    fixed number of scans of the text, among them, where a look past a window meets
    a group's margins, a few scans of each run of lines that hold only whitespace and
    margins, each run read once (`_Reading`); plus at most one needle look for each
    quote kind open at a candidate cut (`_event_from`, `_event_before`; two plain
    kinds and at most `CUT_KINDS_MAX` escaped ones a side, so at most 20), each at
    most `_LOOK` finds of its quote, which together read the text at most once
    (review r4 of #233). A side whose window holds more escaped kinds, or with no
    such break in reach, keeps nothing: one long line keeps only the line saying so."""
    if len(text) <= limit:
        return text
    if _holds_reminder_tag(text):
        return f"{cut_marker(len(text))}\n"
    half = limit // 2
    newline = text.find("\n", len(text) - half - 1)
    reading = _Reading(text, half + 1, len(text) if newline < 0 else newline + 1)
    head = _whole_head_cut(reading, half)
    tail = max(head, _whole_tail_cut(reading, len(text) - half))
    if reading.after(0) >= head and _leads_with_header(reading):
        # Folded to one line, a text that opens with a header is all that header's
        # value (`_HEADER_RE`): a tail without the header before it keeps nothing.
        head, tail = 0, len(text)
    return f"{text[:head]}{cut_marker(tail - head)}\n{text[tail:]}"


def _holds_reminder_tag(text: str) -> bool:
    """Whether `_REMINDER_TAG_RE` matches anywhere in `text`, in C scans: every tag
    ends `r>` in some case; lowered after `_REMINDER_FOLDS`, which keeps every
    position, the text holds the tag in lower case exactly where the regex matches,
    and the regex confirms the first one found, within its length."""
    if "r>" not in text and "R>" not in text:
        return False
    folded = text
    for odd, plain in _REMINDER_FOLDS:
        if odd in folded:
            folded = folded.replace(odd, plain)
    folded = folded.lower()
    for tag in ("<system-reminder>", "</system-reminder>"):
        at = folded.find(tag)
        if at >= 0:
            # Never false where the folds are complete, which a test checks; the
            # whole-text search keeps the answer exact if they were not.
            return (_REMINDER_TAG_RE.match(text, at, at + len(tag)) is not None
                    or _REMINDER_TAG_RE.search(text) is not None)
    return False


def reminder_tags(text: str) -> list[tuple[int, int, bool]]:
    """Every reminder tag `_REMINDER_TAG_RE` matches in `text`, in order, as `(start, end,
    opens)`: found as `_holds_reminder_tag` finds the first, by C scans of the text lowered
    after `_REMINDER_FOLDS`, and each confirmed by the regex within its length (C-25.7: what
    a person reads of daemon.log never shows a tag, `daemon_log.neutralised`)."""
    if "r>" not in text and "R>" not in text:
        return []
    folded = text
    for odd, plain in _REMINDER_FOLDS:
        if odd in folded:
            folded = folded.replace(odd, plain)
    folded = folded.lower()
    found = []
    for tag, opens in (("<system-reminder>", True), ("</system-reminder>", False)):
        at = folded.find(tag)
        while at >= 0:
            found.append((at, at + len(tag), opens))
            at = folded.find(tag, at + len(tag))
    found.sort()
    if all(_REMINDER_TAG_RE.match(text, start, end) is not None for start, end, _ in found):
        return found
    # Never where the folds are complete, which a test checks; the regex keeps the answer exact.
    return [(match.start(), match.end(), match.group()[1] != "/") for match in _REMINDER_TAG_RE.finditer(text)]


def quoted_value_spans(text: str) -> list[tuple[int, int, str]]:
    """What the first pass of `scrub_secrets(..., whole=True)` replaces in `text`, in order:
    `(start, end, out)`, `text[start:end]` replaced by `out`. That pass masks quoted values
    across lines (`_scrub_named_values(..., quoted=True, multiline=True)`) before reminders
    are stripped, so a tag inside one is no tag to that strip (review r4 of #233), and what
    it replaces is the same whatever the rest of the text holds only if these are (review
    r5, P2-1: `daemon_log._reminders_resolved`). One pass of that rule over `text`."""
    spans: list[tuple[int, int, str]] = []
    try:
        _scrub_named_values(text, lambda _matched, out: out, quoted=True, multiline=True, spans=spans)
    finally:
        _forget_ends_early()
    return spans


def _leads_with_header(reading: "_Reading") -> bool:
    """Whether the text's first visible word is a header's name with a `:` as the
    next visible character, as `(?i)\\s*[A-Za-z0-9_-]*(?:authorization|cookie)\\s*:`
    (`_HEADER_RE`'s name, C-23.14) matches the text with its margins read as spaces:
    the name's characters from the first visible one, read by a C scan
    (`_HEADER_NAME_CHARS`), its word matched within its last 13, and a look past it,
    where the regex's `\\s*` would have backtracked over every leading space."""
    text = reading.text
    start = reading.after(0)
    end = _run_end(text, start, _HEADER_NAME_CHARS)
    if _HEADER_WORD_RE.search(text, max(start, end - len("authorization")), end) is None:
        return False
    colon = reading.after(end)
    return text[colon:colon + 1] == ":"


def _blanked(text: str, start: int, end: int) -> str | None:
    """`text[start:end]`, `start` a line start, with each margin's mark read as a
    space (`_MARGIN_MARK_RE`), or None when it holds no margin. The regex runs on
    the slice and the one character after it, which a mark's lookahead reads."""
    part = text[start:end + 1]
    if "  |" not in part and "  +" not in part:
        return None
    return _MARGIN_MARK_RE.sub(r"\1 ", part)[:end - start]


def _is_margin_mark(text: str, at: int) -> bool:
    """Whether the `|` or `+` at `at` is a margin's, as `_MARGIN_MARK_RE` reads one:
    after an even run of two or more spaces that starts its line, and before a
    space, a line break or the end of the text."""
    if text[at] not in "|+" or text[at + 1:at + 2] not in ("", " ", "\n"):
        return False
    start = _run_start(text, at, " ")
    return at - start >= 2 and (at - start) % 2 == 0 and (start == 0 or text[start - 1] == "\n")


def _class_table(*, body: bool) -> bytes:
    """`_char_classes`' table: a space, a line break and `|` stand for themselves,
    any other whitespace is `w`. `+` is `|` too, a margin's other mark, unless `body`,
    where it is `o` with the rest of a data URI's body characters (`_DATA_BODY_RE`).
    Every other character is `x`."""
    table = bytearray(b"x" * 256)
    for char in _SPACES:
        if char < "\x80":
            table[ord(char)] = ord("w")
    for char in " \n|":
        table[ord(char)] = ord(char)
    if body:
        for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=":
            table[ord(char)] = ord("o")
    else:
        table[ord("+")] = ord("|")
    return bytes(table)


_MARGIN_CLASSES = _class_table(body=False)
_BODY_CLASSES = _class_table(body=True)
#: `|` as a tab, for `bytes.expandtabs`, which counts each one's column.
_BAR_TABS = bytes.maketrans(b"|", b"\t")


def _char_classes(text: str, start: int, end: int, table: bytes) -> bytes | None:
    """`text[start:end]` as one class byte a character (`_class_table`), in C scans;
    None if it holds a character outside ASCII that is not whitespace, which neither
    reading allows."""
    part = text[start:end]
    if not part.isascii():
        for char in _SPACES:
            if char >= "\x80" and char in part:
                part = part.replace(char, "\t")
        if not part.isascii():
            return None
    return part.encode("ascii").translate(table)


def _bars_are_marks(classes: bytes) -> bool:
    """Whether every `|` of `classes` (`_char_classes` of whole lines, ending in a line
    break) is a margin's mark: the first visible character of its line after an even
    run of two or more spaces, then a space or the line's end. C scans only: with each
    line's spaces removed, each bar must follow a line break; with each bar a tab,
    `expandtabs(2)` adds one character for each at an even column."""
    bars = classes.count(b"|")
    if not bars:
        return True
    if b"||" in classes or b"|w" in classes or b"|o" in classes or b"|x" in classes \
            or b"\n|" in classes or classes.startswith(b"|"):
        return False
    bare = classes.translate(None, b" ")
    if bare.count(b"|") != bare.count(b"\n|") + bare.startswith(b"|"):
        return False
    columns = classes.translate(_BAR_TABS)
    return len(columns.expandtabs(2)) - len(columns) == bars


def _margin_lines(text: str, start: int, end: int) -> bool:
    """Whether the whole lines `text[start:end]` (`start` a line start, `end` just past
    a line break) hold only whitespace and margins' marks: all whitespace with the
    marks read as spaces. C scans only."""
    classes = _char_classes(text, start, end, _MARGIN_CLASSES)
    return classes is not None and b"x" not in classes and _bars_are_marks(classes)


def _body_between(text: str, start: int, end: int) -> bool:
    """Whether `text[start:end]` (`end` a line start) is all a data URI's body
    characters (`_DATA_BODY_RE`) with the margins' marks read as spaces: no other
    character, and each `|` a mark (a `+` is a body character either way). C scans
    only; on a first line `start` cuts, only its first bar can be a mark."""
    if start >= end:
        return True
    classes = _char_classes(text, start, end, _BODY_CLASSES)
    if classes is None or b"x" in classes:
        return False
    if start and text[start - 1] != "\n":
        line = classes.find(b"\n") + 1     # `end` is a line start: the line ends before it
        bars = classes.count(b"|", 0, line)
        if bars > 1 or (bars and not _is_margin_mark(text, start + classes.index(b"|"))):
            return False
        classes = classes[line:]
    return _bars_are_marks(classes)


class _Reading:
    """`text` as `cut_whole` reads it: each exception group's margin read as spaces (its
    `|` or `+`, as `_MARGIN_MARK_RE` finds them), as the daemon's block form also reads a
    group's text without its margins.

    `read` is that reading in the two windows the cut reads, blanked there by the regex
    (`_blanked`): the head's first `head_end` characters and the tail from `tail_start`, a
    line start. Between them it is `text`. A look past a window (`before`, `after`) knows
    a margin where it meets one (`_is_margin_mark`), and passes the run of whole lines
    after it that hold only whitespace and margins in C scans (`_margin_lines`) of growing
    then shrinking spans, so their cost is linear in the run, however many lines it
    holds; each run found is kept (`spans`), so no later look reads it again."""

    def __init__(self, text: str, head_end: int, tail_start: int):
        self.text = text
        self.spans: list[tuple[int, int]] = []
        head, tail = _blanked(text, 0, head_end), _blanked(text, tail_start, len(text))
        self.read = text if head is None and tail is None else "".join((
            text[:head_end] if head is None else head, text[head_end:tail_start],
            text[tail_start:] if tail is None else tail))

    def before(self, end: int) -> int:
        """The last visible character before `end` (-1: none), as `_last_visible`
        finds it in the text with its margins read as spaces."""
        read, text = self.read, self.text
        while True:
            at = _run_start(read, end) - 1
            if at < 0 or read[at] not in "|+" or not _is_margin_mark(text, at):
                return at
            end = self._back(text.rfind("\n", 0, at) + 1)

    def after(self, start: int) -> int:
        """The first visible character at or after `start` (`len(text)`: none), as
        `_run_end` finds it in the text with its margins read as spaces."""
        read, text = self.read, self.text
        while True:
            at = _run_end(read, start)
            if at >= len(read) or read[at] not in "|+" or not _is_margin_mark(text, at):
                return at
            ahead = _run_end(read, at + 1)
            newline = text.find("\n", at, ahead)
            if newline < 0:
                return ahead                # on the mark's line: a bar there is no margin's
            start = self._forward(newline + 1)

    def _back(self, end: int) -> int:
        """The first line start from which every line up to `end`, a line start, holds
        only whitespace and margins."""
        text, good = self.text, self._known(end, back=True)
        step, growing = 64, True
        while good > 0 and step:
            reach = max(0, good - step)
            newline = text.find("\n", reach - 1, good - 1) if reach else -1
            start = 0 if not reach else (good if newline < 0 else newline + 1)
            if start < good and _margin_lines(text, start, good):
                good, step = start, step * 2 if growing else step // 2
            elif growing and start == good and reach:
                step *= 2                   # the line before is longer than the step
            else:
                growing, step = False, step // 2
        if good < end:
            self.spans.append((good, end))
        return good

    def _forward(self, start: int) -> int:
        """The last line start up to which every line from `start`, a line start, holds
        only whitespace and margins."""
        text, good = self.text, self._known(start, back=False)
        step, growing = 64, True
        while good < len(text) and step:
            reach = min(len(text), good + step)
            newline = text.rfind("\n", good, reach)
            end = good if newline < 0 else newline + 1
            if end > good and _margin_lines(text, good, end):
                good, step = end, step * 2 if growing else step // 2
            elif growing and end == good and reach < len(text):
                step *= 2                   # the line is longer than the step
            else:
                growing, step = False, step // 2
        if good > start:
            self.spans.append((start, good))
        return good

    def _known(self, at: int, *, back: bool) -> int:
        """`at` moved past the runs of margin lines already found that it is inside."""
        moved = True
        while moved:
            moved = False
            for begin, end in self.spans:
                if back and begin < at <= end:
                    at, moved = begin, True
                elif not back and begin <= at < end:
                    at, moved = end, True
        return at


def _newlines(text: str, start: int, end: int):
    """The positions of the line breaks in [start, end)."""
    at = text.find("\n", max(0, start), end)
    while at >= 0:
        yield at
        at = text.find("\n", at + 1, end)


def _around(reading: _Reading, starts: list[int]) -> tuple[list[int], list[int]]:
    """For each of `starts`, line starts in ascending order within one window: the last
    visible character before it and the first at or after it (`len(text)`: none), as
    `_neutral_break` takes them, margins read as spaces. One pass over the lines between
    them, and one look past each end: the starts in one run of blank lines share both,
    and are not read again each."""
    read = reading.read
    visible, seen = [], reading.before(starts[0])
    for index, at in enumerate(starts):
        if index:
            previous = starts[index - 1]
            kept = len(read[previous:at].rstrip())
            if kept:
                seen = previous + kept - 1
        visible.append(seen)
    after, ahead = [0] * len(starts), reading.after(starts[-1])
    for index in range(len(starts) - 1, -1, -1):
        if index < len(starts) - 1:
            at, following = starts[index], starts[index + 1]
            lead = (following - at) - len(read[at:following].lstrip())
            if at + lead < following:
                ahead = at + lead
        after[index] = ahead
    return visible, after


def _whole_break(reading: _Reading, visible: int, after: int, verdicts: dict) -> bool:
    """Whether a line break between `visible` and `after` can be `cut_whole`'s cut.
    Breaks between the same two visible characters (a run of blank lines) share one
    verdict, kept in `verdicts`."""
    verdict = verdicts.get((visible, after))
    if verdict is None:
        text = reading.text
        verdict = verdicts[visible, after] = (_neutral_break(text, visible, after, reading.before)
                                              and not _folds_to_value(text, visible, after))
    return verdict


def _folds_to_value(text: str, visible: int, after: int) -> bool:
    """Whether folding the text to one line, as both of the daemon's forms do before
    they scrub again, would join what ends at `visible` and what starts at `after`
    into a match no line break allows. The rules decide, not a list: a rule whose key
    and value a line break may part is `_neutral_break`'s; folding puts one space in
    the break, and only the flags rule then matches where it did not, a credential
    flag (`_flag_start`) followed by `[ \\t]+` and its value (`_FLAG_VALUE_RE`). (A
    private key's split first and last lines are the other join folding makes; the
    cut reads those lines folded. A test finds every rule's.)"""
    return (after < len(text) and _FLAG_VALUE_RE.match(" " + text[after]) is not None
            and _flag_ends_at(text, visible))


def _flag_ends_at(text: str, end: int) -> bool:
    """Whether an identifier the flags rule reads as a credential flag ends at `end`.
    Its last `--` is read back at most `_WORD_LOOK` characters; one longer than that
    without a `--` in reach reads as a flag when its ending is a sensitive word."""
    if end < 0 or not _identifier_char(text[end]):
        return False
    start = end
    while start > 0 and end - start < _WORD_LOOK and _identifier_char(text[start - 1]):
        start -= 1
    word = text[start:end + 1]
    if start == 0 or not _identifier_char(text[start - 1]) or "--" in word:
        return _flag_start(word) >= 0
    return bool(_SENSITIVE_SUFFIX_RE.search(word[-17:]))


def _quote_events(reading: _Reading, start: int, end: int):
    """Each quote in [start, end) (`start` a line start, so each run is whole):
    `(position, kinds, opens)`. `kinds` are the kinds it pairs as, `("plain",
    quote)` and `("escaped", run, quote)`; `opens` is the kind it may open a value
    of, when a `:` or `=` precedes it (a key is not read: any may be one)."""
    text = reading.text
    for match in _QUOTE_RUN_RE.finditer(text, start, end):
        run, quote = len(match.group(1)), match.group(2)
        kinds = ((("plain", quote),) if run % 2 == 0 else ()) + ((("escaped", run, quote),) if run else ())
        before = reading.before(match.start())
        opens = (("plain", quote) if run == 0 else ("escaped", run, quote)) \
            if before >= 0 and text[before] in ":=" else None
        yield match.start(2), kinds, opens


def _too_many_kinds(events: list) -> bool:
    """Whether a window's quotes are of more than `CUT_KINDS_MAX` escaped kinds."""
    return len({kind for _, kinds, _ in events for kind in kinds if kind[0] == "escaped"}) > CUT_KINDS_MAX


def _needle(kind: tuple) -> str:
    """What every quote of `kind` ends: its quote, after a run of at least its length."""
    return kind[-1] if kind[0] == "plain" else "\\" * kind[1] + kind[-1]


def _run_before(text: str, quote: int) -> int | None:
    """The run of backslashes right before the quote at `quote`; None past `_LOOK`."""
    run = 0
    while run <= _LOOK and quote - run > 0 and text[quote - run - 1] == "\\":
        run += 1
    return None if run > _LOOK else run


def _of_kind(kind: tuple, run: int) -> bool:
    return run % 2 == 0 if kind[0] == "plain" else run == kind[1]


def _event_from(text: str, kind: tuple, start: int) -> bool | None:
    """Whether a quote of `kind` lies at or after the line start `start`: None when
    the first `_LOOK` quotes of its character there (runs at least its length) do
    not tell. At most `_LOOK` C scans, whatever the text's length."""
    needle, at = _needle(kind), start
    for _ in range(_LOOK):
        found = text.find(needle, at)
        if found < 0:
            return False
        run = _run_before(text, found + len(needle) - 1)
        if run is None:
            return None
        if _of_kind(kind, run):
            return True
        at = found + 1
    return None


def _event_before(reading: _Reading, kind: tuple, end: int) -> bool | None:
    """Whether the last quote of `kind` before `end` may open a value of it: None when
    it is too far back to tell (`_LOOK` quotes of its character, runs at least its
    length, or a run of backslashes longer), False when there is none. At most
    `_LOOK` C scans, whatever the text's length, and a look past each margin it meets."""
    text = reading.text
    needle, at = _needle(kind), end
    for _ in range(_LOOK):
        found = text.rfind(needle, 0, at)
        if found < 0:
            return False
        quote = found + len(needle) - 1
        run = _run_before(text, quote)
        if run is None:
            return None
        if _of_kind(kind, run):
            before = reading.before(quote - run)
            return before >= 0 and text[before] in ":=" and (kind[0] == "escaped" or run == 0)
        at = quote
    return None


def _within_spans(starts: list[int], spans) -> set[int]:
    """The starts that fall strictly inside any of `spans` (`(begin, finish)`)."""
    inside = set()
    for begin, finish in spans:
        inside.update(starts[bisect.bisect_right(starts, begin):bisect.bisect_left(starts, finish)])
    return inside


def _whole_head_cut(reading: _Reading, end: int) -> int:
    """The end of the head: the last line start within `end` no whole-text match
    crosses, folded or not (0 if none). Its regexes read `reading.read` within the
    head's window, the first `end + 1` characters."""
    text, read = reading.text, reading.read
    starts = [0, *(at + 1 for at in _newlines(text, 0, end))]
    last = starts[-1]
    key = _PEM_START_FOLDED_RE.search(read, 0, last)
    if key is not None:                     # a private key starts in reach: the head ends before it
        starts = [at for at in starts if at <= text.rfind("\n", 0, key.start()) + 1]
    if len(starts) == 1:
        return 0
    bodies = []
    for prefix in _DATA_URI_PREFIX_RE.finditer(read, 0, last):
        body = _DATA_BODY_RE.match(read, prefix.end(), last + 1)       # whether it passes `last` is enough
        bodies.append((prefix.start(), body.end() if body else prefix.end()))
    blocked = _within_spans(starts, bodies)
    # Which kinds of quote are open at each line start: the last quote of the kind
    # before it may open a value, which an event of the kind after it would close.
    events = list(_quote_events(reading, 0, last))
    if _too_many_kinds(events):
        return 0
    final = {}
    for position, kinds, _ in events:
        for kind in kinds:
            final[kind] = position
    open_at, opened, index = {}, {}, 0
    for at in starts:
        while index < len(events) and events[index][0] < at:
            position, kinds, opens = events[index]
            for kind in kinds:
                opened[kind] = opens == kind
            index += 1
        open_at[at] = [kind for kind, opens in opened.items() if opens]
    visible, after = _around(reading, starts)
    beyond: dict[tuple, bool | None] = {}   # each kind's event past the window, computed once
    verdicts: dict[tuple[int, int], bool] = {}
    for at, seen, ahead in zip(reversed(starts), reversed(visible), reversed(after)):
        if at == 0:
            return 0
        if at in blocked or not _whole_break(reading, seen, ahead, verdicts):
            continue
        closed = True
        for kind in open_at[at]:
            if final[kind] >= at:
                closed = False
                break
            if kind not in beyond:
                beyond[kind] = _event_from(text, kind, last)
            if beyond[kind] is not False:
                closed = False
                break
        if closed:
            return at
    return 0


#: The end of a data URI's prefix.
_BASE64_MARK_RE = re.compile(r"(?i);base64,")


def _whole_tail_cut(reading: _Reading, start: int) -> int:
    """The start of the tail: the first line start at or after `start` no whole-text
    match crosses, folded or not (the end of the text if none). Its regexes read
    `reading.read` within the tail's window, from that first line start."""
    text, read = reading.text, reading.read
    starts = [at + 1 for at in _newlines(text, start - 1, len(text))]
    if not starts:
        return len(text)
    first = starts[0]
    ends = [match.end() for match in _PEM_END_FOLDED_RE.finditer(read, first)]
    if ends:                                # a private key ends in reach: the tail starts after it
        stop = text.find("\n", ends[-1])
        starts = [at for at in starts if 0 <= stop < at]
        if not starts:
            return len(text)
    # A data URI's body: the run after its prefix. The run at `first` began before
    # it, right after the last character no body holds: it is a body when that is
    # the `,` of a `;base64,`, so the last `,` before `first` (a C scan) and nothing
    # but body characters after it (C scans, only then: `_body_between`).
    bodies = []
    for run in _DATA_BODY_RE.finditer(read, first):
        if run.start() == first:
            comma = text.rfind(",", 0, first)
            body = (comma >= 7 and _BASE64_MARK_RE.fullmatch(text, comma - 7, comma + 1) is not None
                    and _body_between(text, comma + 1, first))
        else:
            body = run.start() >= 8 and _BASE64_MARK_RE.fullmatch(text, run.start() - 8, run.start()) is not None
        if body:
            bodies.append((run.start(), run.end()))
    blocked = _within_spans(starts, ((begin - 1, finish) for begin, finish in bodies))
    # A quoted value may run into the tail across a line start when a quote of a
    # kind follows it and the last before it may open a value of that kind.
    events = list(_quote_events(reading, first, len(text)))
    if _too_many_kinds(events):
        return len(text)
    final = {}
    for position, kinds, _ in events:
        for kind in kinds:
            final[kind] = position
    visible, after = _around(reading, starts)
    before: dict[tuple, bool | None] = {}   # each kind's state before the window, computed once
    opened: dict[tuple, bool] = {}
    verdicts: dict[tuple[int, int], bool] = {}
    index = 0
    for at, seen, ahead in zip(starts, visible, after):
        while index < len(events) and events[index][0] < at:
            position, kinds, opens = events[index]
            for kind in kinds:
                opened[kind] = opens == kind
            index += 1
        if at in blocked or not _whole_break(reading, seen, ahead, verdicts):
            continue
        crossed = False
        for kind, position in final.items():
            if position < at:
                continue
            if kind in opened:
                state = opened[kind]
            else:
                if kind not in before:
                    before[kind] = _event_before(reading, kind, first)
                state = before[kind]
            if state is not False:
                crossed = True
                break
        if not crossed:
            return at
    return len(text)


def settled_end(text: str, start: int) -> int:
    """For a text still being written, whole lines so far (`daemon logs --follow`,
    C-25.7): the last line start after `start` (itself a line start) at which no match
    of the whole-text scrub, folded or not, could cross a cut, whatever text comes
    next; `start` if none. `cut_whole`'s rules, its margins read as spaces, with
    nothing known past the text and no window: a break whose next visible character
    is not yet written is judged for each character that could come (`:` and a token
    character, `_neutral_break`'s and `_folds_to_value`'s two kinds); a value a quote
    may open stays open until its kind's next quote; a private key until its last
    line, and a first line still being written (`-----BEGIN RSA`, then a line break,
    `PRIVATE KEY-----` not yet: review r4 of #233) until it can no longer become one;
    a data URI until a character its body cannot hold; and nothing after a reminder
    tag settles."""
    reading = _Reading(text, len(text), len(text))
    read = reading.read
    starts = [at + 1 for at in _newlines(text, start, len(text))]
    tag = _REMINDER_TAG_RE.search(text) if _holds_reminder_tag(text) else None
    if tag is not None:
        starts = [at for at in starts if at <= text.rfind("\n", 0, tag.start()) + 1]
    spans = []                              # what a later text could still continue, or does
    begins = list(_PEM_START_FOLDED_RE.finditer(read))
    for key in begins:
        end = _PEM_END_FOLDED_RE.search(read, key.end())
        spans.append((key.start(), len(text) + 1 if end is None else end.end()))
    # Only the last `-----BEGIN` can still start a first line: an earlier one has the last
    # one's `-` after it, which no word of a first line holds.
    first_line = read.rfind("-----BEGIN")
    if first_line >= 0 and _PEM_START_PREFIX_RE.fullmatch(read, first_line) is not None:
        spans.append((first_line, len(text) + 1))
    for prefix in _DATA_URI_PREFIX_RE.finditer(read):
        body = _DATA_BODY_RE.match(read, prefix.end())
        finish = prefix.end() if body is None else body.end()
        spans.append((prefix.start(), len(text) + 1 if finish == len(text) else finish))
    blocked = _within_spans(starts, spans)
    events = list(_quote_events(reading, 0, len(text)))
    opened, index, verdicts = {}, 0, {}
    settled = start
    if not starts:
        return settled
    visible, after = _around(reading, starts)
    for at, seen, ahead in zip(starts, visible, after):
        while index < len(events) and events[index][0] < at:
            position, kinds, opens = events[index]
            for kind in kinds:
                opened[kind] = opens == kind
            index += 1
        if at in blocked or any(opened.values()):
            continue
        if ahead < len(text):
            safe = _whole_break(reading, seen, ahead, verdicts)
        elif (safe := verdicts.get((seen, None))) is None:     # what follows is not written yet
            safe = verdicts[seen, None] = all(
                _neutral_break(text + following, seen, ahead, reading.before)
                and not _folds_to_value(text + following, seen, ahead) for following in ":A")
        if safe:
            settled = at
    return settled


class _Tally:
    """The values the rules replace: one credential two rules match counts once,
    and a header that held several values (`Cookie: a=...; b=...`) counts each."""

    def __init__(self) -> None:
        self.total = 0

    def __call__(self, matched: str, out: str, *, header: bool = False) -> str:
        if out.rstrip() == matched.rstrip():
            return out          # nothing replaced: a header leaving its key trims only its line's end
        # Count a credential matched by an earlier rule only once.
        earlier = any(mark in matched for mark in _PLACEHOLDERS)
        if header:
            self.total += _values_removed(matched, out) or (0 if earlier else 1)
        else:
            self.total += 1 if not earlier else min(1, _values_removed(matched, out))
        return out


def _apply_rules(text: str, rules, tally: _Tally) -> str:
    for pattern, replacement in rules:
        def replace(match, replacement=replacement, pattern=pattern):
            matched = match.group(0)
            out = replacement(match) if callable(replacement) else match.expand(replacement)
            return tally(matched, out, header=pattern is _HEADER_RE)

        if pattern is _BEARER_RE:
            text = _bearer_sub(text, replace, tally)
        elif pattern is _HEADER_RE:
            text = _header_sub(text, tally)
        else:
            text = _linear_sub(pattern, replace, text)
    return text


def _header_sub(text: str, tally: _Tally) -> str:
    """`_HEADER_RE.sub(_header_value, text)`, where a header's redaction may reach past
    its line (`_header_redaction`'s `reach`: a secret-named flag's folded value, a lone
    flag's, a refused `Bearer`'s next line). Each search starts where the last
    replacement ended, as `re.sub`'s do. The part of a header's name before its header
    word goes when it reads as a secret (`_secret_prefix`), but for its leading `-`, as
    the inline rule's does, so a name the inline rule cannot read, parted from its colon by a line break or
    other whitespace than spaces and tabs (`SECRET7…Cookie` and `: v` on the next line),
    loses it too (#239 review r4, P3-4).

    A header whose value would start on a later line than its colon, at that line's own
    header (one the inline rule or this rule reads there), leaves that line to it, as an
    inline header does (`_scrub_inline_headers`), and takes nothing: taken, its value
    would end before a lone flag that ends that header's line, whose value on the line
    after would then show (`Set-Cookie:`, `Cookie: --api-key` and `SECRET7…` on three
    lines; #239 review r5, P3-1)."""
    parts: list[str] = []
    cursor = 0
    while (match := _HEADER_RE.search(text, cursor)) is not None:
        start, value = match.start(), match.end(1)
        name = _VISIBLE_RE.search(text, start).start()
        colon = text.rfind(":", name, value)
        word = _secret_prefix(text, name, _last_visible(text, colon) + 1)
        if word is not None:
            name = _run_end(text, name, "-")    # the name's leading `-` stays (`_scrub_inline_headers`)
        line = text.rfind("\n", colon, value) + 1
        if line and (_header_name_at(text, value) or _HEADER_RE.match(text, line) is not None):
            if word is not None:
                parts.extend((text[cursor:name], tally(text[name:word], REDACTED)))
                cursor = word
            parts.append(text[cursor:line])
            cursor = line
            continue
        out, stop = _header_redaction(text, start, value, match.end(), reach=True)
        if word is None:
            parts.extend((text[cursor:start], tally(text[start:stop], out, header=True)))
        else:
            parts.extend((text[cursor:name], tally(text[name:word], REDACTED),
                          tally(text[word:stop], out[word - start:], header=True)))
        cursor = stop
    parts.append(text[cursor:])
    return "".join(parts)


def _bearer_sub(text: str, replace: Callable[[re.Match], str], tally: _Tally) -> str:
    """`_BEARER_RE.sub(replace, text)`, and for a run that ends in a secret-named
    credential flag, which the rule takes as a token (`_owning_flag`), the value
    daemon.log's folded line gives the flag, redacted too (`_secret_flag`). Each
    search starts where the last replacement ended, as `re.sub`'s do."""
    parts: list[str] = []
    cursor = 0
    while (match := _BEARER_RE.search(text, cursor)) is not None:
        parts.extend((text[cursor:match.start()], replace(match)))
        cursor = match.end()
        if match.end() == match.start():
            break                               # never: the pattern reads `Bearer` and a run
        fold = _secret_flag(text, match.start(1), match.end(1)) if match.end() == match.end(1) else None
        if fold is not None and _bearer_refusal(match) is None:
            parts.extend((text[cursor:fold[0]], tally(text[fold[0]:fold[1]], REDACTED)))
            cursor = fold[1]
    parts.append(text[cursor:])
    return "".join(parts)


def _header_value(match: re.Match) -> str:
    """The line-start header rule's replacement: the rest of the header's line,
    redacted (`_header_redaction`)."""
    return _header_redaction(match.string, match.start(), match.end(1), match.end())[0]


def _header_redaction(text: str, start: int, value_start: int, end: int, *,
                      rescan: bool = False, reach: bool = False) -> tuple[str, int]:
    """A header's replacement for `text[start:end]`, whose value is
    `text[value_start:end]`: the value redacted whole. A credential key, flag or
    `Bearer` that ends the value and owns a value past it (`Authorization:
    client_secret=` and the secret on the next line; `Cookie: x --password` and the
    password on the next line, which daemon.log's folded line brings onto the
    flag's; `sent Authorization: Bearer` and the token on the next line, which the
    `Bearer` rule reads after the inline rule) is left to its own rule with what
    follows it (C-23.14): taken, it would leave its value to no rule.
    What precedes it is redacted as before; a header holding only the key or the
    flag is left whole, and those rules read it (the header's own name is a key
    too). The trailing whitespace before `end` goes either way, as the redaction
    always took it, and a space parts the redaction from a key that adjoined what it
    hid (`x.token=`, `SECRET7…token=`), so the header's own value ends there and a
    scrubbed line scrubs to itself.

    Returns the replacement and where in `text` it stops: `end`, or, for the inline
    rule (`rescan`), the start of a kept key that is a header's name (`…: x
    Authorization:` and the value on the next line, or a value that is only that
    name: `X-Authorization:` with `Authorization:` on the next line), which that rule
    then reads as a header of its own. The line-start rule keeps such a key in its
    replacement, or leaves a value that is only the key whole, and the inline rule's
    second scan reads it there, at a line start too (`_scrub`)."""
    last = _last_visible(text, end) + 1
    key = _key_past_line(text, value_start, end)
    # A credential flag or `Bearer` that ends the value owns a value past it when the
    # value ends at its line's end. When a closing quote ends it instead (the inline
    # rule's, `_inline_value_end`), only a flag that whitespace parts from the quote
    # does: it owns the quoted value after the quote, as with no header around it
    # (`-H 'Cookie: a=1 --password 'SECRET7…'`, #239 review r3, P3-C). A `Bearer` there
    # owns nothing, since its rule reads no token that starts with a quote, and nor
    # does a flag glued to the quote (`_owns_past`).
    quoted = end < len(text) and text[end] != "\n"
    if key is None and (end == len(text) or text[end] == "\n" or quoted):
        key = _owning_flag(text, value_start, last) if quoted else _owned_item(text, value_start, last)
        if key is None:
            joined = _secret_flag(text, value_start, last)
        elif not text.startswith("--", key):
            joined = _refused_bearer(text, key)
        elif _last_visible(text, key) < value_start:
            # The value is a credential flag alone. Kept, it would leave daemon.log's
            # folded line a header whose value runs to the record's end, past any
            # cut (`handoff.cut_whole`): the header takes the flag, and the value
            # that line gives it.
            joined, key = _fold_value(text, last), None
        else:
            joined = None
        if joined is not None:
            # A secret-named flag that ends the value is the header's, with the value
            # daemon.log's folded line gives it (`_secret_flag`); so is the line after
            # a `Bearer` ending it whose run there is a key (`_refused_bearer`), which
            # release/217 took by joining the two lines (#239 review r2). Only a rule
            # that can reach past the line takes them: the inline rule (`rescan`) and
            # the line-start rule as `_header_sub` applies it (`reach`); applied by
            # `re.sub`, the line-start rule leaves the header whole.
            if not (rescan or reach):
                return text[start:end], end
            # The value's own redaction takes the whitespace before its line's end, as
            # it always did, so the result scrubs to itself.
            return text[start:value_start] + REDACTED + text[end:joined[0]] + REDACTED, joined[1]
    if key is None:
        return text[start:value_start] + REDACTED, end
    before = _last_visible(text, key) + 1
    if before <= value_start:
        if rescan and _header_name_at(text, key):
            return text[start:key], key
        return text[start:last], end
    parted = text[before:key] if before < key else " "
    if rescan and _header_name_at(text, key):
        return text[start:value_start] + REDACTED + parted, key
    # Before a closing quote, the whitespace that parts a kept flag from the quote stays:
    # it is what lets the flag own the quoted value after it.
    return text[start:value_start] + REDACTED + parted + text[key:end if quoted else last], end


def _secret_prefix(text: str, name: int, end: int) -> int | None:
    """Where the header word that ends the header name `text[name:end]` starts, when
    the part of the name before that word holds a run that reads as a secret
    (`_value_runs`: `SECRET7…Authorization`), which goes whatever becomes of the
    header's value (#239 review r3, P3-B); None otherwise."""
    word = _HEADER_WORD_RE.search(text, max(name, end - 13), end)
    if word is None or word.start() <= name or not _value_runs(text[name:word.start()]):
        return None
    return word.start()


def _header_name_at(text: str, key: int) -> bool:
    """Whether the key at `key` (after the quote and backslashes that may open it) is
    a header's name: an identifier ending in a header word, then `[ \t]*:`."""
    opening = _VALUE_QUOTE_RE.match(text, key)
    name = _IDENTIFIER_RE.match(text, opening.end() if opening else key)
    return (name is not None and _HEADER_WORD_RE.search(text, max(name.start(), name.end() - 13), name.end()) is not None
            and _HEADER_COLON_RE.match(text, name.end()) is not None)


def _key_past_line(text: str, start: int, end: int) -> int | None:
    """Where the credential key ending `text[start:end]`, a value's span, begins when
    its separator (`_ASSIGN_SEPARATOR_RE`) reaches past `end` to a value; None
    otherwise. Only the span's last identifier can: between it and `end` the
    separator allows nothing but a quote, whitespace and `:` or `=`. The key is the
    identifier's credential word (`_SENSITIVE_SUFFIX_RE`), so a secret glued to it
    (`sid=SECRET7…token` and `=` on the next line) stays in the redaction; a key that
    is all one word keeps its opening quote and the backslashes escaping that."""
    at = _last_visible(text, end)
    if at >= start and text[at] in ":=":
        at = _last_visible(text, at)
    if at >= start and text[at] in "\"'":
        at -= 1
        while at >= start and text[at] == "\\":
            at -= 1
    if at < start or not _identifier_char(text[at]):
        return None
    key_end = at + 1
    while at > start and _identifier_char(text[at - 1]):
        at -= 1
    window = max(at, key_end - 17)
    word = _SENSITIVE_SUFFIX_RE.search(text[window:key_end])
    if word is None:
        return None
    separator = _ASSIGN_SEPARATOR_RE.match(text, key_end)
    if separator is None or separator.end() <= end or not (
            _PLAIN_VALUE_RE.match(text, separator.end()) or _VALUE_QUOTE_RE.match(text, separator.end())):
        return None
    if window + word.start() > at:
        return window + word.start()
    if at > start and text[at - 1] in "\"'":
        at -= 1
        while at > start and text[at - 1] == "\\":
            at -= 1
    return at


def _owns_past(text: str, end: int) -> bool:
    """Whether a credential flag ending at `end` may own a value past its match: a
    space, tab or line break follows it, or nothing does. The flag rule then takes a
    value on the flag's line (`_FLAG_VALUE_RE`), and daemon.log's folded line puts the
    next line's first word there; a closing quote or `,` after the flag owns nothing."""
    return end == len(text) or text[end].isspace()


def _ending_flag(text: str, start: int, end: int) -> int | None:
    """Where the credential flag (`--` and `_SENSITIVE_KEY`, as the flag rule reads
    one: the last `--` of the identifier ending at `end`) that ends `text[start:end]`
    begins, when it may own a value past `end` (`_owns_past`), whatever its name;
    None otherwise."""
    if not _owns_past(text, end) or not _SENSITIVE_SUFFIX_RE.search(text, max(start, end - 17), end):
        return None
    flag = text.rfind("--", start, end)
    if (flag < 0 or not _IDENTIFIER_RE.fullmatch(text, flag, end)
            or not _SENSITIVE_FLAG_RE.fullmatch(text, flag + 2, end)):
        return None
    return flag


def _owning_flag(text: str, start: int, end: int) -> int | None:
    """Where the credential flag that ends `text[start:end]` begins (`_ending_flag`),
    unless its name reads as a secret (`_value_runs`: `--SECRET7…_token`): a flag of
    that name owns nothing (#239 review r2, P2-1), and a rule whose match would take
    it takes it, with the value daemon.log's folded line would give it (`_secret_flag`).
    A rule whose match would take any other ends its match there instead, so the
    flag's value is the flag rule's exactly as it would be without that rule
    (C-23.14)."""
    flag = _ending_flag(text, start, end)
    if flag is None or _value_runs(text[flag + 2:end]):
        return None
    return flag


def _secret_flag(text: str, start: int, end: int) -> tuple[int, int] | None:
    """For a credential flag that ends `text[start:end]` and whose name reads as a
    secret (`_ending_flag`, `_value_runs`), which an enclosing rule takes whole
    (`_owning_flag`): the span of the value daemon.log's folded line gives it
    (`_fold_value`), which that rule redacts too (#239 review r2, the hub's decision:
    fail closed). None for any other end, or when the flag has no such value."""
    flag = _ending_flag(text, start, end)
    if flag is None or not _value_runs(text[flag + 2:end]):
        return None
    return _fold_value(text, end)


#: A run of visible characters: a flag's value once folding puts a space before it.
_VISIBLE_RUN_RE = re.compile(r"\S+")

#: Per thread, `line`: the last line on which a fold at a header's name found an item that
#: owns a value past the line (`_fold_value`): the text, that line's start, and where the
#: fold's span would have ended before the item. Every fold at a header's name on that
#: line before that point takes nothing, so later ones read it here instead of walking
#: the line's last identifier again, which made a line of such folds quadratic (#239 fix
#: round 4). Only that answer is kept. Each thread has its own, so a scrub on another
#: thread never overwrites it, and the two calls that fold, `_scrub` and
#: `quoted_value_spans` (which `daemon_log` calls outside `_scrub`), clear it when they
#: end, raising or not (`_forget_ends_early`), so no text outlives the call that read it
#: (#239 review r5, P3-3).
_ENDS_EARLY = threading.local()


def _forget_ends_early() -> None:
    """Drop this thread's `_ENDS_EARLY` answer, and the text it holds."""
    _ENDS_EARLY.line = None


def _fold_value(text: str, end: int) -> tuple[int, int] | None:
    """The value the flag rule reads for a flag ending at `end` once daemon.log's
    folded line joins what follows it (`_FLAG_VALUE_RE` on the folded text): the next
    run of visible characters past any whitespace, line breaks and blank lines
    included, unless it starts with `-`; None if there is none. A run that starts at a
    header's name (`_header_name_at`: `Cookie:x …` on the next line) is that header's
    whole line, to its last visible character: the run would take the name and leave
    the rest of the line to no rule, and with no run taken a name that holds a secret
    would show (#239 review r3, P2-B). Unless a credential key, flag or `Bearer` ends
    that line and owns a value past it (`_kept_span` would end the span before it):
    then the fold takes nothing, and the line is its own header's, whose rules read the
    item as they would with no fold before it (a header's lone flag takes the value on
    the line after; #239 review r4, P3-1). It is decided once per line
    (`_ENDS_EARLY`), as one line fold did before. A run that starts a header only the
    line-start rule reads there (`_parts_line_start_header`: `Set-Cookie` and `: v` on
    the next line), or one whose value that rule reads on a later line than the run's
    own, at a header's name too (`Set-Cookie :` and `;v` on the next line; #239 review
    r5, P2-1), takes nothing too, and that rule takes the header's value whole.
    Elsewhere such an item that ends the run is left to its own rule, as an enclosing
    rule leaves one: the span ends before it, and is None when that leaves nothing."""
    value = _run_end(text, end)
    if value == end or value >= len(text) or text[value] == "-":
        return None
    if _header_name_at(text, value):
        early = getattr(_ENDS_EARLY, "line", None)
        if early is not None and early[0] is text and early[1] <= value < early[2]:
            return None
        stop = text.find("\n", value)
        stop = _last_visible(text, len(text) if stop < 0 else stop) + 1
        if _parts_line_start_header(text, end, value, stop):
            return None
        span = _kept_span(text, value, stop)
        if span == (value, stop):
            return span
        _ENDS_EARLY.line = (text, text.rfind("\n", 0, value) + 1, value + 1 if span is None else span[1])
        return None
    stop = _VISIBLE_RUN_RE.match(text, value).end()
    if _parts_line_start_header(text, end, value, stop):
        return None
    return _kept_span(text, value, stop)


def _parts_line_start_header(text: str, end: int, run: int, stop: int) -> bool:
    """Whether a span from the run at `run` to `stop`, which whitespace holding a line
    break parts from `end`, would end before the colon of a header that the line-start
    rule reads at the run's line start (`_HEADER_RE`): a name its colon parts by
    whitespace other than spaces and tabs, which `_header_name_at` does not read
    (`Set-Cookie` and `: v` on the next line; past a fold's run, a vertical tab or a
    no-break space between them too). Taken, the span would end inside the name,
    before its header word (`_kept_span`), or take it whole, so no header rule would
    read the name and the plain rule would take at most the value's first word. Or
    the span would end before the value that rule reads on a later line, past its
    colon (`Set-Cookie :` and `;v` on the next line), and leave that value to no rule
    (#239 review r5, P2-1). A fold, or a refused `Bearer`'s line, takes nothing there,
    and the line-start rule takes the header's value whole, as both references do
    (#239 fix round 4)."""
    gap = text.rfind("\n", end, run)
    match = _HEADER_RE.match(text, gap + 1) if gap >= 0 else None
    return match is not None and (text.rfind(":", run, match.end(1)) >= stop or "\n" in text[stop:match.end(1)])


def _kept_span(text: str, start: int, stop: int) -> tuple[int, int] | None:
    """`(start, stop)`, ended before a credential key, flag or `Bearer` that ends it and
    owns a value past it (`_key_past_line`, `_owned_item`), which is left to its own
    rule as an enclosing rule leaves one, and before any credential flag that ends it
    and may own a value past it (`_ending_flag`), a flag whose name reads as a secret
    included: the flag rule takes that flag with the value daemon.log's folded line
    gives it, which a span that took only the flag would leave to no rule (#239 review
    r3, P2-A). None when that leaves nothing."""
    item = _key_past_line(text, start, stop)
    if item is None:
        item = _owned_item(text, start, stop)
    if item is None:
        item = _ending_flag(text, start, stop)
    if item is not None:
        stop = _last_visible(text, item) + 1
    return (start, stop) if stop > start else None


def _owned_item(text: str, start: int, last: int) -> int | None:
    """Where the credential flag (`_owning_flag`) or the `Bearer` (`_BEARER_END_RE`)
    that ends `text[start:last]` begins; None if neither does. `last` is the end of
    the last visible character before the line break that ends the enclosing value,
    so whitespace after the item hides neither (`x --password  ` and its value on the
    next line). A rule whose value runs to that break ends before the item, which
    keeps its own value past the line, exactly as with no enclosing rule (C-23.14):
    a header's (`_header_redaction`), a header name's the quoted rule swallows, and an
    unclosed quoted value's (`_scrub_named_values`)."""
    item = _owning_flag(text, start, last)
    if item is None and (bearer := _BEARER_END_RE.search(text, max(start, last - 6), last)):
        item = bearer.start()
    return item


def _refused_bearer(text: str, bearer: int) -> tuple[int, int] | None:
    """For a `Bearer` at `bearer` that ends a header's value: when `_BEARER_RE` reads a
    run after it that `_bearer_refusal` refuses, whole or in part (a key, not a token:
    `Authorization: Bearer` and `client_secret=…` on the next line), the span from that
    run to the end of its line, which the header takes (#239 review r2, check 2). None
    when the `Bearer` reads no run, or a token, which the `Bearer` rule takes; when the
    span would end before the colon of a header the line-start rule reads at the run's
    line start, or before that header's value (`_parts_line_start_header`: `Set-Cookie`
    and `: v` on the next line), whose value that rule takes whole; and when the run is
    a header's name (`_header_name_at`) and a key, flag or `Bearer` ends its line and
    owns a value past it (`_kept_span` would end the span before it: `Authorization:
    Bearer` and `Set-Cookie: --api-key` on the next line): then the line is that
    header's, whose rules take the item with its value as they would with no `Bearer`
    before it, as a fold at a header's name leaves its line (`_fold_value`), so a header
    whose whole value is a lone flag takes the value on the line after (#239 review r5,
    P3-1). Any other run's line still ends before such an item, which is left to its own
    rule (`_kept_span`: `Authorization: "Bearer` and `password=` on the next line, its
    value after that), so a word before it that no rule owns stays redacted
    (`client_secret=x SECRET7… --password`)."""
    match = _BEARER_RE.match(text, bearer)
    if match is None or _bearer_refusal(match) is None:
        return None
    stop = text.find("\n", match.start(1))
    stop = _last_visible(text, len(text) if stop < 0 else stop) + 1
    if _parts_line_start_header(text, bearer, match.start(1), stop):
        return None
    span = _kept_span(text, match.start(1), stop)
    if span != (match.start(1), stop) and _header_name_at(text, match.start(1)):
        return None
    return span


def _bearer_refusal(match: re.Match) -> int | None:
    """Whether `_BEARER_RE`'s run is a credential key a named-value rule reads
    rather than a token (C-23.14): it ends in a credential word followed by an
    assignment's separator (`_ASSIGN_SEPARATOR_RE`: `token_type: Bearer\\n
    access_token: …`, `scheme=bearer oauth.client_secret=…`), or in a credential flag
    that may own a value (`_owning_flag`: `Bearer --api-key …`). Taken, the key would
    leave its value to no rule. Both can end only where the run does, so one look at
    its end decides what the old lookahead decided by trying every prefix.

    None: the run is a token. Otherwise the end of what the rule still takes: the
    run before the credential word, its joiners (`_TOKEN_JOINERS`) aside, when that
    reads as a token itself (`_token_like`: `Bearer SECRET7…_token: …`), or the
    run's start (`Bearer secrets/db_password: …`), where the rule takes nothing. It
    takes nothing too where the run starts a header the line-start rule reads on a
    later line than `Bearer` (`_parts_line_start_header`: `Bearer`, then
    `SECRET7…Set-Cookie` and `: v`), whose name and value that rule takes (#239 fix
    round 4)."""
    text, start, end = match.string, match.start(1), match.end(1)
    word = _SENSITIVE_SUFFIX_RE.search(text, max(start, end - 17), end)
    flag = None
    if word is not None and _ASSIGN_SEPARATOR_RE.match(text, end):
        cut = word.start()
    elif (flag := _owning_flag(text, start, end)) is not None:
        cut = flag
    else:
        return None
    while cut > start and text[cut - 1] in _TOKEN_JOINERS:
        cut -= 1
    if _parts_line_start_header(text, match.start(), start, cut):
        return start
    if flag is not None:
        return cut           # a run glued to the flag goes, token or not (`Bearer SECRET7--secret x`)
    return cut if _token_like(text[start:cut]) else start


def _token_like(run: str) -> bool:
    """Whether the run before a refused `Bearer` key's credential word is a token of
    its own: at least the eight characters `_BEARER_RE` reads as one, with a letter
    and a digit, as a secret-looking run has (`_value_runs`). A key's own name before
    its word (`secrets/db_`, `oauth.`, `service_account_`) is not."""
    return len(run) >= 8 and _DIGIT_RE.search(run) is not None and _LETTER_RE.search(run) is not None


_DIGIT_RE = re.compile(r"[0-9]")
_LETTER_RE = re.compile(r"[A-Za-z]")


def _bearer_value(match: re.Match) -> str:
    """`_BEARER_RE`'s replacement: `Bearer [REDACTED]`, unless the run is a key
    (`_bearer_refusal`). A refused run is left as it is, or keeps only its key when
    the run before the key's word is a token: `Bearer [REDACTED]_token: …`."""
    cut = _bearer_refusal(match)
    if cut is None:
        return "Bearer " + REDACTED
    if cut == match.start(1):
        return match.group(0)
    text = match.string
    return text[match.start():match.start(1)] + REDACTED + text[cut:match.end()]


def _scrub_inline_headers(text: str, tally: "_Tally", *, line_start: bool = False) -> str:
    """The inline header rule (C-23.14): a header whose name does not start its line
    (`curl -H 'Authorization: Basic …'`, `wget --header='Cookie: …'`, a header in log
    prose, one that `refusals.detail_line` prefixes). Its name is an identifier ending
    in `authorization` or `cookie`, unquoted, then spaces or tabs and `:`. Its value
    starts at the first visible character after the colon, on the next line if none
    follows on its own, and ends at the closing quote matching one that opens right
    before the name (`_inline_value_end`), or else at its line's end. It is redacted
    whole and counted as a line-start header's (`_header_redaction`), unless it starts
    with `[REDACTED]`: a value already redacted is left alone, which keeps scrubbing
    idempotent and keeps the words daemon.log's folded line puts after a redacted
    header. `line_start`, for the second scan after the line-start rule (`_scrub`):
    a name that starts its line is read too, and so is one that starts its line after
    the `[REDACTED]` the prefix rule leaves of a secret part (`[REDACTED]Cookie:`),
    whose value goes as a line-start header's does, already redacted or not (#239
    review r4, P3-4). Every scan is linear: candidates come in
    order from one scan of the reversed text, each name's identifier is read back once,
    each gap between two candidates is searched once for the line break and the
    visible text before a name, and each value once for its closing quote and its
    line's end."""
    if ":" not in text:
        return text
    parts: list[str] = []
    cursor = scanned = 0
    line_end = -1
    escapes: dict[int, int] | None = None
    size, reverse = len(text), text[::-1]
    hits = [(size - hit.end(), size - hit.start()) for hit in _INLINE_HEADER_REVERSED_RE.finditer(reverse)]
    for start, hit_end in reversed(hits):
        gap, scanned = scanned, hit_end
        word = start
        if start > cursor and _identifier_char(text[start - 1]):
            start = max(cursor, start - (_IDENTIFIER_RE.match(reverse, size - start).end() - (size - start)))
        # A name that starts its line, after whitespace only, is the line-start rule's
        # (`_HEADER_RE`): there is a line break in the gap before it, or nothing at all
        # before it, and nothing visible between that and the name.
        if not line_start:
            newline = text.rfind("\n", gap, start)
            if (newline >= 0 or gap == 0) and _VISIBLE_RE.search(text, newline + 1, start) is None:
                continue
        if start < cursor:
            continue                            # inside the value just taken
        # The rule keeps a header's name, but a name whose part before its header word
        # reads as a secret (`_value_runs`: `SECRET7…Authorization:`) loses that part,
        # whatever becomes of its value (#239 review r3, P3-B). The second scan reads
        # names at a line start too, so a line-start header's loses it there. Its
        # leading `-` stays (`lead`): a fold refuses a run that starts with `-`, and the
        # flag rule's fold, the scrub's last pass, must see it as an excerpt cut before
        # it does (`_neutral_break`), so the whole text scrubs as its excerpts do (#239
        # review r5, P3-2).
        named = word > start and bool(_value_runs(text[start:word]))
        lead = _run_end(text, start, "-") if named else start
        value_start = _WHITESPACE_RUN_RE.match(text, hit_end).end()
        # A value at the text's end or already redacted is left alone, and so is one
        # that starts at the next line's own header (#239 review r2, P2-3), the inline
        # rule's or the line-start rule's, whose colon or value a line break may part
        # from its name (`Set-Cookie` and `:v` on the next line; #239 review r5, P2-1),
        # or one that a closing quote leaves empty. The second scan reads a name that the prefix
        # rule left starting with `[REDACTED]` at a line start as the line-start rule
        # reads a name there: its value goes, already redacted or not, so daemon.log's
        # folded line (`[REDACTED]Cookie: [REDACTED]` and the next line's words) keeps
        # nothing of the header's line (#239 review r4, P3-4); so does one the rule left
        # its leading `-` (`-[REDACTED]Cookie:`).
        led = line_start and start >= len(REDACTED) and text.startswith(REDACTED, start - len(REDACTED))
        if led:
            marker = max(gap, _run_start(text, start - len(REDACTED), "-"))
            led = (((newline := text.rfind("\n", gap, marker)) >= 0 or gap == 0)
                   and _VISIBLE_RE.search(text, newline + 1, marker) is None)
        skip = (value_start == len(text) or (text.startswith(REDACTED, value_start) and not led)
                or ("\n" in text[hit_end:value_start] and (_header_name_at(text, value_start) or _HEADER_RE.match(
                    text, text.rfind("\n", 0, value_start) + 1) is not None)))
        if not skip:
            if line_end < value_start:
                line_end = text.find("\n", value_start)
                if line_end < 0:
                    line_end = len(text)
            end = line_end
            if start and text[start - 1] in "\"'":
                if escapes is None:
                    escapes = {run.end(): len(run.group()) for run in _BACKSLASH_RUN_RE.finditer(text)
                               if text[run.end():run.end() + 1] in ("'", '"')}
                end = _inline_value_end(text, start - 1, value_start, line_end, escapes)
            skip = end <= value_start
        if skip:
            if named:
                parts.extend((text[cursor:lead], tally(text[lead:word], REDACTED)))
                cursor = word
            continue
        out, stop = _header_redaction(text, start, value_start, end, rescan=True)
        if named:
            out = text[start:lead] + REDACTED + out[word - start:]
        parts.extend((text[cursor:start], tally(text[start:stop], out, header=True)))
        cursor = stop
    parts.append(text[cursor:])
    return "".join(parts)


def _inline_value_end(text: str, opening: int, value_start: int, line_end: int,
                      escapes: dict[int, int]) -> int:
    """Where an inline header's value ends: at the closing quote matching the quote at
    `opening`, right before the header's name (the same kind of quote, escaped by a
    run of backslashes of the same length, `escapes` giving each escaped quote's:
    `-H '…'`, `--header="…"`, JSON's `\\"…\\"`), on the value's line, at the start of
    its backslashes; otherwise at `line_end`, the line's end."""
    quote, run = text[opening], escapes.get(opening, 0)
    at = value_start
    while (at := text.find(quote, at, line_end)) >= 0:
        if escapes.get(at, 0) == run:
            return at - run
        at += 1
    return line_end


#: The rules after reminders that can match across lines (`_resolve_blocks`).
_BLOCK_RULES = ((_PEM_RE, "[PRIVATE KEY REDACTED]"), (_DATA_URI_RE, "[BASE64 DATA OMITTED]"))
#: The token rules `_scrub` applies after the blocks and the quoted values, in order,
#: each over the whole text before the next. The inline header rule
#: (`_scrub_inline_headers`) reads the text before them and again right after the
#: line-start header rule (`_HEADER_RE`).
_TOKEN_RULES = (
    (_JWT_RE, REDACTED),
    (_PREFIXED_TOKEN_RE, REDACTED),
    (_BEARER_RE, _bearer_value),
    (_LONG_BASE64_RE, "[BASE64 OMITTED]"),
    (_HEADER_RE, _header_value),
    (_URL_PASSWORD_RE, lambda match: match.group(1) + REDACTED + match.group(3)),
)


def _resolve_blocks(text: str, strip_reminders: bool, tally: _Tally) -> str:
    """The scrub's first rules, which can match across lines: reminders, private
    keys, data URIs (wrapped base64). Each is a scan whose work is linear in the
    text (`_linear_sub`; the data URI pattern has no nested repetition), so a text
    of any length has them resolved whole, as `_scrub` resolves them. Applied
    again to what they leave, they change nothing."""
    if strip_reminders:
        text = _linear_sub(_SYSTEM_REMINDER_RE, "", text)
    return _apply_rules(text, _BLOCK_RULES, tally)


def _scrub(text: str, strip_reminders: bool, *, multiline: bool = False) -> tuple[str, int]:
    """`scrub_secrets` on a text within its work bound. The folds' per-line answer
    (`_ENDS_EARLY`) is dropped when it ends, raising or not."""
    try:
        return _scrub_rules(text, strip_reminders, multiline=multiline)
    finally:
        _forget_ends_early()


def _scrub_rules(text: str, strip_reminders: bool, *, multiline: bool = False) -> tuple[str, int]:
    """`_scrub`'s rules, in order."""
    tally = _Tally()
    if multiline:
        # Mask the original value before headers or reminders can consume its
        # closing quote. Whole-text callers never cut through these assignments.
        text = _scrub_named_values(text, tally, quoted=True, multiline=True)
    text = _resolve_blocks(text, strip_reminders, tally)
    if not multiline:
        # An excerpt's blocks are resolved over the whole text first; then mask
        # quoted values before a token rule can take their key (`Bearer
        # okPASSWORD='…'`), as the whole-text pass above does.
        text = _scrub_named_values(text, tally, quoted=True)
    # Inline headers first among the token rules: a `[REDACTED]` at the start of
    # an inline value is then one the text already held, never a JWT or prefixed
    # token this scrub just replaced with the rest of the value still to hide.
    text = _scrub_inline_headers(text, tally)
    for rule in _TOKEN_RULES:
        text = _apply_rules(text, (rule,), tally)
        if rule[0] is _HEADER_RE:
            # The inline rule's second scan, which reads names that start their line
            # too. It reads a header the first scan could not: a key a line-start
            # header keeps that is a header's name (`Cookie:` and `Set-Cookie:` on the
            # next line), a line-start header's value that is only such a name
            # (`X-Authorization:` and `Authorization:` on the next line), which
            # `_HEADER_RE` leaves whole, and a header a token rule made (`Bearer`
            # joining a value's line to its token's line, or cutting a token before
            # `Authorization:`). Each takes a header's value, as it would on its own
            # and as a second scrub would give it; a value that now starts with
            # `[REDACTED]` is left as the first scan leaves one, whichever rule put it
            # there. It always runs, so whether a line is read again never depends on
            # the rest of the text, and an excerpt scrubs as the whole does.
            text = _scrub_inline_headers(text, tally, line_start=True)
    text = _scrub_named_values(text, tally, quoted=True, multiline=multiline)
    text = _scrub_named_values(text, tally)
    text = _scrub_named_values(text, tally, flags=True)
    return text, tally.total


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
                              text=True, timeout=8, env=build_environment())
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
                override: str | None = None, *, git: bool = False) -> str:
    """The handoff's sandbox: the operator's `-s`, else the task's permission.

    v1 derived the task class from the original instruction text and let the
    dispatcher's `permissions` map decide. v2 keeps the map and drops the guess:
    a handoff with no `--task` is read-only, because a writable job carries
    consequences a text classifier should not choose (C-6.5 refuses one on
    `main`, and requires a committed repository).
    """
    return resolve_sandbox(policy, override or POLICY_SANDBOX, task=task, git=git)


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
    committed_choice = sandbox_for(policy, task, sandbox, git=True)
    # Only conditional worktree authority needs a committed-repository probe.
    if chosen != committed_choice:
        try:
            committed = git_head(target, timeout_s=policy.get("caps", {}).get(
                "workspace_git_timeout_s", DEFAULT_CAPS["workspace_git_timeout_s"])) is not None
        except SalvageError as exc:
            raise HandoffError("could not check the handoff workdir's Git commit",
                               code=Exit.OPERATIONAL,
                               fix="retry the handoff, or pass -s read-only") from exc
        if committed:
            chosen = committed_choice
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
