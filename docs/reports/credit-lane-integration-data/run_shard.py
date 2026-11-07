"""Run part of one whole-suite shard, resuming where the last run stopped (r3 a3).

    lockf -k -t 30 /private/tmp/claude-501/subfleet-suites.lock \
        <worktree>/.venv/bin/python run_shard.py <worktree> <records> I/12 SOFT HARD

Runs `pytest -n 4 -p no:cacheprovider -p ci_shard_plugin --ci-shard I/12 --timeout=540`
with suite_resume.py loaded, in its own process group. No test starts after SOFT seconds;
at HARD seconds the group gets SIGINT (pytest tears its fixtures down), 30 s later
SIGKILL. Prints the shard's tally from the records afterwards.
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

worktree, records, shard, soft, hard = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], float(sys.argv[4]), \
    float(sys.argv[5])
index, count = shard.split("/")
here = Path(__file__).resolve().parent
records.mkdir(parents=True, exist_ok=True)
done = records / f"shard-{index}.jsonl"
runs = sorted(records.glob(f"shard-{index}-run*.log"))
run = len(runs) + 1
log = records / f"shard-{index}-run{run}.log"
basetemp = records.parent / "bt" / f"shard-{index}-run{run}"
basetemp.parent.mkdir(parents=True, exist_ok=True)
memlog = records / "memory-bound"
memlog.mkdir(exist_ok=True)

start = time.time()
env = dict(os.environ)
env.pop("PYTHONPATH", None)
env["PYTHONPATH"] = os.pathsep.join([str(worktree / "tools"), str(here)])
env.update(SUITE_DONE=str(done), SUITE_DEADLINE=str(start + soft), SUITE_HARD=str(start + hard),
           SUBFLEET_TEST_MEMORY_LOG=str(memlog))
argv = [str(worktree / ".venv/bin/python"), "-m", "pytest", "-n", "4", "-q", "-p", "no:cacheprovider",
        "-p", "ci_shard_plugin", "--ci-shard", shard, "--timeout=540", "-p", "suite_resume",
        f"--basetemp={basetemp}", "--ignore=tests/frontend/test_core_live.py"]
with open(log, "w", encoding="utf-8") as out:
    out.write(f"# {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(start))} {' '.join(argv)}\n")
    out.flush()
    proc = subprocess.Popen(argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=out,
                            stderr=subprocess.STDOUT, process_group=0)
    how = "exited"
    try:
        proc.wait(timeout=max(1.0, start + hard - time.time()))
    except subprocess.TimeoutExpired:
        how = "interrupted at the hard deadline"
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            how = "killed 30 s after the interrupt"
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
elapsed = time.time() - start

outcomes, failures = collections.Counter(), []
if done.exists():
    for line in done.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        outcomes[rec["outcome"]] += 1
        if rec["outcome"] in ("failed", "error", "crashed", "xpassed") or rec.get("teardown_error"):
            failures.append(rec["nodeid"] + " " + rec["outcome"] + (" +teardown-error" if rec.get("teardown_error") else ""))
collected = {}
if Path(str(done) + ".collect").exists():
    collected = json.loads(Path(str(done) + ".collect").read_text(encoding="utf-8"))
total = collected.get("collected")
finished = sum(outcomes.values())
summary = {"shard": shard, "run": run, "rc": proc.returncode, "how": how, "seconds": round(elapsed),
           "collected": total, "remaining_at_start": collected.get("remaining"), "finished": finished,
           "complete": total is not None and finished >= total, "outcomes": dict(outcomes),
           "failures": failures, "log": str(log)}
with open(records / "runs.jsonl", "a", encoding="utf-8") as out:
    out.write(json.dumps({"started": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(start)), **summary}) + "\n")
print(json.dumps(summary, indent=1))
tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
print("\n".join(tail))
