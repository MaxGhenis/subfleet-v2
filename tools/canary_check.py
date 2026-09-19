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
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from subfleet.adapters.isolation import CODEX_CONFIG

CLASSIFIED = {"limited", "auth-dead", "cli-too-old", "content-filter", "transient"}
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}
ISOLATION_FLAGS = ("--ephemeral", "--ignore-user-config", "--ignore-rules")
ISOLATION_CONFIG = CODEX_CONFIG


def same_directory(value, expected: Path) -> bool:
    """Launch paths are absolute; resolve aliases without inventing a missing cwd."""
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return False
    try:
        path = Path(value).resolve(strict=True)
        return path.is_dir() and path == expected.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def check(state_root: Path, canary_dir: Path, *, expect: int, min_success: int) -> dict:
    if expect < 1 or not 1 <= min_success <= expect:
        raise ValueError("expect must be positive and min-success must be between 1 and expect")
    db = sqlite3.connect((state_root / 'state.sqlite3').resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    # A running daemon must not make the evidence span different database snapshots.
    db.execute("BEGIN")
    violations: list[str] = []
    clone = canary_dir / "work"
    jobs_file = canary_dir / "jobs.json"
    try:
        recorded = json.loads(jobs_file.read_text())
        if not isinstance(recorded, list) or any(not isinstance(r, dict) or not isinstance(r.get("job_id"), str)
                                                or not r["job_id"] for r in recorded):
            raise ValueError("every submission needs a job id")
        cohort = {row["job_id"] for row in recorded}
        if len(cohort) != len(recorded):
            violations.append("cohort has duplicate job ids")
        if any(row.get("rc", 0) != 0 for row in recorded):
            violations.append("cohort contains a failed submission")
    except (OSError, ValueError):
        cohort = set()
        violations.append(f"missing or invalid cohort at {jobs_file}")
    if not cohort:
        violations.append(f"no cohort recorded at {jobs_file}")
    if cohort and len(cohort) != expect:
        violations.append(f"cohort has {len(cohort)} job ids, {expect} expected")
    jobs = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY job_id")
            if r["job_id"] in cohort or "-canary-" in r["job_id"]]
    found = {j["job_id"] for j in jobs}
    for missing in sorted(cohort - found):
        violations.append(f"{missing} is in the cohort but not in the store")
    for extra in sorted(found - cohort):
        violations.append(f"{extra} is a canary job outside the recorded cohort")
    terminal = succeeded = 0
    for job in jobs:
        if job["sandbox"] != "read-only" or not job["isolated_review"]:
            violations.append(f"{job['job_id']} is not an isolated read-only job")
        for field in ("workdir", "review_root"):
            if not same_directory(job.get(field), clone):
                violations.append(f"{job['job_id']} {field} does not resolve to the canary clone")
        if job.get("worktree") and not same_directory(job["worktree"], clone):
            violations.append(f"{job['job_id']} worktree does not resolve to the canary clone")
        attempts = [dict(r) for r in db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (job["job_id"],))]
        for a in attempts:
            if a["state"] in ("quarantined", "lost"):
                violations.append(f"{a['attempt_id']} is {a['state']}")
            launch = state_root / "jobs" / job["job_id"] / f"a{a['seq']}" / "launch.json"
            try:
                launch_record = json.loads(launch.read_text())
                argv = launch_record.get("argv", [])
                if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
                    raise ValueError("invalid launch argv")
                if not same_directory(launch_record.get("cwd"), clone):
                    violations.append(f"{a['attempt_id']} launch cwd does not resolve to the canary clone")
            except (OSError, ValueError, AttributeError):
                argv = []
            if not argv:
                # launch.json is written at launch; only a finished attempt without one is wrong.
                if a["state"] not in ("reserved", "starting", "running", "finalizing"):
                    violations.append(f"{a['attempt_id']} has no launch.json argv")
                continue
            for i, item in enumerate(argv):
                if item in ("-C", "--cd"):
                    cwd = argv[i + 1] if i + 1 < len(argv) else None
                elif item.startswith("--cd="):
                    cwd = item.removeprefix("--cd=")
                elif item.startswith("-C") and len(item) > 2:
                    cwd = item[2:]
                else:
                    continue
                if not same_directory(cwd, clone):
                    violations.append(f"{a['attempt_id']} argv changes cwd outside the canary clone")
            sandboxes = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "--sandbox"]
            sandboxes += [item.split("=", 1)[1] for item in argv if item.startswith("--sandbox=")]
            if sandboxes != ["read-only"] or any(flag in argv for flag in
                    ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto")):
                violations.append(f"{a['attempt_id']} argv lacks --sandbox read-only")
            for flag in ISOLATION_FLAGS:
                if flag not in argv:
                    violations.append(f"{a['attempt_id']} argv lacks {flag}")
            configs = {}
            for i, item in enumerate(argv):
                config = None
                if item in ("-c", "--config") and i + 1 < len(argv):
                    config = argv[i + 1]
                elif item.startswith("--config="):
                    config = item.removeprefix("--config=")
                elif item.startswith("-c") and len(item) > 2:
                    config = item[2:].removeprefix("=")
                if config and "=" in config:
                    key, value = config.split("=", 1)
                    configs[key] = value
            for config in ISOLATION_CONFIG:
                key, value = config.split("=", 1)
                if configs.get(key) != value:
                    violations.append(f"{a['attempt_id']} argv lacks -c {config}")
        if job["state"] not in TERMINAL:
            continue
        terminal += 1
        accepted = job.get("accepted_attempt_id")
        if accepted:
            accepted_row = next((a for a in attempts if a["attempt_id"] == accepted), None)
            if (job["state"] != "succeeded" or not accepted_row or accepted_row["state"] != "succeeded"
                    or accepted_row["outcome_class"] != "ok" or accepted_row["rc"] != 0
                    or accepted_row != attempts[-1]):
                violations.append(f"{job['job_id']} has an inconsistent accepted attempt")
                continue
            deliverable = db.execute("SELECT path, bytes, sha256 FROM artifacts WHERE attempt_id=? AND role='deliverable'", (accepted,)).fetchone()
            notice = db.execute("SELECT 1 FROM notices WHERE job_id=?", (job["job_id"],)).fetchone()
            if not deliverable or not deliverable["bytes"]:
                violations.append(f"{job['job_id']} accepted without a deliverable")
            elif not notice:
                violations.append(f"{job['job_id']} accepted without a notice")
            else:
                try:
                    content = Path(deliverable["path"]).read_bytes()
                    if len(content) != deliverable["bytes"] or hashlib.sha256(content).hexdigest() != deliverable["sha256"]:
                        raise ValueError("size or SHA-256 mismatch")
                except (OSError, ValueError) as exc:
                    violations.append(f"{job['job_id']} deliverable cannot be verified: {exc}")
                else:
                    succeeded += 1
        else:
            last = attempts[-1] if attempts else None
            if job["state"] != "failed" or not last or last["state"] != "failed" or last["outcome_class"] not in CLASSIFIED:
                violations.append(f"{job['job_id']} ended {job['state']} without a classified failure "
                                  f"({last['outcome_class'] if last else 'no attempt'})")
    db.close()
    baseline_path = canary_dir / "baseline.json"
    try:
        baseline = json.loads(baseline_path.read_text())
        if not isinstance(baseline, dict) or not baseline.get("head") or baseline.get("count") != expect:
            raise ValueError("HEAD or cohort count missing or mismatched")
    except (OSError, ValueError) as exc:
        baseline = {}
        violations.append(f"missing or invalid canary baseline: {exc}")
    if clone.is_dir():
        try:
            status = subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True, check=True).stdout.strip()
            if status:
                violations.append(f"canary clone is dirty: {status.splitlines()[0]} ...")
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True, check=True).stdout.strip()
            if baseline.get("head") and baseline["head"] != head:
                violations.append(f"canary clone HEAD moved: {baseline['head']} -> {head}")
        except (OSError, subprocess.CalledProcessError) as exc:
            violations.append(f"canary clone cannot be verified: {exc}")
    else:
        violations.append(f"canary clone missing at {clone}")
    complete = terminal == expect and len(jobs) == expect and len(cohort) == expect
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
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
