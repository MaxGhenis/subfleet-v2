"""C-11.3: generated laws for use-it-or-lose-it weekly routing.

These properties exercise the real admission path over fleets, rather than
reimplementing the comparator. Admission is also compared with the prior
contract's independent reference: reserves are preferences, never refusals.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.capacity import build_view
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate, prepare, rank_key
from tests.reference_scheduler import reference_evaluate


NOW = "2026-10-03T12:00:00Z"
CLOCK = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
PROVIDERS = [("claude", "opus"), ("codex", "astra")]
LAWS = settings(max_examples=400, deadline=None, derandomize=True)


def reset(seconds: int) -> str:
    return (CLOCK + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def policy(spread=2):
    result = copy.deepcopy(load_policy(DEFAULT_POLICY_PATH))
    result["reserve"]["models"] = []
    # The live policy has zero floor; the shipped default remains 15%. These
    # laws must reach the new preference classes without an admission refusal.
    result["headroom_floor"] = 0
    result["admission"].update(lane_spread=spread, weekly_reserve=.02, five_hour_reserve=.10)
    return result


def lane(identity: str, **changes):
    return {"lane_id": identity, "provider": identity.split("-")[0], "owner": "v2", "enabled": True,
            "desktop": False, "account_key": identity, "home": "/lanes/" + identity, **changes}


def reading(identity: str, headroom: float, when: str | None, *, window="seven_day", scope="account",
            age=0):
    return {"lane_id": identity, "scope": scope, "window": window, "utilization": 1 - headroom,
            "resets_at": when, "observed_at": reset(-age), "label": "provider", "source": "usage"}


def fleet(provider, specs, *, desktop=(), closures=(), rows=(), spread=2, affinity=None, turn=False):
    """Build a generated fleet; a spec is (weekly room, five-hour room, reset, load)."""
    identities = [f"{provider}-{n}" for n in range(len(specs))]
    readings = list(rows)
    for identity, (weekly, five_hour, when, load) in zip(identities, specs):
        readings += [reading(identity, weekly, when),
                     reading(identity, five_hour, reset(3600), window="five_hour")]
    view = build_view([lane(identity, desktop=n in desktop) for n, identity in enumerate(identities)],
                      readings, closures, [], [], now=NOW)
    view["in_flight_turns" if turn else "in_flight"] = {
        identity: spec[3] for identity, spec in zip(identities, specs)}
    job = {"pinned_model": "opus" if provider == "claude" else "astra", "allow_desktop": True,
           "sandbox": "read-only", "kind": "turn" if turn else "dispatch"}
    if affinity is not None:
        job["affinity_lane"] = identities[affinity]
    return policy(spread), view, job


def order(case):
    return evaluate(*case).evaluations[0]["candidates"]


@st.composite
def comparable_pairs(draw, *, same_reset=False):
    spread = draw(st.sampled_from([2, 4, None]))
    band = draw(st.integers(0, 10))
    loads = [draw(st.integers(0, 20) if spread is None else st.integers(band * spread, (band + 1) * spread - 1))
             for _ in range(2)]
    weekly_under = draw(st.booleans())
    five_under = draw(st.booleans())
    weekly = [draw(st.integers(1, 19) if weekly_under else st.integers(21, 999)) / 1000 for _ in range(2)]
    five = draw(st.integers(1, 99) if five_under else st.integers(101, 999)) / 1000
    early = draw(st.integers(1, 500_000))
    late = early if same_reset else early + draw(st.integers(1, 100_000))
    specs = [(weekly[n], five, reset(early if n == 0 else late), loads[n]) for n in range(2)]
    extras = draw(st.lists(st.tuples(st.integers(1, 999), st.integers(1, 999),
                                    st.integers(1, 600_000), st.integers(0, 20)), max_size=6))
    specs += [(weekly / 1000, five / 1000, reset(when), load) for weekly, five, when, load in extras]
    return spread, specs


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(comparable_pairs())
def test_earlier_weekly_reset_never_ranks_worse_in_one_band_and_reserve_class(provider, model, case):
    """(a) Earlier expiry wins even when its weekly headroom and load are worse."""
    spread, specs = case
    candidates = order(fleet(provider, specs, spread=spread))
    assert candidates.index(f"{provider}-0") < candidates.index(f"{provider}-1")


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(comparable_pairs(same_reset=True))
def test_equal_resets_more_weekly_headroom_never_ranks_worse(provider, model, case):
    """(b) At equal resets weekly headroom precedes load and lane identity."""
    spread, specs = case
    first, second = specs[:2]
    if first[0] == second[0]:
        return
    higher, lower = (0, 1) if first[0] > second[0] else (1, 0)
    candidates = order(fleet(provider, specs, spread=spread))
    assert candidates.index(f"{provider}-{higher}") < candidates.index(f"{provider}-{lower}")


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(weekly_under=st.booleans(), five_under=st.booleans(), load=st.integers(0, 30),
       when=st.integers(1, 500_000))
def test_a_lane_under_either_reserve_never_precedes_an_otherwise_equal_clear_lane(
        provider, model, weekly_under, five_under, load, when):
    """(c) Each guard beats earlier expiry, while leaving the guarded lane eligible."""
    if not weekly_under and not five_under:
        five_under = True
    specs = [(.01 if weekly_under else .5, .05 if five_under else .5, reset(when), load),
             (.5, .5, reset(when + 1), load)]
    assert order(fleet(provider, specs)) == [f"{provider}-1", f"{provider}-0"]


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(comparable_pairs(), st.integers(0, 1))
def test_desktop_last_and_a_turns_affinity_first_before_weekly_preferences(provider, model, case, affinity):
    """(d) Desktop remains the leading key; affinity leads the remaining turn lanes."""
    spread, specs = case
    candidates = order(fleet(provider, specs, desktop=[len(specs) - 1], spread=spread,
                             affinity=affinity, turn=True))
    assert candidates[-1] == f"{provider}-{len(specs) - 1}"
    if affinity != len(specs) - 1:
        assert candidates[0] == f"{provider}-{affinity}"


@LAWS
@given(when=st.integers(1, 500_000), load=st.integers(0, 30), weekly=st.integers(1, 19),
       five=st.integers(1, 99))
def test_claude_stranded_term_remains_before_measured_and_reserve_preferences(when, load, weekly, five):
    """(d) C-23.37 still prefers the stranded lane before the new reserve keys."""
    specs = [(weekly / 1000, five / 1000, reset(when + 1), load), (.9, .9, reset(when), load)]
    closure = {"lane_id": "claude-0", "scope": "claude-fable-5-1", "until_at": reset(when),
               "reason": "provider-limit", "source_event": "limited", "clock_source": "reported"}
    assert order(fleet("claude", specs, closures=[closure])) == ["claude-0", "claude-1"]
    # It also precedes measured/unmeasured, just as the old comparator did.
    case = fleet("claude", specs, closures=[closure])
    case[1]["readings"] = [row for row in case[1]["readings"] if row["lane_id"] != "claude-0"]
    assert order(case) == ["claude-0", "claude-1"]


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(spread=st.sampled_from([2, 4]), band=st.integers(0, 10), when=st.integers(1, 500_000))
def test_load_band_precedes_measured_and_weekly_preferences(provider, model, spread, band, when):
    """(d) The burst guard still beats every usage ranking key in both providers."""
    case = fleet(provider, [(.01, .05, reset(when + 1), band * spread),
                            (.9, .9, reset(when), (band + 1) * spread)], spread=spread)
    # A lower band wins even when it is unmeasured and the next band is clear.
    case[1]["readings"] = [row for row in case[1]["readings"] if row["lane_id"] != f"{provider}-0"]
    if provider == "claude":
        # A stranded lane also cannot jump over a lower load band.
        case[1]["closures"].append({"lane_id": "claude-1", "scope": "claude-fable-5-1",
                                    "until_at": reset(when), "reason": "provider-limit"})
    assert order(case) == [f"{provider}-0", f"{provider}-1"]


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(pair=comparable_pairs(), age=st.sampled_from([121, 600]))
def test_measured_precedes_unmeasured_in_the_same_band(provider, model, pair, age):
    """(d) Within the preserved prefix, measured status still precedes both reserves."""
    spread, specs = pair
    first, second = specs[:2]
    specs[:2] = [(.01, .05, first[2], first[3]), (.9, .9, second[2], second[3])]
    case = fleet(provider, specs, spread=spread)
    for row in case[1]["readings"]:
        if row["lane_id"] == f"{provider}-1":
            row["observed_at"] = reset(-age)
    candidates = order(case)
    assert candidates.index(f"{provider}-0") < candidates.index(f"{provider}-1")


@st.composite
def heterogeneous_fleets(draw, provider):
    specs = draw(st.lists(st.tuples(st.integers(1, 999), st.integers(1, 999),
                                   st.one_of(st.none(), st.integers(-3600, 600_000)), st.integers(0, 30)),
                          min_size=1, max_size=8))
    specs = [(weekly / 1000, five / 1000, reset(when) if when is not None else None, load)
             for weekly, five, when, load in specs]
    case = fleet(provider, specs, spread=draw(st.sampled_from([2, 4, None])),
                 desktop=draw(st.lists(st.integers(0, len(specs) - 1), unique=True)),
                 affinity=draw(st.one_of(st.none(), st.integers(0, len(specs) - 1))), turn=draw(st.booleans()))
    for row in case[1]["readings"]:
        row["observed_at"] = reset(-draw(st.sampled_from([0, 30, 119, 121, 600])))
    permutation = draw(st.permutations(case[1]["lanes"]))
    return case, permutation


@st.composite
def admission_fleets(draw, provider):
    """Exercise existing refusals as well as all four new preference classes."""
    case, _ = draw(heterogeneous_fleets(provider))
    if draw(st.booleans()):
        return case
    rules, view, job = case
    rules["headroom_floor"] = draw(st.sampled_from([0, .15, .3]))
    rules["admission"]["weekly_reserve"] = draw(st.sampled_from([0, .02, .2]))
    rules["admission"]["five_hour_reserve"] = draw(st.sampled_from([0, .1, .4]))
    for key in ("max_active_attempts", "max_in_flight_per_lane", "max_in_flight_unmeasured"):
        rules["caps"][key] = draw(st.one_of(st.none(), st.integers(1, 30)))
    for key in ("max_active_turns", "turn_slots_per_lane"):
        rules["conversations"][key] = draw(st.one_of(st.none(), st.integers(1, 30)))
    # The prior model-reserve admission guard is separate from the new ranking
    # reserves. It must reject exactly the same lanes when enabled.
    if provider == "claude" and draw(st.booleans()):
        rules["reserve"]["models"] = ["fable"]
    job["allow_desktop"] = draw(st.booleans())
    job["exclusions"] = []
    unavailable = {}
    model_scope = rules["models"][job["pinned_model"]]["id"]
    for candidate in view["lanes"]:
        identity = candidate["lane_id"]
        candidate["enabled"] = draw(st.booleans())
        candidate["owner"] = draw(st.sampled_from(["v2", "v1"]))
        candidate["desktop_in_use"] = draw(st.one_of(st.none(), st.booleans()))
        candidate["identity_status"] = draw(st.sampled_from(["verified", "mismatch"]))
        candidate["credential_kind"] = draw(st.sampled_from(["oauth", "home"]))
        if draw(st.booleans()):
            job["exclusions"].append(identity)
        if draw(st.booleans()):
            unavailable[identity] = "credential-latched"
        if draw(st.booleans()):
            view["closures"].append({"lane_id": identity,
                                     "scope": draw(st.sampled_from(["account", model_scope])),
                                     "until_at": reset(draw(st.integers(1, 500_000))),
                                     "reason": "provider-limit"})
        if draw(st.booleans()):
            view["readings"] = [row for row in view["readings"] if row["lane_id"] != identity]
    view["unavailable_lanes"] = unavailable
    return case


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(st.data())
def test_order_is_total_and_deterministic_for_generated_fleets(provider, model, data):
    """(e) Unique ids give unique keys and input ordering cannot change the result."""
    case, permutation = data.draw(heterogeneous_fleets(provider))
    decision = evaluate(*case)
    details = decision.evaluations[0]["candidate_details"]
    setup = prepare(*case)
    keys = [rank_key(setup, model, identity, detail) for identity, detail in details.items()]
    assert len(set(keys)) == len(keys)
    for left in keys:
        for right in keys:
            assert (left < right) + (left == right) + (left > right) == 1
    permuted = {**case[1], "lanes": list(permutation), "readings": list(reversed(case[1]["readings"]))}
    assert order(case) == order((case[0], permuted, case[2]))
    assert decision == evaluate(*case)


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(st.data())
def test_new_preferences_never_remove_a_previously_eligible_lane(provider, model, data):
    """(f) Existing floors, closures, identities, slots and model reserves are unchanged."""
    case = data.draw(admission_fleets(provider))
    old = reference_evaluate(*case).evaluations[0]
    new = evaluate(*case).evaluations[0]
    assert set(old["candidates"]) <= set(new["candidates"])
    assert {row["lane_id"]: row["reasons"] for row in old["rejections"]} == {
        row["lane_id"]: row["reasons"] for row in new["rejections"]}


@pytest.mark.parametrize("provider,model", PROVIDERS)
@pytest.mark.parametrize("weekly,five", [(.02, .10), (.25, .125), (.03, .4)])
def test_default_and_configured_reserve_boundaries_are_clear(provider, model, weekly, five):
    """Equality at either configured reserve is clear, including decimal defaults."""
    case = fleet(provider, [(weekly, five, reset(100), 0), (.5, .5, reset(200), 0)])
    case[0]["admission"].update(weekly_reserve=weekly, five_hour_reserve=five)
    result = evaluate(*case).evaluations[0]
    assert result["candidate_details"][f"{provider}-0"]["reserve_class"] == "clear"
    assert result["candidates"] == [f"{provider}-0", f"{provider}-1"]


@pytest.mark.parametrize("provider,model", PROVIDERS)
def test_weekly_reserve_precedes_five_hour_reserve(provider, model):
    """The reserve tuple is lexicographic: clear weekly capacity wins first."""
    assert order(fleet(provider, [(.01, .9, reset(100), 0), (.9, .05, reset(200), 0)])) == [
        f"{provider}-1", f"{provider}-0"]


@pytest.mark.parametrize("provider,model", PROVIDERS)
def test_binding_weekly_scope_supplies_both_headroom_and_reset(provider, model):
    """An earlier nonbinding reset cannot displace the minimum-room weekly window."""
    specs = [(.8, .5, reset(100), 0), (.4, .5, reset(200), 0)]
    scoped = reading(f"{provider}-0", .3, reset(300), scope=policy()["models"][model]["id"])
    case = fleet(provider, specs, rows=[scoped])
    result = evaluate(*case).evaluations[0]
    assert result["candidates"] == [f"{provider}-1", f"{provider}-0"]
    details = result["candidate_details"][f"{provider}-0"]
    assert details["weekly_headroom"] == pytest.approx(.3)
    assert details["seven_day_reset"] == reset(300)


@pytest.mark.parametrize("provider,model", PROVIDERS)
def test_unknown_reset_follows_known_reset_within_the_same_reserve_class(provider, model):
    assert order(fleet(provider, [(.9, .5, None, 0), (.2, .5, reset(500_000), 0)])) == [
        f"{provider}-1", f"{provider}-0"]


@pytest.mark.parametrize("provider,model", PROVIDERS)
@pytest.mark.parametrize("window", ["seven_day", "five_hour"])
def test_renewed_window_is_unmeasured_without_inventing_a_new_reading(provider, model, window):
    case = fleet(provider, [(.5, .5, reset(100), 0), (.3, .3, reset(200), 0)])
    for row in case[1]["readings"]:
        if row["lane_id"] == f"{provider}-0" and row["window"] == window:
            row["resets_at"] = reset(-1)
    before = copy.deepcopy(case[1]["readings"])
    result = evaluate(*case).evaluations[0]
    assert result["candidates"] == [f"{provider}-1", f"{provider}-0"]
    assert result["candidate_details"][f"{provider}-0"]["measured"] is False
    assert result["candidate_details"][f"{provider}-0"]["reserve_class"] == "unmeasured"
    assert case[1]["readings"] == before
