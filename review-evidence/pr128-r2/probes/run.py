"""Foreground slice runner: one command, a hard deadline, its own process group only.

usage: run.py NAME SECONDS -- cmd...
Writes /tmp/pr128r2/slices/NAME.log and appends a JSON line to slices.jsonl.
"""
import json
import os
import signal
import subprocess
import sys
import time

name, seconds = sys.argv[1], float(sys.argv[2])
cmd = sys.argv[sys.argv.index("--") + 1:]
os.makedirs("/tmp/pr128r2/slices", exist_ok=True)
log_path = f"/tmp/pr128r2/slices/{name}.log"
env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
start = time.monotonic()
with open(log_path, "w") as log:
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
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
record = {"name": name, "exit_code": proc.returncode, "timeout": timed_out, "seconds": elapsed, "tail": tail}
with open("/tmp/pr128r2/slices.jsonl", "a") as f:
    f.write(json.dumps(record) + "\n")
print(json.dumps(record, indent=1))
