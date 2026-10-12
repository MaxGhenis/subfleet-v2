"""Sidebar triage and the Worktree toggle in the app's core (C-25.2, C-25.6, C-29.9).

The daemon's real service and store (`daemon_harness.ServiceHarness`) make the
conversations, the archive answers and the watch feed; the app's Swift store
(`CoreProbeTriage.swift`) folds them into the sidebar: unread from the feed's
sequence against what the person has seen, pins in their own section, archived
conversations in the Archived view only. The new conversation's draft finds the
git checkout of its folder, remembers the toggle per repository, and builds the
folder check and the create the daemon's own handlers then accept, cutting a real
worktree.

Invariants checked here: a conversation is unread iff a message of it changed after
the newest change the person saw it at and it is not focused; the focused one never
is; Pinned holds the pinned conversations, newest pin first, and no entry is listed
twice; the draft asks for a worktree iff its folder is in a git checkout and the
toggle is on, and a check of one kind never enables Start for the other.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import pytest

from subfleet import protocol
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness
from tests.frontend.test_core_protocol import assert_lossless

pytestmark = needs_swift


@pytest.fixture
def harness():
    root = Path(tempfile.mkdtemp(prefix="sf-app-tr-", dir="/tmp"))
    harness = ServiceHarness(root)
    yield harness
    harness.close()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def git_env(monkeypatch):
    for key, value in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                       "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
                       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}.items():
        monkeypatch.setenv(key, value)


def git(path, *argv) -> str:
    return subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, branch: str = "main") -> Path:
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "file.txt").write_text("base\n")
    git(path, "init", "-b", branch)
    git(path, "add", ".")
    git(path, "commit", "-m", "base")
    return path


def triage(core_probe, tmp_path, steps, now="2026-10-11T18:00:00.000Z") -> dict:
    marks = tmp_path / f"marks-{uuid.uuid4().hex[:6]}.json"
    return run_probe(core_probe, "triage", write_json(tmp_path / f"tr-{uuid.uuid4().hex[:6]}.json",
                                                      {"steps": steps, "now": now}), marks)


def draft(core_probe, tmp_path, steps, **extra) -> dict:
    return run_probe(core_probe, "worktree-draft", write_json(tmp_path / f"wd-{uuid.uuid4().hex[:6]}.json",
                                                              {"steps": steps, **extra}), tmp_path)


def watch_from(harness, after: int) -> list[dict]:
    """The feed as the app drains it: pages until one comes back empty (caught up)."""
    pages = []
    while True:
        page = harness.call("conversation.watch", after=after, wait_s=0)
        pages.append(page)
        if not page["changes"]:
            return pages
        after = page["next"]


def settle(harness, mid: str, state: str = "complete") -> None:
    harness.store.set_state(mid, state)


def section_ids(snapshot) -> list[tuple[str, list[str]]]:
    return [(s["id"], [e["id"] for e in s["entries"]]) for s in snapshot["sections"]]


# --- archive on the wire --------------------------------------------------------


def test_archive_answers_decode_losslessly_and_the_apps_requests_reach_the_daemon(core_probe, tmp_path, harness):
    cid = harness.create(title="Done")["conversation_id"]
    line = run_probe(core_probe, "request", "conversation.archive",
                     write_json(tmp_path / "a.json", {"conversation_id": cid, "archived": True}), "app-1", raw=True)
    request = protocol.decode_request(line.encode())
    assert request.op == "conversation.archive" and json.loads(line)["args"] == {"conversation_id": cid, "archived": True}
    archived = harness.call(request.op, **request.args)
    assert archived["conversation"]["archived_at"]
    assert_lossless(core_probe, tmp_path, "conversation.archive", archived)
    # The Archived view's read: archived conversations listed, each saying so.
    line = run_probe(core_probe, "request", "conversation.list",
                     write_json(tmp_path / "l.json", {"limit": 1000, "include_catalog": True, "include_archived": True}),
                     raw=True)
    listing = harness.call("conversation.list", **protocol.decode_request(line.encode()).args)
    assert [c["conversation_id"] for c in listing["conversations"]] == [cid]
    assert_lossless(core_probe, tmp_path, "conversation.list", listing)
    assert harness.call("conversation.list")["conversations"] == []
    shown = harness.call("conversation.archive", conversation_id=cid, archived=False)
    assert shown["conversation"]["archived_at"] is None
    assert_lossless(core_probe, tmp_path, "conversation.archive", shown)


def test_archive_refused_while_running_decodes_with_its_reason(core_probe, tmp_path, harness):
    cid = harness.create()["conversation_id"]
    harness.submit(cid, "work on it")
    line = harness.response_line("conversation.archive", {"conversation_id": cid}, "app-7")
    decoded = run_probe(core_probe, "response", "conversation.archive", write_json(tmp_path / "line", json.loads(line)),
                        "app-7")
    assert decoded["error"]["code"] == 7 and decoded["error"]["reason"] == "conversation-active"
    assert decoded["error"]["fix"]


# --- unread, from the feed's sequence -------------------------------------------


def test_unread_follows_the_feed_and_what_the_person_has_seen(core_probe, tmp_path, harness):
    a = harness.create(title="Alpha")["conversation_id"]
    b = harness.create(title="Beta")["conversation_id"]
    first_a = harness.submit(a, "hello")["message_id"]
    first_b = harness.submit(b, "hello")["message_id"]
    listing = harness.call("conversation.list", include_catalog=False)
    baseline = watch_from(harness, 0)
    cursor = baseline[-1]["next"]
    # Activity after the app first caught up: B's turn finishes, unfocused.
    settle(harness, first_b)
    later = watch_from(harness, cursor)
    cursor = later[-1]["next"]
    settle(harness, first_a)
    focused_later = watch_from(harness, cursor)
    cursor = focused_later[-1]["next"]
    second_b = harness.submit(b, "again", after=first_b)["message_id"]
    again = watch_from(harness, cursor)
    cursor = again[-1]["next"]
    rename_only = harness.call("conversation.rename", conversation_id=a, title="Alpha renamed")
    renamed = watch_from(harness, cursor)
    assert rename_only and second_b

    out = triage(core_probe, tmp_path, [
        {"list": listing}, *({"watch": page} for page in baseline), {"snapshot": "baseline"},
        *({"watch": page} for page in later), {"snapshot": "b_finished"},
        {"focus": a}, *({"watch": page} for page in focused_later), {"snapshot": "a_focused"},
        {"focus": b}, {"snapshot": "b_opened"},
        {"focus": None}, *({"watch": page} for page in again), {"snapshot": "b_again"},
        *({"watch": page} for page in renamed), {"snapshot": "after_rename"},
        {"reload": True}, {"snapshot": "reloaded"},
        {"mark_read": b}, {"snapshot": "marked"},
    ])
    snaps = out["snapshots"]
    # Before the first catch-up nothing is unread: everything already there counts as read.
    assert snaps["baseline"]["unread"] == [] and snaps["baseline"]["marks"]["floor"] == baseline[-1]["next"]
    assert snaps["b_finished"]["unread"] == [b]
    # The focused conversation's own changes are seen as they come.
    assert snaps["a_focused"]["unread"] == [b] and a not in snaps["a_focused"]["unread"]
    assert snaps["b_opened"]["unread"] == [] and snaps["b_opened"]["focused"] == b
    assert snaps["b_again"]["unread"] == [b]
    # A rename is no message: nothing new to read.
    assert snaps["after_rename"]["unread"] == [b]
    # Saved 0600 and read back as they were: a relaunch keeps what was seen.
    assert {"reloaded_equal": True, "mode": "600"} in out["log"]
    assert snaps["reloaded"]["unread"] == [b]
    assert snaps["marked"]["unread"] == []
    entries = {e["id"]: e for s in snaps["b_again"]["sections"] for e in s["entries"]}
    assert entries["cv:" + b]["unread"] and not entries["cv:" + a]["unread"]


def test_unread_first_lists_unread_conversations_in_a_section_of_their_own(core_probe, tmp_path, harness):
    ids = [harness.create(title=f"C{i}")["conversation_id"] for i in range(3)]
    mids = [harness.submit(cid, "hello")["message_id"] for cid in ids]
    listing = harness.call("conversation.list", include_catalog=False)
    baseline = watch_from(harness, 0)
    settle(harness, mids[1])
    later = watch_from(harness, baseline[-1]["next"])
    out = triage(core_probe, tmp_path, [
        {"list": listing}, *({"watch": p} for p in baseline), *({"watch": p} for p in later),
        {"snapshot": "plain"}, {"unread_first": True}, {"snapshot": "unread_first"},
        {"grouping": "workspace"}, {"snapshot": "by_workspace"}])
    plain, first = out["snapshots"]["plain"], out["snapshots"]["unread_first"]
    assert all(s["id"] != "unread" for s in plain["sections"])
    assert section_ids(first)[0] == ("unread", ["cv:" + ids[1]])
    listed = [e for _, entries in section_ids(first) for e in entries]
    assert sorted(listed) == sorted("cv:" + cid for cid in ids) and len(listed) == len(set(listed))
    assert section_ids(out["snapshots"]["by_workspace"])[0] == ("unread", ["cv:" + ids[1]])


# --- pins ------------------------------------------------------------------------


def test_pins_lead_the_sidebar_newest_pin_first_and_are_listed_once(core_probe, tmp_path, harness):
    ids = [harness.create(title=f"P{i}")["conversation_id"] for i in range(4)]
    mids = [harness.submit(cid, "hello")["message_id"] for cid in ids]
    listing = harness.call("conversation.list", include_catalog=False)
    baseline = watch_from(harness, 0)
    settle(harness, mids[3])
    later = watch_from(harness, baseline[-1]["next"])
    out = triage(core_probe, tmp_path, [
        {"list": listing}, *({"watch": p} for p in baseline), *({"watch": p} for p in later),
        {"pin": ids[0]}, {"pin": ids[2]}, {"snapshot": "two"},
        {"pin": ids[0]}, {"snapshot": "repinned"},
        {"unpin": ids[2]}, {"snapshot": "one"},
        {"pin": ids[3]}, {"unread_first": True}, {"snapshot": "pinned_unread"},
        {"pin": "cv-gone"}, {"snapshot": "missing"},
        {"reload": True}, {"snapshot": "reloaded"},
    ])
    snaps = out["snapshots"]
    assert section_ids(snaps["two"])[0] == ("pinned", ["cv:" + ids[2], "cv:" + ids[0]])
    assert section_ids(snaps["repinned"])[0] == ("pinned", ["cv:" + ids[0], "cv:" + ids[2]])
    assert section_ids(snaps["one"])[0] == ("pinned", ["cv:" + ids[0]])
    # A pinned unread conversation is in Pinned only; the Unread section has the rest.
    assert [sid for sid, _ in section_ids(snaps["pinned_unread"])][:1] == ["pinned"]
    assert "unread" not in [sid for sid, _ in section_ids(snaps["pinned_unread"])]
    assert section_ids(snaps["pinned_unread"])[0] == ("pinned", ["cv:" + ids[3], "cv:" + ids[0]])
    for name in ("two", "one", "pinned_unread", "missing", "reloaded"):
        listed = [e for _, entries in section_ids(snaps[name]) for e in entries]
        assert sorted(listed) == sorted("cv:" + cid for cid in ids) and len(listed) == len(set(listed)), name
    # A pin of a conversation the list does not have shows nothing, and survives a relaunch.
    assert snaps["missing"]["marks"]["pinned"][0] == "cv-gone"
    assert section_ids(snaps["reloaded"]) == section_ids(snaps["missing"])
    assert {e["id"]: e["pinned"] for e in snaps["one"]["sections"][0]["entries"]} == {"cv:" + ids[0]: True}


# --- the Archived view -----------------------------------------------------------


def test_remote_archive_and_unarchive_update_focus_without_losing_pin_or_read_marks(core_probe, tmp_path, harness):
    cid = harness.create(title="Focused")["conversation_id"]
    mid = harness.submit(cid, "keep the timeline")["message_id"]
    listing = harness.call("conversation.list", include_catalog=False)
    baseline = watch_from(harness, 0)
    settle(harness, mid)
    completed = watch_from(harness, baseline[-1]["next"])
    archived = harness.call("conversation.archive", conversation_id=cid)["conversation"]
    archive_pages = watch_from(harness, completed[-1]["next"])
    filtered = harness.call("conversation.list", include_catalog=False)
    archived_listing = harness.call("conversation.list", include_catalog=False, include_archived=True)
    assert filtered["conversations"] == []
    change = archive_pages[0]["changes"][0]
    assert change["archived"] is True and change["archived_at"] == archived["archived_at"]
    assert_lossless(core_probe, tmp_path, "conversation.watch", archive_pages[0])

    harness.call("conversation.archive", conversation_id=cid, archived=False)
    unarchive_pages = watch_from(harness, archive_pages[-1]["next"])
    refreshed = harness.call("conversation.list", include_catalog=False)
    assert unarchive_pages[0]["changes"][0]["archived"] is False
    assert unarchive_pages[0]["changes"][0]["archived_at"] is None
    # An older feed lacks both fields: it must not clear a known archive.
    old_change = {k: v for k, v in change.items() if k not in ("archived", "archived_at")}
    old_page = {"changes": [old_change], "next": old_change["seq"]}
    out = triage(core_probe, tmp_path, [
        {"list": listing}, *({"watch": p} for p in baseline), {"pin": cid}, {"focus": cid},
        *({"watch": p} for p in completed), {"snapshot": "read"},
        *({"watch": p} for p in archive_pages), {"snapshot": "watch_archived"},
        {"list": filtered}, {"snapshot": "list_archived"},
        *({"watch": p} for p in unarchive_pages),
        {"snapshot": "watch_unarchived"}, {"list": refreshed}, {"snapshot": "list_unarchived"}])
    snaps = out["snapshots"]
    read_marks = snaps["read"]["marks"]
    for name in ("watch_archived", "list_archived"):
        snap = snaps[name]
        assert snap["sections"] == [] and snap["focused"] == cid
        assert snap["focused_conversation"]["archived_at"] == archived["archived_at"]
        assert snap["marks"] == read_marks and snap["unread"] == []
    older = triage(core_probe, tmp_path, [
        {"list": archived_listing}, {"focus": cid}, {"watch": old_page}, {"snapshot": "older"}])
    assert older["snapshots"]["older"]["focused_conversation"]["archived_at"] == archived["archived_at"]
    assert older["snapshots"]["older"]["sections"] == []
    for name in ("watch_unarchived", "list_unarchived"):
        snap = snaps[name]
        assert section_ids(snap) == [("pinned", ["cv:" + cid])]
        assert snap["focused"] == cid and not snap["focused_conversation"].get("archived_at")
        assert snap["marks"] == read_marks and snap["unread"] == []


def test_the_archived_view_lists_archived_conversations_only(core_probe, tmp_path, harness):
    kept = harness.create(title="Kept")["conversation_id"]
    gone = harness.create(title="Gone")["conversation_id"]
    harness.call("conversation.archive", conversation_id=gone)
    (harness.root / "catalog.json").write_text(json.dumps({"generated_at": "2026-10-11T12:00:00.000Z", "complete": True,
        "items": [{"provider": "claude", "native_session_id": str(uuid.uuid4()), "cwd": str(harness.workspace),
                   "mtime": 1790000000.0, "continuable": True}]}))
    everything = harness.call("conversation.list", include_archived=True, limit=1000)
    active = harness.call("conversation.list")
    out = triage(core_probe, tmp_path, [
        {"list": everything}, {"pin": gone}, {"show_archived": True}, {"snapshot": "archived"},
        {"show_archived": False}, {"snapshot": "active"},
        {"focus": gone}, {"list": active}, {"snapshot": "focused_archived"}])
    snaps = out["snapshots"]
    archived = [e for s in snaps["archived"]["sections"] for e in s["entries"]]
    assert [(e["id"], e["archived"]) for e in archived] == [("cv:" + gone, True)]
    assert all(s["id"] not in ("pinned", "unread") for s in snaps["archived"]["sections"])
    active_ids = [e["id"] for s in snaps["active"]["sections"] for e in s["entries"]]
    assert "cv:" + kept in active_ids and "cv:" + gone not in active_ids
    assert any(i.startswith("native:") for i in active_ids)
    # The open conversation stays while open, though a list without archived ones left it out.
    assert gone in snaps["focused_archived"]["conversations"]


# --- the Worktree toggle ---------------------------------------------------------


def test_the_toggle_finds_a_checkout_its_linked_worktrees_and_nothing_else(core_probe, tmp_path, git_env):
    repo = make_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "side", str(linked))
    plain = tmp_path / "plain"
    plain.mkdir()
    fake = tmp_path / "fake"
    (fake / ".git").mkdir(parents=True)                 # no HEAD: not a git directory
    found = run_probe(core_probe, "git-checkout", repo, repo / "pkg", linked, linked / "pkg", plain, fake)
    key = os.path.realpath(repo / ".git")
    assert found[0] == {"top": str(repo), "key": key} and found[1] == {"top": str(repo), "key": key}
    # A linked worktree is the same repository: one remembered choice covers both.
    assert found[2] == {"top": str(linked), "key": key} and found[3]["key"] == key
    assert found[4] is None and found[5] is None


def test_the_draft_asks_for_a_worktree_only_of_a_checkout_with_the_toggle_on(core_probe, tmp_path, git_env, harness):
    repo = make_repo(tmp_path / "repo")
    plain = tmp_path / "plain"
    plain.mkdir()
    suite = f"org.maxghenis.subfleet.probe.{uuid.uuid4().hex}"
    out = draft(core_probe, tmp_path, [
        {"workspace": str(plain)}, {"detect": True}, {"check": {"ok": True}}, {"snapshot": "plain"},
        {"workspace": str(repo)}, {"detect": True}, {"check": {"ok": True}}, {"snapshot": "repo_default"},
        {"pick": True}, {"snapshot": "picked_unchecked"},
        {"check": {"ok": True, "kind": "in-place"}}, {"snapshot": "checked_wrong_kind"},
        {"check": {"ok": True}}, {"snapshot": "picked_checked"},
        {"requests": True},
        {"workspace": str(repo / "pkg")}, {"detect": True}, {"snapshot": "subfolder"},
        {"workspace": str(plain)}, {"detect": True}, {"snapshot": "back_to_plain"},
        {"workspace": None}, {"detect": True}, {"snapshot": "scratch"},
    ], settings=harness.settings(), defaults_suite=suite)
    snaps = out["snapshots"]
    assert snaps["plain"]["shows_toggle"] is False and snaps["plain"]["kind"] == "in-place"
    assert snaps["plain"]["can_start"] is True
    assert snaps["repo_default"]["shows_toggle"] is True and snaps["repo_default"]["worktree"] is False
    assert snaps["repo_default"]["kind"] == "in-place" and snaps["repo_default"]["can_start"] is True
    # Flipping it asks for a new check; an answer for the other kind never enables Start.
    assert snaps["picked_unchecked"]["kind"] == "worktree" and snaps["picked_unchecked"]["can_start"] is False
    assert snaps["checked_wrong_kind"]["can_start"] is False
    assert snaps["picked_checked"]["can_start"] is True and snaps["picked_checked"]["checked_kind"] == "worktree"
    # A subfolder of the same repository keeps the pick; a folder in none hides the toggle.
    assert snaps["subfolder"]["kind"] == "worktree"
    assert snaps["back_to_plain"]["shows_toggle"] is False and snaps["back_to_plain"]["kind"] == "in-place"
    assert snaps["scratch"]["shows_toggle"] is False and snaps["scratch"]["kind"] == "in-place"

    requests = out["log"][0]
    assert requests["check"]["op"] == "workspace.check" and requests["check"]["args"]["workspace_kind"] == "worktree"
    assert requests["journaled"]["workspace_kind"] == "worktree"
    sent = requests["sent"]
    assert sent["op"] == "conversation.create" and sent["args"]["workspace_kind"] == "worktree"
    assert sent["args"] == requests["journaled"]

    # What the app sends, the daemon's own handlers take: the check passes for a
    # worktree of a checkout on main, where the folder itself is refused, and the
    # create cuts a worktree on its own branch.
    check = protocol.decode_request((json.dumps(requests["check"]) + "\n").encode())
    assert harness.call("workspace.check", **check.args)["ok"] is True
    assert harness.call("workspace.check", **{**check.args, "workspace_kind": "in-place"})["ok"] is False
    created = harness.call("conversation.create", **sent["args"])["conversation"]
    assert created["workspace_kind"] == "worktree"
    assert created["worktree"]["branch"] == f"subfleet/{created['conversation_id']}"
    assert git(created["worktree"]["path"], "symbolic-ref", "--short", "HEAD") == created["worktree"]["branch"]


def test_the_toggle_is_remembered_per_repository_and_kept_in_the_draft(core_probe, tmp_path, git_env):
    repo = make_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "side", str(linked))
    other = make_repo(tmp_path / "other")
    suite = f"org.maxghenis.subfleet.probe.{uuid.uuid4().hex}"
    out = draft(core_probe, tmp_path, [
        {"workspace": str(repo)}, {"detect": True}, {"pick": True}, {"check": {"ok": True}}, {"start": True},
        {"memory": True},
        # The draft journals the pick: a relaunch keeps it and looks at the folder again.
        {"relaunch": True}, {"snapshot": "relaunched"}, {"detect": True}, {"snapshot": "relaunched_detected"},
        # Another repository starts from its own choice (none: off); this one's linked
        # worktree from this one's (on).
        {"workspace": str(other)}, {"detect": True}, {"snapshot": "other"},
        {"workspace": str(linked)}, {"detect": True}, {"snapshot": "linked"},
        {"pick": False}, {"check": {"ok": True}}, {"start": True}, {"memory": True},
        {"workspace": str(repo)}, {"detect": True}, {"snapshot": "after_off"},
        # A draft saved before the toggle existed still opens, as in-place.
        {"decode_old": {"text": "old words", "attachments": [], "workspace": str(repo), "provider": "claude",
                        "settings": {"model": "opus", "fast": False, "permission": "ask", "auto_continue": True},
                        "confirmWiden": False, "isPresented": True, "focusRevision": 3, "isSubmitting": False}},
        {"snapshot": "old"}, {"detect": True}, {"snapshot": "old_detected"},
    ], defaults_suite=suite)
    snaps, log = out["snapshots"], out["log"]
    key = os.path.realpath(repo / ".git")
    assert log[0] == {"memory": {key: True}}
    assert snaps["relaunched"]["worktree"] is True and snaps["relaunched"]["shows_toggle"] is False
    assert snaps["relaunched"]["kind"] == "in-place" and snaps["relaunched"]["can_start"] is False
    assert snaps["relaunched_detected"]["kind"] == "worktree"
    assert snaps["other"]["worktree"] is False and snaps["other"]["kind"] == "in-place"
    assert snaps["linked"]["worktree"] is True and snaps["linked"]["kind"] == "worktree"
    assert log[1] == {"memory": {key: False}}
    assert snaps["after_off"]["worktree"] is False and snaps["after_off"]["kind"] == "in-place"
    decoded = log[2]["decoded_old"]
    assert decoded["worktree"] is None and decoded["kind"] == "in-place"
    # The repository's remembered choice (now off) applies once its folder is looked at.
    assert snaps["old_detected"]["shows_toggle"] is True and snaps["old_detected"]["worktree"] is False
