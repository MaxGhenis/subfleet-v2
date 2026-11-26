"""Round 3 (D-KILL3 review r3): the cwd hold excludes no writer; the loss path is paced.

P2: where a writable job ends with cwd observers, its `worktree:` keys become
reader rows on the same folders, held by `cwd:<job id>` (`folders.CWD`), in the
transaction that releases its other leases (C-5.5). Only the cwd worker
releases them (C-5.9), and it touches nothing else.

P3: a receipt-less loss whose census finds only cwd observers takes one census
per due inspection, and records its end once (C-5.9).
"""

import json
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, folders, procs, protocol
from subfleet.daemon import Daemon
from tests.unit.test_daemon_settle import (
    ATTEMPT, JOB, attempt, daemon, publish_receipt, with_launch,
)  # noqa: F401

WORKTREE = "/scratch/owned"
KEY = folders.exclusive_key(WORKTREE)
KEPT = folders.turn_key(WORKTREE, JOB, writable=False)
CWD = procs.Containment(cwd_pids=frozenset({4500}))
#: Admission's lifetime reader row of a detached job, on its resolved run folder;
#: for an allocated writer that folder is its worktree, so the key is `KEPT`.
LIFETIME = KEPT


def leases(core) -> dict[str, str]:
    return {row["lease_key"]: row["holder"] for row in core.store.list_leases()}


def writer(core, *, run_folder=WORKTREE):
    """The fixture's job as an allocated writer holding what admission gives one."""
    core.store.update_job(JOB, sandbox="workspace-write", in_place=False, worktree=WORKTREE, max_attempts=1)
    core.store.acquire_lease(KEY, JOB)
    core.store.acquire_lease(folders.turn_key(run_folder, JOB, writable=False), JOB)
    core.store.acquire_lease("lane:codex-1:slot:0", ATTEMPT)


def kept(core):
    """The fixture's job, finished, as the conversion leaves it."""
    core.store.update_job(JOB, state="succeeded", sandbox="workspace-write", in_place=False, worktree=WORKTREE)
    core.store.update_attempt(ATTEMPT, state="succeeded")
    core.store.acquire_lease(KEPT, folders.cwd_holder(JOB))


# --- where a writable job ends: the conversion --------------------------------------------------


def test_r3_quarantine_resolution_converts_the_hold(daemon, monkeypatch):
    writer(daemon)
    daemon.store.update_job(JOB, state="lost")
    daemon.store.update_attempt(ATTEMPT, state="quarantined")
    daemon._contain = lambda a: CWD
    daemon._salvage = lambda job, a, **kw: ([], None, {})
    monkeypatch.setattr(procs, "signal_group", lambda *a, **kw: pytest.fail("cwd cannot authorize a signal"))
    daemon._resolve_quarantine(attempt(daemon), protocol.KillArgs(job_id=JOB, confirm_dead=True,
                                                                  operator_note="scratch"))
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB)}


@pytest.mark.parametrize("retry", [False, True], ids=["ends", "queued-again"])
def test_r3_an_unlaunched_writer_converts_only_when_it_ends(daemon, retry):
    """Queued again, the job is still a writer there: it keeps its own key."""
    writer(daemon)
    daemon.store.update_job(JOB, max_attempts=2 if retry else 1)
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon._unlaunched(attempt(daemon), "starting-no-receipt", census=CWD)
    assert daemon.store.get_job(JOB)["state"] == ("queued" if retry else "failed")
    assert leases(daemon) == ({KEY: JOB} if retry else {KEPT: folders.cwd_holder(JOB)})


def test_r3_a_failed_finalization_converts_the_hold(daemon, monkeypatch):
    writer(daemon)
    with_launch(daemon, monkeypatch)
    publish_receipt(daemon, rc=1)
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=1)
    daemon._contain = lambda a: CWD
    monkeypatch.setattr(daemon_module, "CWD_SETTLE_S", 0)
    daemon._finalize(attempt(daemon))
    assert daemon.store.get_job(JOB)["state"] == "failed"
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB)}


@pytest.mark.parametrize("run_folder", [WORKTREE, WORKTREE + "/sub"], ids=["same-folder", "subfolder"])
@pytest.mark.parametrize("observers", [frozenset({4500}), "unreadable", frozenset()])
def test_r3_export_converts_the_hold_and_frees_the_lifetime_row(daemon, run_folder, observers):
    """The lifetime reader row is released and the kept row takes its key when the
    folders are one (the key collision the hub named); nothing else is kept."""
    writer(daemon, run_folder=run_folder)
    daemon.store.update_job(JOB, state="succeeded", accepted_attempt_id=ATTEMPT)
    daemon.store.update_attempt(ATTEMPT, state="succeeded")
    daemon.store.release_leases(ATTEMPT)

    def seen(job):
        if observers == "unreadable":
            raise procs.InspectionError("scratch cwd read unavailable")
        return observers
    daemon._worktree_observers = seen
    Daemon._export_locked(daemon, JOB)
    assert leases(daemon) == ({} if observers == frozenset() else {KEPT: folders.cwd_holder(JOB)})


def test_r3_the_conversion_takes_attempt_held_keys_too(daemon):
    writer(daemon)
    daemon.store.release_leases(JOB, prefix="worktree:")
    daemon.store.acquire_lease(KEY, ATTEMPT)
    with daemon.store.transaction("test.end") as tx:
        tx.execute("DELETE FROM leases WHERE holder IN (?,?) AND lease_key NOT LIKE 'worktree:%'", (JOB, ATTEMPT))
        daemon._keep_for_cwd(tx, JOB, ATTEMPT)
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB)}


# --- the cwd worker -------------------------------------------------------------------------------


def test_r3_the_worker_releases_only_the_kept_row(daemon):
    """Nothing but `cwd:<job id>` rows: never a `worktree:` key, never another job's row."""
    kept(daemon)
    resume = "20260905-100100-resume"
    daemon.store.acquire_lease(KEY, resume)
    daemon.store.acquire_lease(folders.turn_key(WORKTREE, resume, writable=False), resume)
    daemon.store.acquire_lease(folders.turn_key(WORKTREE, "20260905-100200-turn", writable=True),
                               "20260905-100200-turn")
    daemon._worktree_observers = lambda job: frozenset({4500})
    assert daemon._pending_worktree_holds() == [JOB]
    daemon._release_worktree_hold(JOB)
    assert KEPT in leases(daemon)
    daemon._cwd_recheck_next = {}
    daemon._worktree_observers = lambda job: frozenset()
    daemon._release_worktree_hold(JOB)
    assert leases(daemon) == {KEY: resume, folders.turn_key(WORKTREE, resume, writable=False): resume,
                              folders.turn_key(WORKTREE, "20260905-100200-turn", writable=True):
                                  "20260905-100200-turn"}
    assert daemon._pending_worktree_holds() == []


@pytest.mark.parametrize("other", ["lane:codex-1:slot:0", "out:/scratch/result", "push:" + JOB])
@pytest.mark.parametrize("holder", [JOB, ATTEMPT])
def test_r3_the_worker_waits_while_the_job_holds_anything_else(daemon, other, holder):
    """The row's release waits for the job's own leases too (`CWD_WORKTREE_HOLDS`)."""
    kept(daemon)
    daemon.store.acquire_lease(other, holder)
    daemon._worktree_observers = lambda job: frozenset()
    assert daemon._pending_worktree_holds() == []
    daemon._release_worktree_hold(JOB)
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB), other: holder}


def test_r3_the_worker_never_offers_a_live_job(daemon):
    kept(daemon)
    daemon.store.update_job(JOB, state="running")
    daemon._worktree_observers = lambda job: frozenset()
    assert daemon._pending_worktree_holds() == []
    daemon._release_worktree_hold(JOB)
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB)}


def test_r3_a_row_taken_during_the_scan_is_not_released(daemon):
    """The eligibility is read again in the releasing transaction."""
    kept(daemon)

    def observers(job):
        daemon.store.acquire_lease("out:/scratch/result", JOB)
        return frozenset()
    daemon._worktree_observers = observers
    assert daemon._pending_worktree_holds() == [JOB]
    daemon._release_worktree_hold(JOB)
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB), "out:/scratch/result": JOB}


# --- P3: the receipt-less loss path ---------------------------------------------------------------


@pytest.mark.parametrize("scan_s", [0, .254], ids=["instant", "review-cost"])
def test_r3_a_receipt_less_loss_with_an_observer_is_paced(daemon, monkeypatch, scan_s):
    """r3's P3: two censuses per inspection and an `attempt.lost_end` on every pass."""
    now, censuses = [0.0], []
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def contain(a):
        censuses.append(now[0])
        now[0] += scan_s
        return CWD
    daemon._contain = contain
    daemon.store.update_job(JOB, max_attempts=1)
    monkeypatch.setattr(procs, "liveness", lambda *a: "dead")
    with_launch(daemon, monkeypatch)
    while attempt(daemon)["state"] == "running" and now[0] < 40:
        daemon._process_attempt(ATTEMPT)
        now[0] += .05
    assert attempt(daemon)["state"] == "lost" and now[0] >= daemon_module.CWD_SETTLE_S
    assert attempt(daemon)["killed_by"] is None
    window = [t for t in censuses if t < censuses[0] + 30]
    assert len(window) <= 30 / daemon.inspect_interval_s + 1, len(window)
    assert len(daemon.store.query("SELECT 1 FROM events WHERE kind='attempt.lost_end'")) == 1
    assert json.loads(attempt(daemon)["evidence_json"])["cwd_holders"] == ["pid 4500: cwd in worktree"]


def test_r3_the_paced_loss_still_sees_the_observer_leave_on_its_next_due_census(daemon, monkeypatch):
    now, observers = [0.0], [frozenset({4500})]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    daemon._contain = lambda a: procs.Containment(cwd_pids=observers[0])
    daemon.store.update_job(JOB, max_attempts=1)
    monkeypatch.setattr(procs, "liveness", lambda *a: "dead")
    with_launch(daemon, monkeypatch)
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "running"
    observers[0] = frozenset()
    now[0] = daemon.inspect_interval_s
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "lost"
    assert len(daemon.store.query("SELECT 1 FROM events WHERE kind='attempt.lost_end'")) == 1
