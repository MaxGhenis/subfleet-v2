"""`subfleet suggest` (C-25.7, C-17.3, C-17.4): what the CLI sends and what it prints."""

from __future__ import annotations

import io
import json
import uuid
from unittest.mock import Mock

import pytest

from subfleet import cli, compat
from subfleet.client import DaemonError, DaemonUnavailable
from subfleet.contracts import Exit

VIEW = {"suggestion_id": "sg-1", "state": "open", "title": "Port the importer", "tldr": "worth its own session",
        "brief": "Do it.", "brief_truncated": False, "brief_chars": 6, "origin_conversation_id": "cv-1",
        "origin_message_id": None, "creator_session": None, "workspace": "/w", "worktree": True,
        "provider": "claude", "model": None, "decided_by": None, "decided_at": None, "start_error": None,
        "started_conversation_id": None, "started_message_id": None, "created_at": "t", "updated_at": "t"}


@pytest.fixture
def call(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-session")
    monkeypatch.setenv("SUBFLEET_TURN_JOB", "turn-job")
    mock = Mock(return_value={"suggestion": VIEW, "created": True, "scrubbed": 0})
    monkeypatch.setattr(cli.Client, "call", mock)
    return mock


def run(*argv: str) -> int:
    args = cli.build_parser().parse_args(["suggest", *argv])
    return args.handler(args)


def test_c25_7_suggest_sends_the_brief_the_caller_and_the_folder(call, tmp_path, capsys):
    brief = tmp_path / "brief.md"
    brief.write_text("Port the importer.\nRun its tests.\n")
    request = str(uuid.uuid4())
    assert run("--title", "Port the importer", "--tldr", "worth its own session", "--brief-file", str(brief),
               "--workspace", str(tmp_path), "--worktree", "--provider", "codex", "--model", "astra",
               "--request-id", request.upper()) == 0
    op, payload = call.call_args.args
    assert op == "suggestion.create" and call.call_args.kwargs["timeout"] == cli.SUGGEST_TIMEOUT_S
    assert payload == {"request_id": request, "title": "Port the importer", "tldr": "worth its own session",
                       "brief": "Port the importer.\nRun its tests.\n", "workspace": str(tmp_path.resolve()),
                       "worktree": True, "provider": "codex", "model": "astra", "session_id": "the-session",
                       "calling_job": "turn-job"}
    captured = capsys.readouterr()
    assert captured.out == "sg-1\n"
    assert "in this conversation and under Suggested" in captured.err and "--withdraw sg-1" in captured.err


def test_c25_7_the_folder_defaults_to_here_and_the_brief_may_come_from_stdin(call, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert run("--title", "T", "--tldr", "S", "--brief", "-") == 0
    payload = call.call_args.args[1]
    assert payload["brief"] == "from stdin" and payload["workspace"] == str(tmp_path.resolve())
    assert payload["worktree"] is False and payload["provider"] is None and payload["model"] is None
    uuid.UUID(payload["request_id"])
    assert run("--title", "T", "--tldr", "S", "--brief", "-", "--no-workspace") == 0
    assert call.call_args.args[1]["workspace"] is None


def test_c25_7_outside_a_turn_only_the_session_is_sent(call, monkeypatch, tmp_path):
    monkeypatch.delenv("SUBFLEET_TURN_JOB")
    brief = tmp_path / "b.md"
    brief.write_text("x")
    run("--title", "T", "--tldr", "S", "--brief-file", str(brief))
    payload = call.call_args.args[1]
    assert payload["calling_job"] is None and payload["session_id"] == "the-session"


def test_c17_3_what_is_missing_is_exit_2_before_any_request(call, tmp_path, capsys):
    assert run("--title", "T") == Exit.INVALID_INPUT
    assert "--tldr" in capsys.readouterr().err
    assert run("--title", "T", "--tldr", "S", "--brief", "notes") == Exit.INVALID_INPUT
    assert run("--title", "T", "--tldr", "S", "--brief-file", str(tmp_path / "missing.md")) == Exit.INVALID_INPUT
    assert call.call_count == 0
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["suggest", "--request-id", "not-a-uuid"])


def test_c25_7_withdraw_list_start_and_dismiss(call, capsys):
    assert run("--withdraw", "sg-1") == 0
    assert call.call_args.args == ("suggestion.withdraw", {"suggestion_id": "sg-1", "session_id": "the-session",
                                                           "calling_job": "turn-job"})
    call.return_value = {"suggestion": {**VIEW, "state": "started", "started_conversation_id": "cv-2"},
                         "conversation": {}, "message": None, "created": True}
    assert run("--start", "sg-1", "--permission", "bypass") == 0
    assert call.call_args.args == ("suggestion.start", {"suggestion_id": "sg-1", "permission": "bypass",
                                                        "confirm_widen": True})
    assert "Started conversation cv-2" in capsys.readouterr().err
    run("--start", "sg-1")
    assert call.call_args.args[1] == {"suggestion_id": "sg-1", "permission": None, "confirm_widen": None}
    run("--dismiss", "sg-1")
    assert call.call_args.args == ("suggestion.dismiss", {"suggestion_id": "sg-1"})
    call.return_value = {"suggestions": [VIEW], "open": 1, "limits": {}}
    assert run("--list") == 0
    assert capsys.readouterr().out.endswith("sg-1\topen\tPort the importer\n")
    assert run("--list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"suggestions": [VIEW], "open": 1, "limits": {}}


def test_c17_3_refusals_keep_their_codes_and_an_old_daemon_is_69(call, capsys):
    call.side_effect = DaemonError(7, "person-only: starting or dismissing a suggestion is a person's decision",
                                   "answer it in the Subfleet app")
    assert run("--start", "sg-1") == 7
    assert "answer it in the Subfleet app" in capsys.readouterr().err
    call.side_effect = DaemonError(2, "unknown op 'suggestion.list'")
    assert run("--list") == Exit.DAEMON_UNAVAILABLE
    assert cli.RESTART_DAEMON in capsys.readouterr().err
    call.side_effect = DaemonUnavailable("no daemon")
    assert run("--list") == Exit.DAEMON_UNAVAILABLE


def test_c17_1_suggest_is_a_permanent_verb():
    assert compat.PERMANENT[("suggest",)] == ["suggest"]
    assert "subfleet suggest" in cli.__doc__


@pytest.mark.parametrize("retired, successor", [("fable", "opus"), ("claude-fable-5", "opus"),
                                                ("claude-fable-5-1", "opus"), ("sol", "astra")])
def test_c17_2_a_retired_suggest_model_sends_its_successor(call, tmp_path, capsys, retired, successor):
    """C-17.2: `suggest --model` is a model flag; a retired name or Fable id is remapped with
    a stderr note before the daemon sees it (review round 3 of PR #66, finding 2)."""
    brief = tmp_path / "brief.md"
    brief.write_text("Do it.\n")
    assert run("--title", "T", "--tldr", "S", "--brief-file", str(brief), "--workspace", str(tmp_path),
               "--model", retired) == 0
    assert call.call_args.args[1]["model"] == successor
    assert f"suggest: --model {retired} is retired; using {successor}" in capsys.readouterr().err


def test_c17_2_a_current_suggest_model_is_sent_as_given(call, tmp_path, capsys):
    brief = tmp_path / "brief.md"
    brief.write_text("Do it.\n")
    assert run("--title", "T", "--tldr", "S", "--brief-file", str(brief), "--workspace", str(tmp_path),
               "--model", "claude-opus-5-5[1m]") == 0
    assert call.call_args.args[1]["model"] == "claude-opus-5-5[1m]"
    assert "is retired" not in capsys.readouterr().err
