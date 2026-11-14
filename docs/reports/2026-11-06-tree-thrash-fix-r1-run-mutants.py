"""Bounded one-off runner for the fix-r1 ordered-test handoff."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


worktree = Path(__file__).resolve().parents[2]
reports = worktree / "docs/reports"
prefix = "2026-11-06-tree-thrash-fix-r1"
marker = reports / f"{prefix}-checks-ready.json"
gate_path = reports / f"{prefix}-mutant-gate.json"
archive_metadata = reports / f"{prefix}-mutant-archive.json"
temp_root = Path(os.environ["TMPDIR"]).resolve()
archive = temp_root / "fix-r1-mutations/archive"
mutation_python = temp_root / "tree-thrash-r1/py314-env/bin/python"
now = datetime.now(timezone.utc)
deadline = now.replace(hour=19, minute=57, second=0, microsecond=0)
latest_gate = now.replace(hour=19, minute=35, second=0, microsecond=0)
state = {"waiting_started": now.isoformat(), "marker": str(marker),
         "hard_stop": deadline.isoformat(), "cleanup_reserve_seconds": 300,
         "driver_pid": os.getpid(), "mutation_python": str(mutation_python)}


def save():
    gate_path.write_text(json.dumps(state, indent=2) + "\n")


save()
print("Waiting for the ordered-test marker", flush=True)
while not marker.exists():
    if datetime.now(timezone.utc) >= latest_gate:
        state["not_run"] = "Ordered-test marker did not arrive by 19:35 UTC"
        save()
        raise SystemExit(0)
    time.sleep(5)

assert not (archive / ".git").exists()
assert (archive / "subfleet/daemon.py").is_file()
assert not (archive / "subfleet/daemon.py").is_symlink()
files = ("subfleet/daemon.py", "tests/fake/test_admission_tree_thrash.py",
         "tests/fixtures/tree_thrash_a8624a4899a2.py", "tools/tree_thrash_mutations.py")
metadata = json.loads(archive_metadata.read_text())
metadata["overlay_sha256"] = {}
for relative in files:
    source, target = worktree / relative, archive / relative
    shutil.copyfile(source, target)
    metadata["overlay_sha256"][relative] = hashlib.sha256(source.read_bytes()).hexdigest()
metadata["launch_refresh_utc"] = datetime.now(timezone.utc).isoformat()
archive_metadata.write_text(json.dumps(metadata, indent=2) + "\n")
original = (archive / "subfleet/daemon.py").read_bytes()

remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
timeout = min(300, int((remaining - 300) // 8))
if timeout < 120:
    state["not_run"] = "Less than 120 seconds per mutant remained after cleanup reserve"
    save()
    raise SystemExit(0)
assert mutation_python.is_file()
command = [str(mutation_python), "tools/tree_thrash_mutations.py", "baseline",
           "reserve-without-tree", "lose-prepared-baseline", "evaluate-again-after-git",
           "forget-sweep-eligibility-recheck", "--timeout", str(timeout),
           "--control-timeout", str(timeout * 4),
           "--control-test", "tests/fake/test_admission_tree_thrash.py::test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree",
           "--control-test", "tests/fake/test_admission_tree_thrash.py::test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation",
           "--control-test", "tests/fake/test_admission_tree_thrash.py::test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass",
           "--junit-dir", str(reports), "--temp-root", str(temp_root / "fix-r1-mutant-tests"),
           "--output", str(reports / f"{prefix}-mutants.json")]
state.update(started=datetime.now(timezone.utc).isoformat(), command=command,
             per_mutant_timeout=timeout, control_timeout=timeout * 4)
save()
environment = {**os.environ, "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
               "UV_CACHE_DIR": str(temp_root / "uv-cache")}
print(f"Starting four mutants with {timeout}s individual bounds", flush=True)
with (reports / f"{prefix}-mutants.log").open("w") as log:
    completed = subprocess.run(command, cwd=archive, env=environment, stdout=log, stderr=subprocess.STDOUT)
state.update(ended=datetime.now(timezone.utc).isoformat(), exit_code=completed.returncode,
             archive_daemon_restored=(archive / "subfleet/daemon.py").read_bytes() == original)
save()
assert state["archive_daemon_restored"]
print(f"Mutation runner exited {completed.returncode}; archived daemon restored", flush=True)
raise SystemExit(completed.returncode)
