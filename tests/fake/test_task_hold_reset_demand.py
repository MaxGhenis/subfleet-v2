"""C-11.10, C-23.16, T7 (D-TASKHOLD1 fix round 1): a held job makes no reset-credit demand.

Review r1 of #260, P2-3, executed: all six Codex lanes closed, a build waiting on capacity,
a hold on `build` committed, then the reset timer before another admission pass. The fake
Codex endpoints recorded one gift credit consumed and a confirmed reset: `_reset_demand`
read the job's stale capacity wait. Now `_reset_demand` reads the holds in force in the same
snapshot as the waiting rows and leaves a held job out, and the reset action's own pending
transaction reads the hold again before anything is spent, so a hold committed after demand
was read still spends nothing. Once the hold is released and admission has looked at the job
again, it is demand again.

The daemon, the reset timer and the Codex adapter are real; the Codex usage and reset-credit
endpoints are `FakeWham` (`tests/fake/test_reset_credit_incident.py`), so no real
credential, endpoint or credit is touched.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.daemon import after
from tests.fake.test_reset_credit_incident import _fleet, limit, state_of, submit
from tests.fake_adapter import FakeAdapter


@contextlib.contextmanager
def reset_fleet():
    """`test_reset_credit_incident`'s six-lane fleet with every lane at its limit and a gift banked
    on each, and one build waiting on capacity after an admission pass. Usable outside pytest's
    fixtures, so the mutant scenarios (`tests/fake/test_task_hold_mutants.py`) build it too."""
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
            build = submit(service, "build-1", task="build", tier="standard")
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
    the action's transaction reads the hold again and spends nothing."""
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
        assert (result["status"], result.get("task_held")) == ("demand-gone", "build"), \
            f"T7-pending: {result}"
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
