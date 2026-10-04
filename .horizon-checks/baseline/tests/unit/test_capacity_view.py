"""Capacity snapshots: C-6.4, C-9.1, C-9.6, C-10.3, C-10.4."""

import copy
import json

import pytest

from subfleet.capacity import build_view, owned_lanes, read_desktop_account
from subfleet.contracts import Reading, ReadingLabel

NOW = "2026-09-05T10:33:00Z"


def lane(identity="claude-1", **values):
    return {"lane_id": identity, "provider": identity.split("-")[0],
            "account_key": "claude:first@example.org", "owner": "v2", "desktop": False, **values}


def reading(**values):
    return {"lane_id": "claude-1", "scope": "account", "window": "seven_day",
            "utilization": 0.4, "resets_at": "2026-09-06T00:00:00Z", "label": "provider",
            "source": "rate_limit_event", "observed_at": NOW, **values}


def test_latest_reading_selected_per_lane_scope_window_and_id():
    """C-9.1: source, age, and the latest evidence survive each independent key."""
    rows = [reading(reading_id=2, utilization=0.2), reading(reading_id=1, utilization=0.1),
            reading(observed_at="2026-09-05T10:30:00Z", utilization=0.9),
            reading(scope="claude-fable-5-1", utilization=0.7), reading(window="five_hour"),
            reading(lane_id="claude-2")]
    view = build_view([lane(), lane("claude-2")], rows, now=NOW)
    assert len(view["readings"]) == 4
    selected = next(row for row in view["readings"] if row["lane_id"] == "claude-1"
                    and row["scope"] == "account" and row["window"] == "seven_day")
    assert selected["utilization"] == 0.2
    assert selected["source"] == "rate_limit_event"
    assert selected["age_s"] == 0


@pytest.mark.parametrize("age, label, measured", [(120, "provider", True), (121, "stale-provider", False)])
def test_staleness_boundary(age, label, measured):
    """C-6.4, C-9.1: evidence beyond the TTL stays numeric but loses measured slots."""
    observed = "2026-09-05T10:31:00Z" if age == 120 else "2026-09-05T10:30:59Z"
    view = build_view([lane()], [reading(observed_at=observed)], now=NOW)
    assert view["readings"][0]["label"] == label
    assert view["readings"][0]["age_s"] == age
    assert view["readings"][0]["utilization"] == 0.4
    assert view["lanes"][0]["measured"] is measured


@pytest.mark.parametrize("label", ["admission-observed", "local-backoff", "unknown", "stale-provider"])
def test_nonprovider_readings_never_make_a_measured_lane(label):
    """C-6.4, C-9.1: admission and backoff evidence cannot supply measured capacity."""
    view = build_view([lane()], [reading(label=label)], now=NOW)
    assert not view["lanes"][0]["measured"]


def test_reset_window_no_longer_counts_as_measured():
    """C-6.4, C-9.1: an elapsed reset cannot stand in for current quota evidence."""
    view = build_view([lane()], [reading(resets_at=NOW)], now=NOW)
    assert not view["lanes"][0]["measured"]


def test_only_active_unreleased_closures_visible():
    """C-9.6: account and model closures persist until their own expiry clocks."""
    base = {"lane_id": "claude-1", "scope": "account", "reason": "provider-limit",
            "clock_source": "reported", "source_event": "e1", "until_at": "2026-09-06T00:00:00Z"}
    closures = [base, {**base, "scope": "claude-fable-5-1"},
                {**base, "until_at": NOW}, {**base, "released_at": NOW}]
    view = build_view([lane()], closures=closures, now=NOW)
    assert [row["scope"] for row in view["closures"]] == ["account", "claude-fable-5-1"]
    assert view["lanes"][0]["closures"] == view["closures"]


def test_in_flight_comes_only_from_active_attempts(monkeypatch):
    """C-4.2, C-5.7, C-6.4: recorded active attempts count slots; quarantine does not."""
    def forbidden(*args, **kwargs):
        raise AssertionError("capacity must not inspect processes")
    monkeypatch.setattr("subprocess.run", forbidden)
    states = ["reserved", "starting", "running", "finalizing", "succeeded", "failed", "lost", "quarantined"]
    attempts = [{"attempt_id": f"j/a{i}", "lane_id": "claude-1", "state": state}
                for i, state in enumerate(states)]
    view = build_view([lane(), lane("claude-2")], attempts=attempts,
                      jobs=[{"job_id": "another-job", "state": "running", "lane_id": "claude-2"}], now=NOW)
    assert view["in_flight"] == {"claude-1": 4, "claude-2": 0}
    assert next(row for row in view["lanes"] if row["lane_id"] == "claude-1")["in_flight"] == 4


def test_desktop_login_refreshed_each_read_without_changing_input(tmp_path):
    """C-10.3: account switches refresh every Claude lane bound to that login."""
    path = tmp_path / ".claude.json"
    lanes = [lane(), lane("claude-2", account_key="claude:second@example.org", desktop=True),
             lane("codex-1", account_key="codex:first@example.org")]
    before = copy.deepcopy(lanes)
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": "FIRST@example.org"}}))
    first = build_view(lanes, now=NOW, desktop_account=read_desktop_account(path))
    assert {row["lane_id"]: row["desktop"] for row in first["lanes"]} == {
        "claude-1": True, "claude-2": False, "codex-1": False}
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": "second@example.org"}}))
    second = build_view(lanes, now=NOW, desktop_account=read_desktop_account(path))
    assert {row["lane_id"]: row["desktop"] for row in second["lanes"]} == {
        "claude-1": False, "claude-2": True, "codex-1": False}
    assert lanes == before


@pytest.mark.parametrize("content", [None, "invalid JSON", "{}", '{"oauthAccount": null}', "[]"])
def test_unknown_desktop_login_preserves_protection(tmp_path, content):
    """C-10.3: unavailable login identity never clears a recorded desktop flag."""
    path = tmp_path / ".claude.json"
    if content is not None:
        path.write_text(content)
    account = read_desktop_account(path)
    assert account is None
    assert build_view([lane(desktop=True)], now=NOW, desktop_account=account)["lanes"][0]["desktop"]


def test_owner_filter_keeps_v1_visible_for_rejection_evidence():
    """C-10.4, C-11.5: owner filtering excludes v1 without hiding its lane from status."""
    view = build_view([lane(), lane("claude-2", owner="v1")], now=NOW)
    assert [row["lane_id"] for row in owned_lanes(view)] == ["claude-1"]
    assert len(view["lanes"]) == 2


def test_reading_dataclass_accepted_and_inputs_unchanged():
    """C-9.1: contract Reading objects and store rows share one capacity view."""
    item = Reading(**{**reading(), "label": ReadingLabel.PROVIDER})
    view = build_view([lane()], [item], now=NOW)
    assert view["readings"][0]["label"] == "provider"
    assert view["lanes"][0]["measured"]


def test_capacity_lane_order_is_codex_weekly_reset_waterfall():
    """C-11.3: status consumers see weekly-reset order, unaffected by in-flight."""
    lanes = [lane("codex-1"), lane("codex-2"), lane("codex-3"), lane()]
    rows = [reading(lane_id="codex-1", utilization=0.4, resets_at="2026-09-11T00:00:00Z"),
            reading(lane_id="codex-2", utilization=0.6, resets_at="2026-09-06T00:00:00Z")]
    view = build_view(lanes, rows, attempts=[{"lane_id": "codex-2", "state": "running"}], now=NOW)
    assert [row["lane_id"] for row in view["lanes"]] == ["codex-2", "codex-1", "codex-3", "claude-1"]


def test_desktop_login_is_parsed_only_when_the_file_changes(tmp_path, monkeypatch):
    """C-10.3 every call looks at the file; only a changed file is parsed again, and a switch is seen at once."""
    import os
    from subfleet import capacity
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": "first@example.org"}}))
    parses = []
    real = capacity._login_email
    monkeypatch.setattr(capacity, "_login_email", lambda text: parses.append(1) or real(text))
    assert [read_desktop_account(path) for _ in range(5)] == ["first@example.org"] * 5
    assert len(parses) == 1
    # The hardest switch to see: rewritten in place, the same length, its mtime put back.
    # Only the inode's change time moves, and that is part of what is compared.
    before = os.stat(path)
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": "other@example.org"}}))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert read_desktop_account(path) == "other@example.org" and len(parses) == 2
    replacement = tmp_path / "next.json"
    replacement.write_text(json.dumps({"oauthAccount": {"emailAddress": "third@example.org"}}))
    replacement.replace(path)
    assert read_desktop_account(path) == "third@example.org" and len(parses) == 3
    path.unlink()
    assert read_desktop_account(path) is None
    path.write_text("not json")
    assert read_desktop_account(path) is None
    capacity.forget_desktop_account()
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": "third@example.org"}}))
    assert read_desktop_account(path) == "third@example.org"
