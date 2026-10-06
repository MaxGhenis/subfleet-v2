"""Run one foreground pytest slice with a deadline below ten minutes."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
name, *args = sys.argv[1:]
env = dict(os.environ)
env["TMPDIR"] = str(REPO / ".review-tmp") + "/"
env["GIT_CEILING_DIRECTORIES"] = str(REPO / ".review-tmp")
env.pop("PYTHONPATH", None)
for directory in (HERE / "logs", HERE / "junit"):
    directory.mkdir(exist_ok=True)
command = [str(REPO / ".venv/bin/python"), "-m", "pytest", "-q", "-s",
           "-p", "no:cacheprovider", "-rfsE",
           "--junitxml=" + str(HERE / "junit" / (name + ".xml")), *args]
started = time.monotonic()
log = HERE / "logs" / (name + ".log")
with log.open("w") as output:
    proc = subprocess.Popen(command, cwd=REPO, env=env, stdout=output,
                            stderr=subprocess.STDOUT, start_new_session=True)
    print(f"{name}: owned pid={proc.pid}; deadline=540s", flush=True)
    expired = False
    try:
        rc = proc.wait(timeout=540)
    except subprocess.TimeoutExpired:
        expired = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            rc = proc.wait()
summary = [line for line in log.read_text().splitlines()
           if line.startswith(("FAILED", "ERROR", "SKIPPED"))
           or " passed" in line or " failed" in line or " error" in line
           or " skipped" in line]
print(f"{name}: rc={rc}; expired={expired}; wall={time.monotonic()-started:.2f}s")
print("\n".join(summary[-35:]), flush=True)
sys.exit(rc if rc >= 0 else 128 - rc)
