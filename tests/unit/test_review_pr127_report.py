"""The corrected PR description states the implementation's actual criterion."""
from pathlib import Path


def test_moot_block_report_uses_the_implemented_mtime_criterion():
    report = (Path(__file__).resolve().parents[2] / "docs/reports/2026-10-04-continuation-pr-body.md").read_text()
    assert "catalog transcript mtime" in report and "strictly after blocked_at" in report
    assert "outside-writer check" in report and "conversation/native lease" in report
    assert "gained turns" not in report
