"""C-5.6, C-5.7b with real processes: a tool command in a session of its own is
owned, killed with its attempt, and, when it outlives its provider, holds a
quarantine only while it lives.

The provider is `tests/bin/fakeprov`'s `session-child` scenario: it starts
`/bin/sleep` in a new session, as Claude Code starts a Bash command (`/bin/zsh`)
and Codex its tool commands. `/bin/sleep` is a platform binary whose environment
`ps -E` does not show, so once the provider is gone no marker finds it; only what
the daemon recorded does. The fake daemon writes the owned record at once
(`owned_persist_s=0`) and rechecks quarantines every 0.3 s (tests/fake/run_daemon.py).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

import pytest

from subfleet import procs

pytestmark = pytest.mark.skipif(not Path("/bin/sleep").exists(), reason="needs /bin/sleep")


def child_of(daemon) -> int:
    marker = daemon.root / "session-child.pid"
    daemon.until(lambda: marker.exists() and marker.read_text())
    return int(marker.read_text())


def recorded(daemon, job_id: str) -> set[int]:
    attempt = daemon.attempts(job_id)[-1]
    return {int(pid) for pid in json.loads(attempt["evidence_json"] or "{}").get("owned_identities", {})}


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout
    return state.strip().startswith("Z") or not state.strip()


def kinds(daemon, attempt_id: str) -> list[str]:
    return [row["kind"] for row in daemon.rows("SELECT kind FROM events WHERE attempt_id=? ORDER BY event_id",
                                               (attempt_id,))]


def test_c5_6_a_kill_reaches_a_tool_command_in_a_session_of_its_own(daemon):
    """C-5.6: the kill protocol proves the tool session owned (a descendant of the
    provider) and signals its group, so the attempt ends contained, not quarantined
    (before: the command ran on in its own group, found by no source once its
    provider died, as `uv run pytest` did on 2026-09-29)."""
    daemon.start()
    job_id = daemon.submit("session-child", linger_s=60)
    daemon.attempt_state(job_id, "running")
    child = child_of(daemon)
    try:
        assert os.getpgid(child) == child and os.getsid(child) == child      # its own session and group
        daemon.call("kill", job_id=job_id)
        finished = daemon.finished(job_id)
        attempt = daemon.attempts(job_id)[-1]
        assert finished["state"] == "cancelled" and attempt["state"] == "interrupted", attempt
        assert "attempt.quarantined" not in kinds(daemon, attempt["attempt_id"])
        daemon.until(lambda: gone(child), timeout=10)
        assert child in recorded(daemon, job_id)
        assert not daemon.rows("SELECT * FROM leases WHERE holder IN (?,?)", (job_id, attempt["attempt_id"]))
    finally:
        if not gone(child):
            os.kill(child, signal.SIGKILL)


def test_c5_7b_a_child_that_outlives_its_provider_holds_the_quarantine_until_it_exits(daemon):
    """C-5.9, C-5.7b: the provider exits 0 leaving its tool command running. Only the
    daemon's record of it (no marker, no group, no parent left) keeps the attempt from
    being released as contained: it is quarantined, stays so for as long as the child
    lives, and the recheck releases its leases, with no operator, once the child is gone."""
    daemon.start()
    out = daemon.root / "export.md"
    job_id = daemon.submit("session-child", linger_s=60, out_path=str(out))
    daemon.attempt_state(job_id, "running")
    child = child_of(daemon)
    try:
        daemon.until(lambda: child in recorded(daemon, job_id), timeout=10)
        (daemon.root / "session-child.pid.release").touch()               # the provider exits 0
        attempt = daemon.attempt_state(job_id, "quarantined")
        assert json.loads(attempt["quarantine_reason"])["reason"] == "writers remain after exit receipt"
        assert child in json.loads(attempt["quarantine_reason"])["live_pids"]
        time.sleep(2)                                                       # about six rechecks
        assert daemon.attempts(job_id)[-1]["state"] == "quarantined"
        assert daemon.rows("SELECT * FROM leases WHERE lease_key=?", (f"out:{out}",))
        os.kill(child, signal.SIGKILL)
        daemon.until(lambda: daemon.attempts(job_id)[-1]["state"] == "lost", timeout=10)
        attempt = daemon.attempts(job_id)[-1]
        assert "quarantine.auto_resolved" in kinds(daemon, attempt["attempt_id"])
        assert not daemon.rows("SELECT * FROM leases WHERE holder IN (?,?)", (job_id, attempt["attempt_id"]))
        assert (daemon.job(job_id)["state"], daemon.job(job_id)["rc"]) == ("lost", 125)
    finally:
        if not gone(child):
            os.kill(child, signal.SIGKILL)


def quarantined_with_helper(daemon):
    """A quarantine whose lingering child the test then kills, and an unrelated
    `/bin/sleep` (no marker, not ours by parent or group) to record by identity."""
    daemon.start()
    job_id = daemon.submit("session-child", linger_s=60)
    daemon.attempt_state(job_id, "running")
    child = child_of(daemon)
    helper = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    daemon.until(lambda: child in recorded(daemon, job_id), timeout=10)
    (daemon.root / "session-child.pid.release").touch()
    attempt = daemon.attempt_state(job_id, "quarantined")
    return job_id, attempt, child, helper


@pytest.mark.parametrize("reused", [False, True], ids=["its-own-start", "another-start"])
def test_c5_7b_a_recorded_identity_holds_by_itself_and_a_reused_pid_does_not(daemon, reused):
    """C-5.7b with the real census: a process the quarantine recorded holds it though no
    source would find it; the same pid recorded under another start, which is what a
    reused pid looks like, holds nothing, and the attempt is released while the
    process now at that pid lives. (Recorded identities are only ever added, so each
    case adds one: C-5.7b's evidence never drops what a census found.)"""
    job_id, attempt, child, helper = quarantined_with_helper(daemon)
    try:
        start = "Thu Jan  1 00:00:00 2026" if reused else procs.proc_start(helper.pid)
        identity = {"pid": helper.pid, "boot_id": procs.boot_id(), "proc_start": start}
        edit(daemon, attempt["attempt_id"], lambda evidence: {**evidence, "recorded": evidence["recorded"] + [identity]})
        os.kill(child, signal.SIGKILL)
        if reused:
            daemon.until(lambda: daemon.attempts(job_id)[-1]["state"] == "lost", timeout=10)
            assert helper.poll() is None                                    # released while it lives
            return
        time.sleep(2)                                                       # about six rechecks
        assert daemon.attempts(job_id)[-1]["state"] == "quarantined"
        assert json.loads(daemon.attempts(job_id)[-1]["quarantine_reason"])["live_pids"] == [helper.pid]
        helper.kill()
        helper.wait()
        daemon.until(lambda: daemon.attempts(job_id)[-1]["state"] == "lost", timeout=10)
    finally:
        helper.kill()
        helper.wait()
        if not gone(child):
            os.kill(child, signal.SIGKILL)


def edit(daemon, attempt_id: str, change) -> None:
    """An operator's hand edit of the running daemon's store (C-3.4), in one transaction."""
    with sqlite3.connect(daemon.root / "state.sqlite3", timeout=10, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        (reason,) = db.execute("SELECT quarantine_reason FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        db.execute("UPDATE attempts SET quarantine_reason=? WHERE attempt_id=?",
                   (json.dumps(change(json.loads(reason))), attempt_id))
        db.execute("COMMIT")
