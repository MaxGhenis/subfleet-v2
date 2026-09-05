#!/usr/bin/env python3
"""The 100-job canary release gate, asserted from the v2 store (decisions memo, "Read-only canary").

Cohort: exactly the job ids `canary_submit.py` recorded in jobs.json. For every job: terminal;
every attempt's launch.json argv carries `--sandbox read-only` and the isolated-review flags
(`--ephemeral --ignore-user-config --ignore-rules`, hooks and plugins disabled); no attempt
`quarantined` or `lost`; an accepted attempt has a deliverable with bytes and a notice row;
a failed job has a classified outcome (never `unknown`). At least `--min-success` jobs must
have succeeded, so a batch that only fails cleanly fails the gate. Then the clone: `git status
--porcelain` empty and HEAD equal to the recorded baseline. Exit 1 on any violation.

    uv run python tools/canary_check.py [--state-root ~/.subfleet] [--canary-dir ~/subfleet-v2-canary]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from pathlib import Path

CLASSIFIED = {"limited", "auth-dead", "cli-too-old", "content-filter", "transient"}
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}
ISOLATION_FLAGS = ("--ephemeral", "--ignore-user-config", "--ignore-rules")
ISOLATION_CONFIG = ("features.hooks=false", "features.plugins=false")


def check(state_root: Path, canary_dir: Path, *, expect: int, min_success: int) -> dict:
    db = sqlite3.connect(f"file:{state_root / 'state.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    violations: list[str] = []
    jobs_file = canary_dir / "jobs.json"
    cohort = {row["job_id"] for row in json.loads(jobs_file.read_text()) if row.get("job_id")} if jobs_file.exists() else set()
    if not cohort:
        violations.append(f"no cohort recorded at {jobs_file}")
    if cohort and len(cohort) != expect:
        violations.append(f"cohort has {len(cohort)} job ids, {expect} expected")
    jobs = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE job_id LIKE '%-canary-%' ORDER BY job_id")]
    found = {j["job_id"] for j in jobs}
    for missing in sorted(cohort - found):
        violations.append(f"{missing} is in the cohort but not in the store")
    for extra in sorted(found - cohort):
        violations.append(f"{extra} is a canary job outside the recorded cohort")
    terminal = succeeded = 0
    for job in jobs:
        attempts = [dict(r) for r in db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (job["job_id"],))]
        for a in attempts:
            if a["state"] in ("quarantined", "lost"):
                violations.append(f"{a['attempt_id']} is {a['state']}")
            launch = state_root / "jobs" / job["job_id"] / f"a{a['seq']}" / "launch.json"
            try:
                argv = list(json.loads(launch.read_text()).get("argv", []))
            except (OSError, ValueError):
                argv = []
            if not argv:
                # launch.json is written at launch; only a finished attempt without one is wrong.
                if a["state"] not in ("reserved", "starting", "running", "finalizing"):
                    violations.append(f"{a['attempt_id']} has no launch.json argv")
                continue
            if "--sandbox" not in argv or argv[argv.index("--sandbox") + 1: argv.index("--sandbox") + 2] != ["read-only"]:
                violations.append(f"{a['attempt_id']} argv lacks --sandbox read-only")
            for flag in ISOLATION_FLAGS:
                if flag not in argv:
                    violations.append(f"{a['attempt_id']} argv lacks {flag}")
            configs = {argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "-c"}
            for config in ISOLATION_CONFIG:
                if config not in configs:
                    violations.append(f"{a['attempt_id']} argv lacks -c {config}")
        if job["state"] not in TERMINAL:
            continue
        terminal += 1
        accepted = job.get("accepted_attempt_id")
        if accepted:
            deliverable = db.execute("SELECT bytes FROM artifacts WHERE attempt_id=? AND role='deliverable'", (accepted,)).fetchone()
            notice = db.execute("SELECT 1 FROM notices WHERE job_id=?", (job["job_id"],)).fetchone()
            if not deliverable or not deliverable["bytes"]:
                violations.append(f"{job['job_id']} accepted without a deliverable")
            elif not notice:
                violations.append(f"{job['job_id']} accepted without a notice")
            else:
                succeeded += 1
        else:
            last = attempts[-1] if attempts else None
            if not last or last["outcome_class"] not in CLASSIFIED:
                violations.append(f"{job['job_id']} ended {job['state']} without a classified failure "
                                  f"({last['outcome_class'] if last else 'no attempt'})")
    clone = canary_dir / "work"
    if clone.is_dir():
        status = subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True).stdout.strip()
        if status:
            violations.append(f"canary clone is dirty: {status.splitlines()[0]} ...")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout.strip()
        baseline_path = canary_dir / "baseline.json"
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text()).get("head")
            if baseline and baseline != head:
                violations.append(f"canary clone HEAD moved: {baseline} -> {head}")
    else:
        violations.append(f"canary clone missing at {clone}")
    complete = terminal >= expect and len(jobs) >= expect
    if complete and succeeded < min_success:
        violations.append(f"{succeeded} succeeded, {min_success} required: a batch that only fails cleanly does not pass")
    verdict = "FAIL" if violations else ("PASS" if complete else f"PENDING ({terminal}/{expect} terminal, {succeeded} succeeded)")
    return {"jobs": len(jobs), "terminal": terminal, "succeeded": succeeded,
            "violations": violations, "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME", "~/.subfleet"))
    ap.add_argument("--canary-dir", default="~/subfleet-v2-canary")
    ap.add_argument("--expect", type=int, default=100)
    ap.add_argument("--min-success", type=int, default=90)
    args = ap.parse_args()
    result = check(Path(args.state_root).expanduser(), Path(args.canary_dir).expanduser(),
                   expect=args.expect, min_success=args.min_success)
    print(f"canary jobs: {result['jobs']} ({result['terminal']} terminal, {result['succeeded']} succeeded); "
          f"violations: {len(result['violations'])}")
    for v in result["violations"]:
        print(f"- {v}")
    print(result["verdict"])
    return 1 if result["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
