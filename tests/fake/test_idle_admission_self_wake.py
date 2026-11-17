"""D-ADMIT-IDLE1 review r2: the detached pass's own queue transitions never end its rest.

`Daemon._wake_admission` ignores a `jobs` notice committed on the thread running
the detached pass (`_admission_thread.active`): admission moving its own job back
to `queued` is no arrival from outside, and waking on it would keep an idle pass
running for ever. Each case has controls: the same transition from another
thread, and another notice kind from the pass's own thread, still wake it.
"""

import threading

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import utcnow
from tests.fake.test_capacity_wait_backoff import submit
from tests.fake.test_idle_admission_pacing import paced  # noqa: F401
from tests.fake.test_idle_capacity_events import clock, held_fleet  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


def notices(service):
    """Every category the store delivers to admission, in order, calling through."""
    seen = []
    watch = service.store._admission_watch

    def recorded(category):
        seen.append((category, threading.get_ident()))
        watch(category)

    service.store._admission_watch = recorded
    return seen


@pytest.mark.parametrize("transition", ["none", "workspace-ready"])
def test_a_real_pass_requeuing_its_own_job_still_rests(held_fleet, transition):
    """A due workspace wait made ready by the pass (`job.workspace_ready`) is requeued by the
    pass itself, then held by the operator closure: nothing placed, so the pass rests."""
    service, harness, _, _ = held_fleet
    job = submit(service, harness, pinned_model="terra")
    service._admit()
    if transition == "workspace-ready":
        service.store.update_job(job, state="waiting", wait_reason="workspace", next_check_at=utcnow())
    seen = notices(service)
    service._admit()
    assert not service.store.list_attempts(job)
    ready = service.store.query("SELECT 1 FROM events WHERE job_id=? AND kind='job.workspace_ready'", (job,))
    assert bool(ready) == (transition == "workspace-ready")
    # The pass's own transition reached the watch on this thread, as a `jobs` notice.
    assert [category for category, _ in seen] == (["jobs"] if transition == "workspace-ready" else [])
    assert {ident for _, ident in seen} <= {threading.get_ident()}
    assert service._admission_rest_until > daemon_module.time.monotonic()
    assert not getattr(service._admission_thread, "active", False)      # restored after the pass


@pytest.mark.parametrize("source", ["own-thread-requeue", "other-thread-requeue", "own-thread-lease",
                                    "own-thread-requeue-after-pass"])
def test_only_the_pass_threads_own_queue_transition_is_ignored(paced, source):
    service, harness, clock = paced
    job = service.dispatch("submit", harness.submit_args())["job_id"]
    service.store.update_job(job, state="waiting", wait_reason="capacity", next_check_at=utcnow())
    service.store.acquire_lease("out:/fixture", "fixture")

    def requeue():
        service.store.update_job(job, state="queued", wait_reason=None, next_check_at=None)

    def during():
        if source == "own-thread-requeue":
            requeue()
        elif source == "other-thread-requeue":
            thread = threading.Thread(target=requeue)
            thread.start()
            thread.join(30)
            assert not thread.is_alive()
        elif source == "own-thread-lease":
            service.store.release_leases("fixture")

    clock.during = during
    clock.offer()
    assert len(clock.starts) == 1
    if source == "own-thread-requeue-after-pass":
        assert service._admission_rest_until > clock.now
        requeue()                                       # the pass is over: this thread is no pass now
    resting = source == "own-thread-requeue"
    assert (service._admission_rest_until > clock.now) is resting
    clock.during = None
    clock.now += .05
    clock.offer()
    assert len(clock.starts) == (1 if resting else 2)
