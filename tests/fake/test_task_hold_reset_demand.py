"""C-11.10, C-23.16, T7 (D-TASKHOLD1 fix round 1): a held job makes no reset-credit demand.

Review r1 of #260, P2-3, executed: all six Codex lanes closed, a build waiting on capacity,
a hold on `build` committed, then the reset timer before another admission pass. The fake
Codex endpoints recorded one gift credit consumed and a confirmed reset: `_reset_demand`
read the job's stale capacity wait. Now `_reset_demand` reads the holds in force in the same
snapshot as the waiting rows and leaves a held job out, and the reset action's own pending
transaction reads the hold again before anything is spent, so a hold committed after demand
was read still spends nothing. Once the hold is released and admission has looked at the job
again, it is demand again.

Fix round 2 (review r2's F1): the hold's own transaction now takes the job off its capacity wait,
so neither read finds it waiting on capacity at all, and the two hold checks are the backstop
for a job the take did not reach.

Fix round 3 (review r3 of #260): no admission write lands over a committed hold any more (R3-1),
so r3's way to the checks, an episode opening under way when the hold commits, now leaves the
job held. A job the take does not reach still waits on capacity before a pass takes it: a held
build whose attempt was live at the commit and ended `transient` since. Each check is the only
one that stops it at some interleaving (`retry_waits_while_held`), and round 1's mutant of each,
the check alone removed with the take intact, is killed there (R3-3).

The daemon, the reset timer and the Codex adapter are real; the Codex usage and reset-credit
endpoints are `FakeWham` (`tests/fake/test_reset_credit_incident.py`), so no real
credential, endpoint or credit is touched.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.daemon import after
from tests.fake.test_reset_credit_incident import _fleet, limit, state_of, submit
from tests.fake_adapter import FakeAdapter


@contextlib.contextmanager
def reset_fleet(*, pinned_lane: str | None = None):
    """`test_reset_credit_incident`'s six-lane fleet with every lane at its limit and a gift banked
    on each, and one build waiting on capacity after an admission pass (pinned to `pinned_lane`,
    if named). Usable outside pytest's fixtures, so the mutant scenarios
    (`tests/fake/test_task_hold_mutants.py`) build it too."""
    base = Path(tempfile.mkdtemp(prefix="sf-thr-"))
    service = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
            patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
            patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
            patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
            service, http = _fleet(base)
            for number in range(1, 7):
                limit(service, number)
            build = submit(service, "build-1", task="build", tier="standard",
                           **({"pinned_lane": pinned_lane} if pinned_lane else {}))
            service._admit()
            assert state_of(service, build) == ("waiting", "capacity")
            assert [item["job_id"] for item in service._reset_demand()] == [build]
            yield service, http, build
    finally:
        if service is not None:
            service.close()
        if "/sf-" in str(base):
            shutil.rmtree(base, ignore_errors=True)


def hold_build(service) -> None:
    service.dispatch("tasks", {"action": "hold", "task": "build", "until": after(3600),
                               "note": "uk-data batch release: no new builds"})


def spent(service, http) -> tuple[list[str], list[dict]]:
    """The credits consumed at the fake endpoint, and the reset actions written."""
    return http.consumed(), service.store.query("SELECT * FROM actions")


def held_job_spends_nothing():
    """r1's scenario: the hold lands, and the reset timer runs before any admission pass."""
    with reset_fleet() as (service, http, build):
        hold_build(service)
        assert service._reset_demand() == [], "T7: a held job was reset-credit demand"
        result = service.timers.reset_credits_cycle()
        assert result["status"] == "no-demand", f"T7: {result}"
        assert spent(service, http) == ([], []), "T7: a credit was spent for a held job"
        assert not service.store.query("SELECT * FROM events WHERE kind='reset-credit.confirmed'"), \
            "T7: a reset was confirmed for a held job"


def hold_after_demand_was_read_spends_nothing():
    """The hold lands after the timer read demand for the last time, before its pending action:
    the hold's own transaction took the job off its capacity wait (fix round 2, F1), and the
    action's transaction, which reads the job's wait and then its hold again, spends nothing."""
    with reset_fleet() as (service, http, build):
        reader = service.timers.demand
        reads = []

        def demand():
            found = reader()
            reads.append(found)
            if len(reads) == 2:                     # the timer's last read, just before the spend
                hold_build(service)
            return found
        service.timers.demand = demand
        result = service.timers.reset_credits_cycle()
        assert len(reads) == 2 and [item["job_id"] for item in reads[1]] == [build]
        assert result["status"] == "demand-gone", f"T7-pending: {result}"
        assert spent(service, http) == ([], []), "T7-pending: a credit was spent for a held job"


def test_a_held_job_is_no_demand_before_admission_looks_again():
    held_job_spends_nothing()


def test_a_hold_committed_after_the_last_demand_read_still_spends_nothing():
    hold_after_demand_was_read_spends_nothing()


def test_the_demand_returns_once_the_hold_is_released():
    with reset_fleet() as (service, http, build):
        hold_build(service)
        service._admit()
        assert state_of(service, build) == ("waiting", "task-held")
        assert service.timers.reset_credits_cycle()["status"] == "no-demand"
        answer = service.dispatch("tasks", {"action": "release", "task": "build"})
        assert answer["woken"] == 1
        service._admit()
        assert state_of(service, build) == ("waiting", "capacity")
        assert [item["job_id"] for item in service._reset_demand()] == [build]
        result = service.timers.reset_credits_cycle()
        assert result["status"] == "confirmed" and result["job_id"] == build, result
        assert http.consumed() == ["fake-6"]


def test_a_hold_on_another_task_leaves_the_demand_alone():
    with reset_fleet() as (service, http, build):
        service.dispatch("tasks", {"action": "hold", "task": "review", "until": after(3600), "note": "reviews off"})
        assert [item["job_id"] for item in service._reset_demand()] == [build]
        assert service.timers.reset_credits_cycle()["status"] == "confirmed"


# --- fix round 3 (review r3 of #260, R3-1 and R3-3) --------------------------------------------


def test_an_episode_opening_under_way_when_the_hold_commits_leaves_the_job_held(monkeypatch):
    """R3-1 on review r3's R3-3 input: the pinned codex-6 demand, its limit extended from six to ten
    days, so the next look opens a C-11.8 episode; that look stopped as it begins, the hold commits
    (its take moves the job to `task-held`), then the look goes on. Its opening wrote a capacity
    wait over `task-held`, which only the two hold checks kept from being demand. Now the opening's
    own transaction reads the hold and leaves the job held."""
    from tests.fake.test_task_hold_clocks import look_under_way
    with reset_fleet(pinned_lane="codex-6") as (service, http, build):
        limit(service, 6, days=10)
        with look_under_way(service, monkeypatch, "_stuck_pin"):
            hold_build(service)
        assert state_of(service, build) == ("waiting", "task-held"), \
            "R3-1: an episode opening under way wrote a capacity wait over a committed hold"
        assert service._reset_demand() == []
        assert service.timers.reset_credits_cycle()["status"] == "no-demand"
        assert spent(service, http) == ([], [])


def retry_waits_while_held(service, build) -> None:
    """Review r3 of #260, R3-3, as fix round 3 reaches it with every admission write reading the
    hold (R3-1). The pinned build is placed on codex-6, its lane open again; the hold lands while
    that attempt is live, so its commit takes nothing (a hold never touches a live attempt,
    C-11.10); the attempt ends `transient` through the daemon's own finalizer, and codex-6 is at
    its limit again, ten days out. The build waits on capacity for its retry, its task held, and
    no pass has taken it for the hold yet: the next one will. Run on its own thread, as the
    finalizer and the hold run beside the reset timer."""
    from subfleet.procs import Containment
    from tests.fake.test_reset_credit_incident import open_lane
    from tests.task_hold_support import finalize_transient
    open_lane(service, 6, measured=True)
    with service.store.transaction("test.recheck_due", job_id=build) as tx:    # its next look comes due
        tx.execute("UPDATE jobs SET next_check_at=? WHERE job_id=?", ("2000-01-01T00:00:00Z", build))
    service._admit()
    [attempt] = service.store.query("SELECT * FROM attempts WHERE job_id=?", (build,))
    assert attempt["lane_id"] == "codex-6"
    hold_build(service)
    assert state_of(service, build)[0] == "running"
    with pytest.MonkeyPatch.context() as patch:     # the finalizer reads no real process
        patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
        patch.setattr(daemon_module.procs, "group_members", lambda *args: {})
        patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
        patch.setattr(daemon_module.os, "killpg", lambda *args: None)
        patch.setattr(daemon_module.os, "kill", lambda *args: None)
        finalize_transient(service, dict(attempt))
    limit(service, 6, days=10)
    assert state_of(service, build) == ("waiting", "capacity")


def held_retry_is_no_reset_demand():
    """R3-3, `_reset_demand`'s own hold check: the timer's read of demand stopped after it copied
    admission's capacity waits and holds and before its store snapshot, while `retry_waits_while_held`
    runs. The snapshot finds the build waiting on capacity, its last look a lane wait, and its task
    held: only the hold check leaves it out."""
    with reset_fleet(pinned_lane="codex-6") as (service, http, build):
        main = threading.current_thread()
        entered = threading.Barrier(2, timeout=20)
        resume = threading.Barrier(2, timeout=20)
        original = service.store.snapshot
        stopped = [False]

        def snapshot(*args, **kwargs):
            if threading.current_thread() is not main and not stopped[0]:
                stopped[0] = True
                entered.wait()
                resume.wait()
            return original(*args, **kwargs)
        service.store.snapshot = snapshot
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                read = pool.submit(service._reset_demand)
                entered.wait()
                try:
                    retry_waits_while_held(service, build)
                finally:
                    resume.wait()
                found = read.result(timeout=20)
        finally:
            service.store.snapshot = original
        assert found == [], f"T7: a held job was reset-credit demand: {found}"


def held_retry_spends_nothing():
    """R3-3, the pending transaction's own hold check: `retry_waits_while_held` runs after the
    timer's last read of demand, which named the build, and before its pending transaction. That
    transaction finds the build waiting on capacity and its task held: only the hold check stops
    the spend (`demand-gone`, with `task_held`)."""
    with reset_fleet(pinned_lane="codex-6") as (service, http, build):
        reader = service.timers.demand
        reads = []

        def demand():
            found = reader()
            reads.append(found)
            if len(reads) == 2:                     # the timer's last read, just before the spend
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(retry_waits_while_held, service, build).result(timeout=30)
            return found
        service.timers.demand = demand
        result = service.timers.reset_credits_cycle()
        assert len(reads) == 2 and [item["job_id"] for item in reads[1]] == [build]
        assert (result["status"], result.get("task_held")) == ("demand-gone", "build"), f"T7-pending: {result}"
        assert spent(service, http) == ([], []), "T7-pending: a credit was spent for a held job"


def test_a_held_retry_is_no_demand_before_admission_takes_it():
    held_retry_is_no_reset_demand()


def test_a_held_retry_spends_nothing_before_admission_takes_it():
    held_retry_spends_nothing()
