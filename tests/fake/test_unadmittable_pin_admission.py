"""C-11.8, C-6.9, C-6.11, C-15.2: a job pinned to a lane that can never admit it, in the daemon.

Incident, 2026-09-29 ~21:53Z: job 20260929-134236-salvage-r3-fix was submitted
with `-a claude-9`. An account switch made claude-9 the desktop login lane;
`subfleet why` said "no lane admits it (desktop)", the job stayed queued with no
word to its caller, and C-6.9's FIFO held 38 younger standard Opus jobs behind
it while claude-7 was open.

Now admission holds such a job `pin-unadmittable` on every pass before it is
compared with any other, so it holds nobody back; its caller's session gets one
notice naming the lane, the reason and the fix; and once `admission.pin_grace_s`
(30 minutes) has passed with the lane still so, the job fails with rc 3 (C-17.3,
no lane) and the terminal notice every ended job gets (C-15.1).
"""

from __future__ import annotations

import contextlib
import copy
import json
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet import scheduler
from subfleet.adapters import registry
from subfleet.capacity import _time
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading,
                                ReadingLabel)
from subfleet.daemon import Daemon, after, utcnow
from subfleet.policy import admission_settings
from tests.caps import capped
from tests.fake.conftest import Harness
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake_adapter import FakeAdapter

FAR_FUTURE = "2099-12-31T00:00:00Z"
RAISES = (ValueError, KeyError, TypeError, AttributeError, IndexError)


def claude(service, lane_id: str, *, owner=LaneOwner.V2, desktop=False, enabled=True, measured=True) -> None:
    service.store.put_lane(Lane(lane_id, "claude", f"claude:{lane_id}@example.invalid",
                                Credential("claude", f"/fake/{lane_id}", "keychain-token"), None, owner, desktop,
                                enabled, None, f"{lane_id}@example.invalid"))
    if measured:
        service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400),
                                          ReadingLabel.PROVIDER, "fixture", utcnow()))


@pytest.fixture
def fleet(routing_state):  # noqa: F811
    """claude-9 (the lane jobs pin) and claude-7 (open), both measured; Claude Code in use."""
    service, harness = routing_state
    claude(service, "claude-9")
    claude(service, "claude-7")
    service._desktop_in_use = lambda: True             # C-10.3: the desktop login is in use
    return service, harness


def submit(service, harness, **changes) -> str:
    values = {"pinned_model": "opus", "pinned_lane": "claude-9", "caller_session": "caller-session", **changes}
    return service.dispatch("submit", harness.submit_args(**values))["job_id"]


def service_notices(service, session="caller-session") -> list[dict]:
    return service.store.query("SELECT * FROM service_notices WHERE session_id=? ORDER BY notice_id", (session,))


def events(service, job_id: str, kind: str) -> list[dict]:
    return [{**row, "data": json.loads(row["data_json"])} for row in service.store.query(
        "SELECT * FROM events WHERE job_id=? AND kind=? ORDER BY event_id", (job_id, kind))]


def expire(service, job_id: str) -> None:
    """Move the job's grace into the past: no test here waits on a real clock."""
    service._pin_episodes[job_id] = {**service._pin_episodes[job_id], "fail_at": "2000-01-01T00:00:00Z"}


def make_unadmittable(service, reason: str, job_id: str | None = None) -> str:
    """Put claude-9 (or the job) in the state that refuses it for `reason`; the label expected."""
    store = service.store
    if reason == "desktop":
        store.update_lane("claude-9", desktop=1)
    elif reason == "disabled":
        store.update_lane("claude-9", enabled=0)
    elif reason == "owner-v1":
        store.update_lane("claude-9", owner="v1")
    elif reason == "identity-mismatch":
        store.update_lane("claude-9", identity_status="mismatch")
    elif reason == "credential-latched":
        service.timers.metadata["claude-9"] = {"probe_status": "revoked"}
    elif reason == "held":
        store.add_closure(Closure("claude-9", "account", FAR_FUTURE, ClosureReason.OPERATOR_HOLD,
                                  ClockSource.REPORTED, "operator"))
        return f"closed:account:{FAR_FUTURE}"
    elif reason == "unknown":
        store.update_job(job_id, pinned_lane="claude-99")           # a lane no longer in the roster
    elif reason == "excluded":
        store.update_job(job_id, exclusions=json.dumps(["claude-9"]))
    return reason


REASONS = ["desktop", "disabled", "owner-v1", "identity-mismatch", "credential-latched", "held", "unknown", "excluded"]


# --- the whole path, for each reason -------------------------------------------------------------

@pytest.mark.parametrize("reason", REASONS)
def test_c11_8_held_told_once_blocking_nobody_then_failed_rc_3(fleet, reason):
    service, harness = fleet
    capped(service.policy)                                  # C-6.9 holds back only in a capped pool
    stuck = submit(service, harness)
    label = make_unadmittable(service, reason, stuck)
    younger = submit(service, harness, pinned_lane=None)     # standard Opus work, as on 2026-09-29
    service._admit()

    # Held for what it is, before it could hold anyone: the younger job is placed on claude-7.
    hold = service._holds[stuck]
    assert (hold["reason"], hold["reasons"]) == ("pin-unadmittable", [label])
    assert hold["lane_id"] == ("claude-99" if reason == "unknown" else "claude-9")
    assert not service.store.list_attempts(stuck)
    assert [a["lane_id"] for a in service.store.list_attempts(younger)] == ["claude-7"]
    job = service.store.get_job(stuck)
    assert (job["state"], job["wait_reason"], job["next_check_at"]) == ("waiting", "capacity", hold["fail_at"])
    grace = admission_settings(service.policy)["pin_grace_s"]
    assert abs((_time(hold["fail_at"]) - _time(hold["since"])).total_seconds() - grace) <= 1

    # One notice, to the session that submitted it, naming the lane, the reason and the fix.
    notices = service_notices(service)
    assert len(notices) == 1
    text = notices[0]["text"]
    assert text.startswith(f"{stuck}: waiting; its pinned lane {hold['lane_id']} can never admit it: ")
    assert "resubmit it unpinned, or pinned to another lane" in text and f"subfleet kill {stuck}" in text
    assert f"fails with rc 3 at {hold['fail_at']}" in text
    recorded = events(service, stuck, "job.pin_unadmittable")
    assert len(recorded) == 1 and recorded[0]["data"]["service_notice_id"] == notices[0]["notice_id"]
    assert recorded[0]["data"]["reasons"] == [label]

    # More passes: still held, still one notice, still one episode.
    for _ in range(3):
        service._admit()
    assert service._holds[stuck]["reason"] == "pin-unadmittable"
    assert len(service_notices(service)) == 1 and len(events(service, stuck, "job.pin_unadmittable")) == 1

    # `why` says it in words a person can act on.
    why = service.dispatch("why", {"job_id": stuck})["text"]
    assert f"Held: its pinned lane {hold['lane_id']} can never admit it" in why
    assert "it holds no other job back" in why and "Fix: resubmit it unpinned" in why

    # The grace passes with the lane still so: failed, rc 3, with the terminal notice.
    expire(service, stuck)
    service._admit()
    job = service.store.get_job(stuck)
    assert (job["state"], job["rc"], job["wait_reason"], job["next_check_at"]) == ("failed", 3, None, None)
    terminal = service.store.query("SELECT * FROM notices WHERE job_id=?", (stuck,))
    assert len(terminal) == 1 and f"{stuck}: failed; rc=3;" in terminal[0]["text"]
    assert f"no lane: its pinned lane {hold['lane_id']} could never admit it" in terminal[0]["text"]
    assert len(service_notices(service)) == 1                   # no second service notice
    assert len(events(service, stuck, "job.pin_refused")) == 1
    why = service.dispatch("why", {"job_id": stuck})
    assert why["refused"].startswith(f"no lane: its pinned lane {hold['lane_id']} could never admit it")


def test_c6_9_c11_8_the_incident_a_desktop_pin_holds_back_none_of_the_opus_jobs_after_it(fleet):
    """2026-09-29: the salvage job was already waiting on its clock when claude-9 became the
    desktop lane; 38 standard Opus jobs came after it. In a capped pool, before C-11.8,
    every one was held `behind-older-job`."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 64
    older = submit(service, harness, pinned_model=None, task="build", tier="standard")
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(30))
    service.store.update_lane("claude-9", desktop=1)
    younger = [submit(service, harness, pinned_lane=None, pinned_model=None, task="review", tier="standard")
               for _ in range(38)]
    service._admit()
    assert service._holds[older]["reasons"] == ["desktop"]
    placed = {job_id: [a["lane_id"] for a in service.store.list_attempts(job_id)] for job_id in younger}
    assert placed == {job_id: ["claude-7"] for job_id in younger}
    assert not any(hold.get("reason") == "behind-older-job" for hold in service._holds.values())


# --- episodes: the lane recovers, relapses, the daemon restarts ---------------------------------

def test_c11_8_a_lane_that_recovers_takes_its_job_at_once_and_ends_the_episode(fleet):
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    assert service._holds[stuck]["reason"] == "pin-unadmittable"
    service.store.update_lane("claude-9", enabled=1)       # an operator re-enabled it within the grace
    service._admit()
    assert [a["lane_id"] for a in service.store.list_attempts(stuck)] == ["claude-9"]
    ended = events(service, stuck, "job.pin_admittable")
    assert len(ended) == 1 and ended[0]["data"]["since"] == events(service, stuck, "job.pin_unadmittable")[0]["data"]["since"]


def test_c11_8_one_notice_per_job_however_often_its_lane_flips(fleet):
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-7", enabled=0)       # nowhere else to go, so it keeps waiting
    for flip in range(3):
        service.store.update_lane("claude-9", desktop=1)
        service._admit()
        assert service._holds[stuck]["reason"] == "pin-unadmittable"
        service.store.update_lane("claude-9", desktop=0)
        service.store.add_closure(Closure("claude-9", "account", after(600), ClosureReason.PROVIDER_LIMIT,
                                          ClockSource.REPORTED, "fixture"))   # a wait, not a hold
        service._admit()
        assert service._holds[stuck]["reason"] != "pin-unadmittable"
        with service.store.transaction("fixture.release") as tx:
            tx.execute("UPDATE closures SET released_at=? WHERE lane_id='claude-9'", (utcnow(),))
    assert len(service_notices(service)) == 1
    started = events(service, stuck, "job.pin_unadmittable")
    assert len(started) == 3 and [row["data"]["service_notice_id"] is not None for row in started] == [True, False, False]
    assert len(events(service, stuck, "job.pin_admittable")) == 3


def test_c11_8_a_restart_keeps_the_clock_and_sends_no_second_notice(routing_state):  # noqa: F811
    service, harness = routing_state
    claude(service, "claude-9")
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    fail_at = service._holds[stuck]["fail_at"]
    root = service.root
    service.close()
    again = Daemon(root)
    try:
        assert again._pin_episodes[stuck] == {**again._pin_episodes[stuck], "open": True, "fail_at": fail_at,
                                              "noticed": True}
        again._admit()
        assert again._holds[stuck]["fail_at"] == fail_at
        assert len(service_notices(again)) == 1 and len(events(again, stuck, "job.pin_unadmittable")) == 1
        expire(again, stuck)
        again._admit()
        assert again.store.get_job(stuck)["rc"] == 3
    finally:
        again.close()


# --- settings and what is left alone -------------------------------------------------------------

def test_c11_8_with_no_grace_it_fails_on_the_pass_that_finds_it_with_one_notice(fleet):
    service, harness = fleet
    service.policy["admission"]["pin_grace_s"] = 0
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    assert service.store.get_job(stuck)["rc"] == 3
    assert service_notices(service) == []                                    # the terminal notice says it all
    assert len(service.store.query("SELECT * FROM notices WHERE job_id=?", (stuck,))) == 1


def test_c11_8_with_a_null_grace_it_waits_and_is_told_so(fleet):
    service, harness = fleet
    service.policy["admission"]["pin_grace_s"] = None
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    for _ in range(3):
        service._admit()
    assert service.store.get_job(stuck)["state"] == "waiting" and service._holds[stuck]["fail_at"] is None
    assert "it waits until that changes" in service_notices(service)[0]["text"]
    assert "it waits until that changes" in service.dispatch("why", {"job_id": stuck})["text"]


def test_c11_8_a_closure_within_the_horizon_is_a_wait_not_a_hold(fleet):
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.add_closure(Closure("claude-9", "account", after(6 * 86400), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    service._admit()
    assert service._holds[stuck]["reason"] != "pin-unadmittable"
    assert service_notices(service) == [] and not events(service, stuck, "job.pin_unadmittable")


@pytest.mark.parametrize("wait_reason", ["approval", "uncertain"])
def test_c11_8_a_wait_that_is_a_persons_is_left_to_them(fleet, wait_reason):
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service.store.update_job(stuck, state="waiting", wait_reason=wait_reason, next_check_at=after(3600))
    service._admit()
    assert service._holds[stuck] == {"reason": wait_reason}
    assert service_notices(service) == []


def test_c11_8_a_job_with_an_attempt_still_quarantined_is_held_not_failed(fleet):
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    with service.store.transaction("fixture.quarantine") as tx:
        tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                   "VALUES(?,?,1,'claude-9','claude-opus-5-5','quarantined',?)", (f"{stuck}/a1", stuck, utcnow()))
    expire(service, stuck)
    service._admit()
    assert service.store.get_job(stuck)["state"] == "waiting"
    assert service._holds[stuck]["reason"] == "pin-unadmittable"


def test_c11_8_a_turn_is_held_and_nothing_more(fleet):
    """C-26.12: a turn's message is the person's and the conversation carries its end."""
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    job = {**service.store.get_job(stuck), "kind": "turn"}
    stuck_pin = scheduler.pin_unadmittable(service.policy, service._pin_view(service._desktop_identity()), job)
    holds = {}
    service._stuck_pin(job, stuck_pin, holds)
    assert holds[stuck]["reason"] == "pin-unadmittable" and holds[stuck]["fail_at"] is None
    service._stuck_pin(job, stuck_pin, holds)
    assert service.store.get_job(stuck)["state"] == "waiting" and service_notices(service) == []


def test_c11_8_a_job_every_lane_refuses_for_good_holds_nobody_back_either(fleet):
    """C-6.9: not only a pin. An Opus job with every Claude lane disabled waits for no slot,
    so a later job whose chain promotes to Astra is not held behind it."""
    service, harness = fleet
    capped(service.policy)
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    older = submit(service, harness, pinned_lane=None)
    for lane_id in ("claude-9", "claude-7"):
        service.store.update_lane(lane_id, enabled=0)
    younger = submit(service, harness, pinned_lane=None, pinned_model=None, task="review", tier="standard")
    service._admit()
    assert service._holds[older]["for_good"] == ["disabled"]
    assert [a["lane_id"] for a in service.store.list_attempts(younger)] == ["codex-1"]
    assert "holds no other job back" in service.dispatch("why", {"job_id": older})["text"]


# --- the property, over the daemon itself ------------------------------------------------------------

@contextlib.contextmanager
def daemon_for(root):
    root.mkdir(parents=True)
    harness = Harness(root)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        service = Daemon(root)
        try:
            yield service, harness
        finally:
            service.close()


def oracle(service, policy: dict) -> callable:
    """Is `job` placed anywhere in the best capacity the store's standing facts allow?

    Written apart from C-11.8's code: the daemon's own view, with every count cap
    lifted, every lane measured idle, nothing in flight, no probe, and every closure
    that ends within the horizon ended; then `evaluate`."""
    view = service._capacity_view(service._desktop_identity())
    far = admission_settings(policy)["pin_hold_far_s"]
    now = _time(view["now"])
    best = copy.deepcopy(view)
    best["readings"] = [{"reading_id": n + 1, "lane_id": lane["lane_id"], "scope": "account", "window": "seven_day",
                         "utilization": 0.0, "resets_at": after(3 * 86400), "label": "provider",
                         "source": "oauth-usage", "observed_at": view["now"], "attempt_id": None}
                        for n, lane in enumerate(view["lanes"])]
    best.update(in_flight={}, in_flight_turns={}, attempts=[], reserved_probes=0,
                unavailable_lanes={k: v for k, v in view["unavailable_lanes"].items() if v == "credential-latched"},
                closures=[row for row in view["closures"] if (_time(row["until_at"]) - now).total_seconds() > far])
    free = copy.deepcopy(policy)
    free["caps"].update(max_active_attempts=None, max_in_flight_per_lane=None, max_in_flight_unmeasured=None,
                        max_active_attempts_per_parent=None)

    def admissible(job: dict) -> bool | None:
        try:
            return scheduler.evaluate(free, best, job).chosen_lane is not None
        except RAISES:
            return None
    return admissible


STATES = st.sampled_from(["open", "open", "open", "desktop", "disabled", "owner-v1", "identity-mismatch",
                          "credential-latched", "held-far", "closed-near", "full"])


@settings(max_examples=120, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                 HealthCheck.data_too_large])
@given(st.data())
def test_c6_9_c11_8_in_the_daemon_no_job_waits_behind_a_job_no_lane_can_admit(tmp_path_factory, data):
    """For any fleet of Claude lanes in any state, any queue of jobs pinned to them (or to
    no lane, or unpinned) and a capped pool: after a pass, no hold names, as the job it
    waits behind or keeps a slot for, a job no lane can admit; and every pinned job no
    lane can admit is held `pin-unadmittable`."""
    draw = data.draw
    with daemon_for(tmp_path_factory.mktemp("pins") / "state") as (service, harness):
        service._desktop_in_use = lambda: True
        service.policy["caps"].update(max_active_attempts=draw(st.sampled_from([1, 2, 4]), label="fleet cap"),
                                      max_in_flight_per_lane=draw(st.sampled_from([1, 2]), label="lane cap"))
        lanes = [f"claude-{n}" for n in range(1, draw(st.integers(1, 4), label="lanes") + 1)]
        for lane_id in lanes:
            claude(service, lane_id)
            state = draw(STATES, label=f"{lane_id} state")
            if state in ("desktop", "disabled", "owner-v1", "identity-mismatch", "credential-latched"):
                make = {"desktop": dict(desktop=1), "disabled": dict(enabled=0), "owner-v1": dict(owner="v1"),
                        "identity-mismatch": dict(identity_status="mismatch")}.get(state)
                if make:
                    service.store.update_lane(lane_id, **make)
                else:
                    service.timers.metadata[lane_id] = {"probe_status": "revoked"}
            elif state in ("held-far", "closed-near"):
                service.store.add_closure(Closure(lane_id, "account", FAR_FUTURE if state == "held-far" else after(3600),
                                                  ClosureReason.OPERATOR_HOLD if state == "held-far"
                                                  else ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
            elif state == "full":
                service.store.add_reading(Reading(lane_id, "account", "seven_day", .99, after(86400),
                                                  ReadingLabel.PROVIDER, "fixture", utcnow()))
        jobs = []
        for n in range(draw(st.integers(2, 7), label="jobs")):
            pin = draw(st.sampled_from([*lanes, None, None, "claude-99"]), label=f"job {n} pin")
            job_id = submit(service, harness, pinned_lane=pin if pin != "claude-99" else lanes[0],
                            pinned_model=draw(st.sampled_from(["opus", "sonnet"]), label=f"job {n} model"))
            if pin == "claude-99":
                service.store.update_job(job_id, pinned_lane="claude-99")
            if draw(st.booleans(), label=f"job {n} waiting on its clock"):
                service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=after(30))
            jobs.append(job_id)
        admissible = oracle(service, service.policy)
        rows = {job_id: service.store.get_job(job_id) for job_id in jobs}
        service._admit()
        holds = service._holds
        named = [(job_id, hold.get("behind") or hold.get("kept_for")) for job_id, hold in holds.items()
                 if hold.get("behind") or hold.get("kept_for")]
        unadmittable = [job_id for job_id in jobs if admissible(rows[job_id]) is False]
        event(f"unadmittable jobs: {min(len(unadmittable), 2)}{'+' if len(unadmittable) >= 2 else ''}")
        event(f"holds naming another job: {min(len(named), 2)}{'+' if len(named) >= 2 else ''}")
        for job_id, older in named:
            assert admissible(rows[older]) is not False, f"{job_id} waits on {older}, which no lane can admit"
        for job_id in unadmittable:
            if rows[job_id]["pinned_lane"]:
                assert holds[job_id]["reason"] == "pin-unadmittable", (job_id, holds[job_id])
