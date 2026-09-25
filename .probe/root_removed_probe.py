"""Probe: a run (no fence) whose root is removed while it reads; print its exit code,
stderr, and whether the root came back. Usage: python .probe/root_removed_probe.py"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROLLOUT = "rollout-2026-09-25T00-00-00-0f0e0d0c-1111-2222-3333-444455556666.jsonl"

base = Path(tempfile.mkdtemp(prefix="pr47-probe-"))
home = base / "home"
(home / ".claude" / "projects").mkdir(parents=True)
day = home / ".codex" / "sessions" / "2026" / "09" / "25"
day.mkdir(parents=True)
fifo = day / ROLLOUT
os.mkfifo(fifo)
root = base / "state"
root.mkdir()
writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
os.write(writer, b"\n")
env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO)}
env.pop("SUBFLEET_CLAUDE_DIR", None)
started = time.monotonic()
process = subprocess.Popen([sys.executable, "-m", "subfleet.conversations.catalog", "--state-root", str(root)],
                           cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, start_new_session=True)
while select.select([writer], [], [], 0)[0]:
    if time.monotonic() - started > 30:
        raise SystemExit("the run never read the FIFO")
    time.sleep(.02)
print(f"run read the FIFO after {time.monotonic() - started:.2f} s; removing root")
shutil.rmtree(root)
os.close(writer)
out, err = process.communicate(timeout=30)
print("exit", process.returncode)
print("stderr tail:", err.decode()[-600:])
print("root exists after:", root.exists(), sorted(os.listdir(root)) if root.exists() else None)
shutil.rmtree(base, ignore_errors=True)
