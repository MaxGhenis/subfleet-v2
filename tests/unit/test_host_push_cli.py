"""C-8.5, review P3-10: `--push-branch` reaches only a daemon that says it pushes.

A daemon older than `push.v1` drops `push_branch` (C-16.2) and would accept the
job without ever pushing, so the CLI refuses before it sends anything.
"""
import json

import pytest

from subfleet import cli, protocol

JOB = "20261009-120000-push"

#: One daemon that predates `capabilities` altogether (it answers "unknown
#: op"), and one that serves capabilities but not `push.v1` (2.1.11).
OLDER_DAEMONS = {"no-capabilities-op": {},
                 "no-push-capability": {"capabilities": lambda request: {
                     "protocol": 1, "capabilities": ["conversations.v1", protocol.JOBS_KIND_CAPABILITY]}}}


def submit_ok(request):
    return {"job_id": JOB, "request_id": request.args.get("request_id"), "created": True, "state": "queued"}


@pytest.fixture(params=sorted(OLDER_DAEMONS))
def older_daemon(request, daemon):
    return daemon({**OLDER_DAEMONS[request.param], "submit": submit_ok})


def test_run_push_branch_is_refused_by_a_daemon_without_push(older_daemon, capsys, workdir):
    code = cli.main(["run", "--push-branch", "jobs/x", "-s", "workspace-write", "--task", "build",
                     "--tier", "standard", "-C", str(workdir), "do the work"])
    captured = capsys.readouterr()
    assert code == 69 and captured.out == "", captured.err
    assert protocol.PUSH_CAPABILITY in captured.err and "never push" in captured.err
    assert "fix: subfleet daemon stop && subfleet daemon start" in captured.err
    assert "submit" not in older_daemon.ops()


def test_run_batch_with_a_push_branch_sends_nothing_to_a_daemon_without_push(older_daemon, capsys,
                                                                             workdir, tmp_path):
    manifest = tmp_path / "jobs.json"
    manifest.write_text(json.dumps({"jobs": [
        {"name": "plain", "task": "build", "tier": "standard", "workdir": str(workdir), "prompt_text": "first"},
        {"name": "pushed", "task": "build", "tier": "standard", "workdir": str(workdir), "prompt_text": "second",
         "sandbox": "workspace-write", "push_branch": "jobs/y"}]}))
    assert cli.main(["run", "--batch", str(manifest), "-d"]) == 69
    assert protocol.PUSH_CAPABILITY in capsys.readouterr().err
    assert "submit" not in older_daemon.ops()


def test_a_daemon_advertising_push_receives_the_branch(daemon, capsys, workdir):
    server = daemon({"capabilities": lambda request: {"protocol": 1, "capabilities": [protocol.PUSH_CAPABILITY]},
                     "submit": submit_ok})
    assert cli.main(["run", "-d", "--push-branch", "jobs/x", "-s", "workspace-write", "--task", "build",
                     "--tier", "standard", "-C", str(workdir), "do the work"]) == 0
    assert server.ops() == ["capabilities", "submit"]
    assert server.args("submit")["push_branch"] == "jobs/x"


def test_a_run_without_a_push_branch_never_asks(daemon, capsys, workdir):
    server = daemon({"submit": submit_ok})
    assert cli.main(["run", "-d", "--task", "build", "--tier", "standard", "-C", str(workdir), "do the work"]) == 0
    assert server.ops() == ["submit"]


def test_this_daemon_advertises_push():
    from subfleet.conversations.service import CAPABILITIES
    assert protocol.PUSH_CAPABILITY in CAPABILITIES
