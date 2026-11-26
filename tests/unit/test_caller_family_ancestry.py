"""Nested model callers use a synthetic process ancestry, never the host's processes."""

from subprocess import CompletedProcess

import pytest

from subfleet import cli, protocol
from subfleet.contracts import Exit


BOTH = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "synthetic-claude-session",
        "CLAUDE_PID": "111", "CODEX_THREAD_ID": "synthetic-codex-thread"}


@pytest.mark.parametrize("family", ["claude", "codex"])
def test_nearest_model_executable_decides_nested_family(monkeypatch, family):
    queried = []
    monkeypatch.setattr(cli.os, "getppid", lambda: 300)

    def ps(command, **kwargs):
        pid = int(command[-1])
        queried.append(pid)
        output = "299 /bin/zsh\n" if pid == 300 else f"298 /synthetic/bin/{family}\n"
        return CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(cli.subprocess, "run", ps)
    assert cli.caller_family(env=BOTH) == family
    assert queried == [300, 299]


@pytest.mark.parametrize("failure", ["unreadable", "malformed", "cycle"])
def test_unreadable_ancestry_leaves_nested_family_unknown(monkeypatch, failure):
    monkeypatch.setattr(cli.os, "getppid", lambda: 300)

    def ps(command, **kwargs):
        if failure == "unreadable":
            raise OSError("synthetic ps failure")
        return CompletedProcess(command, 0,
                                "unreadable\n" if failure == "malformed" else "300 /bin/zsh\n", "")

    monkeypatch.setattr(cli.subprocess, "run", ps)
    assert cli.caller_family(env=BOTH) is None


def _nested_env(monkeypatch, family):
    for name, value in BOTH.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "_ancestor_caller_family", lambda: family, raising=False)


def _handlers():
    return {"submit": lambda request: {"job_id": "20261113-120000-synthetic", "created": True},
            "capabilities": lambda request: {
                "capabilities": [protocol.REVIEW_SUBJECT_CAPABILITY, protocol.REVIEW_FAMILY_CAPABILITY]}}


def test_nested_codex_submission_uses_its_own_thread_identity(daemon, root, workdir, monkeypatch):
    server = daemon(_handlers())
    _nested_env(monkeypatch, "codex")
    assert cli.main(["run", "--task", "review", "--tier", "standard", "-d", "-C", str(workdir),
                     "synthetic review"]) == 0
    sent = server.args("submit")
    assert sent["caller_family"] == "codex"
    assert sent["caller_session"] == "synthetic-codex-thread"
    assert sent["caller_pid"] is None


def test_ambiguous_review_requires_an_explicit_family(daemon, root, workdir, monkeypatch, capsys):
    server = daemon(_handlers())
    _nested_env(monkeypatch, None)
    assert cli.main(["run", "--task", "review", "--tier", "standard", "-d", "-C", str(workdir),
                     "synthetic review"]) == Exit.REFUSED
    assert "submit" not in server.ops()
    assert "--caller-family" in capsys.readouterr().err


def test_ambiguous_nonreview_records_unknown_identity(daemon, root, workdir, monkeypatch):
    server = daemon(_handlers())
    _nested_env(monkeypatch, None)
    assert cli.main(["run", "--task", "build", "--tier", "standard", "-d", "-C", str(workdir),
                     "synthetic build"]) == 0
    sent = server.args("submit")
    assert sent["caller_family"] is None and sent["caller_session"] is None


def _review_source(workdir):
    return {"job_id": "20261113-120000-synthetic-source", "state": "failed", "task": "review",
            "tier": "standard", "workdir": str(workdir), "lane_id": "codex-1",
            "review_subject": "synthetic-pr", "review_round": 2}


def test_ambiguous_review_resume_requires_an_explicit_family(daemon, root, workdir, monkeypatch, capsys):
    source = _review_source(workdir)
    server = daemon({**_handlers(), "show": lambda request: {"job": source}})
    _nested_env(monkeypatch, None)
    assert cli.main(["resume", source["job_id"]]) == Exit.REFUSED
    assert "submit" not in server.ops()
    assert "--caller-family" in capsys.readouterr().err


def test_resume_explicit_family_resolves_ambiguous_ancestry(daemon, root, workdir, monkeypatch):
    source = _review_source(workdir)
    server = daemon({**_handlers(), "show": lambda request: {"job": source}})
    _nested_env(monkeypatch, None)
    assert cli.main(["resume", source["job_id"], "--caller-family", "codex"]) == 0
    sent = server.args("submit")
    assert sent["caller_family"] == "codex" and sent["caller_session"] == "synthetic-codex-thread"


def test_ambiguous_review_session_submission_requires_an_explicit_family(daemon, root, monkeypatch):
    from subfleet.sessions.client import Sessions
    server = daemon(_handlers())
    _nested_env(monkeypatch, None)
    args = protocol.SubmitArgs(request_id="synthetic-review", kind="dispatch", workdir="unused",
                               prompt_path="unused", sandbox="read-only", task="review", tier="standard")
    with pytest.raises(protocol.ProtocolError, match="caller family") as refused:
        Sessions(cli.Client(root)).submit(args)
    assert refused.value.code == Exit.REFUSED and "--caller-family" in refused.value.fix
    assert "submit" not in server.ops()


def test_resume_refuses_review_fields_on_an_older_daemon(daemon, root, workdir, capsys):
    source = _review_source(workdir)
    server = daemon({**_handlers(), "show": lambda request: {"job": source},
                     "capabilities": lambda request: {"capabilities": []}})
    assert cli.main(["resume", source["job_id"]]) == Exit.DAEMON_UNAVAILABLE
    assert "submit" not in server.ops()
    assert "review.subject.v1" in capsys.readouterr().err
