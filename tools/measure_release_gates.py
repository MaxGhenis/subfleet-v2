#!/usr/bin/env python3
"""Measure the numeric release gates of docs/release-gates.md (plan amendment 10, C-20.4).

Runs a real daemon on a temporary state root against the fake providers, using the
end-to-end harness in tests/e2e/conftest.py, and prints a markdown table:

- cached `status` p95 (target under 100 ms) over N calls with 14 lanes and 500 jobs
- `submit` p95 (target under 250 ms, probes excluded) over N submits
- recovery after a daemon SIGKILL (target under 30 s) from restart to the attempt
  being re-adopted or finalized

Usage: uv run python tools/measure_release_gates.py [--calls 200] [--jobs 500]
Needs /bin/ps and /usr/sbin/sysctl reachable (not a sandboxed shell). Nothing here
touches ~/.subfleet or v1; everything lives under a temp root in /tmp.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from tests.e2e.conftest import E2E  # noqa: E402


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)] * 1000.0


def timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def seed_lanes(e2e: E2E, total: int = 14) -> None:
    """Fourteen lanes, like the real fleet: extend the two-per-provider fixture roster."""
    lanes = json.loads((e2e.root / "lanes.json").read_text())
    number = 3
    while len(lanes) < total:
        provider = "codex" if number % 2 else "claude"
        if provider == "codex":
            home = e2e.root / f"codex-{number}"
            home.mkdir(exist_ok=True)
            (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
                "access_token": "fixture.e30.fixture", "account_id": f"fake-{number}"}}))
            lanes.append({"lane_id": f"codex-{number}", "provider": "codex",
                          "account_key": f"codex:fake-{number}", "credential_kind": "home",
                          "credential_ref": str(home), "home": str(home), "owner": "v2"})
        else:
            lanes.append({"lane_id": f"claude-{number}", "provider": "claude",
                          "account_key": f"claude:fake-{number}", "credential_kind": "env",
                          "credential_ref": "E2E_CLAUDE_TOKEN_1", "owner": "v2"})
        number += 1
    (e2e.root / "lanes.json").write_text(json.dumps(lanes))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=200)
    parser.add_argument("--jobs", type=int, default=500)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="sf-gates-", dir="/tmp"))
    e2e = E2E(root)
    seed_lanes(e2e)
    rows: list[tuple[str, str, str]] = []
    try:
        e2e.start(scenario="success")

        # Fill the store so `status` and `submit` measure against a realistic size.
        fill_start = time.perf_counter()
        for i in range(args.jobs):
            e2e.cli(*e2e.run_args("astra", "-n", f"fill{i}"))
        e2e.until(lambda: len(e2e.rows("SELECT 1 FROM jobs WHERE state IN ('succeeded','failed')")) >= args.jobs, timeout=600)
        rows.append(("store fill", f"{args.jobs} jobs to terminal", f"{time.perf_counter() - fill_start:.1f} s"))

        status_samples = [timed(lambda: e2e.cli("status")) for _ in range(args.calls)]
        rows.append(("cached status p95", "under 100 ms", f"{p95(status_samples):.0f} ms"))

        submit_samples = [timed(lambda: e2e.cli(*e2e.run_args("astra", "-n", f"probe{i}")))
                          for i in range(args.calls)]
        rows.append(("submit p95 (probes excluded: measured lanes)", "under 250 ms", f"{p95(submit_samples):.0f} ms"))
        e2e.until(lambda: not e2e.rows("SELECT 1 FROM jobs WHERE state IN ('queued','running','waiting')"), timeout=600)

        # Recovery: a slow job, SIGKILL the daemon while it runs, restart, time to terminal.
        e2e.close()
        e2e.start(scenario="slow", delay_s=3)
        submitted = e2e.cli(*e2e.run_args("astra", "-n", "recover"))
        job_id = submitted.stdout.strip().splitlines()[0].strip()
        e2e.until(lambda: any(a["state"] == "running" for a in e2e.attempts(job_id)), timeout=30)
        e2e.crash()
        restart = time.perf_counter()
        e2e.start(scenario="slow", delay_s=3)
        e2e.until(lambda: e2e.job(job_id)["state"] in ("succeeded", "failed", "lost", "quarantined"), timeout=60)
        rows.append(("recovery after daemon SIGKILL", "under 30 s", f"{time.perf_counter() - restart:.1f} s"))
        final = e2e.job(job_id)["state"]
        attempts = e2e.attempts(job_id)
        rows.append(("re-adopted attempt", "one attempt, succeeded", f"{len(attempts)} attempt(s), {final}"))
    finally:
        e2e.close()

    print(f"Measured {time.strftime('%Y-%m-%d %H:%M %Z')} on {os.uname().nodename}, state root {root}")
    print()
    print("| Gate | Target | Result |")
    print("|---|---|---|")
    for gate, target, result in rows:
        print(f"| {gate} | {target} | {result} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
