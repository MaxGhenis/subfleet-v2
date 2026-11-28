"""The Fable weekly bucket must not strand spare Opus capacity."""

import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from subfleet.adapters.claude import ClaudeAdapter, SCOPED_MODEL_IDS
from subfleet.adapters.claude_stream import parse_stream
from subfleet.contracts import OutcomeClass
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate, probe_required
from tests.conftest import exit_info, make_launch, profile_opener
from tests.unit.test_scheduler import NOW, TOMORROW, closure, job, lane, reading, view


def stream(status="allowed", window="seven_day_overage_included", *, resets_at=None,
           scoped_window="seven_day_overage_included"):
    # Shape of the real Fable refusal: shared weekly 55%, Fable weekly 100%.
    short_reset = 1790000000 if resets_at is None else resets_at
    weekly_reset = 1790362800 if resets_at is None else resets_at
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": window, "resetsAt": weekly_reset,
        "unifiedWindows": {"five_hour": {"utilization": 0, "resetsAt": short_reset},
                           "seven_day": {"utilization": .55, "resetsAt": weekly_reset},
                           scoped_window: {"utilization": 1.01, "resetsAt": weekly_reset}}}})


def stand_in(tmp_path, name, reserve):
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw["models"]["fable"] = {"provider": "claude", "id": "claude-fable-5-1", "priority": 4,
                             "scope": "fable"}
    raw["retired"] = {"sol": "astra", "claude-fable-5": "fable"}
    if name == "L2":
        raw["models"]["sonnet"]["id"] = "claude-sonnet-5-5"
        raw["models"]["haiku"]["id"] = "claude-haiku-5-5"
    raw["reserve"]["models"] = list(reserve)
    path = tmp_path / f"policy-{name}.json"
    path.write_text(json.dumps(raw))
    return load_policy(path)


def scoped_stream(tmp_path, policy, attempt_model, event):
    adapter = ClaudeAdapter(projects_dir=tmp_path / "projects")
    adapter.scoped_models = {name: policy["models"][name]["id"] for name in SCOPED_MODEL_IDS}
    rows = adapter.readings_from_rate_limit(
        parse_stream(event).rate_limit, lane_id="claude-1", model_id=policy["models"][attempt_model]["id"],
        observed_at=NOW, attempt_id="probe/a1",
    )
    scoped, = [row for row in rows if row.scope != "account"]
    state = view([lane(account_key="claude:claude-1@example.invalid", home=str(tmp_path / "claude-home"))], rows)
    return (scoped.scope, scoped.window, scoped.utilization), state


def admission_summary(policy, state, alias):
    result = evaluate(policy, state, job(pinned_model=alias))
    evaluation, = result.evaluations
    reasons = tuple(reason for rejection in evaluation["rejected"] for reason in rejection["reasons"])
    detail = evaluation["candidate_details"].get("claude-1", {})
    return result.chosen_lane, reasons, detail.get("reserve", {}).get("slack")


@pytest.mark.parametrize("name", ["L1", "L2"])
@pytest.mark.parametrize("reserve", [[], ["fable"]], ids=["unreserved", "fable-reserved"])
@pytest.mark.parametrize("attempt_model", ["opus", "sonnet", "haiku", "fable"])
def test_c9_8_another_models_fable_row_takes_the_loaded_fable_id(tmp_path, name, reserve, attempt_model):
    """Fable's own event gives the same row and decisions as events from other families."""
    policy = stand_in(tmp_path, name, reserve)
    row, state = scoped_stream(tmp_path, policy, attempt_model, stream())
    decisions = {alias: admission_summary(policy, state, alias) for alias in SCOPED_MODEL_IDS}
    spare = ("claude-1", (), .45 if reserve else None)
    assert (row, decisions) == (
        ("claude-fable-5-1", "seven_day", 1.0),
        {"fable": (None, ("below-floor",), None), "opus": spare, "sonnet": spare, "haiku": spare},
    ), f"scoped row={row}; admission (lane, reasons, reserve slack)={decisions}"


def test_c9_8_another_models_sonnet_row_takes_the_loaded_sonnet_id(tmp_path):
    policy = stand_in(tmp_path, "L2", [])
    row, state = scoped_stream(tmp_path, policy, "opus", stream(scoped_window="seven_day_sonnet"))
    decision = admission_summary(policy, state, "sonnet")
    assert (row, decision) == (
        ("claude-sonnet-5-5", "seven_day", 1.0), (None, ("below-floor",), None),
    ), f"scoped row={row}; Sonnet admission (lane, reasons, reserve slack)={decision}"


@pytest.mark.parametrize("window,scope", [
    ("seven_day_overage_included", "claude-fable-5-1"),
    ("seven_day_opus", "claude-opus-5-5"),
    ("seven_day_sonnet", "claude-sonnet-5"),
    ("seven_day", "account"), ("five_hour", "account"), ("unknown", "account"),
])
def test_provider_rejection_closes_its_named_bucket(tmp_path, window, scope):
    adapter = ClaudeAdapter(now=lambda: datetime(2026, 9, 20, tzinfo=timezone.utc),
                            projects_dir=tmp_path / "projects", profile_opener=profile_opener())
    (tmp_path / "stream.jsonl").write_text(stream("rejected", window))
    (tmp_path / "stderr").write_text("")
    launch = make_launch(tmp_path, session_id="s", model_id="claude-fable-5-1")
    outcome = adapter.classify(tmp_path, launch, exit_info(1))
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.closure.scope == scope
    assert outcome.evidence["rate_limit"]["windows"]["seven_day_overage_included"]["utilization"] == 1.01


def test_same_stream_measures_spare_opus_without_spending_fable(tmp_path):
    adapter = ClaudeAdapter(projects_dir=tmp_path)
    rows = [asdict(r) for r in adapter.readings_from_rate_limit(
        parse_stream(stream()).rate_limit, lane_id="claude-1", model_id=SCOPED_MODEL_IDS["opus"],
        observed_at=NOW, attempt_id="probe/a1")]
    assert [(r["scope"], r["window"], r["utilization"]) for r in rows] == [
        ("account", "five_hour", 0), ("account", "seven_day", .55),
        (SCOPED_MODEL_IDS["fable"], "seven_day", 1.0)]
    policy = load_policy(DEFAULT_POLICY_PATH)
    state = view([lane()], rows)
    opus = evaluate(policy, state, job(pinned_model="opus"))
    assert opus.chosen_lane == "claude-1"
    assert opus.evaluations[0]["candidate_details"]["claude-1"]["reserve"]["slack"] == .45
    fable = evaluate(policy, state, job(pinned_model="fable"))
    assert fable.chosen_lane is None
    assert "below-floor" in fable.evaluations[0]["rejected"][0]["reasons"]


@pytest.mark.parametrize("difference", ["missing", "time", "attempt"])
def test_incomplete_or_unrelated_stream_windows_do_not_release_reserve(difference):
    rows = [reading("claude-1", .55, source="rate_limit_event", attempt_id="one")]
    if difference != "missing":
        rows.append(reading("claude-1", 1, scope=SCOPED_MODEL_IDS["fable"], source="rate_limit_event",
                            attempt_id="two" if difference == "attempt" else "one",
                            observed_at="2026-09-05T10:32:59Z" if difference == "time" else NOW))
    result = evaluate(load_policy(DEFAULT_POLICY_PATH), view([lane()], rows), job(pinned_model="opus"))
    assert result.chosen_lane is None
    assert "reserve:fable:unmeasured" in result.evaluations[0]["rejected"][0]["reasons"]


def test_reported_fable_exhaustion_requires_opus_probe_even_for_read_only():
    state = view([lane()], closures=[closure("claude-1", SCOPED_MODEL_IDS["fable"])])
    spec = job(pinned_model="opus")
    result = evaluate(load_policy(DEFAULT_POLICY_PATH), state, spec)
    assert result.chosen_lane == "claude-1"
    assert probe_required(result, spec)
    reserve = result.evaluations[0]["candidate_details"]["claude-1"]["reserve"]
    assert reserve["all_remaining"] is None  # no invented shared quota


@pytest.mark.parametrize("change", [{"clock_source": "guessed"}, {"reason": "operator-hold"},
                                    {"until_at": NOW}, {"released_at": NOW}])
def test_guessed_operator_expired_or_released_closures_do_not_release_reserve(change):
    state = view([lane()], closures=[closure("claude-1", SCOPED_MODEL_IDS["fable"], **change)])
    result = evaluate(load_policy(DEFAULT_POLICY_PATH), state, job(pinned_model="opus"))
    assert result.chosen_lane is None


def test_shared_exhaustion_still_blocks_opus_with_fable_closed():
    state = view([lane()], [reading("claude-1", 1)],
                 [closure("claude-1", SCOPED_MODEL_IDS["fable"])])
    result = evaluate(load_policy(DEFAULT_POLICY_PATH), state, job(pinned_model="opus"))
    assert result.chosen_lane is None
    assert "below-floor" in result.evaluations[0]["rejected"][0]["reasons"]


def test_scoped_model_ids_name_the_shipped_policy_models():
    """C-11.7: the no-policy fallback matches the shipped ids. The resolver test
    checks the map the daemon uses with a loaded policy."""
    models = load_policy(DEFAULT_POLICY_PATH)["models"]
    assert SCOPED_MODEL_IDS == {name: models[name]["id"] for name in SCOPED_MODEL_IDS}
    assert SCOPED_MODEL_IDS["opus"] == "claude-opus-5-5"
