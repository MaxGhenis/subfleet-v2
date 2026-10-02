#!/usr/bin/env python3
"""Measure what retention costs when one pass cannot size every job before its deadline (C-8.4).

Runs a real `Daemon` with the production tick on a temporary state root, in this
process, over a store of terminal jobs whose directories hold more files than a
pass can `lstat` before `--pass-s`. Reports, for a fixed window: how many passes
began, the seconds spent inside them, and this process's CPU.

The only thing replaced is the deadline handed to `retention.maintenance` (the
daemon's is 60 s; a tree that takes that long to walk is millions of files), so
the same script measures any revision: run it from a checkout of each. Nothing
is launched, the timers are off, and nothing here touches ~/.subfleet.

Usage: uv run python tools/measure_retention_cost.py [--window 150] [--jobs 200] [--files 1000] [--pass-s 1]
"""

from __future__ import annotations

import argparse
import resource
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet import daemon as daemon_module  # noqa: E402
from subfleet.adapters.registry import register  # noqa: E402
from subfleet.daemon import Daemon  # noqa: E402
from tests.fake.conftest import Harness  # noqa: E402
from tests.fake_adapter import FakeAdapter  # noqa: E402


def cpu() -> float:
    own = resource.getrusage(resource.RUSAGE_SELF)
    return own.ru_utime + own.ru_stime


def measure(window_s: float = 150, jobs: int = 200, files: int = 1000, pass_s: float = 1,
            retention: bool = True) -> dict:
    with tempfile.TemporaryDirectory(prefix="sfr-", dir="/tmp") as directory:
        root = Path(directory)
        Harness(root)
        register("codex", FakeAdapter)
        daemon = Daemon(root, desktop_prober=lambda: None)
        daemon._launch = lambda a: None
        daemon.timers.intervals.clear()
        for index in range(jobs):
            job_id = f"20260901-000000-history-{index}"
            daemon.store.add_job(job_id=job_id, request_id=f"history-{index}", payload_digest="digest", kind="run",
                                 state="succeeded", workdir=str(root), prompt_path=str(root / "prompt.md"),
                                 sandbox="read-only")
            folder = root / "jobs" / job_id / "a1"
            folder.mkdir(parents=True, exist_ok=True)
            for name in range(files):
                (folder / f"f{name}").touch()
        passes: list[float] = []
        real = daemon_module.maintenance

        def bounded(*args, **kwargs):
            began = time.monotonic()
            try:
                return real(*args, **{**kwargs, "deadline": began + pass_s})
            finally:
                passes.append(time.monotonic() - began)

        daemon_module.maintenance = bounded
        # Due on the first tick, as an hour after the daemon started; or never.
        daemon._last_maintenance = time.monotonic() - (3600 if retention else -10 * window_s)
        server = threading.Thread(target=daemon.serve_forever, daemon=True)
        before, started = cpu(), time.monotonic()
        server.start()
        try:
            time.sleep(window_s)
            elapsed, used = time.monotonic() - started, cpu() - before
        finally:
            daemon_module.maintenance = real
            daemon.stopping.set()
            server.join(timeout=30)
    return {"window_s": elapsed, "passes": len(passes), "in_passes_s": sum(passes), "cpu_s": used}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window", type=float, default=150, help="seconds measured (default 150)")
    parser.add_argument("--jobs", type=int, default=200, help="terminal jobs in the store (default 200)")
    parser.add_argument("--files", type=int, default=1000, help="files in each job directory (default 1000)")
    parser.add_argument("--pass-s", type=float, default=1, help="the pass's deadline in seconds (default 1)")
    args = parser.parse_args(argv)
    idle = measure(args.window, args.jobs, args.files, args.pass_s, retention=False)
    busy = measure(args.window, args.jobs, args.files, args.pass_s)
    print("| state | passes | seconds in passes | daemon CPU |\n|---|---:|---:|---:|")
    print(f"| no pass due | {idle['passes']} | {idle['in_passes_s']:.1f} | "
          f"{100 * idle['cpu_s'] / idle['window_s']:.1f}% |")
    print(f"| a pass due, {args.jobs * args.files} files, {args.pass_s:g} s deadline | {busy['passes']} | "
          f"{busy['in_passes_s']:.1f} | {100 * busy['cpu_s'] / busy['window_s']:.1f}% |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
