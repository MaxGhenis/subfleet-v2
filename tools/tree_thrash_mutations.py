"""Kill D-TREE-THRASH1 regressions in an expendable ``git archive`` copy.

Run this script with the project's Python from the archive root. It refuses a
checkout (including a worktree's .git file), applies each exact mutation alone,
runs its named regression with a fresh pytest temporary directory, and restores
the source. Only pytest exit 1 with that selected regression named as FAILED and its
intended assertion in the traceback counts as a kill. Git and process
timeouts are infrastructure failures. Healthy controls share one collection.

    /path/to/project/.venv/bin/python tools/tree_thrash_mutations.py
    /path/to/project/.venv/bin/python tools/tree_thrash_mutations.py --list

TMPDIR must be the Darwin user temporary directory, as for the main suite.
Use ``--temp-root`` for a writable child when its parent is sandboxed; every
test's TMPDIR, pytest basetemp and Hypothesis database stay beneath that child.
``--output`` saves machine-readable timings, the intended failing assertions,
and full failing-run logs. Healthy controls batch only the selected mutants'
regressions into one collection. On a loaded host an archive-only repository
fixture overlay is preserved in
``docs/reports/2026-11-06-tree-thrash-fixture-template.patch``; apply that patch
inside the archive and set ``SUBFLEET_TREE_THRASH_REPO_TEMPLATE`` to a clean,
real committed fixture repository to copy it afresh for each test. The
normal checkout fixture is unchanged. The JSON records that fixture's hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


DAEMON = Path("subfleet/daemon.py")
TESTS = "tests/fake/test_admission_tree_thrash.py::"
WAITER = TESTS + "test_a_fresh_waiter_runs_no_git_and_writes_no_tree[no-slot]"
HEAD = TESTS + "test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head"
RACE = TESTS + "test_the_one_cut_whose_lane_closes_between_evaluation_and_reservation_is_given_back"
FAILURE = TESTS + "test_a_failing_workspace_reaches_the_job_when_capacity_first_allows_placement[False]"
DESKTOP = TESTS + "test_a_closure_race_with_a_desktop_lane_never_refreshes_the_registry_under_the_store_lock"
REOPEN = TESTS + "test_reopening_the_original_lane_during_preparation_reuses_its_original_evaluation"

DEFER = "            deferred_workspace = fresh_workspace\n"
PREPARE_GATE = (
    "                        if deferred_workspace:\n"
    "                            # All placement checks passed, including FIFO, leases,\n"
)
PREPARE = (
    "                except _WorkspaceNeeded:\n"
    "                    try:\n"
    "                        workspace, head, baseline, skipped = self._workspace(job)\n"
)
FAILED = (
    "                except _WorkspaceNeeded:\n"
    "                    try:\n"
    "                        workspace, head, baseline, skipped = self._workspace(job)\n"
    "                        pinned = self._pin_baseline(job, previous, workspace, head, baseline, skipped)\n"
    "                    except AdapterError as exc:\n"
    "                        self._fail_queued(job, self._refused_job(job[\"job_id\"], exc), rc=exc.code)\n"
)
RACE_HOLD = "                            if fresh_workspace and not deferred_workspace and not decision.chosen_lane:\n"
READY = "                    deferred_workspace = False\n"

# name -> (the new rule removed, its replacement, selected regression).
MUTATIONS = {
    "eager-waiter-tree": (DEFER, "            deferred_workspace = False\n", WAITER),
    "reserve-without-tree": (
        PREPARE_GATE, PREPARE_GATE.replace("if deferred_workspace:", "if False and deferred_workspace:"), HEAD),
    "lose-prepared-baseline": (
        PREPARE, PREPARE + "                        baseline = None\n", HEAD),
    "forget-racing-giveback": (RACE_HOLD, RACE_HOLD.replace("if fresh_workspace", "if False and fresh_workspace"), DESKTOP),
    "swallow-workspace-refusal": (
        FAILED, FAILED.replace(
            "                        self._fail_queued(job, self._refused_job(job[\"job_id\"], exc), rc=exc.code)\n",
            "                        pass  # mutant: the workspace failure never reaches the job\n"), FAILURE),
    "evaluate-again-after-git": (
        READY, READY + "                    basis = {}\n"
        "                    decision = self._route(decision_job, extra_exclusions=route_exclusions,\n"
        "                                           desktop=desktop_account, basis=basis)\n", HEAD),
    "refresh-registry-under-store-lock": (
        "self._pin_view(desktop_account, refresh=False), decision_job, far_s=0)",
        "self._pin_view(desktop_account, refresh=True), decision_job, far_s=0)", DESKTOP),
    "reuse-standing-decision-with-old-basis": ("                    decision = evaluated_decision\n", "", REOPEN),
}


# Match the failing source line or explicit assertion message, rather than a
# test body that failed while submitting its fixture through an overloaded Git.
KILL_EVIDENCE = {
    "eager-waiter-tree": "assert not cuts and not calls and not service.store.list_attempts(job)",
    "reserve-without-tree": "assert cuts == [job] and evaluations == [job]",
    "lose-prepared-baseline": 'assert attempt["baseline_tree"] ==',
    "forget-racing-giveback": 'assert service._holds[job].get("unprepared")',
    "swallow-workspace-refusal": 'assert (row["state"], row["rc"]) == ("failed", 7)',
    "evaluate-again-after-git": "assert cuts == [job] and evaluations == [job]",
    "refresh-registry-under-store-lock": "registry refresh under the store write lock",
    "reuse-standing-decision-with-old-basis": 'assert cuts == [job] and attempt["lane_id"] == "codex-1"',
}


def pytest(tests: list[str], *, timeout: float, temp_root: Path) -> dict:
    """Infrastructure failures cannot masquerade as a killed mutant."""
    with tempfile.TemporaryDirectory(prefix="tree-thrash-mutant-", dir=temp_root) as directory:
        env = {**os.environ, "TMPDIR": directory, "PYTHONDONTWRITEBYTECODE": "1", "HYPOTHESIS_PROFILE": "ci",
               "HYPOTHESIS_STORAGE_DIRECTORY": str(Path(directory) / "hypothesis")}
        command = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                   "--basetemp", str(Path(directory) / "pytest"), *tests]
        started = time.monotonic()
        try:
            run = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)
            output = run.stdout + run.stderr
            exit_code, timed_out = run.returncode, False
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            output = decoded(exc.stdout) + decoded(exc.stderr)
            exit_code, timed_out = 124, True
        failed = [match.group(1) for match in re.finditer(r"^FAILED (\S+)", output, re.M)]
        evidence = [line for line in output.splitlines()
                    if line.startswith((">", "E ", "FAILED "))]
        infrastructure = timed_out or any(re.search(
            r"TimeoutExpired|CalledProcessError|inspect-workdir|timed out after [0-9.]+ seconds|fatal:", line)
            for line in evidence if line.startswith(("E ", "FAILED ")))
        return {"test": tests[0] if len(tests) == 1 else "baseline-controls", "tests": tests,
                "exit_code": exit_code, "seconds": round(time.monotonic() - started, 3),
                "failed": failed, "selected_failed": any(test in failed for test in tests),
                "timed_out": timed_out, "infrastructure_error": infrastructure,
                "failure_evidence": evidence, "output": output}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", choices=[*MUTATIONS, "baseline"])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--control-timeout", type=float, help="timeout for the batched healthy controls")
    parser.add_argument("--temp-root", type=Path,
                        default=Path(os.environ["TMPDIR"]) if os.environ.get("TMPDIR") else Path(tempfile.gettempdir()),
                        help="writable directory under the Darwin user temporary directory")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.list:
        for name, (_, _, test) in MUTATIONS.items():
            print(f"{name}\t{test}")
        return 0
    if os.path.lexists(".git"):
        parser.error("run only inside an expendable git archive copy; this directory is a checkout")
    if not DAEMON.is_file():
        parser.error("run from the archive root containing subfleet/daemon.py")
    if "tmp" in args.temp_root.resolve().parts:
        parser.error("--temp-root must be under Darwin user temp, with no literal /tmp path component")
    args.temp_root.mkdir(parents=True, exist_ok=True)
    original = DAEMON.read_text()
    results = {"source_sha256": hashlib.sha256(original.encode()).hexdigest(),
               "tests_sha256": hashlib.sha256(Path(TESTS.split("::")[0]).read_bytes()).hexdigest(),
               "temp_root": str(args.temp_root.resolve()),
               "repository_template": os.environ.get("SUBFLEET_TREE_THRASH_REPO_TEMPLATE"),
               "fixture_sha256": hashlib.sha256(Path("tests/fake/test_unrun_worktrees.py").read_bytes()).hexdigest(),
               "runs": []}
    names = args.names or ["baseline", *MUTATIONS]
    exit_code = 0
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + "\n")
    try:
        for name in names:
            if name == "baseline":
                # A healthy control is necessary before interpreting a failure.
                selected = [mutation for mutation in names if mutation != "baseline"] or list(MUTATIONS)
                tests = list(dict.fromkeys(MUTATIONS[mutation][2] for mutation in selected))
                old = new = ""
            else:
                old, new, test = MUTATIONS[name]
                tests = [test]
                if original.count(old) != 1:
                    raise RuntimeError(f"{name}: anchor occurs {original.count(old)} times, expected one")
            DAEMON.write_text(original.replace(old, new, 1) if old else original)
            try:
                print(f"RUN\t{name}\t{' '.join(tests)}", flush=True)
                timeout = args.control_timeout if name == "baseline" and args.control_timeout else args.timeout
                result = pytest(tests, timeout=timeout, temp_root=args.temp_root)
                result["mutation"] = name
                expected = KILL_EVIDENCE.get(name)
                intended_failure = bool(expected and any(expected in line for line in result["failure_evidence"]))
                result["intended_failure"] = intended_failure
                result["killed"] = (name != "baseline" and result["exit_code"] == 1
                                    and result["selected_failed"] and intended_failure
                                    and not result["infrastructure_error"])
                results["runs"].append({key: value for key, value in result.items() if key != "output"})
                if args.output:
                    args.output.write_text(json.dumps(results, indent=2) + "\n")
                    if result["exit_code"]:
                        args.output.with_name(args.output.stem + f"-{name}.log").write_text(result["output"])
                infrastructure = (result["infrastructure_error"] or result["exit_code"] not in (0, 1)
                                  or (result["exit_code"] == 1 and not result["selected_failed"]))
                if infrastructure or (name == "baseline" and result["exit_code"]):
                    print(result["output"], file=sys.stderr)
                    raise RuntimeError(f"{name}: baseline or infrastructure failure, exit {result['exit_code']}")
                if name != "baseline" and result["exit_code"] and not intended_failure:
                    print(result["output"], file=sys.stderr)
                    raise RuntimeError(f"{name}: failed outside its intended assertion")
                status = "PASS" if name == "baseline" else "KILLED" if result["killed"] else "SURVIVED"
                print(f"{status}\t{name}\t{result['seconds']:.3f}s\t{result['test']}", flush=True)
                if name != "baseline" and not result["killed"]:
                    exit_code = 1
            finally:
                DAEMON.write_text(original)
    except KeyboardInterrupt:
        results["error"] = "interrupted by operator"
        exit_code = 130
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        results["error"] = str(exc)
        exit_code = 2
    finally:
        DAEMON.write_text(original)
        if args.output:
            args.output.write_text(json.dumps(results, indent=2) + "\n")
    killed = sum(run["killed"] for run in results["runs"])
    attempted = sum(run["mutation"] != "baseline" for run in results["runs"])
    print(f"{killed}/{attempted} mutants killed", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
