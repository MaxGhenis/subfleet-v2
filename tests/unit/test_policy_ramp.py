import copy
import json

import pytest

from subfleet.policy import (
    DEFAULT_POLICY_PATH, RAMP_DEFAULTS, PolicyError, admission_settings, load_policy,
    policy_hash, ramp_settings,
)


def test_shipped_policy_preserves_pre_ramp_raw_hash_and_disabled_defaults():
    # A daemon without policy.json copies these bytes, whose hash enters every
    # admission decision even when the ramp is absent or disabled (C-6.18 R6).
    before = "47e1c5111b9cef28751257667cd8aa2eb9ba0ac39ef7e55beff30879a429e49c"
    assert "ramp" not in json.loads(DEFAULT_POLICY_PATH.read_bytes())["admission"]
    assert policy_hash(DEFAULT_POLICY_PATH) == before
    loaded = load_policy(DEFAULT_POLICY_PATH)
    assert loaded["_policy_hash"] == before
    assert loaded["admission"]["ramp"] == RAMP_DEFAULTS


@pytest.mark.parametrize("section", [None, {}, {"enabled": True}, {"per_minute": 3},
                                    {"window_s": 120},
                                    {"enabled": True, "per_minute": 1, "window_s": 1}])
def test_ramp_defaults_and_partial_config(tmp_path, section):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    if section is None:
        raw.pop("admission")
    else:
        raw["admission"]["ramp"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    expected = {**RAMP_DEFAULTS, **(section or {})}
    loaded = load_policy(path)
    assert loaded["admission"]["ramp"] == expected
    assert ramp_settings(loaded) == expected
    assert admission_settings(loaded)["ramp"] == expected
    assert ramp_settings({}) == RAMP_DEFAULTS
    assert admission_settings({})["ramp"] == RAMP_DEFAULTS
    assert RAMP_DEFAULTS == {"enabled": False, "per_minute": 12, "window_s": 60}


@pytest.mark.parametrize("key,value", [
    ("enabled", 1), ("enabled", 0), ("enabled", None), ("enabled", "true"),
    ("per_minute", 0), ("per_minute", -1), ("per_minute", True),
    ("per_minute", 12.0), ("per_minute", 1.5), ("per_minute", "12"),
    ("per_minute", None), ("per_minute", float("nan")), ("per_minute", float("inf")),
    ("window_s", 0), ("window_s", -1), ("window_s", False),
    ("window_s", 60.0), ("window_s", 0.5), ("window_s", "60"),
    ("window_s", None), ("window_s", float("nan")), ("window_s", float("-inf")),
    ("starts_per_minute", 12), ("window", 60),
])
def test_invalid_ramp_setting_names_exact_key(tmp_path, key, value):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"].setdefault("ramp", dict(RAMP_DEFAULTS))[key] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == f"admission.ramp.{key}"


@pytest.mark.parametrize("value", [None, [], True, "enabled", 12])
def test_ramp_section_must_be_an_object(tmp_path, value):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"]["ramp"] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == "admission.ramp"


def test_ramp_default_objects_are_not_shared_between_loads(tmp_path):
    raw = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    raw["admission"].pop("ramp", None)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    first = load_policy(path)
    first["admission"]["ramp"]["enabled"] = True
    assert load_policy(path)["admission"]["ramp"] == copy.deepcopy(RAMP_DEFAULTS)


def test_raw_ramp_settings_merge_without_mutating_defaults():
    raw = {"admission": {"ramp": {"per_minute": 7}}}
    settings = ramp_settings(raw)
    assert settings == {"enabled": False, "per_minute": 7, "window_s": 60}
    settings["window_s"] = 1
    assert raw == {"admission": {"ramp": {"per_minute": 7}}}
    assert RAMP_DEFAULTS["window_s"] == 60
