"""C-20.7: the contract's change history is one file per entry in `docs/contract-history/`.

The contract lists no change of its own (defect D-MRG3: every pull request prepended to
one list there, so each merge made every other open pull request conflict). These tests
pin the folder: each dated entry is well formed, the 157 migrated entries are whole and
unchanged, the index shows each entry once in order, and no doc an author follows still
points at the old list.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path

import pytest

from tools import contract_history as history

ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date(2026, 10, 14)


def test_the_history_folder_is_well_formed_and_complete():
    """C-20.7 `contract_history.problems` finds nothing wrong in the repository."""
    assert history.problems(today=dt.date.today()) == []


def test_the_migrated_list_is_whole_and_in_order():
    """C-20.7 the 157 entries the contract listed are 001 to 157, byte for byte."""
    entries = [e for e in history.read_entries() if e.migrated]
    assert [e.order for e in entries] == list(range(1, history.MIGRATED_COUNT + 1))
    assert history.MIGRATED_COUNT == 157
    assert history.migrated_digest(entries) == history.MIGRATED_SHA256
    # Each was one list item, kept without its marker: one line, not starting `- `.
    assert all("\n" not in e.text and not e.text.startswith("- ") for e in entries)
    # The list's first and last items, as release/217 had them at 3f50142ac.
    assert entries[0].text.startswith("C-6.9, C-6.11, C-6.12 (2026-10-12, stateful admission")
    assert entries[-1].text.startswith("C-5.11, C-5.12: the control loop gives a worker only")


def test_the_contract_lists_no_change():
    """C-20.7 the changes section holds only its pointer: no list item, no other text."""
    lines = (ROOT / "docs" / "acceptance-contract.md").read_text().split("\n")
    start, end = history.section(lines)
    assert lines[start] == history.HEADING
    assert not [line for line in lines[start + 1:end] if line.lstrip().startswith("- ")]
    assert " ".join(" ".join(lines[start + 1:end]).split()) == history.POINTER
    assert history.listed_entries("\n".join(lines)) == []


def test_the_index_shows_every_entry_once_newest_first():
    """C-20.7 the index: dated entries newest first, then the migrated list in its order."""
    entries = history.read_entries()
    text = history.index()
    items = re.findall(r"^- ", text, re.M)
    assert len(items) == len(entries)
    positions = [text.index(history.as_item(e.text)) for e in entries]
    assert positions == sorted(positions)
    dated = [e for e in entries if not e.migrated]
    assert [e.date for e in dated] == sorted((e.date for e in dated), reverse=True)
    assert all(not e.migrated for e in entries[:len(dated)])
    # This change's own entry is there, as the newest or among the newest.
    assert any(e.path.name == "2026-10-14-fix-d-mrg3-contract-history.md" for e in dated)


def test_the_index_is_not_checked_in():
    """C-20.7 a checked-in index would be one more file every pull request edits."""
    names = {p.name for p in (ROOT / "docs" / "contract-history").iterdir()}
    assert not names & {"INDEX.md", "index.md", "CHANGES.md", "history.md"}


@pytest.fixture
def copy(tmp_path):
    """A folder to break: the README and the migrated list, but none of the dated entries,
    which keep arriving and would meet the fixed clock below (review r1b, finding 6).
    The real folder is checked against the real date above."""
    root = tmp_path / "contract-history"
    root.mkdir()
    shutil.copy2(history.HISTORY / "README.md", root / "README.md")
    shutil.copytree(history.HISTORY / history.MIGRATED_DIR, root / history.MIGRATED_DIR)
    contract = tmp_path / "acceptance-contract.md"
    contract.write_text(history.CONTRACT.read_text(encoding="utf-8"), encoding="utf-8")
    return root, contract


def _entry(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


GOOD = "C-20.7 (2026-10-14, a test): an entry that is fine.\n"


def _list_again(_root: Path, contract: Path) -> None:
    text = contract.read_text().replace(
        history.HEADING + "\n", history.HEADING + "\n\n- C-20.7 (2026-10-14): listed here.\n", 1)
    contract.write_text(text)


def _edit_migrated(root: Path, _contract: Path) -> None:
    path = root / history.MIGRATED_DIR / "042.md"
    path.write_text(path.read_text().replace("C-", "C -", 1))


def _swap_migrated(root: Path, _contract: Path) -> None:
    a, b = root / history.MIGRATED_DIR / "001.md", root / history.MIGRATED_DIR / "002.md"
    first = a.read_text()
    a.write_text(b.read_text())
    b.write_text(first)


BREAKAGES = {
    "list item back in the contract": (_list_again, "must hold only the pointer"),
    "pointer reworded": (
        lambda r, c: c.write_text(c.read_text().replace("never lists a change", "may list a change")),
        "must hold only the pointer"),
    "migrated entry edited": (_edit_migrated, "differ from the list"),
    "migrated entries reordered": (_swap_migrated, "differ from the list"),
    "migrated entry removed": (lambda r, c: (r / history.MIGRATED_DIR / "157.md").unlink(),
                               "each once"),
    "migrated entry added": (lambda r, c: _entry(r / history.MIGRATED_DIR, "158.md", GOOD),
                             "each once"),
    "migrated name": (lambda r, c: _entry(r / history.MIGRATED_DIR, "x.md", GOOD), "NNN.md"),
    "entry name with capitals": (lambda r, c: _entry(r, "2026-10-14-Fix.md", GOOD),
                                 "an entry is named"),
    "entry name without a date": (lambda r, c: _entry(r, "fix-something.md", GOOD),
                                  "an entry is named"),
    "impossible date": (lambda r, c: _entry(r, "2026-02-30-fix.md",
                                            GOOD.replace("2026-10-14", "2026-02-30")),
                        "is not a date"),
    "future date": (lambda r, c: _entry(r, "2026-12-01-fix.md",
                                        GOOD.replace("2026-10-14", "2026-12-01")),
                    "after today"),
    "first date is not the file's": (
        lambda r, c: _entry(r, "2026-10-13-fix.md", GOOD), "first date the entry gives"),
    "no date in the text": (
        lambda r, c: _entry(r, "2026-10-14-fix.md", "C-20.7: no date.\n"),
        "first date the entry gives"),
    "cites no clause": (
        lambda r, c: _entry(r, "2026-10-14-fix.md", "Something (2026-10-14): changed.\n"),
        "cites no clause"),
    "cites only a clause the contract lacks": (
        lambda r, c: _entry(r, "2026-10-14-fix.md", "C-99.1 (2026-10-14): changed.\n"),
        "cites no clause"),
    "two paragraphs": (
        lambda r, c: _entry(r, "2026-10-14-fix.md", GOOD + "\nC-20.7 more.\n"),
        "one paragraph"),
    "a list marker": (lambda r, c: _entry(r, "2026-10-14-fix.md", "- " + GOOD),
                      "the index adds the list marker"),
    "no final newline": (lambda r, c: _entry(r, "2026-10-14-fix.md", GOOD.rstrip("\n")),
                         "then one newline"),
    "empty": (lambda r, c: _entry(r, "2026-10-14-fix.md", "\n"), "empty"),
    "duplicate of another entry": (
        lambda r, c: (_entry(r, "2026-10-14-fix.md", GOOD), _entry(r, "2026-10-14-fix-2.md", GOOD)),
        "the same text as"),
    "a second block in the entry": (
        lambda r, c: _entry(r, "2026-10-14-fix.md", GOOD + "- A second item\n"),
        "a later line may not start"),
    "a heading after the entry": (
        lambda r, c: _entry(r, "2026-10-14-fix.md", GOOD + "# Heading\n"),
        "a later line may not start"),
    "a table after the entry": (
        lambda r, c: _entry(r, "2026-10-14-fix.md", GOOD + "| a | b |\n|---|---|\n"),
        "a later line may not start"),
    "a folder": (lambda r, c: (r / "drafts").mkdir(), "no folders"),
    "a stray file": (lambda r, c: _entry(r, "INDEX.md", "x\n"), "an entry is named"),
}


@pytest.mark.parametrize("name", sorted(BREAKAGES))
def test_each_breakage_is_named(copy, name):
    """C-20.7 every way the folder or the contract can drift is reported, with its rule."""
    root, contract = copy
    assert history.problems(root, contract, TODAY) == []
    breakage, expected = BREAKAGES[name]
    breakage(root, contract)
    found = history.problems(root, contract, TODAY)
    assert any(expected in problem for problem in found), found


def test_a_good_entry_and_a_second_from_the_same_branch_pass(copy):
    """C-20.7 the documented names: `<date>-<branch>.md`, then `-2`, `-3`."""
    root, contract = copy
    first = history.free_path(root, "2026-10-14", history.branch_slug("fix/Some_Thing"))
    assert first.name == "2026-10-14-fix-some-thing.md"
    first.write_text(GOOD)
    second = history.free_path(root, "2026-10-14", "fix-some-thing")
    assert second.name == "2026-10-14-fix-some-thing-2.md"
    second.write_text("C-20.7 (2026-10-14, a test, round 2): an entry that is also fine,\n"
                      "written one sentence per line;\nso it has three lines.\n")
    assert history.problems(root, contract, TODAY) == []
    index = history.index(root)
    assert index.index("is fine.") < index.index("is also fine")    # same date: by name
    assert "\n  written one sentence per line;\n  so it has three lines.\n" in index


def test_new_starts_an_entry_the_check_refuses_until_filled(copy, monkeypatch, capsys):
    """C-20.7 `contract_history.py new` names the file after the branch and today."""
    root, contract = copy
    monkeypatch.setattr(history, "HISTORY", root)
    monkeypatch.setattr(history, "REPO", root.parent)
    assert history.main(["new", "--date", "2026-10-14", "--branch", "feat/x-y"]) == 0
    path = root / "2026-10-14-feat-x-y.md"
    assert capsys.readouterr().out.strip() == "contract-history/2026-10-14-feat-x-y.md"
    assert path.read_text().startswith("C-x.y (2026-10-14, ")
    found = history.problems(root, contract, TODAY)
    assert found == ["2026-10-14-feat-x-y.md: cites no clause the contract defines (`C-x.y`)"]


def test_branch_slugs():
    """C-20.7 a branch name as an entry name: lowercase `[a-z0-9]` runs joined by `-`."""
    assert history.branch_slug("fix/d-mrg3-contract-history") == "fix-d-mrg3-contract-history"
    assert history.branch_slug("Feat/Clone__Worktree.Allocation") == "feat-clone-worktree-allocation"
    assert history.branch_slug("///") == "change"
    assert history.ENTRY_NAME.match(f"2026-10-14-{history.branch_slug('a/B c.d')}.md")


# Docs a pull request's author follows today. Reports, decisions, reviews and the history
# itself are records of their time and keep their words.
RECORDS = re.compile(
    r"^docs/(reports|lanes/reports|decisions|desktop/reviews|reference|shadow-diffs|"
    r"contract-history)/|report[^/]*\.md$")
OLD_LIST = re.compile(r"Changes in version \d|change-?list|changes list|version history|"
                      r"under Changes|prepend\w* (a|an|the|one) (line|entry|bullet)", re.I)


def living_docs() -> list[Path]:
    docs = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md"), *(ROOT / ".github").rglob("*.md")]
    return [p for p in docs if not RECORDS.search(p.relative_to(ROOT).as_posix())]


def test_no_doc_an_author_follows_points_at_the_old_list():
    """C-20.7 every mention of the old changes list in a living doc names its new home."""
    stale = []
    for path in living_docs():
        rel = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if rel == "docs/acceptance-contract.md" and line == history.HEADING:
                continue
            if OLD_LIST.search(line) and "docs/contract-history" not in line:
                stale.append(f"{rel}:{number}: {line[:120]}")
    assert not stale, "\n".join(stale)


def test_the_docs_tell_authors_to_add_a_file():
    """C-20.7 the contract, the README and the folder's README say what to do instead."""
    contract = " ".join((ROOT / "docs" / "acceptance-contract.md").read_text().split())
    assert "with an entry in `docs/contract-history/` that records the change (C-20.7)" in contract
    readme = (ROOT / "README.md").read_text()
    assert "add one file of your own to `docs/contract-history/`" in readme
    guide = (ROOT / "docs" / "contract-history" / "README.md").read_text()
    assert "docs/contract-history/<YYYY-MM-DD>-<branch>.md" in guide
    assert "python3 tools/contract_merge.py" in guide


def test_the_living_docs_scan_sees_the_docs_it_must():
    """C-20.7 the scan above reads the contract, the README and the lane briefs."""
    seen = {p.relative_to(ROOT).as_posix() for p in living_docs()}
    assert {"README.md", "docs/acceptance-contract.md", "docs/lanes/identity.md",
            "docs/desktop/maps/v2-docs-contract.md", "docs/invariant-gaps.md"} <= seen
    assert not any(s.startswith(("docs/reports/", "docs/contract-history/")) for s in seen)


def test_an_entry_citing_only_a_titled_clause_passes(copy):
    """C-20.7 review r1b, finding 10: C-8.5 is written `- **C-8.5 Host push ...**`; an
    entry that cites only it cites a clause the contract defines."""
    root, contract = copy
    _entry(root, "2026-10-14-fix.md", "C-8.5 (2026-10-14, clarify host push): changed.\n")
    assert history.problems(root, contract, TODAY) == []
    assert "C-8.5" in history.clause_ids(contract.read_text())


def test_a_later_entry_passes_on_its_own_date(copy):
    """C-20.7 review r1b, finding 6: an entry dated after the test's clock is fine on its
    date; only an entry dated after the day it is checked fails."""
    root, contract = copy
    _entry(root, "2026-10-16-fix.md", "C-20.7 (2026-10-16, later): changed.\n")
    assert history.problems(root, contract, dt.date(2026, 10, 16)) == []
    assert history.problems(root, contract, None) == []
    assert any("after today" in p for p in history.problems(root, contract, TODAY))


def test_old_list_items_are_read_whole():
    """C-20.7 review r1b, finding 5: a wrapped item keeps its later lines, lazy or indented."""
    old = (f"{history.HEADING}\n\n- C-1.1 (2026-10-14, fix): first line.\n  second line.\n"
           "- C-1.2 (2026-10-13, x): one.\nlazy line.\n\nA stray paragraph.\n\n## 1. S\n")
    assert history.listed_entries(old) == [
        "C-1.1 (2026-10-14, fix): first line.\nsecond line.", "C-1.2 (2026-10-13, x): one.\nlazy line."]
    assert history.unrecognised(old) == ["A stray paragraph."]
    assert not history.moved(old) and history.moved(history.without_list(old))
