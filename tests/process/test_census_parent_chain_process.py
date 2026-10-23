"""D-KILL4, C-5.5: the parent-chain rule on this host's real processes.

A real `/bin/sh` loop (`tests/bin/fakeprov --driver --detached`), in a session of its
own with ppid 1, respawns a marked child. Its own markers are hidden from the
census's reader (`tests/hidden_environment.py`), as macOS 26 hides a shell's. The
census reads the loop's uid and cwd in process: with its cwd in the job's worktree it
is added, elsewhere it is held. Only this test's own processes are started; each is
stopped by its recorded identity, and the loop ends by itself within 90 s.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from subfleet import procs
from tests.hidden_environment import HidingKernel

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS in-process reads")

FAKEPROV = Path(__file__).resolve().parents[1] / "bin" / "fakeprov"
ATTEMPT = "20261022-000000-chain-process/a1"


def start_loop(tmp_path: Path, cwd: Path) -> procs.ProcessIdentity:
    marker = tmp_path / f"loop-{cwd.name}.pid"
    env = {**os.environ, "SUBFLEET_ATTEMPT": ATTEMPT, "SUBFLEET_ROOT": str(tmp_path / "state"),
           "SUBFLEET_FAKE_MARKER": str(marker)}
    subprocess.run([sys.executable, str(FAKEPROV), "--driver", "--detached"], cwd=cwd, env=env,
                   stdin=subprocess.DEVNULL, check=True, timeout=30)
    until = time.monotonic() + 30
    while not (marker.exists() and marker.read_text().strip()):
        assert time.monotonic() < until, "the loop never published its pid"
        time.sleep(.02)
    loop = procs.identity(int(marker.read_text()))
    assert loop is not None
    return loop


def stop(identity: procs.ProcessIdentity | None) -> None:
    if identity is not None:
        procs.signal_process(identity, signal.SIGKILL)


def census_of(tmp_path, loop, worktree, monkeypatch):
    monkeypatch.setattr(procs, "kernel", HidingKernel(procs.kernel, lambda: {loop.pid}))
    rule = procs.ParentChain(str(worktree), frozenset({0, 1, os.getpid(), os.getppid()}), os.geteuid())
    until = time.monotonic() + 10
    while True:
        found = procs.containment(None, None, None, ATTEMPT, root=str(tmp_path / "state"), parent_chain=rule)
        if found.marker_pids or time.monotonic() > until:
            return found
        time.sleep(.05)                     # between two children


@pytest.mark.parametrize("where", ["worktree", "subfolder", "elsewhere"])
def test_a_real_hidden_shell_loop_is_added_by_its_cwd_or_held(tmp_path, monkeypatch, where):
    worktree = tmp_path / "worktrees" / "job"
    (worktree / "src").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cwd = {"worktree": worktree, "subfolder": worktree / "src", "elsewhere": elsewhere}[where]
    loop = None
    try:
        loop = start_loop(tmp_path, cwd)
        assert procs.snapshot().rows[loop.pid][0] == 1                 # reparented to launchd
        found = census_of(tmp_path, loop, worktree, monkeypatch)
        assert found.marker_pids, found.to_dict()
        assert loop.pid not in found.marker_pids                        # its markers are hidden
        record = procs.kernel.process(loop.pid)
        assert record is not None and record.uid == os.geteuid()       # the real kernel record
        if where == "elsewhere":
            assert loop.pid in found.held_pids and loop.pid not in found.live_pids
            assert not found.containment_empty
        else:
            assert loop.pid in found.chain_pids and loop.pid in found.live_pids
            assert found.identities[loop.pid] == loop
            assert any(reason.endswith("cwd in worktree") for reason in found.to_dict()["chain"])
    finally:
        stop(loop)
