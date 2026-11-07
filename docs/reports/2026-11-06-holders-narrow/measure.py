"""Alternate whole-machine listings by `lsof` and by the narrow reader, serially.

usage: python measure.py OUT.jsonl ROUND [ROUND ...]

Each round runs both readers under the retention clamp (`taskpolicy -c utility`),
the order alternating by round, and appends one JSON line per listing to OUT: the
load (`os.getloadavg`, what `uptime` prints), the seconds, and counts only. No
path or command of any process is kept. Before each listing it waits up to two
minutes for any other `lsof` to end, and skips that listing if one still runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
READER = HERE.parent / "retention_vnodes.py"         # the prototype reader, kept with its measurements
TASKPOLICY = "/usr/sbin/taskpolicy"
LSOF = ["/usr/sbin/lsof", "-n", "-P", "-w", "-F", "pcftaDin"]


def other_lsof() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-x", "lsof"], capture_output=True).returncode == 0


def wait_quiet(limit: float = 120.0) -> float | None:
    """Seconds waited for no other lsof, or None if one still ran after `limit`."""
    began = time.monotonic()
    while other_lsof():
        if time.monotonic() - began >= limit:
            return None
        time.sleep(5)
    return time.monotonic() - began


def lsof_counts(out: bytes) -> dict[str, int]:
    counts = {"bytes": len(out), "processes": 0, "entries": 0, "descriptors": 0, "mapped": 0}
    for line in out.split(b"\n"):
        if line[:1] == b"p":
            counts["processes"] += 1
        elif line[:1] == b"f":
            counts["entries"] += 1
            value = line[1:]
            if value.isdigit():
                counts["descriptors"] += 1
            elif value in (b"txt", b"mem", b"mmap", b"ltx", b"mxx", b"m86", b"tr"):
                counts["mapped"] += 1
    return counts


def narrow_counts(out: bytes) -> dict[str, float]:
    last = out.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode()
    if not last.startswith("#end "):
        return {"malformed": 1}
    counts: dict[str, float] = {"bytes": len(out)}
    for item in last[5:].split():
        key, value = item.split("=", 1)
        counts[key] = float(value) if "." in value else int(value)
    return counts


def listing(reader: str, probe: str) -> dict:
    if reader == "lsof":
        argv = [TASKPOLICY, "-c", "utility", *LSOF]
    else:
        argv = [TASKPOLICY, "-c", "utility", sys.executable, "-I", "-S", str(READER), "--path", probe]
    load = os.getloadavg()
    began = time.perf_counter()
    done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL)
    seconds = time.perf_counter() - began
    counts = lsof_counts(done.stdout) if reader == "lsof" else narrow_counts(done.stdout)
    return {"reader": reader, "seconds": round(seconds, 3), "load_1m": round(load[0], 2),
            "load_5m": round(load[1], 2), "rc": done.returncode, **counts}


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    probe = tempfile.gettempdir()
    for round_ in map(int, argv[1:]):
        order = ("lsof", "narrow") if round_ % 2 == 0 else ("narrow", "lsof")
        for reader in order:
            waited = wait_quiet()
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if waited is None:
                row = {"round": round_, "reader": reader, "at": stamp, "skipped": "another lsof ran for 2 minutes"}
            else:
                row = {"round": round_, "at": stamp, "waited_s": round(waited, 1), **listing(reader, probe)}
            with out.open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
