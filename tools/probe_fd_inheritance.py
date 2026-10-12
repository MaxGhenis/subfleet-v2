#!/usr/bin/env python3
"""Count how often a process started as `procs._read` starts `ps` inherits another thread's new pipe (C-5.12).

macOS has no `pipe2` and no `SOCK_CLOEXEC`, so `os.pipe()` is `pipe()` and then
`fcntl(FD_CLOEXEC)`: for an instant the new descriptors are inheritable. Two
threads make and close pipes (as every `subprocess.run(capture_output=True)` in
the daemon does); the main thread starts a child with `procs._read`'s arguments
(`capture_output`, `close_fds` as given) that lists its own descriptors. A child
that lists more than the baseline inherited one. With `close_fds=True`, CPython
on macOS forks and closes them; with `False` it uses `posix_spawn`.

Bounded by `--seconds` and `--max-spawns` for each setting; prints one line per
setting, counts only. Touches nothing outside this process and its children.

Usage: uv run python tools/probe_fd_inheritance.py [--seconds 4] [--max-spawns 200]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import threading
import time

LIST = ["/bin/sh", "-c", "ls /dev/fd"]          # `ls` holds one descriptor of its own for the listing
ENV = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"}


def spawn(close_fds: bool) -> list[str]:
    return subprocess.run(LIST, capture_output=True, text=True, timeout=10, env=ENV, close_fds=close_fds).stdout.split()


def measure(close_fds: bool, seconds: float, max_spawns: int) -> dict:
    baseline = spawn(close_fds)
    stop = threading.Event()
    made = [0, 0]

    def churn(slot: int) -> None:
        while not stop.is_set():
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            os.close(write_fd)
            made[slot] += 1
    threads = [threading.Thread(target=churn, args=(slot,), daemon=True) for slot in range(2)]
    for thread in threads:
        thread.start()
    spawns = inherited = 0
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and spawns < max_spawns:
            listed = spawn(close_fds)
            spawns += 1
            inherited += len(listed) > len(baseline)
    finally:
        stop.set()
        for thread in threads:
            thread.join(5)
    return {"close_fds": close_fds, "posix_spawn": subprocess._USE_POSIX_SPAWN, "baseline": baseline,
            "spawns": spawns, "inherited": inherited, "pipes_made": sum(made)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=4, help="seconds for each setting (default 4)")
    parser.add_argument("--max-spawns", type=int, default=200, help="most children for each setting")
    args = parser.parse_args(argv)
    for close_fds in (False, True):
        result = measure(close_fds, args.seconds, args.max_spawns)
        print(f"close_fds={result['close_fds']} posix_spawn_available={result['posix_spawn']} "
              f"baseline_fds={result['baseline']} spawns={result['spawns']} "
              f"inherited={result['inherited']} pipes_made={result['pipes_made']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
