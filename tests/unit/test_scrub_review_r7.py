"""C-23.14 (D-SCR2 fix round 7, #239 review r7): no mode of the head's scrubber shows a
`SECRET7` value that both 473fe2941 and release/217 (`c2e080e44`'s scrubber) hide, but the
stated converse, and an excerpt cut scrubs as the whole text does there.

`scrub_473fe2941` stands for both references, as in `test_scrub_review_r2` to `_r6`.

Review r7's inputs are regressions:
- P2-1: a credential flag on the line after a header name that an unclosed quoted value
  swallows, whose name is a key with its separator on the line after that (`--password`,
  and `:r` there). The quoted pass's two other item sites, the swallowed header name's
  value and a key's plain value within a quoted match, ended the value before the flag
  (`owned_before`, `_owned_item` and `_secret_flag`), so the key rule took only the run's
  word and the next one showed in the excerpt, whole and `clean` modes (36590532c, and
  every round's head since 8ed254385; release be0c2a311, both references and base
  946ea9f96 hide it). Now both sites ask round 6's question (`_key_past_line`) when they
  find an item, and a flag whose name is such a key ends nothing there either: the union
  takes it with the key's value, as both references do. The review's generator of the
  family is rebuilt as a property.
- P3-2: the `keyed` memo (round 6) was untested, and without it the scrub is quadratic:
  each unclosed value on a line walked the line's last identifier back again. Counted
  here: one `_key_past_line` call per line end, and an identifier walk that stays under
  the text's length."""

from __future__ import annotations

import itertools
import os
import random

import pytest

from subfleet.conversations import suggestions
from subfleet.sessions import handoff
from tests.unit import scrub_473fe2941 as base473
from tests.unit.test_scrub_review_r2 import PREFIX, _adversary, _new_leaks, _outputs, _secrets, _splits, _unstable
from tests.unit.test_scrub_review_r3 import _assert_linear
from tests.unit.test_scrub_review_r6 import CLEAN, _p3_4_failures, _pinned, _stratified_sample


# --- P2-1: a flag after a swallowed header name, whose name is a key past its line ---------

#: Review r7's two minimized inputs, each pinned to both references' output in every mode:
#: (A) the swallowed header name's value is a lone `--password` on the next line, whose name
#: is a key with `:` on the line after; (B) a key's plain value within the quoted match ends
#: in a secret-named flag (`--1_token`) whose name is such a key. At 36590532c each showed
#: `SECRET7c5` in the excerpt, whole and `clean` modes. Both references show it in
#: `detail_line` only, and this scrubber does too.
P2_1 = {
    "swallowed-header-name-lone-flag": ('Authorization:"Authorization:\n--password\n:r SECRET7c5',
                                        _pinned('Authorization:[REDACTED]', 'Authorization:[REDACTED]',
                                                'Authorization:[REDACTED]', 'Authorization:[REDACTED] SECRET7c5')),
    "quoted-key-s-secret-named-flag": ('Set-Cookie:"Authorization\n:--1_token\n=r SECRET7c5',
                                       _pinned('Set-Cookie:[REDACTED]', 'Set-Cookie:[REDACTED]', 'Set-Cookie:[REDACTED]',
                                               'Set-Cookie:[REDACTED] SECRET7c5')),
}


@pytest.mark.parametrize("text,pinned", P2_1.values(), ids=P2_1.keys())
def test_review_r7_s_p2_1_inputs_give_both_references_output_in_every_mode(text, pinned):
    """Every mode gives both references' output, so none shows a value they hide; no
    excerpt cut disagrees with the whole text, and a second scrub reveals nothing."""
    assert _outputs(base473, text) == pinned
    assert _outputs(handoff, text) == pinned
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}


#: The two of review r7's 15,000 seeded joins (seed 7241, its `gen.py`) that showed the
#: family's value at 36590532c, in the excerpt and `clean` modes (at 1ad7da99c the second
#: shows it in the whole text too). This scrubber now gives both references' output in
#: four modes; in `detail_line` it hides more (the quotes the references keep, and in the
#: second `SECRET7c5`, which both references show there).
JOINS = {
    "join-after-a-bearer-line": (
        'Authorization:"Authorization:\ny token: Bearer SECRET7un186x rest x Set-Cookie:\'a=1 --db2-password\n'
        '  :r SECRET7c5"',
        _pinned('Authorization:[REDACTED]', 'Authorization:[REDACTED]', 'Authorization:[REDACTED]',
                'Authorization:"[REDACTED]"'),
        PREFIX + '[REDACTED]'),
    "join-of-three-headers": (
        'Set-Cookie:\r\n"Authorization :\n\nSECRET7t5\tx Authorization:"x x--1_token\n  = x SECRET7c5\'\r\n'
        "cookie:'a=1 --1_token\n\n=r SECRET7c5''",
        _pinned('Set-Cookie:\r\n[REDACTED]\ncookie:[REDACTED]', 'Set-Cookie:\r\n[REDACTED]\ncookie:[REDACTED]',
                'Set-Cookie: [REDACTED]', 'Set-Cookie: "[REDACTED]" SECRET7c5\' cookie:[REDACTED]'),
        PREFIX + 'Set-Cookie: [REDACTED] [REDACTED] cookie:[REDACTED]'),
}


@pytest.mark.parametrize("text,references,detail", JOINS.values(), ids=JOINS.keys())
def test_review_r7_s_two_joins_show_nothing_both_references_hide(text, references, detail):
    assert _outputs(base473, text) == references
    assert _outputs(handoff, text) == references | {"detail_line": detail}
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}


#: Review r7's generator of the family (its P2-1): an outer header or key, an opening quote,
#: the inner header name the quoted value swallows, the gap to the next line, a separator,
#: the flag, the gap to the line after, that line's first run, the rest of the line, and a
#: tail (11 x 4 x 10 x 4 x 4 x 7 x 3 x 6 x 5 x 4 parts, 2,217,600 cells).
FAMILY_OUTER = ("Authorization:", "Set-Cookie:", "Cookie: ", "x Cookie: ", "api_key:", "secret:", "token: ", "curl -H ",
                "\n", "x\n", "password=")
FAMILY_QUOTES = ('"', "'", '\\"', "")
FAMILY_INNER = ("Authorization:", "Authorization", "Cookie: ", "Set-Cookie :", "X-Authorization:", "cookie", "x Cookie:",
                "token:", "api_key=", "password")
FAMILY_GAPS = ("\n", "\r\n", "\n  ", "\n\n")
FAMILY_SEPARATORS = ("", ":", ": ", "=")
FAMILY_FLAGS = ("--password", "--api-key", "--1_token", "--SECRET7a1_token", "--db2-password", "x --password", "--token")
FAMILY_NEXT_GAPS = ("\n", "\r\n", "\n  ")
FAMILY_RUNS = (":r", "=r", "=", ":", "=SECRET7r9", "r")
FAMILY_RESTS = (" SECRET7c5", " x SECRET7c5", "\tSECRET7c5", " SECRET7c5'", "")
FAMILY_TAILS = ("", '"', "'", "\nSECRET7t5")
FAMILY_PARTS = {"outer": FAMILY_OUTER, "quote": FAMILY_QUOTES, "inner": FAMILY_INNER, "gap": FAMILY_GAPS,
                "separator": FAMILY_SEPARATORS, "flag": FAMILY_FLAGS, "next gap": FAMILY_NEXT_GAPS, "run": FAMILY_RUNS,
                "rest": FAMILY_RESTS, "tail": FAMILY_TAILS}
FAMILY_DRAWS = 30000
FAMILY_SEED = 7243
FAMILY_SAMPLE = 600

#: The cells of `family_class()`, by index, that the default sample always holds: a greedy
#: cover, over the 5,095 cells that showed a value both references hide at 36590532c, of
#: every part value (46), mode set (2) and pair of two parts' values (916) those cells
#: hold, so the sample fails at 36590532c for every shape the whole class fails for (69
#: cells; measured against the real trees, the report's round-7 section).
FAMILY_STRATA = (
    0, 76, 82, 112, 114, 124, 172, 224, 228, 294, 298, 336, 392, 448, 460, 690, 3017, 24696, 25073, 25103, 25335,
    25338, 25404, 25611, 25900, 26501, 26503, 26739, 26966, 27709, 27798, 27832, 28030, 28560, 28816, 29346,
    30204, 30521, 30960, 31104, 31222, 31783, 31798, 32663, 32936, 33833, 34764, 34833, 35223, 35369, 35807,
    36258, 36480, 36740, 37045, 38830, 39319, 39519, 39553, 40848, 41165, 42402, 43105, 44463, 46966, 47268,
    47700, 51048, 51314)
#: The 12 part values no cell of the class showed a value with at 36590532c.
FAMILY_NEVER_SHOWN = {("outer", "\n"), ("outer", "x\n"), ("outer", "api_key:"), ("outer", "curl -H "),
                      ("outer", "password="), ("outer", "secret:"), ("outer", "token: "), ("outer", "x Cookie: "),
                      ("quote", ""), ("flag", "x --password"), ("run", "r"), ("rest", "")}


def family_core() -> list[tuple[str, ...]]:
    """24,640 cells' parts: every outer, quote, inner name, separator and flag, at a line
    break each side, with either of the runs `:r` and `=r` before ` SECRET7c5`."""
    return [(outer, quote, inner, "\n", separator, flag, "\n", run, " SECRET7c5", "")
            for outer, quote, inner, separator, flag, run in itertools.product(
                FAMILY_OUTER, FAMILY_QUOTES, FAMILY_INNER, FAMILY_SEPARATORS, FAMILY_FLAGS, (":r", "=r"))]


def family_parts() -> dict[str, tuple[str, ...]]:
    """Each cell of the class, in its order, with the parts that first make it: the core,
    then 30,000 seeded draws over the whole product (seed 7243), each kept once and only if
    it holds a secret."""
    rng = random.Random(FAMILY_SEED)
    draws = (tuple(rng.choice(part) for part in FAMILY_PARTS.values()) for _ in range(FAMILY_DRAWS))
    cells: dict[str, tuple[str, ...]] = {}
    for parts in itertools.chain(family_core(), draws):
        if _secrets(cell := "".join(parts)):
            cells.setdefault(cell, parts)
    return cells


def family_class() -> list[str]:
    return list(family_parts())


def family_sample() -> list[str]:
    """The strata, then seeded draws from the rest of the class, 600 cells in all."""
    return _stratified_sample(family_class(), FAMILY_STRATA, FAMILY_SEED, FAMILY_SAMPLE)


def test_the_family_holds_review_r7_s_inputs_and_a_cell_of_every_shown_part_value():
    core = ["".join(parts) for parts in family_core()]
    assert len(core) == len(set(core)) == 24640 and all(_secrets(cell) for cell in core)
    cells = family_parts()
    assert len(cells) == 51371
    assert all(text in core for text, _ in P2_1.values())
    sample = family_sample()
    assert len(sample) == len(set(sample)) == FAMILY_SAMPLE
    order = list(cells)
    held = {(name, value) for index in FAMILY_STRATA for name, value in zip(FAMILY_PARTS, cells[order[index]])}
    every = {(name, value) for name, part in FAMILY_PARTS.items() for value in part}
    assert held == every - FAMILY_NEVER_SHOWN


def test_no_cell_of_a_family_sample_shows_what_both_references_hide():
    """600 of the family's 51,371 cells: the 69 strata and 531 seeded draws, with invariants
    1 to 3 (36590532c: 117 of them show a value both references hide, the 69 strata among
    them)."""
    assert _p3_4_failures(family_sample()) == CLEAN


@pytest.mark.skipif(os.environ.get("SF_SCRUB_FULL_MATRIX") != "1", reason="the whole P2-1 family, on request")
def test_no_cell_of_the_whole_family_shows_what_both_references_hide():
    assert _p3_4_failures(family_class()) == CLEAN


# --- P3-2: the `keyed` memo reads a line's end once -------------------------------------

def _memo_probe(values: int, identifier: int) -> str:
    """Review r7's P3-2 input: `_adversary(values)`'s unclosed values on one line, its last
    word a credential flag glued to an identifier of `identifier` characters
    (` --aaa…_password`) whose name is a key with `=` on the next line. Each unclosed value
    ends at that line's end, and asks whether a key there reaches past it."""
    return _adversary(values)[:-len(" token\n")] + " --" + "a" * identifier + "_password\n=x SECRET7c5"


#: The modes that scrub the whole probe: the excerpt keeps all of it (`SCRUB_MAX_CHARS` is
#: 256 KiB, the largest probe 226,924 characters), and `detail_line`'s is the scrub of its
#: prefixed text.
MEMO_MODES = {"excerpt": lambda text: handoff.scrub_secrets(text)[0],
              "whole": lambda text: handoff.scrub_secrets(text, whole=True)[0],
              "detail_line": lambda text: suggestions._scrub_line(PREFIX + text)[0]}


@pytest.mark.parametrize("mode", MEMO_MODES.values(), ids=MEMO_MODES.keys())
def test_unclosed_values_sharing_a_line_end_read_the_key_past_it_once(mode, monkeypatch):
    """C-23.14: the unclosed values on a line read the line's end once, however many share
    it. 300 values share one, and `_key_past_line` reads it once; without the `keyed`
    memo, 300 times, each walking the 40,000-character identifier back (review r7: 2.6 s,
    against the head's 13 ms)."""
    calls = []
    original = handoff._key_past_line
    monkeypatch.setattr(handoff, "_key_past_line",
                        lambda text, start, end: calls.append(end) or original(text, start, end))
    text = _memo_probe(300, 40000)
    mode(text)
    assert len(calls) == len(set(calls)) <= text.count("\n") + 1, calls[:5]


def _memo_counts(pair, monkeypatch) -> tuple[dict[str, list[dict[str, int]]], list[int]]:
    """For each mode, `_kept_span`'s calls and the Python-level identifier walk
    (`_identifier_char` calls) on the two probes of `pair`, as `test_scrub_review_r3`'s
    `_counts` counts them; and the two probes' lengths."""
    counts = {"kept": 0, "walk": 0}
    kept, walk = handoff._kept_span, handoff._identifier_char

    def counted_kept(text, start, stop):
        counts["kept"] += 1
        return kept(text, start, stop)

    def counted_walk(char):
        counts["walk"] += 1
        return walk(char)
    monkeypatch.setattr(handoff, "_kept_span", counted_kept)
    monkeypatch.setattr(handoff, "_identifier_char", counted_walk)
    texts = [_memo_probe(*size) for size in pair]
    found = {}
    for name, mode in MEMO_MODES.items():
        found[name] = []
        for text in texts:
            counts.update(kept=0, walk=0)
            mode(text)
            found[name].append(dict(counts))
    return found, [len(text) for text in texts]


#: Twice the values (88,474 to 226,924 characters) and twice the identifier (to 128,474),
#: from review r7's 300 values and 40,000 characters.
MEMO_PAIRS = {"2x-values": ((300, 40000), (600, 40000)), "2x-identifier": ((300, 40000), (300, 80000))}


@pytest.mark.parametrize("pair", MEMO_PAIRS.values(), ids=MEMO_PAIRS.keys())
def test_the_memo_s_probe_scrubs_in_linear_time(pair, monkeypatch):
    """`_assert_linear` on each pair, and the identifier walk stays under the text's length
    in every mode: the head walks the identifier once (40,012 and 80,012 calls; 1.0x at
    twice the values, 2.0x at twice the identifier). Without the memo each value walks it,
    12,003,600 calls on 88,474 characters. Each pair alone grows one factor of that
    product, so only the bound sees the quadratic: `_assert_linear`'s 4x passes it."""
    found, lengths = _memo_counts(pair, monkeypatch)
    _assert_linear(found)
    assert all(counts["walk"] <= length for both in found.values() for counts, length in zip(both, lengths)), found
