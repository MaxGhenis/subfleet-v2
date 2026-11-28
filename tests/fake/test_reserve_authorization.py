"""Per-job reserve authorization crosses durable submission and probe admission.

All provider outcomes are synthetic. The state fixture refuses guardian launches;
these tests exercise the real scheduler, probe publication, and reservation path.
"""

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from subfleet import daemon as daemon_module, protocol
from subfleet.adapters import registry
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.capacity import build_view
from subfleet.contracts import (
    ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Launch,
    Outcome, OutcomeClass, Reading, ReadingLabel,
)
from subfleet.credentials import resolve_credential
from subfleet.daemon import after, utcnow
from subfleet.scheduler import evaluate, reserve_verdict
from tests.conftest import exit_info, profile_body, profile_opener
from tests.fake.test_state_contract import receipt_fixture, state_daemon
from tests.fake_adapter import FakeAdapter
from tests.unit.test_scheduler import job
from tests.unit.test_scoped_stream_capacity import stand_in, stream


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
            for scope, used in (("account", .4), ("claude-fable-5-2", .2)):
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


@pytest.fixture
def scoped_lane(state_daemon, monkeypatch):
    daemon, _ = state_daemon
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    daemon.desktop_prober = lambda: None
    home = daemon.root / "claude-home"
    home.mkdir()
    (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "synthetic-token"}}))
    label = "claude-1@example.invalid"
    lane = Lane("claude-1", "claude", f"claude:{label}", Credential("claude", str(home), "home"),
                str(home), LaneOwner.V2, False, label=label)
    daemon.store.put_lane(lane)
    return lane


def scoped_profile(lane):
    return profile_opener(body=profile_body(lane.label, "synthetic-account", "synthetic-org"))


def classify_scoped_stream(adapter, attempt_dir, lane, model_id, *, status="allowed", turn=False):
    resets_at = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
    stdout = attempt_dir / "stdout"
    raw_stream = None if turn else attempt_dir / "stream.jsonl"
    (raw_stream or stdout).write_text(stream(status, resets_at=resets_at) + "\n")
    if turn:
        record = {"state": "complete"} if status == "allowed" else {"state": "failed", "reason": "limited"}
        (attempt_dir / "turn.json").write_text(json.dumps(record))
    # Old launches carry these same attempt facts, without a policy map.
    launch = Launch(
        argv=(), env_add=resolve_credential(lane.credential), env_remove=(), cwd=str(attempt_dir),
        stdin_path=None, stdout_path=str(stdout), stderr_path=str(attempt_dir / "stderr"),
        raw_stream_path=str(raw_stream) if raw_stream else None, native_session_id=None,
        lane_id=lane.lane_id, notes={"lane_id": lane.lane_id, "attempt_id": "previous-job/a1",
                                    "model_id": model_id, "label": lane.label},
    )
    return adapter.classify(attempt_dir, launch, exit_info(0 if status == "allowed" else 1))


@pytest.mark.parametrize("reserve", [[], ["fable"]], ids=["unreserved", "fable-reserved"])
def test_c9_9_the_daemon_hands_the_loaded_fable_id_to_the_usage_sensor(
    state_daemon, scoped_lane, tmp_path, monkeypatch, reserve,
):
    daemon, _ = state_daemon
    daemon.policy = stand_in(tmp_path, "L1", reserve)
    fable_id = daemon.policy["models"]["fable"]["id"]
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    resets_at = (now + timedelta(days=1)).isoformat()
    payload = {
        "five_hour": {"utilization": 0, "resets_at": resets_at},
        "seven_day": {"utilization": 55, "resets_at": resets_at},
        "limits": [{"kind": "weekly_scoped", "percent": 90, "resets_at": resets_at,
                    "scope": {"model": {"display_name": "Fable"}}}],
    }

    def usage_opener(request, timeout):
        assert request.get_header("Authorization") == "Bearer synthetic-token"
        return 200, json.dumps(payload).encode()

    def factory():
        return ClaudeAdapter(now=lambda: now, usage_opener=usage_opener,
                             profile_opener=scoped_profile(scoped_lane))

    monkeypatch.setattr(registry, "_factories", {**registry._factories, "claude": factory})
    adapter = daemon.timers.adapter_factory("claude")
    probe = adapter.probe_status(scoped_lane, resolve_credential(scoped_lane.credential))
    assert probe["status"] == "ok"
    rows = probe["readings"]
    fable, = [row for row in rows if row.scope != "account"]
    snapshot = build_view([scoped_lane], rows, now=now)
    if reserve:
        verdict = reserve_verdict(scoped_lane.lane_id, fable_id, [asdict(row) for row in rows], now=now,
                                  reading_ttl_s=daemon.policy["caps"]["reading_ttl_s"],
                                  reserve=daemon.policy["reserve"])
        opus = evaluate(daemon.policy, snapshot, job(pinned_model="opus"))
        assert (fable.scope, fable.window, verdict["slack"], opus.chosen_lane) == (
            fable_id, "seven_day", .25, scoped_lane.lane_id,
        ), verdict
    else:
        decision = evaluate(daemon.policy, snapshot, job(pinned_model="fable"))
        assert (fable.scope, fable.window, decision.chosen_lane) == (
            fable_id, "seven_day", None,
        ), decision.reason
        assert "below-floor" in decision.evaluations[0]["rejected"][0]["reasons"]


@pytest.mark.parametrize("tier", ["standard", "hard"])
def test_c9_8_the_daemon_admits_opus_beside_a_full_fable_bucket_under_the_loaded_policy(
    state_daemon, scoped_lane, tmp_path, monkeypatch, tier,
):
    daemon, harness = state_daemon
    daemon.policy = stand_in(tmp_path, "L1", ["fable"])
    opus_id = daemon.policy["models"]["opus"]["id"]

    def factory():
        return ClaudeAdapter(projects_dir=tmp_path / "projects", profile_opener=scoped_profile(scoped_lane))

    def unexpected_probe(*args):
        pytest.fail("a measured Opus route must admit without a provider probe")

    monkeypatch.setattr(registry, "_factories", {**registry._factories, "claude": factory})
    monkeypatch.setattr(daemon, "_execute_probe", unexpected_probe)
    adapter = daemon._adapter("claude")
    outcome = classify_scoped_stream(adapter, tmp_path, scoped_lane, opus_id)
    for row in outcome.readings:
        daemon.store.add_reading(row)

    args = harness.submit_args(pinned_model="opus", sandbox="read-only", tier=tier)
    job_id = daemon.dispatch("submit", args)["job_id"]
    daemon._admit()
    daemon._admit()

    attempts = daemon.store.list_attempts(job_id)
    state = daemon.store.get_job(job_id)["state"]
    assert (state, len(attempts)) == ("running", 1), daemon.dispatch("why", {"job_id": job_id})["text"]
    assert (attempts[0]["lane_id"], attempts[0]["model_requested"]) == (scoped_lane.lane_id, opus_id)
    assert all(event["kind"] != "probe.completed" for event in daemon.store.list_events(job_id))

    rejected = classify_scoped_stream(adapter, tmp_path, scoped_lane, opus_id, status="rejected")
    assert rejected.closure.scope == "claude-fable-5-1"


def test_c9_8_a_turns_fable_row_takes_the_loaded_fable_id(state_daemon, scoped_lane, tmp_path, monkeypatch):
    daemon, _ = state_daemon
    daemon.policy = stand_in(tmp_path, "L1", [])
    opus_id = daemon.policy["models"]["opus"]["id"]
    adapter = daemon.conversations.adapter("claude")
    monkeypatch.setattr(adapter.inner, "_profile_opener", scoped_profile(scoped_lane))
    monkeypatch.setattr(adapter.inner, "_projects_dir", tmp_path / "projects")

    outcome = classify_scoped_stream(adapter, tmp_path, scoped_lane, opus_id, turn=True)
    assert outcome.cls is OutcomeClass.OK
    fable, = [row for row in outcome.readings if row.scope != "account"]
    assert (fable.scope, fable.window) == ("claude-fable-5-1", "seven_day")

    rejected = classify_scoped_stream(adapter, tmp_path, scoped_lane, opus_id, status="rejected", turn=True)
    assert rejected.closure.scope == "claude-fable-5-1"
