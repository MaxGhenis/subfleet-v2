"""C-10.3 (2026-09-30): the desktop login is a lane by default, behind a reserve.

Max, on 2026-09-30, with every other Opus lane out and jobs queued beside an idle
desktop lane: "why wouldnt we allow using the active acct?" The lane is now a
candidate whether or not Claude Code is using the login. It still sorts last, and it
keeps a reserve for interactive sessions: detached work is refused it while a window's
counted reading is at or above `1 - admission.desktop_reserve`, and while
`admission.desktop_max_in_flight` detached attempts run there. A turn is never refused
by either. `--no-desktop` (`-x @desktop`) keeps a job off it.
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import capacity, scheduler
from subfleet.capacity import build_view, desktop_reserve_readings, desktop_reserved, lane_horizons, open_lanes
from subfleet.contracts import DESKTOP_EXCLUSION
from subfleet.policy import DEFAULT_POLICY_PATH, admission_settings, load_policy
from subfleet.scheduler import evaluate

NOW = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    return (NOW + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def policy():
    loaded = load_policy(DEFAULT_POLICY_PATH)
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}      # C-11.7 off: these are C-10.3's cases
    return loaded


def lane(identity, **changes):
    provider = identity.split("-")[0]
    return {"lane_id": identity, "provider": provider, "account_key": f"{provider}:{identity}@example.com",
            "owner": "v2", "enabled": True, "desktop": False, "credential_kind": "keychain-token", **changes}


def reading(identity, window, utilization, *, observed=-30, resets=3600, label="provider", reading_id=1):
    return {"reading_id": reading_id, "lane_id": identity, "scope": "account", "window": window,
            "utilization": utilization, "resets_at": None if resets is None else at(resets),
            "observed_at": at(observed), "label": label, "source": "rate_limit_event"}


def attempt(identity, n, *, job_id=None):
    return {"attempt_id": f"a-{identity}-{n}", "job_id": job_id or f"job-{identity}-{n}", "lane_id": identity,
            "state": "running"}


def view(lanes, readings=(), attempts=(), jobs=(), *, in_use=True):
    return build_view(lanes, readings, (), attempts, jobs, now=NOW, desktop_in_use=in_use)


def job(**changes):
    return {"job_id": "j", "kind": "dispatch", "pinned_model": "opus", "sandbox": "read-only", **changes}


DESK = "claude-9"


def desk_only(readings=(), attempts=(), jobs=(), **options):
    return view([lane("claude-1", enabled=False), lane(DESK, desktop=True)], readings, attempts, jobs, **options)


def refused(decision, lane_id=DESK):
    return next(row["reasons"] for row in decision.evaluations[0]["rejections"] if row["lane_id"] == lane_id)


# --- a lane whatever Claude Code is doing ---------------------------------------------------

@pytest.mark.parametrize("in_use", [True, False, None])
def test_c10_3_the_desktop_login_is_a_candidate_whether_or_not_claude_code_uses_it(policy, in_use):
    """C-10.3: in use, idle, or unknown (a view built without the signal): the job runs on the desktop lane."""
    assert evaluate(policy, desk_only(in_use=in_use), job()).chosen_lane == DESK


def test_c10_3_c11_3_the_desktop_lane_still_sorts_after_every_other_candidate(policy):
    """C-11.3: with another lane open the desktop login is not taken, however much better it measures."""
    snapshot = view([lane("claude-1"), lane(DESK, desktop=True)],
                    [reading("claude-1", "five_hour", .6), reading(DESK, "five_hour", .0, reading_id=2)])
    decision = evaluate(policy, snapshot, job())
    assert decision.evaluations[0]["candidates"] == ["claude-1", DESK]


def test_c10_3_allow_desktop_is_accepted_and_changes_nothing(policy):
    """C-10.3: `--allow-desktop` is kept for its callers; it neither admits nor passes the reserve."""
    busy = desk_only(attempts=[attempt(DESK, 1), attempt(DESK, 2)])
    for spec in (job(), job(allow_desktop=True)):
        assert evaluate(policy, desk_only(), spec).chosen_lane == DESK
        assert refused(evaluate(policy, busy, spec)) == ["desktop-reserve:in-flight"]


# --- --no-desktop ----------------------------------------------------------------------------

def test_c10_3_no_desktop_keeps_a_job_off_whichever_lane_is_the_desktop_login(policy):
    """C-10.3: `@desktop` names the lane marked desktop now, and no other: the job runs on claude-1."""
    snapshot = view([lane("claude-1"), lane(DESK, desktop=True)])
    decision = evaluate(policy, snapshot, job(exclusions=[DESKTOP_EXCLUSION]))
    assert decision.chosen_lane == "claude-1"
    assert refused(decision) == ["excluded"]
    moved = view([lane("claude-1", desktop=True), lane(DESK)])
    assert evaluate(policy, moved, job(exclusions=[DESKTOP_EXCLUSION])).chosen_lane == DESK


def test_c10_3_no_lane_identity_can_be_the_desktop_token(policy):
    """C-10.3: a lane labelled like the token is not excluded by it unless it is the desktop login."""
    snapshot = view([lane("claude-1", label=DESKTOP_EXCLUSION, account_key="claude:@desktop")])
    assert evaluate(policy, snapshot, job(exclusions=[DESKTOP_EXCLUSION])).chosen_lane == "claude-1" or \
        refused(evaluate(policy, snapshot, job(exclusions=[DESKTOP_EXCLUSION])), "claude-1") == ["excluded"]
    assert scheduler.job_refusals(scheduler.prepare(policy, snapshot, job(exclusions=[DESKTOP_EXCLUSION])), "opus",
                                  {**snapshot["lanes"][0], "label": None, "account_key": "claude:x"}) == []


# --- the reserve: readings --------------------------------------------------------------------

@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
@pytest.mark.parametrize("utilization,refuses", [(.69, False), (.7, True), (.84, True)])     # .85: the floor too
def test_c10_3_a_window_at_or_above_its_ceiling_refuses_detached_work(policy, window, utilization, refuses):
    """C-10.3: the default reserve keeps 0.3 of each window: a reading at 0.7 or more refuses, 0.69 does not."""
    decision = evaluate(policy, desk_only([reading(DESK, window, utilization)]), job())
    if refuses:
        assert decision.chosen_lane is None and refused(decision) == [f"desktop-reserve:{window}"]
        detail = next(row for row in decision.evaluations[0]["rejections"] if row["lane_id"] == DESK)
        assert detail["desktop_reserve"]["windows"][window] == {
            "utilization": utilization, "ceiling": .7, "observed_at": at(-30), "resets_at": at(3600)}
    else:
        assert decision.chosen_lane == DESK


def test_c10_3_a_reading_past_its_freshness_counts_until_its_window_resets(policy):
    """C-10.3: two hours old (reading_ttl_s is 120) and labelled stale, a 0.8 five-hour reading still refuses."""
    stale = reading(DESK, "five_hour", .8, observed=-7200, resets=600)
    snapshot = desk_only([stale])
    assert snapshot["readings"][0]["label"] == "stale-provider"
    assert refused(evaluate(policy, snapshot, job())) == ["desktop-reserve:five_hour"]


@pytest.mark.parametrize("row", [
    reading(DESK, "five_hour", .8, resets=-1),                          # its window has reset
    reading(DESK, "five_hour", .8, observed=-7200, resets=None),        # no reset time, and not fresh
    reading(DESK, "five_hour", .8, label="admission-observed"),         # not a provider reading
    reading(DESK, "admission", .8),                                     # not an account window
    {**reading(DESK, "five_hour", .8), "scope": "claude-opus-5-5"},     # not the account's window
    reading(DESK, "five_hour", .8, observed=60),                        # observed after now
])
def test_c10_3_what_the_reserve_does_not_count(policy, row):
    """C-10.3: none of these is a counted reading, so none refuses the lane."""
    assert evaluate(policy, desk_only([row]), job()).chosen_lane == DESK
    assert desktop_reserve_readings([row], now=NOW) == {}


def test_c10_3_a_fresh_reading_without_a_reset_counts_while_fresh(policy):
    """C-10.3: a reading with no `resets_at` counts for `reading_ttl_s`, as every fresh reading does."""
    assert refused(evaluate(policy, desk_only([reading(DESK, "seven_day", .8, resets=None)]), job())) == [
        "desktop-reserve:seven_day"]


def test_c10_3_the_newest_counted_reading_decides(policy):
    """C-10.3: a newer, lower reading in the same window replaces an older, higher one."""
    rows = [reading(DESK, "five_hour", .9, observed=-300, reading_id=1),
            reading(DESK, "five_hour", .2, observed=-30, reading_id=2)]
    assert desktop_reserve_readings(rows, now=NOW)["five_hour"]["utilization"] == .2
    assert evaluate(policy, desk_only(rows), job()).chosen_lane == DESK


def test_c10_3_the_reserve_is_policy(policy):
    """C-10.3: null for a window keeps none of it; a larger reserve lowers the ceiling."""
    row = reading(DESK, "five_hour", .6)
    assert evaluate(policy, desk_only([row]), job()).chosen_lane == DESK
    policy["admission"]["desktop_reserve"] = {"five_hour": .5, "seven_day": .3}
    assert refused(evaluate(policy, desk_only([row]), job())) == ["desktop-reserve:five_hour"]
    policy["admission"]["desktop_reserve"] = {"five_hour": None, "seven_day": .3}
    assert evaluate(policy, desk_only([reading(DESK, "five_hour", .84)]), job()).chosen_lane == DESK
    policy["admission"]["desktop_reserve"] = None
    assert evaluate(policy, desk_only([reading(DESK, "seven_day", .84)]), job()).chosen_lane == DESK


# --- the reserve: attempts in flight -----------------------------------------------------------

def test_c10_3_the_desktop_lane_takes_two_detached_attempts_at_once_by_default(policy):
    """C-10.3: readings arrive only as attempts end, so the lane takes `desktop_max_in_flight` (2) at a time."""
    assert evaluate(policy, desk_only(attempts=[attempt(DESK, 1)]), job()).chosen_lane == DESK
    decision = evaluate(policy, desk_only(attempts=[attempt(DESK, 1), attempt(DESK, 2)]), job())
    assert refused(decision) == ["desktop-reserve:in-flight"]
    assert scheduler.dominant_rejection(decision) == "desktop-reserve:in-flight"


def test_c10_3_a_turn_on_the_desktop_lane_takes_no_detached_room(policy):
    """C-10.3, C-26.9: the bound counts detached attempts; a conversation's turns are the person's own use."""
    turns = [{"job_id": f"turn-{n}", "kind": "turn"} for n in (1, 2)]
    snapshot = desk_only(attempts=[attempt(DESK, n, job_id=f"turn-{n}") for n in (1, 2)], jobs=turns)
    assert evaluate(policy, snapshot, job()).chosen_lane == DESK


@pytest.mark.parametrize("bound,running,placed", [(None, 9, True), (0, 0, False), (1, 0, True), (1, 1, False)])
def test_c10_3_the_bound_is_policy(policy, bound, running, placed):
    """C-10.3: null is no bound; 0 keeps detached work off the login entirely."""
    policy["admission"]["desktop_max_in_flight"] = bound
    decision = evaluate(policy, desk_only(attempts=[attempt(DESK, n) for n in range(running)]), job())
    assert (decision.chosen_lane == DESK) is placed


def test_c10_3_a_turn_is_never_refused_by_the_reserve(policy):
    """C-10.3, C-26.9: a turn is attended; the reserve exists for the person's own sessions."""
    policy["admission"]["desktop_max_in_flight"] = 0
    snapshot = desk_only([reading(DESK, "five_hour", .84)])
    turn = job(kind="turn")
    decision = evaluate(policy, snapshot, turn)
    assert decision.chosen_lane == DESK
    assert "desktop_reserve" not in decision.evaluations[0]["candidate_details"][DESK]


# --- the clock, status, open lanes ------------------------------------------------------------

def test_c6_3_the_reset_of_a_counted_stale_reading_is_the_desktop_lanes_horizon():
    """C-6.3: the reserve lifts when the window resets, with no row changing, so that instant is the lane's
    horizon; a non-desktop lane's stale reading adds none."""
    rows = [reading(DESK, "five_hour", .8, observed=-7200, resets=900),
            reading("claude-1", "five_hour", .8, observed=-7200, resets=600, reading_id=2)]
    snapshot = view([lane("claude-1"), lane(DESK, desktop=True)], rows)
    horizons = lane_horizons(snapshot)
    assert horizons[DESK] == NOW + timedelta(seconds=900)
    assert "claude-1" not in horizons


def test_c6_11_open_lanes_and_desktop_reserved_follow_the_reserve_not_claude_code():
    """C-6.11, C-10.3: the desktop lane is open while its reserve admits detached work, in use or not."""
    settings_ = admission_settings({})
    for in_use in (True, False):
        assert DESK in open_lanes(desk_only(in_use=in_use), {}, settings_)
    full = desk_only(attempts=[attempt(DESK, 1), attempt(DESK, 2)])
    assert DESK not in open_lanes(full, {}, settings_)
    hot = {row["lane_id"]: row for row in desk_only([reading(DESK, "seven_day", .8)])["lanes"]}
    assert desktop_reserved(hot[DESK], settings_, now=NOW)
    assert not desktop_reserved(hot["claude-1"], settings_, now=NOW)


# --- properties -------------------------------------------------------------------------------

WINDOWS = st.sampled_from(["five_hour", "seven_day"])
UTILIZATIONS = st.sampled_from([0.0, .3, .5, .69, .7, .71, .85, .99])


@st.composite
def desk_cases(draw):
    rows = [reading(DESK, draw(WINDOWS), draw(UTILIZATIONS), observed=draw(st.sampled_from([-7200, -600, -30, 0])),
                    resets=draw(st.sampled_from([None, -5, 60, 3600, 86400 * 6])),
                    label=draw(st.sampled_from(["provider", "provider", "stale-provider"])), reading_id=n + 1)
            for n in range(draw(st.integers(0, 4)))]
    running = draw(st.integers(0, 4))
    turns = draw(st.integers(0, 2))
    jobs = [{"job_id": f"turn-{n}", "kind": "turn"} for n in range(turns)]
    attempts = ([attempt(DESK, n) for n in range(running)]
                + [attempt(DESK, 10 + n, job_id=f"turn-{n}") for n in range(turns)])
    reserve = {"five_hour": draw(st.sampled_from([None, 0.0, .3, .5])), "seven_day": draw(st.sampled_from([None, .3, .6]))}
    bound = draw(st.sampled_from([None, 0, 1, 2, 3]))
    kind = draw(st.sampled_from(["dispatch", "dispatch", "turn"]))
    in_use = draw(st.sampled_from([None, True, False]))
    return rows, attempts, jobs, reserve, bound, kind, in_use


@settings(max_examples=300, deadline=None)
@given(desk_cases())
def test_c10_3_properties_of_the_reserve(case):
    """C-10.3, for every drawn case:
    1. the in-use signal never changes the decision;
    2. a detached job is placed on the desktop lane only if no counted reading is at or above its ceiling and
       fewer than the bound are in flight, and a turn whatever they say (soundness, both ways);
    3. raising a window's reserve or lowering the bound never places a job that was not placed (monotone)."""
    rows, attempts, jobs, reserve, bound, kind, in_use = case
    base = load_policy(DEFAULT_POLICY_PATH)
    base["reserve"] = {**base.get("reserve", {}), "models": []}
    base["headroom_floor"] = 0.0                  # only the reserve refuses here, never C-11.3's floor

    def decide(reserve_, bound_, in_use_):
        policy = copy.deepcopy(base)
        policy["admission"] = {**policy["admission"], "desktop_reserve": reserve_, "desktop_max_in_flight": bound_}
        snapshot = view([lane(DESK, desktop=True)], rows, attempts, jobs, in_use=in_use_)
        return evaluate(policy, snapshot, job(kind=kind))
    decision = decide(reserve, bound, in_use)
    for other in (None, True, False):
        assert decide(reserve, bound, other) == decision
    counted = desktop_reserve_readings(view([lane(DESK, desktop=True)], rows)["readings"], now=NOW)
    over = any(reserve.get(window) is not None and row["utilization"] >= round(1 - reserve[window], 6)
               for window, row in counted.items())
    detached = sum(1 for row in attempts if not row["job_id"].startswith("turn-"))
    full = bound is not None and detached >= bound
    assert (decision.chosen_lane == DESK) is (kind == "turn" or not (over or full))
    if decision.chosen_lane is None:
        stricter = {window: (None if keep is None else min(1.0, keep + .2)) for window, keep in reserve.items()}
        assert decide(stricter, bound, in_use).chosen_lane is None
        assert decide(reserve, None if bound is None else max(0, bound - 1), in_use).chosen_lane is None
