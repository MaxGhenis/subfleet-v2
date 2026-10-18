"""D-CEN1, C-5.5: the marker source reads a process's raw argument buffer, so a
process that wrote its title over its argv area is still found by its marker.

`ps -E` lists a process's environment from the same buffer but stops at the first
empty string, and a title written over the argv area leaves that area full of NULs
(incident 2026-10-13: vitest's `node (vitest)` and `node (vitest 1)`). Every process
here is this test's own child, killed and reaped on failure too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from subfleet import procs

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="KERN_PROCARGS2 is macOS's")

#: Write the title and NULs over this process's own argv area, as node's
#: `process.title` does, then say so on stdout.
NUL_TITLE = r"""
import ctypes, sys, time
libc = ctypes.CDLL(None)
libc._NSGetArgv.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
libc._NSGetArgc.restype = ctypes.POINTER(ctypes.c_int)
argc, argv = libc._NSGetArgc().contents.value, libc._NSGetArgv().contents
start, last = argv[0], argv[argc - 1]
end = last + len(ctypes.string_at(last)) + 1
title = b"node (vitest)"
ctypes.memset(start, 0, end - start)
ctypes.memmove(start, title, min(len(title), end - start - 1))
print("ready", flush=True)
time.sleep(60)
"""
NODE_TITLE = "process.title = 'node (vitest)'; console.log('ready'); setTimeout(() => {}, 60000);"


@pytest.fixture(scope="module", autouse=True)
def macos_inspection():
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-5.3 host sandbox denies ps/sysctl; no ownership bypass")


def start(argv: list[str], attempt: str, root: str) -> subprocess.Popen:
    env = {"PATH": "/usr/bin:/bin", "SUBFLEET_ATTEMPT": attempt, "SUBFLEET_JOB": attempt.split("/")[0],
           "SUBFLEET_ROOT": root}
    child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             start_new_session=True)
    try:
        assert child.stdout.readline().strip() == b"ready"
    except BaseException:
        finish(child)
        raise
    return child


def finish(child: subprocess.Popen | None) -> None:
    if child is None:
        return
    if child.poll() is None:
        child.kill()             # this test's own unreaped child: its pid cannot have been reused
    child.wait(timeout=10)
    child.stdout.close()


def ps_shows_marker(pid: int, attempt: str) -> bool:
    """What the census read before D-CEN1, for this pid only; kept in memory, never reported."""
    shown = subprocess.run(["/bin/ps", "-Eww", "-o", "command=", "-p", str(pid)],
                           capture_output=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout
    return f"SUBFLEET_ATTEMPT={attempt}".encode() in shown.split()


@pytest.mark.parametrize("kind", ["nul", "node"])
def test_d_cen1_the_marker_of_a_process_that_wrote_its_title_over_argv_is_found(tmp_path, kind):
    """D-CEN1, C-5.5: regression 1 at the census itself. The title leaves `ps -E` no
    environment to show (checked first, so the fixture is the incident's), and the
    census still counts the process by its marker: no group, no walk, ppid not ours.
    A process of the same root carrying `<attempt>0` is not counted; once the retitled
    process exits, the census is verified empty."""
    node = shutil.which("node")
    if kind == "node" and not node:
        pytest.skip("node is not installed; the nul stand-in covers the same argv layout")
    argv = [node, "-e", NODE_TITLE] if kind == "node" else [sys.executable, "-c", NUL_TITLE]
    attempt, root = f"raw-{uuid.uuid4().hex[:8]}/a1", str(tmp_path)
    retitled = start(argv, attempt, root)
    neighbour = None
    try:
        neighbour = start([sys.executable, "-c", NUL_TITLE], attempt + "0", root)
        assert not ps_shows_marker(retitled.pid, attempt), "the fixture no longer hides from ps -E"
        result = procs.containment(None, None, None, attempt, root=root)
        assert retitled.pid in result.marker_pids, result.to_dict()
        assert neighbour.pid not in result.live_pids
        assert not result.verified_empty
        assert retitled.pid in result.identities and retitled.pid in result.shapes
        assert "vitest" not in str(result.to_dict()) and "SUBFLEET" not in str(result.to_dict())
        finish(retitled)
        deadline = time.monotonic() + 10
        while not procs.containment(None, None, None, attempt, root=root).verified_empty:
            assert time.monotonic() < deadline, "the census did not empty after the process exited"
            time.sleep(.05)
    finally:
        finish(retitled)
        finish(neighbour)


def test_d_cen1_a_marker_from_another_state_root_is_not_counted(tmp_path):
    """C-5.5: the raw read applies the state-root half of the marker, as the `ps` read did."""
    attempt = f"raw-{uuid.uuid4().hex[:8]}/a1"
    other = start([sys.executable, "-c", NUL_TITLE], attempt, str(tmp_path / "other-root"))
    try:
        assert other.pid not in procs.containment(None, None, None, attempt, root=str(tmp_path)).live_pids
        assert other.pid in procs.containment(None, None, None, attempt).marker_pids
    finally:
        finish(other)
