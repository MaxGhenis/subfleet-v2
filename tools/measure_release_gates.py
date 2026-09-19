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
import base64
import json
import math
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
from tests.fake.profile import derived_identity  # noqa: E402


def p95(samples: list[float]) -> float:
    if not samples:
        raise ValueError("at least one sample is required")
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1] * 1000.0


def violations(status_ms: float, submit_ms: float, recovery_s: float, attempts: int, final: str) -> list[str]:
    """C-20.4: a measurement command is successful only when every numeric gate passes."""
    failures = []
    for name, measured, limit in (("cached status p95", status_ms, 100),
                                  ("submit p95", submit_ms, 250),
                                  ("SIGKILL recovery", recovery_s, 30)):
        if not math.isfinite(measured) or measured >= limit:
            failures.append(f"{name}: {measured:g} is not below {limit}")
    if attempts != 1 or final != "succeeded":
        failures.append(f"recovery requires one succeeded attempt, got {attempts} attempt(s), {final}")
    return failures


def timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def checked_cli(e2e: E2E, *argv):
    result = e2e.cli(*argv)
    if result.rc != 0:
        raise RuntimeError(f"release measurement command {argv[0]} failed (exit {result.rc}): {result.stderr}")
    return result


def seed_lanes(e2e: E2E, total: int = 14) -> None:
    """Fourteen lanes, like the real fleet: extend the two-per-provider fixture roster."""
    lanes = json.loads((e2e.root / "lanes.json").read_text())
    number = 3
    while len(lanes) < total:
        provider = "codex" if number % 2 else "claude"
        if provider == "codex":
            home = e2e.root / f"codex-{number}"
            home.mkdir(exist_ok=True)
            claims = {"https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus", "chatgpt_account_id": f"fake-{number}"}}
            token = "fixture." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fixture"
            (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
                "access_token": token, "account_id": f"fake-{number}"}}))
            lanes.append({"lane_id": f"codex-{number}", "provider": "codex",
                          "account_key": f"codex:fake-{number}", "credential_kind": "home",
                          "credential_ref": str(home), "home": str(home), "owner": "v2"})
        else:
            identity, label = derived_identity(number)
            e2e.env[f"E2E_CLAUDE_TOKEN_{number}"] = f"fake-subscription-token-{number}"
            lanes.append({"lane_id": f"claude-{number}", "provider": "claude",
                          "account_key": f"claude:{identity}", "credential_kind": "env",
                          "credential_ref": f"E2E_CLAUDE_TOKEN_{number}", "owner": "v2",
                          "identity": identity, "label": label, "identity_status": "verified"})
        number += 1
    (e2e.root / "lanes.json").write_text(json.dumps(lanes))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=200)
    parser.add_argument("--jobs", type=int, default=500)
    args = parser.parse_args()
    if args.calls < 1 or args.jobs < 1:
        parser.error("--calls and --jobs must be positive")

    root = Path(tempfile.mkdtemp(prefix="sf-gates-", dir="/tmp"))
    e2e = E2E(root)
    seed_lanes(e2e)
    rows: list[tuple[str, str, str]] = []
    try:
        e2e.start(scenario="success")

        # Fill the store so `status` and `submit` measure against a realistic size.
        fill_start = time.perf_counter()
        for i in range(args.jobs):
            checked_cli(e2e, *e2e.run_args("astra", "-n", f"fill{i}"))
        e2e.until(lambda: len(e2e.rows("SELECT 1 FROM jobs WHERE state IN ('succeeded','failed')")) >= args.jobs, timeout=600)
        rows.append(("store fill", f"{args.jobs} jobs to terminal", f"{time.perf_counter() - fill_start:.1f} s"))

        status_samples = [timed(lambda: checked_cli(e2e, "status")) for _ in range(args.calls)]
        rows.append(("cached status p95 (CLI, end to end)", "under 100 ms", f"{p95(status_samples):.0f} ms"))

        # The same verb without a Python interpreter start: one in-process client,
        # the three socket calls `subfleet status` makes, timed as one round trip.
        from subfleet import protocol
        from subfleet.client import Client
        client = Client(root)
        def status_round_trip() -> None:
            client.call("daemon.status", {})
            client.call("lanes", protocol.LanesArgs().__dict__)
            client.call("readings", protocol.ReadingsArgs().__dict__)
        socket_samples = [timed(status_round_trip) for _ in range(args.calls)]
        rows.append(("cached status p95 (socket round trip, no interpreter start)", "under 100 ms",
                     f"{p95(socket_samples):.0f} ms"))
        interpreter = [timed(lambda: subprocess.run([sys.executable, "-c", "import subfleet.cli"],
                                                    cwd=REPO, check=True)) for _ in range(20)]
        rows.append(("interpreter start plus CLI import (reference)", "n/a", f"{p95(interpreter):.0f} ms p95"))

        submit_samples = [timed(lambda: checked_cli(e2e, *e2e.run_args("astra", "-n", f"probe{i}")))
                          for i in range(args.calls)]
        rows.append(("submit p95 (probes excluded: measured lanes)", "under 250 ms", f"{p95(submit_samples):.0f} ms"))
        e2e.until(lambda: not e2e.rows("SELECT 1 FROM jobs WHERE state IN ('queued','running','waiting')"), timeout=600)

        # Recovery: a slow job, SIGKILL the daemon while it runs, restart, time to terminal.
        e2e.close()
        e2e.start(scenario="slow", delay_s=3)
        submitted = checked_cli(e2e, *e2e.run_args("astra", "-n", "recover"))
        job_id = submitted.stdout.strip().splitlines()[0].strip()
        e2e.until(lambda: any(a["state"] == "running" for a in e2e.attempts(job_id)), timeout=30)
        e2e.crash()
        restart = time.perf_counter()
        e2e.start(scenario="slow", delay_s=3)
        e2e.until(lambda: e2e.job(job_id)["state"] in ("succeeded", "failed", "lost", "quarantined"), timeout=60)
        recovery_s = time.perf_counter() - restart
        rows.append(("recovery after daemon SIGKILL", "under 30 s", f"{recovery_s:.1f} s"))
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
    failures = violations(p95(status_samples), p95(submit_samples), recovery_s, len(attempts), final)
    for failure in failures:
        print(f"FAIL: {failure}")
    print("FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
