"""Apply round-2 notice mutants to independent temporary source copies.

Each mutant has a green control and must fail by a call-phase assertion.
Collection errors, crashes, fixture errors, and timeout failures are invalid
evidence. Production files are hashed before and after every run. Receipts
include the exact source diff, pytest phases, command, log and counts.
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
TEST_IDENTITY = "tests/unit/test_pin_notice_identity.py::"
TEST_LOOKUP = "tests/unit/test_retention_outcomes.py::"
MUTANTS = {
    "three-field-retirement": ("restore retirement association using only id, session and time", TEST_IDENTITY + "test_retirement_association_requires_all_four_fields[text]"),
    "eager-hash": ("hash every service notice row", TEST_IDENTITY + "test_retirement_hashes_only_matching_rows_and_each_at_most_once"),
    "missing-retirement-digest": ("let a retirement event without a digest name a row", TEST_IDENTITY + "test_retirement_event_without_digest_names_nothing"),
    "pin-requires-digest": ("require a text digest on job.pin_noticed", TEST_IDENTITY + "test_pin_noticed_associations_match_the_previous_key_function"),
    "lookup-copies-database": ("reintroduce a private database copy in the lookup", TEST_LOOKUP + "test_outcomes_allocates_no_temporary_database_copy"),
    "lookup-writable": ("open the lookup's Store writable", TEST_LOOKUP + "test_outcomes_opens_store_read_only_and_sqlite_rejects_writes"),
}

PLUGIN = '''import json
import os
from pathlib import Path
import pytest

phases = []
collection_errors = []
imported_sources = {}

def pytest_sessionstart(session):
    import subfleet.store
    import subfleet.retention_notices
    root = Path.cwd().resolve()
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
        raise RuntimeError(f"expected one mutation anchor {old!r}, found {source.count(old)}")
    return source.replace(old, new)


def mutated_source(name: str, source: str) -> str:
    """Return actual changed source; mutation anchors fail closed on drift."""
    if name == "lookup-writable":
        return replace_once(source, "with Store(database, read_only=True) as store:", "with Store(database) as store:")
    if name == "lookup-copies-database":
        original = ('    with Store(database, read_only=True) as store:\n'
                    '        events = store.query("SELECT event_id, ts, job_id, data_json FROM events WHERE kind=? ORDER BY event_id",\n'
                    '                             (RETIRED_EVENT,))')
        return replace_once(source, original,
                            "    import shutil\n    import tempfile\n"
                            "    with tempfile.TemporaryDirectory(prefix='mutant-outcomes-copy-') as scratch:\n"
                            "        copied = Path(scratch) / 'state.sqlite3'\n"
                            "        shutil.copyfile(database, copied)\n"
                            "        with Store(copied, read_only=True) as store:\n"
                            '            events = store.query("SELECT event_id, ts, job_id, data_json FROM events WHERE kind=? ORDER BY event_id",\n'
                            '                                 (RETIRED_EVENT,))')
    if name == "three-field-retirement":
        return replace_once(source, "        if retired:\n", "        if retired and False:\n")
    if name == "eager-hash":
        return replace_once(source, "    digests: dict[tuple, str] = {}",
                            '    digests: dict[tuple, str] = {key: hashlib.sha256(str(row["text"]).encode("utf-8", "surrogatepass")).hexdigest()\n'
                            '                                      for key, row in wanted.items()}')
    if name == "missing-retirement-digest":
        return replace_once(source, '            if not key[3]:\n                continue',
                            '            if not key[3]:\n                found[row["notice_id"]] = event["job_id"]\n                continue')
    if name == "pin-requires-digest":
        return replace_once(source, '        retired = event["kind"] == "retention.notice_retired"',
                            '        retired = True')
    raise ValueError(name)


def source_hashes() -> dict[str, str]:
    files = ["subfleet/store.py", "subfleet/retention_notices.py", "tests/unit/test_pin_notice_identity.py",
             "tests/unit/test_retention_outcomes.py"]
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def copy_checkout(destination: Path) -> None:
    for name in ("subfleet", "tests", "tools"):
        shutil.copytree(ROOT / name, destination / name,
                        symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    shutil.copy2(ROOT / "pyproject.toml", destination / "pyproject.toml")
    (destination / "docs").mkdir()
    shutil.copy2(ROOT / "docs/acceptance-contract.md", destination / "docs/acceptance-contract.md")


def run_case(name: str | None, nodes: list[str], output: Path, temporary: Path, plugin: Path) -> dict:
    label = name or "controls"
    checkout = temporary / label
    before = source_hashes()
    copy_checkout(checkout)
    if name:
        relative = "subfleet/retention_notices.py" if name.startswith("lookup-") else "subfleet/store.py"
        target = checkout / relative
        original = target.read_text()
        changed = mutated_source(name, original)
        compile(changed, str(target), "exec")
        target.write_text(changed)
        (output / f"{label}.diff").write_text("".join(difflib.unified_diff(
            original.splitlines(keepends=True), changed.splitlines(keepends=True), fromfile=relative, tofile=relative)))
    phases_path = output / f"{label}-phases.json"
    log_path = output / f"{label}.log"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "notice_keep_r2_phases", "--timeout=90",
               f"--basetemp={temporary / (label + '-basetemp')}", *nodes]
    env = os.environ.copy()
    env.update({"SUBFLEET_LIVE": "0", "NOTICE_KEEP_PHASES": str(phases_path),
                "PYTHONPATH": os.pathsep.join([str(plugin), str(checkout)])})
    started = time.perf_counter()
    with log_path.open("w") as log:
        result = subprocess.run(command, cwd=checkout, env=env, stdout=log, stderr=subprocess.STDOUT)
    phases = json.loads(phases_path.read_text()) if phases_path.exists() else {}
    calls = [phase for phase in phases.get("phases", []) if phase["when"] == "call"]
    failures = [phase for phase in phases.get("phases", []) if phase["outcome"] == "failed"]
    assertion_kill = (result.returncode == 1 and bool(failures) and bool(calls)
                      and not phases.get("collection_errors")
                      and all(phase["when"] == "call" and phase["assertion"] for phase in failures))
    status = ("killed" if assertion_kill else "survived" if result.returncode == 0 else "invalid") if name else (
        "passed" if result.returncode == 0 and len(calls) == len(nodes)
        and all(phase["outcome"] == "passed" for phase in calls)
        and not any(phase["outcome"] == "skipped" for phase in phases.get("phases", [])) else "invalid")
    unchanged = before == source_hashes()
    if not unchanged:
        status = "invalid"
    receipt = {"mutant": name, "status": status, "seconds": time.perf_counter() - started,
               "exit_code": result.returncode, "command": command, "cwd": str(checkout),
               "source_sha256": before, "source_unchanged": unchanged,
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
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("output directory must be empty")
    plugin = output / "plugin"
    plugin.mkdir()
    (plugin / "notice_keep_r2_phases.py").write_text(PLUGIN)
    with tempfile.TemporaryDirectory(prefix="notice-keep-r2-mutants-", dir=os.environ["TMPDIR"]) as scratch:
        temporary = Path(scratch)
        nodes = list(dict.fromkeys(MUTANTS[name][1] for name in args.mutants))
        runs = [run_case(None, nodes, output, temporary, plugin)]
        for name in args.mutants:
            runs.append(run_case(name, [MUTANTS[name][1]], output, temporary, plugin))
    summary = {"measured_at": datetime.now(UTC).isoformat(), "mutants": {
        name: {"description": MUTANTS[name][0], "killer": MUTANTS[name][1]} for name in args.mutants}, "runs": runs,
        "killed": sum(run["status"] == "killed" for run in runs),
        "survived": sum(run["status"] == "survived" for run in runs),
        "invalid": sum(run["status"] == "invalid" for run in runs)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["killed"] == len(args.mutants) and not summary["invalid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
