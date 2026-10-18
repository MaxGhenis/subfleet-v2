"""C-20.7: `tools/contract_merge.py` merges a branch from before the history moved out.

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
    """C-20.7 the branch's clause edit and list entry both arrive; nothing conflicts."""
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
    """C-20.7 the release merged into the branch, or the branch into the release: the same."""
    base = contract(CLAUSES)
    ours = moved(contract(edit(CLAUSES, 4, 0, "Theirs changed this.")))
    theirs = contract(edit(CLAUSES, 0, 1, "Ours changed this."),
                      ["C-1.1 (2026-10-11, z): ours.", *ENTRIES])
    result = merge_tool.merge(base, ours, theirs)
    assert result.conflicts == 0
    assert result.added == [("theirs", "C-1.1 (2026-10-11, z): ours.")]
    assert result.text == merge_tool.merge(base, theirs, ours).text


def test_two_moved_branches_merge_as_git_would():
    """C-20.7 after the move nothing is listed, so nothing is added; the merge is Git's."""
    base = moved(contract(CLAUSES))
    ours = moved(contract(edit(CLAUSES, 0, 0, "Ours.")))
    theirs = moved(contract(edit(CLAUSES, 3, 2, "Theirs.")))
    result = merge_tool.merge(base, ours, theirs)
    assert (result.conflicts, result.added, result.dropped) == (0, [], [])
    assert result.text == moved(contract(edit(edit(CLAUSES, 0, 0, "Ours."), 3, 2, "Theirs.")))


def test_one_sentence_edited_on_both_sides_conflicts_and_words_can_settle_it():
    """C-20.7 different words of one sentence: a conflict, which `--words` merges."""
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
    """C-20.7 `--words` never picks a side: the hunk stays, with its base, for a person."""
    base = contract(CLAUSES)
    ours = contract(edit(CLAUSES, 2, 1, "Sentence 2 of clause seven."))
    theirs = moved(contract(edit(CLAUSES, 2, 1, "Sentence 2 of clause eight.")))
    result = merge_tool.merge(base, ours, theirs, words=True)
    assert result.conflicts == 1 and result.word_merged == 0
    assert "seven" in result.text and "eight" in result.text and "||||||| base" in result.text


def test_an_edited_old_entry_is_a_replacement():
    """C-20.7 an old-list entry edited in place reads as a new round of that entry."""
    base = contract(CLAUSES)
    ours = contract(CLAUSES, [ENTRIES[0].replace("older", "old"), ENTRIES[1]])
    result = merge_tool.merge(base, ours, moved(base))
    assert result.replaced == [("ours", ENTRIES[0], ENTRIES[0].replace("older", "old"))]
    assert (result.added, result.dropped) == ([], [])


def test_an_edited_migrated_entry_goes_to_a_person(tmp_path):
    """C-20.7 review r2, finding 13: the migrated list is not edited, so the new round is
    named for a person and nothing is written."""
    root = tmp_path / "contract-history"
    (root / "version-3").mkdir(parents=True)
    (root / "version-3" / "001.md").write_text(ENTRIES[0] + "\n")
    result = merge_tool.Merged("", 0, replaced=[("ours", ENTRIES[0], "C-1.2 (2026-10-10, x): new.")])
    plan = merge_tool.plan_entries(root, result, {"ours": "fix-x"}, "2026-10-14", {})
    assert plan.writes == [] and plan.kept == []
    assert len(plan.people) == 1 and "001.md" in plan.people[0]


def test_a_landed_entry_gets_its_new_round(tmp_path):
    """C-20.7 review r2, finding 4: when another pull request on the same work landed the
    entry first, a later round goes into that file, not a second one."""
    root = tmp_path / "contract-history"
    root.mkdir()
    landed = root / "2026-10-10-fix-sibling.md"
    landed.write_text("C-1.1 (2026-10-10, x): round one.\n")
    result = merge_tool.Merged("", 0, replaced=[(
        "ours", "C-1.1 (2026-10-10, x): round one.", "C-1.1 (2026-10-10, x): round two.")])
    plan = merge_tool.plan_entries(root, result, {"ours": "fix-other"}, "2026-10-14", {})
    assert [(p.name, text) for p, text, _ in plan.writes] == [
        ("2026-10-10-fix-sibling.md", "C-1.1 (2026-10-10, x): round two.")]
    assert plan.people == []


def test_an_edit_of_an_entry_that_never_landed_is_a_new_entry(tmp_path):
    """C-20.7 a base entry no file holds was never on the release line: its new round is
    simply an entry of this branch, and its removal concerns no one."""
    root = tmp_path / "contract-history"
    root.mkdir()
    result = merge_tool.Merged("", 0, replaced=[("ours", "C-1.1 (2026-10-10, x): a.",
                                                 "C-1.1 (2026-10-10, x): b.")],
                               dropped=[("ours", "C-1.2 (2026-10-09, y): gone.")])
    plan = merge_tool.plan_entries(root, result, {"ours": "fix-x"}, "2026-10-14", {})
    assert [(p.name, text) for p, text, _ in plan.writes] == [
        ("2026-10-10-fix-x.md", "C-1.1 (2026-10-10, x): b.")]
    assert plan.people == []


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
    """C-20.7 differential: for edits in clauses at least one apart, the tool's merge is
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
    """C-20.7 an entry the other side added is named after the branch the merge names."""
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
    """C-20.7 `git merge` conflicts; the tool resolves it and names the entry's file."""
    merged = git(repo, "merge", "release", check=False)
    assert merged.returncode == 1 and "CONFLICT" in merged.stdout
    result = run_tool(repo)
    assert result.returncode == 0, result.stderr
    entry = f"{merge_tool.HISTORY}/2026-10-11-fix-thing.md"
    assert f"wrote and staged {entry}" in result.stdout
    assert f"then: git add {merge_tool.PATH}" in result.stdout
    assert entry in git(repo, "diff", "--cached", "--name-only").stdout.split()
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
    """C-20.7 `--dry-run` says what it would write and leaves the conflicted file."""
    git(repo, "merge", "release", check=False)
    before = (repo / merge_tool.PATH).read_text()
    result = run_tool(repo, "--dry-run")
    assert result.returncode == 0
    assert "would write and would stage docs/contract-history/2026-10-11-fix-thing.md" \
        in result.stdout
    assert (repo / merge_tool.PATH).read_text() == before
    assert not list((repo / merge_tool.HISTORY).glob("*.md"))
    assert git(repo, "diff", "--cached", "--name-only", "--", merge_tool.HISTORY).stdout == ""


def test_it_refuses_outside_a_merge_and_between_two_old_branches(repo):
    """C-20.7 exit 2: not mid-merge, or neither side has moved (not the move's merge)."""
    assert run_tool(repo).returncode == 2
    git(repo, "checkout", "-q", "-b", "other", "release~1")
    write(repo, merge_tool.PATH, contract(CLAUSES, ["C-1.4 (2026-10-12, w): other.", *ENTRIES]))
    git(repo, "commit", "-q", "-am", "another old branch")
    assert git(repo, "merge", "fix/thing", check=False).returncode == 1
    refused = run_tool(repo)
    assert refused.returncode == 2
    assert "both sides still list their changes" in refused.stderr


def test_the_replay_sorts_leftover_hunks():
    """C-20.7 `contract_replay.kinds`: an insert at one place, a word-mergeable hunk, and a
    word changed on both sides, as the report counts them."""
    from tools import contract_replay
    base = "a\nb c d\nx\ne f g\ny\n"
    ours = "a\nb C d\nx\ne F g\ny\n"
    theirs = "a\nb c D\nx\ne G g\ny\n"
    merged, conflicts = merge_tool._merge_file(ours, base, theirs, "--diff3")
    assert conflicts == 2
    assert contract_replay.kinds(merged) == {"both inserted": 0, "word-mergeable": 1,
                                             "still conflicts by word": 1}
    merged, _ = merge_tool._merge_file("a\nours\nz\n", "a\nz\n", "a\ntheirs\nz\n", "--diff3")
    assert contract_replay.kinds(merged) == {"both inserted": 1, "word-mergeable": 0,
                                             "still conflicts by word": 0}


def test_a_bare_replay_date_is_midnight_utc():
    """C-20.7 review r1b, finding 15: git reads `--since=2026-10-01` at the current time
    of day, so the same command saw fewer merges as the day went on."""
    from tools import contract_replay
    assert contract_replay.cutoff("2026-10-01") == "2026-10-01T00:00:00Z"
    assert contract_replay.cutoff("2026-10-14T10:05:00Z") == "2026-10-14T10:05:00Z"


# Review r1b of #218: each test below once failed on the tool as it stood.

def test_removing_every_old_entry_is_reported():
    """C-20.7 finding 8: an old list emptied on one side is an edit for a person, not a
    sign that side has moved; only the pointer says a side has moved."""
    base = contract(CLAUSES)
    ours = contract(edit(CLAUSES, 0, 0, "Changed."), [])
    result = merge_tool.merge(base, ours, moved(base))
    assert result.dropped == [("ours", ENTRIES[0]), ("ours", ENTRIES[1])]
    assert "Changed." in result.text


def test_a_wrapped_old_entry_arrives_whole():
    """C-20.7 finding 5: an old-list item's continuation lines are part of the entry."""
    base = contract(CLAUSES)
    ours = contract(CLAUSES, ["C-1.1 (2026-10-14, fix): first line.\n  continuation words.",
                              *ENTRIES])
    result = merge_tool.merge(base, ours, moved(base))
    assert result.added == [("ours", "C-1.1 (2026-10-14, fix): first line.\ncontinuation words.")]
    assert (result.dropped, result.unrecognised) == ([], [])


def test_unrecognised_text_in_the_old_section_goes_to_a_person():
    """C-20.7 the move drops the old section; text there that is not a list item is named."""
    base = contract(CLAUSES)
    ours = contract(CLAUSES, ENTRIES).replace("\n## 1. Section",
                                               "\nA paragraph someone added.\n\n## 1. Section")
    result = merge_tool.merge(base, ours, moved(base))
    assert result.unrecognised == [("ours", "A paragraph someone added.")]


def test_a_marker_shaped_line_is_never_read_as_a_conflict():
    """C-20.7 finding 7: the markers are longer than any marker-shaped line in the inputs,
    and none is left behind."""
    fenced = "```\n<<<<<<< example\n=======\n>>>>>>> example\n```\n"
    base = contract(CLAUSES).replace("## 1. Section\n", "## 1. Section\n\n" + fenced)
    ours = base.replace("Sentence 2 of clause 3.", "Sentence two of clause 3.")
    theirs = moved(base.replace("Sentence 2 of clause 3.", "Sentence 2 of clause three."))
    result = merge_tool.merge(base, ours, theirs, words=True)
    assert result.marker_size > 7
    assert (result.conflicts, result.word_merged) == (0, 1)
    assert fenced in result.text
    assert "Sentence two of clause three." in result.text
    assert not [line for line in result.text.split("\n")
                if line.startswith(("<" * result.marker_size, ">" * result.marker_size))]


def test_two_entries_of_one_date_and_branch_get_distinct_names(tmp_path):
    """C-20.7 review r1b, finding 9: names are chosen before anything is written, so a dry
    run names what a real run writes."""
    added = [("ours", "C-1.1 (2026-10-14, a): first."), ("ours", "C-1.1 (2026-10-14, b): second.")]
    plan = merge_tool.plan_entries(tmp_path, merge_tool.Merged("", 0, added=added),
                                   {"ours": "fix-x"}, "2026-10-14", {})
    assert [p.name for p, _, _ in plan.writes] == ["2026-10-14-fix-x.md", "2026-10-14-fix-x-2.md"]


def _conflicting_release(repo):
    """Release also edits the line beside the branch's: a conflict --words settles."""
    git(repo, "checkout", "-q", "release")
    write(repo, merge_tool.PATH, moved(contract(edit(edit(
        CLAUSES, 5, 2, "Release changed this."), 0, 1, "Release words here."))))
    git(repo, "commit", "-q", "-am", "release edits the line beside the branch's")
    git(repo, "checkout", "-q", "fix/thing")
    assert git(repo, "merge", "release", check=False).returncode == 1


def test_a_rerun_stages_the_entry_the_first_run_wrote(repo):
    """C-20.7 review r1b, finding 3: after a run that left a conflict, a rerun with --words
    keeps the entry the first run wrote staged; its printed `git add` then commits all."""
    _conflicting_release(repo)
    first = run_tool(repo)
    assert first.returncode == 1 and "1 conflict(s) left" in first.stdout
    second = run_tool(repo, "--words")
    assert second.returncode == 0, second.stdout + second.stderr
    entry = f"{merge_tool.HISTORY}/2026-10-11-fix-thing.md"
    assert sorted(p.name for p in (repo / merge_tool.HISTORY).glob("*.md")) == [
        "2026-10-11-fix-thing.md"]
    add = next(line for line in second.stdout.splitlines() if line.startswith("then: "))
    git(repo, *add.removeprefix("then: git ").split())
    git(repo, "commit", "-q", "--no-edit")
    assert "the branch's entry" in git(repo, "show", f"HEAD:{entry}").stdout


def test_a_resolved_contract_is_never_rebuilt(repo):
    """C-20.7 finding 4: once the contract is resolved and added, a rerun refuses and
    leaves the person's resolution in the file and the index."""
    assert git(repo, "merge", "release", check=False).returncode == 1
    resolved = moved(contract(edit(CLAUSES, 0, 0, "A PERSON resolved this.")))
    write(repo, merge_tool.PATH, resolved)
    git(repo, "add", merge_tool.PATH)
    again = run_tool(repo)
    assert again.returncode == 2 and "not unmerged" in again.stderr
    assert (repo / merge_tool.PATH).read_text() == resolved
    assert git(repo, "show", f":{merge_tool.PATH}").stdout == resolved


def test_a_merge_that_leaves_the_contract_alone_is_refused(tmp_path):
    """C-20.7 finding 4: a conflict elsewhere does not make the tool rebuild the contract."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "release")
    write(repo, merge_tool.PATH, moved(contract(CLAUSES)))
    write(repo, "other.txt", "base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "fix/other")
    write(repo, "other.txt", "ours\n")
    git(repo, "commit", "-q", "-am", "ours")
    git(repo, "checkout", "-q", "release")
    write(repo, "other.txt", "theirs\n")
    git(repo, "commit", "-q", "-am", "theirs")
    assert git(repo, "merge", "fix/other", check=False).returncode == 1
    before = (repo / merge_tool.PATH).read_text()
    refused = run_tool(repo)
    assert refused.returncode == 2 and "not unmerged" in refused.stderr
    assert (repo / merge_tool.PATH).read_text() == before


def test_a_detached_head_must_name_its_branch(repo):
    """C-20.7 finding 12: on a detached HEAD the entries need a branch name to be named
    after; with --ours-branch they get it."""
    git(repo, "checkout", "-q", "--detach")
    assert git(repo, "merge", "release", check=False).returncode == 1
    refused = run_tool(repo)
    assert refused.returncode == 2 and "--ours-branch" in refused.stderr
    named = run_tool(repo, "--ours-branch", "fix/thing")
    assert named.returncode == 0
    assert (repo / merge_tool.HISTORY / "2026-10-11-fix-thing.md").exists()


# Review r2 of #218 (findings 1-3): the landing path when a conflict is left.

def test_an_entry_reaches_the_commit_when_a_person_resolves_the_rest(repo):
    """C-20.7 review r2, finding 1: with a conflict left, the entry is staged already, so
    a person who resolves the contract and adds only it still commits the entry."""
    _conflicting_release(repo)
    first = run_tool(repo)
    assert first.returncode == 1
    assert "after resolving the conflicts: git add docs/acceptance-contract.md" in first.stdout
    text = (repo / merge_tool.PATH).read_text()
    size = merge_tool.marker_size(text) - 1     # the markers the tool chose
    lines, out, skip = text.split("\n"), [], False
    for line in lines:                          # take ours for each hunk, by hand
        if line.startswith("<" * size + " "):
            skip = False
            continue
        if line.startswith("|" * size + " "):
            skip = True
            continue
        if line == "=" * size:
            skip = True
            continue
        if line.startswith(">" * size + " "):
            skip = False
            continue
        if not skip:
            out.append(line)
    write(repo, merge_tool.PATH, "\n".join(out))
    git(repo, "add", merge_tool.PATH)
    git(repo, "commit", "-q", "--no-edit")
    entry = f"{merge_tool.HISTORY}/2026-10-11-fix-thing.md"
    assert "the branch's entry" in git(repo, "show", f"HEAD:{entry}").stdout
    assert git(repo, "status", "--porcelain").stdout == ""


def test_a_hand_resolution_is_never_overwritten(repo):
    """C-20.7 review r2, finding 2: after a run, a person resolves the contract by hand
    without adding it; a rerun refuses, and with --force rebuilds after a backup."""
    _conflicting_release(repo)
    assert run_tool(repo).returncode == 1
    hand = "# Resolved by a PERSON\n"
    write(repo, merge_tool.PATH, hand)
    refused = run_tool(repo, "--words")
    assert refused.returncode == 2 and "has changed since this tool wrote it" in refused.stderr
    assert (repo / merge_tool.PATH).read_text() == hand
    forced = run_tool(repo, "--words", "--force")
    assert forced.returncode == 0
    backup = git(repo, "rev-parse", "--git-path", "contract-merge-backup.md").stdout.strip()
    assert (repo / backup).read_text() == hand


def test_a_resolution_made_before_the_first_run_is_never_overwritten(repo):
    """C-20.7 review r2, finding 2: a contract with neither Git's markers nor the tool's
    last result is a person's; the first run refuses too."""
    assert git(repo, "merge", "release", check=False).returncode == 1
    write(repo, merge_tool.PATH, "# Resolved by a PERSON before any run\n")
    refused = run_tool(repo)
    assert refused.returncode == 2 and "since Git left it conflicted" in refused.stderr


def test_an_entry_file_a_person_edited_is_kept_on_rerun(repo):
    """C-20.7 review r2, finding 3: a rerun neither overwrites a person's edit of an entry
    file the tool wrote nor writes a second copy; the edit stays staged."""
    _conflicting_release(repo)
    assert run_tool(repo).returncode == 1
    entry = repo / merge_tool.HISTORY / "2026-10-11-fix-thing.md"
    entry.write_text("C-1.1 (2026-10-11, z): the branch's entry, reworded by a person.\n")
    again = run_tool(repo, "--words")
    assert again.returncode == 0, again.stderr
    assert "kept: changed since this tool wrote it" in again.stdout
    assert sorted(p.name for p in (repo / merge_tool.HISTORY).glob("*.md")) == [
        "2026-10-11-fix-thing.md"]
    assert "reworded by a person" in entry.read_text()
    assert "reworded by a person" in git(repo, "show", f":{merge_tool.HISTORY}/{entry.name}").stdout


# Review r2, finding 4, as it happens with the mirror pull requests (#167 to #173): siblings
# share an entry, the first to land writes its round, and the second has another.

def test_a_second_round_of_a_landed_entry_is_not_written_beside_it(tmp_path):
    """C-20.7 when both siblings changed the shared entry, the second's round is named for
    a person, beside the file that holds the first's; nothing is written."""
    root = tmp_path / "contract-history"
    root.mkdir()
    head = "C-23.28, with C-6.4 and C-17.3 (2026-10-10; incident that day: Max opened a copy"
    (root / "2026-10-10-fix-first.md").write_text(head + ", round two.\n")
    result = merge_tool.Merged("", 0, added=[("ours", head + ", round three.")])
    plan = merge_tool.plan_entries(root, result, {"ours": "fix-second"}, "2026-10-14", {})
    assert plan.writes == []
    assert len(plan.people) == 1 and "2026-10-10-fix-first.md" in plan.people[0]


def test_a_criss_cross_base_is_matched_against_the_files(tmp_path):
    """C-20.7 with a base Git built from several (markers in its list), a side's new
    entries are those no file holds; nothing is read as removed or edited."""
    root = tmp_path / "contract-history"
    (root / "version-3").mkdir(parents=True)
    (root / "version-3" / "001.md").write_text(ENTRIES[1] + "\n")
    (root / "2026-10-10-fix-x.md").write_text(ENTRIES[0] + "\n")
    ours = contract(CLAUSES, ["C-1.3 (2026-10-12, n): new.", *ENTRIES])
    result = merge_tool.Merged("", 0, dropped=[("ours", "noise")], replaced=[("ours", "a", "b")])
    merge_tool.against_files(result, ours, moved(contract(CLAUSES)), root)
    assert result.added == [("ours", "C-1.3 (2026-10-12, n): new.")]
    assert (result.dropped, result.replaced) == ([], [])
