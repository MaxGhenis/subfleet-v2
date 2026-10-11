"""Review r1b of PR 181, the app's two P2s kept as regressions (C-29.14, C-25.7).

- `Suggestions.swift:179`: a feed creation followed by an older, empty
  `suggestion.list` answer removed the new suggestion while the watch cursor kept
  its advance. Now an answer carries the feed sequence it was read at
  (`watch_seq`), and the app neither drops nor rewinds a suggestion the feed
  delivered after that.
- `Suggestions.swift:169`: a card for a folder that is no git checkout promised
  "its own worktree" although Start ran in place. Now the daemon decides where a
  suggestion starts when it is made (`workspace_kind`, `repository`), Start does
  that, and the card renders only that.

The daemon's real service and store (`daemon_harness.ServiceHarness`) produce every
answer and feed page; the app's compiled Swift core folds them.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness

pytestmark = needs_swift

GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "T",
           "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}


@pytest.fixture
def harness():
    root = Path(tempfile.mkdtemp(prefix="sf-app-sg-r1b-", dir="/tmp"))
    harness = ServiceHarness(root)
    yield harness
    harness.close()
    shutil.rmtree(root, ignore_errors=True)


def suggest(harness, title: str, **extra) -> dict:
    args = {"request_id": str(uuid.uuid4()), "title": title, "tldr": "Out of scope here", "brief": "Do the thing.",
            "workspace": str(harness.workspace), **extra}
    return harness.call("suggestion.create", **args)["suggestion"]


def fold(core_probe, tmp_path, steps):
    return run_probe(core_probe, "suggestions", write_json(tmp_path / f"sg-{uuid.uuid4().hex[:6]}.json",
                                                           {"steps": steps}))


def checkout(path: Path) -> Path:
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "file.txt").write_text("base\n")
    env = {**os.environ, **GIT_ENV}
    for argv in (["init", "-q", "-b", "feature/x"], ["add", "."], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True, env=env)
    return path


# --- P2: an older list never drops or rewinds what the feed delivered ----------------


def test_r1b_a_list_read_before_a_feed_creation_never_removes_it(core_probe, harness, tmp_path):
    """The review's sequence: creation at watch sequence S, then an empty list read
    before it. Before: no card, cursor S. Now the card stays."""
    stale = harness.call("suggestion.list")                  # read before the creation
    assert stale["suggestions"] == [] and isinstance(stale["watch_seq"], int)
    row = suggest(harness, "Made after the read")
    page = harness.call("conversation.watch", after=0)
    assert page["next"] > stale["watch_seq"]
    out = fold(core_probe, tmp_path, [{"watch": page}, {"suggestions": stale}])
    assert [card["id"] for card in out["open"]] == [row["suggestion_id"]] and out["count"] == 1
    assert out["watch_cursor"] == page["next"]
    # A list read after it is as new as the feed: it agrees, and keeps it.
    fresh = harness.call("suggestion.list")
    assert fresh["watch_seq"] >= page["next"]
    again = fold(core_probe, tmp_path, [{"watch": page}, {"suggestions": stale}, {"suggestions": fresh}])
    assert [card["id"] for card in again["open"]] == [row["suggestion_id"]]


def test_r1b_a_list_as_new_as_the_feed_still_drops_one_that_closed_and_aged_out(core_probe, harness, tmp_path):
    row = suggest(harness, "Dismissed, then aged out")
    page = harness.call("conversation.watch", after=0)
    opened = fold(core_probe, tmp_path, [{"watch": page}])
    assert [card["id"] for card in opened["open"]] == [row["suggestion_id"]]
    harness.call("suggestion.dismiss", suggestion_id=row["suggestion_id"])
    aged = harness.call("suggestion.list", closed_limit=0)    # read after the dismissal: it is gone
    out = fold(core_probe, tmp_path, [{"watch": page}, {"suggestions": aged}])
    assert out["open"] == [] and row["suggestion_id"] not in out["known"]


def test_r1b_an_older_answer_never_rewinds_a_change_within_a_state(core_probe, harness, tmp_path):
    """`starting`, then why it has not finished: a list read between the two keeps
    the rank but would lose the reason. The feed's later row wins."""
    row = suggest(harness, "Cut short")
    sid = row["suggestion_id"]
    harness.service.suggestions.claim(sid, by="frontend harness",
                                      conversation=harness.service._start_fields(harness.service.suggestions.get(sid),
                                                                                 {}))
    between = harness.call("suggestion.list")
    named = harness.call("suggestion.list", suggestion_ids=[sid])
    harness.service.suggestions.note_error(sid, "worktree-failed: git worktree add failed")
    page = harness.call("conversation.watch", after=0)
    out = fold(core_probe, tmp_path, [{"watch": page}, {"suggestions": between},
                                      {"rows": named["suggestions"], "read_at": named["watch_seq"]}])
    card = out["open"][0]
    assert card["state"] == "starting" and card["start_error"] == "worktree-failed: git worktree add failed"
    assert out["suggestion_seqs"][sid] == page["next"]


# --- P2: the card says exactly what Start will do -------------------------------------


def test_r1b_the_card_says_where_start_will_run_and_start_does_that(core_probe, harness, tmp_path):
    plain = harness.root / "not-a-checkout"
    plain.mkdir()
    repo = checkout(harness.root / "repo")
    rows = {
        "asked, no checkout": suggest(harness, "Asked for a worktree", workspace=str(plain), worktree=True),
        "checkout": suggest(harness, "A worktree", workspace=str(repo), worktree=True),
        "checkout subfolder": suggest(harness, "A worktree from a subfolder", workspace=str(repo / "pkg"),
                                      worktree=True),
        "in place": suggest(harness, "In place", workspace=str(plain)),
        "no folder": suggest(harness, "Scratch", workspace=None),
    }
    out = fold(core_probe, tmp_path, [{"upsert": row} for row in rows.values()])
    cards = {card["id"]: card for card in out["open"]}
    words = {name: cards[row["suggestion_id"]]["where"] for name, row in rows.items()}
    icons = {name: cards[row["suggestion_id"]]["worktree_icon"] for name, row in rows.items()}
    real_plain, real_repo = str(plain.resolve()), str(repo.resolve())
    assert words["asked, no checkout"] == f"{real_plain} · in place: not a git checkout, so no worktree · Claude"
    assert words["checkout"] == f"{real_repo} · a new worktree on its own branch · Claude"
    assert words["checkout subfolder"] == f"{real_repo}/pkg · a new worktree of {real_repo} on its own branch · Claude"
    assert words["in place"] == f"{real_plain} · in place · Claude"
    assert words["no folder"] == "A new scratch folder · Claude"
    assert not any("its own worktree" in text for text in words.values())
    assert icons == {"asked, no checkout": False, "checkout": True, "checkout subfolder": True, "in place": False,
                     "no folder": False}
    # Start does what each card said.
    kinds = {name: harness.call("suggestion.start", suggestion_id=row["suggestion_id"])["conversation"]
             for name, row in rows.items()}
    assert kinds["asked, no checkout"]["workspace_kind"] == "in-place"
    assert kinds["asked, no checkout"]["workspace"] == real_plain
    assert kinds["checkout"]["workspace_kind"] == "worktree" and kinds["checkout"]["worktree"]["repository"] == real_repo
    assert kinds["checkout subfolder"]["workspace_kind"] == "worktree"
    assert kinds["in place"]["workspace_kind"] == "in-place" and kinds["in place"]["workspace"] == real_plain
    assert kinds["no folder"]["workspace_kind"] == "in-place"
