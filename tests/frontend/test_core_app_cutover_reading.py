"""The 2026-10-03 app cutover: native recency, stopped turns, task notices."""

from datetime import datetime, timezone

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json

pytestmark = needs_swift
NOW = datetime(2026, 10, 3, 13, tzinfo=timezone.utc).timestamp()


def conversation(cid, updated, **fields):
    return {
        "conversation_id": cid, "provider": "claude", "native_session_id": "native-" + cid,
        "title": cid, "workspace": "/tmp/project", "workspace_kind": "in-place", "allow_main": False,
        "settings": {"model": "claude-opus-5-5", "permission": "ask", "fast": False, "auto_continue": True},
        "origin": "person", "created_at": "2026-09-24T12:00:00Z", "updated_at": updated,
        "pending_approvals": 0, "active": False, **fields,
    }


def store(core_probe, tmp_path, steps):
    return run_probe(core_probe, "store", write_json(tmp_path / "store.json", {"steps": steps, "now": NOW}))


def test_native_activity_moves_an_old_conversation_into_today(core_probe, tmp_path):
    """A session used in Claude today outranks a newer conversation row."""
    native = conversation("Subfleet desktop transition", "2026-09-28T10:00:00Z",
                          last_activity="2026-10-03T12:45:00Z")
    recent = conversation("recent", "2026-10-03T11:00:00Z")
    old = conversation("old", "2026-09-28T12:00:00Z")
    listed = {"conversations": [recent, old, native]}
    out = store(core_probe, tmp_path, [{"list": listed}])
    assert out["sidebar"][0]["title"] == "Today"
    assert [entry["title"] for entry in out["sidebar"][0]["entries"]] == ["Subfleet desktop transition", "recent"]
    assert [row["id"] for row in out["conversations"]] == ["Subfleet desktop transition", "recent", "old"]


def test_open_keeps_catalog_activity_until_next_list_and_uses_newer_row_activity(core_probe, tmp_path):
    """An open reply from an older daemon must not hide the native session again."""
    native = conversation("native", "2026-09-28T10:00:00Z", last_activity="2026-10-03T12:45:00Z")
    recent = conversation("recent", "2026-10-03T11:00:00Z")
    opened = {"conversation": {k: v for k, v in native.items() if k != "last_activity"},
              "messages": [], "events_cursor": 0, "pending_approvals": []}
    out = store(core_probe, tmp_path, [{"list": {"conversations": [recent, native]}}, {"open": opened}])
    assert [entry["title"] for entry in out["sidebar"][0]["entries"]] == ["native", "recent"]
    # A native timestamp can never mask a more recent Subfleet update.
    native["updated_at"] = "2026-10-03T12:55:00Z"
    native["last_activity"] = "2026-09-28T10:00:00Z"
    out = store(core_probe, tmp_path, [{"list": {"conversations": [recent, native]}}])
    assert [entry["title"] for entry in out["sidebar"][0]["entries"]] == ["native", "recent"]


@pytest.mark.parametrize("reason", ["unfinished-turn", "delivery-unknown"])
def test_blocked_rows_need_you_and_open_with_the_correct_choices(core_probe, tmp_path, reason):
    """Both blockers show on the sidebar and offer their actual recovery actions."""
    blocked = conversation("blocked", "2026-10-03T12:45:00Z", blocked_by=reason)
    if reason == "delivery-unknown":
        blocked["last_message"] = {"message_id": "uncertain", "state": reason}
    out = store(core_probe, tmp_path, [{"list": {"conversations": [blocked]}},
                                     {"open": {"conversation": blocked, "messages": [], "events_cursor": 0, "pending_approvals": []}}])
    assert out["sidebar"][0]["entries"][0].get("needs_you") == "Needs you"
    actions = [choice["action"] for choice in out["banners"]["blocked"]["choices"]]
    expected = ([{"unblock": "continue"}, {"unblock": "leave"}] if reason == "unfinished-turn" else
                [{"resolve": "delivered", "message_id": "uncertain"},
                 {"resolve": "not-delivered", "message_id": "uncertain"}])
    assert actions == expected


def notice(summary="Background command &quot;pytest&quot; completed (exit code 0)", status="completed", extra=""):
    return ("<task-notification>\n<task-id>123</task-id>\n<output-file>/tmp/task.output</output-file>\n"
            f"<status>{status}</status>\n<summary>{summary}</summary>\n{extra}</task-notification>")


def history(core_probe, tmp_path, text, *, role="user", provider="claude"):
    return run_probe(core_probe, "fold", write_json(tmp_path / "history.json", {
        "conversation_id": "native", "provider": provider,
        "steps": [{"history": {"items": [{"role": role, "kind": "text", "text": text, "cursor": 1}],
                                 "next_before": None}}],
    }))["items"][0]



@pytest.mark.parametrize("provider,expected", [("codex", "history"), ("claude", "task_notification")])
def test_the_opened_history_page_is_read_with_the_conversations_provider(core_probe, tmp_path, provider, expected):
    """`conversation.open`'s newest page (#127) folds like a `conversation.history`
    page (#124): only a Claude conversation turns a user task block into a notice.
    Later loads fetch only older pages, so a wrong fold here stays on screen."""
    row = conversation("native", "2026-10-03T12:00:00Z", provider=provider)
    page = {"items": [{"role": "user", "kind": "text", "text": notice(), "cursor": 1}], "next_before": None}
    opened = {"conversation": row, "messages": [], "events_cursor": 0, "pending_approvals": [], "history": page}
    out = store(core_probe, tmp_path, [{"list": {"conversations": [row]}}, {"open": opened}])
    [shown] = out["timeline"]["native"]
    assert shown["type"] == expected
    assert shown["type"] == history(core_probe, tmp_path, notice(), provider=provider)["type"]

def test_claude_task_completion_is_a_system_notice_with_summary_status_and_exit(core_probe, tmp_path):
    shown = history(core_probe, tmp_path, "  " + notice() + "\n")
    assert shown["type"] == "task_notification"
    assert shown["summary"] == 'Background command "pytest" completed (exit code 0)'
    assert shown["status"] == "completed" and shown["exit_code"] == 0
    assert shown["detail"] == "Completed · Exit code 0"
    assert "role" not in shown and "<task-notification>" not in str(shown)
    assert "/tmp/task.output" not in str(shown)


@pytest.mark.parametrize("code_tag", ["exit-code", "exit_code"])
def test_task_notice_reads_explicit_exit_and_unescaped_command_text(core_probe, tmp_path, code_tag):
    shown = history(core_probe, tmp_path, notice("pytest && echo done", "failed", f"<{code_tag}>2</{code_tag}>"))
    assert shown["type"] == "task_notification"
    assert (shown["summary"], shown["status"], shown["exit_code"]) == ("pytest && echo done", "failed", 2)


@pytest.mark.parametrize("text,role,provider", [
    ("Please explain this: " + notice(), "user", "claude"),
    (notice() + "\nThis is my question", "user", "claude"),
    (notice() + notice(), "user", "claude"),
    ("<task-notification><summary>truncated</summary>", "user", "claude"),
    (notice(), "assistant", "claude"),
    (notice(), "user", "codex"),
])
def test_only_a_complete_claude_user_task_block_changes_its_presentation(core_probe, tmp_path, text, role, provider):
    shown = history(core_probe, tmp_path, text, role=role, provider=provider)
    assert shown["type"] == "history" and shown["text"] == text and shown["role"] == role
