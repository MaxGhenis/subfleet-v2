"""C-9.8: family windows follow the attempt's concrete model within that family."""

import json
from dataclasses import asdict
from datetime import datetime

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.adapters.claude import (
    ClaudeAdapter, SCOPED_MODEL_IDS, SCOPED_RATE_LIMITS, scoped_window_scope,
)
from subfleet.adapters.claude_stream import parse_stream
from subfleet.contracts import OutcomeClass, ReadingLabel
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate
from tests.conftest import exit_info, make_launch, profile_opener
from tests.unit.test_scheduler import NOW, TOMORROW, job, lane, view


_FAMILIES = ("fable", "opus", "sonnet", "haiku", "unknown")
_MODEL_IDS = st.builds(
    lambda family, version, suffix: f"claude-{family}-{version}{suffix}",
    st.sampled_from(_FAMILIES),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-", min_size=1, max_size=30),
    st.sampled_from(("", "[1m]")),
)
_MODEL_VALUES = st.one_of(
    _MODEL_IDS, st.none(), st.text(max_size=80),
    st.sampled_from(("opus[1m]", "default", "claude-fable-5-2[1m][1m]",
                     "claude-fable-5[other]-2", "claude--5-2", "claude-fable-")),
)
_UNKNOWN_WINDOWS = st.one_of(
    st.none(), st.text(max_size=80).filter(lambda window: window not in SCOPED_RATE_LIMITS),
)
_LAWS = settings(max_examples=200, deadline=None, derandomize=True)


def _family(model_id):
    """Independent shape oracle, deliberately using splits rather than a regex."""
    if not isinstance(model_id, str):
        return None
    pieces = model_id.split("-", 2)
    if (len(pieces) != 3 or pieces[0] != "claude" or not all(pieces[1:])
            or "[" in model_id or "]" in model_id):
        return None
    return pieces[1]


@pytest.mark.parametrize("window", sorted(SCOPED_RATE_LIMITS))
@_LAWS
@given(model_id=_MODEL_VALUES)
def test_scoped_window_uses_attempt_only_within_its_family(window, model_id):
    """Invariant 1: matching concrete IDs win; every other value uses the map."""
    base = model_id[:-4] if model_id and model_id.endswith("[1m]") else model_id
    fallback = SCOPED_RATE_LIMITS[window]
    expected = base if _family(base) == _family(fallback) else fallback
    assert scoped_window_scope(window, model_id) == expected


@_LAWS
@given(window=_UNKNOWN_WINDOWS, model_id=_MODEL_VALUES)
def test_unscoped_and_missing_windows_are_account_scoped(window, model_id):
    """Invariant 2: absent and unknown windows always belong to the account."""
    assert scoped_window_scope(window, model_id) == "account"


@_LAWS
@given(window=st.one_of(st.sampled_from(tuple(SCOPED_RATE_LIMITS)), _UNKNOWN_WINDOWS),
       model_id=_MODEL_VALUES)
def test_scoped_window_scope_never_contains_a_bracket(window, model_id):
    """Invariant 3: a context suffix or malformed value never becomes a scope."""
    assert "[" not in scoped_window_scope(window, model_id)


@pytest.mark.parametrize("window,model_id,expected", [
    ("seven_day_overage_included", "claude-fable-5-2", "claude-fable-5-2"),
    ("seven_day_overage_included", "claude-fable-5-1", "claude-fable-5-1"),
    ("seven_day_overage_included", "claude-fable-5-2[1m]", "claude-fable-5-2"),
    ("seven_day_overage_included", "claude-fable-5-1[1m]", "claude-fable-5-1"),
    ("seven_day_overage_included", "claude-opus-5-5", SCOPED_MODEL_IDS["fable"]),
    ("seven_day_overage_included", "opus[1m]", SCOPED_MODEL_IDS["fable"]),
    ("seven_day_overage_included", "default", SCOPED_MODEL_IDS["fable"]),
    ("seven_day_overage_included", None, SCOPED_MODEL_IDS["fable"]),
    ("seven_day_overage_included", "claude-fable-5-2[1m][1m]", SCOPED_MODEL_IDS["fable"]),
    ("seven_day_overage_included", "claude-fable-5[other]-2", SCOPED_MODEL_IDS["fable"]),
    ("seven_day_sonnet", "claude-sonnet-5-5", "claude-sonnet-5-5"),
    ("seven_day_opus", "claude-opus-5-5", "claude-opus-5-5"),
    ("unknown", "claude-fable-5-2", "account"),
    ("seven_day", "claude-fable-5-2[1m]", "account"),
    ("five_hour", "claude-sonnet-5-5", "account"),
    (None, "claude-fable-5-2[1m]", "account"),
    (None, None, "account"),
])
def test_scoped_window_scope_examples(window, model_id, expected):
    assert scoped_window_scope(window, model_id) == expected


def _stream(status, window):
    reset = int(datetime.fromisoformat(TOMORROW.replace("Z", "+00:00")).timestamp())
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": window, "resetsAt": reset,
        "unifiedWindows": {window: {"utilization": 1, "resetsAt": reset}},
    }})


def _rejected_outcome(tmp_path, model_id, window):
    adapter = ClaudeAdapter(
        now=lambda: datetime.fromisoformat(NOW.replace("Z", "+00:00")),
        projects_dir=tmp_path / "projects", profile_opener=profile_opener(),
    )
    (tmp_path / "stream.jsonl").write_text(_stream("rejected", window))
    (tmp_path / "stderr").write_text("")
    launch = make_launch(tmp_path, session_id="scope-test", model_id=model_id)
    return adapter.classify(tmp_path, launch, exit_info(1))


@pytest.mark.parametrize("model_id,window", [
    ("claude-fable-5-2", "seven_day_overage_included"),
    ("claude-fable-5-1", "seven_day_overage_included"),
    ("claude-sonnet-5-5", "seven_day_sonnet"),
])
def test_rejected_scoped_window_closes_attempt_model(tmp_path, model_id, window):
    outcome = _rejected_outcome(tmp_path, model_id, window)
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.closure is not None
    assert outcome.closure.scope == model_id
    assert outcome.closure.until_at == TOMORROW


@pytest.mark.parametrize("model_id,window", [
    ("claude-fable-5-2", "seven_day_overage_included"),
    ("claude-fable-5-1", "seven_day_overage_included"),
    ("claude-sonnet-5-5", "seven_day_sonnet"),
])
def test_allowed_scoped_window_reading_tracks_attempt_model(tmp_path, model_id, window):
    rows = ClaudeAdapter(projects_dir=tmp_path).readings_from_rate_limit(
        parse_stream(_stream("allowed", window)).rate_limit,
        lane_id="claude-1", model_id=model_id, observed_at=NOW,
    )
    assert len(rows) == 1
    assert rows[0].scope == model_id
    assert rows[0].window == "seven_day"
    assert rows[0].label == ReadingLabel.PROVIDER


def test_fable52_rejection_blocks_fable52_but_keeps_opus_eligible(tmp_path):
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert policy["models"]["fable"]["id"] == "claude-fable-5-2"
    outcome = _rejected_outcome(tmp_path, "claude-fable-5-2", "seven_day_overage_included")
    assert outcome.closure is not None
    state = view([lane()], closures=[asdict(outcome.closure)])
    fable = evaluate(policy, state, job(pinned_model="fable"))
    assert fable.chosen_lane is None
    assert f"closed:claude-fable-5-2:{TOMORROW}" in (
        fable.evaluations[0]["rejected"][0]["reasons"]
    )
    assert evaluate(policy, state, job(pinned_model="opus")).chosen_lane == "claude-1"
