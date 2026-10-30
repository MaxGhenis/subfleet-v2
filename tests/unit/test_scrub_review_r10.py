"""Review r10's pins for #239 (D-SCR2, round 10), green at `ae2bf1f88`.

Each pins a sentence of C-23.14 that no test read before, so a mutant that breaks it survived
every named test.

- **Per value, not per place** (check 4). `shown_by_both` keeps a run of release's output only
  if it is one of the head's runs. Mutant M4 keeps it if it is anywhere in the head's output,
  so where the head hides a token (`token:--x_token SECRET7…`) and shows a longer value that
  starts with it on the next line (`SECRET7…_more`), M4 writes the token at its place. The
  clause allows the token to show only "where the same value shows elsewhere", and a longer
  value is not the same one. Every value check in the scrub modules reads by substring
  (`value in out`), which cannot tell the two apart.
- **The marker where the two scrubbers cut differently** (check 2). Past the budget, the
  marker gives "what release's own cut of the same excerpts leaves out": the excerpts both
  scrubbers allow. Where the head refuses a break release allows (a line ending in a
  credential flag), those are the head's, and the count is not the number release's own
  output writes. The pieces property reads the count only where both cut alike, so a count
  taken from release's own excerpts (mutant M11) survived it.

Values are made up."""

from __future__ import annotations

import re

import pytest

from subfleet.sessions import handoff, scrub_release
from tests.unit.test_scrub_release_first import _bounds

MARKER = re.compile(r"… \[([\d,]+) characters omitted\] …")

#: A token the head hides at its place, beside a longer value that starts with it.
PREFIX_OF_A_LONGER_VALUE = {
    "the-next-line": ("token:--x_token SECRET7co1\necho SECRET7co1_more",
                      "[REDACTED] [REDACTED]\necho SECRET7co1_more"),
    "inside-a-run": ("token:--x_token SECRET7co2\necho xSECRET7co2y",
                     "[REDACTED] [REDACTED]\necho xSECRET7co2y"),
}


@pytest.mark.parametrize("case", PREFIX_OF_A_LONGER_VALUE)
def test_a_value_the_head_hides_does_not_show_because_a_longer_one_does(case):
    """Release writes `token:[REDACTED] SECRET7co1`, the head `token:--x_token [REDACTED]`, and
    the next line shows `SECRET7co1_more` in both. The token's run is no run of the head's
    output, so it goes; M4 writes `[REDACTED] SECRET7co1`."""
    text, expected = PREFIX_OF_A_LONGER_VALUE[case]
    released, head = scrub_release.scrub_secrets(text)[0], handoff._head_scrub_secrets(text)[0]
    assert "SECRET7co" in released.split("\n")[0] and "SECRET7co" not in head.split("\n")[0]
    assert handoff.shown_by_both(released, head) == expected
    assert handoff.scrub_secrets(text)[0] == expected
    out = handoff.scrub_bounded(text, None, strip_reminders=True)[0]
    assert {run for run in out.split() if "SECRET7" in run} <= set(head.split())


#: Every line ends in a credential flag, whose value the head's fold reads on the next line,
#: so the head allows no break; release allows each.
FLAG_LINES = "mysql -u root --password\n" * 12_000 + "the end of the log, with ordinary words\n"


def _both_allow(text: str, limit: int, strip: bool) -> str:
    """Release's own `scrub_bounded` of the excerpts both scrubbers allow (its scrubs not composed)."""
    head_cut, tail_cut = scrub_release._head_cut, scrub_release._tail_cut
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scrub_release, "_head_cut", lambda part: min(head_cut(part), handoff._head_cut(part)))
        patch.setattr(scrub_release, "_tail_cut", lambda part: max(tail_cut(part), handoff._tail_cut(part)))
        return scrub_release.scrub_bounded(text, limit, strip_reminders=strip)[0]


@pytest.mark.parametrize("limit", [600, 4_000, 60_000])
def test_where_the_head_cuts_more_cautiously_the_marker_counts_its_own_excerpts(limit):
    """At `ae2bf1f88` the head keeps neither excerpt (no break it allows is in reach), so it
    writes only the marker, with all 300,040 characters; release writes its excerpts and,
    at 4,000, 296,074."""
    out, released = handoff.clean(FLAG_LINES, limit)[0], scrub_release.clean(FLAG_LINES, limit)[0]
    assert (scrub_release._head_cut(FLAG_LINES), handoff._head_cut(FLAG_LINES)) == (131_050, 0)
    assert out == f"… [{len(FLAG_LINES):,} characters omitted] …"
    assert MARKER.findall(out) == MARKER.findall(_both_allow(FLAG_LINES, limit, True))
    assert MARKER.findall(out) != MARKER.findall(released)
    assert len(out) <= limit


@pytest.mark.parametrize("strip", [False, True])
def test_with_small_excerpts_the_marker_counts_the_excerpts_both_scrubbers_allow(strip):
    """Excerpts of 40 characters: the head refuses the break after `x --password`, so its head
    excerpt stops a line earlier; at 69 it writes 117, release 115."""
    text = ("x" * 21 + "\nx --password\nSECRET7h8 and more words\n"
            + "ordinary words in a line of prose here\n" * 2 + "or")
    with _bounds(40):
        assert handoff._head_cut(text) < scrub_release._head_cut(text)
        for limit in range(30, 140):
            out = handoff.scrub_bounded(text, limit, strip_reminders=strip)[0]
            assert MARKER.findall(out) == MARKER.findall(_both_allow(text, limit, strip)), limit
            assert len(out) <= limit
        out, released = (module.scrub_bounded(text, 69, strip_reminders=strip)[0] for module in (handoff, scrub_release))
    assert (MARKER.findall(out), MARKER.findall(released)) == (["117"], ["115"])
