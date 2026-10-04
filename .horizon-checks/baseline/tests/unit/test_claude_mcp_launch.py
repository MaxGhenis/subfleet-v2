"""C-12.9: detached Claude launches expose exactly the job's named servers."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import ClaudeAdapter, EMPTY_MCP_CONFIG
from subfleet.contracts import Sandbox
from tests.conftest import make_job, make_lane


def launch_job(tmp_path, *, names=(), servers=None, sandbox=Sandbox.WORKSPACE_WRITE,
               resume=False):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the work.\n")
    config = tmp_path / "job-mcp.json"
    if servers is not None:
        config.write_text(json.dumps({"mcpServers": servers}))
    job = replace(make_job(str(workdir), str(prompt), sandbox=sandbox),
                  mcp_servers=tuple(names), mcp_config=str(config))
    adapter = ClaudeAdapter(claude_bin="claude")
    arguments = (job, "job/a1", tmp_path / "a1", make_lane(), {})
    if resume:
        launch = adapter.resume_launch(*arguments, "existing-session", prompt, None,
                                       model_id="claude-opus-5")
    else:
        launch = adapter.build_launch(*arguments, "claude-opus-5", None, prompt, None)
    return launch


def visible_servers(launch):
    assert launch.argv.count("--strict-mcp-config") == 1
    assert launch.argv.count("--mcp-config") == 1
    argument = launch.argv[launch.argv.index("--mcp-config") + 1]
    document = json.loads(argument if argument == EMPTY_MCP_CONFIG else Path(argument).read_text())
    return document["mcpServers"]


@pytest.mark.parametrize("resume", [False, True])
def test_writable_default_never_reads_a_config_or_enables_servers(tmp_path, resume):
    launch = launch_job(tmp_path, resume=resume)
    assert "--dangerously-skip-permissions" in launch.argv
    assert visible_servers(launch) == {}
    assert launch.argv[launch.argv.index("--mcp-config") + 1] == EMPTY_MCP_CONFIG
    assert not (tmp_path / "a1" / "mcp-config.json").exists()


@pytest.mark.parametrize("resume", [False, True])
def test_writable_opt_in_exposes_only_named_entries(tmp_path, resume):
    servers = {"gitnexus": {"command": "npx", "args": ["gitnexus", "mcp"]},
               "messages": {"command": "send"},
               "notes": {"type": "http", "url": "https://example.test/mcp"}}
    launch = launch_job(tmp_path, names=("gitnexus", "notes"), servers=servers, resume=resume)
    assert visible_servers(launch) == {name: servers[name] for name in ("gitnexus", "notes")}
    path = Path(launch.argv[launch.argv.index("--mcp-config") + 1])
    assert path.parent == tmp_path / "a1"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("servers", [None, {}, {"other": {"command": "unused"}}, {"chosen": None}])
def test_missing_snapshot_or_named_entry_refuses_launch(tmp_path, servers):
    with pytest.raises(AdapterError, match="MCP config cannot give the servers it names"):
        launch_job(tmp_path, names=("chosen",), servers=servers)


@pytest.mark.parametrize("resume", [False, True])
def test_read_only_still_uses_empty_mcp_even_if_spec_has_names(tmp_path, resume):
    launch = launch_job(tmp_path, sandbox=Sandbox.READ_ONLY, names=("chosen",), resume=resume)
    assert visible_servers(launch) == {}
    assert "--dangerously-skip-permissions" not in launch.argv
    assert launch.argv[launch.argv.index("--permission-mode") + 1] == "plan"


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(available=st.dictionaries(st.text(alphabet="abcdef", min_size=1, max_size=12),
                                st.one_of(st.just({"command": "example"}),
                                          st.just({"type": "http", "url": "https://example.test/mcp"})),
                                max_size=12),
       chosen=st.data(), writable=st.booleans(), resume=st.booleans())
def test_any_valid_job_launch_sees_exactly_its_opt_in(tmp_path, available, chosen, writable, resume):
    # Each example overwrites its files; read-only submissions authorize no MCP.
    names = chosen.draw(st.sets(st.sampled_from(sorted(available)), max_size=len(available))) if available else set()
    if not writable:
        names = set()
    launch = launch_job(tmp_path, names=sorted(names), servers=available,
                        sandbox=Sandbox.WORKSPACE_WRITE if writable else Sandbox.READ_ONLY,
                        resume=resume)
    assert visible_servers(launch) == {name: available[name] for name in names}
