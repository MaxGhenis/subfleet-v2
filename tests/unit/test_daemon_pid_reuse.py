"""C-5.5, C-5.7b with pid reuse: the census vouches for a recorded pid only by its
identity, so an attempt whose only live pids are reused ones finalizes and resolves
as contained.

Cases from `fix/containment-pid-reuse-cont` (cd8b9b32, the r218 attempt a2 of
2026-09-28: its child's pid 80817 was held, when it was quarantined, by a process
started that day in a foreign group), run against this line's census as a
differential check."""

import json
import time
from dataclasses import asdict

import pytest

from subfleet import procs, protocol
from tests.unit.test_daemon_settle import (ATTEMPT, JOB, attempt, daemon, publish_receipt,  # noqa: F401 (fixture)
                                         with_launch)

BOOT = "boot"
START = "Sat Sep  5 10:00:00 2026"
REUSED_START = "Mon Sep 28 22:11:29 2026"


def recorded(pid: int) -> procs.ProcessIdentity:
    return procs.ProcessIdentity(pid, BOOT, START)


def synthetic_processes(monkeypatch, rows):
    """The census reads `rows` and no marker; nothing may be signalled."""
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(rows, BOOT))

    def markers(argv, **kwargs):
        assert argv == procs.MARKER_ARGV
        return ""

    monkeypatch.setattr(procs, "_read", markers)
    monkeypatch.setattr(procs, "signal_process", lambda *a, **k: pytest.fail("unexpected process signal"))
    monkeypatch.setattr(procs, "signal_group", lambda *a, **k: pytest.fail("unexpected group signal"))


@pytest.mark.parametrize("reused_guardian", [False, True], ids=["r218-child", "guardian-with-child"])
def test_only_reused_pids_finalize_normally_and_reach_salvage(daemon, monkeypatch, reused_guardian):
    """C-5.5, C-5.9: `_finalize` -> `_contain` -> `containment`, past the exit-settle window."""
    with_launch(daemon, monkeypatch)
    publish_receipt(daemon)
    identities = {str(pid): asdict(recorded(pid)) for pid in (4242, 4243)}
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0, boot_id=BOOT, proc_start=START,
                                evidence_json=json.dumps({"owned_identities": identities}))
    if reused_guardian:
        rows = {4242: (1, 4242, "S", REUSED_START),
                4243: (4242, 4242, "S", REUSED_START),
                4244: (4243, 4244, "S", REUSED_START)}
    else:
        rows = {4243: (76443, 76443, "S", REUSED_START)}
    synthetic_processes(monkeypatch, rows)
    salvaged = []
    daemon._salvage = lambda job, row: (salvaged.append(row["attempt_id"]) or [], None)
    daemon._exit_settle[ATTEMPT] = time.monotonic() - daemon.exit_settle_s - 1

    daemon._finalize(attempt(daemon))

    finished = attempt(daemon)
    assert finished["state"] == "succeeded" and finished["rc"] == 0 and finished["quarantine_reason"] is None
    job = daemon.store.get_job(JOB)
    assert (job["state"], job["rc"], job["accepted_attempt_id"]) == ("succeeded", 0, ATTEMPT)
    assert salvaged == [ATTEMPT]
    kinds = [row["kind"] for row in daemon.store.list_events(JOB)]
    assert "attempt.accepted" in kinds and "attempt.quarantined" not in kinds


def test_confirm_dead_resolves_a_quarantine_held_only_by_reused_pids(daemon, monkeypatch):
    """C-5.7: `--confirm-dead`'s census (C-5.7b) does not count a recorded pid another process holds."""
    synthetic_processes(monkeypatch, {4243: (900, 900, "S", REUSED_START)})
    daemon.store.update_attempt(ATTEMPT, state="quarantined", boot_id=BOOT, proc_start=START,
                                quarantine_reason=json.dumps({"reason": "writers remain after exit receipt"}),
                                evidence_json=json.dumps({"owned_identities": {"4243": asdict(recorded(4243))}}))
    with daemon.store.transaction("test.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES (?,?,?)",
                   (f"worktree:{daemon.root}", ATTEMPT, "2026-09-28T22:11:51Z"))
    salvaged = []
    daemon._salvage = lambda job, row: (salvaged.append(row["attempt_id"]) or [], None)

    daemon._resolve_quarantine(attempt(daemon), protocol.KillArgs(job_id=JOB, confirm_dead=True))

    assert attempt(daemon)["state"] == "lost" and salvaged == [ATTEMPT]
    assert not daemon.store.query("SELECT * FROM leases WHERE holder=?", (ATTEMPT,))
