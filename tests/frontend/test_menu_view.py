"""Host the actual menu view and exercise the size proposal that collapsed it."""

from datetime import timedelta
import json
import subprocess

import pytest

from tests.frontend.test_status_model import NOW, ROOT, _job, lane, pytestmark
from subfleet.status_json import build_status
from tests.frontend.swift import compile_probe


@pytest.fixture(scope="session")
def menu_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("subfleet-menu-view") / "probe",
                         ROOT / "tests/frontend/MenuViewProbe.swift", "SUBFLEET_VIEW_TEST")


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


@pytest.mark.parametrize("oversized_snapshot", [False, True])
def test_c18_2_menu_shows_eight_recent_jobs_with_completed_batches(menu_probe, tmp_path, oversized_snapshot):
    # The daemon projects eight results. The view used to truncate that to five
    # and omit batch headings, despite the model correctly decoding every row.
    jobs = [_job(f"job-{i}", ("succeeded", "failed", "cancelled")[i % 3],
                 finished_at=(NOW - timedelta(minutes=i)).isoformat()) for i in range(9)]
    batch = {"id": "completed-batch", "label": "migration checks", "size": 4}
    payload = build_status({"lanes": [], "jobs": jobs,
                            "batches": {f"job-{i}": {**batch, "index": n}
                                        for n, i in enumerate((0, 2, 7, 8), 1)}}, now=NOW)
    assert [job["job_id"] for job in payload["jobs"]["recent"]] == [f"job-{i}" for i in range(8)]
    if oversized_snapshot:
        # Keep the menu's limit correct if a future daemon includes extra rows.
        older = build_status({"lanes": [], "jobs": jobs[-1:],
                              "batches": {"job-8": {**batch, "index": 4}}}, now=NOW)
        payload["jobs"]["recent"].extend(older["jobs"]["recent"])
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload))
    result = invoke(menu_probe, path)
    assert result["recent_groups"] == [
        {"title": "migration checks · 4 jobs", "job_ids": ["job-0", "job-2", "job-7"]},
        *({"title": None, "job_ids": [f"job-{i}"]} for i in (1, 3, 4, 5, 6)),
    ]
    assert sum(len(group["job_ids"]) for group in result["recent_groups"]) == 8
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


def test_changes_pane_rows_fill_the_pane_without_scrolling_sideways(menu_probe):
    """A diff row is as wide as the pane, less a legacy vertical scroller, so its
    colour runs to the edge and a pane of short lines never scrolls sideways;
    overlay scrollers take no width (review, 2026-09-25)."""
    result = subprocess.run([str(menu_probe), "diff-rows"], capture_output=True, text=True, timeout=20, check=True)
    widths = json.loads(result.stdout)
    assert widths["overlay"] == 500
    assert widths["scroller"] > 0 and widths["legacy"] == 500 - widths["scroller"]
    assert widths["narrow"] == 0
    assert widths["row_width"] >= 480
    assert widths["visible_windows"] == 0


def test_c18_4_menu_lays_out_alerts_and_marks_the_problem(menu_probe, tmp_path):
    """C-18.4 the real menu view hosts the alerts section, keeps its width, and the
    menu bar icon shows a problem while one is in force."""
    from tests.frontend.test_status_model import ALERTS
    path = tmp_path / "status.json"
    path.write_text(json.dumps(build_status({"lanes": [lane("codex", lane_id=f"codex-{i}") for i in range(3)],
                                             "alerts": ALERTS}, now=NOW)))
    result = invoke(menu_probe, path)
    assert result["alert_count"] == 3 and result["has_problem"] is True
    assert result["minimum_width"] == 430 and result["visible_windows"] == 0
