"""Claude Code's live-session registry, staged for a test (C-10.3).

The desktop login's lane is refused only while Claude Code uses that login, read
from `~/.claude/sessions/<pid>.json` (2026-09-27). Tests written before then
assumed it always was; `claude_code_active` stages that world, one busy Claude
app session, this process, in a registry of the test's own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def claude_code_active(monkeypatch, directory: Path) -> Path:
    """Point `SUBFLEET_CLAUDE_DIR` at `directory`, with one busy `claude-desktop` row."""
    sessions = directory / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "00000000-0000-4000-8000-00000000c0de",
        "entrypoint": "claude-desktop", "status": "busy", "statusUpdatedAt": 0}))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(directory))
    return directory
