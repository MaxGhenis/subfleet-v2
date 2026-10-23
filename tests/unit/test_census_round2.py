"""Round 2: cwd evidence owns only its settled, allocated worktree hold."""

import json
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, procs, protocol
from tests.unit.test_daemon_settle import (
    ATTEMPT, JOB, attempt, daemon, publish_receipt, with_launch,
)  # noqa: F401


KEY = "worktree:/scratch/owned"
CWD = procs.Containment(cwd_pids=frozenset({4500}))


def terminal(core, holder=JOB):
    core.store.update_job(JOB, state="succeeded", in_place=False)
    core.store.update_attempt(ATTEMPT, state="succeeded")
    core.store.acquire_lease(KEY, holder)
    core._worktree_observers = lambda job: frozenset()


@pytest.mark.parametrize("holder", [JOB, ATTEMPT])
@pytest.mark.parametrize("other", ["push:pending", "out:/scratch/result", "lane:codex-1"])
def test_cwd_worker_never_offers_or_releases_a_job_with_other_leases(daemon, holder, other):
    terminal(daemon, holder)
    daemon.store.acquire_lease(other, ATTEMPT if holder == JOB else JOB)
    assert daemon._pending_worktree_holds() == []
    daemon._release_worktree_hold(JOB)  # a stale offer must be safe too
    assert {row["lease_key"] for row in daemon.store.list_leases()} == {KEY, other}


def test_in_place_writer_is_never_offered_or_released(daemon):
    terminal(daemon)
    daemon.store.update_job(JOB, in_place=True)
    assert daemon._pending_worktree_holds() == []
    daemon._release_worktree_hold(JOB)
    assert daemon.store.list_leases(JOB)


@pytest.mark.parametrize("pending", ["export", "push"])
def test_unsettled_publication_blocks_even_a_worktree_only_hold(daemon, pending):
    terminal(daemon)
    daemon.store.update_job(JOB, accepted_attempt_id=ATTEMPT,
                            **({"out_path": "/scratch/result"} if pending == "export" else
                               {"push_branch": "jobs/result"}))
    assert daemon._pending_worktree_holds() == []
    daemon._release_worktree_hold(JOB)
    assert daemon.store.list_leases(JOB)


def test_cwd_worker_revalidates_after_the_observer_scan(daemon):
    terminal(daemon)
    def observers(job):
        daemon.store.acquire_lease("out:/scratch/result", JOB)
        return frozenset()
    daemon._worktree_observers = observers
    assert daemon._pending_worktree_holds() == [JOB]
    daemon._release_worktree_hold(JOB)
    assert {row["lease_key"] for row in daemon.store.list_leases()} == {KEY, "out:/scratch/result"}


@pytest.mark.parametrize("force", [False, True], ids=["confirm-dead", "force-release"])
def test_quarantine_resolution_salvages_with_cwd_only_and_preserves_its_hold(daemon, monkeypatch, force):
    terminal(daemon)
    daemon.store.update_attempt(ATTEMPT, state="quarantined")
    daemon.store.acquire_lease("lane:codex-1", ATTEMPT)
    daemon._contain = lambda a: CWD
    salvaged = []
    daemon._salvage = lambda job, a, **kw: (salvaged.append(a["attempt_id"]) or [], None, {})
    monkeypatch.setattr(procs, "signal_group", lambda *a, **kw: pytest.fail("cwd cannot authorize a signal"))
    monkeypatch.setattr(procs, "signal_process", lambda *a, **kw: pytest.fail("cwd cannot authorize a signal"))
    daemon._resolve_quarantine(attempt(daemon), protocol.KillArgs(job_id=JOB, confirm_dead=not force,
                                                               force_release=force, operator_note="scratch"))
    assert attempt(daemon)["state"] == "lost"
    assert salvaged == [ATTEMPT]
    assert {row["lease_key"] for row in daemon.store.list_leases()} == {KEY}
    assert json.loads(attempt(daemon)["evidence_json"])["cwd_holders"] == ["pid 4500: cwd in worktree"]
    assert daemon._pending_worktree_holds() == [JOB]
    daemon._worktree_observers = lambda job: frozenset({4500})
    daemon._release_worktree_hold(JOB)
    assert daemon.store.list_leases(JOB)
    daemon._cwd_recheck_next = {}
    daemon._worktree_observers = lambda job: frozenset()
    daemon._release_worktree_hold(JOB)
    assert daemon.store.list_leases() == []


def test_wall_dead_guardian_with_cwd_only_records_loss_without_a_kill(daemon, monkeypatch):
    daemon.store.update_job(JOB, max_wall_s=1, max_attempts=1)
    daemon._running_s = lambda job_id: 2
    daemon._contain = lambda a: CWD
    monkeypatch.setattr(procs, "liveness", lambda *a: "dead")
    with_launch(daemon, monkeypatch)
    lost = []
    original = daemon._lost
    def record_loss(a):
        lost.append(a["attempt_id"])
        original(a)
    daemon._lost = record_loss
    monkeypatch.setattr(procs, "signal_group", lambda *a, **kw: pytest.fail("no writer to signal"))
    monkeypatch.setattr(procs, "signal_process", lambda *a, **kw: pytest.fail("no writer to signal"))
    assert daemon._enforce_wall(attempt(daemon), daemon._job(JOB)) is False
    assert lost == [ATTEMPT]
    assert attempt(daemon)["killed_by"] is None
    assert not daemon.store.query("SELECT 1 FROM events WHERE kind='attempt.kill_started'")


@pytest.mark.parametrize("ending", ["finalization", "start-grace"])
@pytest.mark.parametrize("scan_s", [0, .254], ids=["instant", "review-cost"])
def test_cwd_wait_censuses_follow_inspect_interval(daemon, monkeypatch, ending, scan_s):
    now, calls = [0.0], []
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    def contain(a):
        calls.append(now[0])
        now[0] += scan_s
        return CWD
    daemon._contain = contain
    if ending == "finalization":
        publish_receipt(daemon)
        daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0)
        step = lambda: daemon._finalize(attempt(daemon))
    else:
        daemon.start_grace_s = 0
        daemon.store.update_attempt(ATTEMPT, state="starting")
        # Offer every tick, including when inspection cannot verify a launch.
        daemon._inspect_running = lambda *a: False
        step = lambda: daemon._process_attempt(ATTEMPT)
    step()
    for tick in range(1, 20):
        now[0] = tick * daemon.inspect_interval_s / 20
        step()
    assert calls == [0.0]
    now[0] = daemon.inspect_interval_s
    step()
    assert calls == [0.0, daemon.inspect_interval_s]


def test_cwd_wait_restart_starts_a_new_thirty_second_window(daemon, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    daemon._contain = lambda a: CWD
    publish_receipt(daemon)
    with_launch(daemon, monkeypatch)
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0)
    daemon._finalize(attempt(daemon))
    now[0] = 8
    daemon._finalize(attempt(daemon))
    # The store survives; restart loses only the daemon's wait/cadence clocks.
    daemon._cwd_settle = {}
    daemon._cwd_wait_next = {}
    daemon._finalize(attempt(daemon))
    now[0] = 30.1
    daemon._finalize(attempt(daemon))
    assert attempt(daemon)["state"] == "finalizing"
    now[0] = 38.1
    daemon._finalize(attempt(daemon))
    assert attempt(daemon)["state"] == "succeeded"
