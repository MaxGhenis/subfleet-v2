"""Run the requested closed pair only if the full comparison finishes by 19:00."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


worktree = Path(__file__).resolve().parents[2]
reports = worktree / "docs/reports"
prefix = "2026-11-06-tree-thrash-fix-r1"
comparison = reports / f"{prefix}-cost.json"
gate = reports / f"{prefix}-closed-repeat-gate.json"
now = datetime.now(timezone.utc)
cutoff = now.replace(hour=19, minute=0, second=0, microsecond=0)
state = {"waiting_started": now.isoformat(), "start_cutoff": cutoff.isoformat()}


def save():
    gate.write_text(json.dumps(state, indent=2) + "\n")


save()
print("Waiting for the complete three-scenario cost comparison", flush=True)
while True:
    try:
        complete = json.loads(comparison.read_text()).get("complete", False)
    except (FileNotFoundError, json.JSONDecodeError):
        complete = False
    if datetime.now(timezone.utc) >= cutoff:
        state["not_run"] = "The full comparison did not finish before 19:00 UTC"
        save()
        raise SystemExit(0)
    if complete:
        break
    time.sleep(5)

temp_root = Path(os.environ["TMPDIR"]).resolve()
command = ["/opt/homebrew/bin/uv", "run", "--no-sync", "python", "tools/benchmark_tree_thrash.py",
           "--samples", "1", "--jobs", "300", "--scenarios", "closed", "--head-overlay",
           "subfleet/daemon.py", "--temp-root", str(temp_root),
           "--output", str(reports / f"{prefix}-closed-repeat.json")]
state.update(started=datetime.now(timezone.utc).isoformat(), command=command,
             runner_sha256=hashlib.sha256((worktree / "tools/benchmark_tree_thrash.py").read_bytes()).hexdigest(),
             head_daemon_sha256=hashlib.sha256((worktree / "subfleet/daemon.py").read_bytes()).hexdigest())
save()
environment = {**os.environ, "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
               "UV_CACHE_DIR": str(temp_root / "uv-cache")}
print("Starting the second closed pair", flush=True)
with (reports / f"{prefix}-closed-repeat.log").open("w") as log:
    completed = subprocess.run(command, cwd=worktree, env=environment, stdout=log, stderr=subprocess.STDOUT)
state.update(ended=datetime.now(timezone.utc).isoformat(), exit_code=completed.returncode)
save()
print(f"Closed repeat exited {completed.returncode}", flush=True)
raise SystemExit(completed.returncode)
