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


def stream(status="allowed", window="seven_day_overage_included"):
    # Shape of the real Fable refusal: shared weekly 55%, Fable weekly 100%.
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": window, "resetsAt": 1790362800,
        "unifiedWindows": {"five_hour": {"utilization": 0, "resetsAt": 1790000000},
                           "seven_day": {"utilization": .55, "resetsAt": 1790362800},
                           "seven_day_overage_included": {"utilization": 1.01,
                                                         "resetsAt": 1790362800}}}})


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
    """C-11.7: a weekly window scoped to an id no policy model carries is ignored, so the
    adapter's names must resolve to the shipped policy's ids (Opus moved to 5.5 on 2026-09-22)."""
    models = load_policy(DEFAULT_POLICY_PATH)["models"]
    assert SCOPED_MODEL_IDS == {name: models[name]["id"] for name in SCOPED_MODEL_IDS}
    assert SCOPED_MODEL_IDS["opus"] == "claude-opus-5-5"
