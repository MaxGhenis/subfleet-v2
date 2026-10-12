"""C-6.4a / C-11.1: provider caps are optional, validated, and byte preserving."""

import hashlib
import json

import pytest
from hypothesis import HealthCheck, given, settings

from subfleet import scheduler
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy, provider_cap
from tests.reference_scheduler import reference_evaluate
from tests.routing_strategies import NOW, view_of
from tests.unit.test_scheduler_split import cases, outcome


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, 0, -1, "2", [], {}])
def test_invalid_provider_cap_names_exact_key(tmp_path, provider, value):
    data = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    data["providers"] = {provider: {"max_active_attempts": value}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == f"providers.{provider}.max_active_attempts"
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("value", [None, 1, 40])
def test_valid_provider_cap(tmp_path, value):
    data = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    data["providers"] = {"codex": {"max_active_attempts": value}}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    loaded = load_policy(path)
    assert provider_cap(loaded, "codex") == value
    assert provider_cap(loaded, "claude") is None


@pytest.mark.parametrize("section,key", [([], "providers"), (None, "providers"),
                                        ({"codex": []}, "providers.codex"),
                                        ({"codex": None}, "providers.codex"),
                                        ({"other": {}}, "providers.other")])
def test_invalid_provider_section(tmp_path, section, key):
    data = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    data["providers"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == key


@pytest.mark.parametrize("section", [None, {}, {"codex": {}}, {"codex": {"max_active_attempts": None}}])
def test_missing_cap_preserves_bytes_and_hash(tmp_path, section):
    raw = DEFAULT_POLICY_PATH.read_bytes()
    data = json.loads(raw)
    assert "providers" not in data
    if section is not None:
        data["providers"] = section
        raw = (json.dumps(data, indent=3) + "\n\n").encode()
    path = tmp_path / "policy.json"
    path.write_bytes(raw)
    loaded = load_policy(path)
    assert provider_cap(loaded, "codex") is None
    assert provider_cap(loaded, "claude") is None
    assert path.read_bytes() == raw
    assert loaded["_policy_hash"] == hashlib.sha256(raw).hexdigest()
    if section is None:
        assert "providers" not in loaded
        # Original shipped bytes at release/217, including the trailing newline.
        assert hashlib.sha256(raw).hexdigest() == "47e1c5111b9cef28751257667cd8aa2eb9ba0ac39ef7e55beff30879a429e49c"


@settings(max_examples=300, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much])
@given(cases())
def test_absent_provider_key_decides_exactly_as_before(case):
    """Differential against the independent pre-cap routing oracle, all decision fields."""
    policy, store, job = case
    assert 'providers' not in policy
    view = view_of(store, NOW)
    assert outcome(scheduler.evaluate, policy, view, job) == outcome(reference_evaluate, policy, view, job)
    caps = policy.get('caps', {})
    conversations = policy.get('conversations', {})
    if job.get('kind') == 'turn':
        pooled = any(conversations.get(key) is not None for key in ('max_active_turns', 'turn_slots_per_lane'))
    else:
        pooled = any(caps.get(key) is not None for key in
                     ('max_active_attempts', 'max_in_flight_per_lane', 'max_in_flight_unmeasured'))
    family = bool(job.get('parent_job_id')) and caps.get('max_active_attempts_per_parent') is not None
    assert scheduler.pool_capped(policy, job) == bool(pooled or family)
    assert scheduler.hold_scope(policy, job) == ('pool' if pooled else 'family' if family else None)
