"""Mutants of D-WALL-RETRY1, each to be killed by named tests.

    python tools/wall_retry_mutants.py --scratch <dir containing /sf-> [--only NAME ...]

HEAD is exported once with `git archive` under the scratch directory (never
this checkout). Each mutant edits that export alone; its named tests run there,
then the edited file is restored from the export's pristine bytes and checked
byte for byte and by SHA-256. A kill is a failed test (an assertion or an
exception in the test's body): a fixture error, a skip or a pass is not. The
unmutated control must pass every named test first. Prints one JSON row per
mutant with each named test's outcome; exits 0 only when the control passes and
every mutant is killed. The export is removed afterwards.

Mutants 1-7 are the build brief's; 8-11 are review r1's (finding 2's two
loosened bounds, an exempt transient attempt counted again, and the finishing
attempt left out of the overrun sum).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ElementTree

ROOT = Path(__file__).resolve().parents[1]
DAEMON = "subfleet/daemon.py"
CONTRACTS = "subfleet/contracts.py"
UNIT = "tests/unit/test_wall_retry_properties.py::"
FAKE = "tests/fake/test_wall_retry.py::"
EXCLUSION = UNIT + "test_wall_retry_exclusion_matches_reference"
LIVE = UNIT + "test_wall_retry_live_attempt_keeps_its_own_time"
TICK = UNIT + "test_wall_retry_tick_projection_agrees_with_exact_sum"
BOUND = UNIT + "test_wall_retry_provider_time_is_bounded_by_attempts_times_cap"
ALL_TRANSIENT = FAKE + "test_a_job_whose_every_attempt_ends_transient_is_bounded"
OVERRUN = FAKE + "test_transient_overrun_counts_its_own_time_after_classification"

PY_RULE = 'and row["outcome_class"] in WALL_EXEMPT_OUTCOME_CLASSES and row["killed_by"] is None'
SQL_RULE = '''             "('reserved','starting','running','finalizing') AND NOT ("
             f"p.state IN ({','.join(repr(s) for s in _WALL_EXEMPT_STATES)}) AND "
             f"COALESCE(p.outcome_class IN ({','.join(repr(c) for c in WALL_EXEMPT_OUTCOME_CLASSES)}),0) "
             "AND p.killed_by IS NULL)),0.0) AS prior_running_s "'''
FINISHING = 'self._running_s(job["job_id"], finishing_attempt_id=a["attempt_id"])'

# name -> (file, [(old, new)], each old occurring once; the named tests)
MUTATIONS = {
    "1-python-no-exclusion": (DAEMON, [(PY_RULE, "and False " + PY_RULE)], [EXCLUSION]),
    "2-sql-no-exclusion": (DAEMON, [(SQL_RULE, '''             "('reserved','starting','running','finalizing')),0.0) AS prior_running_s "''')],
                           [TICK]),
    "3-sql-plain-not-in": (DAEMON, [(SQL_RULE, '''             "('reserved','starting','running','finalizing') AND ("
             "p.state='quarantined' OR p.killed_by IS NOT NULL OR "
             f"p.outcome_class NOT IN ({','.join(repr(c) for c in WALL_EXEMPT_OUTCOME_CLASSES)}))),0.0) AS prior_running_s "''')],
                           [TICK]),
    "4-live-interim-class-excluded": (DAEMON, [('row["state"] in _WALL_EXEMPT_STATES\n',
                                                'row["state"] != "quarantined"\n')], [LIVE]),
    "5-stopped-excluded": (DAEMON, [('and row["killed_by"] is None):', "and True):")], [EXCLUSION]),
    "6-lost-and-unknown-excluded": (DAEMON, [(PY_RULE, 'and (row["outcome_class"] in WALL_EXEMPT_OUTCOME_CLASSES '
                                                       'or row["outcome_class"] == "unknown" or row["state"] == "lost") '
                                                       'and row["killed_by"] is None')], [EXCLUSION]),
    "7-finishing-exempt-attempt-omitted": (DAEMON, [(FINISHING, 'self._running_s(job["job_id"])')], [OVERRUN]),
    # Finding 2: the wall stops counting a transient attempt's running time
    # wherever it appears (its own overrun comparison and a stopped transient too).
    "8-transient-time-uncounted": (DAEMON, [('            if row["state"] == "reserved":\n                continue\n',
                                             '            if row["state"] == "reserved" or row["outcome_class"] == "transient":\n'
                                             '                continue\n')],
                                   [ALL_TRANSIENT, BOUND]),
    # Finding 2: admission no longer asks `max_attempts`.
    "9-max-attempts-dropped": (DAEMON, [('        return a["seq"] - self._uncharged(conn, job["job_id"], before=a["seq"]) '
                                         '< job["max_attempts"]\n', "        return True\n")],
                               [ALL_TRANSIENT, BOUND]),
    # An exempt transient attempt counted again.
    "10-exempt-transient-counted": (CONTRACTS, [("    OutcomeClass.LIMITED.value, OutcomeClass.TRANSIENT.value, "
                                                 "OutcomeClass.AUTH_DEAD.value,\n",
                                                 "    OutcomeClass.LIMITED.value, OutcomeClass.AUTH_DEAD.value,\n")],
                                    [ALL_TRANSIENT, EXCLUSION]),
    # The finishing attempt left out of the overrun sum, whatever its class.
    "11-finishing-attempt-left-out": (DAEMON, [('            if (row["attempt_id"] != finishing_attempt_id and ',
                                                '            if row["attempt_id"] == finishing_attempt_id:\n'
                                                '                continue\n'
                                                '            if (row["attempt_id"] != finishing_attempt_id and ')],
                                      [ALL_TRANSIENT, BOUND, OVERRUN]),
}

#: What the named tests need of HEAD.
EXPORTED = ["subfleet", "tests", "pyproject.toml"]


def export(destination: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD", *EXPORTED],
                             check=True, capture_output=True).stdout
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="tar")


def mutate(tree: Path, name: str) -> None:
    file, edits, _ = MUTATIONS[name]
    path = tree / file
    text = path.read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, (name, old[:60], text.count(old))
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    for cached in (tree / "subfleet" / "__pycache__").glob(f"{Path(file).stem}.*.pyc"):
        cached.unlink()


def pytest(tree: Path, tests: list[str], scratch: Path, label: str) -> tuple[dict[str, str], list[str]]:
    """{file::test name: passed|failed|error|skipped} from one pytest run in ``tree``, and its
    one-line failure reasons; a parametrized test's cases fold into its name (failed if any failed)."""
    report = scratch / f"{label}.xml"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tree)  # Import the export, never an installed or editable copy.
    env["TMPDIR"] = str(scratch)
    probe = subprocess.run([sys.executable, "-c", "import subfleet; print(subfleet.__file__)"],
                           cwd=tree, env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert Path(probe).resolve().is_relative_to(tree.resolve()), probe
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line",
                          f"--junitxml={report}", f"--basetemp={scratch / ('pytest-' + label)}", *tests],
                         cwd=tree, env=env, capture_output=True, text=True)
    (scratch / f"{label}.log").write_text(run.stdout + run.stderr)
    outcomes: dict[str, str] = {}
    rank = ["passed", "skipped", "error", "failed"]
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case}
        outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        name = case.get("classname").replace(".", "/") + ".py::" + case.get("name").split("[", 1)[0]
        outcomes[name] = max(outcomes.get(name, "passed"), outcome, key=rank.index)
    prefixes = sorted({str(tree) + "/", str(tree.resolve()) + "/"}, key=len, reverse=True)
    reasons = []
    for line in run.stdout.splitlines():
        if ".py:" in line and "Error" in line:
            for prefix in prefixes:
                line = line.replace(prefix, "")
            reasons.append(line[:240])
    return outcomes, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=list(MUTATIONS))
    parser.add_argument("--no-control", action="store_true", help="skip the control (it ran in an earlier call)")
    args = parser.parse_args()
    if "/sf-" not in str(args.scratch.resolve()):
        parser.error("--scratch must be a /sf- scratch directory")
    scratch = Path(tempfile.mkdtemp(prefix="sf-mutants-", dir=args.scratch))
    tree = scratch / "export"
    checkout = {file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in (DAEMON, CONTRACTS)}
    ok = True
    try:
        export(tree)
        pristine = {file: (tree / file).read_bytes() for file in (DAEMON, CONTRACTS)}
        if not args.no_control:
            named = sorted({test for name in args.only for test in MUTATIONS[name][2]})
            outcome, _ = pytest(tree, named, scratch, "control")
            print(json.dumps({"mutant": "control", "outcomes": outcome}), flush=True)
            ok = all(outcome.get(test) == "passed" for test in named)
        for name in args.only:
            file, _, tests = MUTATIONS[name]
            try:
                mutate(tree, name)
                outcome, reasons = pytest(tree, tests, scratch, name)
            finally:
                (tree / file).write_bytes(pristine[file])
            restored = (tree / file).read_bytes()
            assert restored == pristine[file], name
            killed_by = [test for test in tests if outcome.get(test) == "failed"]
            ok = ok and bool(killed_by)
            print(json.dumps({"mutant": name, "file": file, "killed": bool(killed_by), "killed_by": killed_by,
                              "outcomes": outcome, "reasons": reasons,
                              "restored_sha256": hashlib.sha256(restored).hexdigest()}), flush=True)
        # The checkout itself was never written.
        assert checkout == {file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in checkout}
    finally:
        if "/sf-" in str(scratch):
            shutil.rmtree(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
