"""Retry at most five explicitly known flakes, using complete JUnit outcomes."""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_RETRIES = 5


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
    """xunit1 keeps the file path, so class/parameter IDs can be reconstructed.

    Pytest may emit separate testcases for a call failure and a teardown error.
    Merge all of them: a later skip or pass must never erase a failure.
    """
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuites", "testsuite"}:
        raise ValueError("not a JUnit report")
    outcomes: dict[str, set[str]] = {}
    for case in root.iter("testcase"):
        file = case.get("file", "")
        classname = case.get("classname", "")
        name = case.get("name", "")
        module = file.removesuffix(".py").replace("/", ".")
        if (not file.endswith(".py") or not name
                or not (classname == module or classname.startswith(module + "."))):
            raise ValueError(f"cannot reconstruct pytest node id: {case.attrib}")
        classes = classname[len(module):].lstrip(".").split(".") if classname != module else []
        node = "::".join([file, *classes, name])
        statuses = {child.tag for child in case if child.tag in {"failure", "error", "skipped"}}
        outcomes.setdefault(node, set()).update(statuses or {"passed"})
    if not outcomes:
        raise ValueError("JUnit report contains no tests")
    return outcomes


def run_pytest(report: Path, nodes: list[str]) -> int:
    report.unlink(missing_ok=True)
    return subprocess.run([
        sys.executable, "-m", "pytest", "-q", "-o", "junit_family=xunit1",
        "--junit-prefix=", f"--junitxml={report}", *nodes,
    ]).returncode


def run(allowlist: Path, reports: Path) -> int:
    defects = read_allowlist(allowlist)
    reports.mkdir(parents=True, exist_ok=True)
    first_report, retry_report = reports / "first.xml", reports / "retry.xml"
    retry_report.unlink(missing_ok=True)
    first = run_pytest(first_report, [])
    if first not in (0, 1):
        return first
    outcomes = read_outcomes(first_report)
    failed = sorted(node for node, statuses in outcomes.items() if statuses & {"failure", "error"})
    if first == 0:
        if failed:
            annotation("error", "Pytest exited successfully but JUnit contains failures")
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
        annotation("error", "Every requested retry must be present and passed in JUnit (including teardown)")
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
    except (OSError, ValueError, ET.ParseError) as error:
        annotation("error", f"Cannot verify known-flake retry: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
