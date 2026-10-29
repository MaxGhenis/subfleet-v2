"""Release/217's credential scrubber, frozen: `subfleet/sessions/handoff.py` at `2c981cba5`
(blob `f78955e05`), its scrubbing half kept verbatim (C-23.14). Every mode the head
scrubs in runs this module's pipeline for that mode first and then the head's own
(`handoff.scrub_secrets`, `scrub_bounded`, `clean`; `suggestions._scrub_line`,
`scrubbed_line` and `normalize`; `daemon_log`'s block form; `redact.mask_approval`), and
the head's pipeline only replaces, folds or cuts what it is given, so every character
release hides stays hidden (#239 round 8).

Kept: lines 59-152 (the constants and patterns) and 236-1568 (the placeholders through
`clean`: the rules, `scrub_secrets`, `scrub_bounded`, the cuts daemon.log uses, `truncate`
and `clean`), then `subfleet/conversations/redact.py`'s lines 121-160 there (`_SHELL_META`
and `mask_approval`, blob `bde628b71`). Left out: the tool-suppression patterns and
`HandoffError` (153-235) and everything after `clean` (transcripts, briefs, dispatch).
Only the imports differ: the module's relative import is made absolute, and
`mask_approval`'s own import reads its names from this module. `tests/unit/test_scrub_release_frozen.py` pins the
kept lines to the git object. Never edit this file; a release change to the scrubber is
a new frozen module."""

from __future__ import annotations

import bisect
import re
from typing import Any, Callable

from subfleet.contracts import SCRUB_MAX_CHARS

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
# A key's closing quote may be escaped, by a repr (`\'password\': …`) or by JSON
# encoded more than once (`\"password\": …`, `\\\"password\\\": …`).
_ASSIGN_SEPARATOR_RE = re.compile(r"(?:\\*[\"'])?\s*[:=]\s*")
# A value that opens with a quote, escaped or not, is the quoted rule's.
_PLAIN_VALUE_RE = re.compile(r"(?!\\*[\"'])[^\s,;\"']+")
_FLAG_VALUE_RE = re.compile(r"[ \t]+(?P<value>[^\s-][^\s]*)")
# A `Bearer` token, unless the token would begin with a credential key a named-value
# rule reads: taken, the key leaves its value to no rule (`token_type: Bearer\n
# access_token: …`, `scheme=bearer oauth.client_secret=…`, `Bearer --api-key …`,
# C-23.14). The lookahead refuses token characters up to a sensitive ending followed
# by an assignment's separator (`_ASSIGN_SEPARATOR_RE`), or up to a flag (`--` and
# `_SENSITIVE_KEY`, as `_SENSITIVE_FLAG_RE` reads one) followed by its value
# (`_FLAG_VALUE_RE`). After any token characters, `_SENSITIVE_WORD` already covers
# the compound keys (`db_password`), as `_SENSITIVE_SUFFIX_RE` does for the
# assignment rules; `_SENSITIVE_KEY` there would nest its repetition in the lazy one.
_BEARER_RE = re.compile(
    r"(?i)\bBearer\s+(?![A-Za-z0-9._~+/-]*?(?:" + _SENSITIVE_WORD + _ASSIGN_SEPARATOR_RE.pattern
    + r"|--" + _SENSITIVE_KEY + r"[ \t]+[^\s-]))[A-Za-z0-9._~+/-]{8,}=*")
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
    for token in _IDENTIFIER_RE.finditer(text):
        # Inside the previous plain replacement (the quoted rule keeps no cursor).
        nested = token.start() < cursor
        if nested and flags:
            continue
        key = token.group()
        if not _SENSITIVE_SUFFIX_RE.search(key[-17:]):
            continue
        start = token.start()
        if flags:
            flag_start = _flag_start(key)
            if flag_start < 0:
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
            if nested and value_start < cursor:
                continue                        # its value is in the previous replacement
            opening = _VALUE_QUOTE_RE.match(text, value_start) if quoted else None
            if quoted and opening is None and not (ranges and start < ranges[-1][1] <= value_start):
                # A plain value is the plain rule's, after this pass, unless its key
                # is in a quoted match that ends before its value starts.
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
                    out = text[start:value_start] + REDACTED
                else:
                    end = closing + opening.end() - value_start
                    out = text[start:opening.end()] + REDACTED + text[closing:end]
            else:
                value = _PLAIN_VALUE_RE.match(text, value_start)
                if value is None:
                    continue
                end = value.end()
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
    if spans is not None:
        spans.extend(ranges)
        return text
    for start, end, out in ranges:
        parts.extend((text[cursor:start], replace(text[start:end], out)))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def scrub_secrets(text: str, *, strip_reminders: bool = False, whole: bool = False) -> tuple[str, int]:
    """Remove credential values and encoded binary; retain ordinary text. The
    count is of values replaced: one credential two rules match counts once, and
    a header that held several values (`Cookie: a=...; b=...`) counts each.

    A text longer than MAX_SCRUB_CHARS keeps a scrubbed head and tail with a
    marker between them (`scrub_bounded`). `whole` scrubs all of it, for a text
    whose length its caller bounds: the assembled handoff brief, whose section
    caps policy keeps within the same bound (C-23.36). Whole-text callers also
    mask quoted assignment values across line breaks, before any folding or cut;
    excerpts retain their within-line rules so no match crosses an excerpt cut."""
    if len(text) > MAX_SCRUB_CHARS and not whole:
        return scrub_bounded(text, None, strip_reminders=strip_reminders)
    return _scrub(text, strip_reminders, multiline=whole)


def scrub_bounded(text: str, limit: int | None, *, strip_reminders: bool = False) -> tuple[str, int]:
    """`truncate(scrub_secrets(text), limit)`: scrubbed, then bounded to `limit`
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
    - `Bearer`, to a token character after the break.
    `_BEARER_RE`'s lookahead, which refuses a token that begins with a key, and the
    header rule's choice to leave the key that ends its line (`_key_past_line`) read
    past a break only between a key or its closing quote and its `:` or `=`, or
    between those and the value, which the first two refuse; a refused `Bearer`
    still refuses the break after it, which is only more cautious.
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
        return not separator
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
#: A header's name, as `_HEADER_RE` reads one; `_leads_with_header` matches it only at
#: a text's first visible character, at most 13 characters.
_HEADER_NAME_RE = re.compile(r"(?i)authorization|cookie|set-cookie")
_HEADER_NAME_MAX = len("authorization")
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
    _scrub_named_values(text, lambda _matched, out: out, quoted=True, multiline=True, spans=spans)
    return spans


def _leads_with_header(reading: "_Reading") -> bool:
    """Whether the text's first visible word is a header's name with a `:` as the
    next visible character, as `(?i)\\s*(?:authorization|cookie|set-cookie)\\s*:`
    matches the text with its margins read as spaces: a name matched at the first
    visible character, and a look past it, where the regex's `\\s*` would have
    backtracked over every leading space."""
    text = reading.text
    start = reading.after(0)
    name = _HEADER_NAME_RE.match(text, start, start + _HEADER_NAME_MAX)
    if name is None:
        return False
    colon = reading.after(name.end())
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

        text = _linear_sub(pattern, replace, text)
    return text


def _header_value(match: re.Match) -> str:
    """The header rule's replacement: the rest of the header's line, redacted. A
    credential key that ends the line, with its value past the line (`Authorization:
    client_secret=` and the secret on the next line), is left to the named-value
    rules with what follows it (C-23.14): taken, it would leave its value to no rule.
    What precedes the key is redacted as before; a header holding only the key is
    left whole, and those rules read it (the header's own name is a key too). The
    line's trailing whitespace goes either way, as the redaction always took it, and
    a space parts the redaction from a key that adjoined what it hid (`x.token=`), so
    the header's own value ends there and a scrubbed line scrubs to itself."""
    text, start, end = match.string, match.end(1), match.end()
    key = _key_past_line(text, start, end)
    if key is None:
        return match.group(1) + REDACTED
    before, last = _last_visible(text, key) + 1, _last_visible(text, end) + 1
    if before <= start:
        return text[match.start():last]
    return match.group(1) + REDACTED + (text[before:last] if before < key else " " + text[key:last])


def _key_past_line(text: str, start: int, end: int) -> int | None:
    """Where the credential key ending `text[start:end]`, a line's rest, begins (its
    opening quote and the backslashes escaping that included) when its separator
    (`_ASSIGN_SEPARATOR_RE`) reaches past `end` to a value; None otherwise. Only the
    line's last identifier can: between it and `end` the separator allows nothing
    but a quote, whitespace and `:` or `=`."""
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
    if not _SENSITIVE_SUFFIX_RE.search(text[max(at, key_end - 17):key_end]):
        return None
    separator = _ASSIGN_SEPARATOR_RE.match(text, key_end)
    if separator is None or separator.end() <= end or not (
            _PLAIN_VALUE_RE.match(text, separator.end()) or _VALUE_QUOTE_RE.match(text, separator.end())):
        return None
    if at > start and text[at - 1] in "\"'":
        at -= 1
        while at > start and text[at - 1] == "\\":
            at -= 1
    return at


#: The rules after reminders that can match across lines (`_resolve_blocks`).
_BLOCK_RULES = ((_PEM_RE, "[PRIVATE KEY REDACTED]"), (_DATA_URI_RE, "[BASE64 DATA OMITTED]"))
#: The token rules `_scrub` applies after the blocks and the quoted values, in order.
_TOKEN_RULES = (
    (_JWT_RE, REDACTED),
    (_PREFIXED_TOKEN_RE, REDACTED),
    (_BEARER_RE, "Bearer " + REDACTED),
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
    """`scrub_secrets` on a text within its work bound."""
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
    text = _apply_rules(text, _TOKEN_RULES, tally)
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


# --- subfleet/conversations/redact.py at 2c981cba5, lines 121-160 ---------------------------

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
    from subfleet.sessions.scrub_release import (_BEARER_RE, _JWT_RE, _PEM_RE, _PREFIXED_TOKEN_RE,
                                                 _URL_PASSWORD_RE, _linear_sub)
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
