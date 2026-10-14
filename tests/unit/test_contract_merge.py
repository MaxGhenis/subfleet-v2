"""C-20.6: `tools/contract_merge.py` merges a branch from before the history moved out.

Each open pull request from before the move needs one last merge of the release line,
which conflicts on the old changes list and on every paragraph the layout split. The
tool brings the three versions Git staged to the new layout, writes the branch's new
list entries as files, and merges. Its defining property is differential: merging after
the move gives what moving the merged contract would.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools import contract_history as history
from tools import contract_layout as layout
from tools import contract_merge as merge_tool

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "contract_merge.py"
SETTINGS = settings(max_examples=60, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow])

ENTRIES = ["C-1.2 (2026-10-10, x): an older entry.", "C-1.1 (2026-10-09, y): the oldest."]


def contract(clauses: list[list[str]], entries: list[str] | None = ENTRIES) -> str:
    """A small contract: a changes list (old layout) or none, and one section of clauses."""
    head = ["# Acceptance contract", "", "Version 3. A clause changes with its tests.", "",
            history.HEADING, ""]
    if entries is None:
        head += [history.POINTER, ""]
    else:
        head += [f"- {e}" for e in entries] + [""]
    body = [f"- **C-1.{n}** " + " ".join(sentences) for n, sentences in enumerate(clauses, 1)]
    return "\n".join(head + ["## 1. Section", "", *body, ""])


CLAUSES = [[f"Sentence {s} of clause {c}." for s in range(1, 4)] for c in range(1, 7)]


def edit(clauses, clause, sentence, text):
    changed = [list(c) for c in clauses]
    changed[clause][sentence] = text
    return changed


def moved(text: str) -> str:
    """The release line after the move: the same contract, history out, laid out."""
    return merge_tool.normalize(text)


def test_an_old_branch_merges_cleanly_with_the_moved_release():
    """C-20.6 the branch's clause edit and list entry both arrive; nothing conflicts."""
    base = contract(CLAUSES)
    ours = contract(edit(CLAUSES, 0, 1, "Ours changed this."),
                    ["C-1.1 (2026-10-11, z): ours.", *ENTRIES])
    theirs = moved(contract(edit(CLAUSES, 4, 0, "Theirs changed this.")))
    result = merge_tool.merge(base, ours, theirs)
    assert result.conflicts == 0
    assert result.added == [("ours", "C-1.1 (2026-10-11, z): ours.")]
    assert result.dropped == []
    both = contract(edit(edit(CLAUSES, 0, 1, "Ours changed this."), 4, 0, "Theirs changed this."))
    assert result.text == moved(both)
    assert history.listed_entries(result.text) == []
    assert layout.unlaid(result.text) == []


def test_the_merge_is_symmetric():
    """C-20.6 the release merged into the branch, or the branch into the release: the same."""
    base = contract(CLAUSES)
    ours = moved(contract(edit(CLAUSES, 4, 0, "Theirs changed this.")))
    theirs = contract(edit(CLAUSES, 0, 1, "Ours changed this."),
                      ["C-1.1 (2026-10-11, z): ours.", *ENTRIES])
    result = merge_tool.merge(base, ours, theirs)
    assert result.conflicts == 0
    assert result.added == [("theirs", "C-1.1 (2026-10-11, z): ours.")]
    assert result.text == merge_tool.merge(base, theirs, ours).text


def test_two_moved_branches_merge_as_git_would():
    """C-20.6 after the move nothing is listed, so nothing is added; the merge is Git's."""
    base = moved(contract(CLAUSES))
    ours = moved(contract(edit(CLAUSES, 0, 0, "Ours.")))
    theirs = moved(contract(edit(CLAUSES, 3, 2, "Theirs.")))
    result = merge_tool.merge(base, ours, theirs)
    assert (result.conflicts, result.added, result.dropped) == (0, [], [])
    assert result.text == moved(contract(edit(edit(CLAUSES, 0, 0, "Ours."), 3, 2, "Theirs.")))


def test_one_sentence_edited_on_both_sides_conflicts_and_words_can_settle_it():
    """C-20.6 different words of one sentence: a conflict, which `--words` merges."""
    base = contract(CLAUSES)
    ours = contract(edit(CLAUSES, 2, 1, "Sentence two of clause 3."))
    theirs = moved(contract(edit(CLAUSES, 2, 1, "Sentence 2 of clause three.")))
    plain = merge_tool.merge(base, ours, theirs)
    assert plain.conflicts == 1
    assert "<<<<<<< ours" in plain.text and "||||||| base" in plain.text
    worded = merge_tool.merge(base, ours, theirs, words=True)
    assert (worded.conflicts, worded.word_merged) == (0, 1)
    assert "Sentence two of clause three." in worded.text
    assert layout.unlaid(worded.text) == []


def test_one_word_changed_on_both_sides_stays_a_conflict():
    """C-20.6 `--words` never picks a side: the hunk stays, with its base, for a person."""
    base = contract(CLAUSES)
    ours = contract(edit(CLAUSES, 2, 1, "Sentence 2 of clause seven."))
    theirs = moved(contract(edit(CLAUSES, 2, 1, "Sentence 2 of clause eight.")))
    result = merge_tool.merge(base, ours, theirs, words=True)
    assert result.conflicts == 1 and result.word_merged == 0
    assert "seven" in result.text and "eight" in result.text and "||||||| base" in result.text


def test_an_edited_old_entry_is_reported_not_carried():
    """C-20.6 the migrated list is not edited, so a branch's edit to it goes to a person."""
    base = contract(CLAUSES)
    ours = contract(CLAUSES, [ENTRIES[0].replace("older", "old"), ENTRIES[1]])
    result = merge_tool.merge(base, ours, moved(base))
    assert result.dropped == [("ours", ENTRIES[0])]
    assert result.added == [("ours", ENTRIES[0].replace("older", "old"))]


@st.composite
def two_edits(draw):
    clauses = len(CLAUSES)
    i = draw(st.integers(0, clauses - 1))
    j = draw(st.integers(0, clauses - 1).filter(lambda j: abs(i - j) >= 2))
    s = draw(st.integers(0, 2))
    t = draw(st.integers(0, 2))
    ours = draw(st.sampled_from(["Ours.", "Ours; with a clause.", "Two. Sentences.", ""]))
    theirs = draw(st.sampled_from(["Theirs.", "Theirs; too.", "Also two. Here.", "x"]))
    new = draw(st.lists(st.sampled_from(["C-1.1 (2026-10-11, a): one.",
                                         "C-1.3 (2026-10-12, b): two."]),
                        unique=True, max_size=2))
    ours_moved = draw(st.booleans())
    return i, s, ours, j, t, theirs, new, ours_moved


@SETTINGS
@given(two_edits())
def test_merging_after_the_move_is_moving_the_merge(edits):
    """C-20.6 differential: for edits in clauses at least one apart, the tool's merge is
    the move applied to the contract with both edits, and the branch's new entries are
    exactly those it added to the list."""
    i, s, ours_text, j, t, theirs_text, new, ours_moved = edits
    base = contract(CLAUSES)
    ours = contract(edit(CLAUSES, i, s, ours_text), [*new, *ENTRIES])
    if ours_moved:
        ours, new = moved(ours), []
    theirs = moved(contract(edit(CLAUSES, j, t, theirs_text)))
    result = merge_tool.merge(base, ours, theirs)
    assert result.conflicts == 0
    assert result.text == moved(contract(edit(edit(CLAUSES, i, s, ours_text), j, t, theirs_text)))
    assert [text for _, text in result.added] == new
    assert result.dropped == []


def test_merge_messages_name_the_branch():
    """C-20.6 an entry the other side added is named after the branch the merge names."""
    assert merge_tool.theirs_branch("Merge branch 'fix/a-b'\n\n# Conflicts:") == "fix/a-b"
    assert merge_tool.theirs_branch("Merge remote-tracking branch 'origin/release/217' into x") \
        == "release/217"
    assert merge_tool.theirs_branch("Merge commit 'abc123'") == "abc123"
    assert merge_tool.theirs_branch("no quotes") == ""


# End to end, in a repository of its own: the hub's one last release merge.

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=check, env=env)


def run_tool(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=repo, capture_output=True,
                          text=True, env=env)


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """A base with the old layout, `release` after the move, `fix/thing` from the base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "release")
    write(repo, merge_tool.PATH, contract(CLAUSES))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "fix/thing")
    write(repo, merge_tool.PATH, moved(contract(edit(CLAUSES, 5, 2, "Release changed this."))))
    for n, text in enumerate(ENTRIES, 1):
        write(repo, f"{merge_tool.HISTORY}/version-3/{n:03d}.md", text + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "the move")
    git(repo, "checkout", "-q", "fix/thing")
    write(repo, merge_tool.PATH, contract(edit(CLAUSES, 0, 0, "The branch changed this."),
                                          ["C-1.1 (2026-10-11, z): the branch's entry.",
                                           *ENTRIES]))
    git(repo, "commit", "-q", "-am", "the branch")
    return repo


def test_the_release_merge_into_an_old_branch_end_to_end(repo):
    """C-20.6 `git merge` conflicts; the tool resolves it and names the entry's file."""
    merged = git(repo, "merge", "release", check=False)
    assert merged.returncode == 1 and "CONFLICT" in merged.stdout
    result = run_tool(repo)
    assert result.returncode == 0, result.stderr
    entry = f"{merge_tool.HISTORY}/2026-10-11-fix-thing.md"
    assert f"wrote {entry}" in result.stdout
    assert f"then: git add {merge_tool.PATH} {entry}" in result.stdout
    assert (repo / entry).read_text() == "C-1.1 (2026-10-11, z): the branch's entry.\n"
    expected = moved(contract(edit(edit(CLAUSES, 0, 0, "The branch changed this."),
                                   5, 2, "Release changed this.")))
    assert (repo / merge_tool.PATH).read_text() == expected
    # Run again (as after a restart): the same contract, and no second copy of the entry.
    again = run_tool(repo)
    assert again.returncode == 0
    assert (repo / merge_tool.PATH).read_text() == expected
    assert sorted(p.name for p in (repo / merge_tool.HISTORY).glob("*.md")) == [
        "2026-10-11-fix-thing.md"]
    git(repo, "add", merge_tool.PATH, entry)
    git(repo, "commit", "-q", "--no-edit")
    assert history.listed_entries((repo / merge_tool.PATH).read_text()) == []


def test_a_dry_run_writes_nothing(repo):
    """C-20.6 `--dry-run` says what it would write and leaves the conflicted file."""
    git(repo, "merge", "release", check=False)
    before = (repo / merge_tool.PATH).read_text()
    result = run_tool(repo, "--dry-run")
    assert result.returncode == 0
    assert "would write docs/contract-history/2026-10-11-fix-thing.md" in result.stdout
    assert (repo / merge_tool.PATH).read_text() == before
    assert not list((repo / merge_tool.HISTORY).glob("*.md"))


def test_it_refuses_outside_a_merge_and_between_two_old_branches(repo):
    """C-20.6 exit 2: not mid-merge, or neither side has moved (not the move's merge)."""
    assert run_tool(repo).returncode == 2
    git(repo, "checkout", "-q", "-b", "other", "release~1")
    write(repo, merge_tool.PATH, contract(CLAUSES, ["C-1.4 (2026-10-12, w): other.", *ENTRIES]))
    git(repo, "commit", "-q", "-am", "another old branch")
    assert git(repo, "merge", "fix/thing", check=False).returncode == 1
    refused = run_tool(repo)
    assert refused.returncode == 2
    assert "both sides still list their changes" in refused.stderr


def test_the_replay_sorts_leftover_hunks():
    """C-20.6 `contract_replay.kinds`: an insert at one place, a word-mergeable hunk, and a
    word changed on both sides, as the report counts them."""
    from tools import contract_replay
    base = "a\nb c d\nx\ne f g\ny\n"
    ours = "a\nb C d\nx\ne F g\ny\n"
    theirs = "a\nb c D\nx\ne G g\ny\n"
    merged, conflicts = merge_tool._merge_file(ours, base, theirs, "--diff3")
    assert conflicts == 2
    assert contract_replay.kinds(merged) == {"both inserted": 0, "word-mergeable": 1, "same word": 1}
    merged, _ = merge_tool._merge_file("a\nours\nz\n", "a\nz\n", "a\ntheirs\nz\n", "--diff3")
    assert contract_replay.kinds(merged) == {"both inserted": 1, "word-mergeable": 0, "same word": 0}
