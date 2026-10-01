"""C-12.9: detached job MCP admission and durable continuation, without providers."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from subfleet import daemon as module, protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude_mcp import JOB_CONFIG_NAME
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential,
                               Outcome, OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon
from tests.fake.test_workspace_contract import repository
from tests.fake_adapter import FakeAdapter


@pytest.fixture
def mcp_daemon(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    repository(daemon, harness)
    config_home = harness.root / "claude-config"
    config_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_home))
    monkeypatch.setattr(module, "get_adapter", lambda _: FakeAdapter())
    lane = daemon.store.get_lane("codex-1")
    for number in (1, 2):
        lane_id = f"claude-{number}"
        daemon.store.put_lane(replace(lane, lane_id=lane_id, provider="claude",
            account_key=f"claude:fixture-{number}", credential=Credential("claude", lane.home, "home")))
        daemon.store.add_reading(Reading(lane_id, "account", "seven_day", .1,
            after(3600), ReadingLabel.PROVIDER, "fixture", utcnow()))
    daemon.desktop_prober = lambda: None
    user = {"selected": {"command": "user-command", "env": {"TOKEN": "${FIXTURE_TOKEN}"}},
            "unselected-user": {"url": "https://unused.example.test"}}
    project = {"selected": {"command": "project-command"},
               "second": {"url": "https://second.example.test"},
               "unselected-project": {"command": "unused-command"}}
    (config_home / ".claude.json").write_text(json.dumps({"mcpServers": user}))
    (harness.workdir / ".mcp.json").write_text(json.dumps({"mcpServers": project}))
    return daemon, harness, project


def submit_args(harness, **overrides):
    return harness.submit_args(sandbox="workspace-write", pinned_model="haiku",
                               in_place=True, **overrides)


def config(daemon, job_id):
    return json.loads((daemon.root / "jobs" / job_id / JOB_CONFIG_NAME).read_text())


def observe_launch(daemon, attempt, monkeypatch, *, resume=False):
    """Inspect the JobSpec crossing the real launch boundary and stop before spawn."""
    seen = []

    class Observed(Exception):
        pass

    class Observe(FakeAdapter):
        def build_launch(self, spec, *args, **kwargs):
            assert not resume, "a resume must use the native continuation"
            seen.append(spec)
            raise Observed()

        def resume_launch(self, spec, *args, **kwargs):
            assert resume, "a fresh attempt must use the normal launch"
            seen.append(spec)
            raise Observed()

    with monkeypatch.context() as patch:
        patch.setattr(module, "get_adapter", lambda _: Observe())
        with pytest.raises(Observed):
            Daemon._launch(daemon, attempt)
    assert daemon._children == {}
    assert len(seen) == 1
    return seen[0]


def test_c12_9_submit_freezes_only_named_entries_and_show_records_opt_in(mcp_daemon):
    daemon, harness, project = mcp_daemon
    args = submit_args(harness, mcp_servers=["second", "selected", "selected"])
    reply = daemon.dispatch("submit", args)
    job_id = reply["job_id"]
    assert config(daemon, job_id) == {"mcpServers": {name: project[name] for name in ("second", "selected")}}
    shown = daemon.dispatch("show", {"job_id": job_id})
    assert json.loads(shown["job"]["mcp_servers"]) == ["second", "selected"]
    assert set(shown["mcp"]) == {"second", "selected"}
    assert shown["mcp"]["selected"] == {"scope": "project", "source": str(harness.workdir / ".mcp.json")}
    # C-6.2 request replay uses its immutable accepted copy after sources vanish.
    (harness.workdir / ".mcp.json").unlink()
    (harness.root / "claude-config" / ".claude.json").unlink()
    assert daemon.dispatch("submit", args) == {**reply, "created": False}
    assert config(daemon, job_id)["mcpServers"]["selected"] == project["selected"]


def test_c12_9_unknown_name_is_refused_before_acceptance(mcp_daemon):
    daemon, harness, _ = mcp_daemon
    with pytest.raises(protocol.ProtocolError, match="unknown MCP server missing.*second.*selected") as error:
        daemon.dispatch("submit", submit_args(harness, mcp_servers=["missing"]))
    assert error.value.code == 2
    assert daemon.store.list_jobs() == []
    assert daemon.store.list_attempts() == []


def test_c12_9_request_digest_binds_explicit_mcp_opt_in(mcp_daemon):
    daemon, harness, _ = mcp_daemon
    args = submit_args(harness, mcp_servers=["selected"])
    daemon.dispatch("submit", args)
    for names in ([], ["second"], ["selected", "second"]):
        with pytest.raises(protocol.ProtocolError, match="different payload"):
            daemon.dispatch("submit", {**args, "mcp_servers": names})
    assert len(daemon.store.list_jobs()) == 1


def test_c12_9_policy_cannot_opt_a_job_into_any_mcp_server(mcp_daemon, monkeypatch):
    daemon, harness, _ = mcp_daemon
    daemon.policy["permissions"]["*"] = "workspace-write"
    # Even a policy carrying such a default cannot supply the job's consent.
    daemon.policy["mcp_servers"] = ["selected", "unselected-user"]
    args = submit_args(harness)
    args["sandbox"] = protocol.POLICY_SANDBOX
    job_id = daemon.dispatch("submit", args)["job_id"]
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert job["sandbox"] == "workspace-write"
    assert json.loads(job["mcp_servers"]) == []
    assert not (daemon.root / "jobs" / job_id / JOB_CONFIG_NAME).exists()
    spec = observe_launch(daemon, daemon.store.list_attempts(job_id)[0], monkeypatch)
    assert spec.mcp_servers == () and spec.mcp_config is None


@pytest.mark.parametrize("sandbox,model,message", [
    ("read-only", "haiku", "read-only job starts no MCP servers"),
    ("workspace-write", "astra", "only a Claude launch starts MCP servers"),
])
def test_c12_9_opt_in_requires_a_writable_claude_job(mcp_daemon, sandbox, model, message):
    daemon, harness, _ = mcp_daemon
    args = submit_args(harness, mcp_servers=["selected"])
    args.update(sandbox=sandbox, pinned_model=model)
    with pytest.raises(AdapterError, match=message):
        daemon.dispatch("submit", args)
    assert daemon.store.list_jobs() == []


def test_c12_9_provider_limit_retry_keeps_opt_in_and_original_entries(mcp_daemon, monkeypatch):
    daemon, harness, project = mcp_daemon
    job_id, first, adir = reserve(daemon, harness, sandbox="workspace-write",
        pinned_model="haiku", in_place=True, mcp_servers=["selected"])
    first_spec = observe_launch(daemon, first, monkeypatch)
    assert first_spec.mcp_servers == ("selected",)

    class Limited(FakeAdapter):
        def classify(self, *args):
            return Outcome(OutcomeClass.LIMITED, "fixture limit", closure=Closure(
                first["lane_id"], "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                ClockSource.REPORTED, "fixture"))

    with monkeypatch.context() as patch:
        patch.setattr(module, "get_adapter", lambda _: Limited())
        daemon._finalize(receipt_fixture(daemon, first, adir, rc=4))
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    (harness.workdir / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "selected": {"command": "replacement-command"}, "added-later": {"command": "unused"}}}))
    daemon.policy["mcp_servers"] = ["added-later"]
    daemon._admit()
    attempts = daemon.store.list_attempts(job_id)
    assert [row["seq"] for row in attempts] == [1, 2]
    assert attempts[0]["lane_id"] != attempts[1]["lane_id"]
    retry_spec = observe_launch(daemon, attempts[1], monkeypatch)
    assert retry_spec.mcp_servers == first_spec.mcp_servers == ("selected",)
    assert retry_spec.mcp_config == first_spec.mcp_config
    assert json.loads(Path(retry_spec.mcp_config).read_text()) == {"mcpServers": {"selected": project["selected"]}}


def test_c12_9_native_resume_keeps_opt_in_after_sources_disappear(mcp_daemon, monkeypatch):
    daemon, harness, project = mcp_daemon
    source_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write",
        pinned_model="haiku", in_place=True, mcp_servers=["selected"])
    daemon.store.update_attempt(attempt["attempt_id"], native_session_id="fixture-native-session")
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    (harness.workdir / ".mcp.json").unlink()
    (harness.root / "claude-config" / ".claude.json").unlink()
    resumed_id = daemon.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source_id))["job_id"]
    assert json.loads(daemon.store.get_job(resumed_id)["mcp_servers"]) == ["selected"]
    assert config(daemon, resumed_id) == {"mcpServers": {"selected": project["selected"]}}
    daemon._admit()
    resumed_spec = observe_launch(daemon, daemon.store.list_attempts(resumed_id)[0], monkeypatch, resume=True)
    assert resumed_spec.mcp_servers == ("selected",)
    assert config(daemon, resumed_id) == json.loads(Path(resumed_spec.mcp_config).read_text())


@pytest.mark.parametrize("names", [["second"], ["selected", "second"]])
def test_c12_9_resume_cannot_change_source_opt_in(mcp_daemon, names):
    daemon, harness, _ = mcp_daemon
    source_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write",
        pinned_model="haiku", in_place=True, mcp_servers=["selected"])
    daemon.store.update_attempt(attempt["attempt_id"], native_session_id="fixture-native-session")
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    with pytest.raises(AdapterError, match="a resume starts its source's MCP servers"):
        daemon.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source_id, mcp_servers=names))
    assert len(daemon.store.list_jobs()) == 1
