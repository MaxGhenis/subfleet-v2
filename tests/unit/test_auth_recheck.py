"""C-10.8: which disabled lanes the auth-dead re-check may touch, and when.

The pure half (`subfleet/recheck.py`) is tested here: the eligibility table, the
schedule, and the bounds as properties over every schedule of ticks, outcomes,
load and busy slots Hypothesis can find:

- per lane, two re-checks start at least `interval_s` apart, and at least
  `backoff_s(k)` apart after k failures in a row (the cadence bound and backoff);
- any two re-checks, of any lanes, start at least `spacing_s` apart;
- at most one lane is chosen per decision (the one-at-a-time bound's pure half;
  the worker and the enrollment lock are tested in tests/fake/test_auth_recheck.py);
- a lane left alone for any reason but auth-dead is never chosen;
- liveness: with no load and a free slot, a due lane is re-checked within one
  tick plus one spacing per other due lane.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from hypothesis import given, settings as hsettings, strategies as st
import pytest

from subfleet import recheck
from subfleet.recheck import (
    AUTH_DEAD, JITTER_FRACTION, MIN_INTERVAL_S, Settings, backoff_s, choose, disable_reason,
    iso, jitter_s, last_started, latest_by_lane, roster_disabled, standings,
)

T0 = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
S = Settings()


def lane(lane_id, *, enabled=False, provider="claude", ref=None, account=None, owner="v2",
         desktop=False, identity=None, status="enrolled", at=T0):
    return {"lane_id": lane_id, "provider": provider, "enabled": int(enabled),
            "credential_ref": ref or f"claude-quota-{lane_id}@example.test",
            "credential_kind": "keychain-token" if provider == "claude" else "home",
            "account_key": account or f"{provider}:{lane_id}@example.test", "owner": owner,
            "desktop": int(desktop), "identity": identity, "identity_status": status,
            "created_at": iso(at), "updated_at": iso(at)}


def dead(lane_id, at=T0):
    return {lane_id: {"reason": AUTH_DEAD, "source": "attempt", "at": iso(at)}}


def judge(rows, *, disabled=None, verdicts=None, rechecks=None, roster_off=frozenset(), settings=S):
    return standings(rows, disabled=disabled or {}, verdicts=verdicts or {}, rechecks=rechecks or {},
                     roster_off=None if roster_off is None else set(roster_off), settings=settings)


# --- the schedule -------------------------------------------------------------


def test_c10_8_backoff_doubles_from_six_hours_to_a_day():
    assert [backoff_s(k, S) / 3600 for k in range(0, 6)] == [6, 12, 24, 24, 24, 24]


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=0, max_value=10_000))
def test_c10_8_backoff_is_monotone_and_bounded(a, b):
    low, high = sorted((a, b))
    assert S.interval_s <= backoff_s(low, S) <= backoff_s(high, S) <= S.max_interval_s


@given(st.text(min_size=1, max_size=20), st.floats(min_value=1, max_value=1e7))
def test_c10_8_jitter_is_a_fixed_fraction_of_the_wait(lane_id, seconds):
    value = jitter_s(lane_id, seconds)
    assert 0 <= value < JITTER_FRACTION * seconds
    assert value == jitter_s(lane_id, seconds)


def test_c10_8_policy_cannot_ask_for_more_than_one_recheck_every_six_hours():
    assert MIN_INTERVAL_S == 6 * 3600
    assert Settings.from_policy({"timers": {"auth_recheck_interval_s": 60}}).interval_s == MIN_INTERVAL_S
    assert Settings.from_policy({"timers": {"auth_recheck_interval_s": 3600}}).interval_s == MIN_INTERVAL_S
    assert not Settings.from_policy({"timers": {"auth_recheck_interval_s": 0}}).enabled
    clamped = Settings.from_policy({"timers": {"auth_recheck_interval_s": 43200,
                                               "auth_recheck_max_interval_s": 60}})
    assert clamped.max_interval_s == 43200
    assert Settings.from_policy({"timers": {"auth_recheck_interval_s": True}}) == Settings()


def test_c10_8_custom_settings_reach_the_schedule():
    custom = Settings.from_policy({"timers": {"auth_recheck_interval_s": 43200, "auth_recheck_max_interval_s": 172800,
                                              "auth_recheck_spacing_s": 3600, "auth_recheck_tick_s": 120,
                                              "auth_recheck_max_load_per_cpu": 0.5}})
    assert (custom.interval_s, custom.max_interval_s, custom.spacing_s, custom.tick_s, custom.max_load_per_cpu) == \
        (43200, 172800, 3600, 120, 0.5)
    assert [backoff_s(k, custom) / 3600 for k in range(4)] == [12, 24, 48, 48]
    current = judge([lane("claude-11"), lane("claude-12")], disabled=dead("claude-11") | dead("claude-12"),
                    settings=custom)
    now = T0 + timedelta(days=3)
    assert choose(current, now=now, settings=custom, started=now - timedelta(seconds=1800)) == (None, "spacing")
    assert choose(current, now=now, settings=custom, started=now - timedelta(seconds=3600))[0]
    soon = min(recheck._instant(s.next_at) for s in current.values()) + timedelta(hours=1)
    assert choose(current, now=soon, settings=custom, started=None, load_per_cpu=0.6)[1].startswith("host load")
    assert choose(current, now=soon, settings=custom, started=None, load_per_cpu=0.4)[0]
    # Overdue by the custom ceiling (48 h), load no longer holds it.
    assert choose(current, now=soon + timedelta(hours=48), settings=custom, started=None, load_per_cpu=0.6)[0]


def test_c10_8_each_lane_has_its_own_positive_jitter():
    offsets = {lane_id: jitter_s(lane_id, 21600) for lane_id in ("claude-11", "claude-12", "claude-13", "codex-3")}
    assert all(value > 0 for value in offsets.values())
    assert len(set(offsets.values())) == len(offsets)


def test_c10_8_a_legacy_lane_is_scheduled_from_its_disable_not_its_creation():
    row = lane("claude-11")
    row["updated_at"] = iso(T0 + timedelta(days=3))            # disabled three days after it was enrolled
    current = judge([row], verdicts={"claude-11": {"verdict": "auth-dead"}})
    wait = S.interval_s
    assert current["claude-11"].disabled_at == iso(T0 + timedelta(days=3))
    assert current["claude-11"].next_at == iso(T0 + timedelta(days=3, seconds=wait + jitter_s("claude-11", wait)))


def test_c10_8_the_first_recheck_is_an_interval_after_the_disable():
    current = judge([lane("claude-11")], disabled=dead("claude-11"))
    standing = current["claude-11"]
    assert standing.eligible and standing.why == AUTH_DEAD
    wait = S.interval_s
    assert standing.next_at == iso(T0 + timedelta(seconds=wait + jitter_s("claude-11", wait)))


@pytest.mark.parametrize("failures,hours", [(0, 6), (1, 12), (2, 24), (7, 24)])
def test_c10_8_a_failed_recheck_backs_off(failures, hours):
    at = T0 + timedelta(days=1)
    current = judge([lane("claude-11")], disabled=dead("claude-11"),
                    rechecks={"claude-11": {"at": iso(at), "result": "failed", "failures": failures}})
    wait = hours * 3600
    assert current["claude-11"].next_at == iso(at + timedelta(seconds=wait + jitter_s("claude-11", wait)))
    assert current["claude-11"].failures == failures


def test_c10_8_a_started_record_counts_so_a_crash_mid_check_keeps_the_cadence():
    at = T0 + timedelta(days=1)
    current = judge([lane("claude-11")], disabled=dead("claude-11"),
                    rechecks={"claude-11": {"at": iso(at), "result": "started", "failures": 2}})
    assert recheck._instant(current["claude-11"].next_at) >= at + timedelta(hours=24)


def test_c10_8_a_recheck_from_before_this_disable_does_not_count():
    later = T0 + timedelta(days=3)
    current = judge([lane("claude-11", at=later)], disabled=dead("claude-11", later),
                    rechecks={"claude-11": {"at": iso(T0), "result": "failed", "failures": 5}})
    assert current["claude-11"].last is None and current["claude-11"].failures == 0
    assert recheck._instant(current["claude-11"].next_at) >= later + timedelta(hours=6)


def test_c10_8_off_in_policy_leaves_eligible_lanes_unscheduled():
    off = Settings.from_policy({"timers": {"auth_recheck_interval_s": 0}})
    current = judge([lane("claude-11")], disabled=dead("claude-11"), settings=off)
    assert current["claude-11"].eligible and current["claude-11"].next_at is None
    assert choose(current, now=T0 + timedelta(days=9), settings=off, started=None) == (None, "off")


# --- who is eligible ------------------------------------------------------------


def test_c10_8_disable_reason_is_the_recorded_one_else_the_verdict_written_with_it():
    assert disable_reason("a", dead("a"), {"a": {"verdict": "duplicate"}}) == AUTH_DEAD
    assert disable_reason("a", {}, {"a": {"verdict": "auth-dead"}}) == AUTH_DEAD
    assert disable_reason("a", {}, {"a": {"verdict": "duplicate"}}) == "non-canonical"
    assert disable_reason("a", {}, {"a": {"verdict": "identity-mismatch"}}) == "identity-mismatch"
    assert disable_reason("a", {}, {"a": {"verdict": "ok"}}) is None
    assert disable_reason("a", {}, {}) is None
    assert disable_reason("a", {"a": {"reason": "non-canonical"}}, {"a": {"verdict": "auth-dead"}}) == "non-canonical"
    # Every reason recorded for the lane stands: a later auth-dead never clears a mismatch.
    assert disable_reason("a", {"a": {"reason": AUTH_DEAD, "reasons": ("identity-mismatch", AUTH_DEAD)}}, {}) == \
        "identity-mismatch"
    assert disable_reason("a", {"a": {"reason": AUTH_DEAD, "reasons": (AUTH_DEAD, AUTH_DEAD)}}, {}) == AUTH_DEAD


def test_c10_8_disables_by_lane_keeps_every_reason():
    rows = [{"lane_id": "a", "ts": "t1", "data_json": json.dumps({"reason": "identity-mismatch", "at": "t1"})},
            {"lane_id": "a", "ts": "t1", "data_json": "{}"},
            {"lane_id": "a", "ts": "t2", "data_json": json.dumps({"reason": "auth-dead", "at": "t2"})}]
    latest = recheck.disables_by_lane(rows)
    assert latest["a"]["reasons"] == ("identity-mismatch", "auth-dead") and latest["a"]["at"] == "t2"
    assert disable_reason("a", latest, {}) == "identity-mismatch"


CASES = {
    # name: (rows, disabled, verdicts, roster_off, expected why for claude-11)
    "auth-dead, recorded": ([lane("claude-11")], dead("claude-11"), {}, (), AUTH_DEAD),
    "auth-dead, before reasons were recorded": (
        [lane("claude-11")], {}, {"claude-11": {"verdict": "auth-dead"}}, (), AUTH_DEAD),
    "a codex home lane": ([lane("claude-11", provider="codex", status=None)], dead("claude-11"), {}, (), AUTH_DEAD),
    "a verified lane": ([lane("claude-11", identity="acct:org", status="unverified")],
                        dead("claude-11"), {}, (), AUTH_DEAD),
    "identity mismatch": ([lane("claude-11")], {"claude-11": {"reason": "identity-mismatch"}}, {}, (),
                          "identity-mismatch"),
    "identity mismatch, legacy": ([lane("claude-11")], {}, {"claude-11": {"verdict": "identity-mismatch"}}, (),
                                  "identity-mismatch"),
    "mismatch status on an auth-dead lane": ([lane("claude-11", status="mismatch")], dead("claude-11"), {}, (),
                                             "identity-mismatch"),
    "non-canonical duplicate": ([lane("claude-11")], {"claude-11": {"reason": "non-canonical"}}, {}, (),
                                "non-canonical"),
    "non-canonical, legacy": ([lane("claude-11")], {}, {"claude-11": {"verdict": "duplicate"}}, (), "non-canonical"),
    "seeded disabled, no record": ([lane("claude-11")], {}, {}, (), "unrecorded"),
    "claude-15: roster-disabled, unverified, no record": (
        [lane("claude-11", status="unverified")], {}, {}, {"claude-11"}, "operator"),
    "operator turned an auth-dead lane off in lanes.json": (
        [lane("claude-11")], dead("claude-11"), {}, {"claude-11"}, "operator"),
    "roster unreadable": ([lane("claude-11")], dead("claude-11"), {}, None, "roster-unreadable"),
    "auth-dead but no identity to compare": ([lane("claude-11", status="unverified")], dead("claude-11"), {}, (),
                                             "identity-unverified"),
    "auth-dead, no identity status at all": ([lane("claude-11", status=None)], dead("claude-11"), {}, (),
                                             "identity-unverified"),
    "owned by v1": ([lane("claude-11", owner="v1")], dead("claude-11"), {}, (), "owner-v1"),
    "desktop": ([lane("claude-11", desktop=True)], dead("claude-11"), {}, (), "desktop"),
    "superseded by a later binding": (
        [lane("claude-11"), lane("claude-18", ref="claude-quota-claude-11@example.test",
                                 at=T0 + timedelta(hours=1))],
        dead("claude-11") | dead("claude-18"), {}, (), "superseded"),
    "its credential bound to an enabled lane": (
        [lane("claude-11"), lane("claude-18", enabled=True, ref="claude-quota-claude-11@example.test",
                                 at=T0 + timedelta(hours=1))], dead("claude-11"), {}, (), "superseded"),
    "its account enabled on another lane": (
        [lane("claude-11"), lane("claude-20", enabled=True, account="claude:claude-11@example.test")],
        dead("claude-11"), {}, (), "account-enabled"),
}


@pytest.mark.parametrize("name", CASES)
def test_c10_8_only_a_lane_disabled_as_auth_dead_is_eligible(name):
    rows, disabled, verdicts, roster_off, why = CASES[name]
    standing = judge(rows, disabled=disabled, verdicts=verdicts, roster_off=roster_off)["claude-11"]
    assert standing.why == why
    assert standing.eligible is (why == AUTH_DEAD)
    assert (standing.next_at is not None) is (why == AUTH_DEAD)
    if not standing.eligible:
        assert choose({"claude-11": standing}, now=T0 + timedelta(days=365), settings=S,
                      started=None) == (None, None)


def test_c10_8_a_verdict_history_that_ever_disqualified_a_lane_keeps_it_off():
    """A duplicate turned off before reasons were recorded, whose running attempt
    later finalized auth-dead, has an auth-dead verdict and an auth-dead event, and
    stays excluded (review of ae3455a1)."""
    standing = judge([lane("claude-11")], disabled=dead("claude-11"),
                     verdicts={"claude-11": {"verdict": "auth-dead"}})["claude-11"]
    assert standing.eligible
    for reason in ("non-canonical", "identity-mismatch"):
        standing = judge([lane("claude-11")], disabled=dead("claude-11"),
                         verdicts={"claude-11": {"verdict": "auth-dead"}})
        excluded = standings([lane("claude-11")], disabled=dead("claude-11"),
                             verdicts={"claude-11": {"verdict": "auth-dead"}}, rechecks={}, roster_off=set(),
                             settings=S, disqualified={"claude-11": reason})["claude-11"]
        assert not excluded.eligible and excluded.why == reason


def test_c10_8_a_restore_that_does_not_hold_carries_its_streak_to_the_successor():
    """The schedule belongs to the credential: a successor disabled as auth-dead
    within a day of its restore starts one failure further on."""
    ref = "claude-quota-claude-11@example.test"
    restored_at = T0 + timedelta(days=1)
    rows = [lane("claude-11"), lane("claude-18", ref=ref, at=restored_at)]
    rechecks = {"claude-11": {"at": iso(restored_at), "result": "restored", "failures": 0, "streak": 2,
                              "successor": "claude-18"}}
    for held, streak in ((timedelta(minutes=1), 3), (timedelta(hours=23), 3), (timedelta(days=1, minutes=1), 0)):
        disabled_at = restored_at + held
        current = judge(rows, disabled=dead("claude-11") | dead("claude-18", disabled_at), rechecks=rechecks)
        standing = current["claude-18"]
        assert standing.eligible and standing.failures == streak
        wait = backoff_s(streak, S)
        assert standing.next_at == iso(disabled_at + timedelta(seconds=wait + jitter_s("claude-18", wait)))
        assert current["claude-11"].why == "superseded"


def test_c10_8_enabled_lanes_have_no_standing():
    assert judge([lane("claude-1", enabled=True)]) == {}


# --- choosing ---------------------------------------------------------------------


def due_pair():
    rows = [lane("claude-11"), lane("claude-12")]
    return judge(rows, disabled=dead("claude-11") | dead("claude-12"))


def test_c10_8_one_lane_at_a_time_the_most_overdue_first():
    current = due_pair()
    now = T0 + timedelta(days=2)
    first = min(current.values(), key=lambda s: (s.next_at, s.lane_id)).lane_id
    assert choose(current, now=now, settings=S, started=None) == (first, None)


def test_c10_8_nothing_due_is_no_decision():
    assert choose(due_pair(), now=T0 + timedelta(hours=1), settings=S, started=None) == (None, None)


def test_c10_8_spacing_between_any_two_rechecks():
    now = T0 + timedelta(days=2)
    assert choose(due_pair(), now=now, settings=S, started=now - timedelta(seconds=S.spacing_s - 1)) == (None, "spacing")
    assert choose(due_pair(), now=now, settings=S, started=now - timedelta(seconds=S.spacing_s))[0]


def test_c10_8_no_free_slot_always_defers():
    now = T0 + timedelta(days=30)
    assert choose(due_pair(), now=now, settings=S, started=None, busy="4 of 4 attempt slots in use") == (
        None, "4 of 4 attempt slots in use")


def test_c10_8_host_load_defers_until_a_lane_is_overdue_by_the_ceiling():
    current = due_pair()
    first = min(current.values(), key=lambda s: (s.next_at, s.lane_id))
    due = recheck._instant(first.next_at)
    lane_id, why = choose(current, now=due + timedelta(hours=1), settings=S, started=None, load_per_cpu=9.0)
    assert lane_id is None and why.startswith("host load 9.0")
    assert choose(current, now=due + timedelta(seconds=S.max_interval_s), settings=S, started=None,
                  load_per_cpu=9.0) == (first.lane_id, None)
    assert choose(current, now=due + timedelta(hours=1), settings=S, started=None,
                  load_per_cpu=S.max_load_per_cpu) == (first.lane_id, None)


# --- the bounds, over every schedule --------------------------------------------


def simulate(steps, lanes_n, outcomes, busy_flags, loads, *, regular=False):
    """Run the decision over a schedule, applying each chosen re-check at once.

    Returns the starts per lane (with the failures in a row before each), the
    global list of starts, and the lanes that were never eligible."""
    ids = [f"claude-{11 + i}" for i in range(lanes_n)]
    rows = [lane(i) for i in ids]
    # Half the lanes, from the third on, are off for a reason other than auth-dead.
    disabled = {}
    for n, lane_id in enumerate(ids):
        disabled[lane_id] = {"reason": AUTH_DEAD if n < 2 or n % 2 == 0 else "non-canonical",
                             "source": "test", "at": iso(T0)}
    rechecks, starts, everything = {}, {i: [] for i in ids}, []
    now = T0
    outcome_iter = iter(outcomes)
    for n, step in enumerate(steps):
        now += timedelta(seconds=S.tick_s if regular else step)
        current = judge(rows, disabled=disabled, rechecks=rechecks)
        busy = None if regular else ("full" if busy_flags[n % len(busy_flags)] else None)
        load = None if regular else loads[n % len(loads)]
        lane_id, why = choose(current, now=now, settings=S, started=last_started(rechecks), busy=busy,
                              load_per_cpu=load)
        if lane_id is None:
            continue
        standing = current[lane_id]
        starts[lane_id].append((now, standing.failures, standing.next_at))
        everything.append(now)
        result = next(outcome_iter, "failed")
        failures = standing.failures + (result == "failed")
        rechecks[lane_id] = {"at": iso(now), "result": result, "failures": failures if result != "restored" else 0}
        if result == "restored":
            successor = f"{lane_id}-next"
            rows.append(lane(successor, enabled=True, ref=rows[ids.index(lane_id)]["credential_ref"], at=now))
    return ids, disabled, starts, everything


schedules = dict(
    steps=st.lists(st.integers(min_value=1, max_value=4 * 3600), min_size=1, max_size=400),
    lanes_n=st.integers(min_value=1, max_value=6),
    outcomes=st.lists(st.sampled_from(["failed", "failed", "failed", "interrupted", "restored"]), max_size=60),
    busy_flags=st.lists(st.booleans(), min_size=1, max_size=8),
    loads=st.lists(st.one_of(st.none(), st.floats(min_value=0, max_value=20)), min_size=1, max_size=8),
)


def assert_bounds(ids, disabled, starts, everything):
    for lane_id in ids:
        seen = starts[lane_id]
        if disabled[lane_id]["reason"] != AUTH_DEAD:
            assert seen == [], "a lane off for another reason was re-checked"
            continue
        if seen:
            assert seen[0][0] >= T0 + timedelta(seconds=S.interval_s)
        for (before, _, _), (after, failures, _) in zip(seen, seen[1:]):
            gap = (after - before).total_seconds()
            assert gap >= S.interval_s                       # at most one re-check per interval
            assert gap >= backoff_s(failures, S)             # and backed off after failures
    for before, after in zip(everything, everything[1:]):
        assert (after - before).total_seconds() >= S.spacing_s


@hsettings(max_examples=150, deadline=None)
@given(**schedules)
def test_c10_8_cadence_spacing_and_backoff_hold_for_every_schedule(steps, lanes_n, outcomes, busy_flags, loads):
    assert_bounds(*simulate(steps, lanes_n, outcomes, busy_flags, loads))


@hsettings(max_examples=25, deadline=None)
@given(ticks=st.integers(min_value=300, max_value=3000), lanes_n=st.integers(min_value=1, max_value=6),
       outcomes=st.lists(st.sampled_from(["failed", "interrupted"]), max_size=40))
def test_c10_8_a_due_lane_is_rechecked_promptly_when_nothing_holds_it(ticks, lanes_n, outcomes):
    # Up to ten days of five-minute ticks: long enough for the backoff to reach its ceiling.
    ids, disabled, starts, everything = simulate([0] * ticks, lanes_n, outcomes, [False], [None], regular=True)
    assert_bounds(ids, disabled, starts, everything)
    eligible = [i for i in ids if disabled[i]["reason"] == AUTH_DEAD]
    assert all(starts[i] for i in eligible), "a lane due for a day and more was never re-checked"
    slack = timedelta(seconds=S.tick_s + len(eligible) * S.spacing_s)
    ceiling = timedelta(seconds=S.max_interval_s * (1 + JITTER_FRACTION)) + slack
    for lane_id in eligible:
        for at, _, next_at in starts[lane_id]:
            assert at <= recheck._instant(next_at) + slack
        for (before, _, _), (after, _, _) in zip(starts[lane_id], starts[lane_id][1:]):
            assert after - before <= ceiling            # the backoff stops at a day


# --- reading the records ------------------------------------------------------


def test_c10_8_latest_record_per_lane_takes_the_event_time_when_it_has_none():
    rows = [{"lane_id": "a", "ts": "2026-09-27T01:00:00Z", "data_json": json.dumps({"reason": "x"})},
            {"lane_id": "a", "ts": "2026-09-27T02:00:00Z", "data_json": json.dumps({"reason": "auth-dead"})},
            {"lane_id": None, "ts": "2026-09-27T03:00:00Z", "data_json": "{}"},
            {"lane_id": "a", "ts": "2026-09-27T02:00:01Z", "data_json": "{}"},     # the audit twin
            {"lane_id": "b", "ts": "2026-09-27T04:00:00Z", "data_json": "not json"}]
    assert latest_by_lane(rows) == {"a": {"reason": "auth-dead", "at": "2026-09-27T02:00:00Z"}}


def test_c10_8_last_started_is_the_latest_of_any_lane():
    assert last_started({}) is None
    assert last_started({"a": {"at": "2026-09-27T01:00:00Z"}, "b": {"at": "2026-09-27T03:00:00Z"}}) == \
        datetime(2026, 9, 27, 3, tzinfo=timezone.utc)


def test_c10_8_the_roster_names_the_operators_choices(tmp_path):
    assert roster_disabled(tmp_path) == set()
    (tmp_path / "lanes.json").write_text(json.dumps({"lanes": [
        {"lane_id": "claude-15", "enabled": False}, {"lane_id": "claude-11", "enabled": True},
        {"lane_id": "claude-12"}, {"lane_id": "claude-13", "enabled": 1}, {"lane_id": "claude-14", "enabled": 0},
        {"lane_id": "claude-16", "enabled": None}, {"lane_id": "claude-17", "enabled": "false"}, "junk"]}))
    # Anything but an explicit true, or no `enabled` at all, is the operator's "off".
    assert roster_disabled(tmp_path) == {"claude-15", "claude-14", "claude-16", "claude-17"}
    (tmp_path / "lanes.json").write_text(json.dumps([{"lane_id": "claude-15", "enabled": False}]))
    assert roster_disabled(tmp_path) == {"claude-15"}
    (tmp_path / "lanes.json").write_text("[]")
    assert roster_disabled(tmp_path) == set()
    for unreadable in ("{not json", "", "  \n", json.dumps({"other": []}), json.dumps({"lanes": {}}), "7"):
        (tmp_path / "lanes.json").write_text(unreadable)
        assert roster_disabled(tmp_path) is None, unreadable
    (tmp_path / "lanes.json").unlink()
    (tmp_path / "lanes.json").mkdir()
    assert roster_disabled(tmp_path) is None
