"""C-23.14 (D-SCR2 fix round 5, #239 review r5): no mode of the head's scrubber shows a
`SECRET7` value that 473fe2941 or release/217 (`c2e080e44`'s scrubber) hides, but the
stated converse (and, through round 5, the quoted-value join below), and an excerpt cut
scrubs as the whole text does there.

`scrub_473fe2941` stands for both references, as in `test_scrub_review_r2` to `_r4`; the
pre-#231 scrubber (`scrub_release217`, 3fbc4b7c5) is checked where `_failures` checks it.

Review r5's executed inputs are regressions, each failing at 8fde0c4ad:
- P2-1: `_HEADER_RE` reads a header across a line break (`\\s*:\\s*`), on either side of
  its colon, but three earlier rules still took part of such a header and left its value
  to no rule: the inline rule, whose value would start on the next line, read past a
  header whose colon a line break parts from its name; a fold at a header's name, and a
  refused `Bearer`'s line, took the line where the header's value starts on the next one.
  The review's generator of the class is rebuilt as a property.
- P3-1: two regressions against c469f44b6 (a refused `Bearer` before a line-start header
  that holds a secret part and ends in a lone flag; a line-start header whose value would
  start on the next line, at another header ending in a lone flag), and the lone-flag
  sentence (invariant 6) over the leads it now claims: folds, refused `Bearer`s, and
  headers whose value would start on the header's line.
- P3-2: an excerpt cut where the prefix rule took a name's leading `-`, which the whole
  text's last fold then no longer refused (invariant 3).
- P3-3: `_ENDS_EARLY` held a text past the call that read it, and one thread's scrub
  overwrote another's.

P3-4, a quoted header value ending in a secret-named flag whose fold takes only the next
line's first run, starting with `=` or `:`, was stated here, not fixed: it showed the line's
next word in the excerpt, whole, `clean` and `detail_line` modes. Fix round 6 (#239 review
r6, P2-1) closes it, with its class (`test_scrub_review_r6`): its four inputs now give both
references' output in every mode."""

from __future__ import annotations

import itertools
import os
import random
import threading

import pytest
from hypothesis import given, strategies as st

from subfleet.sessions import handoff
from tests.unit import scrub_473fe2941 as base473
from tests.unit.test_scrub_key_order import CI, _neutral_breaks
from tests.unit.test_scrub_review_r2 import _new_leaks, _outputs, _secrets, _splits, _unstable
from tests.unit.test_scrub_review_r3 import CLEAN, HN_GAPS, _failures, _trailer
from tests.unit.test_scrub_review_r4 import (ENDING_FLAG_UNITS, LINE_START_NAMES, LINE_START_SEPS, LONE_FLAGS,
                                             LONE_NAMES, P3_1_CLASS_LEADS, REFUSED_BEARER_LEADS)

#: Both references, as one: 473fe2941 and c2e080e44 give the same output in every mode on
#: every text this module draws (measured against both real trees, the report's round-5
#: section).
BOTH = {"473fe2941 and c2e080e44": base473}

# --- P2-1: a header read across a line break ---------------------------------------------

#: Review r5's five inputs, each with 473fe2941's exact output in the excerpt and whole
#: modes: an inline header whose value would start on the next line, at a header whose colon
#: a line break parts from its name (two), a fold whose run is a header whose value starts
#: on the line after its colon (two), and a refused `Bearer`'s line there (one).
P2_1 = {
    "inline-next-line": ("x Cookie:\nSet-Cookie\n:v SECRET7v2", "x Cookie:\n[REDACTED]\n:[REDACTED]"),
    "inline-next-line-crlf": ("curl -H 'Authorization:\nSet-Cookie\r\n: Basic SECRET7v2",
                              "curl -H 'Authorization:\n[REDACTED]\r\n: [REDACTED]"),
    "fold-lone-flag": ("Cookie: --password\nSet-Cookie :\n;v SECRET7v2", "Cookie: [REDACTED]\nSet-Cookie :\n[REDACTED]"),
    "refused-bearer": ("Authorization: Bearer\nSet-Cookie\x0b:\nv SECRET7v2",
                       "Authorization: [REDACTED]\nSet-Cookie\x0b:\n[REDACTED]"),
    "fold-secret-flag": ("Bearer --SECRET7a1_token\nSet-Cookie :\n;v SECRET7v2", "Bearer [REDACTED]\nSet-Cookie :\n[REDACTED]"),
}


@pytest.mark.parametrize("text,scrubbed", P2_1.values(), ids=P2_1.keys())
def test_review_r5_s_p2_1_inputs_are_hidden_in_every_mode(text, scrubbed):
    """No mode shows `SECRET7v2` (8fde0c4ad showed it in four or all five), none shows what
    a reference hides, no excerpt cut disagrees with the whole text, a second scrub reveals
    nothing, and the excerpt and whole scrubs give 473fe2941's exact output."""
    assert all("SECRET7v2" not in out for out in _outputs(handoff, text).values())
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}
    assert handoff.scrub_secrets(text)[0] == handoff.scrub_secrets(text, whole=True)[0] == scrubbed
    assert base473.scrub_secrets(text)[0] == scrubbed


#: Review r5's generator of the class, rebuilt from what it states: 22 leads (round 4's 13
#: folding leads and 3 refused `Bearer`s, and 6 headers whose value would start on the next
#: line: inline, quoted, and one at a line start), 6 gaps, 6 header names, 15 separators
#: between a name and its value (round 4's 8, which part the name from its colon, and 7 that
#: put a line break after the colon or on both sides), and 8 values: 95,040 cells.
P2_1_LEADS = (*P3_1_CLASS_LEADS, *REFUSED_BEARER_LEADS, "x Cookie:", "curl -H 'Authorization:", 'curl -H "Cookie:',
              "wget --header='Set-Cookie:", "log Proxy-Authorization:", "Authorization:")
P2_1_GAPS = (*HN_GAPS, "\r\n\r\n")
P2_1_NAMES = (*LINE_START_NAMES, "Cookie", "Proxy-Authorization", "X-Authorization")
P2_1_SEPS = (*LINE_START_SEPS, "\n:", "\r\n: ", " :\n", "\x0b:\n", ":\n", ":\r\n", "\n:\n")
P2_1_VALUES = ("v SECRET7v2", "Basic SECRET7v2", ";v SECRET7v2", "--api-key\nSECRET7v2", "a=1; sid=SECRET7v2",
               "'v SECRET7v2'", "v\tSECRET7v2 tail", "client_secret=\nSECRET7v2")
#: Its tails, after a cell, and its neighbours: the two shapes of P3-1's regressions at every
#: lead, gap and name.
P2_1_TAILS = ("", " tail", "\nSECRET7t5", "'")
P2_1_NEIGHBOURS = ("{name}:\nCookie: --api-key\nSECRET7m1", "SECRET7ab1{name} : --password\nSECRET7c3")


def p2_1_class() -> list[str]:
    """22 leads x 6 gaps x 6 names x 15 separators x 8 values: 95,040 cells."""
    return [lead + gap + name + sep + value for lead, gap, name, sep, value in itertools.product(
        P2_1_LEADS, P2_1_GAPS, P2_1_NAMES, P2_1_SEPS, P2_1_VALUES)]


P2_1_SLICE = 3000
P2_1_SEED = 2395


def test_the_p2_1_class_holds_the_review_s_inputs():
    cells = p2_1_class()
    assert len(cells) == len(set(cells)) == 95040 and all(_secrets(cell) for cell in cells)
    assert {text for text, _ in P2_1.values()} <= set(cells)


def test_no_cell_of_a_p2_1_class_slice_shows_what_both_references_hide():
    """3,000 of the 95,040 cells, seed 2395 (8fde0c4ad: 73 of them show a value both
    references hide), with invariants 1 to 3: no new leak against either reference, no
    excerpt cut that disagrees with the whole text, no second scrub that reveals."""
    cells = random.Random(P2_1_SEED).sample(p2_1_class(), P2_1_SLICE)
    assert _failures(cells) == CLEAN


@pytest.mark.skipif(os.environ.get("SF_SCRUB_FULL_MATRIX") != "1", reason="the whole 95,040-cell class, on request")
def test_no_cell_of_the_whole_p2_1_class_shows_what_both_references_hide():
    assert [cell for cell in p2_1_class() if _new_leaks(cell, BOTH)] == []


@st.composite
def _p2_1_drawn(draw):
    """A cell of the class with a tail, or a neighbour, or one of each joined."""
    lead, gap, name = (draw(st.sampled_from(part)) for part in (P2_1_LEADS, P2_1_GAPS, P2_1_NAMES))
    if draw(st.booleans()):
        text = lead + gap + name + draw(st.sampled_from(P2_1_SEPS)) + draw(st.sampled_from(P2_1_VALUES))
    else:
        text = lead + gap + draw(st.sampled_from(P2_1_NEIGHBOURS)).format(name=name)
    return text + draw(st.sampled_from(P2_1_TAILS))


@CI
@given(text=_p2_1_drawn())
def test_no_drawn_cell_of_the_p2_1_class_shows_what_both_references_hide(text):
    """The class with its tails and neighbours, as a property (the suite's ci profile: 200
    examples, derandomized): no mode shows a `SECRET7` value both references hide."""
    assert _new_leaks(text, BOTH) == {}, text


# --- P3-1: two regressions against c469f44b6, and the lone-flag sentence -----------------

#: Each hides its value in every mode (c469f44b6 did; 8fde0c4ad showed it in the excerpt,
#: whole and `clean` modes; both references show it). The first: P3-1's rule in
#: `_refused_bearer` (the refused `Bearer`'s line at a header's name takes nothing where
#: it would end early); the second: a line-start header whose value would start on the
#: next line leaves that line's own header to it, as an inline one does.
#: Pinned as every mode writes them since #239 round 8: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`), so a run either scrubber hides
#: reads `[REDACTED]` (`[REDACTED]Cookie` and `--api-key` here).
P3_1 = {
    "refused-bearer-before-a-secret-name": ("Authorization: Bearer\nSECRET7ab1Set-Cookie : --password\nSECRET7c3",
                                            "Authorization: [REDACTED]\n[REDACTED] : [REDACTED]\n[REDACTED]"),
    "line-start-next-line": ("Cookie: --password\nSet-Cookie:\nCookie: --api-key\nSECRET7m1",
                             "Cookie: [REDACTED]\nSet-Cookie:\n[REDACTED]\n[REDACTED]"),
    # Review r5's two open cases of the lone-flag sentence, closed.
    "refused-bearer-lone-flag": ("Authorization: Bearer\nSet-Cookie: --api-key\nSECRET7c3",
                                 "Authorization: [REDACTED]\n[REDACTED] [REDACTED]\n[REDACTED]"),
    "line-start-lone-flag": ("Authorization:\nCookie: --api-key\nSECRET7c3", "Authorization:\n[REDACTED]\n[REDACTED]"),
}


@pytest.mark.parametrize("text,scrubbed", P3_1.values(), ids=P3_1.keys())
def test_review_r5_s_p3_1_inputs_are_hidden_in_every_mode(text, scrubbed):
    for mode, out in _outputs(handoff, text).items():
        assert "SECRET7" not in out, (mode, out)
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}
    assert handoff.scrub_secrets(text)[0] == handoff.scrub_secrets(text, whole=True)[0] == scrubbed


#: Review r5's `_refused_bearer` rule, applied to every refused `Bearer`'s line (the hub's
#: decision 2 as given), left a line that is no header's to its own rules too, where a word
#: no rule owns showed: `SECRET7abc13` here, in the excerpt, whole, `clean` and
#: `detail_line` modes (this round's corpus: 4 joins). 8fde0c4ad and c469f44b6 hid it; both
#: references show it. The rule applies at a header's name only, as the fold's does, so
#: the line is taken up to the flag as before, and round 3's two refused-`Bearer` pins hold.
NOT_A_HEADER_LINE = ("Authorization: Bearer\nclient_secret=x SECRET7abc13 --password",
                     "Authorization: [REDACTED]\n[REDACTED] [REDACTED] --password")   # as `P3_1`'s


def test_a_refused_bearer_s_line_that_is_no_header_s_keeps_its_redaction():
    text, scrubbed = NOT_A_HEADER_LINE
    assert all("SECRET7abc13" not in out for out in _outputs(handoff, text).values())
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}
    assert handoff.scrub_secrets(text)[0] == handoff.scrub_secrets(text, whole=True)[0] == scrubbed


#: Invariant 6's leads beyond round 4's folds: five headers a refused `Bearer` ends, and six
#: headers whose value would start on the header's line (inline, quoted, at a line start).
LONE_REFUSED_BEARERS = (*REFUSED_BEARER_LEADS, "Cookie: Bearer", "Proxy-Authorization: Bearer")
LONE_NEXT_LINE = P2_1_LEADS[-6:]


def lone_flag_leads_matrix() -> list[str]:
    """(13 folding leads + 5 refused `Bearer`s + 6 next-line headers) x 5 gaps x 6 header
    names x 3 lone flags x 5 gaps: 10,800 cells."""
    return [f"{lead}{gap}{name}: {flag}{after}SECRET7c3" for lead, gap, name, flag, after in itertools.product(
        (*P3_1_CLASS_LEADS, *LONE_REFUSED_BEARERS, *LONE_NEXT_LINE), HN_GAPS, LONE_NAMES, LONE_FLAGS, HN_GAPS)]


def test_a_header_s_lone_flag_takes_the_next_line_s_value_whatever_took_its_line():
    """Invariant 6, as C-23.14's lone-flag sentence now claims it, over all 10,800 cells: no
    mode shows any `SECRET7` value (8fde0c4ad showed `SECRET7c3` in 1,650 of them: 1,200
    where a refused `Bearer` reads the next line's name as its run, and all 450 of the
    line-start header whose value would start on the next line)."""
    cells = lone_flag_leads_matrix()
    assert len(cells) == len(set(cells)) == 10800
    failures = []
    for cell in cells:
        shown = {mode for mode, out in _outputs(handoff, cell).items() if "SECRET7" in out}
        if shown:
            failures.append((cell, sorted(shown)))
    assert failures == []


# --- P3-2: invariant 3, a name's leading `-` --------------------------------------------

#: The prefix rule keeps a name's leading `-`, so the whole text's last fold still refuses
#: the run, as the cut's does (8fde0c4ad: the whole text gave `[REDACTED]\n[REDACTED]`,
#: the cut after the first line `[REDACTED]\n` and `[REDACTED]Cookie:`).
#: The third is what every mode writes since #239 round 8 (`handoff.shown_by_both`):
#: release's output, `[REDACTED]` for the run the head writes as `-[REDACTED]Cookie:`.
P3_2 = ("--1_token\n-1_kenCookie:", "[REDACTED]\n-[REDACTED]Cookie:", "[REDACTED]\n[REDACTED]")
P3_2_LEADS = ("--1_token", "--SECRET7a1_token", "--db2-password", "x --1_token", "Cookie: --password")
P3_2_NAMES = ("-1_kenCookie:", "--1_kenCookie:", "-SECRET7ab1Authorization:", " -1_kenCookie:", "-SECRET7ab1Set-Cookie\n:")
P3_2_TAILS = ("", " v", "\nSECRET7c3", " SECRET7c3\nx")


def test_an_excerpt_cut_before_a_dash_led_name_scrubs_as_the_whole():
    text, scrubbed, written = P3_2
    cuts = _neutral_breaks(text)
    assert cuts == [len("--1_token\n")]
    for cut in cuts:
        assert handoff._scrub(text[:cut], False)[0] + handoff._scrub(text[cut:], False)[0] == scrubbed
    assert handoff._scrub(text, False)[0] == scrubbed and _splits(text) == []
    assert handoff.scrub_secrets(text)[0] == handoff.scrub_secrets(text, whole=True)[0] == written


def test_no_dash_led_name_after_a_fold_splits_or_shows_what_a_reference_hides():
    """5 leads x 5 gaps x 5 dash-led names x 4 tails: 500 cells, invariants 1 to 3."""
    cells = [lead + gap + name + tail for lead, gap, name, tail in itertools.product(
        P3_2_LEADS, HN_GAPS, P3_2_NAMES, P3_2_TAILS)]
    assert len(set(cells)) == 500
    assert _failures(cells) == CLEAN


#: Round 4's P3-4 (b) with a dash-led name: the prefix rule keeps the `-`, and the second
#: scan still reads `-[REDACTED]Cookie:` at a line start as a name that starts its line, so
#: daemon.log's line keeps nothing of the header's line, as 8fde0c4ad's did with the `-`
#: gone (`[REDACTED]Cookie: [REDACTED]`). Both references show `SECRET7c3` there.
#: Since #239 round 8 the line is release's, kept for the runs the head's holds: neither
#: holds `-[REDACTED]Cookie:` and release's line, so it reads `[REDACTED]` throughout.
DASH_LED_LINE = ("-SECRET7ab1Cookie: x\nSECRET7c3", "[REDACTED] [REDACTED] [REDACTED]")


def test_daemon_log_s_line_for_a_dash_led_secret_name_keeps_none_of_its_line():
    text, line = DASH_LED_LINE
    outputs = _outputs(handoff, text)
    assert outputs["daemon.log line"] == line
    assert all("SECRET7ab1" not in out for out in outputs.values())
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}


# --- P3-3: `_ENDS_EARLY` per scrub -------------------------------------------------------

def _held() -> tuple | None:
    """What `_ENDS_EARLY` holds for this thread: the text, line start and end, or None."""
    memo = handoff._ENDS_EARLY
    return getattr(memo, "line", None) if isinstance(memo, threading.local) else memo


#: A line of folds at header names that ends in a flag glued to a long identifier: every
#: fold there takes nothing, and the first that finds it keeps the answer (round 4).
ENDS_EARLY_TEXT = ENDING_FLAG_UNITS["quoted-inline"] * 3 + "--" + _trailer(400)


def test_no_text_outlives_quoted_value_spans():
    """`daemon_log` calls it outside `_scrub` (8fde0c4ad left its text in `_ENDS_EARLY`)."""
    text = "".join(("api_key='xyz --SECRET7a1_token\nCookie: --api-key\n", "SECRET7c3"))
    handoff.quoted_value_spans(text)
    assert _held() is None
    handoff.quoted_value_spans(ENDS_EARLY_TEXT)
    assert _held() is None


def test_no_text_outlives_a_scrub_that_raises(monkeypatch):
    """The scrub's last pass raises after its folds kept an answer: it is cleared anyway."""
    named = handoff._scrub_named_values
    kept = []

    def raising(text, replace, **kwargs):
        out = named(text, replace, **kwargs)
        if kwargs.get("flags"):
            kept.append(_held())
            raise RuntimeError("the flag pass")
        return out
    monkeypatch.setattr(handoff, "_scrub_named_values", raising)
    with pytest.raises(RuntimeError):
        handoff.scrub_secrets(ENDS_EARLY_TEXT)
    assert kept and kept[0] is not None          # the pass did keep an answer
    assert _held() is None


#: The 80,010-character shape of round 4's cost fix: 1,000 units, then `--` and a
#: 40,008-character identifier ending `password`.
THREADED_TEXT_UNITS = 1000


def _threaded_text() -> str:
    """A fresh string object each call, so two threads never share one by identity."""
    return "".join((ENDING_FLAG_UNITS["quoted-inline"] * THREADED_TEXT_UNITS, "--", _trailer(40000)))


def test_two_threads_scrubbing_keep_the_single_thread_counts(monkeypatch):
    """Two threads scrub the 80,010-character shape at once, taking turns at every fold (so
    each fold reads `_ENDS_EARLY` right after the other thread's last one): each keeps the
    `_kept_span` calls and identifier walk of a scrub on its own. A slot both threads share
    is overwritten at every turn, and the walk grows with the folds times the identifier
    (8fde0c4ad)."""
    assert len(_threaded_text()) == 80010
    kept, walk, fold = handoff._kept_span, handoff._identifier_char, handoff._fold_value
    counts: dict[tuple[str, int], int] = {}

    def counted_kept(text, start, stop):
        key = ("kept", threading.get_ident())
        counts[key] = counts.get(key, 0) + 1
        return kept(text, start, stop)

    def counted_walk(char):
        key = ("walk", threading.get_ident())
        counts[key] = counts.get(key, 0) + 1
        return walk(char)
    monkeypatch.setattr(handoff, "_kept_span", counted_kept)
    monkeypatch.setattr(handoff, "_identifier_char", counted_walk)
    handoff.scrub_secrets(_threaded_text())
    alone = {kind: counts[(kind, threading.get_ident())] for kind in ("kept", "walk")}

    turn = threading.Condition()
    state = {"turn": 0, "done": set(), "ids": {}}

    def wait_for_turn(me: int) -> None:
        assert turn.wait_for(lambda: state["turn"] == me or (1 - me) in state["done"], timeout=120)

    def taking_turns(text, end):
        me = state["ids"][threading.get_ident()]
        with turn:
            state["turn"] = 1 - me
            turn.notify_all()
            wait_for_turn(me)
        return fold(text, end)
    monkeypatch.setattr(handoff, "_fold_value", taking_turns)
    found, errors = {}, []

    def scrub(me: int) -> None:
        state["ids"][threading.get_ident()] = me
        try:
            with turn:
                wait_for_turn(me)
            handoff.scrub_secrets(_threaded_text())
            found[me] = {kind: counts.get((kind, threading.get_ident()), 0) for kind in ("kept", "walk")}
        except BaseException as exc:       # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            with turn:
                state["done"].add(me)
                turn.notify_all()
    threads = [threading.Thread(target=scrub, args=(me,)) for me in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    assert errors == [] and not any(thread.is_alive() for thread in threads)
    assert found == {0: alone, 1: alone}, (alone, found)


# --- P3-4: a quoted header value's fold onto `=` or `:` (closed in fix round 6) ----------

#: Review r5's input and its three siblings. Both references hide `SECRET7c5` in every mode:
#: their quoted pass masks `"--1_token` and the fold's `=r` as one span, which joins the
#: two lines, and the line-start header then takes the rest of the joined line. Through
#: round 5 the head's quoted pass took the flag and the fold's run as two spans, and the
#: header kept the flag's name as a key whose `=` is past the line, so the word after the
#: run showed in the excerpt, whole, `clean` and `detail_line` modes. Since fix round 6
#: (#239 review r6, P2-1) a flag whose name is such a key ends no quoted value, and the
#: union takes it with the key's value, as both references do. Pinned, mode by mode, at
#: both references' output.
P3_4 = {
    "quoted-flag-equals": ('\nAuthorization:"--1_token\n=r SECRET7c5',
                           ('\nAuthorization:[REDACTED]', 'Authorization:[REDACTED]', 'Authorization:[REDACTED]',
                            'request submit refused (ValueError): Authorization:[REDACTED]')),
    "quoted-digit-flag": ('\nAuthorization: "x --db2-password\n=r SECRET7c5',
                          ('\nAuthorization: [REDACTED]', 'Authorization: [REDACTED]', 'Authorization: [REDACTED]',
                           'request submit refused (ValueError): Authorization: [REDACTED]')),
    "single-quoted-secret-flag": ("x\nCookie: '--SECRET7a1_token\n=r SECRET7c5",
                                  ('x\nCookie: [REDACTED]', 'x\nCookie: [REDACTED]', 'x Cookie: [REDACTED]',
                                   'request submit refused (ValueError): x Cookie: [REDACTED]')),
    "quoted-flag-colon": ('\nAuthorization:"--1_token\n:r SECRET7c5',
                          ('\nAuthorization:[REDACTED]', 'Authorization:[REDACTED]', 'Authorization:[REDACTED]',
                           'request submit refused (ValueError): Authorization:[REDACTED]')),
}


@pytest.mark.parametrize("text,pinned", P3_4.values(), ids=P3_4.keys())
def test_a_quoted_header_s_flag_folding_onto_an_assignment_hides_the_next_word_as_both_references_do(text, pinned):
    """Pinned in all five modes, each both references' output; no excerpt cut or second
    scrub differs."""
    excerpt, clean, line, detail = pinned
    outputs = _outputs(handoff, text)
    assert outputs == {"excerpt": excerpt, "whole": excerpt, "clean": clean, "daemon.log line": line,
                       "detail_line": detail}
    assert _outputs(base473, text) == outputs
    assert _splits(text) == [] and _unstable(text) == {}
