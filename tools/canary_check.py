#!/usr/bin/env python3
"""The 100-job canary release gate, asserted from the v2 store (decisions memo, "Read-only canary").

For every canary job: terminal; a deliverable artifact with bytes or a classified failure
(never `unknown`); no attempt quarantined or lost; every attempt's launch.json argv carries
`--sandbox read-only`. Then the canary clone: `git status --porcelain` empty and HEAD equal to
the recorded baseline. Prints the table and exits 1 on any violation.

    uv run python tools/canary_check.py [--state-root ~/.subfleet] [--clone ~/subfleet-v2-canary/work] [--expect 100]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

CLASSIFIED = {"limited", "auth-dead", "cli-too-old", "content-filter", "transient"}
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME", "~/.subfleet"))
    ap.add_argument("--clone", default="~/subfleet-v2-canary/work")
    ap.add_argument("--baseline", default="~/subfleet-v2-canary/baseline.json", help="written by canary_submit.py")
    ap.add_argument("--canary-like", default="%-canary-%")
    ap.add_argument("--expect", type=int, default=100)
    args = ap.parse_args()
    root = Path(args.state_root).expanduser()
    db = sqlite3.connect(f"file:{root / 'state.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    jobs = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE job_id LIKE ? ORDER BY job_id", (args.canary_like,))]
    violations: list[str] = []
    if len(jobs) < args.expect:
        violations.append(f"{len(jobs)} canary jobs found, {args.expect} expected")
    terminal = 0
    for job in jobs:
        attempts = [dict(r) for r in db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (job["job_id"],))]
        if job["state"] not in TERMINAL:
            continue
        terminal += 1
        for a in attempts:
            if a["state"] in ("quarantined", "lost"):
                violations.append(f"{a['attempt_id']} is {a['state']}")
            launch = root / "jobs" / job["job_id"] / f"a{a['seq']}" / "launch.json"
            try:
                argv = json.loads(launch.read_text()).get("argv", [])
            except (OSError, ValueError):
                argv = []
            if "--sandbox" not in argv or argv[argv.index("--sandbox") + 1: argv.index("--sandbox") + 2] != ["read-only"]:
                violations.append(f"{a['attempt_id']} launch argv lacks --sandbox read-only")
        accepted = job.get("accepted_attempt_id")
        if accepted:
            deliverable = db.execute("SELECT bytes FROM artifacts WHERE attempt_id=? AND role='deliverable'", (accepted,)).fetchone()
            if not deliverable or not deliverable["bytes"]:
                violations.append(f"{job['job_id']} accepted without a deliverable")
        else:
            last = attempts[-1] if attempts else None
            if not last or last["outcome_class"] not in CLASSIFIED:
                violations.append(f"{job['job_id']} ended {job['state']} without a classified failure ({last['outcome_class'] if last else 'no attempt'})")
    clone = Path(args.clone).expanduser()
    if clone.exists():
        status = subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True).stdout.strip()
        if status:
            violations.append(f"canary clone is dirty:\n{status}")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout.strip()
        baseline_path = Path(args.baseline).expanduser()
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text()).get("head")
            if baseline and baseline != head:
                violations.append(f"canary clone HEAD moved: {baseline} -> {head}")
    else:
        violations.append(f"canary clone missing at {clone}")
    print(f"canary jobs: {len(jobs)} ({terminal} terminal); violations: {len(violations)}")
    for v in violations:
        print(f"- {v}")
    print("PASS" if not violations and terminal >= args.expect else "FAIL" if violations else f"PENDING ({terminal}/{args.expect} terminal)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
