"""The editor's commands and path checks (C-31.15).

Every path is resolved with realpath; anything outside the worktree and the attempt
folder is refused, including through a symlink that leaves the worktree, and writes
go only where the shell's sandbox could write.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from subfleet.api_agent import cli, hooks, sandbox
from subfleet.api_agent.editor import Editor, _Refused


@pytest.fixture
def roots(tmp_path):
    work = tmp_path / "work"
    attempt = tmp_path / "attempt"
    outside = tmp_path / "outside"
    for folder in (work / ".git", attempt, outside):
        folder.mkdir(parents=True)
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    (outside / "secret.txt").write_text("outside\n")
    return work, attempt, outside


def make(roots, *, mode=cli.WORKSPACE_WRITE, guard=None):
    work, attempt, _ = roots
    layout = sandbox.layout(workdir=work, attempt=attempt, sandbox=mode, network=False,
                            environ={"HOME": str(work.parent)})
    return Editor(layout, guard, layout.control / "backups", readonly=mode != cli.WORKSPACE_WRITE), layout


def test_view_numbers_lines_and_takes_a_range(roots):
    editor, _ = make(roots)
    shown = editor.run({"command": "view", "path": "a.txt"})
    assert not shown.is_error
    assert shown.text.splitlines() == ["     1\tone", "     2\ttwo", "     3\tthree"]
    window = editor.run({"command": "view", "path": str(roots[0] / "a.txt"), "view_range": [2, -1]})
    assert window.text.splitlines() == ["     2\ttwo", "     3\tthree"]
    assert editor.run({"command": "view", "path": "a.txt", "view_range": [3, 9]}).is_error


def test_view_lists_a_folder_without_hidden_entries(roots):
    editor, _ = make(roots)
    (roots[0] / "pkg" / "sub" / "deep").mkdir(parents=True)
    header, *entries = editor.run({"command": "view", "path": "."}).text.splitlines()
    work = os.path.realpath(roots[0])
    assert "hidden items excluded" in header
    assert entries == [f"{work}/a.txt", f"{work}/pkg/", f"{work}/pkg/sub/"]  # two levels: no .git, no deep/


@pytest.mark.parametrize("command", [
    {"command": "view"},
    {"command": "create", "file_text": "x"},
    {"command": "str_replace", "old_str": "outside", "new_str": "x"},
    {"command": "insert", "insert_line": 0, "insert_text": "x"},
])
def test_paths_outside_are_refused(roots, command):
    editor, _ = make(roots)
    work, _, outside = roots
    for path in (str(outside / "secret.txt"), "../outside/secret.txt", str(work / ".." / "outside" / "secret.txt"),
                 "/etc/hosts"):
        result = editor.run(dict(command, path=path))
        assert result.is_error and result.denied, path
        assert "outside" in result.text
    assert (outside / "secret.txt").read_text() == "outside\n"


def test_a_symlink_that_leaves_the_worktree_is_refused(roots):
    editor, _ = make(roots)
    work, _, outside = roots
    (work / "escape.txt").symlink_to(outside / "secret.txt")
    (work / "escape-dir").symlink_to(outside)
    for path in ("escape.txt", "escape-dir/secret.txt", "escape-dir/new.txt"):
        for command in ({"command": "view"}, {"command": "create", "file_text": "pwned"},
                        {"command": "str_replace", "old_str": "outside", "new_str": "pwned"},
                        {"command": "insert", "insert_line": 0, "insert_text": "pwned"}):
            result = editor.run(dict(command, path=path))
            assert result.is_error and result.denied, (path, command)
    assert (outside / "secret.txt").read_text() == "outside\n"
    assert not (outside / "new.txt").exists()


def test_a_folder_swapped_for_a_link_after_the_check_leads_nowhere(roots):
    """A background job in the shell could swap a folder for a symlink between the
    path check and the write. The editor walks down to the target's folder without
    following a link, so it neither writes outside nor makes folders there."""
    editor, _ = make(roots)
    work, _, outside = roots
    real = Path(os.path.realpath(work)) / "a" / "b" / "new.txt"  # what the check resolved
    (work / "a").symlink_to(outside)  # the swap
    with pytest.raises(_Refused) as refused:
        editor._write_text(real, "pwned\n", create=True)
    assert refused.value.denied
    assert sorted(path.name for path in outside.iterdir()) == ["secret.txt"]


def test_a_symlink_inside_the_worktree_works(roots):
    editor, _ = make(roots)
    work = roots[0]
    (work / "alias.txt").symlink_to(work / "a.txt")
    result = editor.run({"command": "str_replace", "path": "alias.txt", "old_str": "two", "new_str": "2"})
    assert not result.is_error, result.text
    assert (work / "a.txt").read_text() == "one\n2\nthree\n"
    assert (work / "alias.txt").is_symlink()


def test_git_and_records_are_not_writable(roots):
    editor, layout = make(roots)
    work, attempt, _ = roots
    for path in (str(work / ".git" / "config"), str(work / ".git" / "hooks" / "pre-commit"),
                 str(attempt / "stream.jsonl"), str(attempt / "harness" / "x.sh")):
        result = editor.run({"command": "create", "path": path, "file_text": "x"})
        assert result.is_error and result.denied, path
    assert not (work / ".git" / "config").exists()
    ok = editor.run({"command": "create", "path": str(attempt / "notes.md"), "file_text": "fine"})
    assert not ok.is_error, ok.text


def test_create_overwrites_with_a_backup_and_makes_folders(roots):
    editor, layout = make(roots)
    work = roots[0]
    made = editor.run({"command": "create", "path": "new/deep/b.py", "file_text": "print(1)\n"})
    assert not made.is_error and (work / "new" / "deep" / "b.py").read_text() == "print(1)\n"
    again = editor.run({"command": "create", "path": "a.txt", "file_text": "replaced\n"})
    assert not again.is_error and "backed up" in again.text
    assert (work / "a.txt").read_text() == "replaced\n"
    backups = list((layout.control / "backups").iterdir())
    assert [path.read_text() for path in backups] == ["one\ntwo\nthree\n"]
    for folder in (layout.tmp, layout.attempt):  # the other writable roots, nested
        made = editor.run({"command": "create", "path": str(folder / "x" / "y" / "n.txt"), "file_text": "n\n"})
        assert not made.is_error, made.text
        assert (folder / "x" / "y" / "n.txt").read_text() == "n\n"


def test_str_replace_needs_exactly_one_match(roots):
    editor, _ = make(roots)
    work = roots[0]
    (work / "dup.txt").write_text("x\nx\n")
    none = editor.run({"command": "str_replace", "path": "a.txt", "old_str": "four", "new_str": "4"})
    assert none.is_error and "does not appear" in none.text and not none.denied
    many = editor.run({"command": "str_replace", "path": "dup.txt", "old_str": "x", "new_str": "y"})
    assert many.is_error and "2 times" in many.text and "1, 2" in many.text
    assert (work / "dup.txt").read_text() == "x\nx\n"
    one = editor.run({"command": "str_replace", "path": "a.txt", "old_str": "two\n", "new_str": ""})
    assert not one.is_error and (work / "a.txt").read_text() == "one\nthree\n"


def test_insert_after_a_line(roots):
    editor, _ = make(roots)
    work = roots[0]
    assert not editor.run({"command": "insert", "path": "a.txt", "insert_line": 0, "insert_text": "zero"}).is_error
    assert not editor.run({"command": "insert", "path": "a.txt", "insert_line": 4,
                           "insert_text": "four\n"}).is_error
    assert (work / "a.txt").read_text() == "zero\none\ntwo\nthree\nfour\n"
    assert editor.run({"command": "insert", "path": "a.txt", "insert_line": 9, "insert_text": "x"}).is_error


def test_hard_links_are_not_written(roots):
    editor, _ = make(roots)
    work, _, outside = roots
    os.link(outside / "secret.txt", work / "linked.txt")
    result = editor.run({"command": "str_replace", "path": "linked.txt", "old_str": "outside", "new_str": "x"})
    assert result.is_error and result.denied
    assert (outside / "secret.txt").read_text() == "outside\n"


def test_read_only_jobs_only_view(roots):
    editor, _ = make(roots, mode=cli.READ_ONLY)
    assert not editor.run({"command": "view", "path": "a.txt"}).is_error
    for command in ({"command": "create", "path": "b.txt", "file_text": "x"},
                    {"command": "str_replace", "path": "a.txt", "old_str": "one", "new_str": "1"},
                    {"command": "insert", "path": "a.txt", "insert_line": 0, "insert_text": "x"}):
        result = editor.run(command)
        assert result.is_error and result.denied
    assert (roots[0] / "a.txt").read_text() == "one\ntwo\nthree\n"


def test_the_guard_judges_each_write_as_a_patch(roots):
    seen = []

    def guard(patch):
        seen.append(patch)
        return hooks.Verdict(False, "[hf-dest] no") if "forbidden" in patch else hooks.ALLOW

    editor, _ = make(roots, guard=guard)
    work = roots[0]
    refused = editor.run({"command": "create", "path": "c.txt", "file_text": "forbidden\n"})
    assert refused.is_error and refused.denied and "[hf-dest] no" in refused.text
    assert not (work / "c.txt").exists()
    allowed = editor.run({"command": "str_replace", "path": "a.txt", "old_str": "one", "new_str": "uno"})
    assert not allowed.is_error
    assert seen[0].startswith("*** Begin Patch\n*** Add File: ") and "+forbidden" in seen[0]
    assert "*** Update File: " in seen[1] and "-one" in seen[1] and "+uno" in seen[1]
    editor.run({"command": "view", "path": "a.txt"})
    assert len(seen) == 2  # views are not judged


def test_bad_input_is_an_error_not_a_crash(roots):
    editor, _ = make(roots)
    for tool_input in (None, "view", {"command": "undo_edit", "path": "a.txt"}, {"command": "view"},
                       {"command": "view", "path": ""}, {"command": "create", "path": "x"},
                       {"command": "insert", "path": "a.txt", "insert_line": "1", "insert_text": "x"}):
        assert editor.run(tool_input).is_error
