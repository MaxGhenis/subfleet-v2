"""Run round-3 lookup mutants independently against copied source checkouts.

Every selected killer first passes against pristine production bytes. A kill
requires pytest exit 1 and a call-phase AssertionError, with no collection,
setup, or teardown failures. Import provenance, source diffs, complete phase
receipts, logs and pristine-byte restoration are recorded for every run.
The assigned worktree's production source is never modified.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "subfleet/retention_notices.py"
PAGES = "tests/unit/test_retention_outcome_pages.py::"
OUTCOMES = "tests/unit/test_retention_outcomes.py::"
MUTANTS = {
    "no-page-limit": ("remove the page limit, reading the ledger in one statement",
                      PAGES + "test_lookup_pages_never_exceed_1000_events"),
    "no-captured-upper-id": ("read to the changing table end instead of a captured upper event id",
                             PAGES + "test_events_arriving_between_pages_stay_past_captured_upper_id"),
    "deadline-returns-partial": ("return accumulated events as successful history after a deadline",
                                 PAGES + "test_overall_deadline_never_returns_pages_read_so_far"),
    "limit-one": ("r3: restrict the lookup statement to LIMIT 1",
                  OUTCOMES + "test_outcomes_cli_has_json_and_readable_offline_output"),
}

PLUGIN = '''import json
import os
from pathlib import Path
import pytest

phases = []
collection_errors = []
imported_sources = {}

@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    import subfleet.store
    import subfleet.retention_notices
    import tests.failed_keep
    root = Path.cwd().resolve()
    # Keep the shared test harness's pruning and failure artifacts in scratch.
    tests.failed_keep.ROOT = Path(os.environ["NOTICE_KEEP_FAILED_ROOT"])
    for module in (subfleet.store, subfleet.retention_notices):
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(root), f"source escaped temporary checkout: {path}"
        imported_sources[module.__name__] = str(path)

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
        {"exitstatus": int(exitstatus), "phases": phases, "collection_errors": collection_errors,
         "imported_sources": imported_sources}, indent=2) + "\\n")
'''


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"expected one mutation anchor {old!r}; found {source.count(old)}")
    return source.replace(old, new)


def mutated_source(name: str, source: str) -> str:
    """Apply precisely one named behavior fault; reject changed source anchors."""
    parameters = "(RETIRED_EVENT, last, upper, OUTCOME_PAGE_SIZE), deadline)"
    if name == "no-page-limit":
        source = replace_once(source, "ORDER BY event_id LIMIT ?", "ORDER BY event_id")
        return replace_once(source, parameters, "(RETIRED_EVENT, last, upper), deadline)")
    if name == "no-captured-upper-id":
        start = source.index('    upper = _outcome_query(store, "SELECT COALESCE(MAX(event_id), 0) AS upper_event_id "')
        end = source.index("    last = 0\n", start)
        source = source[:start] + source[end:]
        source = replace_once(source, "    while last < upper:\n", "    while True:\n")
        source = replace_once(source, "AND event_id>? AND event_id<=? ORDER BY event_id LIMIT ?",
                              "AND event_id>? ORDER BY event_id LIMIT ?")
        return replace_once(source, parameters, "(RETIRED_EVENT, last, OUTCOME_PAGE_SIZE), deadline)")
    if name == "deadline-returns-partial":
        # Preserve the accumulated events when a paging deadline fires. The
        # private marker lets only that path bypass subsequent deadline checks,
        # reproducing a successful partial-history return without also changing
        # the normal archive-reconstruction deadline behavior.
        anchor = "def _outcome_events(store: Any, session_id: str, deadline: float) -> list[dict[str, Any]]:"
        source = replace_once(source, anchor,
                              "class _MutantPartialEvents(list):\n    pass\n\n\n" + anchor)
        start = source.index("    while last < upper:\n", source.index(anchor))
        end = source.index("    return events\n", start)
        block = source[start:end]
        source = source[:start] + "    try:\n" + "".join("    " + line for line in block.splitlines(keepends=True)) + (
            "    except OutcomesLookupDeadlineError:\n        return _MutantPartialEvents(events)\n") + source[end:]
        return replace_once(source, "            events = _outcome_events(store, session_id, deadline)\n",
                            "            events = _outcome_events(store, session_id, deadline)\n"
                            "            if isinstance(events, _MutantPartialEvents):\n"
                            "                deadline = float('inf')\n")
    if name == "limit-one":
        source = replace_once(source, "ORDER BY event_id LIMIT ?", "ORDER BY event_id LIMIT 1")
        return replace_once(source, parameters, "(RETIRED_EVENT, last, upper), deadline)")
    raise ValueError(name)


def sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def source_hashes() -> dict[str, str]:
    return {name: sha256((ROOT / name).read_bytes()) for name in (
        SOURCE, "tests/unit/test_retention_outcome_pages.py", "tests/unit/test_retention_outcomes.py")}


def copy_checkout(destination: Path) -> None:
    for name in ("subfleet", "tests", "tools"):
        shutil.copytree(ROOT / name, destination / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    shutil.copy2(ROOT / "pyproject.toml", destination / "pyproject.toml")
    (destination / "docs").mkdir()
    shutil.copy2(ROOT / "docs/acceptance-contract.md", destination / "docs/acceptance-contract.md")


def run_case(name: str | None, nodes: list[str], output: Path, temporary: Path,
             plugin: Path, pristine: bytes) -> dict:
    label = name or "controls"
    checkout = temporary / label
    before = source_hashes()
    copy_checkout(checkout)
    target = checkout / SOURCE
    target.write_bytes(pristine)
    pristine_copy = checkout / "pristine-retention-notices.py"
    pristine_copy.write_bytes(pristine)
    phases_path = output / f"{label}-phases.json"
    log_path = output / f"{label}.log"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "notice_keep_r3_phases", "--timeout=90",
               "--override-ini", "pythonpath=.", f"--basetemp={temporary / (label + '-basetemp')}", *nodes]
    child_tmp = temporary / (label + "-tmp")
    child_tmp.mkdir()
    env = os.environ.copy()
    env.update({"SUBFLEET_LIVE": "0", "NOTICE_KEEP_PHASES": str(phases_path),
                "NOTICE_KEEP_FAILED_ROOT": str(temporary / (label + "-failed")), "TMPDIR": str(child_tmp),
                "PYTHONPATH": os.pathsep.join([str(plugin), str(checkout)])})
    started = time.perf_counter()
    try:
        if name:
            original = pristine.decode()
            changed = mutated_source(name, original)
            compile(changed, str(target), "exec")
            target.write_text(changed)
            (output / f"{label}.diff").write_text("".join(difflib.unified_diff(
                original.splitlines(keepends=True), changed.splitlines(keepends=True), fromfile=SOURCE, tofile=SOURCE)))
        mutated_sha256 = sha256(target.read_bytes())
        with log_path.open("w") as log:
            result = subprocess.run(command, cwd=checkout, env=env, stdout=log, stderr=subprocess.STDOUT)
    finally:
        target.write_bytes(pristine_copy.read_bytes())
        restored = target.read_bytes() == pristine and sha256(target.read_bytes()) == sha256(pristine)
        assert restored, f"pristine source was not restored: {target}"
    phases = json.loads(phases_path.read_text()) if phases_path.exists() else {}
    calls = [phase for phase in phases.get("phases", []) if phase["when"] == "call"]
    failures = [phase for phase in phases.get("phases", []) if phase["outcome"] == "failed"]
    assertion_kill = (result.returncode == 1 and bool(failures) and len(calls) == len(nodes)
                      and not phases.get("collection_errors")
                      and all(phase["when"] == "call" and phase["assertion"] for phase in failures))
    status = ("killed" if assertion_kill else "survived" if result.returncode == 0 else "invalid") if name else (
        "passed" if result.returncode == 0 and len(calls) == len(nodes)
        and all(phase["outcome"] == "passed" for phase in calls)
        and not any(phase["outcome"] == "skipped" for phase in phases.get("phases", [])) else "invalid")
    unchanged = before == source_hashes()
    if not unchanged or not restored:
        status = "invalid"
    receipt = {"mutant": name, "status": status, "seconds": time.perf_counter() - started,
               "exit_code": result.returncode, "command": command, "cwd": str(checkout),
               "worktree_source_sha256": before, "worktree_source_unchanged": unchanged,
               "pristine_sha256": sha256(pristine), "mutated_sha256": mutated_sha256,
               "restored_sha256": sha256(target.read_bytes()), "restored_pristine_bytes": restored,
               "calls": {value: sum(phase["outcome"] == value for phase in calls) for value in ("passed", "failed", "skipped")},
               "assertion_failures": sum(phase["assertion"] for phase in failures),
               "setup_or_teardown_failures": sum(phase["when"] != "call" for phase in failures),
               "collection_errors": phases.get("collection_errors", []),
               "imported_sources": phases.get("imported_sources", {}),
               "log": str(log_path), "phases": str(phases_path)}
    (output / f"{label}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"{label}: {status}; calls={receipt['calls']}; {receipt['seconds']:.3f}s", flush=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mutants", nargs="+", choices=tuple(MUTANTS), default=list(MUTANTS))
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("output directory must be in the assigned worktree")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("output directory must be empty")
    plugin = output / "plugin"
    plugin.mkdir()
    (plugin / "notice_keep_r3_phases.py").write_text(PLUGIN)
    pristine = (ROOT / SOURCE).read_bytes()
    temporary = Path(tempfile.mkdtemp(prefix="sf-nk-f3-mutants-", dir=os.environ["TMPDIR"]))
    try:
        nodes = list(dict.fromkeys(MUTANTS[name][1] for name in args.mutants))
        runs = [run_case(None, nodes, output, temporary, plugin, pristine)]
        for name in args.mutants:
            runs.append(run_case(name, [MUTANTS[name][1]], output, temporary, plugin, pristine))
    finally:
        assert "sf-nk-f3" in str(temporary), temporary
        shutil.rmtree(temporary)
    summary = {"measured_at": datetime.now(UTC).isoformat(), "mutants": {
        name: {"description": MUTANTS[name][0], "killer": MUTANTS[name][1]} for name in args.mutants}, "runs": runs,
        "killed": sum(run["status"] == "killed" for run in runs),
        "survived": sum(run["status"] == "survived" for run in runs),
        "invalid": sum(run["status"] == "invalid" for run in runs)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["killed"] == len(args.mutants) and not summary["invalid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
