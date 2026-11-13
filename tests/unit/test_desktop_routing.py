"""C-10.3, C-10.9, C-11.3: routing with and without the desktop app's signals.

Invariant 6 is a differential against release/217's own `capacity.py` and
`scheduler.py` at 266c3ab8b, frozen in `tests/fixtures/` and checked by their
SHA-256, so no test needs git history. Invariant 7 compares `evaluate` with
and without the signals. Every value is synthetic.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import capacity, scheduler
from subfleet.desktop_app import AccountEvent, AppSample, resolve
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy

LAWS = settings(max_examples=100, deadline=None, derandomize=True, database=None)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
#: `git show 266c3ab8b:subfleet/<name>.py | shasum -a 256`
FROZEN = {"capacity": "1771e1d1ff6174fd0d41e68ea2c1d3cef7b1a2edd6b02be75201160276f792f6",
          "scheduler": "f6c9ad0cae11fd8b9d624f856b0782ddb7e155cbc4dfca1ab85ce141d1da7d54"}
NOW = "2026-11-08T00:00:00Z"
CLOCK = datetime(2026, 11, 8, tzinfo=timezone.utc)
EMAILS = [f"user{n}@example.com" for n in range(4)]


def uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


ORGS = [uuid(0xA0 + n) for n in range(3)]


def frozen(name: str):
    """Release/217's module, checked by SHA-256 and loaded inside the package so
    its relative imports resolve."""
    path = FIXTURES / f"{name}_266c3ab8b.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FROZEN[name]
    module_name = f"subfleet._{name}_266c3ab8b"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


def at(seconds: int) -> str:
    return (CLOCK + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def policy(spread=2):
    result = copy.deepcopy(load_policy(DEFAULT_POLICY_PATH))
    result["reserve"]["models"] = []
    result["headroom_floor"] = 0
    result["admission"].update(lane_spread=spread)
    return result


@st.composite
def fleets(draw):
    lanes, readings, in_flight = [], [], {}
    for n in range(draw(st.integers(1, 6))):
        provider = draw(st.sampled_from(["claude", "claude", "codex"]))
        lane_id = f"{provider}-{n}"
        email = draw(st.sampled_from(EMAILS))
        key = draw(st.sampled_from([f"{provider}:{email}", f"{provider}:{email.upper()}",
                                    f"{provider}:{uuid(n)}:{uuid(9)}"]))
        lanes.append({"lane_id": lane_id, "provider": provider, "owner": draw(st.sampled_from(["v2", "v2", "v1"])),
                      "enabled": draw(st.sampled_from([True, True, False])), "desktop": draw(st.booleans()), "account_key": key,
                      "home": f"/lanes/{lane_id}", "label": draw(st.sampled_from([None, email])),
                      "identity": draw(st.sampled_from([None, f"{uuid(n)}:{uuid(9)}"]))})
        for window in ("seven_day", "five_hour"):
            if draw(st.booleans()):
                readings.append({"lane_id": lane_id, "scope": "account", "window": window,
                                 "utilization": draw(st.sampled_from([0.0, 0.3, 0.7, 0.99])),
                                 "resets_at": at(draw(st.integers(1, 7)) * 86400 if window == "seven_day" else 3600),
                                 "observed_at": at(-draw(st.sampled_from([0, 30, 600]))), "label": "provider",
                                 "source": "usage"})
        in_flight[lane_id] = draw(st.integers(0, 5))
    return lanes, readings, in_flight


@st.composite
def identities(draw, module):
    """A desktop identity as release/217 builds one (no app answer), in `module`'s class."""
    kind = draw(st.sampled_from(["none", "hint", "verified", "unverified"]))
    if kind == "none":
        return None, None
    if kind == "hint":
        return None, draw(st.sampled_from(EMAILS))
    if kind == "verified":
        n = draw(st.integers(0, 5))
        return module.DesktopIdentity("verified", f"{uuid(n)}:{uuid(9)}", draw(st.sampled_from(EMAILS))), None
    return module.DesktopIdentity("unverified", None, None, draw(st.sampled_from([None, *EMAILS])),
                                  draw(st.sampled_from([None, *EMAILS]))), None


def job_for(draw, lanes):
    provider = draw(st.sampled_from(sorted({lane["provider"] for lane in lanes})))
    job = {"pinned_model": "opus" if provider == "claude" else "astra", "sandbox": "read-only",
           "allow_desktop": draw(st.booleans()), "kind": draw(st.sampled_from(["dispatch", "turn"]))}
    if job["kind"] == "turn":
        job["affinity_lane"] = draw(st.sampled_from([lane["lane_id"] for lane in lanes]))
    return job


def same_identity(new_module, old):
    if old is None:
        return None
    return new_module.DesktopIdentity(**dataclasses.asdict(old))


def views(module, lanes, readings, in_flight, desktop, hint, in_use):
    view = module.build_view(copy.deepcopy(lanes), copy.deepcopy(readings), now=NOW, desktop=desktop,
                             desktop_account=hint, desktop_in_use=in_use)
    view["in_flight"] = dict(in_flight)
    return view


# --- the frozen copies ------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(FROZEN))
def test_c10_9_the_frozen_copies_are_266c3ab8b(name):
    assert hashlib.sha256((FIXTURES / f"{name}_266c3ab8b.py").read_bytes()).hexdigest() == FROZEN[name]


# --- invariant 6: no app file and no events, every decision as before -----------------

@LAWS
@given(data=st.data(), fleet=fleets(), in_use=st.sampled_from([None, True, False]))
def test_c10_9_invariant_6_without_the_signals_every_decision_equals_base(data, fleet, in_use):
    """Invariant 6: with no app answer, `build_view` and `evaluate` give exactly
    release/217's view and decision, the ranking included."""
    base_capacity, base_scheduler = frozen("capacity"), frozen("scheduler")
    lanes, readings, in_flight = fleet
    old_identity, hint = data.draw(identities(base_capacity))
    assert resolve(None, []) is None                    # what the daemon reads with neither signal
    new_identity = same_identity(capacity, old_identity)
    expected = views(base_capacity, lanes, readings, in_flight, old_identity, hint, in_use)
    found = views(capacity, lanes, readings, in_flight, new_identity, hint, in_use)
    assert found == expected
    job = job_for(data.draw, lanes)
    rules = policy(data.draw(st.sampled_from([2, None])))
    old = base_scheduler.evaluate(rules, expected, dict(job))
    new = scheduler.evaluate(rules, found, dict(job))
    assert dataclasses.asdict(new) == dataclasses.asdict(old)


DETAIL = st.fixed_dictionaries({
    "in_flight": st.integers(0, 6), "measured": st.booleans(), "ranking_measured": st.booleans(),
    "weekly_reserve": st.booleans(), "five_hour_reserve": st.booleans(),
    "seven_day_reset": st.one_of(st.none(), st.sampled_from([at(86400), at(172800)])),
    "weekly_headroom": st.one_of(st.none(), st.sampled_from([0.1, 0.5, 0.9])),
    "stranded_scopes": st.sampled_from([[], ["x"]]), "desktop": st.booleans()})


@LAWS
@given(details=st.lists(DETAIL, min_size=1, max_size=6), provider=st.sampled_from(["claude", "codex"]),
       spread=st.sampled_from([2, 4, None]), turn=st.booleans(), affinity=st.integers(0, 5))
def test_c10_9_invariant_6_rank_key_orders_as_base_without_desktop_possible(details, provider, spread, turn,
                                                                           affinity):
    base_scheduler = frozen("scheduler")
    rules = policy(spread)
    short = "opus" if provider == "claude" else "astra"
    job = {"kind": "turn" if turn else "dispatch", "affinity_lane": f"lane-{affinity}"}
    setup = {"job": job, "policy": rules, "lane_spread": spread}
    named = {f"lane-{n}": detail for n, detail in enumerate(details)}
    assert sorted(named, key=lambda lane: scheduler.rank_key(setup, short, lane, named[lane])) == \
           sorted(named, key=lambda lane: base_scheduler.rank_key(setup, short, lane, named[lane]))


# --- ranking: desktop_possible right after desktop -------------------------------------

@LAWS
@given(details=st.lists(DETAIL, min_size=2, max_size=6), possible=st.lists(st.booleans(), min_size=6, max_size=6),
       turn=st.booleans())
def test_c11_3_desktop_possible_sorts_right_after_desktop_in_every_comparator(details, possible, turn):
    """Mutant 5's test: among lanes alike in `desktop`, every lane known to be
    elsewhere sorts before every `desktop_possible` one, whatever comes after."""
    rules = policy(2)
    job = {"kind": "turn" if turn else "dispatch", "affinity_lane": "lane-0"}
    setup = {"job": job, "policy": rules, "lane_spread": 2}
    named = {f"lane-{n}": {**detail, **({"desktop_possible": True} if possible[n] else {})}
             for n, detail in enumerate(details)}
    for short in ("opus", "astra"):
        ranked = sorted(named, key=lambda lane: scheduler.rank_key(setup, short, lane, named[lane]))
        flags = [(bool(named[lane].get("desktop")), bool(named[lane].get("desktop_possible"))) for lane in ranked]
        assert flags == sorted(flags)
        key = scheduler.rank_key(setup, short, ranked[0], named[ranked[0]])
        assert key[:2] == flags[0]


def test_c11_3_a_possible_lane_with_more_room_still_sorts_after_one_known_elsewhere():
    """Mutant 5's test, end to end: the app's org is unlinked, so claude-0 (no
    event names its email) is `desktop_possible` and claude-1 (linked to another
    org) is not; claude-1 is chosen though claude-0 has more weekly room."""
    lanes = [{"lane_id": f"claude-{n}", "provider": "claude", "owner": "v2", "enabled": True, "desktop": False,
              "account_key": f"claude:{EMAILS[n]}", "home": f"/lanes/claude-{n}"} for n in range(2)]
    readings = [{"lane_id": f"claude-{n}", "scope": "account", "window": "seven_day", "utilization": use,
                 "resets_at": at(3 * 86400), "observed_at": NOW, "label": "provider", "source": "usage"}
                for n, use in ((0, 0.1), (1, 0.8))]
    app = resolve(AppSample(ORGS[0], NOW, 5.0, NOW),
                  [AccountEvent(1, ORGS[1], EMAILS[1], uuid(1), "s", at(-60))])
    view = capacity.build_view(lanes, readings, now=NOW, desktop=capacity.DesktopIdentity("unverified", app=app))
    view["in_flight"] = {}
    decision = scheduler.evaluate(policy(None), view, {"pinned_model": "opus", "sandbox": "read-only"})
    evaluation = decision.evaluations[0]
    assert evaluation["candidates"] == ["claude-1", "claude-0"]
    assert evaluation["candidate_details"]["claude-0"]["desktop_possible"] is True
    assert "desktop_possible" not in evaluation["candidate_details"]["claude-1"]
    assert decision.chosen_lane == "claude-1"
    plain = capacity.build_view(lanes, readings, now=NOW)
    plain["in_flight"] = {}
    assert scheduler.evaluate(policy(None), plain, {"pinned_model": "opus", "sandbox": "read-only"}
                              ).evaluations[0]["candidates"] == ["claude-0", "claude-1"]


# --- invariant 7: ranking only -------------------------------------------------------

@st.composite
def app_answers(draw):
    events = []
    for event_id in range(1, draw(st.integers(0, 4)) + 1):
        events.append(AccountEvent(event_id, draw(st.sampled_from(ORGS)), draw(st.sampled_from(EMAILS)), uuid(event_id),
                                   "s", at(-draw(st.integers(0, 10)) * 60)))
    file_org = draw(st.one_of(st.none(), st.sampled_from(ORGS)))
    app_sample = AppSample(file_org, at(-30), 5.0, at(-30)) if file_org else None
    if app_sample is None and not events:
        app_sample = AppSample(ORGS[0], at(-30), 5.0, at(-30))
    return resolve(app_sample, events)


@LAWS
@given(data=st.data(), fleet=fleets(), app=app_answers(), in_use=st.sampled_from([None, True, False]))
def test_c10_9_invariant_7_the_signals_change_ranking_and_the_in_use_refusal_only(data, fleet, app, in_use):
    """Invariant 7: with and without the signals, `evaluate` refuses the same lanes
    for the same reasons and keeps the same candidates, except C-10.3's in-use
    refusal (`desktop`), which follows the desktop account: with the signals it
    is on exactly the Claude lanes the app's org's email names, while Claude
    Code uses the desktop login (or that is unknown) and the job lacks
    `allow_desktop`; without them, on the lanes the profile and hint rule names."""
    lanes, readings, in_flight = fleet
    old_identity, hint = data.draw(identities(capacity))
    job = job_for(data.draw, lanes)
    rules = policy(2)
    without = views(capacity, lanes, readings, in_flight, old_identity, hint, in_use)
    signal = dataclasses.replace(old_identity, app=app) if old_identity is not None else \
        capacity.DesktopIdentity("unverified", app=app)
    with_signals = views(capacity, lanes, readings, in_flight, signal, hint, in_use)
    old = scheduler.evaluate(rules, without, dict(job)).evaluations[0]
    new = scheduler.evaluate(rules, with_signals, dict(job)).evaluations[0]

    def reasons(evaluation):
        found = {row["lane_id"]: set(row["reasons"]) for row in evaluation["rejections"]}
        found.update({lane: set() for lane in evaluation["candidates"]})
        return found

    before, after = reasons(old), reasons(new)
    assert set(before) == set(after)
    for lane_id in before:
        assert before[lane_id] - {"desktop"} == after[lane_id] - {"desktop"}
    rows = {lane["lane_id"]: lane for lane in lanes}
    for lane_id, found in after.items():
        lane = rows[lane_id]
        if lane["provider"] != "claude":
            # Only a Claude lane is the desktop app's; any other keeps its recorded flag either way.
            assert ("desktop" in found) == ("desktop" in before[lane_id])
            continue
        owned = app.owns(lane)
        assert ("desktop" in found) == (owned and in_use is not False and not job["allow_desktop"])
    assert {lane for lane, found in after.items() if not found} == set(new["candidates"])
