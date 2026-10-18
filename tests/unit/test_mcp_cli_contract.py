"""C-12.9: the CLI transmits explicit MCP opt-in and shows its durable record."""

import json

import pytest

from subfleet import cli, protocol
from subfleet.contracts import Exit


def test_c12_9_run_repeatable_mcp_names_reach_submit(daemon, tmp_path, capsys):
    server = daemon({"submit": lambda request: {
        "job_id": "fixture-job", "request_id": request.args["request_id"], "created": True}})
    assert cli.main(["run", "-m", "haiku", "-s", "workspace-write", "-C", str(tmp_path),
        "--allow-tmp", "--detach", "--mcp", "selected", "--mcp", "second", "do the work"]) == 0
    submitted = next(request.args for request in server.requests if request.op == "submit")
    assert submitted["mcp_servers"] == ["selected", "second"]
    assert "mcp_config" not in submitted


@pytest.mark.parametrize("options,message", [
    (["-s", "read-only"], "read-only job starts no MCP servers"),
    (["-H", "/fixture-codex-home", "-s", "workspace-write"], "only a Claude launch starts MCP servers"),
])
def test_c12_9_cli_refuses_unusable_opt_in_before_contacting_daemon(tmp_path, capsys, options, message, monkeypatch, root):
    contacted = []
    monkeypatch.setattr(cli, "_client", lambda _: contacted.append(True))
    assert cli.main(["run", "-m", "haiku", "-C", str(tmp_path), "--allow-tmp",
                     "--mcp", "selected", *options, "do the work"]) == int(Exit.REFUSED)
    assert message in capsys.readouterr().err
    assert contacted == []


@pytest.mark.parametrize("options", [[], ["--json"]])
def test_c12_9_runs_show_displays_named_servers_and_their_sources(daemon, capsys, options):
    daemon({"show": lambda _: {"job": {"job_id": "fixture-job", "mcp_servers": '["selected"]'},
                               "mcp": {"selected": {"scope": "project", "source": "/fixture/.mcp.json"}}}})
    assert cli.main(["runs", "show", "fixture-job", *options]) == 0
    # The bare v1-compatible display is metadata JSON followed by the artifact.
    shown = json.loads(capsys.readouterr().out.split("--- out.md ---")[0])
    assert json.loads(shown["job"]["mcp_servers"]) == ["selected"]
    assert shown["mcp"]["selected"]["source"] == "/fixture/.mcp.json"


def test_c12_9_protocol_defaults_never_name_a_server():
    args = protocol.coerce_args(protocol.SubmitArgs, {
        "request_id": "fixture", "kind": "dispatch", "workdir": "/fixture",
        "prompt_path": "/fixture/prompt", "sandbox": "workspace-write"})
    assert args.mcp_servers == []
