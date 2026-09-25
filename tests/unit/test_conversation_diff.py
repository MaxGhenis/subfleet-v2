"""C-26.14, C-26.10, C-25.5 (design D-25): a turn's changes, from two working-tree snapshots."""

from __future__ import annotations

import base64
import random
import re
import shutil
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
    """C-26.14, C-26.10: two snapshots through a temporary index; the result lists each
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
    """C-26.14: the unified diff is bounded; the cut is at a whole line and flagged."""
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
    """C-26.14: at most `max_files` files are listed; the stats still count them all."""
    _, start = diff.snapshot(repository)
    for n in range(5):
        (repository / f"f{n}.txt").write_text(f"{n}\n")
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end, max_files=2)
    assert len(result["files"]) == 2 and result["files_truncated"] is True
    assert result["stats"]["files"] == 5 and result["stats"]["additions"] == 5


def test_a_path_names_one_file_from_the_top_level_whatever_the_workspace(repository):
    """C-26.14: `path` is relative to the checkout's top level, also for a workspace
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
    """C-26.14: a path is relative, with no '.' or '..' parts; anything else is exit 2."""
    with pytest.raises(ValueError):
        diff.pathspec(bad)


def test_no_path_means_every_file():
    """C-26.14: no `path` (None or empty) means every file; a relative path is kept as given."""
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
    """C-26.14: an unreferenced snapshot git has pruned is reported as gone."""
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
    """C-6.8, C-26.14: every diff call is capped; a call past the cap is a transient
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
    """C-26.14: a `-z` listing cut mid-record keeps only whole records."""
    names = b"M\0a.txt\0R100\0from.txt\0to.txt\0A\0partial/pa"
    counts = b"1\t2\ta.txt\0" b"0\t0\t\0from.txt\0to.txt\0" b"3\t0\tpartial/pa"
    files = diff._files(names, counts)
    assert files == [
        {"path": "a.txt", "status": "modified", "additions": 1, "deletions": 2, "binary": False},
        {"path": "to.txt", "status": "renamed", "additions": 0, "deletions": 0, "binary": False,
         "from": "from.txt"},
    ]
    assert diff._files(b"R100\0from.txt\0", b"") == []


def test_a_directory_path_selects_every_changed_file_under_it(repository):
    """C-26.14: `path` names a file or a directory; a directory selects every changed
    file under it, and a name that only starts like one selects nothing."""
    _, start = diff.snapshot(repository)
    (repository / "sub" / "inner.txt").write_text("changed\n")
    (repository / "sub" / "deeper").mkdir()
    (repository / "sub" / "deeper" / "new.txt").write_text("new\n")
    (repository / "tracked.txt").write_text("changed outside\n")
    _, end = diff.snapshot(repository)
    under = diff.build(repository, start, end, path="sub")
    assert sorted(f["path"] for f in under["files"]) == ["sub/deeper/new.txt", "sub/inner.txt"]
    assert "tracked.txt" not in under["diff"] and under["stats"]["files"] == 2
    assert diff.build(repository, start, end, path="su")["files"] == []


def test_a_listing_past_its_bound_says_it_is_incomplete(repository):
    """C-26.14: when git's file listing passes its bound (4 MiB), the stats count the
    files it listed and say `complete: false`, `files_truncated` is true, and every
    file that is listed is a whole record."""
    _, start = diff.snapshot(repository)
    names = [f"file-{n:03}.txt" for n in range(60)]
    for name in names:
        (repository / name).write_text(f"{name}\n")
    _, end = diff.snapshot(repository)
    whole = diff.build(repository, start, end)
    assert whole["stats"] == {"files": 60, "additions": 60, "deletions": 0, "complete": True}
    assert whole["files_truncated"] is False
    cut = diff.build(repository, start, end, list_bytes=400)
    assert cut["stats"]["complete"] is False and cut["files_truncated"] is True
    listed = [f["path"] for f in cut["files"]]
    assert 0 < len(listed) < 60 and set(listed) <= set(names)
    assert cut["stats"]["files"] == len(listed)
    assert all(f["status"] == "added" for f in cut["files"])


def test_a_workspace_git_cannot_open_is_gone_not_pruned(repository, tmp_path):
    """C-26.14: a removed linked worktree, or a moved checkout, answers `workspace-gone`
    although the repository still holds both snapshots; `snapshot-pruned` stays for a
    repository that answers but lacks the object."""
    linked = tmp_path / "linked"
    git(repository, "worktree", "add", "-q", "-b", "task/linked", str(linked))
    _, start = diff.snapshot(linked)
    (linked / "tracked.txt").write_text("changed in the worktree\n")
    _, end = diff.snapshot(linked)
    assert diff.build(linked, start, end)["files"][0]["path"] == "tracked.txt"
    git(repository, "worktree", "remove", "--force", str(linked))
    assert diff.have_tree(repository, end)
    with pytest.raises(diff.Unavailable) as gone:
        diff.build(linked, start, end)
    assert gone.value.reason == "workspace-gone"
    with pytest.raises(diff.Unavailable) as missing:
        diff.build(repository, start, "0" * 40)
    assert missing.value.reason == "snapshot-pruned"
    moved = tmp_path / "moved"
    shutil.move(str(repository), str(moved))
    with pytest.raises(diff.Unavailable) as moved_away:
        diff.build(repository, start, end)
    assert moved_away.value.reason == "workspace-gone"
    assert diff.build(moved, start, end)["files"][0]["path"] == "tracked.txt"


# --- private keys a diff shows only part of (C-25.5, C-23.14) --------------------

def key_lines(seed: int, size: int = 1190) -> list[str]:
    """A private key's body: base64 of random bytes, 64 characters a line."""
    raw = base64.b64encode(random.Random(seed).randbytes(size)).decode()
    return [raw[n:n + 64] for n in range(0, len(raw), 64)]


def pem(body: list[str], indent: str = "") -> str:
    return "".join(f"{indent}{line}\n" for line in
                   ["-----BEGIN RSA PRIVATE KEY-----", *body, "-----END RSA PRIVATE KEY-----"])


def raw_patch_bytes(repo, old: str, new: str) -> bytes:
    """What git hands `build` before any cut, decoding or scrub."""
    return subprocess.run(["git", "-C", str(repo), "diff-tree", "-p", "-r", "-M", "--no-ext-diff", "--no-textconv",
                           "--no-color", "--src-prefix=a/", "--dst-prefix=b/", old, new],
                          check=True, capture_output=True).stdout


def raw_patch(repo, old: str, new: str) -> str:
    return raw_patch_bytes(repo, old, new).decode()


def shows_none_of(result: dict, body: list[str]) -> bool:
    return not any(line in result["diff"] for line in body)


def hunks_keep_their_counts(text: str) -> bool:
    """Every whole hunk has the lines its header counts (the last may be cut short)."""
    header = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
    lines, ok, i = text.split("\n"), True, 0
    while i < len(lines):
        match = header.match(lines[i])
        i += 1
        if not match:
            continue
        old = int(match.group(1) if match.group(1) is not None else 1)
        new = int(match.group(2) if match.group(2) is not None else 1)
        while i < len(lines) and (old or new) and lines[i][:1] in ("+", "-", " ", "\\"):
            prefix = lines[i][:1]
            old -= prefix in ("-", " ")
            new -= prefix in ("+", " ")
            i += 1
        ok = ok and (old, new) == (0, 0)
    return ok


def test_a_whole_key_is_replaced_line_by_line(repository):
    """C-25.5, C-26.14: a key with both armour lines in the diff is removed, each line
    replaced in place, so the hunk keeps its line counts."""
    _, start = diff.snapshot(repository)
    body = key_lines(1)
    (repository / "id_rsa").write_text(pem(body))
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end)
    assert shows_none_of(result, body) and "BEGIN RSA PRIVATE KEY" not in result["diff"]
    assert result["scrubbed"] >= 1 and hunks_keep_their_counts(result["diff"])
    assert result["diff"].count(f"+{diff.KEY_REDACTED}\n") == len(body) + 2
    assert result["files"][0]["additions"] == len(body) + 2


def test_a_key_the_cut_lands_in_is_never_shown(repository):
    """C-25.5, C-26.14: wherever the 512 KiB cut lands inside a key, after its BEGIN,
    among its body lines, or just before its END, none of the key is shown."""
    _, start = diff.snapshot(repository)
    body = key_lines(2)
    (repository / "a-first.txt").write_text("before the key\n")
    (repository / "id_rsa").write_text(pem(body))
    _, end = diff.snapshot(repository)
    patch = raw_patch(repository, start, end)
    begin = patch.index("+-----BEGIN RSA PRIVATE KEY-----\n")
    cuts = [begin + len("+-----BEGIN RSA PRIVATE KEY-----\n")]            # just after BEGIN
    cuts += [patch.index(f"+{body[n]}\n") + 65 + 1 for n in (0, 9, len(body) - 2)]   # after a body line
    cuts += [patch.index(f"+{body[5]}\n") + 30]                          # inside a body line
    cuts += [patch.index("+-----END RSA PRIVATE KEY-----")]                # just before END
    for cut in cuts:
        result = diff.build(repository, start, end, max_bytes=cut)
        assert result["truncated"] is True, cut
        assert shows_none_of(result, body), cut
        assert result["scrubbed"] >= 1 and result["diff"].endswith("\n"), cut
        assert len(result["diff"].encode()) <= cut, cut
        assert "+before the key\n" in result["diff"], cut


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_a_hunk_whose_context_reaches_into_a_key_shows_none_of_it(repository, eol):
    """C-25.5, C-26.14: an edit just below a key's END or just above its BEGIN puts
    key lines in the hunk's context without the other armour line; an edit inside a
    key shows neither armour line; none of the key is shown either way, with either
    line ending, and the edits and the lines around them are."""
    body = key_lines(3)
    config = ("name: service\ntls:\n  key: |\n" + pem(body, indent="    ") + "port: 443\nhost: example\n")
    (repository / "config.yaml").write_text(config, newline=eol)
    git(repository, "add", "config.yaml")
    git(repository, "commit", "-m", "config")
    _, start = diff.snapshot(repository)
    edited = config.replace("  key: |", "  key: |-").replace("port: 443", "port: 444")
    (repository / "config.yaml").write_text(edited, newline=eol)
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end)
    assert result["diff"].count("@@ -") == 2                   # one hunk at each end of the key
    assert shows_none_of(result, body) and result["scrubbed"] >= 2
    assert f"+  key: |-{eol}" in result["diff"] and f"+port: 444{eol}" in result["diff"]
    assert f" host: example{eol}" in result["diff"] and f" tls:{eol}" in result["diff"]
    assert hunks_keep_their_counts(result["diff"])

    _, start = diff.snapshot(repository)
    (repository / "config.yaml").write_text(edited.replace(body[12], body[12][::-1]), newline=eol)
    _, inside = diff.snapshot(repository)
    result = diff.build(repository, start, inside)
    assert "BEGIN" not in result["diff"] and "END" not in result["diff"]
    assert shows_none_of(result, body) and body[12][::-1] not in result["diff"]
    assert f"+    {diff.ENCODED_OMITTED}\n" in result["diff"] and hunks_keep_their_counts(result["diff"])


def test_a_hunk_header_never_repeats_a_key_line(repository):
    """C-25.5, C-26.14: git's hunk header repeats the nearest line above the hunk that
    starts with a letter, which in a key file is a body line of the key."""
    body = key_lines(4)
    (repository / "id_rsa").write_text(pem(body) + "\n\n\n\nport: 443\nhost: a\n")
    git(repository, "add", "id_rsa")
    git(repository, "commit", "-m", "key")
    _, start = diff.snapshot(repository)
    (repository / "id_rsa").write_text(pem(body) + "\n\n\n\nport: 444\nhost: a\n")
    _, end = diff.snapshot(repository)
    assert any(line in raw_patch(repository, start, end) for line in body)      # git does repeat one
    result = diff.build(repository, start, end)
    assert shows_none_of(result, body) and result["scrubbed"] >= 1
    assert re.search(r"^@@ -\d+,\d+ \+\d+,\d+ @@ \[BASE64 OMITTED\]$", result["diff"], re.M)
    assert "+port: 444\n" in result["diff"]


def test_a_hunk_header_never_repeats_a_key_s_short_last_line(repository):
    """C-25.5, C-26.14: a hunk that starts just below a key's END has the key's last
    body line, which is short, as its header's function context (END and blank lines do
    not start with a letter); that line is not shown either."""
    body = key_lines(6, size=1157)                        # 24 full lines and one of 8, padded
    assert len(body[-1]) == 8 and body[-1].endswith("=")
    tail = "\n\n\n\n"
    (repository / "id_rsa").write_text(pem(body) + tail + "port: 443\n")
    git(repository, "add", "id_rsa")
    git(repository, "commit", "-m", "key")
    _, start = diff.snapshot(repository)
    (repository / "id_rsa").write_text(pem(body) + tail + "port: 444\n")
    _, end = diff.snapshot(repository)
    assert f"@@ {body[-1]}" in raw_patch(repository, start, end)          # git does repeat it
    result = diff.build(repository, start, end)
    assert shows_none_of(result, body) and result["scrubbed"] >= 1
    assert re.search(r"^@@ -\d+,\d+ \+\d+,\d+ @@ \[BASE64 OMITTED\]$", result["diff"], re.M)
    assert "+port: 444\n" in result["diff"] and hunks_keep_their_counts(result["diff"])


def test_ordinary_lines_are_not_taken_for_a_key(repository):
    """C-25.5: digests (one case), words, code and a lone encoded-looking line inside a
    hunk are shown as they are; only key-shaped runs are removed."""
    _, start = diff.snapshot(repository)
    digests = [f"{random.Random(n).getrandbits(160):040x}" for n in range(6)]
    code = ["def handler(event, context):", "    return {'statusCode': 200}", "Configuration", "README"]
    (repository / ".git-blame-ignore-revs").write_text("".join(f"{d}\n" for d in digests))
    (repository / "handler.py").write_text("".join(f"{line}\n" for line in code))
    lone = base64.b64encode(random.Random(9).randbytes(33)).decode()
    (repository / "notes.txt").write_text(f"first\nsecond\n{lone}\nthird\nfourth\n")
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end)
    for line in digests + code:
        assert f"+{line}\n" in result["diff"]
    assert f"+{lone}\n" in result["diff"] and result["scrubbed"] == 0


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_a_credential_header_in_a_diff_line_is_scrubbed_whole(repository, eol):
    """C-25.5, C-26.14: each line is scrubbed without its diff prefix, so the scrubber's
    rules that start at a line's beginning apply to added and removed lines alike: an
    `Authorization:` or `Cookie:` line loses its whole value, not only its first word or
    first cookie, with either line ending, and every hunk keeps its line counts."""
    _, start = diff.snapshot(repository)
    basic = base64.b64encode(b"deploy:correct horse battery staple").decode()
    request = (f"GET /v1/items HTTP/1.1{eol}Authorization: Basic {basic}{eol}"
               f"Cookie: session=s3cr3t-value; csrf=t0ken-value{eol}")
    (repository / "request.http").write_text(request, newline="")
    _, end = diff.snapshot(repository)
    added = diff.build(repository, start, end)
    assert basic not in added["diff"] and "s3cr3t-value" not in added["diff"] and "t0ken-value" not in added["diff"]
    assert f"+Authorization: [REDACTED]{eol}" in added["diff"] and f"+Cookie: [REDACTED]{eol}" in added["diff"]
    assert f"+GET /v1/items HTTP/1.1{eol}" in added["diff"] and hunks_keep_their_counts(added["diff"])

    git(repository, "add", "request.http")
    git(repository, "commit", "-m", "request")
    _, start = diff.snapshot(repository)
    (repository / "request.http").write_text(f"GET /v1/items HTTP/1.1{eol}Accept: */*{eol}", newline="")
    _, end = diff.snapshot(repository)
    removed = diff.build(repository, start, end)
    assert basic not in removed["diff"] and "s3cr3t-value" not in removed["diff"]
    assert "t0ken-value" not in removed["diff"]
    assert f"-Authorization: [REDACTED]{eol}" in removed["diff"] and f"-Cookie: [REDACTED]{eol}" in removed["diff"]
    assert f"+Accept: */*{eol}" in removed["diff"] and hunks_keep_their_counts(removed["diff"])
    assert removed["scrubbed"] >= 2


def test_a_value_at_the_start_of_a_removed_or_added_line_is_scrubbed(repository):
    """C-25.5, C-26.14: the token and JWT rules refuse a value with a `-` just before it
    and the base64 rule counts a `+` as part of its run, so on a prefixed line a removed
    token or JWT was shown whole and an added base64 line lost its `+`; scrubbed without
    its prefix, each value is replaced and each line keeps its prefix."""
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1"
    jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
           "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
    (repository / "tokens.txt").write_text(f"first\n{token}\n{jwt}\nlast\n")
    git(repository, "add", "tokens.txt")
    git(repository, "commit", "-m", "tokens")
    _, start = diff.snapshot(repository)
    blob = base64.b64encode(random.Random(5).randbytes(150)).decode()
    (repository / "tokens.txt").write_text("first\nlast\n")
    (repository / "blob.txt").write_text(f"first\nsecond\n{blob}\nthird\nfourth\n")
    _, end = diff.snapshot(repository)
    result = diff.build(repository, start, end)
    assert token not in result["diff"] and jwt not in result["diff"] and blob not in result["diff"]
    assert "-[REDACTED]\n" in result["diff"] and result["diff"].count("-[REDACTED]\n") == 2
    assert "+[BASE64 OMITTED]\n" in result["diff"] and "+second\n" in result["diff"]
    assert hunks_keep_their_counts(result["diff"]) and result["scrubbed"] >= 3


def test_a_hunk_header_repeating_a_credential_line_is_scrubbed(repository):
    """C-25.5, C-26.14: git's hunk header repeats the nearest line above the hunk that
    starts with a letter; when that line is an `Authorization:` header its value is
    scrubbed there as it would be on the line itself."""
    basic = base64.b64encode(b"deploy:correct horse battery staple").decode()
    lines = [f"Authorization: Basic {basic}"] + [f"  line {n}" for n in range(8)]
    (repository / "request.http").write_text("\n".join(lines) + "\n")
    git(repository, "add", "request.http")
    git(repository, "commit", "-m", "request")
    _, start = diff.snapshot(repository)
    (repository / "request.http").write_text("\n".join(lines[:-1] + ["  line changed"]) + "\n")
    _, end = diff.snapshot(repository)
    assert f"@@ Authorization: Basic {basic}" in raw_patch(repository, start, end)   # git does repeat it
    result = diff.build(repository, start, end)
    assert basic not in result["diff"] and result["scrubbed"] >= 1
    assert re.search(r"^@@ -\d+,\d+ \+\d+,\d+ @@ Authorization: \[REDACTED\]$", result["diff"], re.M)
    assert "+  line changed\n" in result["diff"] and hunks_keep_their_counts(result["diff"])


def test_the_diff_never_passes_its_bound_after_decoding(repository):
    """C-26.14: the 512 KiB bound is on the text a result carries. A byte that is not
    UTF-8 becomes U+FFFD, three bytes, so a patch under the bound can decode past it;
    the text is cut again after a whole line, and says `truncated: true`."""
    _, start = diff.snapshot(repository)
    latin = b"".join(b"caf\xe9 cr\xe8me br\xfbl\xe9e %05d\n" % n for n in range(400))
    assert b"\0" not in latin                                 # git reads it as text
    (repository / "menu.txt").write_bytes(latin)
    _, end = diff.snapshot(repository)
    whole = diff.build(repository, start, end)
    raw = len(raw_patch_bytes(repository, start, end))
    assert whole["truncated"] is False and len(whole["diff"].encode()) > raw
    limit = raw + 1000                                        # git's output fits; the decoded text does not
    assert len(whole["diff"].encode()) > limit
    result = diff.build(repository, start, end, max_bytes=limit)
    assert result["truncated"] is True
    assert len(result["diff"].encode()) <= limit and result["diff"].endswith("\n")
    assert whole["diff"].startswith(result["diff"])
    assert result["files"][0]["additions"] == 400 and result["stats"]["complete"] is True
