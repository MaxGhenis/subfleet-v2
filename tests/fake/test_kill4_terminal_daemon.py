"""D-KILL4, C-4.2, C-5.5, C-5.6, C-5.9: the three 2026-10-20 shapes through a real daemon.

Each test runs a real daemon, guardian and shell provider (`tests/fake_adapter.py`).
The provider leaves a `/bin/sh` loop in a session of its own that respawns a
marked child (`tests/bin/fakeprov --driver`); the daemon's reader hides the loop's
environment (`--hide-environment-of`), as macOS 26 does for a shell, so only its
children show the markers. No attempt may end `lost` or `interrupted` while the
loop or a child lives: it is contained, or it is quarantined with the loop held.

1. Lost at start (`hub-build-mi-household-members-std/a1`): a guardian alive past
   start grace (here sleeping before its start.json, or stopped) is killed before
   any retry is reserved.
2. Writers after the exit receipt (`hub-us9981-review-r3b/a1`).
3. Walled (`hub-us10299-review-shell-b/a2`).

Every process a test starts is stopped by its recorded identity, on failure too;
each loop ends by itself within 90 s and each child within a second.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

import pytest

from subfleet import procs
from tests.fake.test_census_escapers_daemon import escaper_identity, executable, held, settled, stop

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="C-5.5's census reads macOS process state")
SHELLS = {"sh", "bash", "zsh", "dash"}


def start(daemon, *options: str, **kwargs):
    """`Harness.start` with a 60 s readiness wait: its 5 s wait times out at the host
    loads these holds run under (load 75 to 82 on 2026-10-23), before any of this
    file's behaviour runs. Only this file's starts wait longer; the harness is unchanged."""
    until = daemon.until
    daemon.until = lambda predicate, timeout=5: until(predicate, timeout=max(timeout, 60))
    try:
        return daemon.start(*options, **kwargs)
    finally:
        daemon.until = until


def submit_driver(daemon, marker: Path, scenario: str, **overrides) -> str:
    args = daemon.submit_args(max_attempts=1, **overrides)
    Path(args["prompt_path"]).write_text(json.dumps({"scenario": scenario, "marker": str(marker)}))
    return daemon.call("submit", **args)["job_id"]


def driver_running(daemon, marker: Path) -> procs.ProcessIdentity:
    driver = escaper_identity(daemon, marker)
    # /bin/sh runs the shell /private/var/select/sh names (bash here).
    daemon.until(lambda: os.path.basename(executable(driver.pid) or "") in SHELLS, timeout=30)
    # Its child is running and shows the markers the loop hides.
    daemon.until(lambda: any(row[0] == driver.pid for pid, row in procs.snapshot().rows.items()), timeout=30)
    return driver


def end_loop(driver: procs.ProcessIdentity) -> None:
    """Freeze the loop so it starts nothing more, end its children, then end it."""
    procs.signal_process(driver, signal.SIGSTOP)
    table = procs.snapshot()
    for pid, row in table.rows.items():
        child = table.identity(pid) if row[0] == driver.pid else None
        if child is not None:
            stop(child)
    stop(driver)


def alive(identity: procs.ProcessIdentity | None) -> bool:
    return identity is not None and procs.liveness(identity.pid, identity.boot_id, identity.proc_start) == "alive"


def no_live_children(driver: procs.ProcessIdentity) -> bool:
    return not any(row[0] == driver.pid and not row[2].startswith("Z") for row in procs.snapshot().rows.values())


def assert_never_lost_with_live_pids(attempt: dict, driver: procs.ProcessIdentity) -> None:
    if attempt["state"] != "quarantined":
        assert not alive(driver), f"{attempt['state']} with its loop {driver.pid} alive"


@pytest.mark.parametrize("ending", ["exit-receipt", "walled"])
def test_a_counted_loop_driver_is_contained_with_its_children(daemon, ending):
    """The provider's loop is a walk member while the provider lives, so inspection
    counts it (D-KILL3), or records it as owned if it lands before the loop's `setsid`
    (D-KILL4-EVIDENCE1). After the provider exits by itself, or the wall stops it,
    the kill protocol signals the counted loop and its children: contained, and the
    attempt ends from its own outcome (or `interrupted` by the wall) with none alive.
    Before D-KILL4 both quarantined, the loop alive and never signalled."""
    marker = daemon.root / f"driver-{uuid.uuid4().hex[:8]}.pid"
    start(daemon, "--hide-environment-of", str(marker), "--inspection-witness", str(marker), isolated_group=True)
    driver = None
    try:
        job = submit_driver(daemon, marker, "loop-driver",
                            **({"max_wall_s": 4} if ending == "walled" else {}))
        driver = driver_running(daemon, marker)
        daemon.until(lambda: Path(str(marker) + ".inspected").exists(), timeout=30)
        if ending == "exit-receipt":
            Path(str(marker) + ".release").touch()
        attempt = settled(daemon, job)
        assert attempt["state"] != "quarantined", json.loads(attempt["quarantine_reason"] or "{}")
        daemon.until(lambda: not alive(driver), timeout=10)
        evidence = json.loads(attempt["evidence_json"])
        # D-KILL4-EVIDENCE1: the shell provider backgrounds the driver in the guardian's
        # group, and it stays there until its `os.setsid()` (`tests/bin/fakeprov`, `driver`).
        # An inspection in that window rightly records it as owned, and `_record_owned`
        # never counts an owned identity again; either record attributes it.
        recorded = {**evidence.get("owned_identities", {}), **evidence["counted_identities"]}
        assert recorded[str(driver.pid)]["proc_start"] == driver.proc_start
        rounds = evidence["terminal_containment"]
        assert driver.pid in rounds[-1]["signalled"] and rounds[-1]["contained"] is True
        if ending == "walled":
            assert attempt["state"] == "interrupted" and attempt["killed_by"] == "max_wall_s"
        else:
            assert attempt["killed_by"] is None and attempt["state"] in ("succeeded", "failed")
        assert no_live_children(driver)
        assert_never_lost_with_live_pids(attempt, driver)
    finally:
        stop(driver)
        Path(str(marker) + ".release").touch()


@pytest.mark.parametrize("ending", ["exit-receipt", "walled"])
def test_an_uncounted_detached_loop_in_an_in_place_workdir_is_held_and_quarantines(daemon, ending):
    """The loop is reparented to launchd before any inspection, in an in-place
    workdir: no lineage, no marker, no worktree. Its marked children name it as
    their parent, so the parent-chain rule holds it: the attempt quarantines with
    `held_pids` naming it, and it is never signalled (the operator-safety limit).
    `--confirm-dead` is refused while it lives and releases once it is gone.
    Before D-KILL4 the census named only whichever child was alive (or nothing,
    between two children, and the attempt ended with the loop running)."""
    marker = daemon.root / f"driver-{uuid.uuid4().hex[:8]}.pid"
    # Inspection paced at 20 s: none runs before the loop is reparented, so none counts it
    # (with the default 1 s, one can land while its short-lived intermediate parent lives).
    start(daemon, "--hide-environment-of", str(marker), "--inspect-interval-s", "20", isolated_group=True)
    driver = None
    try:
        job = submit_driver(daemon, marker, "loop-driver-detached",
                            **({"max_wall_s": 4} if ending == "walled" else {}))
        driver = driver_running(daemon, marker)
        if ending == "exit-receipt":
            Path(str(marker) + ".release").touch()
        attempt = settled(daemon, job)
        assert attempt["state"] == "quarantined", attempt["state"]
        census = json.loads(attempt["quarantine_reason"])
        assert driver.pid in census["held_pids"], census
        assert driver.pid not in census["live_pids"]
        evidence = json.loads(attempt["evidence_json"])
        assert evidence["held_parents"][str(driver.pid)]["proc_start"] == driver.proc_start
        assert all(driver.pid not in round_["signalled"] for round_ in evidence["terminal_containment"])
        assert alive(driver), "a held parent is never signalled"
        assert held(daemon, job, attempt["attempt_id"])
        daemon.call("kill", job_id=job, confirm_dead=True)
        daemon.until(lambda: daemon.rows("SELECT 1 FROM events WHERE attempt_id=? AND kind=?",
                                         (attempt["attempt_id"], "quarantine.still_live")), timeout=30)
        assert daemon.attempts(job)[-1]["state"] == "quarantined"
        end_loop(driver)
        until = time.monotonic() + 30
        while held(daemon, job, attempt["attempt_id"]):
            assert time.monotonic() < until, "confirm-dead never released the attempt once the loop was gone"
            daemon.call("kill", job_id=job, confirm_dead=True)
            time.sleep(1)
    finally:
        stop(driver)
        Path(str(marker) + ".release").touch()


@pytest.mark.parametrize("stopped,delay,grace", [(False, 30, None), (True, 30, 3), (True, 2, 4)],
                         ids=["sleeping", "stopped", "stopped-short-delay"])
def test_a_guardian_alive_past_start_grace_is_killed_before_any_retry(daemon, stopped, delay, grace):
    """The guardian is alive past start grace without a start.json: sleeping before it
    (`--start-delay`) past D-QUAR-START1's bound (2 s here), or stopped, as Fleet ops'
    governor or the clamshell guard stop processes, which is never given more time. The
    kill protocol KILLs its group at once (fix 1: its TERM is a no-op by design, and a
    continue would only resume its launch); the attempt ends unlaunched (fix round 2b,
    decision 1), and the job's retry is reserved only once that guardian is dead.
    With a short `--start-delay` (2 s, under a 4 s start grace) the stopped guardian's
    delay has run out by the time start grace expires, so a continue would start its
    provider at once (review r1's P2): it never starts. Before D-KILL4 it was
    quarantined alive, and would have started its provider when continued."""
    options = ["--start-delay", str(delay), *(["--start-grace-s", str(grace)] if grace else []),
               *([] if stopped else ["--start-grace-max-s", "2"])]
    start(daemon, *options, isolated_group=True)
    job = daemon.submit("ok", max_attempts=2)
    first = daemon.until(lambda: next((a for a in daemon.attempts(job) if a["seq"] == 1 and a["guardian_pid"]), None))
    guardian = procs.ProcessIdentity(first["guardian_pid"], first["boot_id"], first["proc_start"])
    if stopped:
        # Stopped once it leads its group (its first act), as the 2026-10-20 guardian was:
        # before its setsid, killpg cannot reach it (the unit regression covers that shape).
        # A start grace of 3 s or more leaves the test time to see the setsid and stop it
        # (with the harness's 0.65 s the daemon could KILL it first, under load).
        daemon.until(lambda: os.getpgid(guardian.pid) == guardian.pid, timeout=2)
        procs.signal_process(guardian, signal.SIGSTOP)
        if (daemon.root / "jobs" / job / "a1" / "start.json").exists():
            pytest.skip("setup race: the guardian wrote start.json before the test could stop it")
    try:
        def checked():
            rows = daemon.attempts(job)
            if len(rows) > 1:
                # Read the rows first: a guardian alive now was alive when a2 was reserved.
                assert not alive(guardian), "a retry was reserved while a1's guardian lived"
            return rows[0] if rows[0]["state"] not in ("reserved", "starting", "running") else None
        a1 = daemon.until(checked, timeout=30)
        assert (a1["state"], a1["outcome_detail"]) == ("failed", "starting-no-receipt"), a1
        assert a1["killed_by"] == "recovery" and not a1["quarantine_reason"]
        assert (daemon.root / "jobs" / job / "a1" / "abandon.json").exists()
        assert not alive(guardian)
        assert not (daemon.root / "jobs" / job / "a1" / "start.json").exists()
        stdout = daemon.root / "jobs" / job / "a1" / "stdout"
        assert not stdout.exists() or not stdout.read_bytes(), "the provider ran"
        evidence = json.loads(a1["evidence_json"])
        last = evidence["terminal_containment"][-1]
        assert last["contained"] is True
        assert last["group_signals"] == [int(signal.SIGKILL)], last          # KILLed at once, never TERMed
        # Never continued, and never TERMed alone: at most a KILL to it as a survivor.
        assert not [e for e in last.get("signals", []) if e["pid"] == guardian.pid
                    and e["signal"] != int(signal.SIGKILL)], last
    finally:
        if alive(guardian):
            procs.signal_process(guardian, signal.SIGKILL)
        for row in daemon.attempts(job)[1:]:
            if row["guardian_pid"]:
                later = procs.ProcessIdentity(row["guardian_pid"], row["boot_id"], row["proc_start"])
                if alive(later):
                    procs.signal_process(later, signal.SIGKILL)
