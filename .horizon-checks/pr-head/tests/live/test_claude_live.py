"""C-20.1: the one live Claude probe, opt-in only.

Skipped unless `SUBFLEET_LIVE=1`. It spends one Haiku turn on a real enrolled lane —
seconds of the account's five-hour window — so nothing in CI, and nothing in a lane
run, may set that variable. It exists to catch the one failure the fixtures cannot:
Claude Code changing the shape of the `rate_limit_event` out from under the sensor.

Run it deliberately, with a lane you mean to spend:

    SUBFLEET_LIVE=1 SUBFLEET_LIVE_CLAUDE_ACCOUNT=max@axiom.org \\
        uv run pytest -q tests/live/test_claude_live.py

`SUBFLEET_LIVE_CLAUDE_ACCOUNT` names the account whose keychain item
(`claude-quota-<email>`) is read; without it the test skips rather than guessing which
subscription to spend.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone

import pytest

from subfleet.adapters.claude import (
    ENROLL_MODEL, ENROLL_PROMPT, KEYCHAIN_PREFIX, ClaudeAdapter,
)
from subfleet.contracts import Credential, ReadingLabel

LIVE = os.environ.get("SUBFLEET_LIVE") == "1"
ACCOUNT = os.environ.get("SUBFLEET_LIVE_CLAUDE_ACCOUNT")

pytestmark = [
    pytest.mark.skipif(not LIVE, reason="live tests need SUBFLEET_LIVE=1"),
    pytest.mark.skipif(
        not ACCOUNT, reason="set SUBFLEET_LIVE_CLAUDE_ACCOUNT=<email> to name the lane"
    ),
    pytest.mark.skipif(
        shutil.which("claude") is None, reason="the claude CLI is not on PATH"
    ),
]


@pytest.fixture(scope="module")
def live_lane_info():
    """One real enrolment turn, shared by every assertion below: the whole point of
    the opt-in gate is that this runs at most once."""
    adapter = ClaudeAdapter()
    return adapter, adapter.enroll(
        Credential(provider="claude", ref=f"{KEYCHAIN_PREFIX}{ACCOUNT}",
                   kind="keychain-token")
    )


def test_a_live_haiku_turn_reports_the_account_and_its_windows(live_lane_info):
    """C-10.2, C-9.8 the enrolment turn authenticates, names the account, and returns
    `provider` readings from the live `rate_limit_event`."""
    _adapter, info = live_lane_info
    assert info.account_key == f"claude:{ACCOUNT}"
    assert info.readings, (
        "the live rate_limit_event produced no readings — the event's shape may have "
        "changed; re-run experiment 0 and update claude_stream.py before trusting "
        "Claude capacity again"
    )
    for reading in info.readings:
        assert reading.label is ReadingLabel.PROVIDER
        assert reading.scope == "account"
        assert reading.source == "rate_limit_event"
        assert reading.utilization is not None and reading.utilization >= 0.0
        assert reading.window in ("five_hour", "seven_day", "seven_day_overage_included")
        assert reading.resets_at and reading.resets_at.endswith("Z")
        datetime.fromisoformat(reading.resets_at.replace("Z", "+00:00"))


def test_the_live_turn_reports_both_subscription_windows(live_lane_info):
    """C-9.8 a subscription lane reports the five-hour and seven-day windows; losing
    either would silently halve what the router knows."""
    _adapter, info = live_lane_info
    windows = {reading.window for reading in info.readings}
    assert {"five_hour", "seven_day"} <= windows, windows


def test_the_live_reset_clocks_are_in_the_future(live_lane_info):
    """C-1.7, C-9.4 `resetsAt` epoch seconds convert to a clock that has not passed;
    a past clock would mean the epoch conversion drifted."""
    _adapter, info = live_lane_info
    now = datetime.now(timezone.utc)
    for reading in info.readings:
        resets = datetime.fromisoformat(reading.resets_at.replace("Z", "+00:00"))
        assert resets > now, f"{reading.window} resets at {reading.resets_at}"


def test_the_probe_command_is_the_one_experiment_zero_ran():
    """C-10.2 the live command is checked without spending a turn: the argv this
    adapter builds is byte for byte the one experiment 0 recorded."""
    adapter = ClaudeAdapter(claude_bin="claude")
    assert adapter._turn_argv(ENROLL_MODEL) == [
        "claude", "-p", ENROLL_PROMPT, "--model", ENROLL_MODEL,
        "--output-format", "stream-json", "--verbose", "--max-turns", "1",
    ]
