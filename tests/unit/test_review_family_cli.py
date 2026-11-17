"""C-6.23: caller-aware explanations and explicit same-family authorization."""

import json

import pytest

from subfleet import cli
from subfleet.contracts import Exit


JOB = "20261113-120000-review"


@pytest.mark.parametrize("marker,family", [(None, None), ("CLAUDECODE", "claude"),
                                         ("CODEX_THREAD_ID", "codex")])
def test_why_detects_caller_exactly_as_run(daemon, monkeypatch, marker, family):
    server = daemon({"why": lambda request: {"decision": None}})
    if marker:
        monkeypatch.setenv(marker, "fixture")
    assert cli.main(["why", "--task", "review", "--tier", "hard"]) == 0
    assert server.args("why")["caller_family"] == family
    assert server.args("why")["allow_same_family"] is False


def test_why_explicit_family_overrides_environment(daemon, monkeypatch):
    server = daemon({"why": lambda request: {"decision": None}})
    monkeypatch.setenv("SUBFLEET_CALLER_FAMILY", "claude")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["why", "--task", "review", "--tier", "hard",
                     "--caller-family", "codex", "--allow-same-family"]) == 0
    assert server.args("why")["caller_family"] == "codex"
    assert server.args("why")["allow_same_family"] is True


def test_why_invalid_family_environment_is_named_before_connecting(root, monkeypatch, capsys):
    monkeypatch.setenv("SUBFLEET_CALLER_FAMILY", "invalid")
    assert cli.main(["why", "--task", "review", "--tier", "hard"]) == Exit.INVALID_INPUT
    assert "SUBFLEET_CALLER_FAMILY" in capsys.readouterr().err


def test_why_existing_job_uses_its_recorded_family(daemon, monkeypatch):
    server = daemon({"why": lambda request: {"decision": None}})
    monkeypatch.setenv("SUBFLEET_CALLER_FAMILY", "invalid")
    assert cli.main(["why", JOB]) == 0
    assert server.args("why")["job_id"] == JOB
    assert server.args("why")["caller_family"] is None


@pytest.mark.parametrize("allowed", [False, True])
def test_run_forwards_same_family_authorization(daemon, workdir, allowed):
    # #314's fix: a submission carrying a caller family needs `review.subject.v1`.
    server = daemon({"submit": lambda request: {"job_id": JOB, "created": True, "state": "queued"},
                     "capabilities": lambda request: {"capabilities": ["review.subject.v1"]}})
    argv = ["run", "--task", "review", "--tier", "hard", "--caller-family", "codex",
            "-C", str(workdir), "-d", "review"]
    if allowed:
        argv.append("--allow-same-family")
    assert cli.main(argv) == 0
    assert server.args("submit")["allow_same_family"] is allowed


def test_runs_show_json_keeps_the_family_and_override(daemon, capsys):
    row = {"job_id": JOB, "caller_family": "codex", "allow_same_family": True}
    daemon({"show": lambda request: {"job": row}})
    assert cli.main(["runs", "show", JOB, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["job"] == row


@pytest.mark.parametrize("verb", ["run", "why"])
def test_help_documents_family_flags(verb, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args([verb, "--help"])
    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    assert "--caller-family" in help_text and "--allow-same-family" in help_text
