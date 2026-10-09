"""Retry at most five explicitly known flakes, using pytest's own node IDs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_RETRIES = 5
OUTCOMES_ENV = "SUBFLEET_PYTEST_OUTCOMES"


def annotation(kind: str, message: str, *, title: str = "") -> None:
    # Workflow commands must not interpret parameter IDs as additional commands.
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{kind}{' title=' + title if title else ''}::{message}", flush=True)


def read_allowlist(path: Path) -> dict[str, str]:
    defects = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        node, separator, comment = line.partition("  # ")
        defect, colon, reason = comment.partition(":")
        if (not separator or "::" not in node or node.startswith("-")
                or not defect.startswith("D-") or not colon or not reason.strip()
                or node in defects):
            raise ValueError(f"{path}:{number}: expected unique node id  # D-id: reason")
        defects[node] = defect
    return defects


def read_outcomes(path: Path) -> dict[str, set[str]]:
    """Merge phase reports without changing node IDs or erasing failures."""
    outcomes: dict[str, set[str]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        report = json.loads(line)
        if (not isinstance(report, dict) or not isinstance(report.get("nodeid"), str)
                or report.get("when") not in ("setup", "call", "teardown", "collect")
                or report.get("outcome") not in ("passed", "failed", "skipped")
                or not isinstance(report.get("xfail"), bool)):
            raise ValueError(f"{path}:{number}: invalid pytest phase report")
        statuses = outcomes.setdefault(report["nodeid"], set())
        if report["outcome"] != "passed" or report["when"] == "call":
            statuses.add(report["outcome"])
        if report["xfail"]:
            statuses.add("xfailed")
    if not outcomes:
        raise ValueError("pytest outcome report contains no tests")
    return outcomes


def run_pytest(report: Path, nodes: list[str]) -> int:
    report.unlink(missing_ok=True)
    env = os.environ.copy()
    env[OUTCOMES_ENV] = str(report.resolve())
    plugin_dir = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = plugin_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run([
        sys.executable, "-m", "pytest", "-q", "-p", "ci_outcomes_plugin", *nodes,
    ], env=env).returncode


def run(allowlist: Path, reports: Path) -> int:
    defects = read_allowlist(allowlist)
    reports.mkdir(parents=True, exist_ok=True)
    first_report, retry_report = reports / "first.jsonl", reports / "retry.jsonl"
    retry_report.unlink(missing_ok=True)
    first = run_pytest(first_report, [])
    if first not in (0, 1):
        return first
    outcomes = read_outcomes(first_report)
    failed = sorted(node for node, statuses in outcomes.items() if "failed" in statuses)
    if first == 0:
        if failed:
            annotation("error", "Pytest exited successfully but recorded outcomes contain failures")
            return 1
        return 0
    if not 1 <= len(failed) <= MAX_RETRIES:
        annotation("error", f"{len(failed)} failed tests; only 1 to {MAX_RETRIES} known flakes may retry")
        return 1
    unknown = [node for node in failed if node not in defects]
    if unknown:
        annotation("error", "Failure outside the known-flake allowlist: " + ", ".join(unknown))
        return 1

    print("::group::Retrying known flakes once", flush=True)
    try:
        second = run_pytest(retry_report, failed)
    finally:
        print("::endgroup::", flush=True)
    if second != 0:
        return second
    retried = read_outcomes(retry_report)
    if set(retried) != set(failed) or any(statuses != {"passed"} for statuses in retried.values()):
        annotation("error", "Every requested retry must be present and passed in pytest outcomes (including teardown)")
        return 1
    for node in failed:
        annotation("warning", f"{node} ({defects[node]})", title="Known flake passed on retry")
    annotation("warning", f"{len(failed)} known flake(s) passed on retry; see the per-test annotations")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, default=Path(".github/known-flaky.txt"))
    parser.add_argument("--reports-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args.allowlist, args.reports_dir)
    except (OSError, ValueError) as error:
        annotation("error", f"Cannot verify known-flake retry: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
