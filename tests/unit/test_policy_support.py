"""Minimal routing, credentials, lazy adapters, and retention contract tests."""

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.credentials import resolve_credential
from subfleet.policy import DEFAULT_POLICY_PATH, lane_capacity, load_policy, pick, policy_hash
from subfleet.retention import maintenance
from subfleet.store import Store


NOW = "2026-09-05T12:00:00Z"


def lane(identity="codex-1", **changes):
    base = Lane(identity, "codex", "codex:" + identity, Credential("codex", "/home/" + identity, "home"), "/home/" + identity, LaneOwner.V2, False)
    return replace(base, **changes)


def test_default_policy_exact_models_and_caps():
    """C-11.1, C-6.4: routing data keeps the plan model map and bounded default caps."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert policy["models"]["astra"] == {"provider": "codex", "id": "gpt-6-astra", "effort": "ultra"}
    assert policy["models"]["fable"]["scope"] == "fable"
    assert policy["caps"]["max_active_attempts"] == 4
    assert policy["caps"]["max_in_flight_per_lane"] == 2
    assert policy["caps"]["max_wall_s"] == 21600
    assert policy_hash(DEFAULT_POLICY_PATH) == hashlib.sha256(DEFAULT_POLICY_PATH.read_bytes()).hexdigest()


@pytest.mark.parametrize("key,value", [("max_attempts", 0), ("reading_ttl_s", False), ("max_active_attempts", "4")])
def test_policy_rejects_invalid_caps(tmp_path, key, value):
    """C-11.1, C-6.4: invalid admission caps fail before dispatch."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["caps"][key] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="positive integer"):
        load_policy(path)


def test_minimal_pick_filters_and_pins():
    """C-11.2, C-10.3, C-10.4: ownership, desktop, exclusions, and closures bind a pin."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    lanes = [lane("codex-1", enabled=False), lane("codex-2", owner=LaneOwner.V1),
             lane("codex-3", desktop=True), lane("codex-4"), lane("codex-5"), lane("codex-6")]
    closures = [{"lane_id": "codex-5", "scope": "gpt-6-astra", "until_at": "2026-09-05T13:00:00Z"}]
    decision = pick(policy, lanes, pinned_model="astra", exclusions=["codex-4"], closures=closures, now=NOW)
    assert decision.chosen_lane == "codex-6"
    assert [row["reason"] for row in decision.evaluations[0]["rejections"]] == ["disabled", "owner-v1", "desktop", "excluded", "closed:gpt-6-astra:2026-09-05T13:00:00Z"]
    assert pick(policy, lanes, pinned_model="astra", pinned_lane="codex-3", now=NOW).chosen_lane is None
    assert pick(policy, lanes, pinned_model="astra", pinned_lane="codex-3", allow_desktop=True, now=NOW).chosen_lane == "codex-3"
    with pytest.raises(ValueError, match="different providers"):
        pick(policy, lanes, pinned_model="opus", pinned_lane="codex-6", now=NOW)


def test_unmeasured_stale_lane_and_fleet_caps():
    """C-6.4: one unmeasured slot expands only for fresh provider evidence."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    reading = {"lane_id": "codex-1", "scope": "account", "window": "seven_day", "utilization": .2,
               "label": "provider", "observed_at": "2026-09-05T11:59:00Z", "resets_at": "2026-09-05T13:00:00Z"}
    assert lane_capacity(policy, "codex-1", [], now=NOW) == 1
    assert lane_capacity(policy, "codex-1", [reading], now=NOW) == 2
    assert lane_capacity(policy, "codex-1", [{**reading, "observed_at": "2026-09-05T11:57:00Z"}], now=NOW) == 1
    assert lane_capacity(policy, "codex-1", [{**reading, "label": "admission-observed"}], now=NOW) == 1
    assert lane_capacity(policy, "codex-1", [{**reading, "utilization": None}], now=NOW) == 1
    assert pick(policy, [lane()], pinned_model="astra", in_flight={"codex-1": 1}, now=NOW).chosen_lane is None
    assert pick(policy, [lane()], pinned_model="astra", in_flight={"codex-1": 1}, readings=[reading], now=NOW).chosen_lane == "codex-1"
    assert pick(policy, [lane()], pinned_model="astra", in_flight={"codex-9": 4}, now=NOW).chosen_lane is None


def test_keychain_secret_only_enters_environment(monkeypatch, tmp_path):
    """C-10.5: keychain argv has only the reference; failures disclose no token."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "private-token\n", "")
    monkeypatch.setattr("subfleet.credentials.subprocess.run", run)
    assert resolve_credential(Credential("claude", "test-service", "keychain-token")) == {"CLAUDE_CODE_OAUTH_TOKEN": "private-token"}
    assert calls[0][0] == ["security", "find-generic-password", "-s", "test-service", "-w"]
    assert resolve_credential(Credential("codex", str(tmp_path), "home")) == {"CODEX_HOME": str(tmp_path.resolve())}
    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "private-token", "private-token")
    monkeypatch.setattr("subfleet.credentials.subprocess.run", failed)
    with pytest.raises(AdapterError) as error:
        resolve_credential(Credential("claude", "test-service", "keychain-token"))
    assert "private-token" not in str(error.value)
    assert error.value.__cause__ is None


def test_env_credential_resolves_without_keychain_and_roundtrips_store(monkeypatch, tmp_path):
    """C-10.1, C-10.5: env references persist by name and resolve only into child env."""
    credential = Credential("claude", "SUBFLEET_TEST_OAUTH", "env")
    monkeypatch.setenv(credential.ref, "test-only-oauth-token")
    monkeypatch.setattr("subfleet.credentials.subprocess.run", lambda *args, **kwargs: pytest.fail("env credentials must not call security"))
    assert resolve_credential(credential) == {"CLAUDE_CODE_OAUTH_TOKEN": "test-only-oauth-token"}
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("claude-1", "claude", "claude:fake", credential, None, LaneOwner.V2, False))
        assert store.get_lane("claude-1").credential == credential
        row = store.lane_rows()[0]
        assert row["credential_ref"] == credential.ref
        assert row["credential_kind"] == "env"
        assert "test-only-oauth-token" not in json.dumps(row)


@pytest.mark.parametrize("token", [None, "", "  "])
def test_env_credential_missing_refuses_with_fix(monkeypatch, token):
    """C-10.5, C-17.3: unresolved environment credentials refuse without disclosure."""
    if token is None:
        monkeypatch.delenv("SUBFLEET_TEST_OAUTH", raising=False)
    else:
        monkeypatch.setenv("SUBFLEET_TEST_OAUTH", token)
    monkeypatch.setattr("subfleet.credentials.subprocess.run", lambda *args, **kwargs: pytest.fail("env credentials must not call security"))
    with pytest.raises(AdapterError) as error:
        resolve_credential(Credential("claude", "SUBFLEET_TEST_OAUTH", "env"))
    assert error.value.code == 7
    assert "SUBFLEET_TEST_OAUTH" in error.value.fix
    assert "daemon environment" in error.value.fix


def test_registry_injected_factory_never_imports_provider(monkeypatch):
    """C-12.1: a fake adapter can execute without either real provider module."""
    fake = object()
    monkeypatch.setattr(registry, "_factories", {})
    monkeypatch.setattr(registry.importlib, "import_module", lambda name: pytest.fail("unexpected provider import"))
    registry.register("codex", lambda: fake)
    assert registry.get_adapter("codex") is fake


def add_terminal(store, root, identity, *, state="succeeded", kind="dispatch"):
    store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind=kind,
                  workdir="/work", prompt_path="/prompt", sandbox="read-only", state=state,
                  created_at="2026-09-05T12:00:00Z")
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    (directory / "stdout").write_bytes(b"x" * 10)


def test_retention_preserves_active_unread_quarantine_salvage_gate(tmp_path, monkeypatch):
    """C-8.4, C-3.3: retention pins required evidence, and archives and deletes files outside tx."""
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(lane())
        for identity in ["old", "notice", "quarantine", "salvage", "gate", "active"]:
            add_terminal(store, tmp_path, identity, state="running" if identity == "active" else "succeeded")
        store.add_notice("notice", "unread", "session")
        for identity, state in [("quarantine", "quarantined"), ("salvage", "failed")]:
            store.add_attempt(attempt_id=identity + "/a1", job_id=identity, seq=1, lane_id="codex-1", model_requested="gpt-6-astra", state=state)
        store.add_artifact("salvage/a1", "salvage", "refs/subfleet-salvage/work-a1", "digest", 0)
        store.add_action(action_id="gate-action", kind="gate-merge", op_key="one", subject="gate", request_json=json.dumps({"evidence": "gate"}))
        import subfleet.retention as retention
        for name in ("archive", "delete_archived"):
            original = getattr(retention.archive, name)
            def outside_tx(*args, _original=original, **kwargs):
                assert not store.connection.in_transaction
                return _original(*args, **kwargs)
            monkeypatch.setattr(retention.archive, name, outside_tx)
        result = maintenance(store, tmp_path, max_jobs=0, max_bytes=0)
        # Salvage no longer pins (design rev 2, section 7): its ref is never touched.
        assert result["pruned"] == ["old", "salvage"]
        assert set(result["protected"]) == {"notice", "quarantine", "gate", "active"}
        assert store.get_job("old") is None
        assert not (tmp_path / "jobs" / "old").exists()
        assert (tmp_path / "archive" / "old" / "manifest.json").is_file()
        assert "retention.pruned" in [event["kind"] for event in store.list_events("old")]


def test_retention_count_and_byte_caps_keep_newest(tmp_path):
    """C-8.4: either the job count or the byte budget prunes oldest unpinned jobs."""
    with Store(tmp_path / "state.sqlite3") as store:
        for identity in ["a", "b", "c"]:
            add_terminal(store, tmp_path, identity)
        result = maintenance(store, tmp_path, max_jobs=2, max_bytes=100)
        assert result["pruned"] == ["a"]
        assert result["bytes_after"] == 20
        result = maintenance(store, tmp_path, max_jobs=2, max_bytes=10)
        assert result["pruned"] == ["b"]
        assert [job["job_id"] for job in store.list_jobs()] == ["c"]
