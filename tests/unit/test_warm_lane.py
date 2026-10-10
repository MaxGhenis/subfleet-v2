"""C-6.19: waiting for the lane whose prompt cache holds a session, or moving."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, strategies as st

from subfleet.scheduler import CACHE_TTL_S, cache_until, warm_reopen, warm_verdict

NOW = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def at(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)


def limit(until: datetime, scope: str = "account", *, reason: str = "provider-limit", clock: str = "reported") -> dict:
    return {"lane_id": "claude-3", "scope": scope, "until_at": iso(until), "reason": reason, "clock_source": clock}


def reading(utilization: float, resets_at: datetime | None, window: str = "five_hour") -> dict:
    return {"lane_id": "claude-3", "scope": "account", "window": window, "utilization": utilization,
            "resets_at": iso(resets_at) if resets_at else None, "label": "provider",
            "observed_at": iso(NOW - timedelta(minutes=1))}


def reopen(reasons, readings=(), closures=()):
    return warm_reopen(reasons, readings, closures, now=NOW, floor=.15, reading_ttl_s=900)


def test_the_measured_lifetime_is_one_hour():
    """C-6.19 every Claude attempt that wrote cache from 2026-09-19 to 2026-10-04 wrote
    one-hour entries only, so a one-hour wait is the longest that can find the cache alive."""
    assert CACHE_TTL_S == {"1h": 3600, "5m": 300, "mixed": 300}


def test_a_limit_closure_reopens_at_its_reported_end():
    """C-6.19 a provider limit with a reported clock ends at its `until_at`."""
    row = limit(at(18))
    assert reopen([f"closed:account:{row['until_at']}"], closures=[row]) == iso(at(18))


def test_only_a_reported_provider_limit_has_a_known_end():
    """C-6.19 a guessed clock, an operator hold, a cooldown, auth, or an exclusion
    is no reopen time the person or the provider gave."""
    for row in (limit(at(18), clock="guessed"), limit(at(18), reason="operator-hold"),
                limit(at(18), reason="cooldown"), limit(at(18), reason="auth-dead")):
        assert reopen([f"closed:account:{row['until_at']}"], closures=[row]) is None
    row = limit(at(18))
    assert reopen(["excluded", f"closed:account:{row['until_at']}"], closures=[row]) is None
    assert reopen(["no-slot"]) is None
    assert reopen([]) is None


def test_below_the_floor_reopens_when_every_window_over_it_resets():
    """C-6.19 `below-floor` ends when each fresh window at or over the floor resets;
    a window with no reset time leaves the end unknown."""
    rows = [reading(.9, at(40)), reading(.95, at(30), window="seven_day"), reading(.2, at(500), window="other")]
    assert reopen(["below-floor"], readings=rows) == iso(at(40))
    assert reopen(["below-floor"], readings=[reading(.9, None)]) is None


def test_the_latest_end_is_the_reopen_time():
    """C-6.19 a lane closed for two reasons is closed until both end."""
    row = limit(at(10))
    assert reopen([f"closed:account:{row['until_at']}", "below-floor"], readings=[reading(.9, at(25))],
                  closures=[row]) == iso(at(25))


def test_the_replayed_cases():
    """C-6.19 the 2026-09-28 failovers: a warm lane that reopened in 18 minutes while
    the cache (last used 2 minutes earlier, one-hour entries) was alive is worth the
    wait; one that reopened in 63 minutes outlived the cache, so the turn moved
    for nothing lost."""
    cache = cache_until(at(-2), "1h")
    waited = warm_verdict(reopens_at=at(18), now=NOW, wait_since=None, warm_wait_s=3600, cache_until=cache)
    assert waited["action"] == "wait" and waited["next_check_at"] == iso(at(18))
    moved = warm_verdict(reopens_at=at(63), now=NOW, wait_since=None, warm_wait_s=3600, cache_until=cache)
    assert moved == {"action": "move", "why": "cache-expires-first", "reopens_at": iso(at(63)),
                     "deadline": iso(at(58)), "wait_since": iso(NOW), "cache_until": iso(at(58))}


def test_an_unknown_lifetime_leaves_only_the_bound():
    """C-6.19 with no measured cache lifetime the bound alone decides."""
    assert cache_until(at(-2), None) is None and cache_until(None, "1h") is None
    assert warm_verdict(reopens_at=at(50), now=NOW, wait_since=None, warm_wait_s=3600,
                        cache_until=None)["action"] == "wait"
    assert warm_verdict(reopens_at=at(61), now=NOW, wait_since=None, warm_wait_s=3600,
                        cache_until=None)["why"] == "past-bound"


def test_a_wait_counts_from_when_it_began():
    """C-6.19 a job that has waited 50 minutes of a 60-minute bound does not begin again."""
    verdict = warm_verdict(reopens_at=at(20), now=NOW, wait_since=at(-50), warm_wait_s=3600, cache_until=None)
    assert verdict["action"] == "move" and verdict["why"] == "past-bound"


def test_off_and_unknown_reopen():
    """C-6.19 null turns the rule off; a lane with no known reopen is left at once."""
    assert warm_verdict(reopens_at=at(1), now=NOW, wait_since=None, warm_wait_s=None,
                        cache_until=None) == {"action": "off"}
    assert warm_verdict(reopens_at=None, now=NOW, wait_since=None, warm_wait_s=3600,
                        cache_until=None)["why"] == "no-known-reopen"


minutes = st.integers(min_value=-600, max_value=600)
optional_minutes = st.none() | minutes
bounds = st.none() | st.integers(min_value=0, max_value=36000)


@given(reopen_m=optional_minutes, since_m=st.none() | st.integers(min_value=-600, max_value=0),
       bound=bounds, cache_m=optional_minutes)
def test_no_job_waits_past_the_bound(reopen_m, since_m, bound, cache_m):
    """C-6.19 for all inputs: a wait has now < deadline <= wait_since + bound and
    deadline <= cache_until, and its next look is no later than the deadline, so
    the job is seen again before the bound passes; the verdict is deterministic."""
    kwargs = dict(reopens_at=None if reopen_m is None else at(reopen_m), now=NOW,
                  wait_since=None if since_m is None else at(since_m), warm_wait_s=bound,
                  cache_until=None if cache_m is None else at(cache_m))
    verdict = warm_verdict(**kwargs)
    assert verdict == warm_verdict(**kwargs)
    if bound is None:
        assert verdict == {"action": "off"}
        return
    assert verdict["action"] in ("wait", "move")
    if verdict["action"] == "wait":
        since = at(since_m) if since_m is not None else NOW
        deadline = verdict["deadline"]
        assert iso(NOW) < deadline <= iso(since + timedelta(seconds=bound))
        if cache_m is not None:
            assert deadline <= iso(at(cache_m))
        assert iso(NOW) <= verdict["next_check_at"] <= deadline
        assert verdict["reopens_at"] <= deadline


@given(reopen_m=minutes, since_m=st.integers(min_value=-600, max_value=0),
       small=st.integers(min_value=0, max_value=18000), extra=st.integers(min_value=0, max_value=18000),
       cache_m=optional_minutes)
def test_a_longer_bound_never_turns_a_wait_into_a_move(reopen_m, since_m, small, extra, cache_m):
    """C-6.19 monotone in the bound: whatever waits under a bound waits under a longer one."""
    common = dict(reopens_at=at(reopen_m), now=NOW, wait_since=at(since_m),
                  cache_until=None if cache_m is None else at(cache_m))
    if warm_verdict(warm_wait_s=small, **common)["action"] == "wait":
        assert warm_verdict(warm_wait_s=small + extra, **common)["action"] == "wait"


@given(ends=st.lists(st.integers(min_value=1, max_value=600), min_size=1, max_size=4),
       noise=st.sampled_from(["excluded", "no-slot", "disabled", "desktop", "reserve:fable:reserved"]))
def test_any_reason_without_an_end_leaves_the_reopen_unknown(ends, noise):
    """C-6.19 for all sets of limit closures: the reopen is their latest end, and one
    more reason with no known end makes it unknown."""
    rows = [limit(at(m), scope=f"s{i}") for i, m in enumerate(ends)]
    reasons = [f"closed:{row['scope']}:{row['until_at']}" for row in rows]
    assert reopen(reasons, closures=rows) == iso(at(max(ends)))
    assert reopen([*reasons, noise], closures=rows) is None
