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
    """A missing --main-model cannot turn known identity into an attestation."""
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
    """Known callers reach both durable gate state and the submitted review job."""
    plan = tmp_path / "plan.md"
    plan.write_text("Review this revision.\n")
    result = dispatch(core, "gate.start", {**wire(arguments(plan, peer=peer)), "caller_family": family})
    assert result.get("job_id"), result
    state = core._gate_service._load(result["gate_id"])
    assert state["caller_family"] == family
    assert state["main_family"] == (family or "claude")
    assert core.store.get_job(result["job_id"])["caller_family"] == family


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
            return {"status": "completed", "code": 0}

    assert gate_cli.run(arguments(plan), root=tmp_path / "state", client=Client()) == 0
    assert len(calls) == 1
    assert calls[0][0] == "gate.start"
    assert calls[0][1]["caller_family"] == family
