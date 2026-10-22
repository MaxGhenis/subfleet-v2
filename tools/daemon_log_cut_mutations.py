#!/usr/bin/env python3
"""Put each of rounds 5's and 6's fixes back (#233 reviews r3 and r4) and see which named test fails.

Each mutation is applied in this process with `pytest.MonkeyPatch`, the named test
functions are called with their fixtures built here, the outcome is printed, and the
mutation is undone. `baseline` is the control: every named test passes. No pytest
process runs, so one hold of the host's shared suite lock covers the whole sweep:

    TMPDIR=… .venv/bin/python tools/daemon_log_cut_mutations.py

Mutations:
- `whole-margins`: the margins blanked over the whole text again (`_Reading`);
- `backtracking-header`: the leading header matched by the regex over the whole text,
  `\\s*` backtracking (round 4's `_LEADING_HEADER_RE`);
- `whole-reminder-search`: the reminder tag searched by its regex over the whole text;
- `folded-tail`: `daemon start`'s tail folded to one line again (no line keeping);
- `raw-logs`: `daemon logs` printing daemon.log raw, with and without `--follow`;

and round 6's (review r4):
- `tail-not-neutralised`: the tail as at round 5 (`tests/reference_tail_1cf653dff.py`):
  reminders not resolved before the cut, tags not marked after the scrub;
- `handler-not-neutralised`: the handler writing the tags its scrub leaves;
- `body-by-regex`: the tail's data URI body check by round 4's regex again,
  `_NOT_BODY_RE.search(text, comma + 1, first)` (review r4, P3-3);
- `unmetered-data-body`: `_DATA_BODY_RE` held where the meter does not see it;
- `pem-prefix-not-held`: `daemon logs -f` no longer holding a private key's first line
  still being written;
- `tail-unmarked-after-scrub`: the tail resolving reminders before its cut, but not
  marking the tags its scrub leaves (a log short enough to be scrubbed whole);
- `follow-not-neutralised`: `daemon logs -f` writing the tags its scrub leaves;
- `unmasked-strip`: the tail taking reminders out without the whole-text scrub's quoted
  pass first, so a closing tag inside a quoted value closes a reminder;

and round 7's (review r5):
- `unconditional-take-out`: reminders taken out whatever the first pass over what is left
  replaces, as round 6 took them out (P2-1);
- `no-margin-free-check`: the margin-free reading no longer checked (P2-1);
- `unanchored-quote-regex`: `_QUOTE_RUN_RE` without its lookbehind, `(\\*)(["'])` (P3-1);
- `follow-start-held`: `daemon logs -f` printing first only what the follower settled,
  the lines before the first tag (P3-2).
"""
from __future__ import annotations

import bisect
import contextlib
import io
import logging
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet import cli, daemon_log  # noqa: E402
from subfleet.sessions import handoff  # noqa: E402
from tests import reference_tail_1cf653dff as round5  # noqa: E402
from tests.unit import test_daemon_log_cut_windows as windows  # noqa: E402
from tests.unit import test_daemon_log_first_pass as first_pass  # noqa: E402
from tests.unit import test_daemon_log_reminders as reminders  # noqa: E402
from tests.unit import test_daemon_log_tail as tail  # noqa: E402


class Captured:
    """`capsys`, for a test called here: what the CLI printed since the last read."""

    def __init__(self):
        self.out, self.err = io.StringIO(), io.StringIO()

    def readouterr(self):
        out, err = self.out.getvalue(), self.err.getvalue()
        self.out.seek(0), self.out.truncate(), self.err.seek(0), self.err.truncate()
        return type("Result", (), {"out": out, "err": err})()


@contextlib.contextmanager
def fixtures():
    """The fixtures the named tests take: `monkeypatch`, `tmp_path`, `root`, `capsys`."""
    patch, captured = pytest.MonkeyPatch(), Captured()
    tmp = Path(tempfile.mkdtemp(prefix="sf-mut-"))
    root = Path(tempfile.mkdtemp(prefix="sf-"))
    patch.setenv("SUBFLEET_HOME", str(root))
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "SUBFLEET_RUN_DETACH", "SUBFLEET_DAEMON_BIN"):
        patch.delenv(name, raising=False)
    try:
        with contextlib.redirect_stdout(captured.out), contextlib.redirect_stderr(captured.err):
            yield {"monkeypatch": patch, "tmp_path": tmp, "root": root, "capsys": captured}
    finally:
        patch.undo()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _whole_margins(patch):
    def init(reading, text, head_end, tail_start):
        reading.text, reading.spans = text, []
        reading.read = handoff._MARGIN_MARK_RE.sub(r"\1 ", text)
    patch.setattr(handoff._Reading, "__init__", init)


def _backtracking_header(patch):
    patch.setattr(handoff, "_LEADING_HEADER_RE", re.compile(r"(?i)\s*(?:authorization|cookie|set-cookie)\s*:"),
                  raising=False)
    patch.setattr(handoff, "_leads_with_header",
                  lambda reading: handoff._LEADING_HEADER_RE.match(reading.read) is not None)


def _whole_reminder_search(patch):
    patch.setattr(handoff, "_holds_reminder_tag", lambda text: handoff._REMINDER_TAG_RE.search(text) is not None)


def _folded_tail(patch):
    def folded(text, count):
        return daemon_log.scrubbed(text).splitlines()[-count:] if count > 0 else []
    patch.setattr(daemon_log, "scrubbed_tail", folded)


def _raw_logs(patch):
    def raw(args):
        path = cli._root(args) / cli.LOG_NAME
        if args.lines < 0:
            return cli.fail(cli.Exit.INVALID_INPUT, "daemon logs: --lines must be non-negative")
        with open(path, "r", errors="replace") as stream:
            text = stream.read()
            for line in (text.splitlines()[-args.lines:] if args.lines else []):
                cli.out(line)
            if not args.follow:
                return int(cli.Exit.OK)
            pending = ""
            try:
                while True:
                    more = stream.read()
                    if more:
                        pending += more
                        *lines, pending = pending.split("\n")
                        for line in lines:
                            cli.out(line)
                    else:
                        cli.time.sleep(0.25)
            except KeyboardInterrupt:
                return int(cli.Exit.OK)
    patch.setattr(cli, "cmd_daemon_logs", raw)


def _tail_not_neutralised(patch):
    patch.setattr(daemon_log, "scrubbed_tail", round5.scrubbed_tail)


def _handler_not_neutralised(patch):
    def format(formatter, record):
        try:
            return formatter._scrubbed(record)
        except Exception as exc:            # noqa: BLE001 - as the formatter's own
            return daemon_log.failed_line(record, exc)
    patch.setattr(daemon_log.ScrubbingFormatter, "format", format)


def _body_by_regex(patch):
    patch.setattr(handoff, "_NOT_BODY_RE", re.compile(r"[^A-Za-z0-9+/=\s]"), raising=False)
    patch.setattr(handoff, "_body_between",
                  lambda text, start, end: handoff._NOT_BODY_RE.search(text, start, end) is None)


class _Unseen:
    """A pattern the meter does not wrap, as one held outside `handoff`'s names would be."""

    def __init__(self, pattern):
        self._pattern = pattern

    def __getattr__(self, name):
        return getattr(self._pattern, name)


def _unmetered_data_body(patch):
    patch.setattr(handoff, "_DATA_BODY_RE", _Unseen(handoff._DATA_BODY_RE))


def _pem_prefix_not_held(patch):
    patch.setattr(handoff, "_PEM_START_PREFIX_RE", re.compile(r"(?!)"))


def _tail_unmarked_after_scrub(patch):
    def unmarked(text, count):              # `scrubbed_tail` without its last `neutralised`
        if count <= 0:
            return []
        try:
            group = daemon_log._GROUP_MARK in text
            if len(text) > daemon_log._tail_limit(text, count):
                resolved = daemon_log._reminders_resolved(text)
                if resolved is not None:
                    text = resolved
            cut = handoff.cut_whole(text, daemon_log._tail_limit(text, count))
            return daemon_log.scrubbed_block(cut, group=group, keep_lines=True).splitlines()[-count:]
        except Exception as exc:            # noqa: BLE001 - as the tail's own
            return [daemon_log.failed_line(daemon_log._record("", logging.INFO), exc)]
    patch.setattr(daemon_log, "scrubbed_tail", unmarked)


def _follow_not_neutralised(patch):
    def write(follower, end):               # `LogFollower._write` without `neutralised`
        written, follower.pending = follower.pending[:end], follower.pending[end:]
        before = follower.context + follower.shown
        try:
            own = daemon_log.scrubbed_block(written, group=daemon_log._GROUP_MARK in written, keep_lines=True)
            if before:
                around = before + written
                shown = daemon_log.scrubbed_block(around, group=daemon_log._GROUP_MARK in around, keep_lines=True)
                own = daemon_log._only(own, set(daemon_log._RUN.findall(shown)) | {"|", "+"})
            lines = own.splitlines()
        except Exception as exc:            # noqa: BLE001 - as the follower's own
            lines = [daemon_log.failed_line(daemon_log._record("", logging.INFO), exc)]
        follower.context, follower.shown = daemon_log._context_of(before + written), ""
        return lines
    patch.setattr(daemon_log.LogFollower, "_write", write)


def _unmasked_strip(patch):
    patch.setattr(daemon_log, "quoted_value_spans", lambda text: [])     # masks nothing


def _unconditional_take_out(patch):
    def unconditional(text, tags):          # round 6: what is left used, whatever its first pass replaces
        spans = daemon_log.quoted_value_spans(text)
        starts = [start for start, _, _ in spans]
        pairs, opening = [], None
        for index, (start, _, opens) in enumerate(tags):
            at = bisect.bisect_right(starts, start) - 1
            if at >= 0 and start < spans[at][1]:
                continue
            if opening is None:
                if opens:
                    opening = index
            elif not opens:
                pairs.append((opening, index))
                opening = None
        pieces, cursor = [], 0
        for opening, closing in pairs:
            pieces.append(text[cursor:tags[opening][0]])
            cursor = tags[closing][1]
        pieces.append(text[cursor:])
        return "".join(pieces), pairs

    def resolved(text):
        # Round 6 checked the margin-free reading's pairs only. Keeping round 7's
        # extra comparison of the text left by that reading would strengthen this
        # historical control, and undercount its leaks and fallbacks in the census.
        tags = daemon_log.reminder_tags(text)
        if not tags:
            return text
        first = next((start for start, _, opens in tags if opens), None)
        if first is None or not any(start > first and not opens for start, _, opens in tags):
            return daemon_log._marked(text, tags)
        kept, pairs = unconditional(text, tags)
        if daemon_log._GROUP_MARGIN_LINE.search(text) is not None:
            bare = daemon_log._margin_free(text)
            bare_tags = daemon_log.reminder_tags(bare)
            if len(bare_tags) != len(tags) or unconditional(bare, bare_tags)[1] != pairs:
                return None
        kept_tags = daemon_log.reminder_tags(kept)
        if len(kept_tags) != len(tags) - sum(closing - opening + 1 for opening, closing in pairs):
            return None
        return daemon_log._marked(kept, kept_tags)

    patch.setattr(daemon_log, "_taken_out", unconditional)
    patch.setattr(daemon_log, "_reminders_resolved", resolved)


def _no_margin_free_check(patch):
    def resolved(text):                     # `_reminders_resolved` without the margin-free reading
        tags = daemon_log.reminder_tags(text)
        if not tags:
            return text
        first = next((start for start, _, opens in tags if opens), None)
        if first is None or not any(start > first and not opens for start, _, opens in tags):
            return daemon_log._marked(text, tags)
        taken = daemon_log._taken_out(text, tags)
        if taken is None:
            return None
        kept, pairs = taken
        kept_tags = daemon_log.reminder_tags(kept)
        if len(kept_tags) != len(tags) - sum(closing - opening + 1 for opening, closing in pairs):
            return None
        return daemon_log._marked(kept, kept_tags)
    patch.setattr(daemon_log, "_reminders_resolved", resolved)


def _unanchored_quote_regex(patch):
    patch.setattr(handoff, "_QUOTE_RUN_RE", re.compile(r"(\\*)([\"'])"))


def _follow_start_held(patch):
    real = cli.cmd_daemon_logs

    def held(args):                         # round 6's start: only what the follower settled
        if not args.follow:
            return real(args)
        with open(cli._root(args) / cli.LOG_NAME, "r", errors="replace") as stream:
            text = stream.read()
            follower = daemon_log.LogFollower(text)
            settled = len(text) - len(follower.shown) - len(follower.pending)
            for line in daemon_log.scrubbed_tail(text[:settled], args.lines):
                cli.out(line)
            try:
                while True:
                    more = stream.read()
                    if more:
                        for line in follower.feed(more):
                            cli.out(line)
                    else:
                        cli.time.sleep(0.25)
            except KeyboardInterrupt:
                for line in follower.flush():
                    cli.out(line)
                return int(cli.Exit.OK)
    patch.setattr(cli, "cmd_daemon_logs", held)


MUTATIONS = {"baseline": lambda patch: None, "whole-margins": _whole_margins,
             "backtracking-header": _backtracking_header, "whole-reminder-search": _whole_reminder_search,
             "folded-tail": _folded_tail, "raw-logs": _raw_logs,
             "tail-not-neutralised": _tail_not_neutralised, "handler-not-neutralised": _handler_not_neutralised,
             "body-by-regex": _body_by_regex, "unmetered-data-body": _unmetered_data_body,
             "pem-prefix-not-held": _pem_prefix_not_held, "tail-unmarked-after-scrub": _tail_unmarked_after_scrub,
             "follow-not-neutralised": _follow_not_neutralised, "unmasked-strip": _unmasked_strip,
             "unconditional-take-out": _unconditional_take_out, "no-margin-free-check": _no_margin_free_check,
             "unanchored-quote-regex": _unanchored_quote_regex, "follow-start-held": _follow_start_held}

#: The named tests, each with its arguments beyond its fixtures.
TESTS = [
    ("test_at_16_mib_no_regex_reads_past_the_cut_s_windows[margin-only-lines]",
     windows.test_at_16_mib_no_regex_reads_past_the_cut_s_windows, {"shape": "margin-only-lines"}),
    ("test_at_16_mib_no_regex_reads_past_the_cut_s_windows[exception-group]",
     windows.test_at_16_mib_no_regex_reads_past_the_cut_s_windows, {"shape": "exception-group"}),
    ("test_at_16_mib_no_regex_reads_past_the_cut_s_windows[leading-spaces]",
     windows.test_at_16_mib_no_regex_reads_past_the_cut_s_windows, {"shape": "leading-spaces"}),
    ("test_at_16_mib_no_regex_reads_past_the_cut_s_windows[run-of-lt]",
     windows.test_at_16_mib_no_regex_reads_past_the_cut_s_windows, {"shape": "run-of-lt"}),
    ("test_daemon_start_s_tail_keeps_its_last_twenty_lines",
     tail.test_daemon_start_s_tail_keeps_its_last_twenty_lines, {}),
    ("test_a_flag_s_value_in_the_tail_stays_hidden_with_the_lines_kept",
     tail.test_a_flag_s_value_in_the_tail_stays_hidden_with_the_lines_kept, {}),
    ("test_daemon_logs_prints_an_older_daemon_s_raw_lines_scrubbed",
     tail.test_daemon_logs_prints_an_older_daemon_s_raw_lines_scrubbed, {}),
    ("test_daemon_logs_follow_prints_new_lines_scrubbed",
     tail.test_daemon_logs_follow_prints_new_lines_scrubbed, {}),
    # round 6
    ("test_at_16_mib_no_regex_reads_past_the_cut_s_windows[data-uri-body]",
     windows.test_at_16_mib_no_regex_reads_past_the_cut_s_windows, {"shape": "data-uri-body"}),
    ("test_the_quote_kinds_cost_a_look_each_and_the_meter_counts_the_body_by_what_it_reads",
     windows.test_the_quote_kinds_cost_a_look_each_and_the_meter_counts_the_body_by_what_it_reads, {}),
    ("test_a_tag_on_the_first_line_no_longer_blanks_the_tail[40-orphan-open]",
     reminders.test_a_tag_on_the_first_line_no_longer_blanks_the_tail, {"form": "orphan-open", "count": 40}),
    ("test_a_tag_on_the_first_line_no_longer_blanks_the_tail[40-complete-block]",
     reminders.test_a_tag_on_the_first_line_no_longer_blanks_the_tail, {"form": "complete-block", "count": 40}),
    ("test_daemon_logs_keeps_its_lines_after_a_tagged_first_line[40-handler-line]",
     reminders.test_daemon_logs_keeps_its_lines_after_a_tagged_first_line, {"form": "handler-line", "count": 40}),
    ("test_the_handler_writes_no_reminder_tag[orphan-open]",
     reminders.test_the_handler_writes_no_reminder_tag, {"form": "orphan-open"}),
    ("test_an_orphan_tag_reaches_daemon_log_as_the_marker_through_a_real_handler",
     reminders.test_an_orphan_tag_reaches_daemon_log_as_the_marker_through_a_real_handler, {}),
    ("test_a_private_key_s_first_line_is_held_until_it_can_no_longer_become_one",
     reminders.test_a_private_key_s_first_line_is_held_until_it_can_no_longer_become_one, {}),
    ("test_a_split_first_line_past_the_context_still_hides_its_key",
     reminders.test_a_split_first_line_past_the_context_still_hides_its_key, {}),
    ("test_a_log_too_short_to_cut_shows_each_tag_as_the_marker[orphan-open]",
     reminders.test_a_log_too_short_to_cut_shows_each_tag_as_the_marker, {"form": "orphan-open"}),
    ("test_daemon_logs_follow_holds_a_split_first_line",
     reminders.test_daemon_logs_follow_holds_a_split_first_line, {}),
    ("test_a_tag_a_quoted_value_masks_is_none_to_the_strip[last-MASKED_CLOSE]",
     reminders.test_a_tag_a_quoted_value_masks_is_none_to_the_strip, {"line": reminders.MASKED_CLOSE, "where": "last"}),
    # round 7
    *((f"test_a_join_that_opens_a_value_keeps_the_marker_rule[{name}]",
       first_pass.test_a_join_that_opens_a_value_keeps_the_marker_rule, {"name": name}) for name in sorted(first_pass.JOINS)),
    ("test_daemon_logs_and_daemon_start_s_tail_hide_what_the_whole_scrub_hides[quote]",
     first_pass.test_daemon_logs_and_daemon_start_s_tail_hide_what_the_whole_scrub_hides, {"name": "quote"}),
    ("test_daemon_logs_and_daemon_start_s_tail_hide_what_the_whole_scrub_hides[margin-free]",
     first_pass.test_daemon_logs_and_daemon_start_s_tail_hide_what_the_whole_scrub_hides, {"name": "margin-free"}),
    ("test_where_the_margin_free_reading_strips_other_reminders_the_marker_rule_stays",
     reminders.test_where_the_margin_free_reading_strips_other_reminders_the_marker_rule_stays, {}),
    ("test_a_join_that_opens_nothing_is_still_resolved[40]",
     first_pass.test_a_join_that_opens_nothing_is_still_resolved, {"count": 40}),
    ("test_at_16_mib_no_regex_reads_past_the_cut_s_windows[backslash-runs]",
     windows.test_at_16_mib_no_regex_reads_past_the_cut_s_windows, {"shape": "backslash-runs"}),
    ("test_the_meter_counts_what_a_regex_reads_not_its_span",
     windows.test_the_meter_counts_what_a_regex_reads_not_its_span, {}),
    ("test_a_settle_reads_a_run_of_backslashes_a_fixed_number_of_times",
     windows.test_a_settle_reads_a_run_of_backslashes_a_fixed_number_of_times, {}),
    ("test_following_a_tagged_log_starts_with_its_last_lines",
     first_pass.test_following_a_tagged_log_starts_with_its_last_lines, {}),
    ("test_following_starts_with_the_tag_s_line_marked[40]",
     first_pass.test_following_starts_with_the_tag_s_line_marked, {"count": 40}),
]


def run(test, arguments) -> str:
    import inspect
    with fixtures() as given:
        wanted = inspect.signature(test).parameters
        try:
            test(**{name: given[name] for name in wanted if name in given}, **arguments)
        except AssertionError as failure:
            return "FAILED " + (str(failure).splitlines() or [""])[0][:120]
        except Exception:                   # noqa: BLE001 - reported, never a pass
            return "ERROR " + traceback.format_exc().strip().splitlines()[-1][:120]
    return "passed"


def main(argv: list[str]) -> int:
    names = argv or list(MUTATIONS)
    caught = True
    for name in names:
        patch = pytest.MonkeyPatch()
        try:
            MUTATIONS[name](patch)
            results = [(label, run(test, arguments)) for label, test, arguments in TESTS]
        finally:
            patch.undo()
        failed = [label for label, outcome in results if outcome != "passed"]
        print(f"## {name}: {len(failed)} of {len(results)} named tests failed")
        for label, outcome in results:
            print(f"- {label}: {outcome}")
        if (name == "baseline") == bool(failed):
            caught = False
    return 0 if caught else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
