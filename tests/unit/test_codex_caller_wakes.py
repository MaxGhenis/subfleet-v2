"""C-17.6, C-24.10: a qualified Codex caller stays with its own provider."""

import uuid

import pytest

from subfleet.conversations import wakes
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import conversation, svc  # noqa: F401


NATIVE = "00000000-0000-4000-8000-00000000ab17"


def add_run(svc, job_id, caller, **extra):
    svc.daemon.store.add_job(
        job_id=job_id, request_id=job_id, payload_digest="fixture", kind="dispatch",
        caller_session=caller, state="succeeded", workdir=svc.test_workspace,
        prompt_path="fixture.md", sandbox="read-only", **extra)


def test_bound_codex_caller_does_not_resolve_to_same_named_claude_session(svc):
    """C-24.10: wake and suggestion caller lookup preserve Codex's qualifier."""
    claude = conversation(svc, provider="claude", native_session_id=NATIVE)
    codex = conversation(svc, provider="codex", native_session_id=NATIVE)
    assert svc.bound_session(NATIVE)["conversation_id"] == claude  # legacy lookup
    assert svc.bound_session(f"codex:{NATIVE}")["conversation_id"] == codex
    origin, _, session, _ = svc._suggestion_caller({"session_id": f"codex:{NATIVE}"})
    assert origin["conversation_id"] == codex
    assert session == f"codex:{NATIVE}"


def test_qualified_codex_caller_cannot_bind_only_claude_conversation(svc):
    """C-24.10: equal native UUIDs do not make a qualified caller Claude's."""
    conversation(svc, provider="claude", native_session_id=NATIVE)
    assert svc.bound_session(f"codex:{NATIVE}") is None


@pytest.mark.parametrize("provider,caller", [
    ("claude", NATIVE), ("codex", NATIVE), ("codex", f"codex:{NATIVE}"),
    ("codex", f"codex:{NATIVE.upper()}"),
])
def test_wake_run_ownership_accepts_native_legacy_and_qualified_codex_callers(svc, provider, caller):
    """C-24.10: the new namespace retains each provider's historical raw callers."""
    cid = conversation(svc, provider=provider, native_session_id=NATIVE)
    add_run(svc, "owned", caller)
    request_id = str(uuid.uuid4())
    spec = wakes.normalize(runs=["owned"], now=svc.wakes.now())
    assert svc.wakes.register(cid, request_id, spec)["request_id"] == request_id


def test_wake_run_ownership_refuses_qualified_codex_job_for_claude(svc):
    """C-24.10: a Claude conversation cannot claim a qualified Codex caller's run."""
    cid = conversation(svc, provider="claude", native_session_id=NATIVE)
    add_run(svc, "codex-run", f"codex:{NATIVE}")
    spec = wakes.normalize(runs=["codex-run"], now=svc.wakes.now())
    with pytest.raises(ConversationError, match="not this conversation's"):
        svc.wakes.register(cid, str(uuid.uuid4()), spec)


def test_wake_request_uses_qualified_bound_codex_session(svc):
    """C-24.10: conversation.wake resolves the id the Codex CLI caller supplies."""
    conversation(svc, provider="claude", native_session_id=NATIVE)
    cid = conversation(svc, provider="codex", native_session_id=NATIVE)
    add_run(svc, "codex-run", f"codex:{NATIVE}")
    request_id = str(uuid.uuid4())
    result = svc.op_conversation_wake(
        {"session_id": f"codex:{NATIVE}", "request_id": request_id, "runs": ["codex-run"]}, None)
    assert result["request_id"] == request_id
    assert svc.store.one("SELECT conversation_id FROM wake_requests")["conversation_id"] == cid


def test_completion_discovery_keeps_qualified_codex_run_with_its_provider(svc):
    """C-24.10: automatic notices cannot attach to the same UUID under Claude."""
    codex = conversation(svc, provider="codex", native_session_id=NATIVE)
    claude = conversation(svc, provider="claude", native_session_id=NATIVE)
    add_run(svc, "codex-run", f"codex:{NATIVE}")
    for cid in (codex, claude):
        svc.daemon.store.add_job(
            job_id=f"turn-{cid}", request_id=f"turn-{cid}", payload_digest="fixture", kind="turn",
            name=f"turn-{cid}", state="succeeded", created_at=svc.store.conversation(cid)["created_at"],
            workdir=svc.test_workspace, prompt_path="fixture.md", sandbox="read-only")
    completions = svc.wakes._completions()
    assert [run["job_id"] for run in completions[codex]] == ["codex-run"]
    assert claude not in completions


def test_conversation_runs_includes_qualified_codex_and_preserves_raw_callers(svc):
    """C-24.10: the runs view finds qualified callers only for Codex's binding."""
    claude = conversation(svc, provider="claude", native_session_id=NATIVE)
    codex = conversation(svc, provider="codex", native_session_id=NATIVE)
    add_run(svc, "raw-run", NATIVE)
    add_run(svc, "codex-run", f"codex:{NATIVE}")
    def listed(cid):
        return {run["job_id"] for run in svc.op_conversation_runs({"conversation_id": cid}, None)["runs"]}
    assert listed(codex) == {"raw-run", "codex-run"}
    assert listed(claude) == {"raw-run"}
