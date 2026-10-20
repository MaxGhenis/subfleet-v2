"""The daemon.log cut reads a group's margins only where it looks (#233 review r3, P3-1).

Round 4's cut ran three passes over the whole text: it blanked every exception group
margin with a regex substitution, searched the whole text for a reminder tag, and
matched a leading header whose `\\s*` backtracked over leading whitespace. Review r3
measured 95, 383 and 1,594 ms at 1, 4 and 16 MiB of margin-only lines, against 9.7 ms
for plain text at 4 MiB. Round 5's cut blanks margins in its two windows only and
recognizes one past them where a look reaches it (`handoff._Reading`); a C scan finds a
reminder tag; the header is matched at the first visible character.

This file holds:
- the bound, counted rather than timed: at 16 MiB no regex reads past the windows, and
  the `str` scans past them stay linear in the text (`Metered`);
- the differential: the windowed cut's output is the cut's as of cce8f1af7
  (`tests/reference_cut_cce8f1af7.py`), byte for byte, on review r3's shapes, the fuzz
  corpora and a property whose texts cross both windows;
- the C scans against the regexes they stand for.
"""
from __future__ import annotations

import contextlib
import random
import re
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet import daemon_log
from subfleet.sessions import handoff
from tests import reference_cut_cce8f1af7 as reference
from tests.unit import test_daemon_log_handler as handler_tests
from tests.unit.test_scrub_key_order import HEADER_KEYS, HEADER_SHAPES, HEADER_VALUES

MIB = 1024 * 1024
LIMIT = daemon_log.RECORD_SCRUB_MAX
BOUNDED = settings(max_examples=150, derandomize=True, database=None, deadline=None)


def _group_traceback(size: int) -> str:
    """Review r3's raised exception group: a member of `size` characters of lines and a
    second member, as Python formats it (every member line behind a margin)."""
    member = RuntimeError("\n".join(["member line words"] * max(1, size // 18)))
    try:
        raise ExceptionGroup("ctx", [member, ValueError("other")])
    except ExceptionGroup as group:
        return daemon_log.ScrubbingFormatter().formatException((type(group), group, group.__traceback__))


#: Review r3's shapes (P3-1), and plain text beside them.
REVIEW_R3_SHAPES = {
    "plain": lambda size: "plain words only\n" * (size // 17),
    "margin-only-lines": lambda size: "  |\n" * (size // 4),
    "margin-only-lines-spaced": lambda size: "  | \n" * (size // 5),
    "member-lines": lambda size: "  | member line\n" * (size // 16),
    "leading-spaces": lambda size: " " * size + "\nx\n",
    "run-of-lt": lambda size: "<<<<<<<<<<<<<<<\n" * (size // 16),
    "exception-group": _group_traceback,
}
SHAPES = {**REVIEW_R3_SHAPES, **{f"round2-{name}": shape for name, shape in handler_tests.COST_SHAPES.items()}}


# --- the bound, counted ----------------------------------------------------------------

class Meter:
    """What one cut reads: the characters each regex call may read (its `pos` to its
    `endpos`, the whole string for a substitution), and the characters each `str` call
    on the text scans or copies (`Metered`)."""

    def __init__(self):
        self.regex = 0
        self.regex_calls: list[tuple[str, int]] = []
        self.scanned = 0

    def read(self, pattern: re.Pattern, span: int) -> None:
        self.regex += span
        self.regex_calls.append((pattern.pattern[:40], span))

    def widest(self) -> list[tuple[str, int]]:
        return sorted(self.regex_calls, key=lambda call: -call[1])[:5]

    @contextlib.contextmanager
    def installed(self, monkeypatch):
        """Every compiled pattern `handoff` holds, counted while the cut runs; and the
        reading's text in the windows (`_Reading.read`), metered as the text is."""
        for name, value in list(vars(handoff).items()):
            if isinstance(value, re.Pattern):
                monkeypatch.setattr(handoff, name, MeteredPattern(value, self))
        reading_class = getattr(handoff, "_Reading", None)       # none at cce8f1af7: one `read`
        if reading_class is not None:
            built = reading_class.__init__

            def metered_reading(reading, text, head_end, tail_start):
                built(reading, text, head_end, tail_start)
                if not isinstance(reading.read, Metered):
                    reading.read = Metered(reading.read, self)
            monkeypatch.setattr(reading_class, "__init__", metered_reading)
        try:
            yield self
        finally:
            monkeypatch.undo()


class MeteredPattern:
    """A compiled pattern whose calls count the span they may read."""

    def __init__(self, pattern: re.Pattern, meter: Meter):
        self._pattern, self._meter = pattern, meter

    def _span(self, string, pos=0, endpos=sys.maxsize):
        self._meter.read(self._pattern, max(0, min(endpos, len(string)) - pos))

    def search(self, string, pos=0, endpos=sys.maxsize):
        self._span(string, pos, endpos)
        return self._pattern.search(string, pos, endpos)

    def match(self, string, pos=0, endpos=sys.maxsize):
        self._span(string, pos, endpos)
        return self._pattern.match(string, pos, endpos)

    def fullmatch(self, string, pos=0, endpos=sys.maxsize):
        self._span(string, pos, endpos)
        return self._pattern.fullmatch(string, pos, endpos)

    def finditer(self, string, pos=0, endpos=sys.maxsize):
        self._span(string, pos, endpos)
        return self._pattern.finditer(string, pos, endpos)

    def sub(self, repl, string, count=0):
        self._span(string)
        return self._pattern.sub(repl, string, count)

    def __getattr__(self, name):
        return getattr(self._pattern, name)


class Metered(str):
    """A text whose `str` calls count what they scan or copy: `find` and `rfind` to
    where they stop, `count`, `in`, `lower`, `replace`, `isascii` and `encode` all of
    their range, and a slice what it copies. A copy is not metered again: each is
    read by a fixed number of the C calls that follow it (`_run_start`'s `rstrip`,
    `_char_classes`' `bytes` scans)."""

    def __new__(cls, value: str, meter: Meter):
        text = super().__new__(cls, value)
        text.meter = meter
        return text

    def _range(self, start, end):
        start = 0 if start is None else (start if start >= 0 else max(0, len(self) + start))
        end = len(self) if end is None else (min(end, len(self)) if end >= 0 else max(0, len(self) + end))
        return start, max(start, end)

    def find(self, sub, start=None, end=None):
        begin, finish = self._range(start, end)
        found = str.find(self, sub, begin, finish)
        self.meter.scanned += (found + len(sub) - begin) if found >= 0 else finish - begin
        return found

    def rfind(self, sub, start=None, end=None):
        begin, finish = self._range(start, end)
        found = str.rfind(self, sub, begin, finish)
        self.meter.scanned += (finish - found) if found >= 0 else finish - begin
        return found

    def count(self, sub, start=None, end=None):
        begin, finish = self._range(start, end)
        self.meter.scanned += finish - begin
        return str.count(self, sub, begin, finish)

    def __contains__(self, sub):
        found = str.find(self, sub)
        self.meter.scanned += found + len(sub) if found >= 0 else len(self)
        return found >= 0

    def __getitem__(self, key):
        part = str.__getitem__(self, key)
        self.meter.scanned += len(part)
        return part

    def lower(self):
        self.meter.scanned += len(self)
        return str.lower(self)

    def replace(self, *args):
        self.meter.scanned += len(self)
        return str.replace(self, *args)

    def isascii(self):
        self.meter.scanned += len(self)
        return str.isascii(self)

    def encode(self, *args, **kwargs):
        self.meter.scanned += len(self)
        return str.encode(self, *args, **kwargs)


#: Regex reads per cut, whatever the text's length: the windows' `limit` characters,
#: each read by a fixed number of patterns.
REGEX_BOUND = 16 * LIMIT
#: `str` reads per cut, past the windows' own: a fixed number of scans of the text.
SCAN_PASSES = 12


def metered_cut(text: str, monkeypatch) -> tuple[str, Meter]:
    meter = Meter()
    with meter.installed(monkeypatch):
        cut = handoff.cut_whole(Metered(text, meter), LIMIT)
    return str(cut), meter


@pytest.mark.timeout(600)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_at_16_mib_no_regex_reads_past_the_cut_s_windows(shape, monkeypatch):
    """Review r3, P3-1: at cce8f1af7 one cut ran a regex over all 16 MiB three times (the
    margins' substitution, the reminder search and the leading header's match), and
    `test_a_cut_s_work_does_not_grow_with_the_text` counted each as one C call. Counted
    in characters: every regex reads within the windows, the same at 1 and at 16 MiB,
    and the `str` scans are linear in the text, a fixed number of passes."""
    reads = {}
    for size in (MIB, 16 * MIB):
        text = SHAPES[shape](size)
        cut, meter = metered_cut(text, monkeypatch)
        assert cut == handoff.cut_whole(text, LIMIT)
        reads[size] = meter
        assert meter.regex <= REGEX_BOUND, (size, meter.regex, meter.widest())
        assert max((span for _, span in meter.regex_calls), default=0) <= 2 * LIMIT, meter.widest()
        assert meter.scanned <= SCAN_PASSES * len(text) + REGEX_BOUND, (size, meter.scanned / len(text))
    assert reads[16 * MIB].regex <= reads[MIB].regex + LIMIT, (reads[MIB].regex, reads[16 * MIB].regex)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("where", ["message", "traceback"])
@pytest.mark.parametrize("shape", sorted(REVIEW_R3_SHAPES))
def test_a_four_mib_record_of_review_r3_s_shapes_is_written_within_a_second(shape, where):
    """A generous guard on the clock, as the round-2 shapes' (D-TM1): under a second of
    CPU beyond Python's own formatting; the counting test above carries the bound.
    Review r3 measured 351.6 ms for the margin-only lines."""
    out, beyond = handler_tests.handler_cpu_beyond_formatting(REVIEW_R3_SHAPES[shape](4 * MIB), where)
    assert "characters omitted] …" in out
    assert beyond < 1.0, beyond


# --- the differential ------------------------------------------------------------------

@pytest.mark.parametrize("limit", [400, LIMIT])
@pytest.mark.parametrize("size", [4096, 65_536, MIB])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_windowed_cut_is_the_reference_cut_on_review_r3_s_shapes(shape, size, limit):
    """Byte for byte, the cut at cce8f1af7's output, at sizes from under the bound to past it."""
    text = SHAPES[shape](size)
    assert handoff.cut_whole(text, limit) == reference.cut_whole(text, limit)


#: #231's header shapes (`test_scrub_key_order.HEADER_SHAPES`): the value before the key or
#: not, the key bare, quoted or escape-quoted, and its separator ending the key's line or
#: starting the next: 12 shapes, each with either colon.
HEADER_PIECES = [(before, quote, separator) for before in HEADER_SHAPES["before"]
                 for quote in HEADER_SHAPES["quote"] for separator in HEADER_SHAPES["separator"]]


def header_piece(rng: random.Random, secret) -> str:
    before, quote, separator = rng.choice(HEADER_PIECES)
    n = rng.randrange(1000)
    header = rng.choice(tuple(HEADER_VALUES))
    value = (HEADER_VALUES[header].format(n=n) + " ") if HEADER_SHAPES["before"][before] else ""
    key = HEADER_SHAPES["quote"][quote].format(HEADER_KEYS[n % len(HEADER_KEYS)])
    colon = HEADER_SHAPES["colon"][rng.choice(tuple(HEADER_SHAPES["colon"]))]
    return f"{header}{colon}{value}{key}{HEADER_SHAPES['separator'][separator]}{secret()} tail\n"


def header_fuzz_text(rng: random.Random) -> tuple[str, int]:
    """The rebuilt generator's text (`fuzz_text`) with #231's header shapes among its lines."""
    text, limit = handler_tests.fuzz_text(rng)
    lines = text.splitlines(True)

    def secret() -> str:
        return "SECRET7" + "".join(rng.choice("abcdefghij0123456789") for _ in range(10))
    for _ in range(rng.randint(1, 4)):
        lines.insert(rng.randint(0, len(lines)), header_piece(rng, secret))
    if rng.random() < 0.2:
        lines.insert(0, header_piece(rng, secret))
    return "".join(lines), limit


CORPORA = {"fuzz_text": handler_tests.fuzz_text, "fuzz_text-with-231-headers": header_fuzz_text}


@pytest.mark.parametrize("corpus", sorted(CORPORA))
@pytest.mark.parametrize("seed", range(3))
def test_the_windowed_cut_is_the_reference_cut_on_the_fuzz_corpora(corpus, seed):
    """Byte for byte over the rebuilt generator's texts and the same with #231's header
    shapes, at each text's limit. The CI run is a bounded slice; the report's is 16,000."""
    rng = random.Random(5000 + seed)
    for _ in range(handler_tests.FUZZ_TEXTS // 3 + (seed < handler_tests.FUZZ_TEXTS % 3)):
        text, limit = CORPORA[corpus](rng)
        assert handoff.cut_whole(text, limit) == reference.cut_whole(text, limit), (text, limit)


@BOUNDED
@given(drawn=st.lists(handler_tests.MEMBERS, min_size=1, max_size=3), depth=st.integers(1, 2),
       raise_it=st.booleans(), over=st.booleans(), limit=st.sampled_from([200, 400, 1000, LIMIT]))
def test_the_windowed_cut_is_the_reference_cut_on_groups(drawn, depth, raise_it, over, limit):
    """Byte for byte over the group generator's tracebacks (`distinct_member`, a group's
    own 1,200 lines when `over`): each member's lines behind its margin."""
    members = [handler_tests.distinct_member(item, index, pad=1000 if over else 0)[0]
               for index, item in enumerate(drawn)]
    group = handler_tests.group_of(members, depth=depth, raise_it=raise_it,
                                   message=handler_tests.GROUP_CONTEXT if over else "ctx")
    text = handler_tests.traceback_of(group)
    assert handoff.cut_whole(text, limit) == reference.cut_whole(text, limit)


#: Lines the windowed reading treats differently from text: margins at depths one to
#: three with and without what follows, near misses (an odd run, a tab, a second bar, a
#: carriage return, a wide space), and the lines the cut's rules read.
MARGIN_LINES = ["  |", "  | ", "    |", "    | ", "      |  \t", "  +", "  + more", "   |", "|", "\t  |",
                "  ||", "  | |", "  |\r", "  |　", "　 |", "  |\x0c", "", " ", "\t", "  | member line"]
PIECES = ["token:", "token =", "--password", "deploy --password", "a='open", "'", '"', "\\\"", "k=\\'",
          "Authorization: Bearer", "Cookie:", "Set-Cookie: a=b;", "x", "  | --password", "  | token:",
          "  | SECRET7m rest", "data:image/png;base64,QUJD", "  | QUJDREVG", "-----BEGIN PRIVATE KEY-----",
          "  | -----BEGIN RSA", "  | PRIVATE KEY-----", "-----END PRIVATE KEY-----", "  +-+---------------- 1 ----------------",
          "=", ":", "SECRET7plain9", "Bearer", "  | Bearer", "<system-remind", "</system-reminder", "r>"]
LEADS = ["", "", "", " " * 300, "  |\n" * 120, "\n" * 200, "  | \n  |\n" * 80, "Authorization: x\n", "  |\n  | Cookie: y\n",
         "<sYstem-reminder>x</system-reminder>\n", "<ſystem-reminder>\n", "</system-remınder>\n",
         "<system-remİnder>\n", "system-reminder>\n", "<system-reminder\n"]
SEGMENTS = st.one_of(
    st.tuples(st.sampled_from(MARGIN_LINES), st.integers(1, 600)).map(lambda run: (run[0] + "\n") * run[1]),
    st.sampled_from(PIECES).map(lambda piece: piece + "\n"),
    st.tuples(st.sampled_from(["  | ", "    | "]), st.sampled_from(PIECES)).map(lambda pair: pair[0] + pair[1] + "\n"),
)


@BOUNDED
@given(lead=st.sampled_from(LEADS), segments=st.lists(SEGMENTS, min_size=1, max_size=12),
       limit=st.integers(40, 600), crlf=st.booleans())
def test_the_windowed_cut_is_the_reference_cut_where_margins_cross_its_windows(lead, segments, limit, crlf):
    """Byte for byte where it is likeliest to differ: long runs of margin-only lines,
    near misses among them, leading whitespace and margins, reminder-like prefixes in
    every case the regex reads, and the rules' pieces with and without margins, at
    limits that put the windows' edges inside the runs."""
    text = lead + "".join(segments)
    if crlf:
        text = text.replace("\n", "\r\n")
    assert handoff.cut_whole(text, limit) == reference.cut_whole(text, limit)


# --- the C scans against what they stand for -----------------------------------------------

MARGIN = re.compile(r"(?m)^((?:  )+)[|+](?= |$)")
NOT_BODY = re.compile(r"[^A-Za-z0-9+/=\s]")
LEADING_HEADER = re.compile(r"(?i)\s*(?:authorization|cookie|set-cookie)\s*:")
SMALL_TEXTS = st.text(alphabet=[" ", " ", " ", "\n", "|", "+", "\t", "\r", "x", "A", "　", "\xa0", ",", "/", ":"],
                      max_size=30)


@settings(max_examples=2000, derandomize=True, database=None, deadline=None)
@given(text=SMALL_TEXTS, data=st.data())
def test_the_margin_scans_read_what_the_margin_regex_blanks(text, data):
    """`_is_margin_mark`, `_margin_lines` and `_body_between` (C scans) agree with
    `_MARGIN_MARK_RE`'s substitution, and with the data-URI body class on its output."""
    blanked = MARGIN.sub(r"\1 ", text)
    for at, char in enumerate(text):
        if char in "|+":
            assert handoff._is_margin_mark(text, at) is (blanked[at] == " "), (text, at)
    starts = [0, *(at + 1 for at, char in enumerate(text) if char == "\n")]
    start, end = sorted(data.draw(st.sampled_from(starts)) for _ in range(2))
    if end > start and text[end - 1] == "\n":
        assert handoff._margin_lines(text, start, end) is blanked[start:end].isspace(), (text, start, end)
    begin = data.draw(st.integers(0, end))
    assert handoff._body_between(text, begin, end) is (NOT_BODY.search(blanked, begin, end) is None), (text, begin, end)


@settings(max_examples=600, derandomize=True, database=None, deadline=None)
@given(lead=st.sampled_from(LEADS), segments=st.lists(SEGMENTS, min_size=1, max_size=8), data=st.data())
def test_the_reading_s_looks_are_the_blanked_text_s(lead, segments, data):
    """`_Reading.before` and `.after`, from any position, with any windows, are
    `_last_visible` and `_run_end` on the text with every margin blanked; and the
    leading header and reminder tests are their regexes'."""
    text = lead + "".join(segments)
    blanked = MARGIN.sub(r"\1 ", text)
    head_end = data.draw(st.integers(0, len(text)))
    starts = [at for at in range(head_end, len(text) + 1) if at == len(text) or at == 0 or text[at - 1] == "\n"]
    reading = handoff._Reading(text, head_end, data.draw(st.sampled_from(starts)))
    for at in data.draw(st.lists(st.integers(0, len(text)), max_size=8)):
        assert reading.before(at) == handoff._last_visible(blanked, at), (text, at)
        assert reading.after(at) == handoff._run_end(blanked, at), (text, at)
    assert handoff._leads_with_header(reading) is (LEADING_HEADER.match(blanked) is not None)
    assert handoff._holds_reminder_tag(text) is (handoff._REMINDER_TAG_RE.search(text) is not None)


def test_the_reminder_folds_are_every_character_the_tag_regex_takes_for_a_letter():
    """`_REMINDER_FOLDS`: for each letter of the tag, the characters `_REMINDER_TAG_RE`
    (which ignores case) matches are exactly those that lower to it once folded; and
    lowering keeps every position once they are, so a C scan finds each tag where it is."""
    every = "".join(map(chr, range(sys.maxunicode + 1)))
    folds = dict(handoff._REMINDER_FOLDS)
    letters = set("system-reminder<>/")
    lowered: dict[str, set[str]] = {letter: set() for letter in letters}
    longer = []
    for char in every:
        low = folds.get(char, char).lower()
        if low in lowered:
            lowered[low].add(char)
        elif len(low) != 1:
            longer.append(char)
    assert longer == []
    for letter in sorted(letters):
        assert set(re.findall("(?i)" + re.escape(letter), every)) == lowered[letter], letter
