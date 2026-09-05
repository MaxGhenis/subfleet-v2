"""Menu bar evidence honesty: C-8.1, C-9.1, C-9.7, C-18.1, C-23.18."""

import copy
import json

import pytest

from subfleet.capacity import build_view
from subfleet.status_json import build_status, write_status

NOW = "2026-09-05T12:00:00Z"


def lane(provider="codex", **extra):
    return {"lane_id": f"{provider}-1", "provider": provider, "account_key": f"{provider}:first@example.org",
            "credential_ref": f"/homes/{provider}-1", "home": f"/homes/{provider}-1", "owner": "v2",
            "enabled": True, "desktop": False, **extra}


def reading(provider="codex", **extra):
    return {"lane_id": f"{provider}-1", "scope": "account", "window": "seven_day", "utilization": 0.25,
            "resets_at": "2026-09-06T12:00:00Z", "label": "provider", "source": "usage", "observed_at": NOW, **extra}


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("label", ["admission-observed", "local-backoff", "unknown"])
def test_no_percentage_without_provider_reading(provider, label):
    """C-9.1, C-18.1: non-provider evidence renders words even if it carries a number."""
    view = build_view([lane(provider)], [reading(provider, label=label)], now=NOW)
    result = build_status(view)
    serialized = json.dumps(result)
    assert "used_percent" not in serialized
    assert "_pct" not in serialized
    assert label in serialized


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_stale_provider_percentages_marked_stale(provider):
    """C-9.1: stale provider readings retain percentages and explicitly carry stale labels."""
    view = build_view([lane(provider)], [reading(provider, observed_at="2026-09-05T11:00:00Z")], now=NOW)
    result = build_status(view)
    if provider == "codex":
        window = result["codex"]["homes"][0]["windows"]["secondary"]
        assert result["codex"]["homes"][0]["windows"]["source"] == "stale-provider"
    else:
        window = result["claude"]["accounts"][0]["probe"]["seven_day"]
        assert result["claude"]["accounts"][0]["live"]["stale"]
    assert window["used_percent"] == 25
    assert window["stale"]
    assert window["status"] == "stale-provider"


def test_v1_swift_shape_and_duration_aliases(tmp_path):
    """C-8.1, C-9.7, C-18.1: Swift-compatible sections publish duration-based aliases atomically."""
    rows = [reading(window="seven_day", utilization=0.8), reading(window="five_hour", utilization=0.1),
            reading("claude", window="five_hour", utilization=0.2)]
    view = build_view([lane(), lane("claude")], rows, now=NOW)
    before = copy.deepcopy(view)
    result = write_status(tmp_path, view)
    assert json.loads((tmp_path / "status.json").read_text()) == result
    assert result["generated_at"] == NOW
    windows = result["codex"]["homes"][0]["windows"]
    assert windows["primary"]["used_percent"] == 10
    assert windows["secondary"]["used_percent"] == 80
    assert result["codex"]["fleet"]["total_homes"] == 1
    assert result["codex"]["fleet"]["dispatchable_now"] == 1
    claude = result["claude"]["accounts"][0]
    assert claude["email"] == "first@example.org"
    assert claude["enrolled"] and not claude["active"]
    assert isinstance(claude["probe"]["five_hour"]["reset_at"], float)
    assert view == before
    assert not list(tmp_path.glob(".status.json-*"))


def test_identity_status_and_post_heal_verdict_preserved():
    """C-23.27, C-23.45: status sees the healed verdict and canonical identity metadata."""
    view = build_view([lane(verdict="ok", identity_status="verified", app_shadowed=True)], now=NOW)
    row = build_status(view)["codex"]["homes"][0]
    assert row["verdict"] == "ok"
    assert row["identity_status"] == "verified"
    assert row["app_shadowed"]
    assert "used_percent" not in json.dumps(row)


@pytest.mark.parametrize("counts, total", [([2, 3], 5), ([2, None], None), ([0, 0], 0)])
def test_fleet_credit_total_unknown_if_any_count_unreadable(counts, total):
    """C-23.18: a partial fleet credit count never masquerades as a complete total."""
    lanes = [lane(lane_id=f"codex-{i}", account_key=f"codex:{i}", reset_credits_remaining=count) for i, count in enumerate(counts)]
    result = build_status(build_view(lanes, now=NOW))
    assert result["codex"]["fleet"]["reset_credits_remaining"] == total


def test_probe_credit_counts_include_disabled_unknown_and_deduplicate_accounts():
    """C-23.18, C-23.45: adapter counts are complete only across all canonical accounts, including disabled lanes."""
    lanes = [lane(probe={"reset_credits": {"available": 2}}),
             lane(lane_id="duplicate", duplicate_of="/homes/codex-1", reset_credits={"available": 2})]
    assert build_status(build_view(lanes, now=NOW))["codex"]["fleet"]["reset_credits_remaining"] == 2
    lanes.append(lane(lane_id="disabled", account_key="codex:disabled", enabled=False))
    assert build_status(build_view(lanes, now=NOW))["codex"]["fleet"]["reset_credits_remaining"] is None


def test_empty_roster_and_model_scope_cannot_supply_account_percentage():
    """C-9.1: an absent account window remains unknown despite model-specific provider evidence."""
    assert build_status({"lanes": [], "now": NOW})["codex"]["fleet"]["total_homes"] == 0
    view = build_view([lane()], [reading(scope="gpt-6-astra")], now=NOW)
    assert "used_percent" not in json.dumps(build_status(view))
