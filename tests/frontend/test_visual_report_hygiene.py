"""Visual reports retain sources and images without generated run artifacts."""
import re

from tests.frontend.swift import ROOT


def test_visual_report_has_no_generated_run_evidence_or_dangling_evidence_links():
    report = ROOT / "docs/reports/2026-10-03-visual-pass"
    generated = {
        "build.json", "final-view-tests.xml", "review-build.json", "review-presentation-tests.xml",
        "review-render.json", "review-tests.xml", "tests.xml", "view-tests.xml", "snapshots.json",
    }
    assert not [name for name in sorted(generated) if (report / name).exists()]
    for source in report.glob("*.md"):
        assert not generated.intersection(re.findall(r"\]\(([^)]+)\)", source.read_text())), source
