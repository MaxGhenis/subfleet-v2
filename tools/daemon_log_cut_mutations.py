#!/usr/bin/env python3
"""Put each of round 5's fixes back (#233 review r3) and see which named test fails.

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
- `raw-logs`: `daemon logs` printing daemon.log raw, with and without `--follow`.
"""
from __future__ import annotations

import contextlib
import io
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
from tests.unit import test_daemon_log_cut_windows as windows  # noqa: E402
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
    root = Path(tempfile.mkdtemp(prefix="sf-", dir="/tmp"))
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


MUTATIONS = {"baseline": lambda patch: None, "whole-margins": _whole_margins,
             "backtracking-header": _backtracking_header, "whole-reminder-search": _whole_reminder_search,
             "folded-tail": _folded_tail, "raw-logs": _raw_logs}

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
