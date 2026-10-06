"""Weekly-quota examples and invariants, over provider readings only."""

from datetime import datetime, timedelta, timezone

from hypothesis import given, strategies as st
import pytest

from subfleet.quota_projection import project_unused

NOW = datetime(2026, 10, 5, 21, 12, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=3)
FRACTIONS = st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False)


def sample(observed_at, utilization, **extra):
    return {"observed_at": observed_at, "utilization": utilization, "resets_at": RESET,
            "window": "seven_day", "label": "provider", **extra}


@given(st.lists(st.tuples(st.integers(0, 10 * 86400), FRACTIONS), min_size=1, max_size=30))
def test_projected_unused_is_bounded(values):
    result = project_unused([(NOW - timedelta(seconds=age), u) for age, u in values], RESET, NOW)
    assert result is not None
    assert 0 <= result.projected_unused <= 1 - result.used


@given(FRACTIONS, st.integers(1, 24), FRACTIONS)
def test_zero_rate_leaves_all_remaining_quota_unused(used, span, drop):
    previous = used + (1 - used) * drop
    result = project_unused([(NOW - timedelta(hours=span), previous), (NOW, used)], RESET, NOW)
    assert result.rate_per_hour == 0
    assert result.projected_unused == 1 - used


@given(FRACTIONS, st.integers(0, 3599))
def test_unknown_rate_leaves_all_remaining_quota_unused(used, span):
    result = project_unused([(NOW - timedelta(seconds=span), 0), (NOW, used)], RESET, NOW)
    assert result.basis == "rate unknown"
    assert result.rate_per_hour is None
    assert result.used == used
    assert result.projected_unused == 1 - used


@given(FRACTIONS, FRACTIONS, FRACTIONS)
def test_higher_rate_never_gives_more_unused(used, first, second):
    start_low, start_high = sorted((used * first, used * second))
    higher = project_unused([(NOW - timedelta(hours=24), start_low), (NOW, used)], RESET, NOW)
    lower = project_unused([(NOW - timedelta(hours=24), start_high), (NOW, used)], RESET, NOW)
    assert higher.rate_per_hour >= lower.rate_per_hour
    assert higher.projected_unused <= lower.projected_unused


@given(FRACTIONS, FRACTIONS, st.integers(0, 12 * 3600))
def test_later_now_same_rate_never_gives_more_unused(used, first, advance):
    samples = [(NOW - timedelta(hours=12), used * first), (NOW, used)]
    before = project_unused(samples, RESET, NOW)
    after = project_unused(samples, RESET, NOW + timedelta(seconds=advance))
    assert after.rate_per_hour == before.rate_per_hour
    assert after.projected_unused <= before.projected_unused


@given(st.lists(st.tuples(st.integers(0, 24 * 3600), FRACTIONS), min_size=1, max_size=20), st.data())
def test_deterministic_and_independent_of_sample_order(values, data):
    samples = [(NOW - timedelta(seconds=age), u) for age, u in values]
    expected = project_unused(samples, RESET, NOW)
    assert project_unused(samples, RESET, NOW) == expected
    assert project_unused(data.draw(st.permutations(samples)), RESET, NOW) == expected


@given(FRACTIONS, st.lists(st.tuples(st.integers(0, 24 * 3600), FRACTIONS), max_size=20),
       st.integers(min_value=1, max_value=7 * 86400))
def test_other_reset_windows_never_affect_projection(used, foreign, reset_offset):
    current = [sample(NOW - timedelta(hours=12), used / 2), sample(NOW, used)]
    others = [sample(NOW - timedelta(seconds=age), u,
                     resets_at=RESET + timedelta(seconds=reset_offset)) for age, u in foreign]
    assert project_unused(current + others, RESET, NOW) == project_unused(current, RESET, NOW)


def test_three_days_to_reset_first_to_last_trend():
    result = project_unused([(NOW - timedelta(hours=24), .29333333333333333), (NOW, .34)], RESET, NOW)
    assert result.used == .34
    assert result.rate_per_hour == pytest.approx(.14 / 72)
    assert result.projected_unused == pytest.approx(.52)
    assert result.basis == "trend"


def test_negative_rate_is_zero():
    result = project_unused([(NOW - timedelta(hours=24), .8), (NOW, .34)], RESET, NOW)
    assert result.rate_per_hour == 0
    assert result.projected_unused == 1 - .34


def test_other_reset_window_does_not_supply_a_rate():
    rows = [sample(NOW, .34), sample(NOW - timedelta(hours=24), 0,
                                   resets_at=RESET + timedelta(days=7))]
    result = project_unused(rows, RESET, NOW)
    assert result.basis == "rate unknown"
    assert result.projected_unused == 1 - .34


def test_clamp_when_trend_would_exhaust_quota():
    result = project_unused([(NOW - timedelta(hours=1), 0), (NOW, .8)], RESET, NOW)
    assert result.projected_unused == 0


@pytest.mark.parametrize("at", [RESET, RESET + timedelta(seconds=1)])
def test_expired_reset_has_no_projection(at):
    assert project_unused([(NOW, .34)], RESET, at) is None


def test_empty_or_future_samples_have_no_projection():
    assert project_unused([], RESET, NOW) is None
    assert project_unused([(NOW + timedelta(seconds=1), .34)], RESET, NOW) is None


def test_trailing_24_hours_excludes_older_samples_but_keeps_current_usage():
    latest = (NOW, .34)
    assert project_unused([(NOW - timedelta(hours=25), 0), latest], RESET, NOW).basis == "rate unknown"
    old = project_unused([(NOW - timedelta(hours=26), .3)], RESET, NOW)
    assert old.used == .3 and old.basis == "rate unknown"


def test_one_hour_span_is_enough_and_duplicates_do_not_weight_rate():
    samples = [(NOW - timedelta(hours=1), .3), (NOW, .34), (NOW, .34)]
    assert project_unused(samples, RESET, NOW).rate_per_hour == pytest.approx(.04)


@pytest.mark.parametrize("label", ["admission-observed", "local-backoff", "unknown"])
def test_other_evidence_labels_do_not_supply_usage_or_rate(label):
    assert project_unused([sample(NOW, .34, label=label)], RESET, NOW) is None


@pytest.mark.parametrize("used", [None, True, -1, 2, float("nan"), float("inf")])
def test_invalid_utilization_is_ignored(used):
    assert project_unused([sample(NOW, used)], RESET, NOW) is None


def test_equivalent_reset_instants_and_stale_provider_are_accepted():
    row = sample(NOW, .34, label="stale-provider", resets_at="2026-10-08T17:12:00-04:00")
    assert project_unused([row], RESET, NOW).used == .34


def test_nonweekly_samples_are_ignored():
    assert project_unused([sample(NOW, .34, window="five_hour")], RESET, NOW) is None


def test_window_identity_uses_provider_reset_without_inventing_a_start():
    # Provider reset identity defines the window; old readings still supply
    # current utilization when too old to establish a trailing-24-hour rate.
    result = project_unused([(RESET - timedelta(days=8), .34)], RESET, NOW)
    assert result.used == .34 and result.basis == "rate unknown"
    assert result.projected_unused == 1 - .34
    assert project_unused([(NOW, .34)], NOW + timedelta(days=10), NOW).used == .34
