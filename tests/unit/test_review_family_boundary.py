"""Family-aware CLI requests and refusals at the v1 socket boundary."""

import json

import pytest

from subfleet import cli, protocol, refusals
from subfleet.adapters.base import AdapterError
from subfleet.client import Client, DaemonError
from subfleet.contracts import Exit
from subfleet.conversations.service import CAPABILITIES
from subfleet.gate import cli as gate_cli


JOB = "20261126-120000-synthetic-review"


def subject_only(_request):
    return {"capabilities": ["review.subject.v1"]}


def submitted(_request):
    return {"job_id": JOB, "created": True, "state": "queued"}


def review_source(workdir):
    return {"job": {"job_id": JOB, "state": "succeeded", "task": "review",
                    "tier": "standard", "pinned_model": "astra", "workdir": str(workdir),
                    "lane_id": "codex-1", "allow_same_family": True}}


def assert_family_capability_refusal(result, server, capsys):
    assert result == Exit.DAEMON_UNAVAILABLE
    assert server.ops() == ["capabilities"]
    assert "review.family.v1" in capsys.readouterr().err


def test_daemon_advertises_family_routing():
    assert "review.family.v1" in CAPABILITIES


@pytest.mark.parametrize("task,flags,session", [
    ("review", ["--caller-family", "claude"], None),
    ("review", ["--allow-same-family"], None),
    ("build", [], "synthetic-thread"),
])
def test_run_requires_family_capability(daemon, workdir, monkeypatch, capsys, task, flags, session):
    if session:
        monkeypatch.setenv("CODEX_THREAD_ID", session)
    server = daemon({"capabilities": subject_only, "submit": submitted})
    result = cli.main(["run", "--task", task, "--tier", "standard",
                       "-C", str(workdir), "-d", *flags, "synthetic prompt"])
    assert_family_capability_refusal(result, server, capsys)


def test_batch_checks_family_capability_before_any_submission(daemon, workdir, tmp_path, capsys):
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps({"jobs": [
        {"task": "build", "tier": "standard", "workdir": str(workdir), "prompt_text": "build"},
        {"task": "review", "tier": "standard", "workdir": str(workdir), "prompt_text": "review"},
    ]}))
    server = daemon({"capabilities": subject_only, "submit": submitted})
    result = cli.main(["run", "--batch", str(manifest), "--caller-family", "claude", "-d"])
    assert_family_capability_refusal(result, server, capsys)


def test_resume_checks_family_capability_before_show(daemon, workdir, capsys):
    server = daemon({"capabilities": subject_only, "submit": submitted,
                     "show": lambda request: review_source(workdir)})
    result = cli.main(["resume", JOB, "--caller-family", "claude"])
    assert_family_capability_refusal(result, server, capsys)


def test_gate_start_requires_family_capability(daemon, root, monkeypatch, capsys):
    for key in gate_cli.MANAGED:
        monkeypatch.delenv(key, raising=False)
    server = daemon({"capabilities": subject_only,
                     "gate.start": lambda request: {"status": "agreement", "code": 0}})
    result = cli.main(["gate", "plan", "synthetic-plan.md", "--peer", "astra", "--main-approve"])
    assert_family_capability_refusal(result, server, capsys)


def test_why_requires_family_capability_for_family_routing(daemon, capsys):
    server = daemon({"capabilities": subject_only, "why": lambda request: {"text": "synthetic route"}})
    result = cli.main(["why", "--task", "review", "--tier", "hard", "--caller-family", "claude"])
    assert_family_capability_refusal(result, server, capsys)


def test_unattributed_review_runs_on_older_daemon(daemon, workdir):
    server = daemon({"submit": submitted})
    assert cli.main(["run", "--task", "review", "--tier", "standard",
                     "-C", str(workdir), "-d", "synthetic review"]) == Exit.OK
    assert server.ops() == ["submit"]


def test_why_and_run_refuse_ambiguous_review_family(daemon, workdir, monkeypatch, capsys):
    server = daemon({"why": lambda request: {"text": "synthetic route"}})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "synthetic-claude")
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-codex")
    monkeypatch.setattr(cli, "_ancestor_caller_family", lambda: None)
    assert cli.main(["run", "--task", "review", "--tier", "hard",
                     "-C", str(workdir), "-d", "synthetic review"]) == Exit.REFUSED
    run_error = capsys.readouterr().err
    assert cli.main(["why", "--task", "review", "--tier", "hard"]) == Exit.REFUSED
    assert capsys.readouterr().err == run_error
    assert "--caller-family" in run_error and not server.ops()


@pytest.mark.parametrize("reason", ["review-same-family", "same-family-consent"])
def test_review_refusal_reason_crosses_the_socket(daemon, root, reason):
    message, fix = refusals.HANDLER_MESSAGES[reason]
    error = AdapterError(message, code=7, fix=fix, reason=reason)
    daemon({"submit": lambda request: protocol.refuse(request.id, error)})
    with pytest.raises(DaemonError) as rejected:
        Client(root).call("submit", {})
    assert rejected.value.code == Exit.REFUSED
    assert str(rejected.value) == message
    assert rejected.value.reason == reason


def test_cli_consent_refusal_names_the_same_reason(daemon, workdir, monkeypatch, capsys):
    server = daemon({"submit": submitted})
    monkeypatch.setenv("SUBFLEET_JOB", "synthetic-parent")
    result = cli.main(["run", "--task", "review", "--tier", "hard", "--allow-same-family",
                       "-C", str(workdir), "-d", "synthetic review"])
    assert result == Exit.REFUSED
    error = capsys.readouterr().err
    assert "same-family-consent" in error
    assert refusals.HANDLER_MESSAGES["same-family-consent"][0] in error
    assert "SUBFLEET_JOB" in error and not server.ops()


def test_resume_same_family_refusal_suggests_a_fresh_review_or_other_session(daemon, workdir, capsys):
    reason = "review-same-family"
    message, fix = refusals.HANDLER_MESSAGES[reason]
    error = AdapterError(message, code=7, fix=fix, reason=reason)
    daemon({"capabilities": lambda request: {"capabilities": ["review.subject.v1", "review.family.v1"]},
            "show": lambda request: review_source(workdir),
            "submit": lambda request: protocol.refuse(request.id, error)})
    assert cli.main(["resume", JOB, "--caller-family", "codex"]) == Exit.REFUSED
    advice = capsys.readouterr().err
    assert "session of the other family" in advice and "submit a fresh review with consent" in advice
    assert "--allow-same-family" not in advice and "pass -m" not in advice
