"""Run the six Fix-r1 admission mutants against isolated source copies.

Run through ``uv run --no-sync --with 'pytest-xdist>=3.6' python``. Every mutant
starts from the same pristine bytes. After pytest, its edited file
is restored from those bytes and checked. A collection error or setup error is
never a kill: the selected regression must fail an assertion.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
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
POOLS = "tests/fake/test_idle_capacity_pools.py"
GUARDS = "tests/fake/test_idle_capacity_guard_news.py"
ROUTES = "tests/fake/test_idle_route_deferrals.py"


def exact(before: str, after: str) -> Callable[[str], str]:
    def replace(source: str) -> str:
        count = source.count(before)
        if count != 1:
            raise ValueError(f"expected one mutation anchor, found {count}: {before!r}")
        return source.replace(before, after, 1)
    return replace


@dataclass(frozen=True)
class Mutation:
    name: str
    change: Callable[[str], str]
    tests: tuple[str, ...]


def copy_source(target: Path) -> None:
    target.mkdir()
    for directory in ("subfleet", "tests"):
        shutil.copytree(REPO / directory, target / directory,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".hypothesis", ".git"))
    shutil.copy2(REPO / "pyproject.toml", target / "pyproject.toml")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pytest(source: Path, scratch: Path, name: str, tests: tuple[str, ...], output: Path) -> dict:
    scratch.mkdir()
    junit = scratch / "junit.xml"
    env = {**os.environ, "PYTHONPATH": str(source), "TMPDIR": str(scratch),
           "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch / "hypothesis")}
    argv = [sys.executable, "-m", "pytest", "-q", "-n", "4", "--tb=short", "-p", "no:cacheprovider",
            f"--basetemp={scratch / 'pytest'}", f"--junitxml={junit}", *tests]
    started = time.monotonic()
    result = subprocess.run(argv, cwd=source, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=600)
    log = output / f"r1-mutation-{name}.log"
    log.write_text(result.stdout)
    cases = list(ET.parse(junit).getroot().iter("testcase")) if junit.exists() else []
    failures = [{"test": case.get("classname", "") + "::" + case.get("name", ""),
                 "assertion": "AssertionError" in (case.find("failure").text or "")
                 or "assert " in (case.find("failure").text or "")}
                for case in cases if case.find("failure") is not None]
    errors = [case.get("name", "") for case in cases if case.find("error") is not None]
    return {"name": name, "command": argv, "cwd": str(source),
            "environment": {key: env[key] for key in ("PYTHONPATH", "TMPDIR", "HYPOTHESIS_STORAGE_DIRECTORY")},
            "returncode": result.returncode, "tests_run": len(cases),
            "passed": sum(case.find("failure") is None and case.find("error") is None
                          and case.find("skipped") is None for case in cases),
            "failures": failures, "errors": errors,
            "killed": result.returncode == 1 and any(row["assertion"] for row in failures) and not errors,
            "elapsed_s": round(time.monotonic() - started, 3),
            "log": str(log.relative_to(REPO)) if log.is_relative_to(REPO) else str(log)}


def mutations() -> tuple[Mutation, ...]:
    # Anchors are deliberately exact: a source drift requires reviewing the
    # mutant rather than accidentally editing a different success/clock path.
    def read_expiries_after_pass(source: str) -> str:
        source = exact("        started_at = utcnow()\n", "")(source)
        return exact("            # Include a closure that expired during this pass, even the first\n",
                     "            started_at = utcnow()\n"
                     "            # Include a closure that expired during this pass, even the first\n")(source)

    def acknowledge_before_waiter_checks(source: str) -> str:
        return exact('                if freed:\n                    self._capacity_news[job["job_id"]] = True\n',
                     "                if freed:\n                    pass  # mutant: the pool census acknowledges the news\n")(source)

    def discard_skipped_waiter_news(source: str) -> str:
        return exact('            saved_news = self._capacity_news.pop(job["job_id"], False)\n',
                     '            self._capacity_news.pop(job["job_id"], None)  # mutant: guard cleared\n'
                     '            saved_news = self._capacity_news.pop(job["job_id"], False)\n')(source)

    def retain_prior_route_errors(source: str) -> str:
        return exact("                    # C-6.12: a completed evaluation ends the run of route errors.\n"
                     '                    self._route_deferrals.pop(job["job_id"], None)\n',
                     "                    # mutant: successful unprepared routes retain prior errors\n")(source)

    return (
        Mutation("acknowledge-news-before-waiter-checks", acknowledge_before_waiter_checks,
                 (GUARDS + "::test_release_news_survives_temporary_door_hold",)),
        Mutation("retain-route-error-history", retain_prior_route_errors,
                 (ROUTES + "::test_successful_route_resets_prior_error_count[True]",
                  ROUTES + "::test_intervening_success_should_not_delay_start_after_release[True]")),
        Mutation("discard-skipped-waiter-news", discard_skipped_waiter_news,
                 (GUARDS + "::test_release_news_survives_temporary_door_hold[long-machine]",)),
        Mutation("read-expiries-after-pass", read_expiries_after_pass,
                 (PACING + "::test_closure_expiring_during_the_first_pass_is_not_lost",
                  PACING + "::test_closure_expiring_during_a_slow_pass_survives_its_completion")),
        Mutation("share-detached-turn-census", exact(
            "        # Capture before the snapshot: a condition added during this pass may\n",
            '        kind = "detached"\n'
            "        # Capture before the snapshot: a condition added during this pass may\n"),
                 (POOLS + "::test_capacity_news_censuses_are_independent_for_each_pool",)),
        Mutation("drop-unprepared-known-reset-clipping", exact(
            "                    next_check = scheduler.waiting_metadata(\n"
            "                        waiting_decision, rechecks=rechecks,\n"
            "                        ceiling_s=scheduler.capacity_recheck_ceiling(waiting_decision),\n"
            '                        capacity_steer=routed_job.get("capacity_steer"))["next_check_at"]',
            "                    next_check = after(scheduler.capacity_recheck_delay(\n"
            "                        rechecks, scheduler.capacity_recheck_ceiling(waiting_decision)))"),
                 (CAPACITY + "::test_c6_10_known_operator_reset_still_beats_the_long_clock[True]",)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=REPO / "docs/reports/2026-11-07-idle-admission-data")
    parser.add_argument("--scratch-root", type=Path, default=Path("/private/tmp/sf-ia-f1/mutations"))
    parser.add_argument("--only", action="append")
    parser.add_argument("--skip-baseline", action="store_true")
    args = parser.parse_args()
    selected = mutations()
    if args.only and set(args.only) - {mutation.name for mutation in selected}:
        parser.error("unknown mutation name")
    if args.only:
        selected = tuple(mutation for mutation in selected if mutation.name in args.only)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scratch_root = args.scratch_root.resolve()
    assert "sf-ia-f1" in scratch_root.parts
    scratch_root.mkdir(parents=True, exist_ok=True)
    report = {"baseline": [], "mutations": [], "sources_sha256": {}}

    def save():
        (output / "fix-r1-mutations.json").write_text(json.dumps(report, indent=2) + "\n")

    scratch = Path(tempfile.mkdtemp(prefix="run-", dir=scratch_root))
    try:
        pristine = scratch / "pristine"
        copy_source(pristine)
        baselines = tuple(dict.fromkeys(test.split("::")[0]
                                       for mutation in selected for test in mutation.tests))
        tracked = ("subfleet/daemon.py", "subfleet/store.py", "subfleet/scheduler.py",
                   "subfleet/contracts.py", *baselines)
        report["sources_sha256"] = {relative: digest(pristine / relative) for relative in tracked}
        if not args.skip_baseline:
            for index, test_file in enumerate(baselines):
                result = run_pytest(pristine, scratch / f"baseline-{index}",
                                    f"baseline-{Path(test_file).stem}", (test_file,), output)
                report["baseline"].append(result)
                save()
                print(f"baseline {test_file}: exit={result['returncode']} tests={result['tests_run']}", flush=True)
                if result["returncode"] != 0:
                    return 2
        for mutation in selected:
            source = scratch / mutation.name
            shutil.copytree(pristine, source, ignore=shutil.ignore_patterns("__pycache__"))
            path = source / "subfleet/daemon.py"
            original = path.read_bytes()
            result = None
            try:
                path.write_text(mutation.change(original.decode()))
                mutated_hash = digest(path)
                assert path.read_bytes() != original
                result = run_pytest(source, scratch / (mutation.name + "-test"),
                                    mutation.name, mutation.tests, output)
                result["mutated_file_sha256"] = mutated_hash
            finally:
                path.write_bytes(original)
                assert path.read_bytes() == original
            result["restored_bytes_match"] = True
            result["restored_file_sha256"] = digest(path)
            report["mutations"].append(result)
            save()
            print(f"{mutation.name}: {'KILLED' if result['killed'] else 'SURVIVED/ERROR'} "
                  f"{', '.join(row['test'] for row in result['failures'])}", flush=True)
        assert all(digest(pristine / relative) == expected
                   for relative, expected in report["sources_sha256"].items())
        report["pristine_bytes_unchanged"] = True
        save()
    finally:
        assert "sf-ia-f1" in scratch.parts
        shutil.rmtree(scratch)
    return 0 if all(result["killed"] for result in report["mutations"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
