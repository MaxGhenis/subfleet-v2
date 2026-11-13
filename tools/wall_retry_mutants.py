"""Run D-WALL-RETRY1's seven mutants, restoring byte-for-byte after each.

Run under the dev environment with TMPDIR pointing to permitted test scratch.
Only this checkout's daemon.py is mutated; each subprocess uses its own pytest
basetemp. The durable JSON/text results go next to the wall-retry report.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "subfleet/daemon.py"
RESULTS = ROOT / "docs/reports/2026-11-07-wall-retry-data"
UNIT = "tests/unit/test_wall_retry_properties.py::"
FAKE = "tests/fake/test_wall_retry.py::"


def replace_once(source: str, before: str, after: str) -> str:
    assert source.count(before) == 1, before
    return source.replace(before, after, 1)


def main() -> int:
    RESULTS.mkdir(exist_ok=True)
    pristine = SOURCE.read_bytes()
    original = pristine.decode()
    # Keep a durable recovery copy before applying even the first mutant.
    backup = RESULTS / "daemon.py.pristine"
    backup.write_bytes(pristine)
    sql_exclusion = '''             "('reserved','starting','running','finalizing') AND NOT ("
             f"p.state IN ({','.join(repr(s) for s in _WALL_EXEMPT_STATES)}) AND "
             f"COALESCE(p.outcome_class IN ({','.join(repr(c) for c in WALL_EXEMPT_OUTCOME_CLASSES)}),0) "
             "AND p.killed_by IS NULL)),0.0) AS prior_running_s "'''
    mutations = [
        ("python_no_exclusion", UNIT + "test_wall_retry_exclusion_matches_reference",
         'and row["outcome_class"] in WALL_EXEMPT_OUTCOME_CLASSES and row["killed_by"] is None',
         'and False and row["outcome_class"] in WALL_EXEMPT_OUTCOME_CLASSES and row["killed_by"] is None'),
        ("sql_no_exclusion", UNIT + "test_wall_retry_tick_projection_agrees_with_exact_sum",
         sql_exclusion, '''             "('reserved','starting','running','finalizing')),0.0) AS prior_running_s "'''),
        ("sql_plain_not_in", UNIT + "test_wall_retry_tick_projection_agrees_with_exact_sum",
         sql_exclusion, '''             "('reserved','starting','running','finalizing') AND ("
             "p.state='quarantined' OR p.killed_by IS NOT NULL OR "
             f"p.outcome_class NOT IN ({','.join(repr(c) for c in WALL_EXEMPT_OUTCOME_CLASSES)}))),0.0) AS prior_running_s "'''),
        ("live_interim_excluded", UNIT + "test_wall_retry_live_attempt_keeps_its_own_time",
         'row["state"] in _WALL_EXEMPT_STATES\n', 'row["state"] != "quarantined"\n'),
        ("stopped_excluded", UNIT + "test_wall_retry_exclusion_matches_reference",
         'and row["killed_by"] is None):', 'and True):'),
        ("lost_unknown_excluded", UNIT + "test_wall_retry_exclusion_matches_reference",
         'and row["outcome_class"] in WALL_EXEMPT_OUTCOME_CLASSES and row["killed_by"] is None',
         'and (row["outcome_class"] in WALL_EXEMPT_OUTCOME_CLASSES or row["outcome_class"] == "unknown" '
         'or row["state"] == "lost") and row["killed_by"] is None'),
        ("finishing_time_excluded", FAKE + "test_transient_overrun_counts_its_own_time_after_classification",
         'self._running_s(job["job_id"], finishing_attempt_id=a["attempt_id"])',
         'self._running_s(job["job_id"])'),
    ]
    results = []
    try:
        for index, (name, test, before, after) in enumerate(mutations, 1):
            assert SOURCE.read_bytes() == pristine
            changed = replace_once(original, before, after)
            SOURCE.write_text(changed)
            # Invalidate timestamp-based bytecode even for same-size edits.
            for cached in (ROOT / "subfleet/__pycache__").glob("daemon.*.pyc"):
                cached.unlink()
            command = [sys.executable, "-m", "pytest", "-q", test, "--tb=short",
                       "--basetemp", str(Path(os.environ["TMPDIR"]) / f"mutant-{index:02d}")]
            try:
                try:
                    run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, timeout=600)
                    output, exit_code = run.stdout, run.returncode
                except subprocess.TimeoutExpired as error:
                    captured = error.stdout or b""
                    output = captured.decode(errors="replace") if isinstance(captured, bytes) else captured
                    output += "\nMutant test exceeded its 600-second limit.\n"
                    exit_code = 124
            finally:
                SOURCE.write_bytes(pristine)
                assert SOURCE.read_bytes() == pristine
                for cached in (ROOT / "subfleet/__pycache__").glob("daemon.*.pyc"):
                    cached.unlink()
            (RESULTS / f"mutant-{index:02d}.log").write_text(output)
            result = {"mutant": name, "test": test, "command": command, "exit_code": exit_code,
                      "killed": exit_code == 1 and "FAILED " in output,
                      "restored_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest()}
            results.append(result)
            (RESULTS / "mutants.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(result), flush=True)
    finally:
        SOURCE.write_bytes(pristine)
        assert SOURCE.read_bytes() == pristine
    backup.unlink()
    return 0 if all(item["killed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
