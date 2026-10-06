"""Run one foreground command in its own process group under a deadline; record the outcome.

Usage: run_slice.py NAME SECONDS -- command...
Only the group this runner started is ever signalled.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path.cwd()
name, seconds = sys.argv[1], int(sys.argv[2])
command = sys.argv[sys.argv.index("--") + 1:]
assert 0 < seconds <= (900 if name.startswith("app-build") else 570)
evidence = root / "review-evidence/pr128-r5"
environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
environment["TMPDIR"] = str(root / ".review-tmp") + "/"
log_path = evidence / "output" / (name + ".log")
start = time.monotonic()
with log_path.open("w") as log:
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment,
                               start_new_session=True)
    print(f"{name}: pid {process.pid}, deadline {seconds}s", flush=True)
    expired = False

    def descendants(root_pid):
        # swift-driver starts each swift-frontend in its own process group, so a
        # group signal alone leaves them orphaned; collect them by parentage.
        table = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout.split()
        children = {}
        for pid, ppid in zip(table[::2], table[1::2]):
            children.setdefault(int(ppid), []).append(int(pid))
        found, stack = [], [root_pid]
        while stack:
            for child in children.get(stack.pop(), []):
                found.append(child)
                stack.append(child)
        return found

    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        expired = True
        for pid in descendants(process.pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
elapsed = round(time.monotonic() - start, 2)
tail = log_path.read_text(errors="replace").splitlines()[-12:]
record = dict(name=name, command=command, seconds=elapsed, deadline=seconds,
              exit_code=process.returncode, timed_out=expired, tail=tail)
with (evidence / "slices.jsonl").open("a") as output:
    output.write(json.dumps(record) + "\n")
print(json.dumps({k: record[k] for k in ("name", "seconds", "exit_code", "timed_out")}), flush=True)
print("\n".join(tail[-4:]), flush=True)
sys.exit(124 if expired else process.returncode)
