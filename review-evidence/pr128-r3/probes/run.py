"""Foreground slice runner: one command, a hard deadline, its own process group only.

usage: run.py NAME SECONDS -- cmd...
Writes $R3_TMP/slices/NAME.log and appends a JSON line to review-evidence/pr128-r3/slices.jsonl.
"""
import json
import os
import signal
import subprocess
import sys
import time

name, seconds = sys.argv[1], float(sys.argv[2])
cmd = sys.argv[sys.argv.index("--") + 1:]
tmp = os.environ["R3_TMP"]
os.makedirs(f"{tmp}/slices", exist_ok=True)
log_path = f"{tmp}/slices/{name}.log"
env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
load = os.getloadavg()
start = time.monotonic()
with open(log_path, "w") as log:
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    print(f"pid {proc.pid}", flush=True)
    timed_out = False
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
elapsed = round(time.monotonic() - start, 2)
tail = open(log_path, errors="replace").read().splitlines()[-12:]
record = {"name": name, "exit_code": proc.returncode, "timeout": timed_out, "seconds": elapsed,
          "load_at_start": [round(x, 1) for x in load], "tail": tail}
with open(os.environ["R3_SLICES"], "a") as f:
    f.write(json.dumps(record) + "\n")
print(json.dumps(record, indent=1))
