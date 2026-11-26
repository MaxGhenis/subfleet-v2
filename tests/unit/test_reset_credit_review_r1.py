"""#179 review, round 1: the reviewer's probes, kept as regressions, and the properties behind them.

Each probe (`review-r179/test_r179_review_probes*.py`) asserted what the code at e46cc4815
did; each case here asserts what C-23.16 requires instead, so every one fails there.

- P2-1: a consume answered 2xx with a body that is not a confirmed reset was `failed`, which
  blocks nothing and starts no interval, so the next cycle, 60 s later, spent again while the
  usage endpoint still lagged (C-23.17). It is `unknown` now, and blocks every spend until a
  usage read settles it. `failed` is kept for the answers that show nothing was spent.
- P3-1: a full fleet or parent cap made a job whose whole route was limited read as Codex
  demand. A capped decision is `capped`, never demand.
- P3-2: the interval ran between pass starts, not consumes: two POSTs landed 29 min 10 s
  apart under a 30-minute interval. The spend is recorded at the clock's reading once the
  consume returns.
- P3-3: a held lane was listed as a shadow blocker. A held lane is never listed.
- P3-5: a consume `_bounded` never started (deadline, cancel, or the lane's HTTP slot taken)
  was `unknown`, blocking every reset until the lane read open. It sent nothing, so it is
  `failed` (`aborted-before-submission`).
- P2-2: the property checker misread a job named twice by lying demand (the checker fix is
  in `test_reset_credit_properties.py`; its counterexample world is kept here).

Every HTTP call goes to a fake opener; no real endpoint is touched and nothing is redeemed.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import actions as actions_module
from subfleet.actions import ResetCredits, demand_verdict
from subfleet.adapters.codex import CodexAdapter, WHAM_RESET_CREDITS_CONSUME_URL, WHAM_RESET_CREDITS_URL
from subfleet.capacity import from_store
from subfleet.scheduler import evaluate
from subfleet.store import Store
from subfleet.timers import Timers
from tests.unit.test_reset_credit_demand import Clock, _limited_probe, astra_job, codex_fleet, place_hold
from tests.unit.test_reset_credit_properties import Step, World, fake_transport_clock, play  # noqa: F401 (fixture)
from tests.unit.test_scheduler import attempt, policy, view  # noqa: F401  (fixture)
from tests.unit.test_timers_reset_credits import HTTP, NOW, limit_again, store, wants  # noqa: F401  (fixture)
from tests.unit.test_timers_reset_credits import lane as limited_lane

INTERVAL = {"reset_credits": {"enabled": True, "min_interval_min": 30}}


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


class Wham:
    """Fake WHAM: lists `credits` on every account, answers each consume from `answers` in turn."""

    def __init__(self, credits, *answers):
        self.credits, self.answers, self.calls = credits, list(answers), []

    def __call__(self, request, timeout):
        self.calls.append((request.get_method(), request.get_header("Chatgpt-account-id"), request.full_url))
        if request.full_url == WHAM_RESET_CREDITS_URL:
            return 200, json.dumps({"credits": self.credits}).encode()
        assert request.full_url == WHAM_RESET_CREDITS_CONSUME_URL
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def posts(http):
    return [call for call in http.calls if call[0] == "POST"]


def view_at(store, moment, **overrides):
    """The timer's snapshot at `moment`: each lane read just now, still `limit_reached` where it is
    closed (usage lags a reset, C-23.17)."""
    found = from_store(store, now=moment)
    for row in found["lanes"]:
        row["probe"] = {"status": "limited" if row["closures"] else "ok", "limit_reached": bool(row["closures"]),
                        "checked_at": _iso(moment), "reset_credits": {"available": 2, "applicable": 2}}
        row.update(overrides.get(row["lane_id"], {}))
    return found


def _resets(store, http, **policy):
    return ResetCredits(store, {"reset_credits": {**INTERVAL["reset_credits"], **policy}},
                        lambda lane: CodexAdapter(opener=http, now=lambda: NOW))


GIFTS = [{"id": f"gift-{n}", "reset_type": "codex_rate_limits", "status": "available"} for n in (1, 2)]
UNRECOGNISED = [{"code": "reset"},                          # windows_reset missing
                {"code": "reset", "windows_reset": "2"},    # a string
                {"code": "reset", "windows_reset": 0},
                {"status": "reset", "windows_reset": 2}]    # code renamed


# --- P2-1: a 2xx that is not a confirmed reset is unknown, and blocks ------------

@pytest.mark.parametrize("body", UNRECOGNISED, ids=["no-windows", "string-windows", "zero-windows", "renamed"])
def test_p2_1_an_unrecognised_2xx_blocks_the_next_cycle_on_the_same_lane(store, tmp_path, body):
    """Two gifts on one limited lane, one waiting job, `min_interval_min` 30. The first consume is
    answered 200 with a body that is not a confirmed reset; 60 s later the lane still reads
    `limit_reached` and the job still waits. Reviewer's probe: a second POST spent `gift-2`."""
    limited_lane(store, tmp_path, 1)
    http = Wham(GIFTS, (200, json.dumps(body).encode()))
    resets = _resets(store, http)
    first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
    assert first["status"] == "unknown"
    assert store.get_action(first["action_id"])["state"] == "unknown"
    assert store.list_closures(active_at=_iso(NOW))                   # nothing released on a guess
    later = NOW + timedelta(seconds=60)
    assert resets.evaluate(view_at(store, later), now=later, demand=wants(store))["status"] == "unsettled-action"
    assert len(posts(http)) == 1


def test_p2_1_an_unrecognised_2xx_blocks_the_job_s_other_lane_too(store, tmp_path):
    """One gift per account: the reviewer's probe saw the second POST go to the job's other limited
    lane (codex-2) 60 s later."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=3)
    http = Wham(GIFTS[:1], (200, b'{"code":"reset"}'))
    resets = _resets(store, http)
    first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
    assert (first["status"], first["lane_id"]) == ("unknown", "codex-1")
    for seconds in (60, 120, 31 * 60, 3 * 3600):
        later = NOW + timedelta(seconds=seconds)
        assert resets.evaluate(view_at(store, later), now=later, demand=wants(store))["status"] == "unsettled-action"
    assert [account for _, account, _ in posts(http)] == ["1"]


def test_p2_1_control_a_5xx_is_unknown_and_blocks(store, tmp_path):
    """Control, as the reviewer ran it: a 503 (network-error) was `unknown` and blocked already."""
    limited_lane(store, tmp_path, 1)
    http = Wham(GIFTS, (503, b"{}"))
    resets = _resets(store, http)
    assert resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))["status"] == "unknown"
    later = NOW + timedelta(seconds=60)
    assert resets.evaluate(view_at(store, later), now=later, demand=wants(store))["status"] == "unsettled-action"
    assert len(posts(http)) == 1


def test_p2_1_an_unrecognised_2xx_is_settled_by_the_lane_reading_open(store, tmp_path):
    """C-19.1: the usage read that finds the lane open settles it as a reset: the job is due, and
    (d) holds the next spend back while that lane has room."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=3)
    http = Wham(GIFTS, (200, b'{"code":"reset"}'))
    resets = _resets(store, http)
    first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
    assert first["status"] == "unknown"
    opened = NOW + timedelta(minutes=2)
    settled = resets.settle_by_usage("codex-1", {"status": "ok", "limit_reached": False,
                                                 "checked_at": _iso(opened)}, now=opened)
    assert settled["original_state"] == "unknown" and settled["outcome"] == "usage-open"
    assert store.get_job("job-1")["next_check_at"] == _iso(opened)
    later = NOW + timedelta(hours=2)
    result = resets.evaluate(view_at(store, later, **{"codex-1": {"probe": {
        "status": "ok", "limit_reached": False, "checked_at": _iso(later)}}}), now=later, demand=wants(store))
    assert result["status"] == "reset-lane-open" and result["reset_lanes"] == ["codex-1"]
    assert len(posts(http)) == 1


def test_p2_1_only_answers_that_show_nothing_was_spent_are_failed():
    """C-23.16 (d): the four answers that are `failed`: a refusal, no credential, the marker, and a
    call never started. Every other non-confirmed answer is `unknown`."""
    assert actions_module.NOT_SPENT == frozenset({"refused", "no-auth", "inhibited", "aborted-before-submission"})


# --- the property behind P2-1: no second consume while an earlier one may have spent ---

ANSWERS = {
    "confirmed": (200, b'{"code":"reset","windows_reset":2}'),
    "no-windows": (200, b'{"code":"reset"}'),
    "string-windows": (200, b'{"code":"reset","windows_reset":"2"}'),
    "zero-windows": (200, b'{"code":"reset","windows_reset":0}'),
    "renamed": (200, b'{"status":"reset","windows_reset":2}'),
    "other-2xx": (201, b'{"code":"accepted"}'),
    "empty-2xx": (200, b"{}"),
    "not-json-2xx": (200, b"<html>"),
    "list-2xx": (200, b"[]"),
    "503": (503, b"{}"),
    "timeout": TimeoutError("the provider never answered"),
    "reset-by-peer": ConnectionResetError("dropped"),
    "409": (409, b'{"error":{"code":"already_redeemed"}}'),
    "429": (429, b'{"error":{"code":"rate_limited"}}'),
    **{str(status): (status, b'{"code":"reset","windows_reset":2}')
       for status in (301, 302, 303, 307, 308)},
    "403-empty": (403, b"{}"),
    "408": (408, b"{}"),
    "409-reset": (409, b'{"code":"reset","windows_reset":2}'),
    "429-conflicting": (429, b'{"error":{"code":"rate_limited"},"windows_reset":2}'),
}
#: Only the explicit 409/429 refusals are `failed`; all other non-2xx are unknown.
EXPECTED = {**{name: "unknown" for name in ANSWERS}, "confirmed": "confirmed", "409": "failed", "429": "failed"}

OUTCOMES = settings(max_examples=int(os.environ.get("SUBFLEET_RESET_EXAMPLES", "100")),
                    deadline=None, database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


@OUTCOMES
@given(answers=st.lists(st.sampled_from(sorted(ANSWERS)), min_size=1, max_size=6),
       gaps=st.lists(st.sampled_from((1, 10, 31, 120, 24 * 60)), min_size=6, max_size=6))
def test_no_second_consume_while_an_earlier_one_may_have_spent(tmp_path_factory, answers, gaps):
    """C-23.16 (d), over generated consume answers: while the usage endpoint lags (every lane still
    reads `limit_reached`, nothing settles), a consume is sent only if every consume before it is
    `failed`, an answer that shows nothing was spent; after any other, every evaluation is held
    back (`unsettled-action`, or `interval-blocked` and `reset-lane-open` after a confirmed
    reset); each answer records the state EXPECTED names; and no gift is sent twice. Three
    limited lanes, two gifts on each, one waiting job, evaluations a minute to a day apart."""
    root = tmp_path_factory.mktemp("outcomes")
    with Store(root / "state.db") as store:
        for number, days in ((1, 6), (2, 4), (3, 2)):
            limited_lane(store, root, number, days=days)
        http = Wham(GIFTS, *(ANSWERS[name] for name in answers), ANSWERS["503"])
        resets = _resets(store, http)
        moment, sent = NOW, []
        for gap in gaps:
            before = len(posts(http))
            result = resets.evaluate(view_at(store, moment), now=moment, demand=wants(store))
            if len(posts(http)) > before:
                assert len(posts(http)) == before + 1
                name = answers[len(sent)] if len(sent) < len(answers) else "503"
                assert all(state == "failed" for _, state in sent), (sent, name)
                assert result["status"] == store.get_action(result["action_id"])["state"] == EXPECTED[name], \
                    (name, result)
                sent.append((name, result["status"]))
            elif sent and sent[-1][1] != "failed":
                assert result["status"] in ("unsettled-action", "interval-blocked", "reset-lane-open"), result
            moment += timedelta(minutes=gap)
        keys = [row["op_key"] for row in store.query("SELECT op_key FROM actions")]
        assert len(keys) == len(set(keys)) == len(sent)


# --- P3-1: a full fleet or a parent at its cap is not Codex demand ----------------

def test_p3_1_a_full_fleet_with_every_route_lane_limited_is_capped_not_demand(policy):
    """Reviewer's probe: six limited lanes and the fleet at its cap read `codex-demand`."""
    lanes, readings, closures, _ = codex_fleet(*["limited"] * 6)
    fillers = [attempt("codex-1", f"filler-{n}") for n in range(policy["caps"]["max_active_attempts"])]
    decision = evaluate(policy, view(lanes, readings, closures, fillers), astra_job())
    assert decision.evaluations[0]["capacity_blocks"] == ["fleet"]
    assert demand_verdict(decision) == {"verdict": "capped", "capacity_lanes": [],
                                        "limited_lanes": [f"codex-{n}" for n in range(1, 7)],
                                        "capacity_blocks": ["fleet"]}
    # Once the fleet has a slot, the same route is demand.
    free = evaluate(policy, view(lanes, readings, closures), astra_job())
    assert demand_verdict(free)["verdict"] == "codex-demand"


def test_p3_1_a_parent_at_its_cap_with_every_route_lane_limited_is_capped(policy):
    policy["caps"]["max_active_attempts_per_parent"] = 1
    lanes, readings, closures, _ = codex_fleet(*["limited"] * 3)
    family = [{"job_id": "parent", "state": "running"},
              {"job_id": "sibling", "parent_job_id": "parent", "state": "running"}]
    snapshot_ = view(lanes, readings, closures, [attempt("claude-9", "sibling")], family)
    decision = evaluate(policy, snapshot_, astra_job(job_id="child", parent_job_id="parent"))
    assert decision.evaluations[0]["capacity_blocks"] == ["parent:parent"]
    assert demand_verdict(decision)["verdict"] == "capped"


def test_p3_1_a_capped_job_is_never_spent_for(store, tmp_path):
    """The timer and an operator's lane alike: a `capped` job is no demand."""
    limited_lane(store, tmp_path, 1)
    http = Wham(GIFTS, ANSWERS["confirmed"])
    resets = _resets(store, http)
    capped = [{**wants(store)[0], "verdict": "capped", "capacity_blocks": ["fleet"]}]
    assert resets.evaluate(view_at(store, NOW), now=NOW, demand=capped)["status"] == "no-demand"
    named = resets.evaluate(view_at(store, NOW), now=NOW, demand=capped, target_lane_id="codex-1")
    assert named["status"] == "no-demand"
    assert http.calls == []


# --- P3-2: the interval runs between consumes, not pass starts --------------------

def _timers(store, tmp_path, clock, opener, interval):
    return Timers(store, tmp_path, {"reset_credits": {"enabled": True, "min_interval_min": interval}},
                  demand=lambda: wants(store), now=clock,
                  adapter_factory=lambda provider: CodexAdapter(opener=opener, now=clock))


def test_p3_2_two_consumes_never_land_closer_than_the_interval(store, tmp_path):
    """Reviewer's probe: the first pass's listing took 50 s, the second pass ran at +30 min, and
    the two POSTs landed 29 min 10 s apart under a 30-minute interval. The spend is recorded when
    its consume returns, so the pass at +30 min is held back and the one at +30 min 50 s spends,
    exactly 30 minutes after the first consume."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=3)
    clock, http, consumed_at, listed = Clock(NOW), HTTP(), [], []

    def opener(request, timeout):
        if request.get_method() == "GET" and not listed:
            listed.append(1)
            clock.advance(50)                     # the first pass's listing takes 50 s
        if request.get_method() == "POST":
            consumed_at.append(clock())
        return http(request, timeout)

    timers = _timers(store, tmp_path, clock, opener, 30)
    try:
        timers.metadata["codex-1"] = _limited_probe(clock())
        first = timers.evaluate_resets(timers.snapshot())
        assert (first["status"], first["lane_id"]) == ("confirmed", "codex-1")
        action = store.get_action(first["action_id"])
        assert action["updated_at"] == "2026-09-05T12:00:50Z"              # the consume's time, not the pass's
        assert first["override"]["confirmed_at"] == "2026-09-05T12:00:50Z"
        clock.value = NOW + timedelta(minutes=10)
        limit_again(store, "codex-1")             # used up again
        clock.value = NOW + timedelta(minutes=30)
        timers.metadata["codex-2"] = _limited_probe(clock())
        held = timers.evaluate_resets(timers.snapshot())
        assert held["status"] == "interval-blocked"
        clock.value = NOW + timedelta(minutes=30, seconds=50)
        timers.metadata["codex-2"] = _limited_probe(clock())
        second = timers.evaluate_resets(timers.snapshot())
    finally:
        timers.stop()
    assert (second["status"], second["lane_id"]) == ("confirmed", "codex-2")
    assert consumed_at[1] - consumed_at[0] == timedelta(minutes=30)


def test_p3_2_a_clock_that_goes_back_after_a_spend_is_inside_the_interval(store, tmp_path):
    """Reviewer's control, kept: the interval is measured from the store, so a clock moved back
    two hours after a spend reads as inside it."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=3)
    clock, http = Clock(NOW), HTTP()
    timers = _timers(store, tmp_path, clock, http, 30)
    try:
        timers.metadata["codex-1"] = _limited_probe(clock())
        assert timers.evaluate_resets(timers.snapshot())["status"] == "confirmed"
        limit_again(store, "codex-1")
        clock.value = NOW - timedelta(hours=2)
        timers.metadata["codex-2"] = _limited_probe(clock())
        result = timers.evaluate_resets(timers.snapshot())
    finally:
        timers.stop()
    assert result["status"] == "interval-blocked"


@settings(max_examples=int(os.environ.get("SUBFLEET_RESET_EXAMPLES", "100")), deadline=None,
          database=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(steps=st.lists(st.tuples(st.sampled_from((1, 10, 29, 30, 31, 45)),          # minutes to the next pass
                                st.sampled_from((0, 1, 30, 50, 90)),               # its listing, seconds
                                st.sampled_from(("none", "used-up"))), min_size=2, max_size=7))
def test_consumes_are_never_closer_than_the_interval_on_the_daemon_s_clock(tmp_path_factory, steps):
    """C-23.16 (d) as a property: on the daemon's clock, however long each pass's listing takes,
    no two POSTs are less than `min_interval_min` apart. Four limited lanes, one gift each, one
    waiting job; between passes the last lane reset may be used up again. Each pass is
    `evaluate` with the daemon's clock, as `Timers.evaluate_resets` calls it (the case above
    runs that path)."""
    root = tmp_path_factory.mktemp("interval")
    with Store(root / "state.db") as store:
        for number, days in ((1, 6), (2, 5), (3, 4), (4, 3)):
            limited_lane(store, root, number, days=days)
        clock, http, consumed_at, delay = Clock(NOW), HTTP(), [], [0]

        def opener(request, timeout):
            if request.get_method() == "GET":
                clock.advance(delay[0])
                delay[0] = 0                     # the pass's first listing takes it all
            else:
                consumed_at.append(clock())
            return http(request, timeout)

        resets, reset = _resets(store, opener), []
        for minutes, listing, change in steps:
            if change == "used-up" and reset:
                limit_again(store, reset[-1])
            delay[0], start = listing, clock()
            result = resets.evaluate(view_at(store, start), now=start, demand=lambda: wants(store), clock=clock)
            if result["status"] == "confirmed":
                reset.append(result["lane_id"])
            # The next pass starts `minutes` after this one started, as a timer's cycles do,
            # or as soon as this one is done if it ran longer.
            clock.value = max(clock(), start + timedelta(minutes=minutes))
        gaps = [later - earlier for earlier, later in zip(consumed_at, consumed_at[1:])]
        assert all(gap >= timedelta(minutes=30) for gap in gaps), (consumed_at, steps)


# --- P3-3: a held lane is never listed, not even as a shadow blocker ---------------

def test_p3_3_a_held_lane_is_not_listed_as_a_shadow_blocker(store, tmp_path):
    """Reviewer's probe: the job's only candidate, codex-1, is app-shadowed; held codex-2 was
    listed to see whether its gift excluded codex-1."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=3)
    place_hold(store, "codex-2", "operator-hold", until="2026-09-12T12:00:00Z")
    http = Wham([], ANSWERS["confirmed"])
    resets = _resets(store, http)
    result = resets.evaluate(view_at(store, NOW, **{"codex-1": {"app_shadowed": True}}), now=NOW,
                             demand=wants(store, "codex-1"))
    assert result["status"] == "no-concrete-credit"
    assert [account for _, account, _ in http.calls] == ["1"]


def test_p3_3_a_held_lane_s_gift_does_not_exclude_a_shadowed_candidate(store, tmp_path):
    """C-23.46 counts only lanes a credit may go on: a held lane cannot take one, so its gift
    (unlisted) leaves the shadowed candidate its turn."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=3)
    place_hold(store, "codex-2", "auth-dead", until="2026-09-12T12:00:00Z")
    http = Wham(GIFTS[:1], ANSWERS["confirmed"])
    resets = _resets(store, http)
    result = resets.evaluate(view_at(store, NOW, **{"codex-1": {"app_shadowed": True}}), now=NOW,
                             demand=wants(store, "codex-1"))
    assert (result["status"], result["lane_id"]) == ("confirmed", "codex-1")
    assert {account for _, account, _ in http.calls} == {"1"}


def test_p3_3_a_hold_that_lands_during_the_pass_stops_the_next_listing(store, tmp_path):
    """Holds are read again before each listing: codex-3 is held while codex-2 is being listed,
    and is then never listed."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=5)
    limited_lane(store, tmp_path, 3, days=4)

    class Holding(Wham):
        def __call__(self, request, timeout):
            if request.get_header("Chatgpt-account-id") == "2":
                place_hold(store, "codex-3", "operator-hold", until="2026-09-12T12:00:00Z")
            return super().__call__(request, timeout)

    http = Holding([], ANSWERS["confirmed"])
    resets = _resets(store, http)
    result = resets.evaluate(view_at(store, NOW, **{"codex-1": {"app_shadowed": True}}), now=NOW,
                             demand=wants(store, "codex-1"))
    assert result["status"] == "no-concrete-credit"
    assert [account for _, account, _ in http.calls] == ["2", "1"]


# --- P3-5: a consume that never started is failed, and holds nothing back --------

def test_p3_5_a_consume_whose_deadline_passed_after_the_claim_is_failed_not_unknown(store, tmp_path, monkeypatch):
    """The deadline passes between the claim and the consume (a slow store): `_bounded` never
    starts the call, so nothing is sent. Recorded `failed`, as `recover` records a pending intent
    that never called the provider; the next evaluation spends the lane's next gift."""
    limited_lane(store, tmp_path, 1)
    http = Wham(GIFTS, ANSWERS["confirmed"])
    resets = _resets(store, http)
    clock = [0.]
    monkeypatch.setattr(actions_module, "time", type("T", (), {"monotonic": staticmethod(lambda: clock[0])}))
    claim = resets.actions.claim

    def slow_claim(*args, **kwargs):
        claimed = claim(*args, **kwargs)
        clock[0] = 61.                                 # past the pass's 60 s deadline
        return claimed
    monkeypatch.setattr(resets.actions, "claim", slow_claim)
    result = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
    assert result["status"] == "failed"
    action = store.get_action(result["action_id"])
    assert json.loads(action["result_json"]) == {"status": "aborted-before-submission", "error_type": "CancelledError"}
    assert posts(http) == []
    monkeypatch.setattr(resets.actions, "claim", claim)
    later = NOW + timedelta(seconds=60)
    clock[0] = 100.
    again = resets.evaluate(view_at(store, later), now=later, demand=wants(store))
    assert again["status"] == "confirmed"
    assert json.loads(store.get_action(again["action_id"])["request_json"])["credit_id"] == "gift-2"
    assert len(posts(http)) == 1


def test_p3_5_a_consume_behind_a_request_still_running_on_the_lane_is_failed(store, tmp_path, monkeypatch):
    """The lane's HTTP slot is still held by an earlier request (one `_bounded` gave up on):
    the consume is never started, so nothing is sent."""
    limited_lane(store, tmp_path, 1)
    http = Wham(GIFTS, ANSWERS["confirmed"])
    resets = _resets(store, http)
    claim, taken = resets.actions.claim, threading.Event()

    def claim_then_take_the_slot(*args, **kwargs):
        claimed = claim(*args, **kwargs)
        taken.set()
        assert resets._http_slots["codex-1"].acquire(blocking=False)
        return claimed
    monkeypatch.setattr(resets.actions, "claim", claim_then_take_the_slot)
    result = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
    resets._http_slots["codex-1"].release()
    assert taken.is_set() and result["status"] == "failed"
    assert json.loads(store.get_action(result["action_id"])["result_json"]) == {
        "status": "aborted-before-submission", "error_type": "PreviousRequestRunning"}
    assert posts(http) == []


def test_p3_5_a_consume_that_started_and_timed_out_stays_unknown(store, tmp_path):
    """The control: once the call has started, the request may have left, so it is `unknown`."""
    limited_lane(store, tmp_path, 1)
    http = Wham(GIFTS, TimeoutError("no answer"))
    resets = _resets(store, http)
    result = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
    assert result["status"] == "unknown" and len(posts(http)) == 1
    later = NOW + timedelta(seconds=60)
    assert resets.evaluate(view_at(store, later), now=later, demand=wants(store))["status"] == "unsettled-action"


# --- P2-2: the checker's false positive, kept as a world --------------------------

def test_p2_2_a_job_named_twice_by_lying_demand_is_matched_on_either_entry():
    """Reviewer's world: lying demand names job-0 twice, once with codex-1 and once with codex-2.
    The spend on codex-1 is for job-0's first entry, and is no violation."""
    world = World(lanes=(("limited", True, 4, False), ("limited", True, 6, False)), jobs=(frozenset(),),
                  steps=(Step(minutes=120, change="used-up", target=1),), enabled=True, interval=30,
                  adversarial=((0, (1,)), (0, (2,))))
    assert play(world) == ([], [("confirmed", "codex-1", "job-0")])
