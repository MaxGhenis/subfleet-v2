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
  past them the `str` scans are a fixed number of passes over the text plus at most 20
  needle looks, each reading the text at most once (`Metered`; review r4 of #233, P3-1:
  round 5 stated at most 12 passes in all, which a text with ten quote kinds open a side
  reads past, and counted a data URI body's match to the window's end);
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
from tests import regex_reads
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


def _quote_kinds(size: int) -> str:
    """Review r4's shape (P3-1): a head that leaves nine kinds of `'` open (the plain one,
    and eight escaped ones: `k1=`, one backslash and `'a`, to `k8=` and eight), and a tail
    of nine kinds of `"`, none of which a quote past the windows closes or opens: each kind
    costs its side one needle look over the text (`_event_from`, `_event_before`)."""
    head = "".join(f"k{run}={'\\' * run}'a\n" for run in range(1, 9)) + "k='a\n" + 'x "\n'
    tail = "".join(f"v{run} {'\\' * run}\"\n" for run in range(9))
    plain = "plain words only\n"
    return head + plain * ((size - len(head) - len(tail)) // len(plain)) + tail


#: Review r4's shapes: the quote kinds; a data URI whose body runs from the head into the
#: tail's window (P3-3: the tail's body check, `_body_between`, reads it in C scans); and
#: 480 data URI prefixes in the head, each body ended by the next line (P3-1: the meter
#: counted each body match to the window's end, 2,012,537 characters at 1 MiB).
REVIEW_R4_SHAPES = {
    "quote-kinds": _quote_kinds,
    "data-uri-body": lambda size: "data:image/png;base64," + "QUJDREVG\n" * ((size - 27) // 9) + "end.\n",
    "data-uri-prefixes": lambda size: ("data:a/b;base64,\n" * 480
                                       + "plain words only\n" * ((size - 480 * 17) // 17)),
}


def _backslash_runs(size: int) -> str:
    """Review r5's shape (P3-1): a run of 8,000 backslashes no quote follows, on its own
    line at each end of plain text, so in both windows. Round 6's `(\\\\*)(["'])` read each
    run again from each of its backslashes: 708 ms a cut at 1 MiB, 470 times plain text."""
    run = "x\n" + "\\" * 8000 + "\n"
    plain = "plain words only\n"
    return run + plain * ((size - 2 * len(run)) // len(plain)) + run


def _four_byte_margin_lines(size: int) -> str:
    """Review r5's four-byte margin shape (P3-1): spaced margin-only lines in a text Python
    stores four bytes a character (an emoji, `r>` and the three characters `_REMINDER_FOLDS`
    reads, on its first line). Review r6 (P3-B) asked whether its cost is linear: round 7's
    table gave 58.6 ms at 4 MiB and 409.9 at 16. Counted, it is: the same regex reads at 1
    and 16 MiB, and four passes of `str` scans (`tools/measure_cut_scaling.py`)."""
    wide = "\U0001F600 r> İ ı ſ\n"
    return wide + "  | \n" * ((size - len(wide)) // 5)


REVIEW_R5_SHAPES = {"backslash-runs": _backslash_runs, "four-byte-margin-lines": _four_byte_margin_lines}
SHAPES = {**REVIEW_R3_SHAPES, **{f"round2-{name}": shape for name, shape in handler_tests.COST_SHAPES.items()},
          **REVIEW_R4_SHAPES, **REVIEW_R5_SHAPES}


# --- the bound, counted ----------------------------------------------------------------

class Meter:
    """What one cut reads: the characters each regex call reads, counted by
    `tests.regex_reads` (each character test of a backtracking match, at each position a
    scan tries, review r5 of #233, P3-1: round 6 counted a call by its span, which a run of
    backslashes `(\\\\*)(["'])` reads again from each of its backslashes), and the
    characters each `str` call on the text scans or copies (`Metered`). The needle looks
    (`_event_from`, `_event_before`) are kept apart, each its own count, and so is what a
    look reads of a run of margin-only lines (`_Reading._back` and `_forward`), which C-25.7
    counts among the fixed scans: a run is read once."""

    def __init__(self):
        self.regex = 0
        self.regex_calls: list[tuple[str, int]] = []
        self.scanned = 0
        self.looks: list[int] = []
        self.margins = 0
        self._plain: dict[int, tuple[str, str]] = {}

    def plain(self, string: str) -> str:
        """`string` as a plain `str`, for the regex count, which a `Metered` text's own
        counting would otherwise see: one copy each text."""
        if type(string) is str:
            return string
        held = self._plain.get(id(string))
        if held is None or held[0] is not string:
            held = self._plain[id(string)] = (string, str.__str__(string))
        return held[1]

    def read(self, pattern: re.Pattern, reads: int) -> None:
        self.regex += reads
        self.regex_calls.append((pattern.pattern[:40], reads))

    def widest(self) -> list[tuple[str, int]]:
        return sorted(self.regex_calls, key=lambda call: -call[1])[:5]

    def fixed(self) -> int:
        """What the `str` scans read besides the needle looks."""
        return self.scanned - sum(self.looks)

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
            for name in ("_back", "_forward"):
                monkeypatch.setattr(reading_class, name, self._margin_run(getattr(reading_class, name)))
        for name in ("_event_from", "_event_before"):
            if hasattr(handoff, name):
                monkeypatch.setattr(handoff, name, self._look(getattr(handoff, name)))
        try:
            yield self
        finally:
            monkeypatch.undo()

    def _look(self, function):
        def look(*args):
            scanned, margins = self.scanned, self.margins
            try:
                return function(*args)
            finally:
                # A run of margin lines the look meets is the fixed scans' (each run once).
                self.looks.append(self.scanned - scanned - (self.margins - margins))
        return look

    def _margin_run(self, function):
        def run(reading, at):
            scanned = self.scanned
            try:
                return function(reading, at)
            finally:
                self.margins += self.scanned - scanned
        return run


class MeteredPattern:
    """A compiled pattern whose calls count what they read (`tests.regex_reads`), each
    call's matches checked against `re`'s own. A call that reads more than `CALL_BUDGET` is
    counted as `CALL_BUDGET + 1` and not read further: past every bound."""

    def __init__(self, pattern: re.Pattern, meter: Meter):
        self._pattern, self._meter = pattern, meter
        self._emulated = regex_reads.emulated(pattern)

    def _count(self, kind, string, pos=0, endpos=sys.maxsize):
        text = self._meter.plain(string)
        try:
            reads, spans = self._emulated.scan(kind, text, pos, endpos, budget=CALL_BUDGET)
        except regex_reads.OverBudget:
            self._meter.read(self._pattern, CALL_BUDGET + 1)
            return
        assert spans == regex_reads.real_spans(kind, self._pattern, text, pos, endpos), (self._pattern.pattern, kind)
        self._meter.read(self._pattern, reads)

    def search(self, string, pos=0, endpos=sys.maxsize):
        self._count("search", string, pos, endpos)
        return self._pattern.search(string, pos, endpos)

    def match(self, string, pos=0, endpos=sys.maxsize):
        self._count("match", string, pos, endpos)
        return self._pattern.match(string, pos, endpos)

    def fullmatch(self, string, pos=0, endpos=sys.maxsize):
        self._count("fullmatch", string, pos, endpos)
        return self._pattern.fullmatch(string, pos, endpos)

    def finditer(self, string, pos=0, endpos=sys.maxsize):
        self._count("finditer", string, pos, endpos)
        return self._pattern.finditer(string, pos, endpos)

    def sub(self, repl, string, count=0):
        assert count == 0, "a counted substitution is metered as a whole scan"
        self._count("sub", string)
        return self._pattern.sub(repl, string, count)

    def __getattr__(self, name):
        if name in ("findall", "split", "subn", "scanner"):
            raise AttributeError(f"{name} is not metered: meter it before the cut uses it")
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
#: The most one regex call is read to: past it, it is past every bound.
CALL_BUDGET = REGEX_BOUND
#: C-25.7: at most one needle look for each quote kind open at a candidate cut, two plain
#: kinds and at most `CUT_KINDS_MAX` escaped ones a side.
LOOKS_MAX = 2 * (2 + handoff.CUT_KINDS_MAX)
#: What one look reads past the text's one pass: its finds' overlaps and the runs of
#: backslashes it counts (`_LOOK` finds, each at most `_LOOK + 1` characters twice), and
#: the steps of its look at the character before the quote it found (`_run_start`).
LOOK_SLACK = 2 * LIMIT
#: The `str` scans besides the needle looks: C-25.7 states a fixed number of passes over
#: the text. This is the guard on these shapes, which read 2 to 6 (the report's table).
FIXED_PASSES = 8


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
    in characters: every regex reads within the windows, the same at 1 and at 16 MiB.
    Review r4, P3-1: past them the `str` scans are a fixed number of passes over the text
    and at most 20 needle looks, each at most one pass (the quote kinds read about 20
    passes in all, against round 5's stated 12), and a data URI body's match is counted
    by what it reads (the 480 prefixes: 2,012,537 characters as counted at round 5)."""
    reads = {}
    for size in (MIB, 16 * MIB):
        text = SHAPES[shape](size)
        cut, meter = metered_cut(text, monkeypatch)
        assert cut == handoff.cut_whole(text, LIMIT)
        reads[size] = meter
        assert meter.regex <= REGEX_BOUND, (size, meter.regex, meter.widest())
        assert max((span for _, span in meter.regex_calls), default=0) <= 2 * LIMIT, meter.widest()
        assert len(meter.looks) <= LOOKS_MAX, (size, len(meter.looks))
        assert max(meter.looks, default=0) <= len(text) + LOOK_SLACK, (size, max(meter.looks) / len(text))
        assert meter.fixed() <= FIXED_PASSES * len(text) + REGEX_BOUND, (size, meter.fixed() / len(text))
    assert reads[16 * MIB].regex <= reads[MIB].regex + LIMIT, (reads[MIB].regex, reads[16 * MIB].regex)


def test_the_quote_kinds_cost_a_look_each_and_the_meter_counts_the_body_by_what_it_reads(monkeypatch):
    """The two counts review r4 found round 5's statement missed, each seen by the meter:
    the quote kinds make 18 needle looks, each about a pass over the text; and each data
    URI body match is counted as what it reads, its match and the character after it,
    so a meter that stopped counting `_DATA_BODY_RE` (or counted it to the window's end
    again) fails here."""
    text = REVIEW_R4_SHAPES["quote-kinds"](MIB)
    _, meter = metered_cut(text, monkeypatch)
    assert len(meter.looks) == 18 and min(meter.looks) >= len(text) // 2, meter.looks
    for shape, matches in (("data-uri-prefixes", 480), ("data-uri-body", 1)):
        text = REVIEW_R4_SHAPES[shape](MIB)
        _, meter = metered_cut(text, monkeypatch)
        body = [span for pattern, span in meter.regex_calls if pattern == handoff._DATA_BODY_RE.pattern[:40]]
        assert len(body) >= matches, (shape, len(body), "data URI body matches metered")
        assert sum(body) <= 2 * LIMIT, (shape, sum(body))


#: Round 6's quote regex, which `finditer` restarted at each backslash of a run.
UNANCHORED_QUOTE_RUN = re.compile(r"(\\*)([\"'])")


def test_the_meter_counts_what_a_regex_reads_not_its_span(monkeypatch):
    """Review r5, P3-1: round 6's meter counted `_QUOTE_RUN_RE` by its span, 49,192
    characters on the backslash runs, inside the bound. Counted by what it reads, round 6's
    regex is past the bound on that shape and the anchored one reads each character of the
    windows a fixed number of times (three: the lookbehind, the run, the quote)."""
    text = REVIEW_R5_SHAPES["backslash-runs"](MIB)
    _, meter = metered_cut(text, monkeypatch)
    quote = [reads for pattern, reads in meter.regex_calls if pattern == handoff._QUOTE_RUN_RE.pattern[:40]]
    assert len(quote) == 2 and max(quote) <= 3 * (LIMIT // 2 + 8_100), quote
    monkeypatch.setattr(handoff, "_QUOTE_RUN_RE", UNANCHORED_QUOTE_RUN)
    _, meter = metered_cut(text, monkeypatch)
    assert meter.regex > REGEX_BOUND, meter.widest()


def test_a_settle_reads_a_run_of_backslashes_a_fixed_number_of_times(monkeypatch):
    """`settled_end` over one 16,000-backslash line (review r5: 1,359 ms with round 6's
    regex, and `daemon logs -f` about 0.3 s a feed after an 8,000-backslash line): its
    regexes read the text a fixed number of times, at most 16 characters a character."""
    text = "start\n" + "\\" * 16_000 + "\nnext line\n" * 50
    meter = Meter()
    with meter.installed(monkeypatch):
        settled = handoff.settled_end(Metered(text, meter), 0)
    assert settled == handoff.settled_end(text, 0) == len(text)
    assert meter.regex <= 16 * len(text), (meter.regex / len(text), meter.widest())


@settings(max_examples=400, derandomize=True, database=None, deadline=None)
@given(text=st.lists(st.sampled_from(list("ab\\\"' \n\t:=-|+<>/;,.AZ09_ſİıK\u3000") + [
    "data:", ";base64,", "-----BEGIN", " PRIVATE KEY-----", "-----END", "token", "password", "Bearer ",
    "system-reminder", "  | ", "Authorization", "--auth-token", "QUJD", "://u:p@"]), max_size=40).map("".join),
       data=st.data())
def test_the_counted_matcher_finds_what_re_finds(text, data):
    """`tests.regex_reads` matches as `re` does, for every pattern `handoff` holds and every
    kind of call the meter counts: its count is of the reads of `re`'s own matches."""
    pos = data.draw(st.integers(0, len(text)))
    endpos = data.draw(st.one_of(st.just(sys.maxsize), st.integers(pos, len(text))))
    for name, pattern in sorted(vars(handoff).items()):
        if isinstance(pattern, re.Pattern):
            emulated = regex_reads.emulated(pattern)
            for kind in ("search", "match", "fullmatch", "finditer"):
                _, spans = emulated.scan(kind, text, pos, endpos)
                assert spans == regex_reads.real_spans(kind, pattern, text, pos, endpos), (name, kind)


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
#: `_HEADER_RE`'s name (C-23.14, #239): an identifier ending in `authorization` or
#: `cookie`, case ignored, so `ſ`, `ı`, `İ` and the Kelvin sign are among its characters.
LEADING_HEADER = re.compile(r"(?i)\s*[A-Za-z0-9_-]*(?:authorization|cookie)\s*:")
#: Leads only the reading's looks are checked on: names `_HEADER_RE` reads since #239,
#: which the reference cut (cce8f1af7) does not.
WIDER_LEADS = ["X-Authorization: x\n", "  |\n  | Proxy-Authorization : y\n", "Coo\u212aie: z\n", "ıd-cookie:\n",
               "x" * 300 + "-Cookie:\n", "Cookies: no\n", "Authorization-x: no\n"]
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
@given(lead=st.sampled_from(LEADS + WIDER_LEADS), segments=st.lists(SEGMENTS, min_size=1, max_size=8), data=st.data())
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


def test_the_header_name_characters_are_every_one_the_header_regex_reads():
    """`_HEADER_NAME_CHARS`, which `_leads_with_header` scans by, holds exactly the code
    points `_HEADER_RE`'s name reads (`[A-Za-z0-9_-]`, case ignored: `ſ`, `ı`, `İ` and
    the Kelvin sign too; #239)."""
    import sys
    name = re.compile(r"(?i)[A-Za-z0-9_-]")
    assert handoff._HEADER_NAME_CHARS == "".join(chr(point) for point in range(sys.maxunicode + 1)
                                                 if name.fullmatch(chr(point)))


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
