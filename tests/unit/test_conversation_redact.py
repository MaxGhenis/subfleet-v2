"""C-25.5: conversation events carry only what a person may see."""

from __future__ import annotations

from subfleet.conversations.redact import (
    HIDDEN, INPUT_MAX, RESULT_MAX, DeltaBuffer, bounded_text, tool_completed, tool_started,
)

TOKEN = "sk-ant-oat01-" + "A" * 60


def test_credential_reading_calls_are_hidden_whole():
    """C-25.5 a call that reads a credential shows neither input nor output."""
    started = tool_started("Bash", {"command": "agent-secret get claude-quota-x"}, tool_id="t1")
    assert started == {"id": "t1", "name": "Bash", "hidden": True, "summary": HIDDEN}
    done = tool_completed("t1", TOKEN, is_error=False, hidden=True)
    assert done["preview"] == "" and done["hidden"]
    assert tool_started("Bash", {"command": "cd x\nenv | sort"}, tool_id="t2")["hidden"]


def test_inputs_and_results_are_scrubbed_and_bounded():
    """C-25.5 summaries at most 500 characters, previews at most 2 KiB, credentials removed."""
    started = tool_started("Bash", {"command": f"curl -H 'Authorization: Bearer {TOKEN}' " + "x" * 900}, tool_id="t")
    assert TOKEN not in started["summary"] and len(started["summary"]) <= INPUT_MAX
    done = tool_completed("t", f"token={TOKEN}\n" + "y" * 5000, is_error=False, hidden=False)
    assert TOKEN not in done["preview"] and len(done["preview"]) <= RESULT_MAX


def test_preferred_fields_name_the_call():
    """C-25.5 a file tool shows its path; a search shows its pattern."""
    assert tool_started("Read", {"file_path": "/repo/a.py", "limit": 20}, tool_id=None)["summary"] == "file_path: /repo/a.py"
    assert "pattern: TODO" in tool_started("Grep", {"pattern": "TODO", "path": "src"}, tool_id=None)["summary"]


def test_binary_output_is_not_kept():
    """C-25.5 binary tool output is replaced by a marker."""
    assert tool_completed("t", "\x00\x01\x02" * 50, is_error=None, hidden=False)["preview"] == "[binary output omitted]"


def test_streamed_text_is_scrubbed_a_line_at_a_time():
    """C-25.5 a credential split across two deltas is still removed."""
    buffer = DeltaBuffer()
    half = len(TOKEN) // 2
    out = buffer.feed("here: " + TOKEN[:half]) + buffer.feed(TOKEN[half:] + " done\nnext")
    out += buffer.flush()
    assert TOKEN not in out and "done" in out and out.endswith("next")


def test_system_reminders_never_reach_events():
    """C-25.5 injected reminders are stripped from displayed text."""
    assert "secret" not in bounded_text("a <system-reminder>secret</system-reminder> b")
