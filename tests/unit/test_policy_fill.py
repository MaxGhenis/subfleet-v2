"""C-11.11: fill policy defaults and the exact keys invalid values name."""

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.policy import (DEFAULT_POLICY_PATH, PolicyError, admission_settings,
                             lane_order, lane_spread, load_policy)


def loaded(tmp_path, admission):
    data = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    data["admission"] = admission
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_policy(path)


def test_fill_defaults_apply_to_bundled_loaded_and_raw_policy(tmp_path):
    for rules in ({}, load_policy(DEFAULT_POLICY_PATH), loaded(tmp_path, {})):
        assert admission_settings(rules)["lane_order"] == {"claude": "fill", "codex": "reset"}
        assert admission_settings(rules)["expiring_s"] == 21600
        assert lane_order(rules, "claude") == "fill"
        assert lane_order(rules, "codex") == "reset"
        assert lane_spread(rules, "claude") == lane_spread(rules, "codex") == 2


@pytest.mark.parametrize("order", [{}, {"codex": "fill"}, {"claude": "reset"},
                                  {"claude": "fill", "codex": "reset"}])
def test_an_explicit_lane_order_map_uses_reset_for_missing_providers(tmp_path, order):
    rules = loaded(tmp_path, {"lane_order": order})
    assert admission_settings(rules)["lane_order"] == order
    for provider in ("claude", "codex"):
        assert lane_order(rules, provider) == order.get(provider, "reset")
        assert lane_order({"admission": {"lane_order": order}}, provider) == order.get(provider, "reset")


@pytest.mark.parametrize("spread", [None, 1, 2, 4, {}, {"claude": 4}, {"claude": None, "codex": 2}])
def test_lane_spread_accepts_scalar_and_per_provider_forms(tmp_path, spread):
    rules = loaded(tmp_path, {"lane_spread": spread})
    assert admission_settings(rules)["lane_spread"] == spread
    for provider in ("claude", "codex"):
        expected = spread.get(provider, 2) if isinstance(spread, dict) else spread
        assert lane_spread(rules, provider) == expected
        assert lane_spread({"admission": {"lane_spread": spread}}, provider) == expected


@pytest.mark.parametrize("expiring", [None, 1, 21600, 86400, 10**1000])
def test_expiring_seconds_accept_only_positive_whole_seconds_or_null(tmp_path, expiring):
    assert loaded(tmp_path, {"expiring_s": expiring})["admission"]["expiring_s"] == expiring


@pytest.mark.parametrize("admission,key", [
    ({"lane_order": None}, "admission.lane_order"),
    ({"lane_order": []}, "admission.lane_order"),
    ({"lane_order": "fill"}, "admission.lane_order"),
    ({"lane_order": {"other": "fill"}}, "admission.lane_order.other"),
    ({"lane_order": {"claude": "other"}}, "admission.lane_order.claude"),
    ({"lane_order": {"claude": None}}, "admission.lane_order.claude"),
    ({"lane_order": {"codex": []}}, "admission.lane_order.codex"),
    ({"lane_spread": []}, "admission.lane_spread"),
    ({"lane_spread": "2"}, "admission.lane_spread"),
    ({"lane_spread": {"other": 2}}, "admission.lane_spread.other"),
    ({"lane_spread": {"claude": 0}}, "admission.lane_spread.claude"),
    ({"lane_spread": {"claude": 2.0}}, "admission.lane_spread.claude"),
    ({"lane_spread": {"codex": True}}, "admission.lane_spread.codex"),
    ({"lane_spread": {"codex": {}}}, "admission.lane_spread.codex"),
    *[({"expiring_s": value}, "admission.expiring_s")
      for value in (0, -1, True, False, 1.0, 1.5, "21600", [], {}, float("nan"), float("inf"))],
])
def test_invalid_fill_policy_values_name_the_exact_key(tmp_path, admission, key):
    with pytest.raises(PolicyError) as caught:
        loaded(tmp_path, admission)
    assert caught.value.key == key


@settings(max_examples=150, deadline=None, derandomize=True, database=None)
@given(st.one_of(st.none(), st.integers(), st.booleans(), st.floats(), st.text(max_size=5)))
def test_generated_expiring_seconds_keep_exactly_positive_integers_or_null(value):
    valid = value is None or type(value) is int and value > 0
    with tempfile.TemporaryDirectory(prefix="fill-policy-") as directory:
        if valid:
            assert loaded(Path(directory), {"expiring_s": value})["admission"]["expiring_s"] == value
        else:
            with pytest.raises(PolicyError) as caught:
                loaded(Path(directory), {"expiring_s": value})
            assert caught.value.key == "admission.expiring_s"
