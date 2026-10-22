"""C-5.9: terminal cwd holds do not turn into twenty kernel scans a second."""

from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, procs
from tests.unit.test_daemon_settle import daemon, JOB, ATTEMPT  # noqa: F401


@pytest.mark.parametrize("unreadable", [False, True])
def test_terminal_cwd_scans_follow_the_inspection_interval(daemon, monkeypatch, unreadable):
    daemon.store.update_job(JOB, state="succeeded")
    daemon.store.update_attempt(ATTEMPT, state="succeeded")
    daemon.store.acquire_lease("worktree:/scratch/owned", JOB)
    now, scans, live = [0.0], [], [True]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    def observers(job):
        scans.append(now[0])
        if unreadable and live[0]:
            raise procs.InspectionError("scratch cwd read unavailable")
        return frozenset({5500}) if live[0] else frozenset()
    daemon._worktree_observers = observers
    assert daemon._pending_worktree_holds() == [JOB]
    daemon._release_worktree_hold(JOB)
    for tick in range(1, 20):
        now[0] = tick * daemon.inspect_interval_s / 20
        daemon._release_worktree_hold(JOB)
    assert scans == [0.0]
    assert daemon._pending_worktree_holds() == []
    live[0] = False
    now[0] = daemon.inspect_interval_s
    assert daemon._pending_worktree_holds() == [JOB]
    daemon._release_worktree_hold(JOB)
    assert scans == [0.0, daemon.inspect_interval_s]
    assert daemon.store.list_leases() == []
    assert daemon._pending_worktree_holds() == []


def test_released_worktree_clocks_are_forgotten(daemon, monkeypatch):
    daemon.store.update_job(JOB, state="succeeded")
    daemon.store.acquire_lease("worktree:/scratch/owned", JOB)
    daemon._worktree_observers = lambda job: frozenset({5500})
    daemon._release_worktree_hold(JOB)
    daemon.store.release_leases(JOB)  # an explicit release need not call the cwd worker
    assert daemon._pending_worktree_holds() == []
    assert not daemon.__dict__.get("_cwd_recheck_next")
