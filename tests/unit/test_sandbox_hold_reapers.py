"""C-11.10: ordinary lease cleanup never removes an operator's sandbox hold.

Exercise the real cleanup SQL through the store, retention, admission-probe,
timer and ending-job paths. Expired operator rows survive too: expiry is a read
decision, never permission for a lease reaper to delete an operator's row.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from subfleet import retention, task_holds
from subfleet.daemon import Daemon
from subfleet.store import Store
from subfleet.timers import Timers


SANDBOX_KEY = "sandbox-hold:workspace-write"
HOLD = "operator: disk pressure booking"
SINCE = "2000-01-01T00:00:00Z"


@pytest.fixture(params=["2000-01-02T00:00:00Z", "2200-01-01T00:00:00Z"], ids=["expired", "future"])
def held_store(tmp_path, request):
    store = Store(tmp_path / "state.sqlite3")
    assert store.acquire_lease(SANDBOX_KEY, HOLD, expires_at=request.param)
    before = store.one("SELECT * FROM leases WHERE lease_key=?", (SANDBOX_KEY,))
    yield store, before
    store.close()


def unchanged(store, before):
    assert store.one("SELECT * FROM leases WHERE lease_key=?", (SANDBOX_KEY,)) == before


def test_sandbox_hold_key_is_outside_the_task_hold_range(held_store):
    store, before = held_store
    assert not task_holds.KEY <= SANDBOX_KEY < task_holds.KEY_END
    assert store.query("SELECT * FROM leases WHERE lease_key>=? AND lease_key<?",
                       (task_holds.KEY, task_holds.KEY_END)) == []
    unchanged(store, before)


@pytest.mark.parametrize("holder", ["job-1", "job-1/a1", "probe:timer:x", "probe:admission:x",
                                   "retention:job-1", "resume:request-1", "cwd:job-1", "enrollment:lane-1"])
def test_release_of_each_ordinary_holder_preserves_sandbox_hold(held_store, holder):
    store, before = held_store
    assert store.acquire_lease("lane:codex-1:slot:0", holder)
    assert store.release_leases(holder, prefix="worktree:") == 0
    unchanged(store, before)
    assert store.release_leases(holder) == 1
    unchanged(store, before)


def test_retention_recovers_orphan_and_stale_resume_leases_without_reaping_sandbox_hold(held_store, tmp_path):
    store, before = held_store
    assert store.acquire_lease("retire:orphan", "retention:orphan")
    assert store.acquire_lease("retire:resuming", "resume:request-1")
    with store.transaction("test.age_fence") as tx:
        tx.execute("UPDATE leases SET acquired_at=? WHERE lease_key='retire:resuming'", (SINCE,))
    root = tmp_path / "retention-root"
    root.mkdir()
    result = retention.maintenance(store, root, holders=lambda *args, **kwargs: {})
    assert result["errors"] == []
    assert store.one("SELECT 1 FROM leases WHERE holder LIKE 'retention:%' OR holder LIKE 'resume:%'") is None
    unchanged(store, before)


def test_startup_probe_and_timer_cleanup_preserves_sandbox_hold(held_store):
    store, before = held_store
    assert store.acquire_lease("lane:codex-1:slot:0", "probe:timer:orphan")
    assert store.acquire_lease("retire:resuming", "resume:request-1")
    timers = SimpleNamespace(store=store, active_holders=set(), _lock=threading.RLock(),
                             actions=SimpleNamespace(recover=lambda: None), start=lambda: None)
    service = SimpleNamespace(store=store, timers=timers, _probe_record=lambda holder: None,
                              _canonicalize_pins=lambda: None, _recover_capacity_waits=lambda: None,
                              _recovery_complete=threading.Event(), _schedule=lambda *args: None,
                              _sweep_attempt_tmpdirs=lambda: None, _offer_unrun_sweep=lambda: None)
    service._recover_probes = lambda: Daemon._recover_probes(service)
    Daemon._recover_then_start_timers(service)
    assert service._recovery_complete.is_set()
    assert store.one("SELECT 1 FROM leases WHERE holder LIKE 'probe:%' OR holder LIKE 'resume:%'") is None
    unchanged(store, before)
    assert store.acquire_lease("lane:codex-1:slot:0", "probe:timer:finished")
    timers.active_holders.add("probe:timer:finished")
    Timers._release(timers, "probe:timer:finished")
    assert not timers.active_holders
    assert store.one("SELECT 1 FROM leases WHERE holder='probe:timer:finished'") is None
    unchanged(store, before)


@pytest.mark.parametrize("observers", [frozenset(), None], ids=["release", "keep-worktree"])
def test_job_and_attempt_end_release_preserves_sandbox_hold(held_store, observers):
    store, before = held_store
    assert store.acquire_lease("lane:codex-1:slot:0", "job-1/a1")
    assert store.acquire_lease("out:/result", "job-1")
    assert store.acquire_lease("worktree:/workspace", "job-1")
    with store.transaction("test.end_job") as tx:
        Daemon._release_ending(None, tx, "job-1", "job-1/a1", observers, convert=False)
    assert store.one("SELECT 1 FROM leases WHERE lease_key LIKE 'lane:%' OR lease_key LIKE 'out:%'") is None
    assert bool(store.one("SELECT 1 FROM leases WHERE lease_key LIKE 'worktree:%'")) == (observers is None)
    unchanged(store, before)
