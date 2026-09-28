"""C-11.1, C-17.2 on the conversation surface: a model the running policy retires
routes to its successor, and the turn runs the successor's catalog value.

Fable was retired on 2026-09-27 (Max: "opus 5.5 is strictly better than fable").
The desktop line stores a conversation's model as the value the person picked
(`claude-fable-5-1[1m]`), so without this a policy that drops `fable` would fail
every message in such a conversation as `unknown-model`. The running policy
decides: under one that still lists Fable, a Fable conversation stays on Fable.
"""

from __future__ import annotations

import json
import uuid

import hypothesis
import pytest
from hypothesis import strategies as st

from subfleet.conversations.service import policy_model
from subfleet.conversations.store import ConversationError
from subfleet.policy import DEFAULT_POLICY_PATH, RETIRED_MODELS, load_policy
from tests.unit.test_conversation_service import SETTINGS, conversation, svc  # noqa: F401 (a fixture)

SHIPPED = load_policy(DEFAULT_POLICY_PATH)
FABLE_SPELLINGS = ("fable", "claude-fable-5", "claude-fable-5-1")

RETIRING = {"models": {"opus": {"provider": "claude", "id": "claude-opus-5-5"},
                       "astra": {"provider": "codex", "id": "gpt-6-astra"}},
            "retired": {"fable": "opus", "claude-fable-5": "opus", "claude-fable-5-1": "opus", "sol": "astra"}}
# The live policy as of 2026-09-28 (d574 open): Fable is still a model, and one old id maps to it.
LISTING_FABLE = {"models": {"fable": {"provider": "claude", "id": "claude-fable-5-1"},
                            "opus": {"provider": "claude", "id": "claude-opus-5-5"}},
                 "retired": {"claude-fable-5": "fable", "sol": "astra"}}


def current(policy: dict) -> dict[str, str]:
    """Every current model's short name, by each spelling a conversation may store."""
    spellings = {}
    for short, entry in policy["models"].items():
        spellings[short] = short
        spellings[entry["id"]] = short
    return spellings


# --- policy_model ----------------------------------------------------------------


def test_the_shipped_policy_retires_every_fable_spelling_to_opus():
    """The conversation surface agrees with the shipped `retired` map and the
    CLI's RETIRED_MODELS table (a differential check of the two sources)."""
    for spelling in FABLE_SPELLINGS:
        for suffix in ("", "[1m]"):
            assert policy_model(SHIPPED, "claude", spelling + suffix) == "opus"
    assert policy_model(SHIPPED, "claude", "fable") == RETIRED_MODELS["fable"]


@pytest.mark.parametrize("policy", [SHIPPED, RETIRING, LISTING_FABLE], ids=["shipped", "retiring", "listing-fable"])
@hypothesis.given(value=st.one_of(st.sampled_from(sorted({*FABLE_SPELLINGS, "sol", "opus", "haiku", "astra",
                                                          "claude-opus-5-5", "gpt-6-astra", "gpt-5.6-sol",
                                                          "default", "gpt-9", ""})),
                                  st.text(min_size=0, max_size=24)),
                  suffix=st.sampled_from(("", "[1m]")),
                  provider=st.sampled_from(("claude", "codex")))
def test_a_conversation_model_resolves_to_a_current_model_of_its_provider_or_is_refused(policy, value, suffix,
                                                                                       provider):
    """Invariants, for every stored value and provider:
    - the result is a current `models` key of that provider, never a retired alias;
    - a current spelling resolves to itself (retirement changes nothing else);
    - a `retired` spelling resolves to its successor exactly when the successor is
      that provider's; anything else is refused as `unknown-model`."""
    stored = value + suffix
    base = stored[:-4] if stored.endswith("[1m]") else stored
    spellings = current(policy)
    retired = policy.get("retired") or {}
    try:
        short = policy_model(policy, provider, stored)
    except ConversationError as exc:
        assert exc.reason == "unknown-model"
        assert not (base in spellings and policy["models"][spellings[base]]["provider"] == provider)
        assert not (base not in spellings and base in retired
                    and policy["models"].get(retired[base], {}).get("provider") == provider)
        return
    assert short in policy["models"] and policy["models"][short]["provider"] == provider
    assert short not in retired and short not in RETIRED_MODELS.keys() - policy["models"].keys()
    if base in spellings:
        assert short == spellings[base]
    else:
        assert short == retired[base]


def test_a_retired_alias_never_crosses_providers():
    """`sol` retires to Astra, a Codex model: a Claude conversation cannot route it."""
    with pytest.raises(ConversationError) as refused:
        policy_model(RETIRING, "claude", "sol")
    assert refused.value.reason == "unknown-model"
    with pytest.raises(ConversationError):
        policy_model(RETIRING, "codex", "claude-fable-5-1")


# --- the turn a retired conversation submits -------------------------------------


def turns_of(svc):  # noqa: F811 (the fixture's name)
    """Record the turn manifest each submit carries, as the daemon receives it."""
    turns: list[dict] = []
    submit = svc.daemon.submit

    def recording(args, *, turn=None):
        turns.append(turn)
        return submit(args, turn=turn)

    svc.daemon.submit = recording
    return turns


def send(svc, cid, model: str) -> str:  # noqa: F811
    mid = str(uuid.uuid4())
    svc.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None, text="hello",
                             attachments=[], settings={**SETTINGS, "model": model})
    return mid


def catalog(svc, values: dict[str, list[str]]) -> None:  # noqa: F811
    path = svc.root / "conversations" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claude": {model_id: {"values": listed, "efforts": []}
                                           for model_id, listed in values.items()}}))


@pytest.mark.parametrize("stored", ["claude-fable-5-1[1m]", "claude-fable-5-1", "fable", "claude-fable-5"])
@pytest.mark.parametrize("listed, expected", [
    (None, "claude-opus-5-5"),                      # no catalog yet: the successor's id
    (["opus"], "opus"),                             # the value models.list offers for Opus
    (["default", "opus"], "opus"),                  # `default` is never a conversation's model
])
def test_a_fable_conversation_runs_its_turn_on_opus_once_the_policy_retires_fable(svc, stored, listed, expected):  # noqa: F811
    svc.daemon.policy = {**svc.daemon.policy, **json.loads(json.dumps(RETIRING))}
    if listed is not None:
        catalog(svc, {"claude-opus-5-5": listed})
    turns = turns_of(svc)
    cid = conversation(svc, settings={**SETTINGS, "model": stored})
    mid = send(svc, cid, stored)
    svc._dispatch()
    assert [a.pinned_model for a in svc.daemon.submits] == ["opus"]
    assert [t["settings"]["model"] for t in turns] == [expected]
    # The message keeps what the person picked; the successor is resolved per turn.
    assert svc.store.message(mid)["settings"]["model"] == stored
    assert svc.store.message(mid)["state"] != "failed"


@pytest.mark.parametrize("stored", ["opus", "claude-opus-5-5", "claude-opus-5-5[1m]"])
def test_a_current_model_is_never_rewritten(svc, stored):  # noqa: F811
    svc.daemon.policy = {**svc.daemon.policy, **json.loads(json.dumps(RETIRING))}
    catalog(svc, {"claude-opus-5-5": ["opus"]})
    turns = turns_of(svc)
    cid = conversation(svc, settings={**SETTINGS, "model": stored})
    send(svc, cid, stored)
    svc._dispatch()
    assert [t["settings"]["model"] for t in turns] == [stored]


@pytest.mark.parametrize("stored, pinned, runs", [
    ("claude-fable-5-1[1m]", "fable", "claude-fable-5-1[1m]"),
    ("fable", "fable", "fable"),
    ("claude-fable-5", "fable", "claude-fable-5-1"),   # the policy's own alias onto Fable's id
])
def test_under_a_policy_that_still_lists_fable_a_fable_conversation_stays_on_fable(svc, stored, pinned, runs):  # noqa: F811
    """The running policy decides (d574): the live policy still lists Fable, and a
    conversation on Fable keeps running there until that policy retires it."""
    svc.daemon.policy = {**svc.daemon.policy, **json.loads(json.dumps(LISTING_FABLE))}
    turns = turns_of(svc)
    cid = conversation(svc, settings={**SETTINGS, "model": stored})
    send(svc, cid, stored)
    svc._dispatch()
    assert [a.pinned_model for a in svc.daemon.submits] == [pinned]
    assert [t["settings"]["model"] for t in turns] == [runs]
