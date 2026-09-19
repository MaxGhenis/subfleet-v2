"""The real Swift menu model consumes daemon JSON without launching a GUI."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from subfleet.status_json import build_status


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("xcrun") is None,
                                reason="native Swift frontend validation requires macOS developer tools")


@pytest.fixture(scope="session")
def probe(tmp_path_factory):
    """Compile production model code with Foundation only; no AppKit entry point."""
    binary = tmp_path_factory.mktemp("subfleet-swift-model") / "probe"
    compiled = subprocess.run(["xcrun", "swiftc", "-D", "SUBFLEET_MODEL_TEST", "-parse-as-library",
                               str(ROOT / "app/SubfleetApp.swift"), str(ROOT / "tests/frontend/StatusModelProbe.swift"),
                               "-o", str(binary)], capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stderr
    return binary


def lane(provider, *, label="provider", **overrides):
    return {"lane_id": f"{provider}-1", "provider": provider, "owner": "v2", "enabled": True,
            "account_key": f"{provider}:fixture", "email": "fixture@example.invalid",
            "readings": [{"scope": "account", "window": window, "label": label,
                          "source": "fixture", "utilization": utilization,
                          "observed_at": NOW.isoformat(), "resets_at": (NOW + timedelta(days=1)).isoformat()}
                         for window, utilization in (("five_hour", .25), ("seven_day", .60))],
            **overrides}


def display(probe, tmp_path, rows, *, generated_at=NOW, offline=False, now=NOW):
    return project(probe, tmp_path, build_status({"lanes": rows, "offline": offline}, now=generated_at), now)


def project(probe, tmp_path, payload, now=NOW):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload))
    result = subprocess.run([str(probe), str(path), str(now.timestamp())], check=True,
                            capture_output=True, text=True, timeout=10)
    return json.loads(result.stdout)


def test_c18_1_frontend_reads_live_provider_windows(probe, tmp_path):
    """C-9.1, C-18.1 the Swift model decodes actual Codex and Claude daemon projections."""
    result = display(probe, tmp_path, [lane("codex"), lane("claude")])
    assert result["stale"] is False
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] == 25 and row["weekly_percentage"] == 60
        assert row["five_hour_reset"] == (NOW + timedelta(days=1)).timestamp()
        assert row["weekly_reset"] == (NOW + timedelta(days=1)).timestamp()
        assert row["stale"] is False
        assert row["tone"] == "good"


@pytest.mark.parametrize("label", ["unknown", "admission-observed", "local-backoff"])
def test_c9_1_frontend_never_invents_percentage_without_provider_evidence(probe, tmp_path, label):
    """C-9.1 non-provider readings cannot become percentages in either provider's menu row."""
    result = display(probe, tmp_path, [lane("codex", label=label), lane("claude", label=label)])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] is None and row["weekly_percentage"] is None
        assert row["five_hour_reset"] is None and row["weekly_reset"] is None
        if label != "admission-observed":
            assert row["tone"] != "good"


@pytest.mark.parametrize("condition", ["stale-reading", "stale-snapshot", "offline"])
def test_c9_1_frontend_marks_cached_evidence_stale(probe, tmp_path, condition):
    """C-9.1 stale provider data stays labelled stale rather than looking like fresh live capacity."""
    label = "stale-provider" if condition == "stale-reading" else "provider"
    result = display(probe, tmp_path, [lane("codex", label=label), lane("claude", label=label)],
                     offline=condition == "offline",
                     generated_at=NOW - timedelta(minutes=11) if condition == "stale-snapshot" else NOW)
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["stale"] is True
        assert row["tone"] != "good"


def test_c10_frontend_does_not_present_v1_lanes_as_available(probe, tmp_path):
    """C-10.4 ownership stays visible; a lane still assigned to v1 is not shown as available."""
    result = display(probe, tmp_path, [lane("codex", owner="v1"), lane("claude", owner="v1")])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert "v1" in (row["status"] + " " + row["detail"]).lower()
        assert row["tone"] != "good"


def test_c10_6_frontend_suppresses_mismatched_identity_percentages(probe, tmp_path):
    """C-10.6 a mismatched lane cannot advertise another account's observed capacity."""
    result = display(probe, tmp_path, [lane("codex", identity_status="mismatch"),
                                      lane("claude", identity_status="mismatch")])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] is None and row["weekly_percentage"] is None
        assert row["five_hour_reset"] is None and row["weekly_reset"] is None
        assert "mismatch" in (row["status"] + " " + row["detail"]).lower()
        assert row["tone"] == "error"


def test_c2_1_frontend_resolves_custom_state_root(probe, tmp_path):
    """C-2.1 the app resolves SUBFLEET_HOME independently of the historical v1 location."""
    home = tmp_path / "home"
    for override, expected in [(None, home / ".subfleet/status.json"),
                               (str(tmp_path / "custom state"), tmp_path / "custom state/status.json"),
                               ("~/custom-state", home / "custom-state/status.json")]:
        args = [str(probe), "path", str(home)]
        if override is not None:
            args.append(override)
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=10)
        assert result.stdout.strip() == str(expected)


def test_c18_1_frontend_accepts_empty_fleet(probe, tmp_path):
    """C-18.1 an empty initial daemon snapshot decodes without inventing lanes or capacity."""
    result = display(probe, tmp_path, [])
    assert result == {"stale": False, "codex": [], "claude": []}


def test_c9_1_frontend_handles_lanes_without_usage_windows(probe, tmp_path):
    """C-9.1 newly enrolled lanes without any readings display unknown rather than zero usage."""
    result = display(probe, tmp_path, [lane("codex", readings=[]), lane("claude", readings=[])])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] is None and row["weekly_percentage"] is None
        assert row["tone"] != "good"


@pytest.mark.parametrize("timestamp,stale", [
    ("2026-09-19T12:00:00.123Z", False),
    ("2026-09-19T11:50:00Z", False),
    ("2026-09-19T11:49:59Z", True),
    ("2026-09-19T12:01:01Z", True),
    ("invalid-clock", True),
])
def test_c18_1_frontend_snapshot_clock_is_conservative(probe, tmp_path, timestamp, stale):
    """C-18.1 stale, future and invalid snapshot clocks cannot present provider data as live."""
    payload = build_status({"lanes": [lane("codex"), lane("claude")]}, now=NOW)
    payload["generated_at"] = timestamp
    result = project(probe, tmp_path, payload)
    assert result["stale"] is stale
    for provider in ("codex", "claude"):
        assert result[provider][0]["stale"] is stale
