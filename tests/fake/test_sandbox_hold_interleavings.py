"""D-RO-ADMIT1: composed holds preserve clocks and fence stale admission/reset reads."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from subfleet import daemon as daemon_module
from tests.disk_floor_model import stamp
from tests.fake.test_sandbox_hold_properties import fleet, jobs_from, sandbox_hold, sandbox_release
from tests.fake.test_task_hold_clocks import at, fails_exactly_at, stuck
from tests.task_hold_support import attempts, finalize_transient, hold, job, release, submit


def writable(service, ident):
    """Model a persisted writable row; fleet stubs workspace preparation, never admission."""
    with service.store.transaction("test.persisted_sandbox", job_id=ident) as tx:
        tx.execute("UPDATE jobs SET sandbox='workspace-write' WHERE job_id=?", (ident,))


def hold_target(service, target, end):
    return sandbox_hold(service, end) if target == "sandbox" else hold(service, "build", stamp(end))


def release_target(service, target):
    return sandbox_release(service) if target == "sandbox" else release(service, "build")


@pytest.mark.parametrize("kind", ["pinned", "retry"])
@pytest.mark.parametrize("released_first", ["task", "sandbox"])
def test_releasing_one_composed_hold_keeps_the_pin_grace_paused(kind, released_first):
    """Both targets cover the job; releasing either one leaves its 50 seconds paused."""
    with fleet() as (service, harness, clock, signals):
        ident = stuck(service, harness, clock, kind)
        writable(service, ident)
        clock[0] = 10
        hold(service, "build", stamp(100))
        sandbox_hold(service, 200)
        at(service, clock, 10)
        assert job(service, ident)["next_check_at"] == stamp(200)
        assert service._pin_episodes[ident]["paused_s"] == 50
        clock[0] = 30
        answer = release_target(service, released_first)
        remaining = "sandbox" if released_first == "task" else "task"
        expected_end = 200 if remaining == "sandbox" else 100
        assert answer["woken"] == 0
        assert (job(service, ident)["wait_reason"], job(service, ident)["next_check_at"]) == (
            "task-held", stamp(expected_end))
        assert service.store.query("SELECT * FROM events WHERE kind='job.task_hold_ended' AND job_id=?",
                                   (ident,)) == []
        at(service, clock, 75)
        assert service._pin_episodes[ident]["paused_s"] == 50
        assert job(service, ident)["wait_reason"] == "task-held"
        clock[0] = 80
        assert release_target(service, remaining)["woken"] == 1
        at(service, clock, 80)
        assert service._pin_episodes[ident]["fail_at"] == stamp(130)
        fails_exactly_at(service, clock, ident, 130)


@pytest.mark.parametrize("kind", ["pinned", "retry"])
@pytest.mark.parametrize("shortened", ["task", "sandbox"])
def test_shorter_replacement_keeps_the_other_targets_later_end(kind, shortened):
    """Replacing 300 with 50 still pauses through the other target's end at 200."""
    with fleet() as (service, harness, clock, signals):
        ident = stuck(service, harness, clock, kind)
        writable(service, ident)
        other = "sandbox" if shortened == "task" else "task"
        clock[0] = 10
        hold_target(service, shortened, 300)
        hold_target(service, other, 200)
        clock[0] = 20
        replacement = hold_target(service, shortened, 50)
        assert replacement["task_hold"]["since"] == stamp(10)
        assert job(service, ident)["next_check_at"] == stamp(200)
        at(service, clock, 50)
        assert job(service, ident)["wait_reason"] == "task-held"
        assert service._pin_episodes[ident]["paused_s"] == 50
        at(service, clock, 200)
        assert service._pin_episodes[ident]["fail_at"] == stamp(250)
        fails_exactly_at(service, clock, ident, 250)


@pytest.mark.parametrize("sandbox_end,expected_start", [(40, 60), (90, 90)])
def test_composed_holds_preserve_the_retry_deadline(sandbox_end, expected_start):
    """A release cannot start a retry before its backoff, and a longer hold satisfies it."""
    with fleet() as (service, harness, clock, signals):
        ident = submit(service, harness, "build", "retry-backoff")
        service._admit()
        [first] = attempts(service, ident)
        finalize_transient(service, first)
        assert job(service, ident)["next_check_at"] == stamp(60)
        # The attempt uses the existing read-only finalizer fixture; only then
        # model the persisted writable retry row that admission will judge.
        writable(service, ident)
        clock[0] = 10
        hold(service, "build", stamp(100))
        sandbox_hold(service, sandbox_end)
        clock[0] = 20
        assert release(service, "build")["woken"] == 0
        assert job(service, ident)["next_check_at"] == stamp(sandbox_end)
        at(service, clock, expected_start - 1)
        assert len(attempts(service, ident)) == 1
        at(service, clock, expected_start)
        assert len(attempts(service, ident)) == 2


@pytest.mark.parametrize("sandbox", ["workspace-write", ""], ids=["stored-write", "legacy-empty"])
def test_sandbox_hold_after_the_door_snapshot_still_fences_reservation(monkeypatch, sandbox):
    """The reservation transaction must see the sandbox lease even with a stale door epoch."""
    with fleet() as (service, harness, clock, signals):
        ident = jobs_from(service, harness, [("review", sandbox, "dispatch")])["generated-0"]
        prepare = service._prepare_route
        committed = []

        def commit_after_door(row, *args, **kwargs):
            if row["job_id"] == ident and not committed:
                epoch = service._task_hold_epoch
                sandbox_hold(service, 100)
                committed.append(True)
                # Existing task-hold race tests likewise bypass the epoch fast
                # path, proving the writer independently checks the lease row.
                service._task_hold_epoch = epoch
            return prepare(row, *args, **kwargs)

        monkeypatch.setattr(service, "_prepare_route", commit_after_door)
        service._admit()
        assert committed == [True]
        assert attempts(service, ident) == []
        assert (job(service, ident)["state"], job(service, ident)["wait_reason"],
                job(service, ident)["next_check_at"]) == ("waiting", "task-held", stamp(100))


def test_reset_pending_transaction_names_the_sandbox_hold_without_spending(monkeypatch):
    """A persisted writable retry follows stale demand into action.pending."""
    from tests.fake import test_task_hold_reset_demand as reset_helpers

    def hold_writable_jobs(service):
        writable(service, ident)
        before_job, before_attempts = dict(job(service, ident)), attempts(service, ident)
        try:
            service.dispatch("tasks", {"action": "hold", "sandbox": "workspace-write",
                                        "until": daemon_module.after(3600), "note": "disk brake"})
            assert job(service, ident) == before_job
            assert attempts(service, ident) == before_attempts
        finally:
            # The existing finalizer helper models a read-only attempt. Restore
            # that fixture shape until it finishes, then model the persisted
            # writable retry before action.pending reads its sandbox.
            with service.store.transaction("test.finalizer_sandbox", job_id=ident) as tx:
                tx.execute("UPDATE jobs SET sandbox='read-only' WHERE job_id=?", (ident,))

    monkeypatch.setattr(reset_helpers, "hold_build", hold_writable_jobs)
    with reset_helpers.reset_fleet(pinned_lane="codex-6") as (service, http, ident):
        reader = service.timers.demand
        reads = []

        def demand():
            found = reader()
            reads.append(found)
            if len(reads) == 2:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(reset_helpers.retry_waits_while_held, service, ident).result(timeout=30)
                # Keep the finalizer's read-only fixture intact, then model the
                # persisted row at the reset writer's independent hold check.
                writable(service, ident)
            return found

        service.timers.demand = demand
        result = service.timers.reset_credits_cycle()
        assert len(reads) == 2 and [item["job_id"] for item in reads[1]] == [ident]
        assert (result["status"], result.get("sandbox_held")) == ("demand-gone", "workspace-write"), result
        assert "task_held" not in result
        assert reset_helpers.spent(service, http) == ([], [])
