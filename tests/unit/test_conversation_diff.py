"""C-26.13, C-26.10, C-25.5 (design D-25): a turn's changes, from two working-tree snapshots."""

from __future__ import annotations

import subprocess
import sys

import pytest

from subfleet.conversations import diff
from subfleet.salvage import SalvageError


def git(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "task/diff")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("a\nb\nc\n")
    (repo / "old.txt").write_text("gone soon\n")
    (repo / "moving.txt").write_text("one line that moves\nand another\nand a third\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "inner.txt").write_text("inner\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return repo


def private_state(repo) -> tuple:
    """What a snapshot must never change: HEAD, the real index, refs, files, status."""
    files = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*")
             if p.is_file() and ".git" not in p.relative_to(repo).parts}
    return (git(repo, "rev-parse", "HEAD"), (repo / ".git" / "index").read_bytes(),
            git(repo, "for-each-ref"), files, git(repo, "status", "--porcelain=v1"))


def test_every_kind_of_change_is_listed_with_counts_and_nothing_private_moves(repository):
    """C-26.13, C-26.10: two snapshots through a temporary index; the result lists each
    file's status and line counts and a unified diff; HEAD, the real index, the refs and
    the files are as they were, and no ref is written."""
    head, start = diff.snapshot(repository)
    (repository / "tracked.txt").write_text("a\nB\nc\nd\n")
    (repository / "old.txt").unlink()
    (repository / "moving.txt").rename(repository / "moved.txt")
    (repository / "new file é.txt").write_text("fresh\n")
    (repository / "blob.bin").write_bytes(b"\0\1\2binary")
    (repository / "ignored.txt").write_text("never listed\n")
    before = private_state(repository)
    head_after, end = diff.snapshot(repository)
    assert private_state(repository) == before
    assert head_after == head and end != start

    result = diff.build(repository, start, end)
    files = {f["path"]: f for f in result["files"]}
    assert set(files) == {"tracked.txt", "old.txt", "moved.txt", "new file é.txt", "blob.bin"}
    assert files["tracked.txt"] == {"path": "tracked.txt", "status": "modified", "additions": 2,
                                    "deletions": 1, "binary": False}
    assert files["old.txt"]["status"] == "deleted" and files["old.txt"]["deletions"] == 1
    assert files["moved.txt"]["status"] == "renamed" and files["moved.txt"]["from"] == "moving.txt"
    assert files["new file é.txt"]["status"] == "added" and files["new file é.txt"]["additions"] == 1
    assert files["blob.bin"]["binary"] is True and files["blob.bin"]["additions"] is None
    assert result["stats"] == {"files": 5, "additions": 3, "deletions": 2, "complete": True}
    assert not result["truncated"] and not result["files_truncated"]
    assert "diff --git a/tracked.txt b/tracked.txt" in result["diff"]
    assert "\n-b\n+B\n" in result["diff"] and "+fresh" in result["diff"]
    assert "Binary files /dev/null and b/blob.bin differ" in result["diff"]
    assert "ignored.txt" not in result["diff"]
    assert private_state(repository) == before
    assert git(repository, "for-each-ref", "refs/subfleet-salvage") == ""
    assert not list((repository / ".git").glob("subfleet-salvage-*"))


def test_the_diff_is_cut_at_a_line_boundary_and_says_so(repository):
    """C-26.13: the unified diff is bounded; the cut is at a whole line and flagged."""
    _, start = diff.snapshot(repository)
    (repository / "big.txt").write_text("".join(f"line {n}\n" for n in range(5000)))
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end, max_bytes=1000)
    assert result["truncated"] is True
    assert 0 < len(result["diff"].encode()) <= 1000 and result["diff"].endswith("\n")
    assert result["files"][0]["additions"] == 5000         # the counts are not cut
    whole = diff.build(repository, start, end)
    assert whole["truncated"] is False and whole["diff"].count("\n+line ") == 5000


def test_the_file_list_is_bounded_and_the_stats_cover_every_file(repository):
    """C-26.13: at most `max_files` files are listed; the stats still count them all."""
    _, start = diff.snapshot(repository)
    for n in range(5):
        (repository / f"f{n}.txt").write_text(f"{n}\n")
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end, max_files=2)
    assert len(result["files"]) == 2 and result["files_truncated"] is True
    assert result["stats"]["files"] == 5 and result["stats"]["additions"] == 5


def test_a_path_names_one_file_from_the_top_level_whatever_the_workspace(repository):
    """C-26.13: `path` is relative to the checkout's top level, also for a workspace
    in a subdirectory, and is taken literally."""
    _, start = diff.snapshot(repository / "sub")
    (repository / "tracked.txt").write_text("changed\n")
    (repository / "sub" / "inner.txt").write_text("changed too\n")
    (repository / "[x].txt").write_text("glob characters\n")
    _, end = diff.snapshot(repository / "sub")
    whole = diff.build(repository / "sub", start, end)
    assert {f["path"] for f in whole["files"]} == {"tracked.txt", "sub/inner.txt", "[x].txt"}
    one = diff.build(repository / "sub", start, end, path="tracked.txt")
    assert [f["path"] for f in one["files"]] == ["tracked.txt"]
    assert "sub/inner.txt" not in one["diff"] and "+changed\n" in one["diff"]
    literal = diff.build(repository / "sub", start, end, path="[x].txt")
    assert [f["path"] for f in literal["files"]] == ["[x].txt"]
    assert diff.toplevel(repository / "sub") == str(repository.resolve())


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "a/../b", "./a", "a//b", "a/", "x\0y", 7])
def test_a_path_outside_the_checkout_is_refused(bad):
    """C-26.13: a path is relative, with no '.' or '..' parts; anything else is exit 2."""
    with pytest.raises(ValueError):
        diff.pathspec(bad)


def test_no_path_means_every_file():
    assert diff.pathspec(None) is None and diff.pathspec("") is None
    assert diff.pathspec("dir/file name.txt") == "dir/file name.txt"


def test_a_credential_in_a_changed_file_is_scrubbed(repository):
    """C-25.5: the diff text is scrubbed like event text; the counts are git's."""
    _, start = diff.snapshot(repository)
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    (repository / "settings.env").write_text(f'GITHUB_TOKEN="{token}"\nplain=value\n')
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end)
    assert token not in result["diff"] and result["scrubbed"] >= 1
    assert result["files"][0]["additions"] == 2


def test_a_pruned_snapshot_is_unavailable_not_an_error(repository):
    """C-26.13: an unreferenced snapshot git has pruned is reported as gone."""
    _, start = diff.snapshot(repository)
    with pytest.raises(diff.Unavailable) as gone:
        diff.build(repository, start, "0" * 40)
    assert gone.value.reason == "snapshot-pruned"


def test_the_end_snapshot_records_head_after_and_only_brackets_a_started_snapshot(repository, tmp_path):
    """C-26.10: HEAD after always; an end tree only when there is a start tree to compare;
    a commit made by the turn is part of its changes."""
    head, start = diff.snapshot(repository)
    assert diff.end_snapshot(repository, head_before=head, start_tree=None) == {"head_after": head,
                                                                                 "end_tree": None}
    (repository / "tracked.txt").write_text("committed by the turn\n")
    git(repository, "commit", "-am", "turn work")
    end = diff.end_snapshot(repository, head_before=head, start_tree=start)
    assert end["head_after"] != head and end["end_tree"]
    files = diff.build(repository, start, end["end_tree"])["files"]
    assert [f["path"] for f in files] == ["tracked.txt"]
    plain = tmp_path / "plain"
    plain.mkdir()
    assert diff.snapshot(plain) is None
    assert diff.end_snapshot(plain, head_before=None, start_tree=None) == {"head_after": None, "end_tree": None}
    with pytest.raises(SalvageError):
        diff.end_snapshot(plain, head_before=None, start_tree=start)


def test_a_git_call_past_its_cap_is_killed_and_transient(repository, monkeypatch):
    """C-6.8, C-26.13: every diff call is capped; a call past the cap is a transient
    failure, never a partial answer."""
    _, start = diff.snapshot(repository)
    real = subprocess.Popen

    def hang(argv, **kwargs):
        return real([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(diff.subprocess, "Popen", hang)
    with pytest.raises(SalvageError) as late:
        diff._bounded(repository, ("diff-tree", "-p", start, start), 1000, 0.3)
    assert late.value.transient and "git diff-tree timed out after 0.3 s" == str(late.value)


def test_listings_cut_by_the_bound_drop_the_partial_record():
    """C-26.13: a `-z` listing cut mid-record keeps only whole records."""
    names = b"M\0a.txt\0R100\0from.txt\0to.txt\0A\0partial/pa"
    counts = b"1\t2\ta.txt\0" b"0\t0\t\0from.txt\0to.txt\0" b"3\t0\tpartial/pa"
    files = diff._files(names, counts)
    assert files == [
        {"path": "a.txt", "status": "modified", "additions": 1, "deletions": 2, "binary": False},
        {"path": "to.txt", "status": "renamed", "additions": 0, "deletions": 0, "binary": False,
         "from": "from.txt"},
    ]
    assert diff._files(b"R100\0from.txt\0", b"") == []
