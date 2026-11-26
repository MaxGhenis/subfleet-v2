"""Review-family replays, handoff attribution and offline show (C-6.2, C-6.5, C-6.23)."""

import pytest

from subfleet import cli, daemon, protocol
from subfleet.adapters.base import AdapterError
from subfleet.client import DaemonError
from subfleet.offline import Offline
from subfleet.sessions import revive
from tests import sessions_fixtures as fx
from tests.fake.test_state_contract import state_daemon  # noqa: F401


def _clear_caller_markers(monkeypatch):
    for marker in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "SUBFLEET_SESSION_ID",
                   "SUBFLEET_CALLER_FAMILY", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        monkeypatch.delenv(marker, raising=False)


@pytest.mark.parametrize("pin,reason", [
    ("model", "review-same-family"),
    ("lane", "review-same-family"),
    ("consent", "same-family-consent"),
])
def test_review_request_replay_uses_the_accepted_job(state_daemon, pin, reason):
    service, harness = state_daemon
    request = harness.submit_args(task="review", tier="hard", caller_family="claude")
    if pin == "lane":
        request.update(pinned_model=None, pinned_lane="codex-1", pinned_provider="codex")
    if pin == "consent":
        parent = service.dispatch("submit", harness.submit_args())["job_id"]
        request.update(caller_family="codex", allow_same_family=True)
        changed_attribution = {"parent_job_id": parent}
    else:
        changed_attribution = {"caller_family": "codex", "caller_session_kind": "codex"}

    first = service.dispatch("submit", request)
    replay = {**request, **changed_attribution}
    again = service.dispatch("submit", replay)
    assert (again["job_id"], again["created"]) == (first["job_id"], False)

    with pytest.raises(protocol.ProtocolError) as conflict:
        service.dispatch("submit", {**replay, "tier": "standard"})
    assert conflict.value.reason == "different-payload"
    with pytest.raises(AdapterError) as refused:
        service.dispatch("submit", {**replay, "request_id": "synthetic-new-request"})
    assert refused.value.reason == reason


@pytest.mark.parametrize("cold", [False, True], ids=["direct", "cold-continue"])
def test_native_codex_handoffs_keep_writer_fanout(state_daemon, monkeypatch, capsys, cold):
    service, harness = state_daemon
    _clear_caller_markers(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-thread")
    monkeypatch.setenv("SUBFLEET_HOME", str(harness.root))
    monkeypatch.setattr(daemon, "git_head", lambda *args, **kwargs: "a" * 40)
    home = fx.claude_home(harness.root, monkeypatch)
    candidates = []
    for index in (1, 2):
        session_id = f"00000000-0000-4000-8000-{index:012d}"
        target = harness.root / f"target-{index}"
        target.mkdir()
        entries = [{**entry, "cwd": str(target)} for entry in fx.interrupted()]
        fx.transcript(home, session_id, entries, cwd=str(target))
        candidates.append(revive.Candidate(session_id, cwd=str(target)))
    monkeypatch.setattr(revive, "cold_candidates", lambda *args, **kwargs: candidates)
    capability_calls = []

    class InProcessClient:
        def call(self, op, payload, **kwargs):
            if op == "capabilities":
                capability_calls.append(op)
                return {"capabilities": ["review.subject.v1", "review.family.v1"]}
            assert op == "sessions" and payload["action"] == "state"
            return {"conversation_sessions": [], "lane_sessions": []}

        def call_settled(self, op, payload, **kwargs):
            try:
                # The handoff's staged brief and both targets live in the test's temp directory.
                return service.dispatch(op, {**payload, "allow_tmp": True})
            except AdapterError as exc:
                raise DaemonError(exc.code, *protocol.refusal_text(exc), reason=exc.reason) from exc

    monkeypatch.setattr(cli, "_client", lambda _: InProcessClient())
    flags = ["--task", "build", "--to", "astra"]
    if cold:
        assert cli.main(["sessions", "continue", "--scope", "cold", "--handoff", *flags]) == 0
    else:
        for candidate in candidates:
            assert cli.main(["handoff", candidate.session_id, "-C", candidate.cwd, *flags]) == 0
    jobs = service.store.list_jobs()
    assert len(jobs) == 2, capsys.readouterr().err
    assert len(capability_calls) == (1 if cold else 2)
    assert {row["workdir"] for row in jobs} == {item.cwd for item in candidates}
    for job in jobs:
        assert job["sandbox"] == "workspace-write"
        assert job["caller_session"] == "synthetic-thread"
        assert service.dispatch("show", {"job_id": job["job_id"]})["caller_session_kind"] == "codex"


@pytest.mark.parametrize("argv", [
    ["handoff", "00000000-0000-4000-8000-000000000001", "--to", "opus"],
    ["sessions", "continue", "--scope", "cold", "--handoff", "--to", "opus"],
], ids=["direct", "cold-continue"])
@pytest.mark.parametrize("task,marker,value", [
    ("build", "CODEX_THREAD_ID", "synthetic-thread"),
    ("review", "SUBFLEET_CALLER_FAMILY", "codex"),
], ids=["native-kind", "attributed-review"])
def test_handoff_requires_family_support_before_session_reads(
        tmp_path, monkeypatch, capsys, argv, task, marker, value):
    _clear_caller_markers(monkeypatch)
    monkeypatch.setenv(marker, value)
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    calls = []

    class SubjectOnlyDaemon:
        def call(self, op, payload):
            calls.append(op)
            if op == "capabilities":
                return {"capabilities": ["review.subject.v1"]}
            return {"conversation_sessions": [], "lane_sessions": []}

    monkeypatch.setattr(cli, "_client", lambda _: SubjectOnlyDaemon())
    assert cli.main([*argv, "--task", task]) == 69
    assert calls == ["capabilities"]
    assert "review.family.v1" in capsys.readouterr().err


def test_offline_show_keeps_the_online_review_attribution(state_daemon):
    service, harness = state_daemon
    reply = service.dispatch("submit", harness.submit_args(
        task="review", caller_family="codex", pinned_model="astra", allow_same_family=True,
        caller_session="synthetic-claude-session", caller_session_kind="claude"))
    job_id = reply["job_id"]
    online = service.dispatch("show", {"job_id": job_id})
    offline = Offline(harness.root).show_job(job_id)

    assert offline["caller_family"] == online["job"]["caller_family"] == "codex"
    assert offline["allow_same_family"] is online["job"]["allow_same_family"] is True
    assert offline["caller_session_kind"] == online["caller_session_kind"] == "claude"
    assert offline["same_family_consent"] == online["same_family_consent"] == {
        "session": "synthetic-claude-session", "session_kind": "claude"}
    assert offline["caller_family_disagreement"] == online["caller_family_disagreement"] == {
        "declared": "codex", "session_kind": "claude", "writer_check": "applied"}
