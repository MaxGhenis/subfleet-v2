#!/usr/bin/env python3
"""Reproduce the 2026-09-27 census failure: a `ps -axEww` read in a process like the daemon (C-5.5, C-5.12).

The daemon quarantined probes on "marker enumeration unavailable" while the same census, taken by a separate
process, came back verified empty in 0.07 s. What differs inside the daemon is how it is scheduled: its threads
run at priority 20 (the `utility` QoS; a login shell's run at 31), and it has some 140 threads that want the
interpreter lock. This tool puts one process in that position and reads the census's marker source there, two
ways, interleaved so both meet the same load:

- `pipe`: the reader 2.1.7 shipped (`subprocess.run(capture_output=True, text=True, timeout=10)`), whose 1.3 MB
  answer comes back through a 16 to 64 KiB pipe, one interpreter-lock wait per piece, while `ps` cannot exit;
- `socket`: `subfleet.procs._read`, whose reader writes its whole answer into an 8 MiB socket buffer and exits,
  with the 10 s cap on the reader rather than on this process's reading.

`--hogs N` starts N pure-Python threads that contend for the interpreter lock, standing in for the daemon's busy
workers; `--qos utility` re-runs this tool under `taskpolicy -c utility`, the daemon's scheduling. The machine's
own load is whatever it is: `--load M` adds M busy child processes this tool owns and kills at the end.

Only sizes, times, read counts and exception types are printed. The answer is parsed for pids and dropped; no
command or environment text is kept or shown (C-5.5).

Usage: uv run python tools/census_under_load.py [--hogs 4] [--seconds 60] [--qos utility] [--load 0]
Measured 2026-09-27 at load near 120 with `--hogs 4 --qos utility`: pipe p50 10.5 s, 4 of 7 past the 10 s cap,
43 reads; socket p50 3.3 s, 0 of 7, 3 reads. `ps` alone took 0.17 s at that QoS.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subfleet import procs  # noqa: E402

MARKER_ARGV = ["/bin/ps", "-axEww", "-o", "pid=,command="]
ENV = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"}


def pipe_read(argv: list[str]) -> str:
    """The reader as 2.1.7 shipped it (`procs._read` before 2026-09-27), for comparison."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=ENV, close_fds=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise procs.InspectionError(f"{os.path.basename(argv[0])} inspection unavailable") from exc
    if result.returncode:
        raise procs.InspectionError(f"{os.path.basename(argv[0])} inspection failed ({result.returncode})")
    return result.stdout


def hog(stop: threading.Event) -> None:
    count = 0
    while not stop.is_set():
        for _ in range(10_000):
            count += 1


def spinner() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "while True: pass"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def measure(args) -> dict:
    reads = {"n": 0}
    real_read = os.read

    def counted(fd, n):
        if threading.current_thread() is threading.main_thread():
            reads["n"] += 1
        return real_read(fd, n)

    stop = threading.Event()
    hogs = [threading.Thread(target=hog, args=(stop,), daemon=True) for _ in range(args.hogs)]
    for thread in hogs:
        thread.start()
    children = [spinner() for _ in range(args.load)]
    samples: dict[str, list[tuple[float, str, int, int]]] = {"pipe": [], "socket": []}
    readers = {"pipe": pipe_read, "socket": procs._read}
    end = time.monotonic() + args.seconds
    os.read = counted
    try:
        while time.monotonic() < end:
            for name, reader in readers.items():
                reads["n"] = 0
                started = time.monotonic()
                status, size = "ok", 0
                try:
                    text = reader(MARKER_ARGV)
                    size = len(text)
                    for row in text.splitlines():
                        int(row.strip().partition(" ")[0])
                except procs.InspectionError as exc:
                    cause = exc.__cause__
                    status = type(cause).__name__ if cause is not None else str(exc).split(":")[0]
                except ValueError as exc:
                    status = type(exc).__name__
                samples[name].append((time.monotonic() - started, status, reads["n"], size))
    finally:
        os.read = real_read
        stop.set()
        for child in children:
            child.kill()
        for child in children:
            child.wait()
    summary = {"hogs": args.hogs, "load_children": args.load, "qos": args.qos,
               "loadavg": [round(value, 1) for value in os.getloadavg()], "readers": {}}
    for name, rows in samples.items():
        if not rows:
            continue
        times = sorted(row[0] for row in rows)
        summary["readers"][name] = {
            "n": len(rows),
            "p50_s": round(times[len(times) // 2], 3),
            "max_s": round(times[-1], 3),
            "failed": sum(row[1] != "ok" for row in rows),
            "failures": sorted({row[1] for row in rows if row[1] != "ok"}),
            "reads_p50": sorted(row[2] for row in rows)[len(rows) // 2],
            "chars_p50": sorted(row[3] for row in rows)[len(rows) // 2],
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hogs", type=int, default=4, help="threads contending for the interpreter lock")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--qos", choices=["inherit", "utility"], default="inherit",
                        help="utility: run under `taskpolicy -c utility`, as launchd runs the daemon")
    parser.add_argument("--load", type=int, default=0, help="busy child processes to add (killed at the end)")
    args = parser.parse_args(argv)
    if args.qos == "utility" and not os.environ.get("SUBFLEET_CENSUS_QOS"):
        command = ["/usr/sbin/taskpolicy", "-c", "utility", sys.executable, os.path.abspath(__file__),
                   *(argv if argv is not None else sys.argv[1:])]
        return subprocess.run(command, env={**os.environ, "SUBFLEET_CENSUS_QOS": "utility"}).returncode
    print(json.dumps(measure(args), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
