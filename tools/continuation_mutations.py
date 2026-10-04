"""Foreground mutation checks for C-24.10 and C-24.11. Restores every edit.

Run with the checkout's test Python. No daemon or native state is opened.
"""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ("blocked-wake", "subfleet/conversations/wakes.py",
     'if not c or c["blocked_by"] or c["legacy_hold"] or c["archived_at"]:',
     'if not c or c["legacy_hold"] or c["archived_at"]:', "test_property_no_wake_into_held_conversation"),
    ("unlimited-wakes", "subfleet/conversations/wakes.py",
     'if c["wake_streak"] >= MAX_STREAK and now - (c["last_wake_at"] or 0) < COOLDOWN_S:',
     'if False:', "test_throttle_eight_and_cooldown"),
    ("request-replay", "subfleet/conversations/wakes.py",
     "UPDATE wake_requests SET state='fired',message_id=?", "UPDATE wake_requests SET state='pending',message_id=?",
     "test_one_request_with_multiple_kinds_fires_once"),
    ("unblock-live-writer", "subfleet/conversations/service.py",
     'if external_writers(c["native_session_id"]):', 'if False and external_writers(c["native_session_id"]):',
     "test_moot_block_differential_against_dispatch_live_writer_check"),
]


def main():
    results = []
    for name, relative, before, after, test in MUTATIONS:
        path = ROOT / relative
        original = path.read_text()
        assert original.count(before) == 1, (name, "mutation anchor must be unique")
        try:
            path.write_text(original.replace(before, after))
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short",
                                   f"tests/unit/test_conversation_wakes.py::{test}"],
                                  cwd=ROOT, capture_output=True, text=True, timeout=120)
            killed = done.returncode == 1 and "failed" in done.stdout
            results.append({"mutation": name, "killed": killed, "result": done.stdout.strip().splitlines()[-1]})
            print(json.dumps(results[-1]), flush=True)
        finally:
            path.write_text(original)
    report = ROOT / "docs/reports/2026-10-04-continuation-mutations.json"
    report.write_text(json.dumps(results, indent=2) + "\n")
    return int(not all(r["killed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
