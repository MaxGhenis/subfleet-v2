"""C-23.14 (D-SCR2 fix round 6, #239 review r6): no mode of the head's scrubber shows a
`SECRET7` value that both 473fe2941 and release/217 (`c2e080e44`'s scrubber) hide, but the
stated converse, and an excerpt cut scrubs as the whole text does there.

`scrub_473fe2941` stands for both references, as in `test_scrub_review_r2` to `_r5`; the
pre-#231 scrubber (`scrub_release217`, 3fbc4b7c5) is checked where `_new_leaks` checks it.

Review r6's inputs are regressions:
- P2-1, round 5's P3-4 and its class: a quoted header value ending in a credential flag
  whose name is a key with its separator on the next line (`--1_token`, `--password` and
  `=r` there). The quoted pass ended the value before the flag (`_owned_item`, or the
  value a secret-named flag is given), so the line-start header kept the flag's name as a
  key whose `=` is past the line, and the word after the fold's run showed in the
  excerpt, whole and `clean` modes and `detail_line`, and in daemon.log's line after a
  first line (1ad7da99c; release be0c2a311 and both references hide it). Now the union
  takes the flag with the key's value, as it does for any other such key, and each input
  gives both references' exact output in every mode. The review's generator of the class
  is rebuilt as a property.
- P3-1: two cases the lone-flag sentence does not cover, which both references, 8fde0c4ad
  and c469f44b6 show too: a quoted header on the next line, and a next-line header that
  isn't at the value's start. Each is pinned at today's output, so a change is seen.
- P3-3: both halves of be6fb4330's next-line skip in `_header_sub`, which no earlier test
  told from their mutants (M4: the skip's secret-prefix redaction; M7: its `_HEADER_RE`
  half). Each input is pinned at today's output."""

from __future__ import annotations

import itertools
import os
import random

import pytest

from subfleet.sessions import handoff
from tests.unit import scrub_473fe2941 as base473
from tests.unit.test_scrub_review_r2 import PREFIX, _new_leaks, _outputs, _secrets, _splits, _unstable

#: Both references, as one: 473fe2941 and c2e080e44 give the same output in every mode on
#: every text this module draws (measured against both real trees, the report's round-6
#: section).
BOTH = {"473fe2941 and c2e080e44": base473}
MODES = ("excerpt", "whole", "clean", "daemon.log line", "detail_line")


def _pinned(excerpt: str, clean: str, line: str, detail: str) -> dict[str, str]:
    """A text's five outputs, the excerpt's and whole text's alike."""
    return dict(zip(MODES, (excerpt, excerpt, clean, line, PREFIX + detail)))


# --- P2-1: a quoted header value's flag whose name is a key past its line ---------------

#: Round 5's four P3-4 inputs (two secret-named flags, a digit-named one and the `:r` form)
#: and review r6's five siblings: three ordinary flags (`--password`, `--api-key`) and two
#: quoted headers on the line after a header whose value would start there. Each pinned to
#: both references' output in every mode. At 1ad7da99c each showed `SECRET7c5` in the
#: excerpt, whole, `clean` and `detail_line` modes, and two in daemon.log's line too.
#: Both references show it in one mode only: `detail_line` of the quoted next-line
#: `Cookie:`, which this scrubber now shows there too.
P2_1 = {
    "quoted-flag-equals": ('\nAuthorization:"--1_token\n=r SECRET7c5',
                           _pinned('\nAuthorization:[REDACTED]', 'Authorization:[REDACTED]', 'Authorization:[REDACTED]',
                                   'Authorization:[REDACTED]')),
    "quoted-digit-flag": ('\nAuthorization: "x --db2-password\n=r SECRET7c5',
                          _pinned('\nAuthorization: [REDACTED]', 'Authorization: [REDACTED]', 'Authorization: [REDACTED]',
                                  'Authorization: [REDACTED]')),
    "single-quoted-secret-flag": ("x\nCookie: '--SECRET7a1_token\n=r SECRET7c5",
                                  _pinned('x\nCookie: [REDACTED]', 'x\nCookie: [REDACTED]', 'x Cookie: [REDACTED]',
                                          'x Cookie: [REDACTED]')),
    "quoted-flag-colon": ('\nAuthorization:"--1_token\n:r SECRET7c5',
                          _pinned('\nAuthorization:[REDACTED]', 'Authorization:[REDACTED]', 'Authorization:[REDACTED]',
                                  'Authorization:[REDACTED]')),
    "ordinary-flag": ('\nAuthorization:"--password\n=r SECRET7c5',
                      _pinned('\nAuthorization:[REDACTED]', 'Authorization:[REDACTED]', 'Authorization:[REDACTED]',
                              'Authorization:[REDACTED]')),
    "ordinary-flag-after-a-word": ('\nAuthorization: "x --api-key\n=r SECRET7c5',
                                   _pinned('\nAuthorization: [REDACTED]', 'Authorization: [REDACTED]',
                                           'Authorization: [REDACTED]', 'Authorization: [REDACTED]')),
    "ordinary-flag-colon": ("\nCookie: '--password\n:r SECRET7c5",
                            _pinned('\nCookie: [REDACTED]', 'Cookie: [REDACTED]', 'Cookie: [REDACTED]',
                                    'Cookie: [REDACTED]')),
    "quoted-header-on-the-next-line": ("Authorization:\n'Cookie: --api-key\n=r SECRET7c5",
                                       _pinned('Authorization:\n[REDACTED]', 'Authorization:\n[REDACTED]',
                                               'Authorization: [REDACTED]', 'Authorization: [REDACTED] SECRET7c5')),
    "quoted-header-on-the-next-line-after-a-line": ('x\nAuthorization:\n"Cookie: x --password\n=r SECRET7c5',
                                                    _pinned('x\nAuthorization:\n[REDACTED]', 'x\nAuthorization:\n[REDACTED]',
                                                            'x Authorization: [REDACTED]', 'x Authorization: [REDACTED]')),
}


@pytest.mark.parametrize("text,pinned", P2_1.values(), ids=P2_1.keys())
def test_review_r6_s_p2_1_inputs_give_both_references_output_in_every_mode(text, pinned):
    """Every mode gives both references' output, so none shows a value they hide; no
    excerpt cut disagrees with the whole text, and a second scrub reveals nothing."""
    assert _outputs(base473, text) == pinned
    assert _outputs(handoff, text) == pinned
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}


#: Review r6's generator of the class (its P2-1): a lead, a header (or a plain key), an
#: opening quote, a value before the flag, the flag, the gap to the next line, that line's
#: first run, the rest of the line, and a tail.
P3_4_PRE = ("\n", "x\n", "", "curl -H ", "log ", "x ", "\n  ", "-H '")
P3_4_HEADERS = ("Authorization:", "Authorization: ", "Cookie: ", "Set-Cookie:", "cookie:", "X-Authorization: ",
                "Proxy-Authorization:", "api_key:", "password=", "token: ")
P3_4_QUOTES = ('"', "'", "", '\\"')
P3_4_VALUES = ("", "x ", "a=1 ")
P3_4_FLAGS = ("--1_token", "--db2-password", "--SECRET7a1_token", "--password", "--api-key", "x--1_token")
P3_4_GAPS = ("\n", "\r\n", "  \n", "\n  ", "\n\n")
P3_4_RUNS = ("=r", ":r", "r", "=", ":", "=SECRET7r9", ";r", ",r", "'r", "-r", "Cookie:")
P3_4_RESTS = (" SECRET7c5", " x SECRET7c5", "\tSECRET7c5", " SECRET7c5 --password\nSECRET7c6", "", " SECRET7c5'")
P3_4_TAILS = ("", "\nSECRET7t5", "'", '"')
P3_4_PARTS = (P3_4_PRE, P3_4_HEADERS, P3_4_QUOTES, P3_4_VALUES, P3_4_FLAGS, P3_4_GAPS, P3_4_RUNS, P3_4_RESTS,
              P3_4_TAILS)
#: The review gives the core's size (6,720 cells), not its shape. This one is every lead,
#: header, quote, value and flag at round 5's input's gap, run and rest (`\n`, `=r` and
#: ` SECRET7c5`): 5,760; and every header, quote and flag after its lead (`\n`) with each
#: other run that starts with `=` or `:`: 960.
P3_4_OTHER_RUNS = (":r", "=", ":", "=SECRET7r9")
P3_4_DRAWS = 30000
P3_4_SEED = 6239
P3_4_SAMPLE = 600


def p3_4_core() -> list[str]:
    """6,720 cells, each holding a secret."""
    first = [pre + header + quote + value + flag + "\n=r SECRET7c5" for pre, header, quote, value, flag in
             itertools.product(P3_4_PRE, P3_4_HEADERS, P3_4_QUOTES, P3_4_VALUES, P3_4_FLAGS)]
    other = [f"\n{header}{quote}{flag}\n{run} SECRET7c5" for header, quote, flag, run in
             itertools.product(P3_4_HEADERS, P3_4_QUOTES, P3_4_FLAGS, P3_4_OTHER_RUNS)]
    return first + other


def p3_4_class() -> list[str]:
    """The core, then 30,000 seeded draws over the whole product (8 x 10 x 4 x 3 x 6 x 5 x
    11 x 6 x 4 parts, 7,603,200 cells), each kept once and only if it holds a secret."""
    rng = random.Random(P3_4_SEED)
    draws = ("".join(rng.choice(part) for part in P3_4_PARTS) for _ in range(P3_4_DRAWS))
    return list(dict.fromkeys(cell for cell in itertools.chain(p3_4_core(), draws) if _secrets(cell)))


def _p3_4_failures(cells) -> dict[str, list]:
    """Invariants 1 to 3 over `cells`, against both references as one: a value both hide
    that the head shows, an excerpt cut that disagrees with the whole text, a second scrub
    that shows what the first hid."""
    failures = {"leaks": [], "splits": [], "unstable": []}
    for cell in cells:
        if (leaks := _new_leaks(cell, BOTH)):
            failures["leaks"].append((cell, leaks))
        if (splits := _splits(cell)):
            failures["splits"].append((cell, splits))
        if (unstable := _unstable(cell)):
            failures["unstable"].append((cell, unstable))
    return failures


CLEAN = {"leaks": [], "splits": [], "unstable": []}


def test_the_p3_4_class_holds_round_5_s_inputs_and_the_ordinary_flags():
    core = p3_4_core()
    assert len(core) == len(set(core)) == 6720 and all(_secrets(cell) for cell in core)
    cells = p3_4_class()
    assert len(cells) == len(set(cells)) == 33740
    inline = [text for name, (text, _) in P2_1.items() if not name.startswith("quoted-header-on-the-next-line")]
    assert len(inline) == 7 and set(inline) <= set(core)


def test_no_cell_of_a_p3_4_class_sample_shows_what_both_references_hide():
    """600 of the class's 33,740 cells, seed 6239, with invariants 1 to 3 (1ad7da99c: 45 of them
    show a value both references hide)."""
    cells = random.Random(P3_4_SEED).sample(p3_4_class(), P3_4_SAMPLE)
    assert _p3_4_failures(cells) == CLEAN


@pytest.mark.skipif(os.environ.get("SF_SCRUB_FULL_MATRIX") != "1", reason="the whole P3-4 class, on request")
def test_no_cell_of_the_whole_p3_4_class_shows_what_both_references_hide():
    assert _p3_4_failures(p3_4_class()) == CLEAN


# --- P3-1: the lone-flag sentence's two named exceptions --------------------------------

#: A header whose value would start on the next line, where a header the lone-flag sentence
#: does not reach takes the line: (a) a quoted one, which the quoted pass reads with the
#: first header as a key and its unclosed value, ended before the flag; (b) one that isn't
#: at the value's start, which the inline rule takes as the first header's value, up to the
#: flag. `SECRET7c3` shows in the excerpt, whole and `clean` modes; both references show it
#: in four or five modes, and so do 8fde0c4ad and c469f44b6 (review r6, P3-1). Pinned at
#: today's output, mode by mode.
LONE_FLAG_EXCEPTIONS = {
    "quoted-next-line-header": ("Authorization:\n'Cookie: --api-key\nSECRET7c3",
                                _pinned('Authorization:\n[REDACTED] --api-key\nSECRET7c3',
                                        'Authorization:\n[REDACTED] --api-key\nSECRET7c3', 'Authorization: [REDACTED]',
                                        'Authorization: [REDACTED] --api-key [REDACTED]')),
    "next-line-header-not-at-the-value-s-start": ("x Cookie:\nx Cookie: --api-key\nSECRET7c3",
                                                  _pinned('x Cookie:\n[REDACTED] --api-key\nSECRET7c3',
                                                          'x Cookie:\n[REDACTED] --api-key\nSECRET7c3',
                                                          'x Cookie: [REDACTED] --api-key [REDACTED]',
                                                          'x Cookie: [REDACTED] --api-key [REDACTED]')),
}


@pytest.mark.documented_limitation
@pytest.mark.parametrize("text,pinned", LONE_FLAG_EXCEPTIONS.values(), ids=LONE_FLAG_EXCEPTIONS.keys())
def test_a_lone_flag_the_sentence_does_not_reach_shows_its_value_as_stated(text, pinned):
    """C-23.14's lone-flag sentence names both cases. Nothing shows that a reference hides."""
    assert _outputs(handoff, text) == pinned
    assert {mode for mode, out in pinned.items() if "SECRET7c3" in out} == {"excerpt", "whole", "clean"}
    assert all("SECRET7c3" in _outputs(base473, text)[mode] for mode in ("excerpt", "whole", "clean", "detail_line"))
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}


# --- P3-3: be6fb4330's next-line skip, both halves --------------------------------------

#: M4 (the skip leaves the name's secret part): `SECRET7ab1` showed in the excerpt, whole
#: and `clean` modes; C-23.14's prefix sentence says it goes. M7 (the skip without its
#: `_HEADER_RE` half): `SECRET7c3` showed in three modes. Both references show each value,
#: so no leak check sees either mutant. Pinned at today's output, mode by mode.
NEXT_LINE_SKIP = {
    "secret-prefix-goes": ("x\nSECRET7ab1Cookie\n:\nCookie: v SECRET7c3",
                           _pinned('x\n[REDACTED]Cookie\n:\n[REDACTED] [REDACTED]',
                                   'x\n[REDACTED]Cookie\n:\n[REDACTED] [REDACTED]',
                                   'x [REDACTED]Cookie : [REDACTED] [REDACTED]',
                                   'x [REDACTED]Cookie : [REDACTED] [REDACTED]')),
    "line-start-header-read-across-its-colon": ("Authorization:\nCookie\n: --api-key\nSECRET7c3",
                                                _pinned('Authorization:\n[REDACTED]\n: [REDACTED]\n[REDACTED]',
                                                        'Authorization:\n[REDACTED]\n: [REDACTED]\n[REDACTED]',
                                                        'Authorization: [REDACTED]',
                                                        'Authorization: [REDACTED] : [REDACTED] [REDACTED]')),
}


@pytest.mark.parametrize("text,pinned", NEXT_LINE_SKIP.values(), ids=NEXT_LINE_SKIP.keys())
def test_the_next_line_skip_s_halves_are_pinned(text, pinned):
    outputs = _outputs(handoff, text)
    assert outputs == pinned
    assert all(secret not in out for out in outputs.values() for secret in _secrets(text))
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}
