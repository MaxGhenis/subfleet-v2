"""Replay D-NOTICE-KEEP1's behavioral mutants without editing source.

Run from the checkout after syncing the dev environment::

    export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
    export UV_CACHE_DIR="$TMPDIR/uv-cache"
    SUBFLEET_LIVE=0 uv run --no-sync python tools/notice_keep_mutants.py

Each control/mutant runs in a separate pytest process, serially, with a fresh
basetemp below TMPDIR and a temporary pytest plugin. The plugin monkeypatches
imported functions only. A mutant is killed only by a call-phase AssertionError,
with no collection, setup or teardown failure; crashes and other exceptions are
invalid evidence. Logs, phase receipts and summary.json go to --output-dir, or
a fresh directory below TMPDIR by default. The command exits 0 only when all
controls pass and all selected mutants are killed. No live state is used.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/unit/test_notice_keep.py::"
MUTANTS = {
    "a": ("drop the notice age clause", TEST + "test_a_notice_just_under_the_keep_pins_and_one_just_over_does_not[7]"),
    "b": ("compare raw timestamp strings", TEST + "test_ages_compare_as_times_not_as_strings"),
    "c": ("leave the survey on the old permanent pin rule", TEST + "test_the_survey_and_a_pass_give_the_same_pins[7]"),
    "d": ("read zero as zero seconds", TEST + "test_zero_keeps_an_unread_notice_for_ever"),
    "e": ("omit the retirement outcome record", TEST + "test_one_outcome_per_session_and_none_for_a_notice_no_one_can_read_or_has_read"),
    "f": ("let an outcome pin its job", TEST + "test_the_outcome_pins_nothing"),
    "g": ("never withdraw an expired unread outcome", TEST + "test_an_undelivered_outcome_is_withdrawn_after_the_keep_and_a_delivered_one_is_left"),
    "h": ("keep a notice at exactly the cutoff", TEST + "test_a_notice_just_under_the_keep_pins_and_one_just_over_does_not[7]"),
    "i": ("drop 299 of 300 distinct outcome headers", TEST + "test_three_hundred_headers_and_mixed_state"),
    "j": ("expire an outcome at exactly the cutoff", TEST + "test_expiry_exact_cutoff_is_not_older"),
    "k": ("omit the delete's creation-time check", TEST + "test_row_replaced_between_expiry_read_and_delete_stays[creation-time]"),
    "l": ("omit the delete's text identity check", TEST + "test_row_replaced_between_expiry_read_and_delete_stays[text]"),
    "m": ("open the outcome lookup the old way", "tests/unit/test_retention_outcomes.py::test_outcomes_does_not_create_sqlite_sidecars"),
    "n": ("omit PR 294 from the change log", TEST + "test_changelog_names_pr_294"),
    "o": ("omit the retirement event's outcome digest check", TEST + "test_reused_same_second_ping_is_not_expired"),
}
SOURCE_FILES = (
    "subfleet/retention.py", "subfleet/retention_archive.py", "subfleet/retention_notices.py",
    "subfleet/retention_survey.py", "subfleet/store.py", "subfleet/retention_snapshot.py",
    "tests/unit/test_notice_keep.py", "tests/unit/test_retention_outcomes.py", "docs/acceptance-contract.md",
)


def install_mutant(name: str):
    """Install one behavioral fault in this subprocess and return its undo handle."""
    import pytest
    from subfleet import retention, retention_notices as notices, retention_survey

    patch = pytest.MonkeyPatch()
    if name == "a":
        patch.setattr(notices, "fresh", lambda created_at, oldest: True)
    elif name == "b":
        def raw_strings(created_at, oldest):
            return oldest is None or str(created_at) > oldest.isoformat(timespec="milliseconds")
        patch.setattr(notices, "fresh", raw_strings)
    elif name == "c":
        # Only the survey sees this proxy. The live pass still uses the real
        # retention module, so this actually tests their differential.
        class OldSurveyRule:
            def __getattr__(self, attribute):
                return getattr(retention, attribute)

            def _pin_reasons(self, *args, **kwargs):
                return retention._pin_reasons(*args, **{**kwargs, "notice_keep_s": 0})
        patch.setattr(retention_survey, "ret", OldSurveyRule())
    elif name == "d":
        original = notices.cutoff

        def zero_seconds(now, keep_s):
            return now if keep_s == 0 else original(now, keep_s)
        patch.setattr(notices, "cutoff", zero_seconds)
    elif name == "e":
        patch.setattr(notices, "write_outcomes", lambda *args, **kwargs: [])
    elif name == "f":
        from subfleet.store import pin_notice_jobs
        original = retention._pin_reasons

        def pin_outcomes(store, *args, **kwargs):
            reasons = original(store, *args, **kwargs)
            rows = store.query("SELECT notice_id, session_id, created_at FROM service_notices "
                               "WHERE state IN ('pending','offered')")
            for job_id in pin_notice_jobs(store.query, rows, kinds=(notices.RETIRED_EVENT,)).values():
                reasons.setdefault(job_id, "retention-outcome")
            return reasons
        patch.setattr(retention, "_pin_reasons", pin_outcomes)
    elif name == "g":
        patch.setattr(notices, "expire_outcomes", lambda *args, **kwargs: [])
    elif name == "h":
        if "written > oldest" not in inspect.getsource(notices.fresh):
            raise RuntimeError("mutant h requires the strict production cutoff")

        def inclusive_cutoff(created_at, oldest):
            if oldest is None:
                return True
            written = notices.written_at(created_at)
            return written is None or written >= oldest
        patch.setattr(notices, "fresh", inclusive_cutoff)
    elif name == "i":
        original = notices.outcome_text

        def first_header_only(first_lines, *args, **kwargs):
            return original(first_lines[:1], *args, **kwargs)
        patch.setattr(notices, "outcome_text", first_header_only)
    elif name in ("j", "k", "l", "o"):
        source = inspect.getsource(notices.expire_outcomes)
        replacements = {
            "j": [("written < oldest", "written <= oldest")],
            "k": [("AND created_at=? AND text=? ", "AND text=? "),
                  ('row["session_id"], row["created_at"], row["text"]', 'row["session_id"], row["text"]')],
            "l": [("AND text=? ", ""), ('row["created_at"], row["text"]', 'row["created_at"]')],
            "o": [("match_outcome_text=True", "match_outcome_text=False")],
        }
        for old, new in replacements[name]:
            if source.count(old) != 1:
                raise RuntimeError(f"mutant {name} requires exactly one {old!r}")
            source = source.replace(old, new)
        namespace = dict(notices.__dict__)
        exec(compile(source, f"<notice-keep-mutant-{name}>", "exec"), namespace)
        patch.setattr(notices, "expire_outcomes", namespace["expire_outcomes"])
    elif name == "m":
        from contextlib import contextmanager
        from subfleet import retention_snapshot
        from subfleet.store import Store

        @contextmanager
        def old_lookup(database):
            with Store(database, read_only=True) as store:
                yield store, "sqlite-coordination"
        patch.setattr(retention_snapshot, "lookup_store", old_lookup)
    elif name == "n":
        original = Path.read_text

        def missing_pr(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path.resolve() == ROOT / "docs/acceptance-contract.md":
                text = text.replace("D-NOTICE-KEEP1, #294;", "D-NOTICE-KEEP1;")
            return text
        patch.setattr(Path, "read_text", missing_pr)
    else:
        raise ValueError(f"unknown notice-keep mutant: {name}")
    return patch


PLUGIN = '''"""Temporary phase-recording plugin for tools.notice_keep_mutants."""
import json
import os
from pathlib import Path
import pytest
from tools.notice_keep_mutants import install_mutant

phases = []
collection_errors = []
patch = None

def pytest_addoption(parser):
    parser.addoption("--notice-keep-mutant", default=None)

def pytest_configure(config):
    global patch
    name = config.getoption("--notice-keep-mutant")
    if name:
        patch = install_mutant(name)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    phases.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
                   "exception": call.excinfo.typename if call.excinfo else None,
                   "assertion": bool(call.excinfo and call.excinfo.errisinstance(AssertionError)),
                   "failure": str(report.longrepr) if report.failed else None})

def pytest_collectreport(report):
    if report.failed:
        collection_errors.append(str(report.longrepr))

def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["NOTICE_KEEP_PHASES"]).write_text(json.dumps(
        {"exitstatus": int(exitstatus), "phases": phases, "collection_errors": collection_errors},
        indent=2) + "\\n")

def pytest_unconfigure(config):
    if patch is not None:
        patch.undo()
'''


def hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def run_case(label: str, nodes: list[str], *, mutant: str | None, output: Path,
             plugin: Path, tmpdir: Path) -> dict[str, Any]:
    # mkdtemp reserves a unique parent; pytest receives its absent child.
    scratch = Path(tempfile.mkdtemp(prefix=f"sf-notice-keep-{label}-", dir=tmpdir))
    phase_path = output / f"{label}-phases.json"
    log_path = output / f"{label}.log"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "notice_keep_pytest_plugin",
               "--timeout=540", f"--basetemp={scratch / 'basetemp'}", *nodes]
    if mutant:
        command.append(f"--notice-keep-mutant={mutant}")
    environment = os.environ.copy()
    environment.update({"SUBFLEET_LIVE": "0", "TMPDIR": str(tmpdir),
                        "UV_CACHE_DIR": str(tmpdir / "uv-cache"), "NOTICE_KEEP_PHASES": str(phase_path),
                        "PYTHONPATH": os.pathsep.join(filter(None, [str(plugin), str(ROOT),
                                                                   environment.get("PYTHONPATH")]))})
    before = hashes()
    started = datetime.now(UTC).isoformat()
    tick = time.monotonic()
    with log_path.open("w") as log:
        result = subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
    receipt = json.loads(phase_path.read_text()) if phase_path.exists() else {}
    phases = receipt.get("phases", [])
    failures = [phase for phase in phases if phase["outcome"] == "failed"]
    calls = [phase for phase in phases if phase["when"] == "call"]
    assertion_kill = (result.returncode == 1 and bool(failures) and bool(calls)
                      and not receipt.get("collection_errors")
                      and all(phase["when"] == "call" and phase["assertion"] for phase in failures))
    unchanged = before == hashes()
    if mutant:
        status = "killed" if assertion_kill else "survived" if result.returncode == 0 else "invalid"
    else:
        status = ("passed" if result.returncode == 0 and bool(calls)
                  and all(phase["outcome"] == "passed" for phase in calls) else "invalid")
    if not unchanged:
        status = "invalid"
    record = {"label": label, "mutant": mutant, "status": status, "exit_code": result.returncode,
              "started": started, "seconds": round(time.monotonic() - tick, 3), "command": command,
              "calls": {outcome: sum(phase["outcome"] == outcome for phase in calls)
                        for outcome in ("passed", "failed", "skipped")},
              "setup_or_teardown_failures": sum(phase["when"] != "call" for phase in failures),
              "assertion_failures": sum(phase["assertion"] for phase in failures),
              "collection_errors": receipt.get("collection_errors", []),
              "source_unchanged": unchanged, "source_sha256": before,
              "log": str(log_path), "phases": str(phase_path), "basetemp": str(scratch / "basetemp")}
    (output / f"{label}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"{label}: {status}; rc={result.returncode}; calls={record['calls']}; {record['seconds']}s", flush=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, help="fresh directory for logs and JSON receipts")
    parser.add_argument("--mutants", nargs="+", choices=tuple(MUTANTS), default=list(MUTANTS))
    parser.add_argument("--no-controls", action="store_true", help="use separately recorded green controls")
    args = parser.parse_args()
    if not os.environ.get("TMPDIR"):
        parser.error("set TMPDIR to a writable temporary directory before running")
    tmpdir = Path(os.environ["TMPDIR"]).resolve(strict=True)
    if args.output_dir:
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            parser.error("--output-dir must be empty so earlier evidence is preserved")
    else:
        output = Path(tempfile.mkdtemp(prefix="sf-notice-keep-mutants-", dir=tmpdir))
    plugin = output / "plugin"
    plugin.mkdir()
    (plugin / "notice_keep_pytest_plugin.py").write_text(PLUGIN)
    print(f"Evidence: {output}", flush=True)
    runs = []
    if not args.no_controls:
        nodes = list(dict.fromkeys(MUTANTS[name][1] for name in args.mutants))
        runs.append(run_case("controls", nodes, mutant=None, output=output, plugin=plugin, tmpdir=tmpdir))
    for name in args.mutants:
        runs.append(run_case(f"mutant-{name}", [MUTANTS[name][1]], mutant=name, output=output,
                             plugin=plugin, tmpdir=tmpdir))
    summary = {"mutants": {name: {"description": MUTANTS[name][0], "killer": MUTANTS[name][1]}
                           for name in args.mutants}, "runs": runs,
               "killed": sum(run["status"] == "killed" for run in runs),
               "survived": sum(run["status"] == "survived" for run in runs),
               "invalid": sum(run["status"] == "invalid" for run in runs)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["killed"] == len(args.mutants) and not summary["invalid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
