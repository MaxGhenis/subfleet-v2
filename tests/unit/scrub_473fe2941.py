"""The scrubber at `473fe2941` (`subfleet/sessions/handoff.py` there): release/217 after
#231 (D-SCR1), the base this branch (D-SCR2) was cut from. Kept verbatim as a reference
for the differential checks in `test_scrub_inline_headers.py` (C-23.14: no mode of the
head shows a value this base keeps out). Only the scrubbing half of the module is kept,
as `scrub_release217` keeps it: its constants and patterns, the rules, `scrub_secrets`,
`scrub_bounded` and their helpers, `truncate` and `clean`. The rest (tool suppression,
transcripts, briefs) is left out, and the one relative import is made absolute.
`scrubbed_line` at the end mirrors `suggestions._scrub_line`, the daemon.log fold,
without its 2,000-character bound. Never imported by `subfleet`."""

from __future__ import annotations

import re
from typing import Callable

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


def _scrub_named_values(text: str, replace: Callable[[str, str], str],
                       *, quoted: bool = False, flags: bool = False, multiline: bool = False) -> str:
    """Replace assignment/flag values in linear scans, preserving rule order."""
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


def _last_visible(text: str, end: int) -> int:
    """The position of the last non-whitespace character before `end`, or -1."""
    at = end - 1
    while at >= 0 and text[at].isspace():   # `\s` in these patterns is `str.isspace`
        at -= 1
    return at


def _neutral_break(text: str, visible: int, after: int) -> bool:
    """Whether a line break can be a cut: no rule could continue a match past
    it. `visible` is the last visible character before it (-1: none), `after`
    the first after it (`len(text)`: none). Only these continue past a break,
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
        before = _last_visible(text, visible)   # the separator may follow blank lines
        if before >= 0 and text[before] in "\"'":
            return not _key_ends_at(text, before, quoted=True)
        return not _key_ends_at(text, before)
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


def _resolve_blocks(text: str, strip_reminders: bool, tally: _Tally) -> str:
    """The scrub's first rules, which can match across lines: reminders, private
    keys, data URIs (wrapped base64). Each is a scan whose work is linear in the
    text (`_linear_sub`; the data URI pattern has no nested repetition), so a text
    of any length has them resolved whole, as `_scrub` resolves them. Applied
    again to what they leave, they change nothing."""
    if strip_reminders:
        text = _linear_sub(_SYSTEM_REMINDER_RE, "", text)
    return _apply_rules(text, ((_PEM_RE, "[PRIVATE KEY REDACTED]"),
                               (_DATA_URI_RE, "[BASE64 DATA OMITTED]")), tally)


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
    text = _apply_rules(text, (
        (_JWT_RE, REDACTED),
        (_PREFIXED_TOKEN_RE, REDACTED),
        (_BEARER_RE, "Bearer " + REDACTED),
        (_LONG_BASE64_RE, "[BASE64 OMITTED]"),
        (_HEADER_RE, _header_value),
        (_URL_PASSWORD_RE, lambda match: match.group(1) + REDACTED + match.group(3)),
    ), tally)
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


def scrubbed_line(text: str) -> str:
    """`suggestions._scrub_line` at 473fe2941 over this scrubber: scrubbed whole,
    folded, scrubbed whole again."""
    clean, _ = scrub_secrets(text, strip_reminders=True, whole=True)
    return scrub_secrets(" ".join(clean.split()), strip_reminders=True, whole=True)[0]
