"""C-5.5/C-5.9: native cwd observers hold a tree without deciding its result."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, folders, procs
from subfleet.contracts import attempt_dir
from subfleet.daemon import Daemon
from tests.unit.test_daemon_settle import (
    ATTEMPT, JOB, attempt, daemon as core, publish_receipt, with_launch,
)  # noqa: F401


@pytest.mark.real_cwd
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS in-process cwd read")
@pytest.mark.parametrize("ending", ["receipt", "start-grace", "leaves-within-window"])
def test_cwd_only_preserves_outcome_and_holds_only_worktree(core, monkeypatch, ending):
    worktree = core.root / "worktrees" / JOB
    worktree.mkdir(parents=True)
    core.store.update_job(JOB, sandbox="workspace-write", worktree=str(worktree), max_attempts=1)
    with_launch(core, monkeypatch)
    core._export = lambda job_id: Daemon._export_locked(core, job_id)
    key = folders.exclusive_key(worktree)
    core.store.acquire_lease(key, JOB)
    core.store.acquire_lease("lane:codex-1", ATTEMPT)
    monkeypatch.setattr(procs, "containment", lambda *a, **k: procs.Containment())
    monkeypatch.setattr(procs, "signal_process", lambda *a: pytest.fail("cwd grants no signal authority"))
    now = [0.0]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    child = subprocess.Popen(["/bin/sleep", "120"], cwd=worktree, start_new_session=True)
    monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset({child.pid}))
    try:
        assert procs.worktree_cwd_pids(worktree) == {child.pid}
        if ending == "start-grace":
            core.start_grace_s = 0
            core.store.update_attempt(ATTEMPT, state="starting")
            step = lambda: core._process_attempt(ATTEMPT)
        else:
            publish_receipt(core)
            core.store.update_attempt(ATTEMPT, state="finalizing", rc=0)
            step = lambda: core._finalize(attempt(core))
        step()
        now[0] = 3.1
        step()
        assert attempt(core)["state"] in ("starting", "finalizing")
        now[0] = 29.9
        step()
        assert not attempt(core)["quarantine_reason"]
        if ending == "leaves-within-window":
            child.terminate()
            child.wait(timeout=10)
            monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset())
        else:
            now[0] = 30.1
        step()
        job, row = core.store.get_job(JOB), attempt(core)
        assert row["state"] == ("failed" if ending == "start-grace" else "succeeded")
        assert not row["quarantine_reason"]
        if ending != "start-grace":
            assert job["rc"] == 0 and job["accepted_attempt_id"] == ATTEMPT
            assert (attempt_dir(core.root, JOB, 1) / "deliverable.md").read_bytes() == b"done\n"
        held = core.store.one("SELECT 1 FROM leases WHERE lease_key=?", (key,))
        assert bool(held) is (ending != "leaves-within-window")
        if ending != "leaves-within-window":
            assert json.loads(row["evidence_json"])["cwd_holders"] == [f"pid {child.pid}: cwd in worktree"]
            notices = core.store.query("SELECT text FROM notices WHERE job_id=?", (JOB,))
            assert any(f"pid {child.pid}: cwd in worktree" in n["text"] for n in notices)
            assert JOB in core._pending_worktree_holds()
            core._release_worktree_hold(JOB)
            assert core.store.one("SELECT 1 FROM leases WHERE lease_key=?", (key,))
            child.terminate()
            child.wait(timeout=10)
            monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset())
            now[0] += core.inspect_interval_s
            core._release_worktree_hold(JOB)
            assert not core.store.one("SELECT 1 FROM leases WHERE lease_key=?", (key,))
        assert not json.loads(row["evidence_json"]).get("counted_identities")
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS process census")
def test_real_daemon_rc_zero_after_eight_seconds_keeps_deliverable_with_observer(daemon):
    """The reviewer's rc-0 /bin/sleep input through the real daemon and guardian."""
    from tests.unit.test_retention_worktrees import git
    git(daemon.workdir, "init", "-b", "feature")
    (daemon.workdir / "tracked").write_text("baseline")
    git(daemon.workdir, "add", "tracked")
    git(daemon.workdir, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    daemon.start()
    job_id = daemon.submit(delay_s=8, sandbox="workspace-write", max_attempts=1)
    daemon.until(lambda: daemon.job(job_id).get("worktree"), timeout=30)
    worktree = Path(daemon.job(job_id)["worktree"])
    child = subprocess.Popen(["/bin/sleep", "120"], cwd=worktree, start_new_session=True)
    try:
        daemon.until(lambda: daemon.job(job_id)["state"] in {"succeeded", "failed", "lost"}, timeout=100)
        job, row = daemon.job(job_id), daemon.attempts(job_id)[-1]
        assert job["state"] == "succeeded" and job["rc"] == 0
        assert job["accepted_attempt_id"] == row["attempt_id"]
        assert (attempt_dir(daemon.root, job_id, 1) / "deliverable.md").read_bytes().strip()
        assert daemon.rows("SELECT 1 FROM leases WHERE lease_key=?", (folders.exclusive_key(worktree),))
    finally:
        child.terminate()
        child.wait(timeout=10)
    daemon.until(lambda: not daemon.rows("SELECT 1 FROM leases WHERE lease_key=?",
                                         (folders.exclusive_key(worktree),)), timeout=30)


def test_counted_and_cwd_evidence_never_authorize_signals_or_release_quarantine(core, monkeypatch):
    core.term_grace_s = core.kill_settle_s = 0
    core.store.acquire_lease("worktree:/scratch/owned", JOB)
    census = procs.Containment(descendant_pids=frozenset({4401}), cwd_pids=frozenset({4500}),
                               identities={4401: procs.ProcessIdentity(4401, "boot", "start")})
    core._contain = lambda a: census
    signals = []
    monkeypatch.setattr(procs, "signal_group", lambda pgid, sig, **kw: signals.append((pgid, sig)))
    monkeypatch.setattr(procs, "signal_process", lambda *a, **k: pytest.fail("evidence is not authority"))
    core._kill_attempt(attempt(core))
    assert attempt(core)["state"] == "quarantined"
    assert all(pid == 4242 for pid, _ in signals)
    assert JOB not in core._pending_worktree_holds()
    core._release_worktree_hold(JOB)
    assert core.store.one("SELECT 1 FROM leases WHERE lease_key='worktree:/scratch/owned'")


def test_unreadable_cwd_recheck_keeps_terminal_worktree_lease(core, monkeypatch):
    core.store.update_job(JOB, state="succeeded")
    core.store.acquire_lease("worktree:/scratch/owned", JOB)
    def failed(job):
        raise procs.InspectionError("unreadable")
    core._worktree_observers = failed
    core._release_worktree_hold(JOB)
    assert core.store.one("SELECT 1 FROM leases WHERE lease_key='worktree:/scratch/owned'")
