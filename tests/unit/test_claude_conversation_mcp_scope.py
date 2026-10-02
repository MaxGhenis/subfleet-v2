"""C-12.9, C-26.5: d714 excludes Subfleet.app conversation turns."""

from __future__ import annotations

import os

import pytest

from subfleet.adapters.claude import ClaudeAdapter
from subfleet.conversations.launch import claude_launch
from tests.conftest import make_lane


SESSION = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"
PERMISSIONS = {
    "ask": "default",
    "accept-edits": "acceptEdits",
    "bypass": "bypassPermissions",
}


@pytest.mark.parametrize("permission", [*PERMISSIONS, "read-only"])
@pytest.mark.parametrize("resumed", [False, True], ids=["new-session", "resumed-session"])
def test_d714_keeps_every_conversation_permission_mode_argv_unchanged(tmp_path, monkeypatch, permission, resumed):
    """A real turn launch retains its exact pre-d714 flags, including writable
    bypass turns and the existing strict empty MCP config of read-only turns."""
    for name in tuple(os.environ):
        if name.startswith("CLAUDE_COWORK_MEMORY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CLAUDE_COWORK_MEMORY_D714", "test-memory")
    workdir = tmp_path / "work"
    workdir.mkdir()
    turn = {
        "provider": "claude", "conversation_id": "conversation-1", "message_id": "message-1",
        "text": "continue the conversation", "cwd": str(workdir),
        "settings": {"model": "opus", "effort": "high", "permission": permission, "fast": False},
        "native_session_id" if resumed else "new_session_id": SESSION,
    }
    launch = claude_launch(
        turn, attempt_id="turn-job/a1", attempt_dir=tmp_path / "attempt", lane=make_lane(),
        credential_env={"CLAUDE_CODE_OAUTH_TOKEN": "REDACTED"}, model_id="claude-opus-5-5",
        adapter=ClaudeAdapter(projects_dir=tmp_path / "projects"),
    )
    expected = [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--include-partial-messages", "--replay-user-messages",
        "--thinking-display", "summarized", "--model", "opus", "--effort", "high",
        "--resume" if resumed else "--session-id", SESSION,
    ]
    if permission == "read-only":
        tools = "Read,Glob,Grep,WebSearch,WebFetch"
        expected += [
            "--permission-mode", "plan", "--tools", tools, "--allowedTools", tools,
            "--setting-sources", "", "--safe-mode", "--no-chrome", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
        ]
    else:
        expected += [
            "--permission-mode", PERMISSIONS[permission], "--permission-prompt-tool", "stdio",
            "--disallowedTools", "Monitor,CronCreate,ScheduleWakeup,RemoteTrigger,EnterPlanMode,ExitPlanMode",
            "--settings", '{"disableAllHooks":false}',
        ]
    assert launch.argv == tuple(expected)
    assert launch.env_add == {
        "CLAUDE_CODE_OAUTH_TOKEN": "REDACTED", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "120000",
    }
    expected_remove = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    if permission == "read-only":
        expected_remove += (
            "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", "CLAUDE_MEMORY_STORES",
            "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CLAUDE_COWORK_MEMORY_D714",
        )
    assert launch.env_remove == expected_remove
    assert launch.stdin_path is None and launch.native_session_id == SESSION
    assert launch.notes["turn"] is True
