"""Host the actual menu view and exercise the size proposal that collapsed it."""

from datetime import timedelta
import json
import subprocess

import pytest

from tests.frontend.test_status_model import NOW, ROOT, lane, pytestmark
from subfleet.status_json import build_status


@pytest.fixture(scope="session")
def menu_probe(tmp_path_factory):
    binary = tmp_path_factory.mktemp("subfleet-menu-view") / "probe"
    result = subprocess.run(
        ["xcrun", "swiftc", "-D", "SUBFLEET_VIEW_TEST", "-parse-as-library",
         str(ROOT / "app/SubfleetApp.swift"), str(ROOT / "tests/frontend/MenuViewProbe.swift"),
         "-o", str(binary)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return binary


def invoke(menu_probe, path, *extra):
    result = subprocess.run([str(menu_probe), str(path), *map(str, extra)],
                            capture_output=True, text=True, timeout=20, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("populated", [True, False])
def test_menu_minimum_proposal_keeps_accounts_visible(menu_probe, tmp_path, populated):
    # The host's ideal size already passed before the fix; its zero/minimum
    # proposal reduced a populated menu to 96px, hiding every account row.
    lanes = ([lane(provider, lane_id=f"{provider}-{i}")
              for provider, count in (("codex", 6), ("claude", 17))
              for i in range(count)] if populated else [])
    path = tmp_path / "status.json"
    path.write_text(json.dumps(build_status({"lanes": lanes}, now=NOW)))
    result = invoke(menu_probe, path)
    assert result["lanes"] == (23 if populated else 0)
    assert result["minimum_width"] == 430
    assert 580 <= result["minimum_height"] <= 700
    assert result["proposed_height"] == pytest.approx(result["minimum_height"], abs=1)
    assert result["visible_windows"] == 0


def test_menu_missing_snapshot_still_fits_error_and_controls(menu_probe, tmp_path):
    result = invoke(menu_probe, tmp_path / "missing.json")
    assert result["has_snapshot"] is False
    assert result["minimum_width"] == 430
    assert 80 <= result["minimum_height"] <= 350
    assert result["visible_windows"] == 0


def test_manual_reload_acknowledges_read_and_loads_replacement(menu_probe, tmp_path):
    path, replacement = tmp_path / "status.json", tmp_path / "next.json"
    path.write_text(json.dumps(build_status({"lanes": []}, now=NOW)))
    next_snapshot = build_status({"lanes": [lane("codex")]}, now=NOW + timedelta(minutes=5))
    replacement.write_text(json.dumps(next_snapshot))
    result = invoke(menu_probe, path, replacement)
    assert result["initial_feedback"] is None
    assert result["unchanged_feedback"].startswith("Snapshot reloaded at ")
    assert result["unchanged_generation"] is True
    assert 580 <= result["reload_minimum_height"] <= 700
    assert result["new_generation"] == next_snapshot["generated_at"]
    assert result["new_lane_count"] == 1
    assert result["failed_feedback"].startswith("Reload failed at ")
    assert result["failed_read_error"]
    assert result["failed_snapshot_cleared"] is True
    assert result["visible_windows"] == 0
