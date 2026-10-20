"""Measure C-6.13 signals and per-pass cost without starting any subprocess.

Run through the hub suite-hold wrapper, with the GIL CPython 3.14 environment.
This observes existing host activity; it creates no storage or CPU workload.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subfleet import machine  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--repeats", type=int, default=100)
    args = parser.parse_args()
    if not (2 <= args.samples <= 60 and 0 < args.interval <= 10 and 1 <= args.repeats <= 1000):
        parser.error("samples 2..60, interval (0,10], repeats 1..1000")
    sampler = machine.Sampler()
    first = sampler.read()
    print(f"cold read: {first['read_cost_s'] * 1e6:.1f} us", flush=True)
    rows = []
    for index in range(args.samples):
        time.sleep(args.interval)
        row = sampler.read()
        rows.append(row)
        print(f"sample {index + 1}: idle={row['cpu_idle']} disk={row['disk_busy']} "
              f"load1={row['load1']} cpus={row['cpus']} cost_us={row['read_cost_s'] * 1e6:.1f}", flush=True)
    for key in ("cpu_idle", "disk_busy"):
        values = [row[key] for row in rows if row[key] is not None]
        print(f"{key}: readable={len(values)}/{len(rows)} "
              + (f"min={min(values):.6f} median={statistics.median(values):.6f} max={max(values):.6f}"
                 if values else "unavailable"), flush=True)
    for label, function in (("cpu_ticks", machine._cpu_ticks), ("disk_counters", machine._disk_counters),
                            ("full_read", machine.Sampler().read)):
        costs = []
        for _ in range(args.repeats):
            started = time.perf_counter()
            function()
            costs.append((time.perf_counter() - started) * 1e6)
        print(f"{label}: N={args.repeats} best_us={min(costs):.1f} "
              f"median_us={statistics.median(costs):.1f} max_us={max(costs):.1f}", flush=True)


if __name__ == "__main__":
    main()
