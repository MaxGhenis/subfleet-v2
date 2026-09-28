"""C-26.8: the ultracode effort level, and the conversation default effort.

Claude Code's ultracode is xhigh effort plus standing dynamic-workflow orchestration,
set per session through the `ultracode` settings key (2.1.280's own settings
description); `--effort` itself takes only low, medium, high, xhigh and max. A
conversation names it as its effort; a Claude turn with no effort runs at the
policy's `conversations.default_effort`, ultracode by default, where the model's
catalog offers it.
"""
from __future__ import annotations

import json

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet.conversations import claude_turn
from subfleet.conversations.claude_turn import (
    ULTRACODE, ULTRACODE_EFFORT, ClaudeTurn, argv, observed_catalog, offered_efforts,
)
from subfleet.policy import CONVERSATION_DEFAULT_EFFORT
from tests.unit.test_claude_turn import INIT_OK, SID, spec, started
from tests.unit.test_conversation_service import SETTINGS, conversation, submit, svc  # noqa: F401 (a fixture)

LEVELS = ["low", "medium", "high", "xhigh", "max"]


# --- the level ---------------------------------------------------------------------


@hypothesis.given(st.lists(st.sampled_from(["low", "medium", "high", "xhigh", "max", "ultra", ULTRACODE]),
                           unique=True))
def test_c26_8_ultracode_is_offered_exactly_where_xhigh_is(levels):
    """Invariants: the offered set keeps every catalog level in order, adds ultracode iff
    xhigh (the effort it runs at) is offered, never duplicates it, and is idempotent."""
    offered = offered_efforts(levels)
    assert offered[:len(levels)] == levels
    assert (ULTRACODE in offered) == (ULTRACODE_EFFORT in levels or ULTRACODE in levels)
    assert offered.count(ULTRACODE) <= 1
    assert offered_efforts(offered) == offered


def test_c26_8_the_catalog_offers_ultracode_on_models_with_xhigh_and_not_on_haiku():
    body = json.loads(INIT_OK)["response"]["response"]
    catalog = {entry["value"]: entry["efforts"] for entry in observed_catalog(body["models"])}
    assert catalog["opus"] == [*LEVELS, ULTRACODE]
    assert catalog["haiku"] == []


# --- the command line --------------------------------------------------------------


def test_c26_8_an_ultracode_turn_runs_at_xhigh_with_the_ultracode_setting():
    command = argv(spec(effort=ULTRACODE))
    assert command[command.index("--effort") + 1] == "xhigh"
    assert "ultracode" not in command[command.index("--effort") + 1]
    settings = json.loads(command[command.index("--settings") + 1])
    assert settings == {"disableAllHooks": False, "ultracode": True}
    fast = argv(spec(effort=ULTRACODE, fast=True, permission="bypass"))
    assert json.loads(fast[fast.index("--settings") + 1]) == {"disableAllHooks": False, "fastMode": True,
                                                              "ultracode": True}


@pytest.mark.parametrize("effort", [None, "low", "high", "xhigh", "max"])
def test_c26_8_every_other_effort_passes_through_and_sets_no_ultracode(effort):
    command = argv(spec(effort=effort))
    if effort:
        assert command[command.index("--effort") + 1] == effort
    else:
        assert "--effort" not in command
    assert "ultracode" not in " ".join(command)


def test_c26_8_a_read_only_ultracode_turn_gets_only_its_effort():
    """Read-only turns have no Workflow tool, so ultracode there is its xhigh effort alone."""
    command = argv(spec(effort=ULTRACODE, permission="read-only"),
                   read_only_flags=("--permission-mode", "plan", "--setting-sources", ""))
    assert command[command.index("--effort") + 1] == "xhigh"
    assert "ultracode" not in " ".join(command)


# --- validation before sending -----------------------------------------------------


def test_c26_8_ultracode_is_accepted_where_xhigh_is_offered_and_refused_elsewhere():
    ok = started(ClaudeTurn(spec(effort=ULTRACODE), read_bytes=lambda p: b""))
    assert ok.outcome is None and [f.tag for f in ok.frames] == ["user-message"]
    served = next(e.data for e in ok.events if e.kind == "served")
    assert served["effort"] == ULTRACODE                     # the chip names what the turn ran at
    haiku = started(ClaudeTurn(spec(model_id="haiku", effort=ULTRACODE), read_bytes=lambda p: b""))
    assert haiku.outcome.reason == "effort-unsupported"


def test_c26_8_a_turn_without_an_effort_reports_none():
    step = started(ClaudeTurn(spec(effort=None), read_bytes=lambda p: b""))
    assert "effort" not in next(e.data for e in step.events if e.kind == "served")


# --- the policy --------------------------------------------------------------------


def _policy(tmp_path, **conversations):
    """The live policy file with `conversations` changed, loaded the way the daemon loads it."""
    from pathlib import Path
    from subfleet.policy import load_policy
    base = json.loads((Path(__file__).resolve().parents[2] / "subfleet" / "default_policy.json").read_text()) \
        if (Path(__file__).resolve().parents[2] / "subfleet" / "default_policy.json").exists() else None
    if base is None:
        pytest.skip("no example policy in the repository to load")
    base.setdefault("conversations", {}).update(conversations)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(base))
    return load_policy(path)


def test_c26_8_the_default_is_ultracode_for_claude_and_the_provider_own_for_codex():
    assert CONVERSATION_DEFAULT_EFFORT == {"claude": ULTRACODE, "codex": None}


@pytest.mark.parametrize("value", [{"claude": "high"}, {"claude": None}, {"codex": "ultra"}, {}])
def test_c26_8_policy_accepts_an_effort_or_null_per_provider(value, tmp_path):
    _policy(tmp_path, default_effort=value)


@pytest.mark.parametrize("value,where", [
    ("ultracode", "conversations.default_effort"),
    ({"gemini": "high"}, "conversations.default_effort.gemini"),
    ({"claude": 3}, "conversations.default_effort.claude"),
    ({"claude": ""}, "conversations.default_effort.claude"),
])
def test_c26_8_policy_refuses_a_malformed_default(value, where, tmp_path):
    from subfleet.policy import PolicyError
    with pytest.raises(PolicyError, match=where.replace(".", r"\.")):
        _policy(tmp_path, default_effort=value)


# --- the default at turn build, and models.list ------------------------------------


def _catalog(svc, efforts):
    path = svc.daemon.root / "conversations" / "models.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"claude": {"claude-opus-5-5": {"values": ["opus"], "efforts": efforts}}}))


def _turns(svc, monkeypatch):
    seen = []
    submit_ = svc.daemon.submit

    def capture(args, *, turn=None):
        seen.append(turn)
        return submit_(args, turn=turn)
    monkeypatch.setattr(svc.daemon, "submit", capture)
    return seen


def test_c26_8_a_claude_message_without_an_effort_runs_at_the_default(svc, monkeypatch):
    _catalog(svc, LEVELS)
    turns = _turns(svc, monkeypatch)
    cid = conversation(svc)
    submit(svc, cid)
    svc._dispatch()
    assert turns[0]["settings"]["effort"] == ULTRACODE and turns[0]["effort_default"] == ULTRACODE


def test_c26_8_an_explicit_effort_is_kept(svc, monkeypatch):
    _catalog(svc, LEVELS)
    turns = _turns(svc, monkeypatch)
    cid = conversation(svc)
    svc.store.submit_message(conversation_id=cid, message_id="5d6f0c2e-1a2b-4c3d-8e4f-a0b1c2d3e4f5", after_message_id=None, text="hi",
                             attachments=[], settings={**SETTINGS, "effort": "high"})
    svc._dispatch()
    assert turns[0]["settings"]["effort"] == "high" and turns[0]["effort_default"] is None


@pytest.mark.parametrize("efforts", [[], ["low", "high"], None])
def test_c26_8_no_default_where_the_catalog_does_not_offer_it(svc, monkeypatch, efforts):
    """A default never fails a turn: a model whose catalog lacks xhigh (or that no turn
    has reported yet) runs at the provider's own default."""
    if efforts is not None:
        _catalog(svc, efforts)
    turns = _turns(svc, monkeypatch)
    submit(svc, conversation(svc))
    svc._dispatch()
    assert turns[0]["settings"]["effort"] is None and turns[0]["effort_default"] is None


def test_c26_8_policy_null_turns_the_default_off(svc, monkeypatch):
    _catalog(svc, LEVELS)
    svc.daemon.policy["conversations"]["default_effort"] = {"claude": None}
    turns = _turns(svc, monkeypatch)
    submit(svc, conversation(svc))
    svc._dispatch()
    assert turns[0]["settings"]["effort"] is None


def test_c26_8_models_list_offers_ultracode_and_names_the_default(svc):
    _catalog(svc, LEVELS)                      # a catalog cached before this change: no ultracode in it
    entry = svc.handle("models.list", {"provider": "claude"}, None)["models"][0]
    assert entry["efforts"] == [*LEVELS, ULTRACODE]
    assert entry["conversation_default_effort"] == ULTRACODE
    _catalog(svc, ["low"])
    entry = svc.handle("models.list", {"provider": "claude"}, None)["models"][0]
    assert entry["conversation_default_effort"] is None


def test_c26_8_the_stored_settings_are_not_rewritten(svc, monkeypatch):
    """The default is resolved per turn, so changing the policy changes every later turn;
    the conversation and message keep `effort: null`."""
    _catalog(svc, LEVELS)
    _turns(svc, monkeypatch)
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc._dispatch()
    assert svc.store.message(mid)["settings"]["effort"] is None
    assert svc.store.conversation(cid)["settings"]["effort"] is None


def test_c26_8_the_ultracode_constant_is_what_the_cli_is_given():
    assert claude_turn.ULTRACODE_EFFORT in LEVELS and claude_turn.ULTRACODE not in LEVELS
    assert SID  # the imported fixture spec resumes this session
