"""C-11.1, C-11.2: tier preferences and unchanged string policy loading."""

import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, flatten_chain, load_policy

MODELS = tuple(json.loads(DEFAULT_POLICY_PATH.read_text())["models"])
ENTRY = st.one_of(st.sampled_from(MODELS),
                  st.lists(st.sampled_from(MODELS), min_size=1, max_size=len(MODELS), unique=True))
CHAINS = st.lists(ENTRY, min_size=4, max_size=4)


def write_policy(tmp_path, chain):
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    data["chains"]["review"] = chain
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    return path


@settings(max_examples=250, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(CHAINS)
def test_every_valid_mix_loads(tmp_path, chain):
    """Any valid combination of strings and preference lists retains its shape."""
    assert load_policy(write_policy(tmp_path, chain))["chains"]["review"] == chain


@pytest.mark.parametrize("entry,offset,message", [
    ([], "", "unknown model []; expected a models key"),
    ("typo", "", "unknown model 'typo'; expected a models key"),
    ("sol", "", "unknown model 'sol'; expected a models key"),
    (["opus", "typo"], "[1]", "unknown model 'typo'; expected a models key"),
    (["opus", "sol"], "[1]", "unknown model 'sol'; expected a models key"),
    (["opus", "opus"], "[1]", "duplicate model 'opus'"),
    (["opus", []], "[1]", "unknown model []; expected a models key"),
    (["opus", ""], "[1]", "unknown model ''; expected a models key"),
    (["opus", None], "[1]", "unknown model None; expected a models key"),
    (["opus", 1], "[1]", "unknown model 1; expected a models key"),
    (None, "", "unknown model None; expected a models key"),
    ({"opus": 1}, "", "unknown model {'opus': 1}; expected a models key"),
])
def test_invalid_entries_keep_existing_diagnostics(tmp_path, entry, offset, message):
    with pytest.raises(PolicyError) as caught:
        load_policy(write_policy(tmp_path, ["haiku", "sonnet", entry, "astra"]))
    assert caught.value.key == "chains.review[2]" + offset
    assert str(caught.value).endswith(message)


def test_default_and_live_string_chain_shape_load_unchanged(tmp_path):
    """Use the supplied live shape without reading the live ~/.subfleet."""
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    assert load_policy(DEFAULT_POLICY_PATH)["chains"] == data["chains"]
    data["models"].update(luna={"provider": "codex", "id": "gpt-6-luna"},
                          sol61={"provider": "codex", "id": "gpt-6.1-sol"})
    data["chains"]["review"] = ["luna", "haiku", "sol61", "sol61"]
    path = tmp_path / "supplied-live-shape.json"
    path.write_text(json.dumps(data))
    assert load_policy(path)["chains"] == data["chains"]
    assert flatten_chain(data["chains"]["review"], 2) == ["sol61"]
    data["chains"]["review"] = ["luna", ["haiku", "sonnet"], ["sol61", "opus"], ["sol61", "opus"]]
    path.write_text(json.dumps(data))
    loaded = load_policy(path)
    assert flatten_chain(loaded["chains"]["review"], 1) == ["haiku", "sonnet", "sol61", "opus"]


@settings(max_examples=80, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(CHAINS, st.integers(0, 3), st.sampled_from(["empty", "unknown", "retired", "duplicate"]),
       st.sampled_from(MODELS))
def test_generated_invalid_lists_are_rejected(tmp_path, chain, tier, fault, model):
    invalid = {"empty": [], "unknown": [model, "unknown-model"],
               "retired": [model, "sol"], "duplicate": [model, model]}[fault]
    chain[tier] = invalid
    with pytest.raises(PolicyError) as caught:
        load_policy(write_policy(tmp_path, chain))
    assert caught.value.key.startswith(f"chains.review[{tier}]")
