"""Visual reports retain sources and images without generated run artifacts."""
import re
from urllib.parse import unquote

import pytest

from tests.frontend.swift import ROOT

REPORTS = sorted(path for path in (ROOT / "docs/reports").iterdir() if path.is_dir())
# The visual reports keep prose, renders and source diffs; run logs, JUnit and
# JSON evidence stay out of the repository.
VISUAL_REPORTS = ["2026-10-03-visual-pass", "2026-10-04-visual-review-fixes"]
SOURCE_LIKE = {".md", ".png", ".patch", ".gitattributes"}
LINK = re.compile(r"\]\(([^)\s]+)")


def test_visual_report_has_no_generated_run_evidence_or_dangling_evidence_links():
    report = ROOT / "docs/reports/2026-10-03-visual-pass"
    generated = {
        "build.json", "final-view-tests.xml", "review-build.json", "review-presentation-tests.xml",
        "review-render.json", "review-tests.xml", "tests.xml", "view-tests.xml", "snapshots.json",
    }
    assert not [name for name in sorted(generated) if (report / name).exists()]
    for source in report.glob("*.md"):
        assert not generated.intersection(re.findall(r"\]\(([^)]+)\)", source.read_text())), source


@pytest.mark.parametrize("report", REPORTS, ids=lambda path: path.name)
def test_every_report_link_resolves_inside_the_repository(report):
    dangling = []
    for source in sorted(report.rglob("*.md")):
        for target in LINK.findall(source.read_text()):
            if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue                # a URL, or an anchor in the same page
            path = (source.parent / unquote(target.split("#", 1)[0])).resolve()
            if not path.is_relative_to(ROOT) or not path.exists():
                dangling.append(f"{source.relative_to(ROOT)}: {target}")
    assert not dangling


@pytest.mark.parametrize("name", VISUAL_REPORTS)
def test_visual_reports_hold_only_prose_renders_and_diffs(name):
    files = [path for path in (ROOT / "docs/reports" / name).rglob("*") if path.is_file() and path.name != ".DS_Store"]
    assert files
    assert not [str(path.relative_to(ROOT)) for path in files
                if (path.suffix or path.name) not in SOURCE_LIKE]
