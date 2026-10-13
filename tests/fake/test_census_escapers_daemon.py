"""D-CEN1, C-5.5: an attempt's processes that renamed themselves or erased their
environment are still found, and nothing else is.

Incident, 2026-10-13: attempt `20261013-015921-pr213-suite-rerun-inplace/a1` left
a vitest group behind in its in-place worktree: vitest 19935 (`node (vitest)`),
its worker 21910 (`node (vitest 1)`) and esbuild 21192, in a group of their own
with ppid 1. The census found only esbuild, by its marker. Node's title had
filled the argv area of the other two with NULs, which ends `ps -E`'s
environment list, so their markers were invisible, and they were outside the
attempt's group and off the descendant walk. Without esbuild the census would
have been verified empty and the leases released with vitest still running.

Each test here runs a real attempt through a real daemon, guardian and fake
provider (`tests/bin/fakeprov`), whose escaper is born outside the attempt's group
(its parent called setsid and exited), hides from `ps -E`, and outlives the exit
receipt. Every process a test starts is stopped by its recorded identity, on
failure too, and each escaper exits by itself within a minute.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import uuid

import pytest

from subfleet import procs

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="C-5.5's census reads macOS process state")

NODE = shutil.which("node")
TERMINAL = {"quarantined", "succeeded", "failed", "lost", "interrupted"}


def submit_escaper(daemon, scenario: str, *, hold_s: float = 0) -> tuple[str, Path]:
    """Submit `scenario`, whose provider stays `hold_s` after its escaper is ready;
    returns the job id and the file its escaper's pid lands in."""
    args = daemon.submit_args(scenario)
    marker = daemon.root / f"escaper-{uuid.uuid4().hex[:8]}.pid"
    settings = {"scenario": scenario, "delay_s": hold_s, "marker": str(marker)}
    if NODE:
        settings["node"] = NODE
    Path(args["prompt_path"]).write_text(json.dumps(settings))
    return daemon.call("submit", **args)["job_id"], marker


def escaper_identity(daemon, marker: Path) -> procs.ProcessIdentity:
    daemon.until(lambda: marker.exists() and marker.read_text().strip(), timeout=60)
    found = procs.identity(int(marker.read_text()))
    assert found is not None, "the escaper exited before the test could record it"
    return found


def settled(daemon, job_id: str) -> dict:
    """The attempt once it is quarantined or ended; the C-5.9 window comes first."""
    def done():
        attempts = daemon.attempts(job_id)
        return attempts[-1] if attempts and attempts[-1]["state"] in TERMINAL else None
    return daemon.until(done, timeout=90)


def stop(identity: procs.ProcessIdentity | None) -> None:
    """SIGKILL a process this test started, only while it is still that process (C-5.3)."""
    if identity is None:
        return
    procs.signal_process(identity, signal.SIGKILL)
    until = time.monotonic() + 10
    while procs.liveness(identity.pid, identity.boot_id, identity.proc_start) == "alive":
        assert time.monotonic() < until, f"escaper {identity.pid} outlived SIGKILL"
        time.sleep(.05)


def spawn_own_responsibility(argv: list[str], env: dict[str, str]) -> int:
    """posix_spawn with responsibility disclaimed: a process that is its own responsible
    process, as an unrelated app or launchd job is. The test's own copy of the SPI call,
    independent of the daemon's."""
    libc = ctypes.CDLL(None, use_errno=True)
    disclaim = getattr(libc, "responsibility_spawnattrs_setdisclaim", None)
    if disclaim is None:
        pytest.skip("this macOS has no responsibility_spawnattrs_setdisclaim")
    attr = ctypes.c_void_p()
    assert libc.posix_spawnattr_init(ctypes.byref(attr)) == 0
    try:
        disclaim.argtypes = [ctypes.c_void_p, ctypes.c_int]
        assert disclaim(ctypes.byref(attr), 1) == 0
        pid = ctypes.c_int()
        args = (ctypes.c_char_p * (len(argv) + 1))(*[os.fsencode(a) for a in argv], None)
        envp = (ctypes.c_char_p * (len(env) + 1))(*[os.fsencode(f"{k}={v}") for k, v in env.items()], None)
        rc = libc.posix_spawn(ctypes.byref(pid), os.fsencode(argv[0]), None, ctypes.byref(attr), args, envp)
        assert rc == 0, os.strerror(rc)
        return pid.value
    finally:
        libc.posix_spawnattr_destroy(ctypes.byref(attr))


def held(daemon, job_id: str, attempt_id: str) -> list[dict]:
    return daemon.rows("SELECT * FROM leases WHERE holder IN (?,?)", (job_id, attempt_id))


def assert_walk_only_from_a_recorded_responsibility(daemon, attempt: dict, pid: int, census: dict) -> None:
    """The escaper has no parent link to the attempt: the walk counts it only because an
    earlier census saw it name the guardian and recorded it as a seed (D-CEN1)."""
    evidence = json.loads(attempt["evidence_json"] or "{}")
    assert evidence["responsible_root"]["pid"] == attempt["guardian_pid"], evidence
    owned = evidence.get("owned_identities", {})
    assert str(pid) not in owned, "the escaper was never a group member while the guardian led it"
    if pid in census["descendant_pids"]:
        assert str(pid) in evidence.get("responsible_identities", {}), evidence


@pytest.mark.parametrize("scenario", ["retitled-node", "retitled-nul"])
def test_d_cen1_a_retitled_child_alive_after_the_exit_receipt_is_not_released(daemon, scenario):
    """D-CEN1, C-5.5, C-5.9: regression 1. The provider's child leaves the group, sets a
    node-style title and keeps running after the exit receipt. `ps -E` cannot see its
    marker; the census reads the environment from the raw argument buffer and does,
    so the attempt is quarantined, not released. `retitled-nul` is the deterministic
    stand-in: it writes the title and NULs over its own argv area through ctypes."""
    if scenario == "retitled-node" and not NODE:
        pytest.skip("node is not installed; retitled-nul covers the same argv layout")
    daemon.start(rooted=True)
    job_id, marker = submit_escaper(daemon, scenario)
    escaper = None
    try:
        escaper = escaper_identity(daemon, marker)
        attempt = settled(daemon, job_id)
        assert attempt["state"] == "quarantined", (attempt["state"], daemon.job(job_id)["state"])
        census = json.loads(attempt["quarantine_reason"])
        assert census["reason"] == "writers remain after exit receipt"
        assert escaper.pid in census["live_pids"]
        assert escaper.pid in census["marker_pids"], census       # the raw buffer, not `ps -E`
        assert escaper.pid in census["responsible_pids"], census  # and the guardian, which waited for it
        assert escaper.pid not in census["group_pids"], census
        assert_walk_only_from_a_recorded_responsibility(daemon, attempt, escaper.pid, census)
        assert held(daemon, job_id, attempt["attempt_id"]), "a quarantined attempt keeps its leases (C-5.7)"
        assert daemon.job(job_id)["state"] != "succeeded"
        shown = json.dumps(daemon.call("show", job_id=job_id))
        assert "SUBFLEET_ATTEMPT=" not in shown and "vitest" not in shown       # never a command or environment
    finally:
        stop(escaper)


def test_d_cen1_a_renamed_setsid_escaper_is_found_by_responsibility_and_released_once_gone(daemon):
    """D-CEN1, C-5.5: regressions 2 and 3. The provider double-forks, calls setsid and runs
    perl, which renames itself with `$0` after its launcher removes the markers:
    no marker, no group, no parent link. It is found because the guardian is its
    responsible process, and the attempt is quarantined. A stranger of the same uid,
    started after the guardian, that is its own responsible process and carries this
    root's marker for another attempt (`<attempt>0`), is never counted. Once the escaper
    exits, `kill --confirm-dead` finds the census verified empty and releases, with the
    stranger still running."""
    daemon.start(rooted=True)
    job_id, marker = submit_escaper(daemon, "perl-escape")
    escaper = stranger = None
    stranger_pid = None
    try:
        escaper = escaper_identity(daemon, marker)
        attempt_id = f"{job_id}/a1"
        ready = daemon.root / f"stranger-{uuid.uuid4().hex[:8]}.pid"
        stand_in = (f"import os, time; open({str(ready)!r}, 'w').write(str(os.getpid())); "
                    "time.sleep(60)")
        stranger_pid = spawn_own_responsibility(
            [sys.executable, "-c", stand_in],
            {"PATH": "/usr/bin:/bin", "SUBFLEET_ATTEMPT": attempt_id + "0", "SUBFLEET_JOB": job_id,
             "SUBFLEET_ROOT": str(daemon.root)})
        daemon.until(lambda: ready.exists() and ready.read_text().strip(), timeout=30)
        stranger = procs.identity(stranger_pid)
        assert stranger is not None
        attempt = settled(daemon, job_id)
        assert attempt["state"] == "quarantined", (attempt["state"], daemon.job(job_id)["state"])
        census = json.loads(attempt["quarantine_reason"])
        assert census["reason"] == "writers remain after exit receipt"
        assert escaper.pid in census["live_pids"]
        assert escaper.pid in census["responsible_pids"], census
        # Nothing else could have found it: the regression is responsibility's alone.
        assert escaper.pid not in census["marker_pids"] + census["group_pids"], census
        assert_walk_only_from_a_recorded_responsibility(daemon, daemon.attempts(job_id)[-1], escaper.pid, census)
        assert stranger_pid not in census["live_pids"], census
        assert str(stranger_pid) not in census["identities"] and str(stranger_pid) not in census["shapes"]
        assert held(daemon, job_id, attempt_id)

        guardian = daemon.attempts(job_id)[-1]
        assert census["group_pids"] == [guardian["guardian_pid"]], "the guardian waits while the escaper runs"
        stop(escaper)
        # With nothing left naming it, the guardian ends by itself.
        daemon.until(lambda: procs.liveness(guardian["guardian_pid"], guardian["boot_id"],
                                            guardian["proc_start"]) == "dead", timeout=30)
        daemon.call("kill", job_id=job_id, confirm_dead=True)
        daemon.until(lambda: not held(daemon, job_id, attempt_id), timeout=30)
        assert daemon.attempts(job_id)[-1]["state"] in ("lost", "interrupted")
        os.kill(stranger_pid, 0)          # released with the stranger alive: it was never ours
    finally:
        stop(escaper)
        if stranger_pid is not None:
            # This test's own unreaped child: its pid cannot have been reused.
            try:
                os.kill(stranger_pid, signal.SIGKILL)
                os.waitpid(stranger_pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


def test_d_cen1_an_escaper_seen_through_the_guardian_stays_counted_after_the_kill_protocol(daemon):
    """D-CEN1, C-5.5, C-5.6: the operator kills a running attempt whose provider left a
    renamed perl escaper. The kill protocol's first census, taken while the guardian
    lives, sees the escaper name it and records it; SIGKILL then ends the guardian, after
    which the kernel names the escaper as its own responsible process. The recorded
    identity keeps it counted, so the attempt is quarantined, not released, and the
    escaper is never signalled: a responsibility member gains no signal authority."""
    daemon.start(rooted=True)
    job_id, marker = submit_escaper(daemon, "perl-escape", hold_s=30)
    escaper = None
    try:
        escaper = escaper_identity(daemon, marker)
        daemon.until(lambda: daemon.attempts(job_id)[-1]["state"] == "running", timeout=30)
        daemon.call("kill", job_id=job_id)
        attempt = settled(daemon, job_id)
        assert attempt["state"] == "quarantined", (attempt["state"], daemon.job(job_id)["state"])
        census = json.loads(attempt["quarantine_reason"])
        assert escaper.pid in census["live_pids"], census
        evidence = json.loads(attempt["evidence_json"])
        assert evidence["responsible_identities"][str(escaper.pid)]["proc_start"] == escaper.proc_start
        assert str(escaper.pid) not in evidence.get("owned_identities", {})
        assert procs.liveness(escaper.pid, escaper.boot_id, escaper.proc_start) == "alive"
        assert held(daemon, job_id, attempt["attempt_id"])
    finally:
        stop(escaper)
