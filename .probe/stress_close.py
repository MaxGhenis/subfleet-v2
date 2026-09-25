"""Stress probe: a real Daemon closed at a random moment of its first catalog run.

For each iteration: an isolated HOME holding a few ordinary transcripts and rollouts,
a fresh state root under /tmp, `Daemon.serve_forever` on a thread, `stopping.set()`
after a seeded random delay, then (after close returns) a `ps` sweep for catalog runs
on that root, `rmtree(root)`, a 1.5 s grace, and a check that nothing recreated it.

Usage: python .probe/stress_close.py <repo> <iterations> [seed]
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
iterations = int(sys.argv[2])
seed = int(sys.argv[3]) if len(sys.argv) > 3 else 0
home = Path(tempfile.mkdtemp(prefix="pr47-home-"))
projects = home / ".claude" / "projects" / "-tmp-x"
projects.mkdir(parents=True)
for n in range(40):
    (projects / f"{n:08d}-0000-0000-0000-000000000000.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/Users/x", "message": {"content": f"hello {n}"}}) + "\n")
day = home / ".codex" / "sessions" / "2026" / "09" / "25"
day.mkdir(parents=True)
for n in range(40):
    (day / f"rollout-2026-09-25T00-00-00-{n:08d}-1111-2222-3333-444455556666.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": f"{n:08d}-1111-2222-3333-444455556666",
                                                        "cwd": "/Users/x"}}) + "\n")
os.environ["HOME"] = str(home)
os.environ.pop("SUBFLEET_CLAUDE_DIR", None)
os.environ["PYTHONPATH"] = str(repo)
sys.path.insert(0, str(repo))
os.chdir(repo)

from subfleet import daemon as daemon_module  # noqa: E402

daemon_module.procs.boot_id = lambda: "pr47-stress-boot"
daemon_module.procs.proc_start = lambda pid: "pr47-stress-start"


def catalog_pids(root: Path) -> list[int]:
    out = subprocess.run(["/bin/ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=30).stdout
    spelled = "|".join(re.escape(str(r)) for r in {root, root.resolve()})
    pattern = re.compile(rf"subfleet\.conversations\.catalog --state-root ({spelled})(\s|$)")
    return [int(line.split(None, 1)[0]) for line in out.splitlines() if pattern.search(line)]


rng = random.Random(seed)
bad = 0
outcomes: dict[str, int] = {}
for i in range(iterations):
    root = Path(tempfile.mkdtemp(prefix="pr47s-", dir="/tmp"))
    daemon = daemon_module.Daemon(root, tick_s=.05)
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    delay = rng.uniform(0, 2.5)
    time.sleep(delay)
    had_proc = daemon.conversations._catalog_proc is not None
    daemon.stopping.set()
    t0 = time.monotonic()
    thread.join(60)
    took = time.monotonic() - t0
    stray = catalog_pids(root)
    wrote = (root / "catalog.json").exists()
    problems = []
    if thread.is_alive():
        problems.append("daemon did not close")
    if stray:
        problems.append(f"catalog runs outlived close: {stray}")
    try:
        shutil.rmtree(root)
    except OSError as exc:
        problems.append(f"rmtree failed: {exc}")
    time.sleep(1.5)
    for pid in stray:
        deadline = time.monotonic() + 30
        while pid in catalog_pids(root) and time.monotonic() < deadline:
            time.sleep(.1)
    if root.exists():
        problems.append(f"root came back holding {sorted(os.listdir(root))}")
        shutil.rmtree(root, ignore_errors=True)
    key = f"proc_at_close={had_proc} catalog_before_rmtree={wrote}"
    outcomes[key] = outcomes.get(key, 0) + 1
    print(f"[{i:02d}] delay={delay:.2f}s close={took:.2f}s {key} {'; '.join(problems) or 'ok'}", flush=True)
    bad += bool(problems)
shutil.rmtree(home, ignore_errors=True)
print("outcomes:", outcomes)
print(f"{bad}/{iterations} iterations had problems")
