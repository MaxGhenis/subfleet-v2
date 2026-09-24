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
