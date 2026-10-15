"""C-11.8: permanent all-lane retry refusals have the pin grace; timed waits do not."""
from datetime import timedelta

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters.registry import register
from subfleet.contracts import ClockSource, Closure, ClosureReason, Outcome, OutcomeClass
from tests.fake.test_review_r223_wall_probes import clocked  # noqa: F401
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.fake_adapter import FakeAdapter


def retry(clocked):
    service, harness, clock = clocked

    class Transient(FakeAdapter):
        def classify(self, *args):
            return Outcome(OutcomeClass.TRANSIENT, "fixture transport disconnected")

    register("codex", Transient)
    jid, first, adir = reserve(service, harness, caller_session="caller")
    service._finalize(receipt_fixture(service, first, adir, rc=1))
    return service, clock, jid


@pytest.mark.parametrize("refusal", ["disabled", "credential-latched", "no-lanes", "auth-dead"])
def test_permanent_retry_refusal_notifies_once_survives_restart_and_fails(clocked, refusal):
    service, clock, jid = retry(clocked)
    if refusal == "disabled":
        service.store.update_lane("codex-1", enabled=False)
    elif refusal == "credential-latched":
        service.timers.metadata["codex-1"] = {"probe_status": "revoked"}
    elif refusal == "auth-dead":
        service.store.add_closure(Closure("codex-1", "account", (clock.t + timedelta(days=1)).isoformat(),
                                          ClosureReason.AUTH_DEAD, ClockSource.REPORTED, "fixture"))
    else:
        service.policy["models"]["astra"]["provider"] = "claude"
    service._admit()
    clock.t += timedelta(seconds=daemon_module.PIN_NOTICE_AFTER_S)
    for _ in range(3):
        service._admit()
    notices = service.store.query("SELECT * FROM service_notices WHERE session_id='caller'")
    assert len(notices) == 1
    words = {"disabled": "disabled", "credential-latched": "credential", "no-lanes": "no lane",
             "auth-dead": "credential"}[refusal]
    assert words in notices[0]["text"]
    service._pin_episodes = service._load_pin_episodes()
    clock.t += timedelta(seconds=1800)
    service._admit()
    job = service.store.get_job(jid)
    assert (job["state"], job["rc"], job["cancel_requested_at"]) == ("failed", 3, None)
    assert len(service.store.list_attempts(jid)) == 1
    assert words in service.store.list_notices("caller")[0]["text"]


@pytest.mark.parametrize("reason", [ClosureReason.OPERATOR_HOLD, ClosureReason.PROVIDER_LIMIT])
def test_known_reset_and_operator_hold_do_not_start_refusal_grace(clocked, reason):
    service, clock, jid = retry(clocked)
    until = (clock.t + timedelta(days=90)).isoformat()
    service.store.add_closure(Closure("codex-1", "account", until, reason,
                                      ClockSource.REPORTED, "fixture"))
    service._admit()
    clock.t += timedelta(days=30)
    service._admit()
    job = service.store.get_job(jid)
    assert (job["state"], job["rc"], job["cancel_requested_at"]) == ("waiting", None, None)
    assert service.store.query("SELECT 1 FROM events WHERE job_id=? AND kind='job.pin_unadmittable'", (jid,)) == []
    assert service.store.query("SELECT 1 FROM service_notices WHERE session_id='caller'") == []


def test_temporary_lane_recovery_ends_refusal_episode_and_restarts_grace(clocked):
    service, clock, jid = retry(clocked)
    service.store.update_lane("codex-1", enabled=False)
    service._admit()
    first = service._pin_episodes[jid]["since"]
    clock.t += timedelta(seconds=100)
    service.store.update_lane("codex-1", enabled=True)
    service.store.add_closure(Closure("codex-1", "account", (clock.t + timedelta(days=10)).isoformat(),
                                      ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "fixture"))
    service._admit()
    assert not service._pin_episodes[jid]["open"]
    service.store.update_lane("codex-1", enabled=False)
    clock.t += timedelta(seconds=2000)
    service._admit()
    assert service.store.get_job(jid)["state"] == "waiting"
    assert service._pin_episodes[jid]["since"] != first
    assert service._pin_episodes[jid]["open"]


def test_one_temporarily_closed_candidate_prevents_all_lane_refusal(clocked):
    from dataclasses import replace
    service, clock, jid = retry(clocked)
    lane = service.store.get_lane("codex-1")
    service.store.put_lane(replace(lane, lane_id="codex-2", account_key="codex:second"))
    service.store.update_lane("codex-1", enabled=False)
    service.store.add_closure(Closure("codex-2", "account", (clock.t + timedelta(days=90)).isoformat(),
                                      ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "fixture"))
    service._admit()
    clock.t += timedelta(days=30)
    service._admit()
    assert service.store.get_job(jid)["state"] == "waiting"
    assert not service._pin_episodes.get(jid, {}).get("open")
