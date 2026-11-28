"""Review gates use recorded author identity before complementary attestation."""
from __future__ import annotations

import pytest

from subfleet import cli
from subfleet.gate import cli as gate_cli
from subfleet.gate.service import dispatch
from tests.fake.test_gate_end_to_end import arguments, wire
from tests.unit.test_gate_admission import core


@pytest.mark.parametrize("family,peer", [("claude", "fable"), ("codex", "astra")])
def test_recorded_caller_family_refuses_same_family_peer(core, tmp_path, family, peer):
    """C-6.24: a missing --main-model cannot turn known identity into an attestation."""
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    before = core.store.list_events()
    result = dispatch(core, "gate.start", {**wire(arguments(plan, peer=peer)), "caller_family": family})
    assert result["code"] == 2
    assert "different model families" in result["message"]
    assert core.store.list_jobs() == []
    assert core.store.list_events() == before


@pytest.mark.parametrize("family", [True, ["claude"], "other"])
def test_invalid_gate_caller_family_is_a_reported_input_error(core, tmp_path, family):
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    result = dispatch(core, "gate.start", {**wire(arguments(plan)), "caller_family": family})
    assert result["code"] == 2
    assert "caller_family" in result["message"]
    assert core.store.list_jobs() == []


@pytest.mark.parametrize("family,peer", [("claude", "astra"), ("codex", "fable"), (None, "astra")])
def test_recorded_gate_family_is_retained_on_peer_job(core, tmp_path, family, peer):
    """C-6.24: known callers reach durable gate state and the submitted peer job."""
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    result = dispatch(core, "gate.start", {**wire(arguments(plan, peer=peer)), "caller_family": family})
    assert result.get("job_id"), result
    state = core._gate_service._load(result["gate_id"])
    assert state["caller_family"] == family
    assert state["main_family"] == (family or "claude")
    assert core.store.get_job(result["job_id"])["caller_family"] == family


@pytest.mark.parametrize("peer,live", [
    pytest.param("sol62", False, id="explicit"),
    pytest.param("sol", False, id="shipped-alias"),
    pytest.param("sol", True, id="live-alias"),
])
def test_claude_gate_submits_sol62(core, tmp_path, peer, live):
    """C-6.24, C-11.1: explicit Sol 6.2 and the policy alias pin the Claude author's peer."""
    if live:
        core.policy["retired"]["sol"] = "sol62"
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    payload = {**wire(arguments(plan)), "peer": peer, "caller_family": "claude"}
    result = dispatch(core, "gate.start", payload)
    assert result.get("job_id"), result
    state = core._gate_service._load(result["gate_id"])
    assert state["peer"] == "sol62"
    assert state["rounds"][0]["requested_model"] == "gpt-6.2-sol"
    job = core.store.get_job(result["job_id"])
    assert job["pinned_model"] == "sol62"
    assert job["caller_family"] == "claude"


@pytest.mark.parametrize("live", [False, True], ids=["shipped", "live"])
def test_daemon_preview_resolves_retired_sol(core, tmp_path, live):
    """C-6.24, C-11.1: a daemon preview follows its policy's retired model map."""
    if live:
        core.policy["retired"]["sol"] = "sol62"
    plan = tmp_path / "plan.md"
    plan.write_text("Preview this revision.\n")
    before = core.store.list_events()
    result = dispatch(core, "gate.start", wire(arguments(plan, "--dry-run", peer="sol")))
    assert result["code"] == 0 and result["peer"] == "sol62", result
    assert core.store.list_jobs() == []
    assert core.store.list_events() == before
    assert not (core.root / "gates").exists()


@pytest.mark.parametrize("peer", ["missing-peer", "sol61"])
def test_daemon_preview_refuses_unsupported_peer(core, tmp_path, peer):
    """C-6.24: a wire preview refuses both unknown and unsupported policy models."""
    plan = tmp_path / "plan.md"
    plan.write_text("Preview this revision.\n")
    before = core.store.list_events()
    payload = {**wire(arguments(plan, "--dry-run")), "peer": peer}
    result = dispatch(core, "gate.start", payload)
    assert result["code"] == 2, result
    assert all(choice in result["message"] for choice in ("fable", "sol62", "astra"))
    assert core.store.list_jobs() == []
    assert core.store.list_events() == before
    assert not (core.root / "gates").exists()


def test_gate_refuses_missing_sol62_policy_model(core, tmp_path):
    """C-6.24, C-11.1: a missing peer model fails before submission or journaling."""
    del core.policy["models"]["sol62"]
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    before = core.store.list_events()
    payload = {**wire(arguments(plan)), "peer": "sol62", "caller_family": "claude"}
    result = dispatch(core, "gate.start", payload)
    assert result["code"] == 2
    assert core.store.list_jobs() == []
    assert core.store.list_events() == before
    assert not (core.root / "gates").exists()
    assert "peer: unknown model 'sol62'" in result["message"]


@pytest.mark.parametrize("main_model", ["astra", "missing-model"])
def test_explicit_main_model_still_validates_recorded_family(core, tmp_path, main_model):
    """Explicit model identity cannot disagree with the known author's family."""
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    result = dispatch(core, "gate.start", {
        **wire(arguments(plan, "--main-model", main_model)), "caller_family": "claude"})
    assert result["code"] == 2
    assert ("recorded caller family" if main_model == "astra" else "unknown --main-model") in result["message"]
    assert core.store.list_jobs() == []


@pytest.mark.parametrize("family", ["claude", "codex", None])
def test_gate_cli_detects_and_sends_caller_family(tmp_path, monkeypatch, family):
    """The gate client uses the run client's identity detector at submission."""
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    monkeypatch.setattr(cli, "caller_family", lambda: family)
    calls = []

    class Client:
        def call(self, op, payload, **kwargs):
            calls.append((op, payload))
            if op == "capabilities":
                return {"capabilities": ["review.family.v1"]}
            return {"status": "completed", "code": 0}

    assert gate_cli.run(arguments(plan), root=tmp_path / "state", client=Client()) == 0
    assert [op for op, _ in calls] == ["capabilities", "gate.start"]
    assert calls[1][1]["caller_family"] == family
