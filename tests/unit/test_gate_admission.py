"""C-23.2–4, C-23.10: gates use durable job admission and fresh isolation."""
from dataclasses import replace
import json
import logging
from pathlib import Path
import sqlite3
import threading

import pytest

from subfleet import daemon as daemon_module, protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.adapters.isolation import CODEX_DISABLED_FEATURES, MANAGED_ENV, codex_args
from subfleet.contracts import Attestation, Credential, Decision, JobSpec, Lane, LaneOwner, Outcome, OutcomeClass, Sandbox
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.store import Store


def lane(home, provider="codex"):
    return Lane(f"{provider}-1", provider, f"{provider}:test",
                Credential(provider, str(home), "home"), str(home), LaneOwner.V2, False)


@pytest.fixture
def core(tmp_path, monkeypatch):
    """C-23.10: exercise the real admission transaction without starting processes."""
    root = tmp_path / "state"
    (root / "jobs").mkdir(parents=True)
    (root / "neutral").mkdir()
    prompt = root / "prompt.md"
    prompt.write_text("Review the copied input.")
    daemon = object.__new__(Daemon)
    daemon.root, daemon.store = root, Store(root / "state.sqlite3")
    daemon.policy, daemon.policy_digest = load_policy(DEFAULT_POLICY_PATH), "test"
    daemon._submit_lock = threading.Lock()
    daemon._busy_lock, daemon._busy = threading.Lock(), set()
    daemon._pending_launches = set()
    daemon._export_locks = {}
    daemon._exit_settle = {}
    daemon._workspace_deferrals = {}                      # C-6.8: consecutive transient workspace failures
    daemon._reset_admission_state()                       # C-6.10, C-6.11: what admission remembers between passes
    daemon.log = logging.getLogger("subfleet.test.gate-admission")
    daemon._desktop_cache = (0.0, daemon_module._UNSET)   # C-10.3: the identity lane's per-window profile cache
    daemon._notify = lambda: None
    daemon._boundary = lambda *args: None
    daemon._publish = lambda role, path, contents: atomic_publish(path, contents)
    daemon._recover_probes = lambda: None
    daemon._prepare_route = lambda *args: (set(), None)
    chosen = Decision(("astra",), (), "codex-1", "astra", "test", "test")
    daemon._pick = lambda *args, **kwargs: chosen
    daemon._pin_roster = daemon.store.lane_rows          # C-11.2: no timers here, so no probe-reported names
    daemon._route_stands = lambda basis, decision: (None, 0, decision)  # C-6.3: `_pick` is fixed; nothing moves
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(daemon_module.scheduler, "probe_required", lambda *args: False)
    daemon.store.put_lane(lane(root / "home"))
    yield daemon
    daemon.store.close()


def args(core, **changes):
    return protocol.SubmitArgs(
        request_id=changes.pop("request_id", "gate-request"), kind="gate-review",
        workdir=str(core.root / "neutral"), prompt_path=str(core.root / "prompt.md"),
        sandbox="read-only", pinned_model="astra", allow_tmp=True, isolated_review=True,
        review_root=str(core.root / "neutral"), round_lease="gate:sample:round:1", **changes)


def test_round_lease_is_admitted_with_attempt_and_survives_export(core):
    """C-23.10: admission reserves one round; terminal export retains it for consumption."""
    job_id = core.submit(args(core))["job_id"]
    assert core.store.list_leases() == []
    core._admit()
    attempts = core.store.list_attempts(job_id)
    assert len(attempts) == 1
    lease = core.store.one("SELECT * FROM leases WHERE lease_key='gate:sample:round:1'")
    assert lease["holder"] == f"gate-round:{job_id}"
    assert core.store.get_job(job_id)["isolated_review"] == 1
    assert core._spec(core.store.get_job(job_id)).review_root == str(core.root / "neutral")
    core.store.update_job(job_id, state="succeeded", accepted_attempt_id=attempts[0]["attempt_id"])
    core._export(job_id)
    assert core.store.one("SELECT * FROM leases WHERE lease_key='gate:sample:round:1'") == lease
    with pytest.raises(AdapterError, match="reserved peer round"):
        core.submit(args(core, request_id="concurrent"))


def test_concurrent_queued_round_refused_before_admission(core):
    """C-23.10: a second job cannot race the first round's pending admission."""
    core.submit(args(core))
    second = replace(args(core, request_id="second"), round_lease="gate:sample:round:2")
    with pytest.raises(AdapterError, match="reserved peer round"):
        core.submit(second)
    assert len(core.store.list_jobs()) == 1


@pytest.mark.parametrize("changes", [
    {"pinned_model": None, "task": "review"}, {"isolated_review": False},
    {"round_lease": None}, {"round_lease": "gate:../escape:round:1"},
    {"review_root": None}, {"sandbox": "workspace-write"},
])
def test_gate_job_requires_isolation_and_valid_round(core, changes):
    """C-23.2, C-23.10: malformed or writable gate submissions never enter the store."""
    with pytest.raises((AdapterError, protocol.ProtocolError)):
        core.submit(replace(args(core), **changes))
    assert core.store.list_jobs() == []


def test_gate_review_refuses_repository_workdir(core, monkeypatch):
    """C-23.10: a state-root path inside a checkout is not a neutral review cwd."""
    monkeypatch.setattr(daemon_module, "git_head", lambda path, **_: "a" * 40)
    with pytest.raises(AdapterError, match="neutral"):
        core.submit(args(core))


def test_dry_run_does_not_submit_or_reserve(core):
    """C-17.2, C-23.10: a dry-run creates neither a job nor a round lease."""
    assert core.submit(replace(args(core), dry_run=True))["dry_run"]
    assert core.store.list_jobs() == core.store.list_leases() == []


def job(tmp_path, provider="codex"):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review this input.")
    return JobSpec("request", "gate-review", str(tmp_path), str(prompt), None, None,
                   "astra" if provider == "codex" else "fable", None, Sandbox.READ_ONLY,
                   None, "review", isolated_review=True, review_root=str(tmp_path / "source"))


def metadata(servers=()):
    return ({"requirements": None}, {"layers": [{"name": {"type": "user"},
             "config": {"secret": "never-record-this"}}]}, list(servers))


def test_codex_prepares_fresh_lane_isolation_for_each_attempt(tmp_path):
    """C-23.2, C-23.4: lane rotation rechecks metadata and disables each named MCP server."""
    calls = []
    def inspect(binary, **kwargs):
        calls.append(kwargs)
        return metadata([{"name": Path(kwargs["home"]).name, "transport": {"type": "stdio"}}])
    adapter = CodexAdapter()
    adapter.isolation_inspector = inspect
    spec = job(tmp_path)
    for index in (1, 2):
        home = tmp_path / f"home{index}"
        launch = adapter.build_launch(spec, f"job/a{index}", tmp_path / f"a{index}",
                                     lane(home), {}, "gpt-6-astra", "high",
                                     Path(spec.prompt_path), "hooks=ignored")
        assert {"--ephemeral", "--ignore-user-config", "--ignore-rules"} <= set(launch.argv)
        assert "--skip-git-repo-check" in launch.argv
        assert all(f"features.{feature}=false" in launch.argv for feature in CODEX_DISABLED_FEATURES)
        assert f"mcp_servers.home{index}.enabled=false" in launch.argv
        assert "hooks=ignored" not in launch.argv
        assert "--add-dir" not in launch.argv  # Codex's option grants writes.
        assert "never-record-this" not in repr(launch)
    assert [Path(call["home"]).name for call in calls] == ["home1", "home2"]


def test_ephemeral_codex_header_and_requested_model_are_not_attestation(tmp_path):
    """C-23.2, C-23.43: absent ephemeral evidence cannot be replaced by the startup model echo."""
    adapter = CodexAdapter()
    adapter.isolation_inspector = lambda *args, **kwargs: metadata()
    spec = job(tmp_path)
    attempt = tmp_path / "a1"
    launch = adapter.build_launch(spec, "job/a1", attempt, lane(tmp_path / "home"), {},
                                  "gpt-6-astra", "high", Path(spec.prompt_path), None)
    (attempt / "stderr").write_text("model: gpt-6-astra\nsession id: peer-thread\n")
    (attempt / "stream.jsonl").write_text(json.dumps({"type": "thread.started",
                                                      "thread_id": "peer-thread"}) + "\n")
    outcome = Outcome(OutcomeClass.OK, "complete", native_session_id="peer-thread")
    result = adapter.attest(attempt, launch, outcome, "gpt-6-astra")
    assert result.status == Attestation.UNATTESTED and result.served_model is None
    assert "--ephemeral" in result.evidence and "cannot attest" in result.evidence


@pytest.mark.parametrize("source", ["system", "project"])
def test_nonempty_codex_layers_are_named_refusals(tmp_path, source):
    """C-6.5, C-23.3: operator configuration is refused with its layer named, not logged."""
    data = ({"requirements": None}, {"layers": [{"name": {"type": source},
            "config": {"secret": "never-record-this"}}]}, [])
    with pytest.raises(AdapterError, match=source) as error:
        codex_args("codex", home=tmp_path, workdir=tmp_path, env={}, inspector=lambda *a, **k: data)
    assert error.value.code == 7
    assert "never-record-this" not in str(error.value)


def test_managed_codex_requirements_are_refused(tmp_path):
    """C-6.5, C-23.3: managed requirements cannot override review restrictions."""
    data = ({"requirements": {}}, metadata()[1], [])
    with pytest.raises(AdapterError, match="managed requirements"):
        codex_args("codex", home=tmp_path, workdir=tmp_path, env={}, inspector=lambda *a, **k: data)


@pytest.mark.parametrize("name", MANAGED_ENV)
def test_managed_submission_environment_refused(core, monkeypatch, name):
    """C-6.5, C-23.3: inherited managed environment is refused without its value."""
    monkeypatch.setenv(name, "never-record-this")
    with pytest.raises(AdapterError, match=name) as error:
        core.submit(args(core))
    assert error.value.code == 7 and "never-record-this" not in str(error.value)


def test_claude_isolated_tools_and_all_readonly_memory_environment(tmp_path, monkeypatch):
    """C-23.2: Claude peers have only Read/Glob/Grep and no inherited memory sources."""
    monkeypatch.setenv("CLAUDE_COWORK_MEMORY_DYNAMIC", "private-memory")
    spec = job(tmp_path, "claude")
    adapter = ClaudeAdapter()
    launch = adapter.build_launch(spec, "job/a1", tmp_path / "a1", lane(tmp_path, "claude"),
                                  {}, "claude-fable-5", None, Path(spec.prompt_path), None)
    assert launch.argv[launch.argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert launch.argv[launch.argv.index("--add-dir") + 1] == spec.review_root
    assert "CLAUDE_COWORK_MEMORY_DYNAMIC" in launch.env_remove
    assert "CLAUDE_MEMORY_STORES" in launch.env_remove
    ordinary = adapter.build_launch(replace(spec, isolated_review=False), "job/a2", tmp_path / "a2",
                                     lane(tmp_path, "claude"), {}, "claude-fable-5", None,
                                     Path(spec.prompt_path), None)
    assert "CLAUDE_COWORK_MEMORY_DYNAMIC" in ordinary.env_remove


def test_existing_schema_is_upgraded_without_rewriting_job_data(tmp_path):
    """C-3.1, C-23.10: isolation columns migrate additively for existing job stores."""
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        store.add_job(job_id="old", request_id="old", payload_digest="original",
                      kind="dispatch", workdir="/source", prompt_path="/prompt", sandbox="read-only")
    with sqlite3.connect(path) as db:
        for name in ("isolated_review", "review_root", "round_lease"):
            db.execute(f"ALTER TABLE jobs DROP COLUMN {name}")
        db.execute("DELETE FROM schema_version")
        db.execute("INSERT INTO schema_version VALUES (2,'2026-01-01T00:00:00Z')")
    with Store(path) as migrated:
        old = migrated.get_job("old")
        assert old["payload_digest"] == "original"
        assert old["isolated_review"] == 0 and old["round_lease"] is None
