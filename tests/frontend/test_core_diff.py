"""The Changes pane's models against diffs the daemon's own code produces (C-26.14, C-25.2, design D-25).

A real checkout is changed in every way `git diff-tree` reports (added, modified,
deleted, renamed, binary, a mode change, names git quotes), bracketed by real
snapshots through the store, and answered by the real `turn.diff` and
`conversation.diff` handlers. The Swift models must carry every field, and the
Swift parser must give each listed file its own section with the right line
numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

import pytest

from subfleet.conversations import diff as turn_diff
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness
from tests.frontend.test_core_protocol import assert_lossless, roundtrip

pytestmark = needs_swift

QUOTED = 'say "hi"\tnow.txt'          # git quotes a name with `"` or a tab even with core.quotepath=false


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def harness():
    root = Path(tempfile.mkdtemp(prefix="sf-app-d-", dir="/tmp"))
    harness = ServiceHarness(root)
    repo = harness.workspace
    git(repo, "init", "-q", "-b", "feature/diff")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "keep.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
    (repo / "gone.txt").write_text("to be removed\n")
    (repo / "old name.txt").write_text("moved as is\n" * 5)
    (repo / "run.sh").write_text("echo hi\n")
    (repo / "logo.bin").write_bytes(b"\x00\x01\x02" * 10)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    yield harness
    harness.close()


def change_everything(repo: Path) -> None:
    (repo / "keep.txt").write_text("one\nTWO\nthree\nfour\nfive\nsix\n")
    (repo / "gone.txt").unlink()
    (repo / "old name.txt").rename(repo / "new name.txt")
    (repo / "run.sh").chmod(0o755)
    (repo / "logo.bin").write_bytes(b"\x00\x09\x02" * 10)
    (repo / "café.txt").write_text("accent\n")
    (repo / QUOTED).write_text("quoted\n")


def turn(harness: ServiceHarness) -> tuple[str, str]:
    """A conversation with one writable turn whose start snapshot is recorded."""
    conversation = harness.create()
    cid = conversation["conversation_id"]
    mid = harness.submit(cid, "change things")["message_id"]
    head, tree = turn_diff.snapshot(harness.workspace)
    harness.store.record_trees(attempt_id="j-1/a1", message_id=mid, conversation_id=cid,
                               workspace=str(harness.workspace), writable=True, started_at="2026-09-25T10:00:00Z",
                               head_before=head, start_tree=tree)
    return cid, mid


def test_c26_14_diff_results_decode_without_losing_a_field(core_probe, tmp_path, harness):
    """Live and ended turn diffs, the conversation's, a path filter, and each unavailable
    shape survive the Swift model unchanged."""
    cid, mid = turn(harness)
    change_everything(harness.workspace)
    live = harness.call("turn.diff", message_id=mid)
    assert live["available"] and live["to"]["live"] is True
    head, tree = turn_diff.snapshot(harness.workspace)
    harness.store.record_trees(attempt_id="j-1/a1", message_id=mid, conversation_id=cid,
                               workspace=str(harness.workspace), writable=True, started_at="2026-09-25T10:00:00Z",
                               head_after=head, end_tree=tree, ended=True)
    # C-26.14: another conversation's turn in the same folder, still running, met this one.
    beside = harness.create(title="Beside")["conversation_id"]
    beside_mid = harness.submit(beside, "also here")["message_id"]
    harness.store.record_trees(attempt_id="j-2/a1", message_id=beside_mid, conversation_id=beside,
                               workspace=str(harness.workspace), writable=True, started_at="2026-09-25T10:00:01Z",
                               head_before=head, start_tree=tree)
    ended = harness.call("turn.diff", message_id=mid)
    assert ended["to"]["live"] is False and ended["from"]["message_id"] == mid
    assert [(s["title"], s["message_ids"], s["to"]) for s in ended["shared"]] == [("Beside", [beside_mid], None)]
    whole = harness.call("conversation.diff", conversation_id=cid)
    assert [s["conversation_id"] for s in whole["shared"]] == [beside]
    one = harness.call("turn.diff", message_id=mid, path="keep.txt")
    other = harness.create()["conversation_id"]
    waiting = harness.submit(other, "not started")["message_id"]
    for op, result in [("turn.diff", live), ("turn.diff", ended), ("conversation.diff", whole), ("turn.diff", one),
                       ("turn.diff", harness.call("turn.diff", message_id=waiting)),
                       ("conversation.diff", harness.call("conversation.diff", conversation_id=other))]:
        assert_lossless(core_probe, tmp_path, op, result)
    statuses = {f["path"]: f["status"] for f in ended["files"]}
    assert statuses == {"keep.txt": "modified", "gone.txt": "deleted", "new name.txt": "renamed",
                        "run.sh": "modified", "logo.bin": "modified", "café.txt": "added", QUOTED: "added"}
    shown = roundtrip(core_probe, tmp_path, "turn.diff", ended)
    assert shown["stats"] == ended["stats"] and shown["to"]["live"] is False


def test_c26_14_the_pane_names_the_nested_repositories_a_live_diff_cannot_show(core_probe, tmp_path, harness):
    """C-13.1, C-26.14 (review of cda4c161, N3): a live `to` lists the nested repositories
    with no commit its snapshot left out. The Swift model keeps the list, and the Changes
    pane says they are not shown, beside a change it does show; a stored end has no list."""
    cid, mid = turn(harness)
    (harness.workspace / "keep.txt").write_text("one\nTWO\nthree\nfour\nfive\n")
    (harness.workspace / "scratch").mkdir()
    git(harness.workspace / "scratch", "init", "-q", "empty")
    live = harness.call("turn.diff", message_id=mid)
    assert live["to"]["live"] is True and live["to"]["skipped"] == ["scratch/empty/"]
    assert [f["path"] for f in live["files"]] == ["keep.txt"]
    assert assert_lossless(core_probe, tmp_path, "turn.diff", live)["to"]["skipped"] == ["scratch/empty/"]
    notes = run_probe(core_probe, "diff-notes", write_json(tmp_path / "live.json", live))
    assert notes == ["1 nested repository with no commit is not shown: scratch/empty/."]
    whole = harness.call("conversation.diff", conversation_id=cid)
    assert whole["to"]["skipped"] == ["scratch/empty/"]
    assert_lossless(core_probe, tmp_path, "conversation.diff", whole)
    many = {**live, "to": {**live["to"], "skipped": [f"r{n}/" for n in range(7)]}}
    assert run_probe(core_probe, "diff-notes", write_json(tmp_path / "many.json", many)) == [
        "7 nested repositories with no commit are not shown: r0/, r1/, r2/, r3/, r4/ and 2 more."]
    head, tree = turn_diff.snapshot(harness.workspace)
    harness.store.record_trees(attempt_id="j-1/a1", message_id=mid, conversation_id=cid,
                               workspace=str(harness.workspace), writable=True, started_at="2026-09-25T10:00:00Z",
                               head_after=head, end_tree=tree, ended=True)
    ended = harness.call("turn.diff", message_id=mid)
    assert "skipped" not in ended["to"]
    assert run_probe(core_probe, "diff-notes", write_json(tmp_path / "ended.json", ended)) == []


def test_c26_14_the_pane_gives_every_listed_file_its_section(core_probe, tmp_path, harness):
    """Each file the daemon lists has one section under the same path, quoted names
    included; line numbers follow the hunk headers; binary and mode-only files have
    sections with no lines."""
    cid, mid = turn(harness)
    change_everything(harness.workspace)
    result = harness.call("turn.diff", message_id=mid)
    sections = run_probe(core_probe, "diff-parse", write_text(tmp_path / "d.txt", result["diff"]))
    assert sorted(s["path"] for s in sections) == sorted(f["path"] for f in result["files"])
    by_path = {s["path"]: s for s in sections}
    keep = [line for line in by_path["keep.txt"]["lines"] if line[0] != "context"]
    assert keep == [["hunk", "@@ -1,5 +1,6 @@", None, None], ["removed", "two", 2, None], ["added", "TWO", None, 2],
                    ["added", "six", None, 6]]
    assert by_path["logo.bin"]["binary"] is True and by_path["logo.bin"]["lines"] == []
    assert by_path["run.sh"]["lines"] == [] and "new mode 100755" in by_path["run.sh"]["header"]
    assert [line[:2] for line in by_path["gone.txt"]["lines"]] == [["hunk", "@@ -1 +0,0 @@"],
                                                                   ["removed", "to be removed"]]
    assert by_path[QUOTED]["lines"][-1] == ["added", "quoted", None, 1]
    assert by_path["café.txt"]["lines"][-1] == ["added", "accent", None, 1]
    # Every added and removed row the parser counts is one git's numstat counted.
    for listed in result["files"]:
        rows = by_path[listed["path"]]["lines"]
        if listed["additions"] is not None:
            assert sum(r[0] == "added" for r in rows) == listed["additions"], listed["path"]
            assert sum(r[0] == "removed" for r in rows) == listed["deletions"], listed["path"]


def test_the_parser_reads_lines_that_look_like_headers_inside_a_hunk(core_probe, tmp_path):
    """A removed line `-- x` is `--- x` in the diff and an added `++ y` is `+++ y`: inside
    a hunk they are content, never a new file's header; `\\ No newline` is a meta row."""
    text = ("diff --git a/a.txt b/a.txt\n"
            "index 1111111..2222222 100644\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -3,2 +3,2 @@ def f():\n"
            "--- x\n"
            "+++ y\n"
            " kept\n"
            "\\ No newline at end of file\n")
    sections = run_probe(core_probe, "diff-parse", write_text(tmp_path / "d.txt", text))
    assert len(sections) == 1 and sections[0]["path"] == "a.txt"
    assert sections[0]["lines"] == [["hunk", "@@ -3,2 +3,2 @@ def f():", None, None], ["removed", "-- x", 3, None],
                                    ["added", "++ y", None, 3], ["context", "kept", 4, 4],
                                    ["meta", "\\ No newline at end of file", None, None]]


def write_text(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_c26_14_the_pane_says_who_else_wrote_in_the_folder(core_probe, tmp_path, harness):
    """I4 in the app: a turn's diff whose folder another conversation's turn wrote in
    meanwhile is labelled with that conversation's title (and whether it still runs);
    the whole conversation's diff says since when; a diff no other turn met, or from a
    daemon without the field, says nothing of the kind."""
    cid, mid = turn(harness)
    change_everything(harness.workspace)
    head, tree = turn_diff.snapshot(harness.workspace)
    harness.store.record_trees(attempt_id="j-1/a1", message_id=mid, conversation_id=cid,
                               workspace=str(harness.workspace), writable=True, started_at="2026-09-25T10:00:00Z",
                               head_after=head, end_tree=tree, ended=True)
    alone = harness.call("turn.diff", message_id=mid)
    assert alone["shared"] == []
    words = lambda result: run_probe(core_probe, "diff-words", write_json(tmp_path / "r.json", result))  # noqa: E731
    assert words(alone)["shared"] is None
    del alone["shared"]
    assert words(alone)["shared"] is None                       # an older daemon's answer
    for n, title in enumerate(("Scratch", None)):
        other = harness.create(title=title)["conversation_id"]
        other_mid = harness.submit(other, "beside")["message_id"]
        harness.store.record_trees(attempt_id=f"j-{n + 2}/a1", message_id=other_mid, conversation_id=other,
                                   workspace=str(harness.workspace), writable=True,
                                   started_at="2026-09-25T10:00:01Z", head_before=head, start_tree=tree,
                                   ended=n == 1)
    shared = harness.call("turn.diff", message_id=mid)
    assert words(shared)["shared"] == ("This folder was also changed by “Scratch” (still running) and an "
                                       "untitled conversation during this turn; the diff may include their edits.")
    whole = harness.call("conversation.diff", conversation_id=cid)
    assert words(whole)["shared"].endswith("since this conversation's first turn began; the diff may include "
                                           "their edits.")


def test_c26_14_empty_diffs_still_disclose_other_conversations(core_probe, tmp_path, harness):
    """P3: overlapping writes can leave no net changes. The empty pane still names
    the other conversation for a turn and a whole conversation, and keeps notices
    about nested repositories the comparison could not show."""
    cid, mid = turn(harness)
    head, tree = turn_diff.snapshot(harness.workspace)
    beside = harness.create(title="Scratch")["conversation_id"]
    beside_mid = harness.submit(beside, "beside")["message_id"]
    harness.store.record_trees(attempt_id="j-2/a1", message_id=beside_mid, conversation_id=beside,
                               workspace=str(harness.workspace), writable=True, started_at="2026-09-25T10:00:01Z",
                               head_before=head, start_tree=tree)
    harness.store.record_trees(attempt_id="j-1/a1", message_id=mid, conversation_id=cid,
                               workspace=str(harness.workspace), writable=True, started_at="2026-09-25T10:00:00Z",
                               head_after=head, end_tree=tree, ended=True)
    for op, params, when in [("turn.diff", {"message_id": mid}, "during this turn"),
                             ("conversation.diff", {"conversation_id": cid},
                              "since this conversation's first turn began")]:
        result = harness.call(op, **params)
        assert result["available"] and result["files"] == [] and result["diff"] == ""
        assert [entry["conversation_id"] for entry in result["shared"]] == [beside]
        words = run_probe(core_probe, "diff-words", write_json(tmp_path / "empty.json", result))
        assert words["empty"] == ("No changes. This folder was also changed by “Scratch” (still running) "
                                   f"{when}; the diff may include its edits.")
        legacy = {key: value for key, value in result.items() if key != "shared"}
        assert run_probe(core_probe, "diff-words", write_json(tmp_path / "legacy.json", legacy))["empty"] == "No changes."
        skipped = {**result, "to": {**result["to"], "skipped": ["scratch/empty/"]}}
        notice = run_probe(core_probe, "diff-words", write_json(tmp_path / "skipped.json", skipped))["empty"]
        assert notice.startswith("No changes to show. This folder was also changed by “Scratch”")
        assert notice.endswith("1 nested repository with no commit is not shown: scratch/empty/.")
        unavailable = {**result, "available": False, "reason": "snapshot-pruned"}
        words = run_probe(core_probe, "diff-words", write_json(tmp_path / "unavailable.json", unavailable))
        assert words["unavailable"] == ("The repository no longer holds this snapshot, so the changes cannot "
                                            "be shown. This folder was also changed by “Scratch” (still running) "
                                            f"{when}; the diff may include its edits.")


def test_c26_14_the_pane_quotes_a_long_title_cut_to_one_short_line(core_probe, tmp_path):
    """Review of 5e9f2fbd (P3-7): a conversation's title is whatever a person or a native
    session gave it, so the note above a diff quotes at most 60 characters of it, on one
    line, with an ellipsis where it was cut; a title of 60 is quoted whole, and a blank
    one reads as untitled."""
    long = "Refactor the admission path " * 20
    sixty = "x" * 60
    result = {"available": True, "conversation_id": "cv-1", "message_id": "m-1", "files": [],
              "files_truncated": False, "stats": {"files": 0, "additions": 0, "deletions": 0, "complete": True},
              "diff": "", "truncated": False, "scrubbed": 0,
              "shared": [{"conversation_id": f"cv-{n}", "title": title, "message_ids": [f"m-{n}"],
                          "from": "2026-09-29T12:00:00.000Z", "to": "2026-09-29T12:01:00.000Z"}
                         for n, title in enumerate((long, "two\nlines", sixty, " \n "), start=2)]}
    words = run_probe(core_probe, "diff-words", write_json(tmp_path / "r.json", result))["shared"]
    cut = long[:59]
    assert words == (f"This folder was also changed by “{cut}…”, “two lines”, "
                     f"“{sixty}” and an untitled conversation during this turn; the diff may include "
                     "their edits.")
    assert len(words) < 300
