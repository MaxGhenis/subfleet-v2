"""C-23.14 (D-SCR2 fix round 4, #239 review r4): no mode of the head's scrubber shows a
`SECRET7` value that 473fe2941 or release/217 (`c2e080e44`'s scrubber) hides, the stated
converse aside, and the over-redaction that failing closed costs is the kind C-23.14 names.

`scrub_473fe2941` stands for both references, as in `test_scrub_review_r2` and `_r3`, and
the pre-#231 scrubber (`scrub_release217`, 3fbc4b7c5) is checked too.

Review r4's executed inputs are regressions, each failing at c469f44b6:
- P3-1: a fold that took the next line's header through `_kept_span` (round 3's P2-B
  line fold), where the span ended before a credential flag at the end of that line,
  moved the header rules past the line, so a header whose whole value was that lone flag
  never took the value on the line after (six leads, one header line).
- P3-4 (a): a line-start header's name parted from its colon by whitespace other than
  spaces and tabs (a line break, `\\x0b`, `\\x0c`, `\\xa0`, `\\r`), which only the
  line-start rule reads, kept the secret part before its header word.
- P3-4 (b): daemon.log's line for a text that starts with such a name, once P3-B left
  it `[REDACTED]Cookie:`, kept the words the fold put after its redacted value.

One class found this round beyond review r4, which shows a value both references hide at
c469f44b6 and 902cfe90a, is a regression too: a fold, or a refused `Bearer`'s line, whose
run starts a line-start header whose name its colon parts by whitespace other than spaces
and tabs, which only the line-start rule reads, took the start of that name (or all of
it) and left the header's value to the plain rule.

P3-3's mutant MQ2 is committed here as a mutant the count test kills. P3-2's kinds of
over-redaction are pinned at their exact output. Invariant 6 (a header whose whole value
is a lone credential flag takes the next line's value, whatever fold took its line) is a
property over review r4's six leads, the header names and the gaps, and P3-1's class is a
generator with no new leak against either reference."""

from __future__ import annotations

import inspect
import itertools
import textwrap
import types

import pytest

from subfleet.sessions import handoff
from tests.unit.test_scrub_review_r2 import _assert_hidden, _new_leaks, _outputs, _secrets, _splits, _unstable
from tests.unit.test_scrub_review_r3 import CLEAN, HN_GAPS, HN_LEADS, _assert_linear, _counts, _failures, _trailer

# --- review r4's executed inputs -------------------------------------------------------

#: P3-1: review r4's six leads, each a fold onto the next line, which is a header whose
#: whole value is a lone credential flag, with a value on the line after that.
P3_1_LEADS = ("Cookie: --password", "x Cookie: --password", "Bearer --SECRET7a1_token",
              "api_key='xyz --SECRET7a1_token", "secret:'Cookie: x --SECRET7a1_token",
              "Authorization: x --SECRET7a1_token")
P3_1 = {lead: (lead + "\nCookie: --api-key\nSECRET7c3", scrubbed + "\nCookie: [REDACTED]\n[REDACTED]")
        for lead, scrubbed in zip(P3_1_LEADS, ("Cookie: [REDACTED]", "x Cookie: [REDACTED]", "Bearer [REDACTED]",
                                               "api_key=[REDACTED]", "secret:[REDACTED]",
                                               "Authorization: [REDACTED]"))}
#: P3-4 (a): a name parted from its colon by a line break, `\x0b`, `\x0c`, `\xa0` or `\r`.
#: Since #239 round 8 every mode writes release/217's output kept for the runs the head's
#: holds (`handoff.shown_by_both`): release's `[REDACTED]` for the name, which the head writes
#: as `[REDACTED]Cookie`.
P3_4_A = {repr(gap): (f"x\nSECRET7ab1Cookie{gap}: v", f"x\n[REDACTED]{gap}: [REDACTED]")
          for gap in ("\n", "\x0b", "\x0c", "\xa0", "\r")}
#: P3-4 (b): daemon.log's line, as 902cfe90a gave it with the name whole, now with the
#: name's secret part gone too; the other modes keep the next line, as both references do.
#: Since #239 round 8, release's line and scrub kept for the runs the head's hold: no run of
#: the header's line survives, the name's `Cookie` included.
P3_4_B = ("SECRET7ab1Cookie: x\nSECRET7c3", "[REDACTED] [REDACTED] [REDACTED]", "[REDACTED] [REDACTED]\nSECRET7c3")

REGRESSIONS = {**{f"P3-1-{lead}": pair for lead, pair in P3_1.items()},
               **{f"P3-4-a-{gap}": pair for gap, pair in P3_4_A.items()}}


@pytest.mark.parametrize("text,scrubbed", REGRESSIONS.values(), ids=REGRESSIONS.keys())
def test_review_r4_s_inputs_are_hidden_in_every_mode(text, scrubbed):
    """Every executed P3-1 and P3-4 (a) input of review r4 (each showed a value at
    c469f44b6 that 902cfe90a hid): no mode shows any `SECRET7` value, none shows what a
    reference hides, no excerpt cut disagrees with the whole text, a second scrub reveals
    nothing, and the excerpt and whole scrubs are pinned."""
    for mode, out in _assert_hidden(text).items():
        assert "SECRET7" not in out, (mode, out)
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}
    assert handoff.scrub_secrets(text)[0] == scrubbed
    assert handoff.scrub_secrets(text, whole=True)[0] == scrubbed


def test_daemon_log_s_line_for_a_header_name_holding_a_secret_keeps_none_of_its_line():
    """P3-4 (b): `[REDACTED]Cookie:` at the folded line's start is read as a line-start
    header's name, so its value goes, already redacted or not, with the words the fold put
    after it; no mode shows the name's secret part."""
    text, line, scrubbed = P3_4_B
    outputs = _outputs(handoff, text)
    assert outputs["daemon.log line"] == line
    assert all("SECRET7ab1" not in out for out in outputs.values()), outputs
    assert handoff.scrub_secrets(text)[0] == handoff.scrub_secrets(text, whole=True)[0] == scrubbed
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}


# --- P3-2: the over-redaction failing closed costs, pinned ------------------------------

#: Each kind review r4 found beyond the two round 3 pinned (`test_scrub_review_r3`'s
#: `OVER_REDACTED`): stated in C-23.14 as the cost of failing closed, not narrowed.
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
OVER_REDACTED = {
    # A fold that starts at a header's name takes the name with its line (round 3's line
    # fold, a follow-up since; daemon.log's line is the line-start header's whole).
    "Cookie: --password\nCookie:x ok": "Cookie: [REDACTED]\n[REDACTED]",
    # The line fold at a quoted header's name takes the rest of the line past the name's
    # closing quote: a digit-named flag's fold, and a secret-named flag's, which takes the
    # name of the `--password` flag after the quote with it.
    'curl --oauth2-token "Authorization: Bearer abc" -X POST https://api.example.com': 'curl [REDACTED] [REDACTED] [REDACTED] [REDACTED] [REDACTED] [REDACTED] [REDACTED]',
    'deploy --SECRET7a1_token "Cookie:x" --password SECRET7p9': 'deploy [REDACTED] [REDACTED] [REDACTED] [REDACTED]',
    # P3-B's prefix rule takes a name prefix that holds a digit, secret or not.
    "X-Auth0-Authorization: Basic x": '[REDACTED] [REDACTED] [REDACTED]',
    "curl -H 'X-Amz1-Cookie: a=b' https://example.com": "curl -H [REDACTED] [REDACTED]' https://example.com",
    "session_v2_cookie: abc123": '[REDACTED] [REDACTED]',
    # Round 3's decision 4 (a digit-named flag is secret-named) chained through P2-A's
    # stop: the second flag's fold takes the third line's first word.
    "--SECRET7a1_token\nKEY=--db3-password\necho done": '[REDACTED]\n[REDACTED]\n[REDACTED] done',
}


#: daemon.log's line where it is not the excerpt's folded: the folded line starts with the
#: header, whose value is the rest of it.
LINES = {"Cookie: --password\nCookie:x ok": "Cookie: [REDACTED]"}


@pytest.mark.parametrize("text,scrubbed", OVER_REDACTED.items())
def test_failing_closed_over_redacts_as_stated(text, scrubbed):
    """The stated cost (C-23.14), pinned in the excerpt and whole-text scrubs and, folded,
    in daemon.log's line and `detail_line`."""
    for whole in (False, True):
        assert handoff.scrub_secrets(text, whole=whole)[0] == scrubbed
    outputs = _outputs(handoff, text)
    folded = " ".join(scrubbed.split())
    assert outputs["daemon.log line"] == LINES.get(text, folded)
    assert outputs["detail_line"].endswith("): " + folded)
    assert _new_leaks(text) == {}


# --- invariant 6: a lone flag survives a fold ------------------------------------------

#: The header line, its name, and its lone credential flag (none secret-named).
LONE_NAMES = ("Cookie", "Authorization", "Set-Cookie", "Proxy-Authorization", "X-Authorization", "cookie")
LONE_FLAGS = ("--api-key", "--password", "--client-secret")


def lone_flag_matrix() -> list[str]:
    """Review r4's six leads x 5 gaps x 6 header names x 3 lone flags x 5 gaps: 2,700."""
    return [f"{lead}{gap}{name}: {flag}{after}SECRET7c3" for lead, gap, name, flag, after in
            itertools.product(P3_1_LEADS, HN_GAPS, LONE_NAMES, LONE_FLAGS, HN_GAPS)]


def test_a_header_s_lone_flag_takes_the_next_line_s_value_whatever_fold_took_its_line():
    """Invariant 6, over all 2,700 cells: no mode shows any `SECRET7` value (c469f44b6
    showed `SECRET7c3` in the excerpt, whole and `clean` modes wherever the lead's fold
    took the header's line)."""
    cells = lone_flag_matrix()
    assert len(cells) == len(set(cells)) == 2700
    failures = []
    for cell in cells:
        shown = {mode for mode, out in _outputs(handoff, cell).items() if "SECRET7" in out}
        if shown:
            failures.append((cell, sorted(shown)))
    assert failures == []


# --- P3-1's class: a generator ---------------------------------------------------------

#: Header lines that end in an item owning a value past the line (a lone flag, a flag after
#: a value, glued or tab-parted, a secret-named flag, a key whose value is on the next
#: line, `Bearer`), so `_kept_span` would end a fold of the line before it.
P3_1_LINES = ("Cookie: --api-key", "Cookie:x --password", "Authorization: Basic x --token", "Set-Cookie:a=1\t--api-key",
              "Cookie: x --SECRET7d4_token", "Cookie: client_secret=", "Authorization: Bearer", "cookie:x--password")
P3_1_AFTER = ("\n", "\n\n", "\r\n")
P3_1_VALUES = ("SECRET7c3 tail", "'SECRET7c3'")
#: Round 3's 11 folding leads (`HN_LEADS`) and the two of review r4's six they lack.
P3_1_CLASS_LEADS = tuple(dict.fromkeys((*HN_LEADS, *P3_1_LEADS)))


def p3_1_matrix() -> list[str]:
    """13 folding leads x 5 gaps x 8 header lines x 3 gaps x 2 values: 3,120 cells."""
    return [lead + gap + line + after + value for lead, gap, line, after, value in
            itertools.product(P3_1_CLASS_LEADS, HN_GAPS, P3_1_LINES, P3_1_AFTER, P3_1_VALUES)]


def test_no_cell_of_p3_1_s_class_shows_what_a_reference_hides():
    """Invariants 1 to 3 over P3-1's class (round 3's header-name leads before header
    lines that end in an owning item): no new leak against either reference, no excerpt
    cut that disagrees with the whole text, no second scrub that reveals anything."""
    cells = p3_1_matrix()
    assert len(cells) == len(set(cells)) == 3120 and all(_secrets(cell) for cell in cells)
    assert _failures(cells) == CLEAN


# --- P3-4's edge in the folds: a line-start header that only `_HEADER_RE` reads -------
# (a fold, or the line a header's refused `Bearer` takes)

#: A fold whose run starts a line-start header whose name its colon parts by whitespace
#: other than spaces and tabs (`Set-Cookie` and `: v` on the next line): `_header_name_at`
#: reads no header there (it allows only `[ \t]*:`), so the run went to `_kept_span`, which
#: ended it before the header word, a key whose value is past the line. The fold then
#: redacted the name's start (`[REDACTED]Cookie`), so `_HEADER_RE`, whether it had yet to
#: run or was the rule that folded, never read that header, and the plain rule took only
#: the value's first word. Found this round beyond review r4 (it shows at c469f44b6 and
#: 902cfe90a; 473fe2941 and c2e080e44 hide it), with the fourth input, which P3-1's fix
#: routed into it. The line a header's refused `Bearer` takes (`_refused_bearer`) ended
#: the same way, a value the plain rule does not read (`;v …`) lost the whole name, and
#: the `Bearer` rule took the part of the name before its credential word when that read
#: as a token (`SECRET7…Set-Cookie`).
#: Since #239 round 8, pinned as every mode writes them: release/217's output, kept for the
#: runs the head's output holds (`handoff.shown_by_both`).
FOLD_AT_LINE_START = {
    "lone-flag": ("Cookie: --password\nSet-Cookie\n: v SECRET7v2", "Cookie: [REDACTED]\nSet-Cookie\n: [REDACTED]"),
    "secret-flag": ("Cookie: x --SECRET7a1_token\nSet-Cookie \n: v SECRET7v2", "Cookie: [REDACTED]\nSet-Cookie \n: [REDACTED]"),
    "bearer": ("Bearer --SECRET7a1_token\nSet-Cookie\n: v SECRET7v2", "Bearer [REDACTED]\nSet-Cookie\n: [REDACTED]"),
    "vertical-tab": ("Cookie: --password\nSet-Cookie\x0b: v SECRET7v2", "Cookie: [REDACTED]\nSet-Cookie\x0b: [REDACTED]"),
    "unread-value": ("Cookie: --password\nSet-Cookie\n: ;v SECRET7v2", "Cookie: [REDACTED]\nSet-Cookie\n: [REDACTED]"),
    "refused-bearer": ("Authorization: Bearer\nSet-Cookie\n: v SECRET7v2", "Authorization: [REDACTED]\nSet-Cookie\n: [REDACTED]"),
    "inline-refused-bearer": ("x Cookie: Bearer\nSet-Cookie\n: v SECRET7v2", "x Cookie: [REDACTED]\nSet-Cookie\n: [REDACTED]"),
    "bearer-token-like-name": ("Authorization: Bearer\nSECRET7ab1Set-Cookie\n: v SECRET7v2",
                               'Authorization: [REDACTED]\n[REDACTED]\n: [REDACTED] [REDACTED]'),
    "bearer-token-like-name-same-line": ("Authorization: Bearer\nSECRET7ab1Set-Cookie\x0b: v SECRET7v2",
                                         'Authorization: [REDACTED]\n[REDACTED]\x0b[REDACTED] [REDACTED] [REDACTED]'),
}
ROUTED = ("api_key='xyz --SECRET7a1_token\n  Proxy-Authorization:Basic SECRET7c3'\xa0--SECRET7a1_token\n"
          "SECRET7ab1Set-Cookie \n: v SECRET7v2\n\nSECRET7c3")


@pytest.mark.parametrize("text,scrubbed", FOLD_AT_LINE_START.values(), ids=FOLD_AT_LINE_START.keys())
def test_a_fold_leaves_a_line_start_header_to_the_line_start_rule(text, scrubbed):
    """The fold, or the refused `Bearer`'s line, takes nothing there, so the line-start rule
    takes the header's value whole, in every mode, as both references do."""
    _assert_hidden(text)
    assert _new_leaks(text) == {} and _splits(text) == [] and _unstable(text) == {}
    assert handoff.scrub_secrets(text)[0] == handoff.scrub_secrets(text, whole=True)[0] == scrubbed


def test_the_join_p3_1_s_fix_routed_into_that_class_hides_its_value():
    """With P3-1's fix the first line's fold takes nothing, so the second line's header folds
    onto the third line's name: no mode shows `SECRET7v2` (902cfe90a and c469f44b6 hid it;
    `SECRET7c3`, alone on the last line, shows in every tree)."""
    assert all("SECRET7v2" not in out for out in _outputs(handoff, ROUTED).values())
    assert _new_leaks(ROUTED) == {} and _splits(ROUTED) == [] and _unstable(ROUTED) == {}


LINE_START_NAMES = ("Set-Cookie", "Authorization", "SECRET7ab1Set-Cookie")
#: Headers whose value a `Bearer` ends, before a run that `Bearer` refuses as a key.
REFUSED_BEARER_LEADS = ("Authorization: Bearer", "x Cookie: Bearer", "secret:'Cookie: Bearer")
#: A value the plain rule reads, and one it does not (`;`), which a span took the whole
#: name before.
LINE_START_VALUES = ("v SECRET7v2", "Basic SECRET7v2", ";v SECRET7v2")
#: Whitespace other than spaces and tabs between the name and its colon, which only
#: `_HEADER_RE` reads (`\s*:`).
LINE_START_SEPS = ("\n: ", "\x0b: ", "\x0c: ", "\xa0: ", "\r: ", " \n: ", "\n\n: ", "\u2028: ")


def fold_at_line_start_matrix() -> list[str]:
    """16 leads (13 folding, 3 refused `Bearer`s) x 5 gaps x 3 names x 8 separators x 3
    values: 5,760 cells."""
    return [lead + gap + name + sep + value for lead, gap, name, sep, value in itertools.product(
        (*P3_1_CLASS_LEADS, *REFUSED_BEARER_LEADS), HN_GAPS, LINE_START_NAMES, LINE_START_SEPS, LINE_START_VALUES)]


def test_no_fold_hides_a_line_start_header_from_its_rule():
    """All 5,760 cells: no mode shows the header's value (c469f44b6: 2,925 cells showed
    it, 1,560 of them where both references hide it), and none shows what a reference
    hides, cuts an excerpt that disagrees with the whole text, or reveals on a second
    scrub. 285 of them, each a refused `Bearer` before a name that holds a secret part,
    showed the value at c469f44b6 and in both references, and 75 of those where 3fbc4b7c5
    hides it: the `Bearer` rule took that part (`[REDACTED]Cookie`) before any header rule
    read the line."""
    cells = fold_at_line_start_matrix()
    assert len(cells) == len(set(cells)) == 5760
    assert [cell for cell in cells if any("SECRET7v2" in out for out in _outputs(handoff, cell).values())] == []
    assert _failures(cells) == CLEAN


# --- P3-3: the count test kills MQ2 ----------------------------------------------------

#: MQ2 (review r4): the flag rule's cursor stops at a secret-named flag, not past its fold,
#: so a later secret-named flag inside that fold computes its own fold again before it is
#: skipped. The output is the head's; the work is quadratic in the flags a line holds.
MQ2 = (("            if start < cursor:\n"
        "                continue                        # the flag is inside the previous flag's value\n",
        "            if start < cursor and not _value_runs(key[flag_start + 2:]):\n"
        "                continue\n"),
       ("                end, fold = token.end(), _fold_value(text, token.end())\n",
        "                end, fold = token.end(), _fold_value(text, token.end())\n"
        "                if start < cursor:\n"
        "                    continue\n"))


def _mq2() -> types.FunctionType:
    """`_scrub_named_values` with MQ2 applied, in `handoff`'s own globals (so the count
    test's patched `_kept_span` and `_identifier_char` are the ones it calls)."""
    source = textwrap.dedent(inspect.getsource(handoff._scrub_named_values))
    for old, new in MQ2:
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    module = compile(source, handoff.__file__, "exec")
    original = handoff._scrub_named_values
    code = next(const for const in module.co_consts
                if isinstance(const, types.CodeType) and const.co_name == original.__name__)
    mutant = types.FunctionType(code, vars(handoff), original.__name__)
    mutant.__kwdefaults__ = original.__kwdefaults__
    return mutant


def test_mq2_scrubs_as_the_head_does():
    """The mutant changes the work, not the output (so only a count or a timing sees it)."""
    mutant = _mq2()
    for text in ("--SECRET7a1_token 'Cookie:x' " * 3 + "tail", "--SECRET7a1_token\nKEY=--db3-password v",
                 "deploy --SECRET7a1_token x --SECRET7b2_token y"):
        for kwargs in ({}, {"flags": True}, {"quoted": True}):
            assert mutant(text, lambda matched, out: out, **kwargs) == handoff._scrub_named_values(
                text, lambda matched, out: out, **kwargs)


def test_the_count_test_kills_mq2(monkeypatch):
    """P3-3: with several flag-rule folds on one line and a trailer no token rule takes,
    MQ2's walk grows about 16x at 4x input, and the count test fails on it; the head's
    grows 4x (`test_scrub_review_r3.test_the_new_folds_read_each_line_once`, this unit
    among them). With round 3's unit and trailer it passed."""
    monkeypatch.setattr(handoff, "_scrub_named_values", _mq2())
    found = _counts("--SECRET7a1_token 'Cookie:x' ", monkeypatch)
    with pytest.raises(AssertionError):
        _assert_linear(found)
    small, large = found["excerpt"]
    assert large["walk"] > 12 * small["walk"], found


# --- P3-1's fix, made linear: folds before a line that ends in an owning flag ---------

#: Lines of folds at quoted header names that end in a credential flag glued to a long
#: identifier (`--aaa…_password`), which owns a value past the line. P3-1's fix makes each
#: such fold take nothing; computed afresh, each walked the line's last identifier again,
#: so the work was the folds times the identifier (found this round: 12.5 s for 80,010
#: characters, against 19 ms at c469f44b6). A fold now reads a line it found ending in an
#: owning item from `_ENDS_EARLY` (`handoff._fold_value`).
ENDING_FLAG_UNITS = {"quoted-inline": "-H 'Cookie: --SECRET7a1_token 'Cookie:x ",
                     "flags": "--SECRET7a1_token 'Cookie:x "}


@pytest.mark.parametrize("unit", ENDING_FLAG_UNITS.values(), ids=ENDING_FLAG_UNITS.keys())
def test_folds_before_a_line_ending_in_an_owning_flag_read_the_line_once(unit, monkeypatch):
    """At 4x input (50 and 200 units before a 2,010- and an 8,010-character flag), the walk
    over identifiers grows at most 4x in the excerpt, whole-text and `detail_line` modes,
    and the line's flag keeps its own rule: no `SECRET7` value shows."""
    found = _counts(unit, monkeypatch, trailer=lambda size: "--" + _trailer(size))
    _assert_linear(found)
    text = unit * 50 + "--" + _trailer(2000)
    assert "SECRET7" not in handoff.scrub_secrets(text)[0] and _new_leaks(text) == {}

