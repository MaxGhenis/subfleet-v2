"""C-10.6, C-10.9: verdicts, recording and twins at their edges (review of #159, findings 2, 3, 12, 13).

Written by the independent review of PR #159 (Subfleet job
20261009-100051-pr159-review-r1, GPT-6.1 Sol), each a failing reproduction of one
finding before its fix; kept as regressions. Finding numbers are the review's.
"""

from datetime import datetime, timedelta, timezone
from itertools import combinations

import pytest

from subfleet import lane_identity as li
from subfleet.contracts import Credential, IdentityStatus, Lane, LaneOwner
from subfleet.store import Store
from subfleet.timers import Timers


def test_disagreeing_profile_facts_do_not_latch_a_third_label_mismatch():
    """C-10.6: disagreements decide nothing, regardless of the lane's label."""
    facts = [li.AccountFact(email, "account:org-id", "claude_max", f"login:{email}")
             for email in ("a@example.test", "b@example.test")]
    status, verdict = li.judge(IdentityStatus.ENROLLED, label="c@example.test",
                               identity="org:org-id", facts=facts)
    assert verdict.verdict == "unproven"
    assert status is IdentityStatus.ENROLLED


def test_home_lane_profile_keeps_organization_type_for_setup_token_labels(tmp_path):
    """C-10.6: a home lane's personal profile can judge setup-token labels."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane = Lane("claude-1", "claude", "claude:account:org-id",
                    Credential("claude", str(tmp_path / "home"), "home"),
                    str(tmp_path / "home"), LaneOwner.V2, False, True, None, "a@example.test")
        store.put_lane(lane, identity_status="enrolled")
        finding = {"status": "verified", "source": "profile", "observed": "account:org-id",
                   "org_type": "claude_max", "identity": {"email": "a@example.test",
                   "account_uuid": "account", "org_uuid": "org-id"}}
        assert li.record(store, lane.lane_id, finding)
        timers = Timers(store, tmp_path, {"timers": {}})
        try:
            facts = timers.identity_facts()
            status, verdict = li.judge(IdentityStatus.ENROLLED, label="b@example.test",
                                       identity="org:org-id", facts=facts)
            assert verdict.contradicted
            assert status is IdentityStatus.MISMATCH
        finally:
            timers.stop()


def test_two_hour_aligned_weekly_scopes_do_not_count_as_five_hour_and_weekly():
    """C-10.9: a scoped weekly window is not a matching five-hour window."""
    lanes = [{"lane_id": lane, "provider": "claude", "identity": None, "owner": "v2", "enabled": True}
             for lane in ("claude-1", "claude-2")]
    readings = [{"lane_id": lane["lane_id"], "scope": scope, "window": "seven_day", "label": "provider",
                 "utilization": .5, "resets_at": "2026-10-10T14:00:00Z",
                 "observed_at": "2026-10-09T12:00:00Z"}
                for lane in lanes for scope in ("account", "claude-sonnet-4-6")]
    assert li.reading_twins(lanes, readings) == []


def test_stale_first_read_cannot_bind_another_accounts_windows(tmp_path):
    """C-10.6 a concurrent first read must be checked against the now-recorded binding (finding 2)."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane = Lane("claude-1", "claude", "claude:a@example.test",
                    Credential("claude", "unused-synthetic-credential", "env"),
                    None, LaneOwner.V2, False, True, None, "a@example.test")
        store.put_lane(lane, identity_status="enrolled")
        first = {"status": "identity-enrolled", "source": "org-header", "observed": "org:org-a"}
        stale = {"status": "identity-enrolled", "source": "org-header", "observed": "org:org-b"}
        assert li.record(store, lane.lane_id, first)
        assert not li.record(store, lane.lane_id, stale)
        row = store.one("SELECT identity,identity_status FROM lanes WHERE lane_id=?", (lane.lane_id,))
        assert row["identity"] == "org:org-a"
        assert row["identity_status"] == "mismatch"


@pytest.mark.parametrize("threshold", [.25, 1., 60., 180.])
def test_bucketed_matches_equal_independent_brute_force_at_boundaries(threshold):
    """C-10.9 sweep correctness across custom widths, negative epochs and exact edges."""
    settings = li.TwinSettings(within_s=threshold, reset_tolerance_s=threshold,
                               utilization_tolerance=.01, min_values=3)
    providers = {"a": "claude", "b": "claude", "c": "codex"}
    # Keep this edge isolated: denser samples can produce the same set entry
    # through an interior pair and conceal a rejected boundary match.
    edge = [li._Sample("a", "account", "five_hour", .5, 0., 0.),
            li._Sample("b", "account", "five_hour", .5, threshold, 0.)]
    assert dict(li._matches(edge, providers, settings)) == {
        ("a", "b"): {("account", "five_hour", .5)}}
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    samples = [li._Sample(lane, scope, window, value, reset, observed)
               for lane in providers
               for scope, window in (("account", "five_hour"), ("account", "seven_day"),
                                     ("claude-sonnet-4-6", "seven_day"))
               for value in (.4, .405, .41)
               for reset, observed in ((-threshold, 0.), (0., threshold),
                                        (threshold / 2, threshold / 2),
                                        (threshold + .001, threshold + .001))]
    expected = {}
    for first, second in combinations(samples, 2):
        if first.lane_id == second.lane_id or providers[first.lane_id] != providers[second.lane_id]:
            continue
        if (first.scope, first.window) != (second.scope, second.window):
            continue
        if (abs(first.reset - second.reset) > threshold or abs(first.observed - second.observed) > threshold
                or abs(first.utilization - second.utilization) > settings.utilization_tolerance):
            continue
        key = tuple(sorted((first.lane_id, second.lane_id)))
        expected.setdefault(key, set()).add((first.scope, first.window,
                                            round((first.utilization + second.utilization) / 2, 2)))
    assert dict(li._matches(samples, providers, settings)) == expected
    readings = [{"lane_id": sample.lane_id, "scope": sample.scope, "window": sample.window,
                 "label": "provider", "utilization": sample.utilization,
                 "resets_at": (epoch + timedelta(seconds=sample.reset)).isoformat(),
                 "observed_at": (epoch + timedelta(seconds=sample.observed)).isoformat()}
                for sample in samples]
    rebuilt = li._samples(readings, providers)
    assert dict(li._matches(rebuilt, providers, settings)) == expected
