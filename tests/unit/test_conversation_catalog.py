"""C-30.2, IR-15: one Claude transcript's facts for a conversation row.

`conversation.open` of a native session and the legacy cockpit import (C-30.4)
both create a conversation from `catalog.claude_session`, so the two can never
disagree about a session's workspace, model value, permission or continuability.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from subfleet.conversations import catalog
from subfleet.sessions import transcripts

SESSION = "5e551011-0000-4000-8000-0000000000c1"


def write_transcript(projects: Path, session_id: str, cwd: str, *, mode: str | None = "default",
                     source: str | None = None, title: str | None = None,
                     model: str = "claude-opus-5-5") -> Path:
    directory = projects / cwd.replace("/", "-")
    directory.mkdir(parents=True, exist_ok=True)
    user = {"type": "user", "uuid": "u1", "cwd": cwd, "sessionId": session_id,
            "message": {"role": "user", "content": "fix the importer"}}
    if mode:
        user["permissionMode"] = mode
    if source:
        user["promptSource"] = source
    rows = [user, {"type": "assistant", "uuid": "a1", "cwd": cwd, "sessionId": session_id,
                   "message": {"role": "assistant", "model": model,
                               "content": [{"type": "text", "text": "done"}]}}]
    if title:
        rows.append({"type": "custom-title", "customTitle": title, "sessionId": session_id})
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_a_session_continues_in_its_transcripts_workspace(tmp_path):
    """C-30.2, design D-9: cwd, title, model value and the mapped permission."""
    work = tmp_path / "work"
    work.mkdir()
    path = write_transcript(tmp_path / "projects", SESSION, str(work), mode="bypassPermissions",
                            title="Importer fix")
    facts = catalog.claude_session(path)
    assert facts == {"cwd": str(work), "title": "Importer fix", "model_value": "claude-opus-5-5",
                     "permission": "bypass", "permission_source": "bypassPermissions",
                     "continuable": True, "lane_id": None}


def test_a_lane_run_a_missing_directory_and_tmp_do_not_continue(tmp_path):
    """C-30.2, IR-15: a lane run, a vanished cwd and a /tmp cwd are not continuable."""
    work = tmp_path / "work"
    work.mkdir()
    lane = write_transcript(tmp_path / "a", SESSION, str(work), source="sdk")
    gone = write_transcript(tmp_path / "b", SESSION, str(tmp_path / "gone"))
    assert catalog.claude_session(lane)["continue_blocker"] == "a Subfleet lane run"
    assert catalog.claude_session(gone)["continue_blocker"] == "its working directory no longer exists"
    for path in (lane, gone):
        assert catalog.claude_session(path)["continuable"] is False
    # An existing directory under /private/tmp: the tmp rule, not the missing one, refuses it.
    scratch = tempfile.mkdtemp(prefix="sf-catalog-", dir="/private/tmp")
    try:
        temporary = write_transcript(tmp_path / "c", SESSION, scratch)
        assert catalog.claude_session(temporary)["continue_blocker"] == "tmp-workspace"
    finally:
        os.rmdir(scratch)


def test_a_transcript_is_found_under_a_named_projects_directory(tmp_path, monkeypatch):
    """C-30.4's `--claude-dir`: a projects directory other than `~/.claude/projects`."""
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(tmp_path / "elsewhere"))
    work = tmp_path / "work"
    work.mkdir()
    path = write_transcript(tmp_path / "projects", SESSION, str(work))
    assert transcripts.transcript_path(SESSION) is None
    assert transcripts.transcript_path(SESSION, tmp_path / "projects") == path


def test_a_line_that_is_not_an_object_is_not_an_entry(tmp_path):
    """C-30.2, C-30.4 (review L2): a valid JSON line that is not an object, and an
    entry whose message or cwd has another shape, are skipped; reading such a
    transcript never raises, for `conversation.open` or the legacy import."""
    work = tmp_path / "work"
    work.mkdir()
    path = write_transcript(tmp_path / "projects", SESSION, str(work), title="Importer fix")
    odd = ["[]", '"text"', json.dumps({"type": "user", "message": ["not", "an", "object"], "cwd": 7})]
    path.write_text("\n".join([*odd, *path.read_text().splitlines(), "[1]",
                               json.dumps({"type": "assistant", "message": "x"})]) + "\n", encoding="utf-8")
    facts = catalog.claude_session(path)
    assert (facts["continuable"], facts["cwd"], facts["title"], facts["model_value"]) == (
        True, str(work), "Importer fix", "claude-opus-5-5")
    assert transcripts.headless_transcript(path) is False


def _copy(projects: Path, cwd: Path, rows: list[dict], mtime: float) -> Path:
    """One copy of SESSION's transcript under `cwd`'s project directory, as Claude Code names it."""
    from subfleet.adapters.claude import encode_project_dir
    directory = projects / encode_project_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{SESSION}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _turn(cwd: Path, uuid: str, **user) -> list[dict]:
    return [{"type": "user", "uuid": f"u{uuid}", "cwd": str(cwd), "sessionId": SESSION,
             "message": {"role": "user", "content": f"prompt {uuid}"}, **user},
            {"type": "assistant", "uuid": f"a{uuid}", "cwd": str(cwd), "sessionId": SESSION,
             "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "ok"}]}}]


def test_a_moved_session_continues_where_its_live_copy_is(tmp_path):
    """C-30.2, C-30.4 (review L7): a session moved to another worktree leaves a
    copy under each project directory. The newest copy, the one resumed, starts
    with the old rows; its workspace is the cwd whose project directory holds
    it, with the permission its last rows carry, never the old worktree's."""
    old, new = tmp_path / "work" / "old_tree", tmp_path / "work" / "new.tree"
    for directory in (old, new):
        directory.mkdir(parents=True)
    projects = tmp_path / "projects"
    head = _turn(old, "1", permissionMode="default")
    stale = _copy(projects, old, head, 1_700_000_000)
    live = _copy(projects, new, [*head, *_turn(new, "2", permissionMode="acceptEdits")], 1_800_000_000)
    assert transcripts.transcript_path(SESSION, projects) == live
    facts = catalog.claude_session(live)
    assert (facts["cwd"], facts["permission"]) == (str(new), "accept-edits")
    assert catalog.claude_session(stale)["cwd"] == str(old)


def test_a_session_that_moves_within_its_project_keeps_its_project(tmp_path):
    """C-30.2 (review L7): a session whose later rows name a directory inside its
    project still continues from the project's own directory, where its one
    copy lives."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    path = _copy(tmp_path / "projects", root, [*_turn(root, "1"), *_turn(root / "sub", "2")], 1_800_000_000)
    assert catalog.claude_session(path)["cwd"] == str(root)


def test_a_session_that_moved_and_then_moved_within_its_new_project_keeps_the_new_one(tmp_path):
    """C-30.2 (review L7 follow-up): a session moved to a new worktree and then
    into a directory inside it continues from the new worktree, the latest cwd
    that names its copy's project directory."""
    old, new = tmp_path / "work" / "old", tmp_path / "work" / "new"
    (new / "sub").mkdir(parents=True)
    old.mkdir(parents=True)
    projects = tmp_path / "projects"
    head = _turn(old, "1")
    _copy(projects, old, head, 1_700_000_000)
    live = _copy(projects, new, [*head, *_turn(new, "2"), *_turn(new / "sub", "3")], 1_800_000_000)
    assert catalog.claude_session(live)["cwd"] == str(new)
