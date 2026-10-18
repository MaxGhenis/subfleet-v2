"""Replay PR #180 rollback regressions and the four ramp mutations serially.

Each pytest process holds the shared suites lock. Source edits are restored in
finally, including on interruption. Run --baseline or --mutations, never both
concurrently. This runner uses only the explicitly allowed ramp test modules.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = "cacdb35aad7c4b1de0f5fb6bcddb114af772ba33"
REGRESSIONS = [
    "tests/fake/test_admission_ramp.py::test_rollback_next_pass_counts_future_completed_reservation",
    "tests/fake/test_admission_ramp.py::test_rollback_real_restart_counts_future_durable_reservation",
    "tests/fake/test_admission_ramp.py::test_rollback_during_workspace_rechecks_committed_budget_without_leases",
    "tests/unit/test_placement_ramp.py::test_rollback_census_keeps_history_that_expired_at_pass_entry",
]
MUTATIONS = [
    ("own-pass-count", [("subfleet/daemon.py", "                self._ramp.reserve(aid, reserved_at)\n",
                         "                pass  # mutation: omit this pass's committed starts\n")], [
        "tests/fake/test_admission_ramp.py::test_committed_placements_this_pass_fill_window_and_rows_read_once",
        "tests/fake/test_admission_ramp.py::test_disk_reopening_56_jobs_on_10_lanes"]),
    ("count-attended", [
        ("subfleet/daemon.py", "\"WHERE j.kind NOT IN ('turn','probe') AND a.reserved_at>=?\"",
         "\"WHERE j.kind NOT IN ('probe') AND a.reserved_at>=?\""),
        ("subfleet/ramp.py", '            if row.get("kind") in ("turn", "probe"):\n',
         '            if row.get("kind") == "probe":\n')], [
        "tests/fake/test_admission_ramp.py::test_attended_turn_never_reads_waits_on_or_counts_against_ramp"]),
    ("drop-reconstruction", [("subfleet/ramp.py", "        for row in attempts:\n",
                             "        for row in ():\n")], [
        "tests/fake/test_admission_ramp.py::test_real_restart_counts_completed_attempt_and_retry_until_each_expires"]),
    ("process-memory-only", [("subfleet/daemon.py", "            self._ramp.begin_pass(self.policy, rows, now)\n",
         '            self._ramp.begin_pass(self.policy, [row for row in rows\n'
         '                                                if row["attempt_id"] in self._ramp.reservations], now)\n')], [
        "tests/fake/test_admission_ramp.py::test_real_restart_counts_completed_attempt_and_retry_until_each_expires",
        "tests/fake/test_admission_ramp.py::test_gate_review_counts_holds_and_reconstructs_completed_start_after_restart"]),
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(tests):
    temp_root = subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
    tmpdir = Path(temp_root) / f"sf-ramp2-{os.getpid()}"
    tmpdir.mkdir()
    command = ["/usr/bin/lockf", "-k", "/private/tmp/claude-501/subfleet-suites.lock",
               "uv", "run", "pytest", "-q", *tests, "--tb=short"]
    env = {**os.environ, "TMPDIR": str(tmpdir) + "/", "UV_CACHE_DIR": str(ROOT / ".uv-cache")}
    print("Waiting for suites lock / running:", " ".join(command), flush=True)
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    lines = []
    try:
        for line in process.stdout:
            lines.append(line)
            print(line, end="", flush=True)
        code = process.wait()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        shutil.rmtree(tmpdir)
    output = "".join(lines)
    summaries = re.findall(r"^.*\d+ (?:failed|passed).* in .*s.*$", output, re.MULTILINE)
    return {"tests": tests, "command": command, "exit_code": code,
            "summary": summaries[-1] if summaries else "no pytest summary",
            "failed": re.findall(r"^FAILED (\S+)", output, re.MULTILINE),
            "wall_seconds_including_lock_wait": round(time.monotonic() - started, 2),
            "output": output, "tmpdir_removed": not tmpdir.exists()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--baseline", action="store_true")
    group.add_argument("--mutations", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    originals = {name: (ROOT / name).read_bytes() for name in ("subfleet/daemon.py", "subfleet/ramp.py")}
    results = []
    try:
        if args.baseline:
            for name in originals:
                before = subprocess.check_output(["/usr/bin/git", "--git-dir=.git-local", "show", f"{BASE}:{name}"], cwd=ROOT)
                (ROOT / name).write_bytes(before)
            result = run(REGRESSIONS)
            result.update(case="base-regressions", base=BASE)
            results.append(result)
        else:
            for name, edits, tests in MUTATIONS:
                print(f"Mutation: {name}", flush=True)
                try:
                    for file, old, new in edits:
                        source = (ROOT / file).read_text()
                        assert source.count(old) == 1, (name, file, "mutation anchor changed")
                        (ROOT / file).write_text(source.replace(old, new, 1))
                    result = run(tests)
                    result.update(case=name, edits=[{"file": f, "original": o, "mutated": n} for f, o, n in edits],
                                  result="killed" if result["exit_code"] == 1 and result["failed"] else "survived-or-error")
                    results.append(result)
                finally:
                    for file, data in originals.items():
                        (ROOT / file).write_bytes(data)
    finally:
        for file, data in originals.items():
            (ROOT / file).write_bytes(data)
        restored = {file: digest((ROOT / file).read_bytes()) == digest(data) for file, data in originals.items()}
        args.output.write_text(json.dumps({"source_restored": restored, "runs": results}, indent=2) + "\n")
        assert all(restored.values())
    if args.baseline:
        assert results[0]["exit_code"] == 1 and set(results[0]["failed"]) == set(REGRESSIONS)
    else:
        assert len(results) == 4 and all(r["result"] == "killed" for r in results)


if __name__ == "__main__":
    main()
