"""A pytest plugin for `tools/footprint_watch.py`: which test each process runs, and its stacks.

With `FOOTPRINT_DIR` set, each pytest process (each xdist worker too) appends the
start and finish of every test to `tests-<pid>.log` there, and registers
`faulthandler` for SIGUSR1, so the watch's first signal writes every thread's Python
stack to `stacks-<pid>.txt` before its SIGKILL. Load it with `-p footprint_stacks`
from a folder on the import path. Standard library only.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import time

_files = []


def _folder() -> str | None:
    return os.environ.get("FOOTPRINT_DIR") or None


def pytest_configure(config):
    folder = _folder()
    if folder is None:
        return
    stacks = open(os.path.join(folder, f"stacks-{os.getpid()}.txt"), "w")
    _files.append(stacks)
    faulthandler.register(signal.SIGUSR1, file=stacks, all_threads=True)


def _note(what: str, nodeid: str) -> None:
    folder = _folder()
    if folder is None:
        return
    with open(os.path.join(folder, f"tests-{os.getpid()}.log"), "a") as log:
        log.write(f"{time.time():.3f} {what} {nodeid}\n")


def pytest_runtest_logstart(nodeid, location):
    _note("start", nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _note("finish", nodeid)
