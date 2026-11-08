"""D-ADMIT-IDLE1: kill the seven requested mutants in temporary source copies.

Run with the checkout's synced Python environment. The source checkout is only
read; each pytest invocation sees its own copied package, tests and base temp.
JUnit failures identify the tests that kill each mutant. A collection error,
timeout, missing anchor or other infrastructure failure never counts as a kill.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable
import xml.etree.ElementTree as ET


REPO = Path(__file__).resolve().parents[1]
PACING = "tests/fake/test_idle_admission_pacing.py"
CAPACITY = "tests/fake/test_idle_capacity_events.py"


def exact(before: str, after: str) -> Callable[[str], str]:
    def replace(source: str) -> str:
        if source.count(before) != 1:
            raise ValueError(f"expected one mutation anchor, found {source.count(before)}: {before!r}")
        return source.replace(before, after, 1)
    return replace


def omit_released_closure_news(source: str) -> str:
    method = re.search(r"^    def _capacity_change_news\(.*?(?=^    def |\Z)", source, re.M | re.S)
    if method is None:
        raise ValueError("missing _capacity_change_news method")
    changed, count = re.subn(r"\b\w+\[0\]\s*-\s*(?:\w+\[0\]|closures)\b", "frozenset()", method.group())
    if count != 1:
        raise ValueError(f"expected one removed-closure comparison, found {count}")
    # The revision also catches an operator change that came and went between
    # two censuses. Removing only the row delta leaves that second path working.
    changed, count = re.subn(r"\s+or previous\[3\] != revision", "", changed)
    if count != 1:
        raise ValueError(f"expected one closure/lane revision comparison, found {count}")
    return source[:method.start()] + changed + source[method.end():]


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    change: Callable[[str], str]
    tests: tuple[str, ...]


MUTATIONS = (
    Mutation("no-rest", "subfleet/daemon.py", exact(
        'if key == "admission" and not self._admission_ready():',
        'if False and key == "admission" and not self._admission_ready():'),
        (PACING + "::test_idle_pass_rests_from_its_end_for_at_least_four_durations",)),
    Mutation("no-submit-wake", "subfleet/store.py", exact(
        '("jobs_insert", "INSERT", "jobs", "NEW.state IN (\'queued\',\'waiting\')", "jobs")',
        '("jobs_insert", "INSERT", "jobs", "0", "jobs")'),
        (PACING + "::test_each_semantic_wake_starts_detached_on_the_next_tick[submit]",)),
    Mutation("no-released-closure-wake", "subfleet/store.py", exact(
        '"source_event", "released_at"', '"source_event"'),
        (PACING + "::test_each_semantic_wake_starts_detached_on_the_next_tick[closure-release]",)),
    Mutation("600-seconds-for-every-verdict", "subfleet/scheduler.py", exact(
        '    value = _row(decision)\n    evaluations = value.get("evaluations") or ()\n',
        '    return CAPACITY_RECHECK_EVENT_CEILING_S\n'
        '    value = _row(decision)\n    evaluations = value.get("evaluations") or ()\n'),
        (CAPACITY + "::test_c6_10_reading_or_time_verdicts_keep_the_30_second_ceiling",)),
    Mutation("no-released-closure-bring-forward", "subfleet/daemon.py", omit_released_closure_news,
        (CAPACITY + "::test_c6_10_capacity_events_bring_every_eligible_wait_forward[released]",)),
    Mutation("bring-forward-every-pass", "subfleet/daemon.py", exact(
        'hurried = bool(freed and known and known["expedite"]) or turn_came or unblocked',
        'hurried = bool(known and known["expedite"]) or turn_came or unblocked'),
        (CAPACITY + "::test_c6_10_no_change_never_brings_waits_forward",)),
    Mutation("unknown-reason-event-only", "subfleet/scheduler.py", exact(
        '    if not reason.startswith("closed:"):\n        return False\n',
        '    if not reason.startswith("closed:"):\n        return True\n'),
        (CAPACITY + "::test_c6_10_every_rejection_reason_has_a_conservative_classification[future-rejection-False]",)),
)


def copy_source(target: Path) -> None:
    target.mkdir()
    for directory in ("subfleet", "tests"):
        shutil.copytree(REPO / directory, target / directory,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".hypothesis", ".git"))
    shutil.copy2(REPO / "pyproject.toml", target / "pyproject.toml")


def run_pytest(source: Path, scratch: Path, name: str, tests: tuple[str, ...], output: Path) -> dict:
    junit = scratch / "junit.xml"
    base = scratch / "pytest"
    env = {**os.environ, "PYTHONPATH": str(source), "TMPDIR": str(scratch),
           "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch / "hypothesis")}
    argv = [sys.executable, "-m", "pytest", "-q", "-x", "--tb=short", "-p", "no:cacheprovider",
            f"--basetemp={base}", f"--junitxml={junit}", *tests]
    started = time.monotonic()
    result = subprocess.run(argv, cwd=source, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=240)
    log = output / f"mutation-{name}.log"
    log.write_text(result.stdout)
    cases = list(ET.parse(junit).getroot().iter("testcase")) if junit.exists() else []
    failed = [case.get("classname", "") + "::" + case.get("name", "")
              for case in cases if case.find("failure") is not None]
    errors = [case.get("name", "") for case in cases if case.find("error") is not None]
    return {"name": name, "returncode": result.returncode, "tests_run": len(cases),
            "failed_tests": failed, "errors": errors, "elapsed_s": round(time.monotonic() - started, 3),
            "killed": result.returncode == 1 and bool(failed) and not errors,
            "log": str(log.relative_to(REPO)) if log.is_relative_to(REPO) else str(log),
            "command": argv}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=REPO / "docs/reports/2026-11-07-idle-admission-data")
    parser.add_argument("--scratch-root", type=Path, default=Path("/private/tmp/idle-admission-mutations"))
    parser.add_argument("--only", choices=[mutation.name for mutation in MUTATIONS], action="append")
    parser.add_argument("--skip-baseline", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    report = {"baseline": None, "mutations": [], "sources_sha256": {}}

    def save():
        (output / "mutations.json").write_text(json.dumps(report, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="run-", dir=args.scratch_root) as directory:
        scratch = Path(directory)
        pristine = scratch / "pristine"
        copy_source(pristine)
        for relative in ("subfleet/daemon.py", "subfleet/store.py", "subfleet/scheduler.py",
                         "subfleet/contracts.py", PACING, CAPACITY):
            report["sources_sha256"][relative] = hashlib.sha256((pristine / relative).read_bytes()).hexdigest()
        if not args.skip_baseline:
            base_scratch = scratch / "baseline"
            base_scratch.mkdir()
            report["baseline"] = run_pytest(pristine, base_scratch, "baseline", (PACING, CAPACITY), output)
            save()
            if report["baseline"]["returncode"] != 0:
                print("Baseline failed; no mutations applied.", flush=True)
                return 2
        for mutation in MUTATIONS:
            if args.only and mutation.name not in args.only:
                continue
            source = scratch / mutation.name
            shutil.copytree(pristine, source, ignore=shutil.ignore_patterns("__pycache__"))
            path = source / mutation.file
            path.write_text(mutation.change(path.read_text()))
            run_scratch = scratch / (mutation.name + "-test")
            run_scratch.mkdir()
            result = run_pytest(source, run_scratch, mutation.name, mutation.tests, output)
            result["mutated_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            report["mutations"].append(result)
            save()
            print(f"{mutation.name}: {'KILLED' if result['killed'] else 'SURVIVED/ERROR'} "
                  f"{', '.join(result['failed_tests'])}", flush=True)
    return 0 if all(result["killed"] for result in report["mutations"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
