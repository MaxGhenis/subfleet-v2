"""Review r5's four CLI refusal cases, and their neighbours, against a real in-process daemon.

Rounds 5 to 7 turned every daemon refusal into `args: request was refused`; each
case here printed that instead of what it names (C-11.1, C-16.3, C-25.7).
"""
from __future__ import annotations

import pytest

from subfleet import cli, client
from tests.fake.test_request_routing import served  # noqa: F401 - real socket fixture


def prepare(served, monkeypatch, workdir):
    daemon, _ = served
    # `served` records the daemon as booted at "routing-boot" and started at
    # "routing-start"; the CLI checks daemon.lock against the same (C-5.3).
    monkeypatch.setattr(client, "boot_id", lambda: "routing-boot")
    monkeypatch.setattr(client, "proc_start", lambda pid: "routing-start")
    monkeypatch.setenv("SUBFLEET_HOME", str(daemon.root))
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
                 "SUBFLEET_RUN_DETACH", "SUBFLEET_DAEMON_BIN", "SUBFLEET_TURN_JOB"):
        monkeypatch.delenv(name, raising=False)
    prompt = workdir / "prompt.md"
    prompt.write_text("first prompt")
    return daemon, prompt


def test_cli_different_payload_refusal_names_the_existing_job(served, monkeypatch, workdir, capsys):
    """C-16.3 the different-payload refusal names the job."""
    daemon, prompt = prepare(served, monkeypatch, workdir)
    args = ["run", "-d", "-m", "astra", "-s", "read-only", "-C", str(workdir), "-p", str(prompt),
            "--request-id", "probe-rid-1"]
    assert cli.main(args) == 0
    job_id = capsys.readouterr().out.strip()
    prompt.write_text("a different prompt")
    assert cli.main(args) == 2
    assert f"request id already used with a different payload by job {job_id}" in capsys.readouterr().err
    assert len(daemon.store.query("SELECT job_id FROM jobs WHERE request_id='probe-rid-1'")) == 1


@pytest.mark.parametrize("policy", (False, True))
def test_cli_writable_outside_git_names_the_policy_fix(served, monkeypatch, workdir, capsys, policy):
    """C-11.1 a job that writes by policy outside git is refused with the fix `-s read-only`."""
    daemon, prompt = prepare(served, monkeypatch, workdir)
    options = ["--task", "build", "--tier", "standard"] if policy else ["-m", "astra", "-s", "workspace-write"]
    assert cli.main(["run", *options, "-C", str(workdir), "-p", str(prompt)]) == 7
    err = capsys.readouterr().err
    if policy:
        assert "build jobs write by policy, and this directory is not a git repository" in err
        assert "or pass -s read-only to run it here without writing" in err
    else:
        assert "writable jobs require a committed git repository" in err
        assert "initialize a feature branch and commit a baseline" in err
    assert daemon.store.query("SELECT job_id FROM jobs") == []


@pytest.mark.parametrize("command", (["runs", "show"], ["kill"]))
def test_cli_unknown_job_names_the_checked_id(served, monkeypatch, workdir, capsys, command):
    prepare(served, monkeypatch, workdir)
    assert cli.main([*command, "20260925-000000-none"]) == 2
    assert "unknown job 20260925-000000-none" in capsys.readouterr().err


def test_cli_release_limits_names_the_unknown_lane_and_fix(served, monkeypatch, workdir, capsys):
    prepare(served, monkeypatch, workdir)
    assert cli.main(["lanes", "release", "codex-99", "--limits", "--note", "probe"]) == 2
    err = capsys.readouterr().err
    assert "lanes release --limits: unknown lane codex-99" in err and "fix: subfleet lanes list" in err


def test_cli_transfer_names_the_unknown_lane_and_fix(served, monkeypatch, workdir, capsys):
    prepare(served, monkeypatch, workdir)
    assert cli.main(["lanes", "transfer", "codex-99", "--to", "v2"]) == 2
    err = capsys.readouterr().err
    assert "lanes transfer: unknown lane codex-99" in err and "fix: subfleet lanes list" in err


def test_cli_hold_leaves_out_a_lane_that_is_not_a_lane_id(served, monkeypatch, workdir, capsys):
    """C-25.7 a value that fails its check is left out; the refusal still says what and the fix."""
    prepare(served, monkeypatch, workdir)
    assert cli.main(["lanes", "hold", "password=SECRET7-left", "--until", "2026-10-14T00:00:00Z"]) == 2
    err = capsys.readouterr().err
    assert "lanes hold: unknown lane" in err and "fix: subfleet lanes list" in err
    assert "SECRET7" not in err and "request was refused" not in err


# --- the operator's override of a quarantined probe (C-5.7a) ----------------------

@pytest.mark.parametrize("command, message, fix", [
    (["kill", "20260925-000000-none", "--confirm-dead"], "unknown job 20260925-000000-none", None),
    (["kill", "20260925-000000-none", "--force-release"], "unknown job 20260925-000000-none", None),
    (["lanes", "release-probe", "codex-99"], "lanes release-probe: unknown lane codex-99", "subfleet lanes list"),
    (["lanes", "release-probe", "nonsense"], "lanes release-probe: unknown lane", "subfleet lanes list"),
    (["lanes", "release-probe", "probe:timer:never"],
     "lanes release-probe: no probe by that holder was ever recorded", "subfleet status"),
])
def test_cli_probe_override_refusals_say_what_they_refuse(served, monkeypatch, workdir, capsys, command, message, fix):
    """C-25.7 on release/217: a handler refusal reads its reason's template. The
    override's refusals had none, so each read `operation: request was refused`
    and lost its fix (review of the merge of release/217, finding 1)."""
    prepare(served, monkeypatch, workdir)
    assert cli.main(command) == 2
    err = capsys.readouterr().err
    assert message in err and "request was refused" not in err
    assert (f"fix: {fix}" in err) if fix else ("fix:" not in err)


@pytest.mark.parametrize("op, args, message, fix", [
    ("lanes", {"action": "release-probe", "lane_id": "  "},
     "lanes release-probe: name a lane or a probe holder", "subfleet status"),
    ("lanes", {"action": "release-probe", "lane_id": "codex-99", "issued_at": "2026-09-29T12:00:00+00:00:01"},
     "issued_at is not an instant of the form YYYY-MM-DDTHH:MM:SS[.ffffff][Z|+HH:MM]", None),
    ("kill", {"job_id": "20260925-000000-none", "confirm_dead": True, "issued_at": "2026-02-30T00:00:00Z"},
     "issued_at is not an instant of the form YYYY-MM-DDTHH:MM:SS[.ffffff][Z|+HH:MM]", None),
])
def test_probe_override_refusals_no_cli_sends_read_their_templates(served, op, args, message, fix):
    """The refusals the CLI never provokes (it names a target and stamps `issued_at`
    itself) still read their templates on the wire, as exit 2."""
    from tests.fake.test_request_routing import request
    _, path = served
    reply, _ = request(path, op, **args)
    assert (reply["ok"], reply["error"]["code"]) == (False, 2)
    assert (reply["error"]["message"], reply["error"]["fix"]) == (message, fix)


def test_a_resolution_naming_a_credential_job_id_leaves_it_out(served, monkeypatch, workdir, capsys):
    """`kill --confirm-dead` looks the job up as `kill` does (`Daemon._unknown_job`):
    an id holding a credential is left out of the reply and of daemon.log, whose
    scrubber reads it whole only (review r6, P2)."""
    daemon, _ = prepare(served, monkeypatch, workdir)
    token = "sk-" + "a" * 20
    assert cli.main(["kill", "20261013-000000-" + token, "--confirm-dead"]) == 2
    err = capsys.readouterr().err
    assert "unknown job" in err and token not in err
    daemon._log_handler.flush()
    assert token not in (daemon.root / "daemon.log").read_text()
