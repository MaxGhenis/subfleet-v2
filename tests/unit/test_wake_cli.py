import json
import subprocess
import uuid
from unittest.mock import Mock

from subfleet import cli, compat
from subfleet.conversations import wakes


def test_wake_cli_uses_calling_session_and_turn_and_permanent_front_door(monkeypatch, capsys):
    request_id = str(uuid.uuid4())
    monkeypatch.setenv("SUBFLEET_SESSION_ID", "conversation-session")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("SUBFLEET_TURN_JOB", "turn-job")
    call = Mock(return_value={"request_id": request_id, "kinds": ["runs", "pr"]})
    monkeypatch.setattr(cli.Client, "call", call)
    args = cli.build_parser().parse_args(["wake", "--runs", "run1", "run2", "--pr", "o/r#1", "--note", "Deliver it", "--request-id", request_id])
    assert args.handler(args) == 0
    assert compat.PERMANENT[("wake",)] == ["wake"]
    assert call.call_args.args == ("conversation.wake", {
        "session_id": "conversation-session", "calling_job": "turn-job", "request_id": request_id,
        "runs": ["run1", "run2"], "prs": ["o/r#1"], "at": None, "note": "Deliver it"})
    assert "Wake recorded" in capsys.readouterr().out


def test_batched_gh_query_reads_all_targets_once_and_does_not_treat_partial_checks_as_finished(monkeypatch):
    pr = {"state": "OPEN", "headRefOid": "head", "reviews": {"nodes": [{"id": "review1", "submittedAt": "now"}]},
          "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
              "nodes": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "now"}],
              "pageInfo": {"hasNextPage": True}}}}}]}}
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"data": {"p0": {"pullRequest": pr}, "p1": {"pullRequest": pr}}}), ""))
    monkeypatch.setattr(wakes.subprocess, "run", run)
    snapshots = wakes.query_prs(["o/r#1", "p/s#2"])
    assert run.call_count == 1
    query = json.loads(run.call_args.kwargs["input"])["query"]
    assert 'repository(owner:"o",name:"r")' in query and 'repository(owner:"p",name:"s")' in query
    assert run.call_args.args[0] == ["gh", "api", "graphql", "--input", "-"]
    assert run.call_args.kwargs["timeout"] == 20
    assert not wakes.pr_changed({"state": "OPEN", "checks": [], "reviews": ["review1"]}, snapshots["o/r#1"])
