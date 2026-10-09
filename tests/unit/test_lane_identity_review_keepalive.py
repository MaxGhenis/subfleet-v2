"""C-10.8: the keepalive reservation judges sharing under the lock (review of #159, finding 10).

Written by the independent review of PR #159 (Subfleet job
20261009-100051-pr159-review-r1, GPT-6.1 Sol), each a failing reproduction of one
finding before its fix; kept as regressions. Finding numbers are the review's.
"""

from datetime import datetime, timezone

from subfleet import lane_identity
from subfleet.contracts import Credential, Lane, LaneOwner, Outcome, OutcomeClass
from subfleet.store import Store
from subfleet.timers import Timers


def test_keepalive_reservation_rechecks_sharing_after_the_cycle_snapshot(tmp_path, monkeypatch):
    """C-10.8: a probe may learn a peer's org while the keepalive worker looks up
    its lane's latest request, after the cycle selected its candidate lanes.
    The reservation must refuse a lane that became shadowed during that gap.
    """
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    policy = {
        "models": {"haiku": {"id": "claude-haiku-4-5-20251001", "provider": "claude"}},
        "timers": {}, "reset_credits": {"enabled": False}, "alerts": {},
        "sessions": {"mirror_interval_s": 0},
    }
    with Store(tmp_path / "state.sqlite3") as store:
        lanes = []
        for number, identity in ((1, None), (2, "org:shared")):
            lane = Lane(f"claude-{number}", "claude", f"claude:lane-{number}@example.com",
                        Credential("claude", f"UNUSED_REVIEW_TOKEN_{number}", "env"),
                        None, LaneOwner.V2, False, True, identity, f"lane-{number}@example.com")
            store.put_lane(lane, identity_status="enrolled")
            lanes.append(lane)
        timer = Timers(store, tmp_path, policy, now=lambda: now)
        turned = []

        def latest_request(lane_id):
            if lane_id == "claude-1":
                return now  # Its keepalive is unnecessary in this scenario.
            # A concurrent probe cycle publishes its completed read here. Probe
            # and keepalive cycles have separate overlap guards and workers.
            assert "claude-2" not in lane_identity.shadowing(store.lane_rows())
            timer._persist(lanes[0], {"status": "ok", "readings": (), "identity": {
                "status": "identity-enrolled", "observed": "org:shared", "source": "models"}})
            assert lane_identity.shadowing(store.lane_rows())["claude-2"]["shadowed_by"] == "claude-1"
            return None

        monkeypatch.setattr(timer, "latest_request", latest_request)
        timer.turn = lambda lane, *args, **kwargs: turned.append(lane.lane_id) or Outcome(OutcomeClass.OK, "answered")
        try:
            timer.keepalive_cycle()
            assert lane_identity.shadowing(store.lane_rows())["claude-2"]["shadowed_by"] == "claude-1"
            assert turned == []
        finally:
            timer.stop()
