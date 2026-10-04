"""Per-job reserve authorization crosses durable submission and probe admission.

All provider outcomes are synthetic. The state fixture refuses guardian launches;
these tests exercise the real scheduler, probe publication, and reservation path.
"""

import json

import pytest

from subfleet import daemon as daemon_module, protocol
from subfleet.adapters import registry
from subfleet.contracts import (
    ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner,
    Outcome, OutcomeClass, Reading, ReadingLabel,
)
from subfleet.daemon import after, utcnow
from tests.fake.test_state_contract import receipt_fixture, state_daemon
from tests.fake_adapter import FakeAdapter


LANE = "claude-reserve"
EMAIL = "reserve@example.test"
MODEL = "claude-opus-5-5"
REASON = "Operator accepts unknown Fable reserve for this exact Opus job."


@pytest.fixture
def reserve_state(state_daemon, monkeypatch):
    service, harness = state_daemon
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    service.desktop_prober = lambda: None
    service.policy["reserve"] = {"models": ["fable"], "cap_ratio": 2., "min_slack": .05}
    home = service.root / "claude-home"
    home.mkdir()
    service.store.put_lane(Lane(LANE, "claude", "claude:" + EMAIL,
        Credential("claude", str(home), "home"), str(home), LaneOwner.V2, False,
        label=EMAIL))

    def unexpected_probe(*args):
        pytest.fail("blocked work must not reach a provider probe")

    monkeypatch.setattr(service, "_execute_probe", unexpected_probe)
    return service, harness


def submit_args(harness, *, authorized=False, **changes):
    values = {"pinned_lane": LANE, "pinned_model": "opus", "tier": "standard",
              "sandbox": "read-only", **changes}
    if authorized:
        values["unmeasured_reserve_reason"] = REASON
    return harness.submit_args(**values)


def install_probe(service, monkeypatch, outcome=None, *, before_return=None):
    calls = []

    def probe(job, lane, model, holder):
        assert not service.store.conn.in_transaction
        assert not service.store.list_attempts(job["job_id"])
        assert service.store.list_leases(holder)
        assert job["sandbox"] == "read-only" and job["tier"] == "standard"
        calls.append((job["job_id"], lane.lane_id, model["id"]))
        if before_return:
            before_return()
        return outcome or Outcome(OutcomeClass.OK, "same-model admission succeeded", {"rc": 0})

    monkeypatch.setattr(service, "_execute_probe", probe)
    return calls


def test_unmeasured_reserve_without_authorization_waits_without_probe(reserve_state):
    service, harness = reserve_state
    job_id = service.dispatch("submit", submit_args(harness))["job_id"]
    service._admit()
    assert service.store.get_job(job_id)["state"] == "waiting"
    assert not service.store.list_attempts(job_id)
    assert not service.store.list_leases()
    assert "reserve:fable:unmeasured" in service.dispatch("why", {"job_id": job_id})["text"]


def test_standard_readonly_authorization_probes_exact_model_before_attempt(reserve_state, monkeypatch):
    service, harness = reserve_state
    calls = install_probe(service, monkeypatch)
    # Aliases are frozen to the exact lane and model before authorization is stored.
    args = submit_args(harness, authorized=True, pinned_lane=EMAIL, pinned_model=MODEL)
    job_id = service.dispatch("submit", args)["job_id"]
    job = service.store.get_job(job_id)
    assert job["pinned_lane"] == LANE and job["pinned_model"] == "opus"
    assert job["unmeasured_reserve_reason"] == REASON
    manifest = json.loads((service.root / "jobs" / job_id / "manifest.json").read_text())
    assert manifest["job"]["unmeasured_reserve_reason"] == REASON
    assert any(REASON in event["data_json"] for event in service.store.list_events(job_id))

    service._admit()
    assert calls == [(job_id, LANE, MODEL)]
    attempt, = service.store.list_attempts(job_id)
    assert attempt["lane_id"] == LANE and attempt["model_requested"] == MODEL
    events = [event["kind"] for event in service.store.list_events(job_id)]
    assert events.index("probe.completed") < events.index("attempt.reserved")
    reading, = service.store.list_readings()
    assert reading["scope"] == MODEL and reading["label"] == "admission-observed"
    assert reading["utilization"] is None
    assert service.policy["reserve"]["models"] == ["fable"]


@pytest.mark.parametrize("result", ["limited", "unknown", "identity-unverified", "identity-mismatch"])
def test_authorization_never_turns_failed_or_unbound_probe_into_work(reserve_state, monkeypatch, result):
    service, harness = reserve_state
    if result.startswith("identity-"):
        outcome = Outcome(OutcomeClass.OK, "provider returned another or unverified identity",
                          {"rc": 0, "identity": {"status": result}})
    else:
        outcome = Outcome(OutcomeClass(result), "probe did not admit requested model", {"rc": 1})
    calls = install_probe(service, monkeypatch, outcome)
    job_id = service.dispatch("submit", submit_args(harness, authorized=True))["job_id"]
    service._admit()
    assert calls == [(job_id, LANE, MODEL)]
    assert not service.store.list_attempts(job_id)
    assert not service._pending_launches
    assert service.store.get_job(job_id)["state"] == "waiting"
    assert not service.store.list_leases()
    assert not service.store.list_readings()
    if result == "limited":
        closure, = service.store.list_closures()
        assert closure["lane_id"] == LANE and closure["scope"] == MODEL
    else:
        assert not service.store.list_closures()


@pytest.mark.parametrize("blocker", ["closure", "measured-reserve", "floor"])
def test_successful_probe_cannot_override_new_capacity_evidence(reserve_state, monkeypatch, blocker):
    service, harness = reserve_state

    def new_evidence():
        if blocker == "closure":
            service.store.add_closure(Closure(LANE, MODEL, after(3600),
                ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "concurrent provider evidence"))
        elif blocker == "floor":
            service.store.add_reading(Reading(LANE, "account", "five_hour", .95, after(3600),
                ReadingLabel.PROVIDER, "oauth-usage", utcnow()))
        else:
            observed = utcnow()
            for scope, used in (("account", .4), ("claude-fable-5-1", .2)):
                service.store.add_reading(Reading(LANE, scope, "seven_day", used, after(86400),
                    ReadingLabel.PROVIDER, "oauth-usage", observed))

    calls = install_probe(service, monkeypatch, before_return=new_evidence)
    job_id = service.dispatch("submit", submit_args(harness, authorized=True))["job_id"]
    service._admit()
    assert calls == [(job_id, LANE, MODEL)]
    assert not service.store.list_attempts(job_id)
    assert not service._pending_launches
    assert service.store.get_job(job_id)["state"] == "waiting"
    expected = {"closure": "closed:", "measured-reserve": "reserve:fable:reserved", "floor": "below-floor"}
    assert expected[blocker] in service.dispatch("why", {"job_id": job_id})["text"]


@pytest.mark.parametrize("continuation", [False, True], ids=["next-job", "resume"])
def test_authorization_and_probe_admission_do_not_carry_to_next_job(reserve_state, monkeypatch, continuation):
    service, harness = reserve_state
    calls = install_probe(service, monkeypatch)
    source = service.dispatch("submit", submit_args(harness, authorized=True))["job_id"]
    service._admit()
    attempt, = service.store.list_attempts(source)
    service._pending_launches.discard(attempt["attempt_id"])
    service.store.update_attempt(attempt["attempt_id"], native_session_id="synthetic-source-session")
    adir = service.root / "jobs" / source / "a1"
    adir.mkdir()
    service._finalize(receipt_fixture(service, attempt, adir))
    assert service.store.get_job(source)["state"] == "succeeded"
    assert not service.store.list_leases()
    changes = {"kind": "resume", "parent_job_id": source} if continuation else {}
    next_id = service.dispatch("submit", submit_args(harness, **changes))["job_id"]
    assert service.store.get_job(next_id)["unmeasured_reserve_reason"] is None
    service._admit()
    assert not service.store.list_attempts(next_id)
    assert service.store.get_job(next_id)["state"] == "waiting"
    assert calls == [(source, LANE, MODEL)]
    assert "reserve:fable:unmeasured" in service.dispatch("why", {"job_id": next_id})["text"]


def test_authorization_reason_is_part_of_durable_request_identity(reserve_state):
    service, harness = reserve_state
    args = submit_args(harness, authorized=True)
    first = service.dispatch("submit", args)
    again = service.dispatch("submit", args)
    assert first["created"] and not again["created"] and first["job_id"] == again["job_id"]
    for reason in ("A different operator justification.", None):
        with pytest.raises(protocol.ProtocolError, match="different payload"):
            service.dispatch("submit", {**args, "unmeasured_reserve_reason": reason})
    assert len(service.store.list_jobs()) == 1
    assert service.store.get_job(first["job_id"])["unmeasured_reserve_reason"] == REASON
