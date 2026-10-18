"""C-11.8: the independent review of fix/unadmittable-pin (2026-09-30, Opus lane job
20260930-090849-unadmittable-pin-review) found these holes with these probes; each
failed on 76c81f9c and is kept as the regression test for its fix.

- I4: one notice per job, ever, also across a run and a restart (the notice is now
  recorded as a `job.pin_noticed` event and asked of the store when it is due);
- grace 0 read the clock twice (now once);
- I5: an expired token, which the timers heal (C-23.47), was called "never";
- I3: a job no lane can admit kept its place in a lease's queue on its clock;
- the notice never reached a caller Subfleet launched (it now names its job);
- a `for_good` from an earlier look outlived the lane's recovery in `why`.
"""

from __future__ import annotations

import json

from subfleet import daemon as daemon_module
from subfleet import scheduler
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake.test_unadmittable_pin_admission import (claude, events, fleet, service_notices,  # noqa: F401
                                                         settle, submit)


def _recover_without_placing(service) -> None:
    """claude-9 can take the job again, but a near closure (a wait) keeps it from being placed."""
    service.store.update_lane("claude-9", enabled=1)
    service.store.add_closure(Closure("claude-9", "account", after(600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))


def _release_closures(service) -> None:
    with service.store.transaction("fixture.release") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id='claude-9'", (utcnow(),))


# --- I4: one notice per job, ever -------------------------------------------------------------------

def test_probe_i4_a_job_that_ran_between_two_episodes_gets_a_second_notice(fleet):
    """C-11.8: "the first time for that job only, one notice". A pinned job can run and
    be retried (C-4.5: a first transient attempt, or a lost read-only one, puts it back
    `waiting`). While it runs it is not queued, so every pass prunes its `_pin_episodes`
    entry, and with it the record that its one notice went."""
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    assert len(service_notices(service)) == 1                      # the one notice

    _recover_without_placing(service)
    service._admit()
    assert len(events(service, stuck, "job.pin_admittable")) == 1   # episode over

    # As a reservation leaves it; one pass sees it running (and prunes its entry).
    service.store.update_job(stuck, state="running", wait_reason=None, next_check_at=None)
    service._admit()
    pruned = stuck not in service._pin_episodes

    # As a first transient attempt's end leaves it (`_finalize`: waiting, capacity, 60 s).
    _release_closures(service)
    service.store.update_job(stuck, state="waiting", wait_reason="capacity", next_check_at=after(60))
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    notices = service_notices(service)
    assert pruned
    assert len(notices) == 1, [row["text"][:80] for row in notices]


def test_probe_i4_a_restart_while_the_job_runs_forgets_its_notice(fleet):
    """C-11.8: "The episode's clock and its one notice survive a restart". `_load_pin_episodes`
    reads only jobs queued or waiting, so a job running at the restart and retried later
    is told again."""
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    _recover_without_placing(service)
    service._admit()
    service.store.update_job(stuck, state="running", wait_reason=None, next_check_at=None)
    root = service.root
    service.close()
    again = Daemon(root)
    try:
        again._desktop_in_use = lambda: True
        _release_closures(again)
        again.store.update_job(stuck, state="waiting", wait_reason="capacity", next_check_at=after(60))
        again.store.update_lane("claude-9", enabled=0)
        again._admit()
        settle(again, stuck)
        assert len(service_notices(again)) == 1, [row["text"][:80] for row in service_notices(again)]
    finally:
        again.close()


# --- grace 0: "fails it on the pass that finds it, with no service notice" ----------------------------

def test_probe_grace_0_across_a_second_boundary_sends_a_notice_and_does_not_fail(fleet, monkeypatch):
    """`now = utcnow()` and `fail_at = after(0)` are two reads of a one-second clock. When the
    second ticks between them, fail_at > now: the notice goes (the guard is `fail_at <= now`)
    and the job is not failed on this pass (the check is `now >= fail_at`)."""
    service, harness = fleet
    service.policy["admission"]["pin_grace_s"] = 0
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    job = service.store.get_job(stuck)
    view = service._pin_view(service._desktop_identity())
    found = scheduler.pin_unadmittable(service.policy, view, job)
    assert found is not None
    monkeypatch.setattr(daemon_module, "utcnow", lambda: "2026-09-30T12:00:00Z")
    monkeypatch.setattr(daemon_module, "after", lambda seconds: "2026-09-30T12:00:01Z")   # the tick
    holds: dict = {}
    service._stuck_pin(job, found, holds)
    assert service.store.get_job(stuck)["rc"] == 3 and service_notices(service) == [], (
        service.store.get_job(stuck)["state"], len(service_notices(service)))


# --- I5: no false "never" --------------------------------------------------------------------------

def test_probe_i5_an_expired_token_that_heals_itself_is_called_never(fleet):
    """`capacity.credential_latched` counts `expired-token`, which a Claude home lane's timer
    heals by itself (C-23.47: a heal turn, retried every 20 minutes; the adapter: "One heal
    turn is the answer, not a latch"). C-11.8 tells the caller the lane "can never admit it"
    and fails the job at the grace if a heal has not landed by then."""
    service, harness = fleet
    stuck = submit(service, harness)
    service.timers.metadata["claude-9"] = {"probe_status": "expired-token"}
    service._admit()
    assert service._holds[stuck].get("reason") != "pin-unadmittable", service._holds[stuck]
    assert not events(service, stuck, "job.pin_unadmittable") and service_notices(service) == []
    # A revoked credential is still the person's to fix.
    service.timers.metadata["claude-9"] = {"probe_status": "revoked"}
    service._admit()
    assert service._holds[stuck]["reasons"] == ["credential-latched"]


# --- I3: the lease FIFO ------------------------------------------------------------------------------

def test_probe_i3_an_unadmittable_job_on_its_clock_still_queues_for_a_lease(fleet):
    """C-6.9 (amended): a job no lane can admit "waits for no slot another job could take, so
    no later job is ... queued behind it for a lease". On the clock branch the lease FIFO
    (`queue_for`) runs whatever `for_good` says, and in an uncapped pool (the default)
    `for_good` is not even asked. Here an unpinned Opus job whose every Claude lane is
    disabled keeps, from its last look, a lease-held hold naming `out:` (kept for it), and
    a younger Astra job wanting that output path is held `queued_behind` it."""
    service, harness = fleet
    out = str(harness.root / "shared-out.md")
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    older = submit(service, harness, pinned_lane=None)                       # Opus, any lane
    for lane_id in ("claude-9", "claude-7"):
        service.store.update_lane(lane_id, enabled=0)
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(30))
    service._capacity_waits[older] = {"signature": "lease-held:out", "rechecks": 0, "since": utcnow(),
                                      "checked_at": utcnow(), "label": "lease-held", "expedite": True,
                                      "hold": {"reason": "lease-held", "leases": [], "queued": [f"out:{out}"],
                                               "queued_behind": ["someone-older"]}}
    assert scheduler.unadmittable(service.policy, service._pin_view(service._desktop_identity()),
                                  service.store.get_job(older)) == ["disabled"]
    younger = submit(service, harness, pinned_lane=None, pinned_model="astra", out_path=out)
    service._admit()
    hold = service._holds.get(younger) or {}
    assert older not in (hold.get("queued_behind") or ()), hold
    assert [a["lane_id"] for a in service.store.list_attempts(younger)] == ["codex-1"]      # it went


# --- the one notice reaches its caller ---------------------------------------------------------------

def test_probe_the_notice_is_never_surfaced_to_a_caller_subfleet_launched(fleet):
    """C-11.8: "one notice to its caller's session ..., a `service_notices` row that the session
    hooks surface (C-15.2)". `notice.pending` returns a service notice with `job_id` None, and
    inside a process Subfleet launched (an app conversation turn, a lane run: C-26.13) the
    SessionStart/UserPromptSubmit hook keeps only rows that name a job (`hooks.names_a_job`),
    so a caller there never sees it; only the rc 3 terminal notice (which names the job) does."""
    from subfleet import hooks
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    rows = service.dispatch("notice.pending", {"session_id": "caller-session"})["notices"]
    assert any(stuck in row["text"] for row in rows)                       # it is pending ...
    surfaced = [row for row in rows if hooks.names_a_job(row)]              # ... what a launched session keeps
    assert any(stuck in row["text"] for row in surfaced), [row["job_id"] for row in rows]


# --- what `why` says on the clock ---------------------------------------------------------------------

def test_probe_a_stale_for_good_outlives_the_lanes_recovery_on_the_clock(fleet):
    """The clock branch builds the hold as `{**known["hold"], **({"for_good": ...} if for_good else {})}`:
    a `for_good` a look recorded stays in the hold after a fresh check finds a lane that could
    take the job, so `why` says "it holds no other job back" of a job that is a waiter again."""
    from tests.caps import capped
    service, harness = fleet
    capped(service.policy)
    older = submit(service, harness, pinned_lane=None)                       # Opus, any Claude lane
    for lane_id in ("claude-9", "claude-7"):
        service.store.update_lane(lane_id, enabled=0)
    service._admit()
    assert service._holds[older].get("for_good") == ["disabled"]            # the look's finding
    service.store.update_lane("claude-7", enabled=1)                        # a lane could take it again
    service.store.update_job(older, next_check_at=after(30))                # still on its clock
    assert scheduler.unadmittable(service.policy, service._pin_view(service._desktop_identity()),
                                  service.store.get_job(older)) is None
    service._admit()
    assert "for_good" not in service._holds[older], service._holds[older]
