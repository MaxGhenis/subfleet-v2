"""Review r9b's pins for #239 (D-SCR2, rounds 8 and 9), red at `f5cbd1df7` by design.

Both findings are the mechanism of CI run 40545353102's counterexample, which round 9 fixed
for a text within the budget only: a piece release's cut leaves is kept for the runs of the
head's scrub of the uncut text, which holds no part-word, so the part-word at the cut becomes
`[REDACTED]`. Neither shows a value either scrub hides; both write what neither scrub does.

- **P2-1** (`handoff._released_pieces`, the excerpt path with a limit): a text over the budget
  that `scrub_bounded` or `clean` bounds keeps release's excerpts cut to the limit first, then
  each kept for the runs the head's scrub of its whole excerpt holds. The part-words at both
  cuts become `[REDACTED]`, the result passes the limit, and `truncate` cuts it again, through
  release's marker: the written marker counts 6 or 13 characters where some 300,000 were left
  out, beside placeholder fragments (`[REDACTE`, `ACTED]`). The excerpts module's own
  oversized-text input does it; its test reads only that some marker is there.
- **P2-2** (`suggestions._line_bounded`, so `scrubbed_line`: daemon.log's lines, a refusal's
  words, a suggestion's title and summary): release's folded line is cut first, then kept for
  the runs of the head's uncut line. The part-word at the cut becomes `[REDACTED]` or is
  dropped, and a line whose first `limit` characters are one run keeps only `[REDACTED]…`.

Each case's text is one both scrubbers scrub alike, so the composition changes nothing there,
and release's own output is what a mode should write. Values are made up."""

from __future__ import annotations

import re

import pytest

from subfleet import refusals
from subfleet.conversations import redact, suggestions
from subfleet.sessions import handoff, scrub_release

PROSE = "ordinary words in a line of prose, with nothing in it worth hiding\n"
MARKER = re.compile(r"\n… \[([\d,]+) characters omitted\] …\n")


def prose(n: int) -> str:
    return (PROSE * (n // len(PROSE) + 1))[:n]


def _fragments(out: str) -> list[str]:
    """Runs that are part of `[REDACTED]` but not all of it."""
    return [run for run in out.split() if run != handoff.REDACTED and len(run) > 1
            and (handoff.REDACTED.startswith(run) or handoff.REDACTED.endswith(run))]


OVER_BUDGET = {
    "the-excerpts-module-s-input-at-text-event-max": (
        'first line, api_key = "Zq1headsecret99"\n' + prose(400_000)
        + 'last lines, password = "Zq2tailsecret99"\n' + prose(2_000), redact.TEXT_EVENT_MAX),
    "prose-at-4000": (prose(300_000), 4_000),
    "prose-at-8000-a-recent-tool-result-s-cap": (prose(300_000), 8_000),
    "prose-at-600": (prose(300_000), 600),
}


@pytest.mark.parametrize("case", OVER_BUDGET)
def test_p2_1_a_bounded_excerpt_keeps_one_marker_that_counts_what_it_left_out(case):
    """At `f5cbd1df7`, `prose-at-4000` writes `…of prose,\\n… [\\n… [6 characters omitted]
    …\\nACTED]\\nordina…`, where release writes `…of prose,\\n… [296,034 characters omitted]
    …\\nding\\nordina…`."""
    text, limit = OVER_BUDGET[case]
    out, _ = handoff.clean(text, limit)
    released, _ = scrub_release.clean(text, limit)
    assert len(out) <= limit
    counts = [int(count.replace(",", "")) for count in MARKER.findall(out)]
    assert len(counts) == 1 and counts[0] >= len(text) - limit, counts
    assert _fragments(out) == []
    assert out == released


def _released_line(text: str, limit: int) -> str:
    return suggestions.bounded(suggestions._twice(scrub_release.scrub_secrets, text, suggestions._fold)[0], limit, "…")


LINES = {
    "a-path-cut-at-60": ("OSError: [Errno 2] No such file or directory: "
                         "'/Users/someone/projects/subfleet/state/jobs/x.json'", 60),
    "one-run-past-60": ("x" * 70, 60),
    "prose-cut-mid-word-at-60": ("the scheduler refused to start because the configuration file was missing", 60),
    "a-daemon-log-line-of-paths-at-2000": ("job 1234 failed: " + "/Users/someone/projects/subfleet-v2/state/jobs/" * 60,
                                           refusals.LOG_LINE_MAX),
}


@pytest.mark.parametrize("case", LINES)
def test_p2_2_a_cut_line_is_release_s_cut_where_both_lines_agree(case):
    """At `f5cbd1df7`, `a-path-cut-at-60` writes `OSError: [Errno 2] No such file or
    directory: [REDACTED]…` and `one-run-past-60` writes `[REDACTED]…`, where release writes
    the path's first characters and sixty `x`s' first fifty-nine."""
    text, limit = LINES[case]
    released, head, _ = suggestions._both(text, suggestions._fold)
    assert released == head                      # both scrubbers write the same line
    out = refusals.scrubbed_line(text, limit)
    assert handoff.REDACTED not in out
    assert out == _released_line(text, limit)
