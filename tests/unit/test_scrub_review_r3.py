"""C-23.14 (D-SCR2 fix round 3, #239 review r3): no mode of the head's scrubber shows a
`SECRET7` value that 473fe2941 or release/217 hides, the stated converse aside.

Release/217 is `63c64d6b3` here. Its scrubber is `c2e080e44`'s (#244 changes nothing under
`subfleet/sessions/`), and `c2e080e44`'s gives what 473fe2941's gives in every mode on
every cell below (measured this round against both real trees), so `scrub_473fe2941`
stands for both, as in `test_scrub_review_r2`. The pre-#231 scrubber (`scrub_release217`,
3fbc4b7c5) is checked too.

Review r3's executed inputs are regressions, each failing at 902cfe90a: five where a span
`_kept_span` bounds took a secret-named flag's name and left its value (P2-A), three where
a fold that started at a header's name ate the name and left the rest of its line (P2-B),
a header name holding a secret glued to a closing quote (P3-B), and a quoted inline
header ending at the quote that opens a flag's quoted value (P3-C). The narrow P2-B fix
the review measured (the fold returning None at a header's name) showed a secret held in a
header's name; P3-B now takes that secret from the name, so the line fold and P3-B each
hide that input, which is pinned as a guard on both. The two over-redactions the
fail-closed ruling accepts are pinned too.

The review's two generators were not kept; they are rebuilt here from what it states of
them, at its sizes: the rules matrix (16 leads x 4 secret-named flags x 8 gaps x 20
following runs x 6 tails, 61,440 cells) and the header-name matrix (11 folding leads x 5
gaps x 8 header lines x 4 tails, 1,760 cells). Their counts are their own. The header-name
matrix runs whole; the rules matrix runs as a seeded slice of 6,144 cells, and whole only
with `SF_SCRUB_FULL_MATRIX=1` (the repository has no `slow` marker; this is the opt-in
`test_host_push_build`'s benchmark uses). Over each cell: no new leak against either
reference, an excerpt cut where `_neutral_break` allows scrubs as the whole text does
there, and a second scrub shows nothing the first hid."""

from __future__ import annotations

import itertools
import os
import random
from collections.abc import Callable

import pytest
from hypothesis import given, strategies as st

from subfleet.sessions import handoff
from tests.unit.test_scrub_key_order import CI
from tests.unit.test_scrub_review_r2 import (_assert_hidden, _new_leaks, _outputs, _secrets, _shown, _splits,
                                             _unstable)

# --- review r3's executed inputs -------------------------------------------------------

#: P2-A: a span `_kept_span` bounds (a fold, a refused `Bearer`'s line) that ends in a
#: secret-named flag took the flag's name but not the value daemon.log's fold gives it,
#: and the flag rule, whose cursor was past the flag, never read it.
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
P2_A = {
    "fold-of-a-bare-flag": ("--SECRET7a1_token\nKEY=--db3-password SECRET7c3",
                            '[REDACTED]\n[REDACTED] [REDACTED]'),
    "digit-named-flags": ("--db2-password\nx--db3-password SECRET7c3", '[REDACTED]\n[REDACTED] [REDACTED]'),
    "header-s-lone-flag": ("Authorization: --db2-password\nx--db3-password SECRET7c3",
                           'Authorization: [REDACTED]\n[REDACTED] [REDACTED]'),
    "refused-bearer-line": ("x cookie: Bearer\nclient_secret=x --SECRET7a1_token\nSECRET7b2",
                            "x cookie: [REDACTED]\n[REDACTED] [REDACTED]\n[REDACTED]"),
    "refused-bearer-detail-line": ("Cookie:Bearer\n--password\t--2-apikey\nSECRET7821",
                                   'Cookie:[REDACTED]\n[REDACTED]'),
}
#: P2-B: a fold whose run starts at a header's name, its value glued to the colon,
#: took the name; nothing then read the rest of that line as a header.
P2_B = {
    "header-s-lone-flag": ("Cookie: --password\nCookie:x SECRET7c3", "Cookie: [REDACTED]\n[REDACTED]"),
    "bearer-s-secret-flag": ("Bearer --SECRET7a1_token\nAuthorization:Basic SECRET7c3", "Bearer [REDACTED]\n[REDACTED]"),
    "line-start-reach": ("x\nAuthorization: --api-key\nCookie:x SECRET7c3", "x\nAuthorization: [REDACTED]\n[REDACTED]"),
}
#: P3-B: a header name whose part before its header word is a secret, glued to a
#: closing quote; the inline rule kept the name.
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
P3_B = ('secret:"x"SECRET7abcAuthorization: B', '[REDACTED]')
#: P3-C: a quoted inline header whose value ends at the quote that opens a flag's
#: quoted value; the header took the flag and left its value.
P3_C = ("curl -H 'Cookie: a=1 --password 'SECRET7c3'", "curl -H 'Cookie: [REDACTED] --password [REDACTED]")
#: The leak the review found with the narrow P2-B fix (the fold returning None at a
#: header's name) before P3-B: the name holds a secret, which daemon.log's line and
#: `detail_line` show (review r3: 3 of 4,500 joined texts). The line fold takes the whole
#: line, and P3-B takes the secret from the name, so either hides it; with both dropped it
#: shows. It is hidden at 902cfe90a too, whose fold took the name's run. The pin is the
#: output P3-B gives, the name's secret gone and its header word kept.
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
NARROW = ("deploy --SECRET7abc1_token\nSECRET7nxt9Authorization: Basic x", 'deploy [REDACTED]\n[REDACTED] [REDACTED] [REDACTED]')
#: Fail closed (the hub's ruling, round 2, decision 2): a flag whose name holds a digit
#: reads as secret-named, so it goes with the value the fold gives it. Stated
#: over-redaction (C-23.14), pinned.
OVER_REDACTED = {
    "deploy --k8s-token\necho done": "deploy [REDACTED]\n[REDACTED] done",
    "kubectl --oauth2-token abc --verbose": "kubectl [REDACTED] [REDACTED] --verbose",
}

REGRESSIONS = {**{f"P2-A-{name}": pair for name, pair in P2_A.items()},
               **{f"P2-B-{name}": pair for name, pair in P2_B.items()},
               "P3-B": P3_B, "P3-C": P3_C}


@pytest.mark.parametrize("text,scrubbed", REGRESSIONS.values(), ids=REGRESSIONS.keys())
def test_review_r3_s_inputs_are_hidden_in_every_mode(text, scrubbed):
    """Every executed input of review r3 that showed a value 473fe2941 and c2e080e44 hide
    (each failed at 902cfe90a): no mode shows it, no excerpt cut disagrees with the whole
    text, a second scrub reveals nothing, and the excerpt and whole scrubs are pinned."""
    _assert_hidden(text)
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}
    assert handoff.scrub_secrets(text)[0] == scrubbed
    assert handoff.scrub_secrets(text, whole=True)[0] == scrubbed


def test_the_narrow_p2_b_fix_s_leak_stays_hidden():
    """A fold that returned None at a header's name, with no P3-B, left `SECRET7nxt9` in
    the name for the line forms to show. P3-B takes it from the name first, and the fold
    would take the line: either hides it."""
    text, scrubbed = NARROW
    _assert_hidden(text)
    assert _new_leaks(text) == {} and _splits(text) == []
    assert handoff.scrub_secrets(text)[0] == scrubbed


@pytest.mark.parametrize("text,scrubbed", OVER_REDACTED.items())
def test_a_digit_in_a_flag_s_name_over_redacts_as_stated(text, scrubbed):
    """Decision 4: fail closed stays the ruling, and its cost is pinned, not changed."""
    for whole in (False, True):
        assert handoff.scrub_secrets(text, whole=whole)[0] == scrubbed


# --- the rules matrix ------------------------------------------------------------------

#: Where a secret-named flag sits: bare, after a command, `Bearer`, headers (line start,
#: inline, quoted, a value on the next line), plain, quoted and unclosed values, glued
#: to a flag's value, a refused `Bearer`'s next line, a swallowed header name, and a
#: quoted inline header holding a plain pair.
RULES_LEADS = ("", "deploy ", "Bearer ", "Authorization: ", "sent Cookie: x ", "token:", "password=", "api_key='xyz ",
               "--password y", "x cookie: Bearer\nclient_secret=x ", "Cookie:Bearer\n--password\t",
               "curl -H 'Authorization: ", "secret:'Cookie: x ", "X-Authorization:\n  ", "x\nAuthorization: ",
               "curl -H 'Cookie: a=1 ")
#: Secret-named flags: a secret in the name, and names that read as secrets only by a digit.
RULES_FLAGS = ("--SECRET7a1_token", "--db2-password", "--2-apikey", "x--SECRET7a1_token")
#: Between the flag and the run its fold takes: spaces, tabs, line breaks, blank lines,
#: trailing spaces, CRLF and indents.
RULES_GAPS = (" ", "\t", "\n", "\n\n", "  \n", "\r\n", "\n  ", " \n\n ")
#: The run the fold takes, and what it ends in: a secret, a run ending in a credential
#: flag (secret-named or not, glued or not), keys, `Bearer`, headers with values glued
#: to the colon or not, a quoted value, a flag's value on the next line, an unclosed
#: quote, a header name holding a secret, and a pair before a flag's quoted value.
RULES_RUNS = ("SECRET7b2", "KEY=--db3-password SECRET7c3", "x--db3-password SECRET7c3", "--password SECRET7c3",
              "x--password SECRET7c3", "x--SECRET7d4_token SECRET7c3", "client_secret=SECRET7c3", "token: SECRET7c3",
              "Bearer SECRET7c3abcd", "Authorization:Basic SECRET7c3", "Cookie:x SECRET7c3", "Authorization: SECRET7c3",
              "'SECRET7c3'", "x--api-key\nSECRET7c3", "x--2-apikey\nSECRET7c3", "api_key='SECRET7c3",
              "Bearer\nSECRET7c3abcd", "x\tBearer SECRET7c3abcd", "SECRET7e5Cookie: x", "a=1 --password 'SECRET7c3'")
RULES_TAILS = ("", " tail", "\nSECRET7t5", " --verbose", "'", "\r\nnext")


def rules_matrix() -> list[str]:
    return [lead + flag + gap + run + tail for lead, flag, gap, run, tail in
            itertools.product(RULES_LEADS, RULES_FLAGS, RULES_GAPS, RULES_RUNS, RULES_TAILS)]


#: The unit tier's slice of the rules matrix: a tenth of it, seeded.
RULES_SLICE = 6144
RULES_SEED = 2393

# --- the header-name matrix ------------------------------------------------------------

#: What folds onto the next line: a header whose value is a lone flag (line start,
#: inline, quoted, after another line), `Bearer` and plain values ending in a
#: secret-named flag, bare and glued secret-named flags, an inline header ending in one,
#: and an unclosed value ending in one.
HN_LEADS = ("Cookie: --password", "Bearer --SECRET7a1_token", "x\nAuthorization: --api-key", "--SECRET7a1_token",
            "deploy --db2-password", "sent Cookie: x --SECRET7a1_token", "token:--SECRET7a1_token",
            "api_key='xyz --SECRET7a1_token", "--password x--SECRET7a1_token", "Authorization: x --SECRET7a1_token",
            "curl -H 'Cookie: --2-apikey")
HN_GAPS = ("\n", "\n\n", "  \n", "\r\n", "\n  ")
#: The next line's header: values glued to the colon (with the secret past a space or
#: tab), a secret glued itself, and a value past a space.
HN_LINES = ("Cookie:x SECRET7c3", "Authorization:Basic SECRET7c3", "Set-Cookie:a=1 SECRET7c3", "cookie:x\tSECRET7c3",
            "Proxy-Authorization:Basic SECRET7c3", "Authorization:Bearer SECRET7c3abcd", "Cookie:SECRET7c3 x",
            "cookie: x SECRET7c3")
HN_TAILS = ("", " tail", "\nSECRET7t5", "'")


def header_name_matrix() -> list[str]:
    return [lead + gap + line + tail for lead, gap, line, tail in itertools.product(HN_LEADS, HN_GAPS, HN_LINES, HN_TAILS)]


MATRICES = {"rules": (rules_matrix, 61440), "header-name": (header_name_matrix, 1760)}


@pytest.mark.parametrize("name", MATRICES)
def test_the_rebuilt_matrices_have_the_review_s_sizes(name):
    """And each holds the review's inputs of its class."""
    build, size = MATRICES[name]
    cells = build()
    assert len(cells) == size == len(set(cells))
    assert all(_secrets(cell) for cell in cells)
    held = {"rules": [P2_A["fold-of-a-bare-flag"][0], P2_A["digit-named-flags"][0]],
            "header-name": [P2_B["header-s-lone-flag"][0], P2_B["line-start-reach"][0]]}[name]
    assert set(held) <= set(cells)


def _failures(cells) -> dict[str, list]:
    """Invariants 1 to 3 over `cells`: new leaks against either reference, excerpt cuts
    that disagree with the whole text, and second scrubs that show what the first hid."""
    failures = {"leaks": [], "splits": [], "unstable": []}
    for cell in cells:
        if (leaks := _new_leaks(cell)):
            failures["leaks"].append((cell, leaks))
        if (splits := _splits(cell)):
            failures["splits"].append((cell, splits))
        if (unstable := _unstable(cell)):
            failures["unstable"].append((cell, unstable))
    return failures


CLEAN = {"leaks": [], "splits": [], "unstable": []}


def test_no_cell_of_the_header_name_matrix_shows_what_a_reference_hides():
    """All 1,760 cells (902cfe90a: 560 show a value both references hide)."""
    assert _failures(header_name_matrix()) == CLEAN


def test_no_cell_of_the_rules_matrix_slice_shows_what_a_reference_hides():
    """6,144 of the 61,440 cells, seed 2393 (902cfe90a: about one in six show a value
    both references hide)."""
    cells = random.Random(RULES_SEED).sample(rules_matrix(), RULES_SLICE)
    assert _failures(cells) == CLEAN


@pytest.mark.skipif(os.environ.get("SF_SCRUB_FULL_MATRIX") != "1", reason="the whole 61,440-cell matrix, on request")
def test_no_cell_of_the_whole_rules_matrix_shows_what_a_reference_hides():
    assert _failures(rules_matrix()) == CLEAN


# --- P3-B and P3-C: their shapes ---------------------------------------------------------

#: P3-B's class: a header name whose part before its header word reads as a secret, after
#: a closing quote, a space, a line start or a quoted inline opening, with every kind of
#: value the inline rule meets: plain, empty, at the text's end, on the next line, the
#: next line's own header, already redacted, a key, a flag, `Bearer`.
P3_B_LEADS = ('secret:"x"', "secret:'x'", "x ", "x\n", "curl -H '", "token=")
P3_B_NAMES = ("SECRET7abcAuthorization", "xSECRET7ab1-Cookie", "SECRET7ab1_cookie", "SECRET7ab1Set-Cookie")
P3_B_VALUES = (": B", ":", ": ", ":\nB", ":\nCookie: v", ": [REDACTED]", ": token=\nv", ": --password\nv",
               ": Bearer\nSECRET7bv1abcd", ":x y")
P3_B_TAILS = ("", "'", " tail")


def p3_b_matrix() -> list[str]:
    return [lead + name + value + tail for lead, name, value, tail in
            itertools.product(P3_B_LEADS, P3_B_NAMES, P3_B_VALUES, P3_B_TAILS)]


def test_no_header_name_holding_a_secret_shows_in_any_mode():
    """P3-B's 720 cells: every mode hides the name's secret (902cfe90a showed it wherever
    the name was not at a line start), and none shows what a reference hides."""
    failures = []
    for cell in p3_b_matrix():
        shown = _shown(_outputs(handoff, cell), {"SECRET7abc", "SECRET7ab1"})
        if shown or _new_leaks(cell) or _splits(cell) or _unstable(cell):
            failures.append((cell, sorted(shown)))
    assert failures == []


#: P3-C's class: a quoted inline header (single, double, `--header=` and JSON-escaped
#: quotes) whose value ends at the quote that opens a flag's quoted value, the flag
#: parted from the quote by whitespace; and each one's bare twin, the same flag and
#: quoted value with no header around them (invariant 4).
P3_C_OPENERS = (("curl -H '", "'"), ('curl -H "', '"'), ("wget --header='", "'"), ('{"c": "curl -H \\"', '\\"'))
P3_C_HEADERS = ("Cookie: a=1 ", "Authorization: Basic x ", "Cookie: ", "cookie: a ")
P3_C_FLAGS = ("--password", "--api-key", "x--token", "--SECRET7f1_token", "--db2-password")
P3_C_GAPS = (" ", "  ", "\t")
P3_C_TAILS = ("", " -v", "\nnext")


def p3_c_matrix() -> list[tuple[str, str]]:
    return [(f"{opening}{header}{flag}{gap}{closing}SECRET7qv{n}{closing}{tail}",
             f"{flag}{gap}{closing}SECRET7qv{n}{closing}{tail}")
            for n, ((opening, closing), header, flag, gap, tail) in enumerate(itertools.product(
                P3_C_OPENERS, P3_C_HEADERS, P3_C_FLAGS, P3_C_GAPS, P3_C_TAILS))]


def test_a_flag_s_quoted_value_after_a_quoted_header_is_hidden_as_its_twin_s():
    """P3-C's 720 cells: no mode shows the flag's quoted value where its bare twin's same
    mode hides it, and none shows what a reference hides."""
    failures = []
    for enclosed, twin in p3_c_matrix():
        secrets = _secrets(enclosed)
        shown = _shown(_outputs(handoff, enclosed), secrets) - _shown(_outputs(handoff, twin), secrets)
        if shown or _new_leaks(enclosed) or _splits(enclosed) or _unstable(enclosed):
            failures.append((enclosed, sorted(shown)))
    assert failures == []


# --- a property over the matrices' pieces -----------------------------------------------

_JOINS = (" ", "\n", "\n  ", "", "\r\n", "\t", "  \n", "\n\n")


@st.composite
def _drawn(draw):
    """One to three pieces of the two matrices and the P3 shapes, joined."""
    pieces = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        kind = draw(st.sampled_from(("rules", "header-name", "p3-b", "p3-c")))
        if kind == "rules":
            pieces.append("".join(draw(st.sampled_from(part)) for part in (
                RULES_LEADS, RULES_FLAGS, RULES_GAPS, RULES_RUNS, RULES_TAILS)))
        elif kind == "header-name":
            pieces.append("".join(draw(st.sampled_from(part)) for part in (HN_LEADS, HN_GAPS, HN_LINES, HN_TAILS)))
        elif kind == "p3-b":
            pieces.append("".join(draw(st.sampled_from(part)) for part in (
                P3_B_LEADS, P3_B_NAMES, P3_B_VALUES, P3_B_TAILS)))
        else:
            (opening, closing), header, flag, gap = (draw(st.sampled_from(part)) for part in (
                P3_C_OPENERS, P3_C_HEADERS, P3_C_FLAGS, P3_C_GAPS))
            pieces.append(f"{opening}{header}{flag}{gap}{closing}SECRET7qv9{closing}")
    return pieces[0] + "".join(draw(st.sampled_from(_JOINS)) + piece for piece in pieces[1:])


@CI
@given(text=_drawn())
def test_no_drawn_join_of_the_pieces_shows_what_a_reference_hides(text):
    """Invariants 1 to 3 over joined pieces (the suite's ci profile: 200 examples,
    derandomized)."""
    assert _failures([text]) == CLEAN, text


# --- invariant 5: what the new folds read ------------------------------------------------

#: Lines whose folds take this round's paths, each before an identifier ending in a
#: credential word that `_kept_span` and `_key_past_line` would walk back: folds that
#: start at a header's name on one line, from quoted keys, bare flags, quoted inline
#: headers and `Bearer` (P2-B); headers whose lone flag folds onto a header line (P2-B);
#: folds that end in a flag (P2-A); header names holding a secret (P3-B); and flags
#: before a closing quote (P3-C). And several flag-rule folds on one line, each at a
#: quoted header's name (#239 review r4, P3-3): the inline header rule takes an
#: unquoted header's rest of line first, so only a quoted one leaves the flag rule a
#: fold per flag there, which a flag rule whose cursor stopped at the flag (MQ2) would
#: compute again for every later flag on the line.
COUNTED = {"quoted-keys-at-header-names": "api_key='x token'=--SECRET7a1_token Cookie:x ",
           "flags-at-header-names": "--SECRET7a1_token Cookie:x ",
           "quoted-inline-at-header-names": "-H 'Cookie: --SECRET7a1_token 'Cookie:x ",
           "bearer-at-header-names": "Bearer --SECRET7a1_token Cookie:x ",
           "lone-flag-header-lines": "Cookie: --password\nCookie:x v ",
           "folds-ending-in-a-flag": "--SECRET7a1_token\nKEY=--db3-password v\n",
           "secret-header-names": "x SECRET7ab1Authorization: v ",
           "flags-before-a-closing-quote": "-H 'Cookie: a=1 --password 'v' ",
           "flag-folds-sharing-a-line": "--SECRET7a1_token 'Cookie:x' "}


def _trailer(size: int) -> str:
    """An identifier of about `size` characters ending in a credential word, which a fold
    whose span ends at it walks back whole (`_key_past_line`). No token rule takes it:
    its runs of `a` are 99 long, under `_LONG_BASE64_RE`'s 160, so it reaches the header,
    plain and flag passes as written (review r4, P3-3: `"a" * size` was one base64 run,
    which `[BASE64 OMITTED]` replaced before any fold could walk it)."""
    return ("a" * 99 + "_") * (size // 100) + "password"


def _counts(unit: str, monkeypatch, trailer: Callable[[int], str] | None = None) -> dict[str, list[dict[str, int]]]:
    """For each mode (the excerpt, whole-text and `detail_line` scrubs), the `_kept_span`
    calls and the Python-level identifier walk (`_identifier_char` calls) over `unit`
    repeated 50 times before a 2,008-character trailer, and 200 times before an
    8,008-character one: 4x input. `trailer`, if given, makes the trailer of a size.

    `detail_line`'s is the scrub of its prefixed text (`suggestions._scrub_line`), all of
    its work that grows with the input. Its line bound's check (`suggestions.bounded`)
    scrubs at most the 2,000 characters the line keeps, once the line is longer: a step,
    not growth (434 walk calls on the lone-flag header lines, which round 4's P3-1 fix
    first puts past the bound between 100 and 150 units, whatever the length after)."""
    from subfleet.conversations import suggestions
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
    found = {}
    for name, mode in (("excerpt", handoff.scrub_secrets), ("whole", lambda text: handoff.scrub_secrets(text, whole=True)),
                       ("detail_line", lambda text: suggestions._scrub_line("request submit refused (ValueError): " + text))):
        found[name] = []
        for repeats, tail in ((50, 2000), (200, 8000)):
            counts.update(kept=0, walk=0)
            mode(unit * repeats + (trailer or _trailer)(tail))
            found[name].append(dict(counts))
    return found


def _assert_linear(found: dict[str, list[dict[str, int]]]) -> None:
    for small, large in found.values():
        assert large["kept"] <= 4 * max(small["kept"], 1), found
        assert large["walk"] <= 4.05 * small["walk"], found


@pytest.mark.parametrize("unit", COUNTED.values(), ids=COUNTED.keys())
def test_the_new_folds_read_each_line_once(unit, monkeypatch):
    """Invariant 5's counts behind the cost (run b): at 4x input, `_kept_span` runs at
    most 4x as often, once per fold where folds have lines of their own and once per line
    where they share one (the quoted pass's union, and the flag and header rules'
    cursors, stop later folds there), and the Python-level walk over identifiers grows
    at most 4x, in the excerpt, whole-text and `detail_line` modes. Each trailer is walked
    whole, so a fold computed again for each later flag on a line grows the walk about
    16x (MQ2, `test_scrub_review_r4`)."""
    _assert_linear(_counts(unit, monkeypatch))
