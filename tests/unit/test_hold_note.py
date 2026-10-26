"""C-9.6, D-RAMP-HOLD-LABEL: a six-hour safety bound is an operator hold."""

from subfleet.alerts import evaluate_conditions
from subfleet.cli import format_status


def test_six_hour_hold_status_says_held_with_latest_bound():
    """C-9.6: status must not describe the ramp's safety bound as a provider reset."""
    lane = {"lane_id": "codex-1", "provider": "codex", "home": "/homes/codex-1",
            "owner": "v2", "enabled": True, "closures": [
                {"scope": "account", "reason": "operator-hold", "until_at": "2026-10-26T22:55:00Z"}]}
    alerts = evaluate_conditions({"lanes": [lane]}, now="2026-10-26T16:55:00Z")
    text = format_status({"now": "2026-10-26T16:55:00Z", "lanes": [lane], "alerts": alerts})
    assert "/homes/codex-1: held by the operator (until 2026-10-26T22:55:00Z at the latest)" in text
    assert "closed (operator-hold); resets" not in text
