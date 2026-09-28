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
    ("caps.workspace_git_timeout_s", 0, "caps.workspace_git_timeout_s"),
    ("caps.worktree_add_timeout_s", 2.5, "caps.worktree_add_timeout_s"),
    ("caps.workspace_retry_max", -1, "caps.workspace_retry_max"),
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
    ("network", [], "network"),
    ("network.codex_workspace_write", "yes", "network.codex_workspace_write"),
    ("network.claude", True, "network.claude"),
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


def test_c6_8_workspace_caps_default_and_follow_the_policy_file(tmp_path, policy_data):
    """C-6.8 the git caps are policy data: 60 s, 180 s and 8 retries unless the file says otherwise."""
    policy_data["caps"].pop("workspace_git_timeout_s", None)
    caps = load_policy(write_policy(tmp_path, policy_data))["caps"]
    assert (caps["workspace_git_timeout_s"], caps["worktree_add_timeout_s"], caps["workspace_retry_max"]) == (60, 180, 8)
    policy_data["caps"].update(workspace_git_timeout_s=300, workspace_retry_max=20)
    caps = load_policy(write_policy(tmp_path, policy_data))["caps"]
    assert (caps["workspace_git_timeout_s"], caps["worktree_add_timeout_s"], caps["workspace_retry_max"]) == (300, 180, 20)


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


def test_conversation_and_retention_sections_default_and_follow_the_policy_file(tmp_path, policy_data):
    """C-30.1, C-25.4, C-26.9, C-8.4, C-26.12: the catalog interval, compaction
    thresholds, approval wait and both retention budgets are policy data."""
    from subfleet.policy import CONVERSATION_DEFAULTS, RETENTION_DEFAULTS
    policy_data.pop("conversations", None)
    policy_data.pop("retention", None)
    policy = load_policy(write_policy(tmp_path, policy_data))
    assert policy["conversations"] == CONVERSATION_DEFAULTS
    assert policy["conversations"]["catalog_interval_s"] == 60
    assert policy["retention"] == RETENTION_DEFAULTS
    assert (policy["retention"]["jobs"], policy["retention"]["bytes"]) == (500, 2 * 1024 ** 3)
    assert (policy["retention"]["turn_jobs"], policy["retention"]["turn_bytes"],
            policy["retention"]["turn_keep_days"]) == (2000, 4 * 1024 ** 3, 14)
    policy_data["conversations"] = {"catalog_interval_s": 0, "compact_after_s": 0}
    policy_data["retention"] = {"turn_jobs": 50, "turn_keep_days": 0}
    policy = load_policy(write_policy(tmp_path, policy_data))
    assert policy["conversations"]["catalog_interval_s"] == 0 and policy["conversations"]["compact_after_s"] == 0
    assert policy["conversations"]["approval_wait_s"] is None
    assert policy["retention"]["turn_jobs"] == 50 and policy["retention"]["turn_keep_days"] == 0
    assert policy["retention"]["jobs"] == 500


@pytest.mark.parametrize("section, key, value", [
    ("conversations", "catalog_interval_s", -5),
    ("conversations", "catalog_interval_s", True),
    ("conversations", "compact_after_s", -1),
    ("conversations", "compact_per_tick", 2.5),
    ("retention", "turn_jobs", 0),
    ("retention", "bytes", "2GiB"),
    ("retention", "turn_keep_days", -1),
])
def test_conversation_and_retention_values_are_validated(tmp_path, policy_data, section, key, value):
    """C-11.1: a bad value names its section and key; zero only where it means none."""
    policy_data[section] = {key: value}
    with pytest.raises(PolicyError, match=f"{section}.{key}"):
        load_policy(write_policy(tmp_path, policy_data))


def test_a_section_that_is_not_an_object_is_refused(tmp_path, policy_data):
    """C-11.1: `conversations` and `retention` are objects."""
    for section in ("conversations", "retention"):
        data = {**policy_data, section: [1]}
        with pytest.raises(PolicyError, match=section):
            load_policy(write_policy(tmp_path, data))


def test_d260_the_shipped_policy_lets_writable_codex_jobs_reach_the_network():
    assert load_policy(DEFAULT_POLICY_PATH)["network"] == {"codex_workspace_write": True}


def test_conversation_clocks_default_and_follow_the_policy_file(tmp_path, policy_data):
    """C-24.7, C-26.5, C-26.9: a policy without `conversations` gets the runner's
    defaults (10, 20, 30 and 135 s, approvals unlimited); a supplied section is kept
    and reaches the turn runner's clocks."""
    from subfleet.conversations.runner import Clocks

    del policy_data["conversations"]
    loaded = load_policy(write_policy(tmp_path, policy_data))
    clocks = ("approval_wait_s", "stop_sigint_after_s", "stop_close_after_s", "stop_contain_after_s", "after_result_s")
    assert {k: loaded["conversations"][k] for k in clocks} == {
        "approval_wait_s": None, "stop_sigint_after_s": 10, "stop_close_after_s": 20,
        "stop_contain_after_s": 30, "after_result_s": 135}
    assert Clocks.from_policy(loaded) == Clocks()
    policy_data["conversations"] = {"stop_sigint_after_s": 0.5, "stop_close_after_s": 1.5,
                                    "stop_contain_after_s": 2.5, "after_result_s": 4}
    loaded = load_policy(write_policy(tmp_path, policy_data))
    assert Clocks.from_policy(loaded) == Clocks(sigint_after_s=0.5, close_after_s=1.5, contain_after_s=2.5,
                                                after_result_s=4.0, approval_wait_s=None)


@pytest.mark.parametrize("section,expected", [({}, None), ({"approval_wait_s": None}, None),
                                           ({"approval_wait_s": 2.5}, 2.5)])
def test_approval_wait_accepts_no_limit_or_a_positive_number(tmp_path, policy_data, section, expected):
    """C-26.9: null and an omitted approval wait are unlimited; a finite policy
    limit reaches the runner unchanged, including fractional seconds."""
    from subfleet.conversations.runner import Clocks

    policy_data["conversations"] = section
    loaded = load_policy(write_policy(tmp_path, policy_data))
    assert loaded["conversations"]["approval_wait_s"] == expected
    assert Clocks.from_policy(loaded).approval_wait_s == expected
    assert Clocks.from_policy({"conversations": section}).approval_wait_s == expected
    assert Clocks.from_policy({}).approval_wait_s is None
    assert load_policy(DEFAULT_POLICY_PATH)["conversations"]["approval_wait_s"] is None


def test_turn_caps_default_to_no_cap_and_accept_null_or_a_whole_number(tmp_path, policy_data):
    """C-26.9: a policy that does not set the turn caps has none (null); null and a
    positive whole number are both kept; `turn_cap` reads a missing key as no cap."""
    from subfleet.policy import TURN_CAPS, turn_cap

    loaded = load_policy(write_policy(tmp_path, policy_data))
    assert {key: loaded["conversations"][key] for key in TURN_CAPS} == {
        "max_active_turns": None, "turn_slots_per_lane": None}
    del policy_data["conversations"]
    loaded = load_policy(write_policy(tmp_path, policy_data))
    assert loaded["conversations"]["max_active_turns"] is None
    assert turn_cap(loaded["conversations"], "turn_slots_per_lane") is None
    policy_data["conversations"] = {"max_active_turns": 4, "turn_slots_per_lane": None}
    loaded = load_policy(write_policy(tmp_path, policy_data))
    assert turn_cap(loaded["conversations"], "max_active_turns") == 4
    assert turn_cap(loaded["conversations"], "turn_slots_per_lane") is None
    assert turn_cap({}, "max_active_turns") is None and turn_cap(None, "turn_slots_per_lane") is None
    with pytest.raises(KeyError):
        turn_cap({"compact_per_tick": 20}, "compact_per_tick")


@pytest.mark.parametrize("section,error_key", [
    ({"max_active_turns": 0}, "conversations.max_active_turns"),
    ({"max_active_turns": -1}, "conversations.max_active_turns"),
    ({"max_active_turns": True}, "conversations.max_active_turns"),
    ({"max_active_turns": "3"}, "conversations.max_active_turns"),
    ({"turn_slots_per_lane": 1.5}, "conversations.turn_slots_per_lane"),
    ({"turn_slots_per_lane": float("inf")}, "conversations.turn_slots_per_lane"),
    # Counts that are not caps still require a number.
    ({"compact_per_tick": None}, "conversations.compact_per_tick"),
])
def test_invalid_turn_caps_name_the_key(tmp_path, policy_data, section, error_key):
    """C-26.9, C-11.1: a turn cap is null or a positive whole number; nothing else is."""
    policy_data["conversations"] = section
    path = tmp_path / "custom-policy.json"
    path.write_text(json.dumps(policy_data), encoding="utf-8")
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == error_key


@pytest.mark.parametrize("section,error_key", [
    ([], "conversations"),
    ({"stop_sigint_after_s": 0}, "conversations.stop_sigint_after_s"),
    ({"stop_close_after_s": -1}, "conversations.stop_close_after_s"),
    ({"stop_contain_after_s": True}, "conversations.stop_contain_after_s"),
    ({"after_result_s": "135"}, "conversations.after_result_s"),
    ({"approval_wait_s": float("inf")}, "conversations.approval_wait_s"),
    ({"approval_wait_s": 0}, "conversations.approval_wait_s"),
    ({"approval_wait_s": -1}, "conversations.approval_wait_s"),
    ({"approval_wait_s": True}, "conversations.approval_wait_s"),
    ({"approval_wait_s": "3600"}, "conversations.approval_wait_s"),
    ({"approval_wait_s": float("nan")}, "conversations.approval_wait_s"),
    # C-24.7: the escalation keeps its order.
    ({"stop_sigint_after_s": 20, "stop_close_after_s": 10}, "conversations.stop_close_after_s"),
    ({"stop_close_after_s": 30, "stop_contain_after_s": 30}, "conversations.stop_close_after_s"),
])
def test_invalid_conversation_clocks_name_the_key(tmp_path, policy_data, section, error_key):
    """C-24.7, C-26.5, C-26.9, C-11.1: the loader refuses a clock that is not a positive
    finite number of seconds, and an escalation out of order, naming the key."""
    policy_data["conversations"] = section
    path = tmp_path / "custom-policy.json"
    # json.dumps writes Infinity for inf, which Python's json reads back.
    path.write_text(json.dumps(policy_data), encoding="utf-8")
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == error_key
