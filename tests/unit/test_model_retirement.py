"""Properties of model retirement (C-11.1, C-17.2): Sol on 2026-09-04, Fable on 2026-09-27.

A retired model is one the shipped policy's `retired` map names, or one the code retires
whatever a policy file says (`policy.RETIRED_MODELS`). These properties hold for every
chain, every spelling, and every fleet: nothing the shipped policy routes, reserves, or
resolves to is retired, and every surface that takes a model name from a person hands the
daemon a live successor.
"""

from __future__ import annotations

import argparse
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli
from subfleet.capacity import build_view
from subfleet.gate import service as gate_service
from subfleet.policy import DEFAULT_POLICY_PATH, RETIRED_MODELS, load_policy, resolve_model
from subfleet.scheduler import evaluate
from subfleet.sessions import cli as sessions_cli

NOW = "2026-09-27T12:00:00Z"
LATER = "2026-09-28T12:00:00Z"
SHIPPED = load_policy(DEFAULT_POLICY_PATH)
#: Every retired spelling: the policy's aliases (short names and exact ids) and the code's.
RETIRED = frozenset(SHIPPED["retired"]) | frozenset(RETIRED_MODELS)
CURRENT = frozenset(SHIPPED["models"])
CURRENT_IDS = frozenset(model["id"] for model in SHIPPED["models"].values())
SPELLINGS = sorted(RETIRED | CURRENT | CURRENT_IDS)
QUIET = dict(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def test_the_default_policy_file_loads_and_retires_fable_and_sol():
    """The shipped file validates, and today's two retirements are in it."""
    assert {"sol", "fable", "claude-fable-5", "claude-fable-5-1"} <= RETIRED
    assert not RETIRED & CURRENT


@pytest.mark.parametrize("task", sorted(SHIPPED["chains"]))
def test_no_default_chain_names_a_retired_model(task):
    """For all chains and tiers: each entry is a current model, never a retired name, and
    never a model whose id a retirement alias claims."""
    chain = SHIPPED["chains"][task]
    assert len(chain) == len(SHIPPED["tiers"])
    for short in chain:
        assert short in CURRENT and short not in RETIRED
        assert SHIPPED["models"][short]["id"] not in RETIRED


def test_the_shipped_reserve_and_priorities_name_no_retired_model():
    """A reserve or a priority on a retired model would hold back or strand live work."""
    assert not set(SHIPPED.get("reserve", {}).get("models", ())) & RETIRED
    assert set(SHIPPED.get("reserve", {}).get("models", ())) <= CURRENT
    assert all(short in CURRENT for short, model in SHIPPED["models"].items() if "priority" in model)


def test_code_and_policy_retirements_agree():
    """Differential: the code's retirements and the shipped data name the same successor,
    and every successor is a current, unretired model (no retirement chains)."""
    for alias, successor in RETIRED_MODELS.items():
        assert SHIPPED["retired"][alias] == successor
    for successor in {*RETIRED_MODELS.values(), *SHIPPED["retired"].values()}:
        assert successor in CURRENT and successor not in RETIRED


@settings(max_examples=200, **QUIET)
@given(name=st.sampled_from(SPELLINGS))
def test_resolution_always_lands_on_a_live_model_and_is_idempotent(name, capsys):
    """For every spelling: resolve_model returns a current, unretired short name, a retired
    spelling resolves with a stderr note, and resolving the result again is a no-op."""
    short = resolve_model(SHIPPED, name)
    assert short in CURRENT and short not in RETIRED
    assert resolve_model(SHIPPED, short, note=False) == short
    noted = "resolves to" in capsys.readouterr().err
    assert noted == (name in SHIPPED["retired"])


def _lane(identity, desktop=False):
    provider = identity.split("-")[0]
    return {"lane_id": identity, "provider": provider, "account_key": f"{provider}:{identity}",
            "owner": "v2", "enabled": True, "desktop": desktop, "home": f"/lanes/{identity}"}


LANES = ["claude-1", "claude-2", "claude-3", "codex-1", "codex-2"]
SCOPES = ["account", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5", "gpt-6-astra"]
fleets = st.fixed_dictionaries({
    "lanes": st.lists(st.sampled_from(LANES), min_size=1, max_size=5, unique=True),
    "readings": st.lists(st.tuples(st.sampled_from(LANES), st.sampled_from(SCOPES),
                                   st.floats(0, 1, allow_nan=False)), max_size=8),
    "closures": st.lists(st.tuples(st.sampled_from(LANES), st.sampled_from(SCOPES)), max_size=4),
})
shapes = st.one_of(
    st.tuples(st.sampled_from(sorted(SHIPPED["chains"])), st.sampled_from(SHIPPED["tiers"]), st.none()),
    st.tuples(st.none(), st.none(), st.sampled_from(SPELLINGS)),
)


@settings(max_examples=300, **QUIET)
@given(fleet=fleets, shape=shapes)
def test_no_fleet_and_no_shape_evaluates_a_retired_model(fleet, shape, capsys):
    """For all fleets (lanes, readings incl. Fable's bucket, closures) and every task/tier
    or pin: evaluate() walks only current, unretired models and never chooses one retired."""
    task, tier, pin = shape
    lanes = [_lane(identity) for identity in fleet["lanes"]]
    readings = [{"lane_id": lane_id, "scope": scope, "window": "seven_day", "utilization": used,
                 "resets_at": LATER, "observed_at": NOW, "label": "provider", "source": "oauth-usage"}
                for lane_id, scope, used in fleet["readings"]]
    closures = [{"lane_id": lane_id, "scope": scope, "until_at": LATER, "reason": "provider-limit",
                 "clock_source": "reported", "source_event": "fixture"}
                for lane_id, scope in fleet["closures"]]
    job = {"sandbox": "read-only", "task": task, "tier": tier, "pinned_model": pin}
    decision = evaluate(SHIPPED, build_view(lanes, readings, closures, (), (), now=NOW), job)
    capsys.readouterr()
    assert decision.chain and set(decision.chain) <= CURRENT and not set(decision.chain) & RETIRED
    assert all(row["model_id"] not in RETIRED for row in decision.evaluations)
    assert decision.chosen_model is None or decision.chosen_model not in RETIRED


def _namespace(**values):
    return argparse.Namespace(**{"t": None, "overflow": False, "m": None, "task": None, "tier": None,
                                 **values})


@settings(max_examples=100, **QUIET)
@given(model=st.sampled_from(cli.MODEL_CHOICES), legacy=st.sampled_from([None, "fable"]))
def test_every_run_spelling_hands_the_daemon_a_live_model(model, legacy, capsys):
    """`run -m`/`-t` (C-17.2): whatever is typed, the submitted pin is not retired, and a
    note is printed exactly when a retired name was replaced."""
    args = _namespace(m=None if legacy else model, t=legacy)
    typed = legacy if legacy else model
    cli._apply_deprecations(args)
    err = capsys.readouterr().err
    assert args.m not in RETIRED and args.m in CURRENT
    assert ("is retired" in err) == (typed in RETIRED_MODELS)


@settings(max_examples=100, **QUIET)
@given(target=st.sampled_from(sessions_cli.HANDOFF_TARGETS),
       model=st.one_of(st.none(), st.sampled_from(sessions_cli.HANDOFF_TARGETS)))
def test_every_session_target_is_live(target, model, capsys):
    """`sessions handoff --to` and `revive --model` never pass a retired model on."""
    args = argparse.Namespace(target=target, model=model)
    sessions_cli.retire_models(args, "sessions handoff")
    err = capsys.readouterr().err
    assert args.target in CURRENT and args.target not in RETIRED
    assert args.model is None or (args.model in CURRENT and args.model not in RETIRED)
    assert err.count("is retired") == (target in RETIRED_MODELS) + (model in RETIRED_MODELS)


@pytest.mark.parametrize("peer", ["astra", "opus", "fable", "sol"])
def test_every_gate_peer_spelling_dispatches_a_live_peer(peer):
    """`gate --peer` (C-17.2): each accepted spelling is a live peer of a known family."""
    current = gate_service.current_peer(peer)
    assert current in gate_service.PEERS and current in CURRENT and current not in RETIRED
    assert SHIPPED["models"][current]["provider"] == ("claude" if peer in ("opus", "fable") else "codex")


def test_the_shipped_json_is_what_load_policy_validated():
    """No policy key hides a retired name the validator would not look at."""
    text = DEFAULT_POLICY_PATH.read_text()
    data = json.loads(text)
    assert "fable" not in json.dumps({key: value for key, value in data.items() if key != "retired"})
