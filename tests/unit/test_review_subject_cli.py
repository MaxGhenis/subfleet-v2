"""D-REVIEW-SUBJECT1: caller identity and review metadata at the CLI boundary."""

from __future__ import annotations

import json

import pytest

from subfleet import cli, launch_env, protocol
from subfleet.contracts import Exit

JOB = "20261113-120000-review"


def _submitted(request):
    return {"job_id": JOB, "request_id": request.args["request_id"],
            "created": True, "state": "queued"}


@pytest.mark.parametrize("detected", [
    {}, {"CLAUDECODE": "1"}, {"CLAUDE_CODE_SESSION_ID": "claude-thread"},
    {"CODEX_THREAD_ID": "codex-thread"}, {"CODEX_SESSION_ID": "codex-session"},
    {"CLAUDECODE": "1", "CODEX_THREAD_ID": "codex-thread"},
])
@pytest.mark.parametrize("explicit", ["claude", "codex"])
def test_explicit_caller_family_wins_over_detection(explicit, detected):
    """I1: neither provider's marker can override the caller's explicit family."""
    assert cli.caller_family(explicit, {**detected, "SUBFLEET_CALLER_FAMILY": "invalid"}) == explicit
    assert cli.caller_family(env={**detected, "SUBFLEET_CALLER_FAMILY": explicit}) == explicit


@pytest.mark.parametrize("explicit,env,source", [
    ("gemini", {"CLAUDECODE": "1"}, "--caller-family"),
    (None, {"SUBFLEET_CALLER_FAMILY": "gemini", "CODEX_THREAD_ID": "thread"},
     "SUBFLEET_CALLER_FAMILY"),
])
def test_invalid_caller_family_is_refused_by_name(explicit, env, source):
    """I1: invalid explicit input never falls back to detection."""
    with pytest.raises(ValueError, match=source + ".*gemini"):
        cli.caller_family(explicit, env)


@pytest.mark.parametrize("env,expected", [
    ({}, None), ({"CLAUDECODE": ""}, "claude"),
    ({"CLAUDE_CODE_SESSION_ID": "thread"}, "claude"),
    ({"CODEX_THREAD_ID": "thread"}, "codex"),
    ({"CODEX_SESSION_ID": "session"}, "codex"),
    ({"CODEX_HOME": "/installation", "CODEX_MANAGED_BY_BUN": "1"}, None),
    ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "thread"}, "claude"),
])
def test_native_caller_family_detection(env, expected):
    assert cli.caller_family(env=env) == expected


def test_nested_submission_detects_the_lane_family_after_launch_environment_selection():
    """A daemon's explicit submitting family cannot leak into another family's lane."""
    parent = {"SUBFLEET_CALLER_FAMILY": "claude", "CODEX_THREAD_ID": "old-thread",
              "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "old-session"}
    child = launch_env.build_environment(environ=parent)
    assert "SUBFLEET_CALLER_FAMILY" not in child
    assert cli.caller_family(env=child) is None
    assert cli.caller_family(env={**child, "CODEX_THREAD_ID": "lane-thread"}) == "codex"
    assert cli.caller_family(env={**child, "CLAUDECODE": "1"}) == "claude"


@pytest.mark.parametrize("env,expected", [
    ({}, None), ({"CODEX_SESSION_ID": "root-session"}, "root-session"),
    ({"CODEX_THREAD_ID": "thread", "CODEX_SESSION_ID": "root-session"}, "thread"),
    ({"SUBFLEET_SESSION_ID": "override", "CODEX_THREAD_ID": "thread"}, "override"),
    ({"CLAUDE_CODE_SESSION_ID": "claude", "SUBFLEET_SESSION_ID": "override",
      "CODEX_THREAD_ID": "thread"}, "claude"),
])
def test_codex_session_fallback_preserves_existing_precedence(env, expected):
    assert cli.session_id(env) == expected


def test_run_forwards_explicit_family_and_review_fields(daemon, root, workdir, monkeypatch):
    server = daemon({"submit": _submitted})
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("SUBFLEET_CALLER_FAMILY", "claude")
    assert cli.main(["run", "--task", "review", "--tier", "standard", "-d",
                     "--caller-family", "codex", "--subject", "https://github.com/Owner/Repo/pull/3/files",
                     "--round", "4", "-C", str(workdir), "review it"]) == 0
    sent = server.args("submit")
    assert sent["caller_family"] == "codex"
    assert sent["review_subject"] == "https://github.com/Owner/Repo/pull/3/files"
    assert sent["review_round"] == 4


def test_run_forwards_detected_codex_identity(daemon, root, workdir, monkeypatch):
    server = daemon({"submit": _submitted})
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread")
    monkeypatch.setenv("CODEX_SESSION_ID", "root-session")
    assert cli.main(["run", "--task", "build", "--tier", "standard", "-d",
                     "-C", str(workdir), "build it"]) == 0
    sent = server.args("submit")
    assert sent["caller_family"] == "codex"
    assert sent["caller_session"] == "codex-thread"
    assert sent["review_subject"] is None and sent["review_round"] is None


def test_resume_records_the_current_caller_identity(daemon, root, workdir, monkeypatch):
    source = {"job_id": JOB, "state": "succeeded", "caller_family": "claude",
              "caller_session": "original", "workdir": str(workdir), "lane_id": "claude-1"}
    server = daemon({"show": lambda request: source, "submit": _submitted})
    monkeypatch.setenv("CODEX_THREAD_ID", "current-thread")
    assert cli.main(["resume", JOB]) == 0
    sent = server.args("submit")
    assert sent["caller_family"] == "codex" and sent["caller_session"] == "current-thread"


def test_resume_refuses_an_invalid_family_before_contacting_the_daemon(root, monkeypatch, capsys):
    monkeypatch.setenv("SUBFLEET_CALLER_FAMILY", "gemini")
    assert cli.main(["resume", JOB]) == Exit.INVALID_INPUT
    assert "SUBFLEET_CALLER_FAMILY" in capsys.readouterr().err


@pytest.mark.parametrize("explicit,env,expected", [
    (None, {"CODEX_THREAD_ID": "current-thread"}, "codex"),
    (None, {"CLAUDECODE": "1"}, "claude"),
    (None, {"SUBFLEET_CALLER_FAMILY": "claude", "CODEX_THREAD_ID": "thread"}, "claude"),
    ("codex", {"SUBFLEET_CALLER_FAMILY": "invalid", "CLAUDECODE": "1"}, "codex"),
])
def test_session_job_submissions_record_the_cli_caller_family(
        daemon, root, workdir, monkeypatch, explicit, env, expected):
    """Handoff and revive share the submitting caller's family detection."""
    from subfleet.sessions.client import Sessions
    server = daemon({"submit": _submitted})
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    args = protocol.SubmitArgs(request_id="session-family", kind="dispatch", workdir=str(workdir),
                               prompt_path="unused", sandbox="read-only", caller_family=explicit,
                               caller_session="retained-session")
    Sessions(cli.Client(root)).submit(args)
    sent = server.args("submit")
    assert sent["caller_family"] == expected
    assert sent["caller_session"] == "retained-session"
    assert args.caller_family == explicit


def test_session_job_submission_refuses_invalid_family_before_contacting_daemon(root, monkeypatch):
    from subfleet.sessions.client import Sessions
    monkeypatch.setenv("SUBFLEET_CALLER_FAMILY", "gemini")
    args = protocol.SubmitArgs(request_id="invalid-family", kind="dispatch", workdir="unused",
                               prompt_path="unused", sandbox="read-only")
    with pytest.raises(protocol.ProtocolError, match="SUBFLEET_CALLER_FAMILY.*gemini") as refused:
        Sessions(object()).submit(args)
    assert refused.value.reason == "caller-family"


def test_run_refuses_invalid_environment_family_before_submission(root, workdir, monkeypatch, capsys):
    monkeypatch.setenv("SUBFLEET_CALLER_FAMILY", "gemini")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    assert cli.main(["run", "-m", "astra", "-d", "-C", str(workdir), "hi"]) == Exit.INVALID_INPUT
    assert "SUBFLEET_CALLER_FAMILY" in capsys.readouterr().err


def test_run_parser_refuses_invalid_flag_family_by_name(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(["run", "--caller-family", "gemini"])
    assert exited.value.code == 2
    assert "--caller-family" in capsys.readouterr().err


@pytest.mark.parametrize("flags,message", [
    (["--task", "build", "--tier", "standard", "--subject", "key"], "--task review"),
    (["--task", "build", "--tier", "standard", "--round", "1"], "--task review"),
    (["--task", "review", "--tier", "standard", "--round", "1"], "--subject"),
    (["--task", "review", "--tier", "standard", "--subject", "key", "--round", "0"], "at least 1"),
])
def test_review_flags_refuse_inapplicable_input(root, workdir, capsys, flags, message):
    assert cli.main(["run", *flags, "-C", str(workdir), "hi"]) == Exit.INVALID_INPUT
    assert message in capsys.readouterr().err


def test_explicit_fifth_round_reaches_the_daemon_refusal(daemon, root, workdir, capsys):
    def refuse(request):
        assert request.args["review_round"] == 5
        return protocol.fail(request.id, Exit.REFUSED, "review: key has had 4 rounds; a fifth needs a person.")
    daemon({"submit": refuse})
    assert cli.main(["run", "--task", "review", "--tier", "standard", "--subject", "key",
                     "--round", "5", "-d", "-C", str(workdir), "hi"]) == Exit.REFUSED
    assert "a fifth needs a person" in capsys.readouterr().err


def test_review_cap_has_a_distinct_encoded_reason_without_unvalidated_details():
    error = protocol.ProtocolError("private exception prose", Exit.REFUSED, "private repair",
                                   reason="review-round-limit",
                                   details={"review_subject": "my-pr", "unvalidated": "private detail"})
    encoded = protocol.encode(protocol.refuse("review-cap", error))
    body = json.loads(encoded)["error"]
    assert body == {"code": 7, "reason": "review-round-limit", "fix": None,
                    "message": "review: my-pr has had 4 rounds; a fifth needs a person. "
                               "Hand the PR to its hub (~/reviews/hubs.md) or queue a decision for Max."}
    decoded = protocol.decode_response(encoded)
    assert decoded.error.reason == "review-round-limit" and decoded.error.details is None
    assert "private" not in encoded.decode()


def test_client_keeps_the_fixed_review_cap_reason_and_discards_details(daemon, root):
    """Even a daemon sending additional fields cannot turn the code into reflection."""
    daemon({"submit": lambda request: protocol.fail(
        request.id, Exit.REFUSED, "review cap", reason="review-round-limit",
        details={"review_subject": "private subject", "unvalidated": "private detail"})})
    with pytest.raises(cli.DaemonError) as error:
        cli.Client(root).call("submit", {})
    assert error.value.code == 7 and error.value.reason == "review-round-limit"
    assert error.value.details is None


@pytest.mark.parametrize("reason", [None, "review-round-limit-suffix", ["review-round-limit"]])
def test_only_the_literal_review_cap_reason_becomes_public(reason):
    error = protocol.ProtocolError("private", Exit.REFUSED, reason=reason,
                                   details={"unvalidated": "private detail"})
    reply = protocol.decode_response(protocol.encode(protocol.refuse("not-review-cap", error)))
    assert reply.error.reason is None and reply.error.details is None
    received = cli.DaemonError(7, "refused", reason=reason, details={"unvalidated": "private detail"})
    assert received.reason is None and received.details is None


@pytest.mark.parametrize("verb", ["runs", "wait"])
def test_mine_uses_codex_thread_id(daemon, root, monkeypatch, capsys, verb):
    server = daemon({"capabilities": lambda request: {"capabilities": ["jobs.kind.v1"]},
                     "list": lambda request: {"jobs": []},
                     "wait": lambda request: {"jobs": {JOB: {"job_id": JOB, "state": "succeeded", "rc": 0}}}})
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread")
    assert cli.main([verb, "--mine"]) == 0
    assert server.args("list" if verb == "runs" else "wait")["mine"] == "codex-thread"
    capsys.readouterr()


@pytest.mark.parametrize("verb", ["runs", "wait"])
def test_mine_missing_identity_names_codex_and_override(root, capsys, verb):
    assert cli.main([verb, "--mine"]) == Exit.INVALID_INPUT
    error = capsys.readouterr().err
    assert "CODEX_THREAD_ID" in error and "SUBFLEET_SESSION_ID" in error


def test_runs_round_is_beside_review_name_and_json_retains_columns(daemon, capsys):
    row = {"job_id": JOB, "state": "queued", "task": "review", "name": "pr-review",
           "caller_family": "codex", "review_subject": "key", "review_round": 2}
    daemon({"capabilities": lambda request: {"capabilities": ["jobs.kind.v1"]},
            "list": lambda request: {"jobs": [row]}, "show": lambda request: {"job": row}})
    assert cli.main(["runs"]) == 0
    assert "pr-review [round 2]" in capsys.readouterr().out
    assert cli.main(["runs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == row
    assert cli.main(["runs", "show", JOB, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["job"] == row


def test_run_help_documents_caller_and_review_flags(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(["run", "--help"])
    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    assert all(flag in help_text for flag in ("--caller-family", "--subject", "--round"))
