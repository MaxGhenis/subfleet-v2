"""Routing policy data validation and compatibility names (C-11.1)."""

import hashlib
import json

import pytest

from subfleet.contracts import DEFAULT_CAPS, HEADROOM_FLOOR, READING_TTL_S, Exit
from subfleet.policy import (
    DEFAULT_POLICY_PATH, PolicyError, load_policy, policy_hash, resolve_model,
)


@pytest.fixture
def policy_data():
    return json.loads(DEFAULT_POLICY_PATH.read_bytes())


def write_policy(tmp_path, value):
    path = tmp_path / "custom-policy.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize("key", [
    "tiers", "chains", "fallback", "permissions", "models", "retired",
    "desktop_login", "caps", "reset_credits",
])
def test_missing_required_key_names_file_and_key(tmp_path, policy_data, key):
    """C-11.1: each required routing key fails with its policy file and key."""
    del policy_data[key]
    path = write_policy(tmp_path, policy_data)
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert str(path) in str(caught.value)
    assert caught.value.key == key
    assert caught.value.code == Exit.INVALID_INPUT


@pytest.mark.parametrize("key,value,error_key", [
    ("tiers", "standard", "tiers"),
    ("tiers", [], "tiers"),
    ("tiers", ["standard", "standard"], "tiers[1]"),
    ("tiers", ["standard", []], "tiers[1]"),
    ("tiers", [" "], "tiers[0]"),
    ("models", [], "models"),
    ("models", {}, "models"),
    ("models.opus", "claude-opus-5", "models.opus"),
    ("models.opus.provider", "other", "models.opus.provider"),
    ("models.opus.provider", [], "models.opus.provider"),
    ("models.opus.id", "", "models.opus.id"),
    ("models.opus.effort", None, "models.opus.effort"),
    ("models.opus.scope", [], "models.opus.scope"),
    ("models.opus.priority", -1, "models.opus.priority"),
    ("models.opus.priority", True, "models.opus.priority"),
    ("models.opus.priority", "3", "models.opus.priority"),
    ("chains", [], "chains"),
    ("chains", {}, "chains"),
    ("chains.research", ["opus"], "chains.research"),
    ("chains.research", ["haiku", "sonnet", "typo", "astra"], "chains.research[2]"),
    ("chains.research", ["haiku", "sonnet", [], "astra"], "chains.research[2]"),
    ("chains.research", ["haiku", "sonnet", "sol", "astra"], "chains.research[2]"),
    ("fallback", "downward", "fallback"),
    ("desktop_login", "sometimes", "desktop_login"),
    ("permissions", [], "permissions"),
    ("permissions", {}, "permissions.lookup"),
    ("permissions.build", "danger-full-access", "permissions.build"),
    ("permissions.typo", "read-only", "permissions.typo"),
    ("retired", [], "retired"),
    ("retired.sol", "typo", "retired.sol"),
    ("retired.sol", [], "retired.sol"),
    ("retired.opus", "astra", "retired.opus"),
    ("caps", [], "caps"),
    ("caps.max_active_attempts", 0, "caps.max_active_attempts"),
    ("caps.max_active_attempts_per_parent", -1, "caps.max_active_attempts_per_parent"),
    ("caps.reading_ttl_s", True, "caps.reading_ttl_s"),
    ("caps.max_in_flight_unmeasured", 1.5, "caps.max_in_flight_unmeasured"),
    ("caps.max_tokens_observed", False, "caps.max_tokens_observed"),
    ("headroom_floor", -0.1, "headroom_floor"),
    ("headroom_floor", 1.1, "headroom_floor"),
    ("headroom_floor", True, "headroom_floor"),
    ("headroom_floor", float("nan"), "headroom_floor"),
    ("reset_credits", [], "reset_credits"),
    ("reset_credits", {}, "reset_credits.enabled"),
    ("reset_credits.enabled", "true", "reset_credits.enabled"),
    ("reset_credits.headroom_floor_pct", 101, "reset_credits.headroom_floor_pct"),
    ("reset_credits.headroom_floor_pct", float("inf"), "reset_credits.headroom_floor_pct"),
    ("reset_credits.min_interval_min", 0, "reset_credits.min_interval_min"),
    ("reset_credits.min_interval_min", False, "reset_credits.min_interval_min"),
])
def test_invalid_nested_shape_names_exact_key(tmp_path, policy_data, key, value, error_key):
    """C-11.1, C-6.4, C-11.3: malformed routing fields fail before admission."""
    parts = key.split(".")
    parent = policy_data
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = value
    path = write_policy(tmp_path, policy_data)
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == error_key
    assert str(path) in str(caught.value)
    assert error_key in str(caught.value)
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize("raw", [b"[]", b"null", b'{"tiers":', b"\xff"])
def test_invalid_json_document_names_file_and_root(tmp_path, raw):
    """C-11.1: malformed JSON or a non-object root is an exit-2 policy error."""
    path = tmp_path / "bad-policy.json"
    path.write_bytes(raw)
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == "$"
    assert str(path) in str(caught.value)
    assert caught.value.code == Exit.INVALID_INPUT


def test_unreadable_policy_names_file(tmp_path):
    """C-11.1: an unreadable policy reports its file as invalid input."""
    path = tmp_path / "absent-policy.json"
    with pytest.raises(PolicyError, match="absent-policy.json"):
        load_policy(path)


def test_defaults_preserve_optional_caps_and_floor(tmp_path, policy_data):
    """C-6.4, C-11.1, C-11.3: omitted cap fields and floor use contract defaults."""
    policy_data["caps"] = {"max_active_attempts_per_parent": 2}
    policy = load_policy(write_policy(tmp_path, policy_data))
    assert all(policy["caps"][key] == value for key, value in DEFAULT_CAPS.items())
    assert policy["caps"]["reading_ttl_s"] == READING_TTL_S
    assert policy["caps"]["max_tokens_observed"] is None
    assert policy["caps"]["max_active_attempts_per_parent"] == 2
    assert policy["headroom_floor"] == HEADROOM_FLOOR


def test_hash_is_stable_and_uses_exact_file_bytes(tmp_path, policy_data):
    """C-11.1: repeated loads keep exact-byte SHA-256, including whitespace."""
    path = write_policy(tmp_path, policy_data)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    first, second = load_policy(path), load_policy(path)
    assert first == second
    assert policy_hash(path) == first["_policy_hash"] == digest
    assert first["_policy_path"] == str(path)
    assert path.read_bytes() == raw
    path.write_bytes(raw + b"\n")
    assert policy_hash(path) != digest
    assert load_policy(path)["_policy_hash"] == policy_hash(path)


@pytest.mark.parametrize("configured", [None, 30, 300])
def test_probe_cadence_defaults_below_ttl_without_overriding_config(tmp_path, policy_data, configured):
    """C-18.1: the 60 s default fits TTL120; explicit polling intervals remain policy."""
    assert policy_data["timers"]["probe_interval_s"] == 60
    assert policy_data["caps"]["reading_ttl_s"] == 120
    if configured is None:
        policy_data.pop("timers")
    else:
        policy_data["timers"]["probe_interval_s"] = configured
    policy = load_policy(write_policy(tmp_path, policy_data))
    assert policy["timers"]["probe_interval_s"] == (60 if configured is None else configured)
    assert policy["caps"]["reading_ttl_s"] == 120


def test_serialized_metadata_cannot_override_file_hash(tmp_path, policy_data):
    """C-11.1: policy provenance always describes the bytes actually loaded."""
    policy_data.update(_policy_hash="spoofed", _policy_path="wrong.json")
    path = write_policy(tmp_path, policy_data)
    loaded = load_policy(path)
    assert loaded["_policy_hash"] == policy_hash(path)
    assert loaded["_policy_path"] == str(path)


def test_retired_sol_alias_resolves_to_astra_with_note(capsys):
    """C-11.1: the retired sol alias resolves to Astra with a stderr note."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert resolve_model(policy, "sol") == "astra"
    captured = capsys.readouterr()
    assert not captured.out
    assert "retired" in captured.err and "sol" in captured.err and "astra" in captured.err


@pytest.mark.parametrize("name", ["astra", "gpt-6-astra"])
def test_current_name_and_exact_id_resolve_without_note(name, capsys):
    """C-11.1, C-11.2: current names and exact ids preserve model pins silently."""
    assert resolve_model(load_policy(DEFAULT_POLICY_PATH), name) == "astra"
    assert not capsys.readouterr().err


def test_retired_note_can_be_suppressed_for_repeat_evaluations(capsys):
    """C-11.1: reevaluation may suppress a retired alias note already emitted."""
    assert resolve_model(load_policy(DEFAULT_POLICY_PATH), "sol", note=False) == "astra"
    assert not capsys.readouterr().err


@pytest.mark.parametrize("name", ["typo", "", None, []])
def test_unknown_model_is_exit_two_naming_input_key(name):
    """C-11.1, C-17.3: an unknown model is exit 2 and names the model input key."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    with pytest.raises(PolicyError) as caught:
        resolve_model(policy, name, key="job.pinned_model")
    assert caught.value.code == Exit.INVALID_INPUT
    assert caught.value.key == "job.pinned_model"
    assert str(DEFAULT_POLICY_PATH) in str(caught.value)
    assert "unknown model" in str(caught.value)
