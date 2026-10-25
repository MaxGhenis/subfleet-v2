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
from tests.unit.test_scrub_review_r3 import CLEAN, HN_GAPS, HN_LEADS, _assert_linear, _counts, _failures

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
P3_4_A = {repr(gap): (f"x\nSECRET7ab1Cookie{gap}: v", f"x\n[REDACTED]Cookie{gap}: [REDACTED]")
          for gap in ("\n", "\x0b", "\x0c", "\xa0", "\r")}
#: P3-4 (b): daemon.log's line, as 902cfe90a gave it with the name whole, now with the
#: name's secret part gone too; the other modes keep the next line, as both references do.
P3_4_B = ("SECRET7ab1Cookie: x\nSECRET7c3", "[REDACTED]Cookie: [REDACTED]", "[REDACTED]Cookie: [REDACTED]\nSECRET7c3")

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
OVER_REDACTED = {
    # The line fold at a quoted header's name takes the rest of the line past the name's
    # closing quote: a digit-named flag's fold, and a secret-named flag's, which takes the
    # name of the `--password` flag after the quote with it.
    'curl --oauth2-token "Authorization: Bearer abc" -X POST https://api.example.com': "curl [REDACTED] [REDACTED]",
    'deploy --SECRET7a1_token "Cookie:x" --password SECRET7p9': "deploy [REDACTED] [REDACTED]",
    # P3-B's prefix rule takes a name prefix that holds a digit, secret or not.
    "X-Auth0-Authorization: Basic x": "[REDACTED]Authorization: [REDACTED]",
    "curl -H 'X-Amz1-Cookie: a=b' https://example.com": "curl -H '[REDACTED]Cookie: [REDACTED]' https://example.com",
    "session_v2_cookie: abc123": "[REDACTED]cookie: [REDACTED]",
    # Round 3's decision 4 (a digit-named flag is secret-named) chained through P2-A's
    # stop: the second flag's fold takes the third line's first word.
    "--SECRET7a1_token\nKEY=--db3-password\necho done": "[REDACTED]\n[REDACTED][REDACTED]\n[REDACTED] done",
}


@pytest.mark.parametrize("text,scrubbed", OVER_REDACTED.items())
def test_failing_closed_over_redacts_as_stated(text, scrubbed):
    """The stated cost (C-23.14), pinned in the excerpt and whole-text scrubs and, folded,
    in daemon.log's line and `detail_line`."""
    for whole in (False, True):
        assert handoff.scrub_secrets(text, whole=whole)[0] == scrubbed
    outputs = _outputs(handoff, text)
    folded = " ".join(scrubbed.split())
    assert outputs["daemon.log line"] == folded
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
