#!/usr/bin/env python3
"""Measure inventory throughput with busy Python threads sharing the GIL.

Every record and mirror state lives in temporary scratch, never the desktop
store. --revision loads that revision's mirror module without changing files
or git history. Compare with identical arguments and the production Python:

    .venv/bin/python tools/measure_mirror_inventory.py --revision 03de432df957
    .venv/bin/python tools/measure_mirror_inventory.py

Cold scans parse all files; sweep scans stat cached files. Each measured scan
has --busy-threads pure-Python workers running concurrently in the process.
The benchmark exercises _scan directly so flag publication, progress sidecar
writes, and the separate hot/full scheduling decision do not mask inventory
costs. It leaves the interpreter's switch interval unchanged.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def load_mirror(revision, checkout):
    sys.path.insert(0, str(checkout))
    if revision is None:
        from subfleet.sessions import mirror
        return mirror
    source = subprocess.check_output(
        ["git", "show", f"{revision}:subfleet/sessions/mirror.py"], cwd=checkout, text=True)
    name = "subfleet.sessions._benchmark_mirror"
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, f"{revision}:subfleet/sessions/mirror.py", "exec"), module.__dict__)
    return module


def measure(scan, count):
    stop = threading.Event()
    ready = threading.Barrier(count + 1)

    def busy():
        ready.wait()
        value = 0
        while not stop.is_set():
            for _ in range(1000):
                value = (value + 1) % 1_000_003

    workers = [threading.Thread(target=busy) for _ in range(count)]
    for worker in workers:
        worker.start()
    ready.wait()
    # Let every runnable worker compete before timing, including short warm
    # sweeps that could otherwise finish before any busy worker got the GIL.
    if workers:
        time.sleep(0.1)
    try:
        started = time.perf_counter()
        scanned = scan()
        elapsed = time.perf_counter() - started
    finally:
        stop.set()
        for worker in workers:
            worker.join()
    return {"seconds": round(elapsed, 6), "entries": scanned,
            "entries_per_second": round(scanned / elapsed, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="baseline git revision; default: working tree")
    parser.add_argument("--entries", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--busy-threads", type=int, default=3)
    parser.add_argument("--scratch", type=Path, help="parent directory for temporary fixture")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.entries < 1 or args.rounds < 1 or args.busy_threads < 0:
        parser.error("entries/rounds must be positive and busy-threads nonnegative")
    checkout = Path(__file__).resolve().parents[1]
    mirror = load_mirror(args.revision, checkout)
    samples = {"cold": [], "sweep": []}
    with tempfile.TemporaryDirectory(prefix="mirror-inventory-", dir=args.scratch) as scratch:
        root = Path(scratch)
        folder = root / "store" / "account" / "org"
        folder.mkdir(parents=True)
        for n in range(args.entries):
            # Repeated metadata approximates the 12 KB desktop records. The
            # projection drops that metadata, as it does in the real store.
            payload = {"cliSessionId": f"session-{n}", "title": f"Session {n}",
                       "isArchived": False, "isStarred": False,
                       "sessionSettings": {"ultracode": True}, "metadata": "x" * 12_000}
            (folder / f"local_{n:06}.json").write_text(json.dumps(payload))
        for _ in range(args.rounds):
            running = mirror.Mirror(root / "state")

            def scan():
                current = mirror.Pass("benchmark", kind="hot")
                running._scan(folder, current, sweep=True)
                return current.entries_scanned

            for phase in samples:
                result = measure(scan, args.busy_threads)
                samples[phase].append(result)
                print(json.dumps({"phase": phase, "round": len(samples[phase]), **result}),
                      flush=True)
    result = {"revision": args.revision or "working-tree", "python": sys.version,
              "platform": platform.platform(), "switch_interval_s": sys.getswitchinterval(),
              "busy_threads": args.busy_threads, "entries": args.entries,
              "rounds": args.rounds, "samples": samples,
              "median_entries_per_second": {
                  phase: statistics.median(row["entries_per_second"] for row in rows)
                  for phase, rows in samples.items()}}
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
