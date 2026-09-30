"""C-10.8, C-11.1: the auth-dead re-check's policy keys, as the loader reads them."""
from __future__ import annotations

import json

import pytest

from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from subfleet.recheck import DEFAULTS, Settings


def write(tmp_path, timers):
    policy = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    policy["timers"] = timers
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_c10_8_a_policy_written_before_the_recheck_loads_with_its_defaults(tmp_path):
    loaded = load_policy(write(tmp_path, {"probe_interval_s": 60, "keepalive_interval_s": 18300}))
    assert {key: loaded["timers"][key] for key in DEFAULTS} == DEFAULTS
    assert Settings.from_policy(loaded) == Settings()


def test_c10_8_the_shipped_policy_carries_the_defaults():
    assert {key: load_policy(DEFAULT_POLICY_PATH)["timers"][key] for key in DEFAULTS} == DEFAULTS


def test_c10_8_zero_switches_the_recheck_off(tmp_path):
    loaded = load_policy(write(tmp_path, {"auth_recheck_interval_s": 0}))
    assert not Settings.from_policy(loaded).enabled


@pytest.mark.parametrize("timers,key,words", [
    ({"auth_recheck_interval_s": 3600}, "auth_recheck_interval_s", "at least 21600"),
    ({"auth_recheck_interval_s": 21599}, "auth_recheck_interval_s", "at least 21600"),
    ({"auth_recheck_interval_s": -1}, "auth_recheck_interval_s", "nonnegative"),
    ({"auth_recheck_interval_s": "6h"}, "auth_recheck_interval_s", "nonnegative"),
    ({"auth_recheck_interval_s": True}, "auth_recheck_interval_s", "nonnegative"),
    ({"auth_recheck_interval_s": 43200, "auth_recheck_max_interval_s": 21600},
     "auth_recheck_max_interval_s", "at least timers.auth_recheck_interval_s"),
    ({"auth_recheck_tick_s": 0}, "auth_recheck_tick_s", "positive"),
    ({"auth_recheck_max_load_per_cpu": 0}, "auth_recheck_max_load_per_cpu", "positive"),
    ({"auth_recheck_spacing_s": float("nan")}, "auth_recheck_spacing_s", "nonnegative"),
])
def test_c10_8_the_loader_names_a_bad_key(tmp_path, timers, key, words):
    with pytest.raises(PolicyError) as caught:
        load_policy(write(tmp_path, timers))
    assert f"timers.{key}" in str(caught.value) and words in str(caught.value)
