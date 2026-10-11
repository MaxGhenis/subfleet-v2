"""C-23.16 as properties: generated fleets, queues, holds, markers, restarts and clocks.

Each example is a small world: Codex lanes in one of seven states, each with or
without a banked gift; waiting jobs, each with its own route (the lanes its
exclusions leave it); and a run of steps, each moving the clock, perhaps changing
the world (a reset lane used up again, a restart, the no-reset marker put down or
lifted), then asking the real `ResetCredits.evaluate` for a spend, automatic or
for an operator's lane. Demand is either the real one (`scheduler.evaluate` on a
view of the store, judged by `demand_verdict`, as `Daemon._reset_demand` does) or
adversarial (any job naming any lanes as limited), because the guards on holds,
the marker, the switch, the interval and one-at-a-time must hold whatever the
demand says.

The invariants are judged against the world's own model, never against the code
under test:

- at most one action per evaluation (C-23.16 (c));
- none with the switch off (f) or the marker down (f), and then no HTTP at all;
- none without a job demand names as limited on that very lane (a), (c);
- with real demand, none while a lane of that job's route has room: open,
  unmeasured, busy, or reset and not used up again (b);
- never on a lane under an operator hold, auth-dead, or disabled (e);
- none while a lane reset in the last week still has room (d);
- none within `min_interval_min` of the last (d);
- an operator's spend is on the lane it names and no other (e);
- a restart before every step changes no decision (d);
- with the switch off, every decision is release/217's: the same refusal, no HTTP and no
  action, the same C-23.17 overrides, and the same usage reconciliations (differential,
  against release/217's own `actions.py`, kept as `tests/unit/reset_credits_release217.py`).

The mutation checks at the end show the properties are not vacuous: each mutant
of rules 2, 4 and 5 is caught by a generated counterexample.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, find, given, settings, strategies as st

from subfleet import actions as actions_module
from subfleet.actions import ResetCredits, demand_verdict
from subfleet.adapters.codex import CodexAdapter, WHAM_RESET_CREDITS_CONSUME_URL, WHAM_RESET_CREDITS_URL
from subfleet.capacity import from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate as route
from subfleet.store import Store
from tests.caps import capped
from tests.unit import reset_credits_release217 as release217

START = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)
STATES = ("limited", "open", "unmeasured", "busy", "held", "auth-dead", "disabled")
LIMITED_KINDS = frozenset({"limited", "held", "auth-dead", "disabled"})
HELD_KINDS = frozenset({"held", "auth-dead", "disabled"})
ROOM_KINDS = frozenset({"open", "unmeasured", "busy"})
ASTRA = "gpt-6-astra"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _route_policy() -> dict:
    """The shipped policy with the slot caps a busy lane needs and no reserve (as `test_scheduler`)."""
    loaded = capped(load_policy(DEFAULT_POLICY_PATH))
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}
    return loaded


ROUTE_POLICY = _route_policy()


# --- the world ---------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    minutes: int
    change: str            # none | used-up | restart | marker-on | marker-off
    target: int | None     # an operator's lane number, or None for the timer


@dataclass(frozen=True)
class World:
    lanes: tuple[tuple[str, bool, int, bool], ...]   # state, gift, days to the weekly reset, hold row
    jobs: tuple[frozenset[int], ...]                  # each job's excluded lane numbers
    steps: tuple[Step, ...]
    enabled: bool
    interval: int                                     # min_interval_min
    adversarial: tuple[tuple[int, tuple[int, ...]], ...] | None   # (job, lanes it names) or real demand


@st.composite
def worlds(draw, *, lane_states=st.sampled_from(STATES), max_lanes=5, max_steps=5, enabled=None,
           adversarial=None, changes=st.sampled_from(("none", "none", "used-up", "restart", "marker-on", "marker-off"))):
    count = draw(st.integers(1, max_lanes))
    lanes = tuple((draw(lane_states), draw(st.booleans()), draw(st.integers(1, 6)), draw(st.booleans()))
                  for _ in range(count))
    numbers = list(range(1, count + 1))
    jobs = tuple(frozenset(draw(st.lists(st.sampled_from(numbers), unique=True, max_size=count - 1)))
                 for _ in range(draw(st.integers(0, 3))))
    steps = tuple(Step(draw(st.sampled_from((0, 1, 10, 29, 31, 120))), draw(changes),
                       draw(st.one_of(st.none(), st.none(), st.sampled_from(numbers))))
                  for _ in range(draw(st.integers(1, max_steps))))
    switch = draw(st.booleans() if enabled is None else st.just(enabled))
    interval = draw(st.sampled_from((0, 30)))
    lying = draw(st.booleans()) if adversarial is None else adversarial
    named = (tuple((draw(st.integers(0, max(0, len(jobs) - 1))),
                    tuple(draw(st.lists(st.sampled_from(numbers), unique=True, min_size=1))))
                   for _ in range(draw(st.integers(1, 3)))) if lying and jobs else None)
    return World(lanes, jobs, steps, switch, interval, named)


class Wham:
    """The two reset-credit endpoints over the world's gifts; never the network."""

    def __init__(self, gifts: dict[str, bool]):
        self.gifts, self.calls = dict(gifts), []
        self._lock = threading.Lock()

    def __call__(self, request, timeout):
        account = request.get_header("Chatgpt-account-id")
        with self._lock:
            self.calls.append((request.get_method(), account))
        if request.full_url == WHAM_RESET_CREDITS_URL:
            credits = [{"id": f"gift-{account}", "status": "available", "reset_type": "codex_rate_limits"}] \
                if self.gifts.get(account) else []
            return 200, json.dumps({"credits": credits}).encode()
        assert request.full_url == WHAM_RESET_CREDITS_CONSUME_URL and request.get_method() == "POST"
        assert self.gifts.get(account), "a consume of a gift that is not there"
        self.gifts[account] = False
        return 200, b'{"code":"reset","windows_reset":2}'


@dataclass
class Model:
    """What the world is, by construction: the oracle the invariants are judged against."""

    kinds: dict[int, str]
    routes: list[frozenset[int]]
    reset_at: dict[int, datetime] = field(default_factory=dict)
    used_up: set[int] = field(default_factory=set)
    last_spend: datetime | None = None
    marker: bool = False

    def limited(self, number: int) -> bool:
        if number in self.reset_at:
            return number in self.used_up
        return self.kinds[number] in LIMITED_KINDS

    def held(self, number: int) -> bool:
        return self.kinds[number] in HELD_KINDS

    def room(self, number: int) -> bool:
        """Room for a job: open, unmeasured, busy, or reset and not used up again (never a held lane)."""
        if self.held(number):
            return False
        if number in self.reset_at:
            return number not in self.used_up
        return self.kinds[number] in ROOM_KINDS

    def reset_lane_with_room(self, now: datetime) -> bool:
        return any(now - when < timedelta(days=7) and self.room(number) for number, when in self.reset_at.items())


def _lane_id(number: int) -> str:
    return f"codex-{number}"


def _build(world: World, root: Path) -> tuple[Store, Wham, Model]:
    store = Store(root / "state.db")
    store.__enter__()
    kinds = {}
    for number, (kind, gift, days, hold_row) in enumerate(world.lanes, 1):
        kinds[number] = kind
        home = root / f"codex-{number}"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "test-only", "account_id": str(number)}}))
        lane = Lane(_lane_id(number), "codex", f"codex:account-{number}", Credential("codex", str(home), "home"),
                    str(home), LaneOwner.V2, False)
        store.put_lane(lane)
        until = _iso(START + timedelta(days=days))
        if kind in LIMITED_KINDS:
            store.put_closure(Closure(lane.lane_id, "account", until, ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
            store.add_reading(Reading(lane.lane_id, "account", "seven_day", 1., until, ReadingLabel.PROVIDER,
                                      "fixture", _iso(START)))
        if kind == "open":
            store.add_reading(Reading(lane.lane_id, "account", "seven_day", .4, until, ReadingLabel.PROVIDER,
                                      "fixture", _iso(START)))
        if kind == "held":
            # `lanes hold`: its event, and a row only when it ends after the open limit (Store.put_closure).
            hold_until = _iso(START + timedelta(days=days + (1 if hold_row else 0), hours=-1 if not hold_row else 0))
            with store.transaction("lane.held", lane_id=lane.lane_id, data={"until": hold_until}):
                store.add_closure(Closure(lane.lane_id, "account", hold_until, ClosureReason.OPERATOR_HOLD,
                                          ClockSource.REPORTED, "operator"))
        if kind == "auth-dead":
            store.add_closure(Closure(lane.lane_id, ASTRA, until, ClosureReason.AUTH_DEAD, ClockSource.GUESSED,
                                      "attempt"))
        if kind == "disabled":
            store.update_lane(lane.lane_id, enabled=0)
        if kind == "busy":
            filler = f"filler-{number}"
            store.add_job(job_id=filler, request_id=filler, payload_digest="digest", kind="dispatch",
                          workdir="/work", prompt_path="/prompt", sandbox="read-only", state="running",
                          started_at=_iso(START))
            store.add_attempt(attempt_id=filler + "/a1", job_id=filler, seq=1, lane_id=lane.lane_id,
                              model_requested=ASTRA, state="running", reserved_at=_iso(START))
    for index, excluded in enumerate(world.jobs):
        job_id = f"job-{index}"
        store.add_job(job_id=job_id, request_id=job_id, payload_digest="digest", kind="dispatch", workdir="/work",
                      prompt_path="/prompt", sandbox="read-only", tier="hard", pinned_model="astra",
                      exclusions=json.dumps(sorted(_lane_id(n) for n in excluded)), state="waiting",
                      wait_reason="capacity", next_check_at=_iso(START), created_at=_iso(START + timedelta(seconds=index)))
    routes = [frozenset(set(kinds) - excluded) for excluded in world.jobs]
    gifts = {str(number): gift for number, (_, gift, _, _) in enumerate(world.lanes, 1)}
    return store, Wham(gifts), Model(kinds, routes)


def _component(store: Store, world: World, http: Wham, root: Path) -> ResetCredits:
    policy = {"reset_credits": {"enabled": world.enabled, "min_interval_min": world.interval},
              "caps": {"reading_ttl_s": 120}}
    return ResetCredits(store, policy, lambda lane: CodexAdapter(opener=http, now=lambda: START),
                        inhibit=root / "no-reset")


def _snapshot(store: Store, model: Model, now: datetime) -> dict:
    """The timer's snapshot: the store's view, each lane with a fresh usage read of what it is."""
    view = from_store(store, now=now)
    for row in view["lanes"]:
        number = int(row["lane_id"].rsplit("-", 1)[1])
        limited = model.limited(number)
        row["probe"] = {"status": "limited" if limited else "ok", "limit_reached": limited,
                        "checked_at": _iso(now), "account_key": row["account_key"]}
    return view


def _real_demand(store: Store, resets: ResetCredits, now: datetime) -> list[dict]:
    """`Daemon._reset_demand` without the daemon: each waiting job's route on the store's view."""
    view = from_store(store, now=now)
    held_out = {row["lane_id"] for row in view["lanes"] if resets.confirmed_override(row["lane_id"], now=now)}
    view["readings"] = [row for row in view["readings"] if row["lane_id"] not in held_out]
    demand = []
    for job in store.query("SELECT * FROM jobs WHERE state='waiting' AND wait_reason='capacity' "
                           "AND cancel_requested_at IS NULL ORDER BY created_at,rowid"):
        spec = {"job_id": job["job_id"], "task": None, "tier": "hard", "pinned_model": "astra",
                "sandbox": "read-only", "exclusions": json.loads(job["exclusions"] or "[]")}
        demand.append({"job_id": job["job_id"], "tier": "hard",
                       **actions_module.demand_verdict(route(ROUTE_POLICY, view, spec))})
    return demand


def _adversarial(world: World) -> list[dict]:
    return [{"job_id": f"job-{job}", "tier": "hard", "verdict": "codex-demand", "capacity_lanes": [],
             "limited_lanes": [_lane_id(number) for number in lanes]} for job, lanes in world.adversarial]


def play(world: World, *, restart_every_step: bool = False) -> tuple[list[str], list[tuple]]:
    """Run the world. Returns the invariant violations, and each step's decision."""
    violations, decisions = [], []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store, http, model = _build(world, root)
        try:
            resets = _component(store, world, http, root)
            now = START
            for index, step in enumerate(world.steps):
                now += timedelta(minutes=step.minutes)
                if step.change == "restart" or restart_every_step:
                    resets = _component(store, world, http, root)        # a new daemon: nothing in memory
                if step.change == "used-up":
                    spent = sorted(number for number in model.reset_at if number not in model.used_up)
                    if spent:
                        model.used_up.add(spent[0])
                        store.put_closure(Closure(_lane_id(spent[0]), "account", _iso(now + timedelta(days=6)),
                                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "attempt"))
                elif step.change in ("marker-on", "marker-off"):
                    model.marker = step.change == "marker-on"
                    marker = root / "no-reset"
                    if model.marker:
                        marker.write_text("{}")
                    elif marker.exists():
                        marker.unlink()
                demand = _adversarial(world) if world.adversarial is not None else _real_demand(store, resets, now)
                before_calls, before = len(http.calls), {row["action_id"] for row in store.query(
                    "SELECT action_id FROM actions")}
                target = _lane_id(step.target) if step.target is not None else None
                result = resets.evaluate(_snapshot(store, model, now), now=now, demand=demand, target_lane_id=target,
                                         clock=lambda now=now: now)
                calls = http.calls[before_calls:]
                new = [row for row in store.query("SELECT * FROM actions") if row["action_id"] not in before]
                decisions.append((result["status"], result.get("lane_id"), result.get("job_id")))
                where = f"step {index} at +{(now - START).total_seconds() / 60:g} min: {result['status']}"
                if len(new) > 1:
                    violations.append(f"{where}: {len(new)} actions in one evaluation")
                if (model.marker or not world.enabled) and (calls or new):
                    violations.append(f"{where}: marker {model.marker}, switch {world.enabled}, yet {calls} {len(new)}")
                for action in new:
                    request = json.loads(action["request_json"])
                    number = int(request["lane_id"].rsplit("-", 1)[1])
                    named = {item["job_id"]: item for item in demand if item.get("verdict") == "codex-demand"}
                    job = named.get(request.get("job_id"))
                    if job is None or request["lane_id"] not in job.get("limited_lanes", ()):
                        violations.append(f"{where}: spent on {request['lane_id']} for no job naming it")
                    if world.adversarial is None and job is not None:
                        route_lanes = model.routes[int(job["job_id"].split("-")[1])]
                        if number not in route_lanes:
                            violations.append(f"{where}: {request['lane_id']} is not on {job['job_id']}'s route")
                        roomy = sorted(n for n in route_lanes if model.room(n))
                        if roomy:
                            violations.append(f"{where}: spent for {job['job_id']} while its lanes {roomy} have room")
                    if model.held(number):
                        violations.append(f"{where}: spent on held lane {request['lane_id']} ({model.kinds[number]})")
                    if model.reset_lane_with_room(now):
                        violations.append(f"{where}: spent while a lane reset this week has room")
                    if model.last_spend is not None and (now - model.last_spend) < timedelta(minutes=world.interval):
                        violations.append(f"{where}: spent {now - model.last_spend} after the last")
                    if target is not None and request["lane_id"] != target:
                        violations.append(f"{where}: operator named {target}, spent on {request['lane_id']}")
                    if action["state"] == "confirmed":
                        model.reset_at[number] = now
                    model.last_spend = now
        finally:
            store.__exit__(None, None, None)
    return violations, decisions


PROPERTY = settings(max_examples=int(os.environ.get("SUBFLEET_RESET_EXAMPLES", "100")), deadline=None,
                    database=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


@PROPERTY
@given(worlds())
def test_every_rule_holds_over_generated_worlds(world):
    """C-23.16 (a)-(f): no generated world, real or lying demand, breaks an invariant."""
    violations, _ = play(world)
    assert not violations, violations


@PROPERTY
@given(worlds(lane_states=st.sampled_from(("limited", "limited", "busy", "open", "held")), enabled=True,
              adversarial=False))
def test_real_demand_spends_only_when_no_route_lane_has_room(world):
    """C-23.16 (a), (b): biased to busy and limited fleets with the switch on and the real demand."""
    violations, _ = play(world)
    assert not violations, violations


@PROPERTY
@given(worlds(enabled=True, changes=st.sampled_from(("none", "used-up", "used-up"))))
def test_a_restart_before_every_step_changes_no_decision(world):
    """C-23.16 (d): the interval and one-at-a-time are read from the store, so a daemon restarted
    before every evaluation decides exactly as one that never restarted."""
    _, steady = play(world)
    violations, restarted = play(world, restart_every_step=True)
    assert not violations, violations
    assert restarted == [(status, lane, job) for status, lane, job in steady]


def test_a_world_where_spending_is_allowed_does_spend():
    """The properties are not satisfied by spending nothing: an allowed spend happens, once."""
    world = World(lanes=(("limited", True, 6, False), ("limited", True, 2, False)), jobs=(frozenset(),),
                  steps=(Step(0, "none", None), Step(10, "used-up", None), Step(25, "none", None),
                         Step(31, "none", None)),
                  enabled=True, interval=30, adversarial=None)
    violations, decisions = play(world)
    assert not violations
    assert [status for status, _, _ in decisions] == ["confirmed", "interval-blocked", "confirmed",
                                                      "reset-lane-open"]
    assert [lane for status, lane, _ in decisions if status == "confirmed"] == ["codex-1", "codex-2"]
    assert {job for status, _, job in decisions if status == "confirmed"} == {"job-0"}


# --- differential: with the switch off, release/217's decisions -------------------

histories = st.lists(st.tuples(st.integers(1, 5), st.sampled_from(("confirmed", "unknown", "failed")),
                               st.integers(0, 60 * 24 * 9), st.booleans()), max_size=4)


def _seed(store: Store, world: World, history) -> None:
    """Reset-credit actions already on record, as v1's import or an earlier daemon left them."""
    for index, (number, state, minutes, by_subject) in enumerate(history):
        if number > len(world.lanes):
            continue
        at = _iso(START - timedelta(minutes=minutes))
        request = {} if by_subject else {"lane_id": _lane_id(number), "account_key": f"codex:account-{number}"}
        store.add_action(action_id=f"earlier-{index}", kind="reset-credit",
                         op_key=f"codex:account-{number}:gift-earlier-{index}", subject=_lane_id(number),
                         state=state, request_json=json.dumps(request), created_at=at, updated_at=at)


@PROPERTY
@given(worlds(enabled=False), histories)
def test_with_the_switch_off_every_decision_is_release_217s(world, history):
    """C-23.16 (f): `reset_credits.enabled: false` decides exactly as release/217 did, timer and operator."""
    with tempfile.TemporaryDirectory() as left_dir, tempfile.TemporaryDirectory() as right_dir:
        left_root, right_root = Path(left_dir), Path(right_dir)
        left, left_http, model = _build(world, left_root)
        right, right_http, _ = _build(world, right_root)
        try:
            _seed(left, world, history)
            _seed(right, world, history)
            policy = {"reset_credits": {"enabled": False, "headroom_floor_pct": 15, "min_interval_min": world.interval},
                      "caps": {"reading_ttl_s": 120}}
            today = release217.ResetCredits(left, policy, lambda lane: CodexAdapter(opener=left_http))
            ours = ResetCredits(right, policy, lambda lane: CodexAdapter(opener=right_http), inhibit=right_root / "no-reset")
            now = START
            for step in world.steps:
                now += timedelta(minutes=step.minutes)
                target = _lane_id(step.target) if step.target is not None else None
                then = today.evaluate(_snapshot(left, model, now), now=now, target_lane_id=target)
                demand = _adversarial(world) if world.adversarial is not None else _real_demand(right, ours, now)
                mine = ours.evaluate(_snapshot(right, model, now), now=now, target_lane_id=target, demand=demand)
                assert then["status"] == mine["status"] == "disabled"
                assert then["fleet_credits_remaining"] == mine["fleet_credits_remaining"]
                for number in range(1, len(world.lanes) + 1):
                    assert (today.confirmed_override(_lane_id(number), now=now)
                            == ours.confirmed_override(_lane_id(number), now=now))
            assert not left_http.calls and not right_http.calls
            assert left.query("SELECT * FROM actions ORDER BY action_id") == right.query(
                "SELECT * FROM actions ORDER BY action_id")
            # A usage read that finds a lane open settles what it settled before, and releases the same closures.
            for number in range(1, len(world.lanes) + 1):
                read = {"status": "ok", "limit_reached": False, "checked_at": _iso(now)}
                was = today.settle_by_usage(_lane_id(number), read, now=now)
                is_ = ours.settle_by_usage(_lane_id(number), read, now=now)
                assert (was is None) == (is_ is None)
                if was is not None:
                    assert {key: value for key, value in is_.items() if key != "reconciled_at"} == was
            closures = "SELECT lane_id,scope,reason,until_at,released_at FROM closures ORDER BY closure_id"
            assert left.query(closures) == right.query(closures)
            jobs = "SELECT job_id,state,wait_reason,next_check_at FROM jobs ORDER BY job_id"
            assert left.query(jobs) == right.query(jobs)
        finally:
            left.__exit__(None, None, None)
            right.__exit__(None, None, None)


# --- mutation checks: rules 2, 4 and 5 ------------------------------------------

SEARCH = settings(max_examples=int(os.environ.get("SUBFLEET_RESET_MUTANT_EXAMPLES", "300")), deadline=None,
                  database=None, phases=[Phase.generate],
                  suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def _caught(strategy) -> World:
    """A generated world the current (mutated) code breaks an invariant in, or a failure."""
    return find(strategy, lambda world: bool(play(world)[0]), settings=SEARCH)


def test_mutant_rule_2_a_busy_lane_is_no_room_is_caught(monkeypatch):
    """Rule 2 mutant: a lane refused only for want of a slot counts as limited, not room."""
    original = actions_module.route_lane_state

    def busy_is_limited(rejection, closures=()):
        reasons = [str(reason) for reason in (rejection.get("reasons") or [rejection.get("reason")]) if reason]
        if reasons and set(reasons) == {"no-slot"}:
            return actions_module.LIMITED
        return original(rejection, closures)
    monkeypatch.setattr(actions_module, "route_lane_state", busy_is_limited)
    world = _caught(worlds(lane_states=st.sampled_from(("limited", "busy")), enabled=True, adversarial=False,
                           changes=st.just("none")))
    assert any(kind == "busy" for kind, _, _, _ in world.lanes)


def test_mutant_rule_4_holds_ignored_is_caught(monkeypatch):
    """Rule 4 mutant: no lane is ever judged held."""
    monkeypatch.setattr(actions_module, "lane_hold", lambda row, *, now, held=None: None)
    monkeypatch.setattr(actions_module, "store_holds", lambda store, *, now: {})
    world = _caught(worlds(lane_states=st.sampled_from(("held", "auth-dead", "disabled", "limited")), enabled=True,
                           adversarial=True, changes=st.just("none")))
    assert any(kind in HELD_KINDS for kind, _, _, _ in world.lanes)


def test_mutant_rule_5_one_at_a_time_dropped_is_caught(monkeypatch):
    """Rule 5 mutant: a lane reset this week with room no longer holds the next spend back."""
    monkeypatch.setattr(ResetCredits, "reset_lanes_open", lambda self, rows, *, now, held=None: [])
    world = _caught(worlds(lane_states=st.just("limited"), enabled=True, changes=st.just("none")))
    assert len(world.steps) > 1


def test_mutant_rule_5_interval_dropped_is_caught(monkeypatch):
    """Rule 5 mutant: the minimum interval is not read."""
    original = ResetCredits._gate

    def no_interval(self, history, reconciled, instant, settings):
        return original(self, history, reconciled, instant, {**settings, "min_interval_min": 0})
    monkeypatch.setattr(ResetCredits, "_gate", no_interval)
    world = _caught(worlds(lane_states=st.just("limited"), enabled=True,
                           changes=st.sampled_from(("used-up", "none"))))
    assert world.interval == 30


def test_mutant_rule_5_interval_from_memory_is_caught(monkeypatch):
    """Rule 5 mutant: the interval is measured from what this component spent, not from the
    store's action rows, so a restart forgets it."""
    gate, evaluate = ResetCredits._gate, ResetCredits.evaluate

    def remembered(self, history, reconciled, instant, settings):
        status = gate(self, history, reconciled, instant, {**settings, "min_interval_min": 0})
        mine = self.__dict__.get("_spent")
        if status is None and mine is not None and (instant - mine).total_seconds() < settings["min_interval_min"] * 60:
            return "interval-blocked"
        return status

    def recording(self, snapshot, **kwargs):
        result = evaluate(self, snapshot, **kwargs)
        if result.get("status") == "confirmed":
            self._spent = actions_module._time(kwargs["now"])
        return result
    monkeypatch.setattr(ResetCredits, "_gate", remembered)
    monkeypatch.setattr(ResetCredits, "evaluate", recording)
    world = _caught(worlds(lane_states=st.just("limited"), enabled=True,
                           changes=st.sampled_from(("used-up", "restart", "used-up"))))
    assert any(step.change == "restart" for step in world.steps)
