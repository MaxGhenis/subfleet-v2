"""Run the Fix-r2 admission mutants, and Fix r1's six again, against isolated source copies.

Run through ``uv run --no-sync --with 'pytest-xdist>=3.6' python``. Every mutant
starts from the same pristine copy of this checkout. After pytest, its edited
`subfleet/daemon.py` is restored from the pristine bytes and compared. A
collection or setup error is never a kill: a selected regression must fail an
assertion, and each mutant's controls (named with ``--control``) must pass.
Fix r1's mutants and anchors come from `tools/idle_admission_r1_mutations.py`
unchanged; only their scratch root and output names are this round's.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
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
_SPEC = importlib.util.spec_from_file_location("idle_admission_r1_mutations",
                                               REPO / "tools/idle_admission_r1_mutations.py")
R1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(R1)
exact, copy_source, digest = R1.exact, R1.copy_source, R1.digest

CARRIED = "tests/fake/test_idle_capacity_carried_clock.py"
SELF_WAKE = "tests/fake/test_idle_admission_self_wake.py"
SOURCES = "tests/fake/test_idle_capacity_news_sources.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    change: Callable[[str], str]
    tests: tuple[str, ...]          # each must fail an assertion
    controls: tuple[str, ...] = ()  # each must pass under the mutant


def run_pytest(source: Path, scratch: Path, name: str, tests: tuple[str, ...], output: Path) -> dict:
    scratch.mkdir()
    junit = scratch / "junit.xml"
    env = {**os.environ, "PYTHONPATH": str(source), "TMPDIR": str(scratch),
           "HYPOTHESIS_STORAGE_DIRECTORY": str(scratch / "hypothesis")}
    argv = [sys.executable, "-m", "pytest", "-q", "-n", "4", "--tb=short", "-p", "no:cacheprovider",
            f"--basetemp={scratch / 'pytest'}", f"--junitxml={junit}", *tests]
    started = time.monotonic()
    result = subprocess.run(argv, cwd=source, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=480)
    log = output / f"r2-mutation-{name}.log"
    log.write_text(result.stdout)
    cases = list(ET.parse(junit).getroot().iter("testcase")) if junit.exists() else []

    def label(case):
        return case.get("classname", "") + "::" + case.get("name", "")

    failures = [{"test": label(case),
                 "assertion": "AssertionError" in (case.find("failure").text or "")
                 or "assert " in (case.find("failure").text or "")}
                for case in cases if case.find("failure") is not None]
    return {"name": name, "command": argv, "cwd": str(source),
            "returncode": result.returncode, "tests_run": len(cases),
            "passed_tests": [label(case) for case in cases if case.find("failure") is None
                             and case.find("error") is None and case.find("skipped") is None],
            "failures": failures, "errors": [label(case) for case in cases if case.find("error") is not None],
            "elapsed_s": round(time.monotonic() - started, 3), "log": str(log)}


def mutations() -> tuple[Mutation, ...]:
    keep = exact("        if not carried or carried <= ceiling:\n            return False, carried\n",
                 "        if True:  # mutant: keep the carried clock\n            return False, carried\n")
    probe_only = exact(
        "                        taken, hold[\"next_check_at\"] = self._clip_carried_clock(\n"
        "                            job, current[\"next_check_at\"], holds, \"probe-pending\")\n",
        "                        taken, hold[\"next_check_at\"] = False, current[\"next_check_at\"]  # mutant\n")
    moved_only = exact(
        "                if self._clip_carried_clock(job, job[\"next_check_at\"], holds, \"route-moved\")[0]:\n",
        "                if False:  # mutant: route-moved keeps the carried clock\n")
    guard = exact("        if source == \"jobs\" and getattr(self._admission_thread, \"active\", False):\n"
                  "            return\n", "")
    lanes = exact("        return bool(previous and (previous[0] - closures or previous[1] != lanes\n",
                  "        return bool(previous and (previous[0] - closures\n")
    additions = exact("            if source in (\"closure\", \"lane\"):\n",
                      "            if source == \"lane\":  # mutant: no closure write is capacity news\n")
    return (
        Mutation("keep-carried-event-only-clock", keep,
                 (CARRIED + "::test_probe_pending_look_brought_forward_keeps_at_most_30_seconds[long]",
                  CARRIED + "::test_route_moved_look_brought_forward_keeps_at_most_30_seconds[long]"),
                 (CARRIED + "::test_probe_pending_look_brought_forward_keeps_at_most_30_seconds[short]",
                  CARRIED + "::test_route_moved_look_brought_forward_keeps_at_most_30_seconds[short]")),
        Mutation("keep-carried-clock-on-probe-pending-only", probe_only,
                 (CARRIED + "::test_probe_pending_look_brought_forward_keeps_at_most_30_seconds[long]",),
                 (CARRIED + "::test_probe_pending_look_brought_forward_keeps_at_most_30_seconds[short]",
                  CARRIED + "::test_route_moved_look_brought_forward_keeps_at_most_30_seconds[long]")),
        Mutation("keep-carried-clock-on-route-moved-only", moved_only,
                 (CARRIED + "::test_route_moved_look_brought_forward_keeps_at_most_30_seconds[long]",),
                 (CARRIED + "::test_route_moved_look_brought_forward_keeps_at_most_30_seconds[short]",
                  CARRIED + "::test_probe_pending_look_brought_forward_keeps_at_most_30_seconds[long]")),
        Mutation("drop-self-wake-guard", guard,
                 (SELF_WAKE + "::test_a_real_pass_requeuing_its_own_job_still_rests[workspace-ready]",
                  SELF_WAKE + "::test_only_the_pass_threads_own_queue_transition_is_ignored[own-thread-requeue]"),
                 (SELF_WAKE + "::test_a_real_pass_requeuing_its_own_job_still_rests[none]",
                  SELF_WAKE + "::test_only_the_pass_threads_own_queue_transition_is_ignored[other-thread-requeue]",
                  SELF_WAKE + "::test_only_the_pass_threads_own_queue_transition_is_ignored[own-thread-lease]",
                  SELF_WAKE + "::test_only_the_pass_threads_own_queue_transition_is_ignored"
                              "[own-thread-requeue-after-pass]")),
        Mutation("drop-lane-census-comparison", lanes,
                 tuple(SOURCES + "::test_a_lane_change_from_another_connection_reaches_admission_only_through_"
                       f"the_census[{change}]" for change in ("enrolled", "disabled", "removed")),
                 (SOURCES + "::test_a_lane_change_from_another_connection_reaches_admission_only_through_"
                            "the_census[none]",)),
        Mutation("closure-additions-bring-nothing-forward", additions,
                 tuple(SOURCES + f"::test_c6_10_a_closure_added_brings_every_eligible_wait_forward[{added}]"
                       for added in ("provider-limit-on-the-held-lane", "operator-hold-on-another-lane")),
                 ()),
    )


def r1_mutations() -> tuple[Mutation, ...]:
    return tuple(Mutation("r1-" + each.name, each.change, each.tests) for each in R1.mutations())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--only", action="append")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--report", default="fix-r2-mutations.json",
                        help="report file name in --output (one per invocation under a tool time limit)")
    args = parser.parse_args()
    selected = (*mutations(), *r1_mutations())
    if args.only and set(args.only) - {mutation.name for mutation in selected}:
        parser.error("unknown mutation name")
    if args.only:
        selected = tuple(mutation for mutation in selected if mutation.name in args.only)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scratch_root = args.scratch_root.resolve()
    assert "sf-ia-f2" in str(scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True)
    report = {"baseline": [], "mutations": [], "sources_sha256": {}}

    def save():
        (output / args.report).write_text(json.dumps(report, indent=2) + "\n")

    scratch = Path(tempfile.mkdtemp(prefix="run-", dir=scratch_root))
    try:
        pristine = scratch / "pristine"
        copy_source(pristine)
        daemon = pristine / "subfleet/daemon.py"
        assert digest(daemon) == digest(REPO / "subfleet/daemon.py")
        # Every anchor applies exactly once to this head before anything runs.
        for mutation in selected:
            mutation.change(daemon.read_text())
        baselines = tuple(dict.fromkeys(test.split("::")[0] for mutation in selected
                                        for test in (*mutation.tests, *mutation.controls)))
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
            assert original == daemon.read_bytes()
            result = None
            try:
                path.write_text(mutation.change(original.decode()))
                mutated_hash = digest(path)
                assert path.read_bytes() != original
                result = run_pytest(source, scratch / (mutation.name + "-test"), mutation.name,
                                    (*mutation.tests, *mutation.controls), output)
                result["mutated_file_sha256"] = mutated_hash
            finally:
                path.write_bytes(original)
                assert path.read_bytes() == daemon.read_bytes()
            result["restored_bytes_match"] = True
            def selects(selector: str, case: str) -> bool:
                # An unparametrized selector (Fix r1's) names every one of its cases.
                return case == selector or case.startswith(selector + "[")

            failed = [row["test"].split("::")[-1] for row in result["failures"] if row["assertion"]]
            passed = [test.split("::")[-1] for test in result["passed_tests"]]
            killers = [test.split("::")[-1] for test in mutation.tests]
            controls = [test.split("::")[-1] for test in mutation.controls]
            result["killed_by"] = [case for case in failed if any(selects(test, case) for test in killers)]
            result["controls_passed"] = [test for test in controls if test in passed]
            result["killed"] = (not result["errors"] and bool(result["killed_by"])
                                and len(result["controls_passed"]) == len(controls))
            report["mutations"].append(result)
            save()
            print(f"{mutation.name}: {'KILLED' if result['killed'] else 'SURVIVED/ERROR'} "
                  f"by {result['killed_by']}; controls passed {len(result['controls_passed'])}/{len(controls)}",
                  flush=True)
        assert all(digest(pristine / relative) == expected
                   for relative, expected in report["sources_sha256"].items())
        report["pristine_bytes_unchanged"] = True
        save()
    finally:
        assert "sf-ia-f2" in str(scratch)
        shutil.rmtree(scratch)
    return 0 if all(result["killed"] for result in report["mutations"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
