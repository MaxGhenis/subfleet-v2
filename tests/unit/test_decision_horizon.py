"""C-6.3: until `capacity.decision_horizon`, the clock alone changes no routing decision.

Admission reserves on an evaluation made before its transaction only while the
rows it rests on stand (`route_check.still_stands`) and its horizon has not
passed. The review of f48df54 found two clocks the first horizon (reading
freshness alone) missed, each able to close a lane inside the window with no
row changing: a confirmed reset-credit override that ends puts back a reading
below the floor, and a reported closure on the reserved model that ends turns
its lane's probe-gated slack into `unmeasured` (C-11.7).

The property, over generated fleets: readings fresh, stale, not yet observed,
at or past their reset (weekly and five-hour, including windows already stale
when they reset); closures ending in seconds or days, reported or
guessed, on the account, the reserved model or another; overrides ending in
seconds or days; attempts filling slots. `scheduler.evaluate` on the same rows
gives the same decision (lane, model, whether it needs a probe, every
candidate and rejection) at every instant from the view's `now` up to the
horizon, and just past the horizon one of the clocks has moved, so the
horizon is not merely early. hypothesis is not a dependency here, so the cases
are a seeded loop.

C-6.3's check reads each lane's own horizon (`capacity.lane_horizons`), never the
fleet's: the review of d04b8b3 found sixty unrelated lanes' staggered readings
keeping the fleet-wide horizon a second or two away, so a job no lane of theirs
could take was refused at every check. The second property: each lane is judged
(`scheduler.judge_lane`, for every model of its provider) the same at every
instant before its own horizon, whatever the other lanes' clocks do, and the
fleet's horizon is the earliest of the lanes' own.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import subfleet
from subfleet import capacity, scheduler
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.policy import load_policy

POLICY = load_policy(Path(subfleet.__file__).parent / "default_policy.json")
TTL = POLICY["caps"]["reading_ttl_s"]
T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
MODEL_IDS = {name: model["id"] for name, model in POLICY["models"].items()}
LANES = (("codex-1", "codex"), ("codex-2", "codex"), ("claude-a", "claude"), ("claude-b", "claude"))


def iso(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def lane(lane_id: str, provider: str) -> Lane:
    return Lane(lane_id, provider, f"{provider}:{lane_id}", Credential(provider, f"/fake/{lane_id}", "home"),
                f"/fake/{lane_id}", LaneOwner.V2, False, True, None, None)


def fleet(rng: random.Random) -> dict:
    readings, closures, attempts = [], [], []
    # A quiet fleet waits on no clock: its readings are long stale and its
    # closures over or released, so its decision has no horizon.
    quiet = rng.random() < .15
    for lane_id, provider in LANES:
        scopes = ("account", MODEL_IDS["fable"], MODEL_IDS["opus"]) if provider == "claude" else ("account",)
        for _ in range(rng.randrange(0, 4)):
            resets = rng.choice((None, None, T0 + timedelta(seconds=rng.choice((0, 2, 5, 3600, 86400))),
                                 T0 - timedelta(seconds=1)))
            readings.append({
                "reading_id": len(readings) + 1, "lane_id": lane_id, "scope": rng.choice(scopes),
                "window": rng.choice(("seven_day", "seven_day", "five_hour")),
                "utilization": rng.choice((.1, .5, .9, .96, .99, 1.0)),
                "label": rng.choice(("provider", "provider", "provider", "unknown")),
                "source": rng.choice(("oauth-usage", "rate_limit_event", "fixture")),
                "attempt_id": rng.choice((None, "a1")),
                "observed_at": iso(T0 - timedelta(seconds=300 if quiet else rng.choice(
                    (-3, -1, 0, 1, 60, 117, 119, 120, 121, 300)))),
                "resets_at": iso(resets) if resets else None})
        for _ in range(rng.randrange(0, 3)):
            scope = rng.choice(("account", *(MODEL_IDS[name] for name in ("fable", "opus", "astra"))))
            closures.append({
                "lane_id": lane_id, "scope": scope,
                "until_at": iso(T0 + timedelta(seconds=-1 if quiet else rng.choice((-1, 1, 3, 7, 3600)))),
                "reason": rng.choice(("provider-limit", "provider-limit", "usage")),
                "clock_source": rng.choice(("reported", "reported", "guessed")), "source_event": None,
                "created_at": iso(T0 - timedelta(hours=1)),
                "released_at": rng.choice((None, None, None, iso(T0 - timedelta(minutes=1))))})
    for n in range(rng.randrange(0, 4)):
        attempts.append({"attempt_id": f"busy-{n}/a1", "job_id": f"busy-{n}", "seq": 1,
                         "lane_id": rng.choice(LANES)[0], "state": "running"})
    overrides = {lane_id: T0 + timedelta(seconds=rng.choice((2, 4, 86400)))
                 for lane_id, _ in LANES if not quiet and rng.random() < .25}
    job = {"job_id": "j", "sandbox": "read-only", "exclusions": (), "kind": "dispatch"}
    if rng.random() < .7:
        job["pinned_model"] = rng.choice(("astra", "terra", "opus", "sonnet", "fable"))
    else:
        job.update(task=rng.choice(("research", "build")), tier=rng.choice(("easy", "standard", "hard")))
    return {"lanes": [lane(*spec) for spec in LANES], "readings": readings, "closures": closures,
            "attempts": attempts, "jobs": [{"job_id": a["job_id"], "kind": "dispatch"} for a in attempts],
            "overrides": overrides, "job": job}


def view_at(case: dict, instant: datetime) -> tuple[dict, list]:
    """The view `_pick` evaluates at `instant`: an override still confirmed holds
    its lane's readings out (`Actions.confirmed_override`, `confirmed + 7 days`)."""
    view = capacity.build_view(case["lanes"], [dict(row) for row in case["readings"]], case["closures"],
                               case["attempts"], case["jobs"], now=instant, reading_ttl_s=TTL)
    held = [lane_id for lane_id, end in case["overrides"].items() if end > instant]
    view["readings"] = [row for row in view["readings"] if row["lane_id"] not in held]
    return view, [case["overrides"][lane_id] for lane_id in held]


def decision(case: dict, instant: datetime) -> tuple:
    view, _ = view_at(case, instant)
    made = scheduler.evaluate(POLICY, view, case["job"])
    return (made.chosen_lane, made.chosen_model, scheduler.probe_required(made, case["job"]),
            [(row["model"], row["candidates"], row["candidate_details"],
              [(rejected["lane_id"], rejected["reasons"]) for rejected in row["rejections"]])
             for row in made.evaluations])


def clocks(case: dict, instant: datetime) -> tuple:
    """Every clock the evaluation reads, at `instant`."""
    return (tuple(capacity.fresh_provider(row, now=instant, reading_ttl_s=TTL) for row in case["readings"]),
            tuple(window_not_renewed(row, instant) for row in case["readings"]),
            tuple(not row["released_at"] and capacity._time(row["until_at"]) > instant for row in case["closures"]),
            tuple(end > instant for end in case["overrides"].values()))


def window_not_renewed(row: dict, instant: datetime) -> bool:
    """The independent C-11.3 uncertainty clock starts at observation and ends at TTL."""
    return not bool(row.get("label") in ("provider", "stale-provider")
                    and row.get("utilization") is not None and row.get("resets_at")
                    and 0 <= (instant - capacity._time(row["observed_at"])).total_seconds() <= TTL
                    and capacity._time(row["resets_at"]) <= max(instant, capacity._time(row["observed_at"])))


def test_the_clock_alone_changes_no_decision_before_its_horizon():
    reached = {"horizons": 0, "none": 0, "moved": 0}
    for seed in range(600):
        case = fleet(random.Random(seed))
        view, ends = view_at(case, T0)
        until = capacity.decision_horizon(view, reading_ttl_s=TTL, ends=ends)
        first = decision(case, T0)
        if until is None:
            reached["none"] += 1
            for later in (timedelta(seconds=1), timedelta(minutes=5), timedelta(days=3)):
                assert decision(case, T0 + later) == first, (seed, later)
            continue
        reached["horizons"] += 1
        assert until >= T0, (seed, until)
        for step in (0, .3, .7, .999):
            instant = T0 + (until - T0) * step
            assert decision(case, instant) == first, (seed, step, until)
        # Not merely early: a clock moves at the horizon or within a second of it
        # (a reading stops being fresh a second after its TTL, the rest at it).
        start = clocks(case, T0)
        assert (clocks(case, until) != start
                or clocks(case, until + timedelta(seconds=1)) != start), (seed, until)
        if decision(case, until + timedelta(seconds=1)) != first:
            reached["moved"] += 1
    # The generator reaches every branch: decisions that wait on no clock, and
    # decisions the clock changes just past their horizon.
    assert reached["none"] > 20 and reached["horizons"] > 200 and reached["moved"] > 20, reached


def test_an_override_that_ends_is_a_horizon():
    """The review's first case: codex-1's reading is at 99%, held out by a confirmed
    override; the lane is chosen while it holds and rejected once it ends."""
    case = {"lanes": [lane("codex-1", "codex")], "attempts": [], "jobs": [], "closures": [],
            "readings": [{"reading_id": 1, "lane_id": "codex-1", "scope": "account", "window": "seven_day",
                          "utilization": .99, "resets_at": iso(T0 + timedelta(days=1)), "label": "provider",
                          "source": "fixture", "observed_at": iso(T0)}],
            "overrides": {"codex-1": T0 + timedelta(seconds=3)},
            "job": {"job_id": "j", "pinned_model": "astra", "sandbox": "read-only", "exclusions": ()}}
    view, ends = view_at(case, T0)
    assert capacity.decision_horizon(view, reading_ttl_s=TTL, ends=ends) == T0 + timedelta(seconds=3)
    assert decision(case, T0)[0] == "codex-1"
    assert decision(case, T0 + timedelta(seconds=4))[0] is None


def test_a_closure_that_ends_is_a_horizon_even_when_it_closes_a_lane():
    """The review's second case: a reported provider-limit closure on the reserved
    model (Fable) gives claude-a slack behind a probe for Opus; when it ends, with
    no fresh account reading, the reserve is unmeasured and claude-a is rejected."""
    case = {"lanes": [lane("claude-a", "claude")], "attempts": [], "jobs": [], "readings": [],
            "closures": [{"lane_id": "claude-a", "scope": MODEL_IDS["fable"],
                          "until_at": iso(T0 + timedelta(seconds=3)), "reason": "provider-limit",
                          "clock_source": "reported", "source_event": None,
                          "created_at": iso(T0 - timedelta(hours=1)), "released_at": None}],
            "overrides": {},
            "job": {"job_id": "j", "pinned_model": "opus", "sandbox": "read-only", "exclusions": ()}}
    view, ends = view_at(case, T0)
    assert capacity.decision_horizon(view, reading_ttl_s=TTL, ends=ends) == T0 + timedelta(seconds=3)
    assert decision(case, T0)[0] == "claude-a"
    assert decision(case, T0 + timedelta(seconds=4))[0] is None


def test_a_reading_not_yet_observed_is_a_horizon_only_if_it_will_be_fresh():
    """A reading observed after the view's clock (another host's clock ahead of
    this one) turns fresh at its `observed_at`, and can show the lane below the
    floor; one that will not be fresh then (past its reset, or not a provider
    reading) moves nothing."""
    row = {"reading_id": 1, "lane_id": "codex-1", "scope": "account", "window": "seven_day",
           "utilization": .99, "resets_at": None, "label": "provider", "source": "fixture",
           "observed_at": iso(T0 + timedelta(seconds=2))}
    view = {"now": iso(T0), "readings": [row], "closures": []}
    assert capacity.decision_horizon(view, reading_ttl_s=TTL) == T0 + timedelta(seconds=2)
    for inert in ({**row, "label": "unknown"},):
        assert capacity.decision_horizon({**view, "readings": [inert]}, reading_ttl_s=TTL) is None


def judgements(case: dict, instant: datetime) -> dict:
    """Every lane, judged alone for every model of its provider, at `instant`."""
    view, _ = view_at(case, instant)
    setup = scheduler.prepare(POLICY, view, case["job"])
    found = {}
    for lane_row in setup["lanes"]:
        readings = [row for row in view["readings"] if row["lane_id"] == lane_row["lane_id"]]
        closures = [row for row in view["closures"] if row["lane_id"] == lane_row["lane_id"]]
        for short, model in POLICY["models"].items():
            if model["provider"] == lane_row["provider"]:
                found[(lane_row["lane_id"], short)] = scheduler.judge_lane(
                    setup, short, lane_row, readings, closures,
                    in_flight=setup["in_flight"].get(lane_row["lane_id"], 0), unavailable={})
    return found


def lane_clocks(case: dict, lane_id: str, instant: datetime) -> tuple:
    """The clocks a lane's own judgement reads, at `instant`."""
    return (tuple(capacity.fresh_provider(row, now=instant, reading_ttl_s=TTL) for row in case["readings"]
                  if row["lane_id"] == lane_id),
            tuple(window_not_renewed(row, instant) for row in case["readings"] if row["lane_id"] == lane_id),
            tuple(not row["released_at"] and capacity._time(row["until_at"]) > instant
                  for row in case["closures"] if row["lane_id"] == lane_id))


def test_the_clock_alone_changes_no_lane_before_its_own_horizon():
    reached = {"lanes": 0, "moved": 0, "others-earlier": 0}
    for seed in range(400):
        case = fleet(random.Random(seed))
        case["overrides"] = {}                    # an override's end is the check's own comparison, per lane
        view, _ = view_at(case, T0)
        horizons = capacity.lane_horizons(view, reading_ttl_s=TTL)
        assert capacity.decision_horizon(view, reading_ttl_s=TTL) == min(horizons.values(), default=None)
        first = judgements(case, T0)
        for lane_id, until in horizons.items():
            reached["lanes"] += 1
            assert until > T0 - timedelta(seconds=1), (seed, lane_id, until)
            mine = {key: value for key, value in first.items() if key[0] == lane_id}
            if min(horizons.values()) < until:
                reached["others-earlier"] += 1      # another lane's clock passes first: this one's stands
            for step in (0, .3, .7, .999):
                later = judgements(case, T0 + (until - T0) * step)
                assert {key: later[key] for key in mine} == mine, (seed, lane_id, step, until)
            # Not merely early: one of this lane's own clocks moves at its horizon or within a second of it.
            start = lane_clocks(case, lane_id, T0)
            assert (lane_clocks(case, lane_id, until) != start
                    or lane_clocks(case, lane_id, until + timedelta(seconds=1)) != start), (seed, lane_id)
            later = judgements(case, until + timedelta(seconds=1))
            if {key: later[key] for key in mine} != mine:
                reached["moved"] += 1
    assert reached["lanes"] > 300 and reached["moved"] > 50 and reached["others-earlier"] > 100, reached


def rank_keys(case: dict, instant: datetime, lane_id: str | None = None) -> dict:
    """Compare the entire returned key, including any future comparator fields."""
    view, _ = view_at(case, instant)
    setup = scheduler.prepare(POLICY, view, case["job"])
    found = {}
    for lane_row in setup["lanes"]:
        identity = lane_row["lane_id"]
        if lane_id is not None and identity != lane_id:
            continue
        readings = [row for row in view["readings"] if row["lane_id"] == identity]
        closures = [row for row in view["closures"] if row["lane_id"] == identity]
        for short, model in POLICY["models"].items():
            if model["provider"] == lane_row["provider"]:
                _, detail = scheduler.judge_lane(setup, short, lane_row, readings, closures,
                    in_flight=setup["in_flight"].get(identity, 0), unavailable={})
                found[(identity, short)] = scheduler.rank_key(setup, short, identity, detail)
    return found


def test_every_rank_key_is_constant_before_its_lanes_horizon():
    for seed in range(600):
        case = fleet(random.Random(seed))
        case["overrides"] = {}
        view, _ = view_at(case, T0)
        horizons = capacity.lane_horizons(view, reading_ttl_s=TTL)
        first = rank_keys(case, T0)
        for lane_id, _ in LANES:
            mine = {key: value for key, value in first.items() if key[0] == lane_id}
            until = horizons.get(lane_id)
            instants = ([T0 + (until - T0) * step for step in (0, .3, .7, .999)] if until else
                        [T0 + delta for delta in (timedelta(seconds=1), timedelta(minutes=5), timedelta(days=3))])
            for instant in instants:
                later = rank_keys(case, instant, lane_id)
                assert {key: later[key] for key in mine} == mine, (seed, lane_id, instant, until)


def test_weekly_and_five_hour_resets_bound_only_recent_ranking():
    for window in ("seven_day", "five_hour"):
        for age in (0, TTL + 1):
            row = {"lane_id": "codex-1", "scope": "account", "window": window,
                   "utilization": .9, "label": "provider", "observed_at": iso(T0 - timedelta(seconds=age)),
                   "resets_at": iso(T0 + timedelta(seconds=2))}
            case = {"lanes": [lane("codex-1", "codex")], "readings": [row,
                    {**row, "window": "seven_day", "scope": MODEL_IDS["astra"], "utilization": .2,
                     "observed_at": iso(T0), "resets_at": iso(T0 + timedelta(days=1))}],
                    "closures": [], "attempts": [], "jobs": [], "overrides": {},
                    "job": {"pinned_model": "astra", "sandbox": "read-only"}}
            view, _ = view_at(case, T0)
            until = T0 + timedelta(seconds=2 if age == 0 else TTL)
            assert capacity.lane_horizons(view, reading_ttl_s=TTL) == {"codex-1": until}
            assert capacity.decision_horizon(view, reading_ttl_s=TTL) == until
            assert rank_keys(case, until - timedelta(microseconds=1)) == rank_keys(case, T0)
            if age == 0:
                assert rank_keys(case, until) != rank_keys(case, T0)
            else:
                assert rank_keys(case, T0 + timedelta(seconds=3)) == rank_keys(case, T0)
