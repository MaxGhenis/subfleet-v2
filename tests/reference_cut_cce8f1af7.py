"""The cut the daemon's handler ran at cce8f1af7 (#233 round 4), kept as a reference.

Everything below is copied verbatim from `subfleet/sessions/handoff.py` at cce8f1af7:
`cut_whole` and every function and constant it reaches. That cut blanked each exception
group's margins over the whole text (`_margins_blanked`, a regex substitution), searched
the whole text for a reminder tag and matched a leading header whose leading
whitespace backtracked; review r3 of #233 measured those three passes (P3-1). Round 5's cut reads the
margins only in its two windows and past them where a look reaches; the differential
tests hold its output equal to this one's. Nothing outside tests imports it.
"""
from __future__ import annotations

import bisect
import re

__all__ = ["cut_whole", "cut_marker"]

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

_FLAG_VALUE_RE = re.compile(r"[ \t]+(?P<value>[^\s-][^\s]*)")


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


#: A data URI's prefix, as `_DATA_URI_RE` reads it; its body is the run of
#: `_DATA_BODY_RE` after it, line breaks included.
_DATA_URI_PREFIX_RE = re.compile(r"(?i)data:[a-z0-9.+/-]+(?:;[a-z0-9=.+/-]+)*;base64,")

_DATA_BODY_RE = re.compile(r"[A-Za-z0-9+/=\s]+")

#: A quote and the run of backslashes right before it. `_quoted_ends` pairs a quote
#: after an even run (none included) with the next such quote of its kind;
#: `_escaped_quote_ends`, one after a run of one or more with the next after a run
#: of the same length.
_QUOTE_RUN_RE = re.compile(r"(\\*)([\"'])")

#: Either reminder tag (`_REMINDER_START_RE`, `_REMINDER_END_RE`), in one scan.
_REMINDER_TAG_RE = re.compile(r"</?system-reminder>", re.IGNORECASE)

#: A text whose first word is a header's name, as `_HEADER_RE` reads one.
_LEADING_HEADER_RE = re.compile(r"(?i)\s*(?:authorization|cookie|set-cookie)\s*:")

#: A private key's first and last lines as `_PEM_RE` reads them once the text is
#: folded to one line: each of their spaces may be any run of whitespace, line
#: breaks included (`-----BEGIN RSA\nPRIVATE KEY-----`). A test checks them against
#: `_PEM_START_RE` and `_PEM_END_RE` on folded text.
_PEM_START_FOLDED_RE = re.compile(r"-----BEGIN(?:\s+[A-Z0-9]+)*\s+PRIVATE\s+KEY(?:\s+BLOCK)?-----")

_PEM_END_FOLDED_RE = re.compile(r"-----END(?:\s+[A-Z0-9]+)*\s+PRIVATE\s+KEY(?:\s+BLOCK)?-----")

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
    - with each exception group's margin read as spaces (`_margins_blanked`), as
      the block form's margin-free reading of a group reads it;
    - and a text holding a reminder tag is not cut at all: stripping a reminder
      joins what was on either side of it, which can make a quote open a value.
      Nor is one that opens with a header when the head would keep none of it:
      folded to one line, as the line form folds it, all of it is the header's.
    `scrub_bounded` needs only the first: it resolves the blocks over the whole
    text first and scrubs its excerpts within lines, where no quoted value
    crosses a line break, and it never folds.

    The work is bounded whatever the text's length (review r2 of #233): linear in
    the two windows (`limit // 2` characters each, and the lines that hold their
    edges), plus a fixed number of C scans of the rest, plus at most one event
    computation for each quote kind a side reads (`_event_from`, `_event_before`,
    each at most `_LOOK` C scans): two plain kinds and at most `CUT_KINDS_MAX`
    escaped ones. A side whose window holds more escaped kinds, or with no such
    break in reach, keeps nothing: one long line keeps only the line saying so."""
    if len(text) <= limit:
        return text
    head, tail = 0, len(text)
    read = _margins_blanked(text)
    if not _REMINDER_TAG_RE.search(text):
        head = _whole_head_cut(read, limit // 2)
        tail = max(head, _whole_tail_cut(read, len(text) - limit // 2))
    if _LEADING_HEADER_RE.match(read) and not read[:head].strip():
        # Folded to one line, a text that opens with a header is all that header's
        # value (`_HEADER_RE`): a tail without the header before it keeps nothing.
        head, tail = 0, len(text)
    return f"{text[:head]}{cut_marker(tail - head)}\n{text[tail:]}"


def _margins_blanked(text: str) -> str:
    """`text` as the cut reads it: each exception group's margin read as spaces, in
    place, so positions are kept. The margin-free reading the daemon's block form
    also scrubs (`daemon_log._group_block`) then crosses a cut exactly where this
    one does, and the margined reading never hides what this one shows: a margin's
    `|` only stops a match a space would let run. Two C scans when there are none."""
    if "  |" not in text and "  +" not in text:
        return text
    return _MARGIN_MARK_RE.sub(r"\1 ", text)


def _newlines(text: str, start: int, end: int):
    """The positions of the line breaks in [start, end)."""
    at = text.find("\n", max(0, start), end)
    while at >= 0:
        yield at
        at = text.find("\n", at + 1, end)


def _around(text: str, starts: list[int]) -> tuple[list[int], list[int]]:
    """For each of `starts`, line starts in ascending order: the last visible character
    before it and the first at or after it (`len(text)`: none), as `_neutral_break`
    takes them. One pass over the lines between them, and one look past each end:
    the starts in one run of blank lines share both, and are not read again each."""
    visible, seen = [], _last_visible(text, starts[0])
    for index, at in enumerate(starts):
        if index:
            previous = starts[index - 1]
            kept = len(text[previous:at].rstrip())
            if kept:
                seen = previous + kept - 1
        visible.append(seen)
    after, ahead = [0] * len(starts), _run_end(text, starts[-1])
    for index in range(len(starts) - 1, -1, -1):
        if index < len(starts) - 1:
            at, following = starts[index], starts[index + 1]
            lead = (following - at) - len(text[at:following].lstrip())
            if at + lead < following:
                ahead = at + lead
        after[index] = ahead
    return visible, after


def _whole_break(text: str, visible: int, after: int, verdicts: dict) -> bool:
    """Whether a line break between `visible` and `after` can be `cut_whole`'s cut.
    Breaks between the same two visible characters (a run of blank lines) share one
    verdict, kept in `verdicts`."""
    verdict = verdicts.get((visible, after))
    if verdict is None:
        verdict = verdicts[visible, after] = (_neutral_break(text, visible, after)
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


def _quote_events(text: str, start: int, end: int):
    """Each quote in [start, end) (`start` a line start, so each run is whole):
    `(position, kinds, opens)`. `kinds` are the kinds it pairs as, `("plain",
    quote)` and `("escaped", run, quote)`; `opens` is the kind it may open a value
    of, when a `:` or `=` precedes it (a key is not read: any may be one)."""
    for match in _QUOTE_RUN_RE.finditer(text, start, end):
        run, quote = len(match.group(1)), match.group(2)
        kinds = ((("plain", quote),) if run % 2 == 0 else ()) + ((("escaped", run, quote),) if run else ())
        before = _last_visible(text, match.start())
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


def _event_before(text: str, kind: tuple, end: int) -> bool | None:
    """Whether the last quote of `kind` before `end` may open a value of it: None when
    it is too far back to tell (`_LOOK` quotes of its character, runs at least its
    length, or a run of backslashes longer), False when there is none. At most
    `_LOOK` C scans, whatever the text's length."""
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
            before = _last_visible(text, quote - run)
            return before >= 0 and text[before] in ":=" and (kind[0] == "escaped" or run == 0)
        at = quote
    return None


def _within_spans(starts: list[int], spans) -> set[int]:
    """The starts that fall strictly inside any of `spans` (`(begin, finish)`)."""
    inside = set()
    for begin, finish in spans:
        inside.update(starts[bisect.bisect_right(starts, begin):bisect.bisect_left(starts, finish)])
    return inside


def _whole_head_cut(text: str, end: int) -> int:
    """The end of the head: the last line start within `end` no whole-text match
    crosses, folded or not (0 if none)."""
    starts = [0, *(at + 1 for at in _newlines(text, 0, end))]
    last = starts[-1]
    key = _PEM_START_FOLDED_RE.search(text, 0, last)
    if key is not None:                     # a private key starts in reach: the head ends before it
        starts = [at for at in starts if at <= text.rfind("\n", 0, key.start()) + 1]
    if len(starts) == 1:
        return 0
    bodies = []
    for prefix in _DATA_URI_PREFIX_RE.finditer(text, 0, last):
        body = _DATA_BODY_RE.match(text, prefix.end(), last + 1)       # whether it passes `last` is enough
        bodies.append((prefix.start(), body.end() if body else prefix.end()))
    blocked = _within_spans(starts, bodies)
    # Which kinds of quote are open at each line start: the last quote of the kind
    # before it may open a value, which an event of the kind after it would close.
    events = list(_quote_events(text, 0, last))
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
    visible, after = _around(text, starts)
    beyond: dict[tuple, bool | None] = {}   # each kind's event past the window, computed once
    verdicts: dict[tuple[int, int], bool] = {}
    for at, seen, ahead in zip(reversed(starts), reversed(visible), reversed(after)):
        if at == 0:
            return 0
        if at in blocked or not _whole_break(text, seen, ahead, verdicts):
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


#: A character no data URI's body holds, and the end of a data URI's prefix.
_NOT_BODY_RE = re.compile(r"[^A-Za-z0-9+/=\s]")

_BASE64_MARK_RE = re.compile(r"(?i);base64,")


def _whole_tail_cut(text: str, start: int) -> int:
    """The start of the tail: the first line start at or after `start` no whole-text
    match crosses, folded or not (the end of the text if none)."""
    starts = [at + 1 for at in _newlines(text, start - 1, len(text))]
    if not starts:
        return len(text)
    first = starts[0]
    ends = [match.end() for match in _PEM_END_FOLDED_RE.finditer(text, first)]
    if ends:                                # a private key ends in reach: the tail starts after it
        stop = text.find("\n", ends[-1])
        starts = [at for at in starts if 0 <= stop < at]
        if not starts:
            return len(text)
    # A data URI's body: the run after its prefix. The run at `first` began before
    # it, right after the last character no body holds: it is a body when that is
    # the `,` of a `;base64,`, so the last `,` before `first` (a C scan) and nothing
    # but body characters after it (one more, only then).
    bodies = []
    for run in _DATA_BODY_RE.finditer(text, first):
        if run.start() == first:
            comma = text.rfind(",", 0, first)
            body = (comma >= 7 and _BASE64_MARK_RE.fullmatch(text, comma - 7, comma + 1) is not None
                    and _NOT_BODY_RE.search(text, comma + 1, first) is None)
        else:
            body = run.start() >= 8 and _BASE64_MARK_RE.fullmatch(text, run.start() - 8, run.start()) is not None
        if body:
            bodies.append((run.start(), run.end()))
    blocked = _within_spans(starts, ((begin - 1, finish) for begin, finish in bodies))
    # A quoted value may run into the tail across a line start when a quote of a
    # kind follows it and the last before it may open a value of that kind.
    events = list(_quote_events(text, first, len(text)))
    if _too_many_kinds(events):
        return len(text)
    final = {}
    for position, kinds, _ in events:
        for kind in kinds:
            final[kind] = position
    visible, after = _around(text, starts)
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
        if at in blocked or not _whole_break(text, seen, ahead, verdicts):
            continue
        crossed = False
        for kind, position in final.items():
            if position < at:
                continue
            if kind in opened:
                state = opened[kind]
            else:
                if kind not in before:
                    before[kind] = _event_before(text, kind, first)
                state = before[kind]
            if state is not False:
                crossed = True
                break
        if not crossed:
            return at
    return len(text)
