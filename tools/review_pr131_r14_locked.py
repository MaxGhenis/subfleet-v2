"""Foreground review runner: one lock hold, bounded queue and wall time."""
import os
from pathlib import Path
import signal
import subprocess
import sys

log_path = Path(sys.argv[1])
with log_path.open("w") as output:
    child = subprocess.Popen(
        ["/usr/bin/lockf", "-t", os.environ.get("SF_REVIEW_LOCK_WAIT_S", "45"), "-k",
         "/private/tmp/claude-501/subfleet-suites.lock",
         "uv", "run", "pytest", "-q", *sys.argv[2:]],
        stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    try:
        result = child.wait(timeout=1100)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        result = 124
print(log_path.read_text())
sys.exit(result)
