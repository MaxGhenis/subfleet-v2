"""Review #195 follow-ups: interrupted finalization never accepts live evidence."""

from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from subfleet import daemon as module
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, ExitInfo, Outcome, OutcomeClass
from subfleet.daemon import Daemon
from tests.conftest import LANE_EMAIL, LANE_IDENTITY
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.frozen_streams import frozen_identity, make_frozen


def stage_provider(daemon, harness, name, out):
    lane = daemon.store.get_lane("codex-1")
    if name == "claude":
        lane = replace(lane, lane_id="claude-frozen", provider="claude", account_key="claude:fixture",
                       credential=Credential("claude", lane.credential.ref, "home"),
                       identity=LANE_IDENTITY, label=LANE_EMAIL)
        (Path(lane.credential.ref) / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "fixture-home-token"}}))
        daemon.store.put_lane(lane)
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1, out_path=str(out),
                                  pinned_lane=lane.lane_id, pinned_model="haiku" if name == "claude" else "astra")
    adapter = ClaudeAdapter(projects_dir=adir / "projects") if name == "claude" else CodexAdapter()
    spec = daemon._spec(daemon.store.get_job(job_id))
    launch = adapter.build_launch(spec, attempt["attempt_id"], adir, lane, {},
                                  attempt["model_requested"], None, Path(spec.prompt_path), None)
    saved = asdict(launch)
    saved.pop("env_add")
    (adir / "launch.json").write_text(json.dumps(saved))
    events = ([{"type": "result", "is_error": False, "result": "Finished."}] if name == "claude" else [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Finished."}},
        {"type": "turn.completed"},
    ])
    finalizing = receipt_fixture(daemon, attempt, adir, stdout=(
        "".join(json.dumps(event) + "\n" for event in events).encode()))
    return job_id, attempt, adir, adapter, launch, finalizing


def replay(daemon, harness, attempt, finalizing, restart, monkeypatch):
    if restart:
        daemon.close()
        successor = Daemon(harness.root)
        monkeypatch.setattr(successor, "_launch", lambda *args: pytest.fail("recovery must not launch"))
        try:
            successor._process_attempt(attempt["attempt_id"])
        except BaseException:
            successor.close()
            raise
        return successor
    daemon._finalize(finalizing)
    return daemon


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("checkpoint", ("finalization", "deliverable"))
@pytest.mark.parametrize("kind", ("empty", "directory", "fifo", "symlink", "dangling-symlink"))
def test_claude_frozen_stream_survives_interrupted_finalization(
        state_daemon, monkeypatch, restart, checkpoint, kind):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous output\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, "claude", out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    make_frozen(adir / "stream.jsonl", kind)
    identity = frozen_identity(adir / "stream.jsonl")
    if checkpoint == "deliverable":
        # Interrupted older finalization can have an ok verdict but no captured
        # deliverable yet. Extraction must still honor the frozen path.
        verdict = Outcome(OutcomeClass.OK, "cached success", native_session_id=launch.native_session_id)
        attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
        (adir / "finalization.json").write_text(json.dumps({
            "outcome": asdict(verdict), "attestation": asdict(attest)}))
    def crash(role, path):
        if role == checkpoint:
            raise RuntimeError("interrupted finalization")
    daemon.publish_hook = crash
    with pytest.raises(RuntimeError, match="interrupted finalization"):
        daemon._finalize(finalizing)
    daemon.publish_hook = None
    frozen_verdict = (adir / "finalization.json").read_bytes() if checkpoint == "deliverable" else None
    (adir / "stdout").write_text('{"type":"result","is_error":false,"result":"Later summary."}\n')
    successor = None
    try:
        active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
        successor = active if restart else None
        job = active.store.get_job(job_id)
        assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
        row = active.store.get_attempt(attempt["attempt_id"])
        assert row["outcome_class"] == "unknown"
        assert (adir / "deliverable.md").read_bytes() == b""
        assert frozen_identity(adir / "stream.jsonl") == identity
        if frozen_verdict is not None:
            assert (adir / "finalization.json").read_bytes() == frozen_verdict
        assert out.read_bytes() == b"previous output\n"
        assert not active.store.query("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                      (attempt["attempt_id"],))
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
@pytest.mark.parametrize("name,contents,expected", [
    ("codex", b" \n\t\r\v\f", "unknown"),
    ("claude", b" \n\t\r\v\f", "unknown"),
    ("claude", "\u2003\u00a0".encode(), "unknown"),
    # The fresh Codex check is on bytes, where these are nonblank; preserve it.
    ("codex", "\u2003\u00a0".encode(), "ok"),
    ("codex", b" \n Finished. \t", "ok"),
    ("claude", b" \n Finished. \t", "ok"),
])
def test_cached_ok_uses_fresh_deliverable_content_boundary(
        state_daemon, monkeypatch, restart, name, contents, expected):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous output\n")
    job_id, attempt, adir, adapter, launch, finalizing = stage_provider(daemon, harness, name, out)
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    verdict = adapter.classify(adir, launch, ExitInfo(0, None, .1, None, None))
    assert verdict.cls == OutcomeClass.OK
    attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
    (adir / "finalization.json").write_text(json.dumps({
        "outcome": asdict(verdict), "attestation": asdict(attest), "rule": module.FINALIZATION_RULE}))
    (adir / "deliverable.md").write_bytes(contents)
    captured = {name: (adir / name).read_bytes() for name in ("deliverable.md", "finalization.json", "exit.json")}
    salvage = daemon._salvage
    monkeypatch.setattr(daemon, "_salvage", lambda *args: (_ for _ in ()).throw(RuntimeError("interrupted salvage")))
    with pytest.raises(RuntimeError, match="interrupted salvage"):
        daemon._finalize(finalizing)
    monkeypatch.setattr(daemon, "_salvage", salvage)
    monkeypatch.setattr(adapter, "classify", lambda *args: pytest.fail("cached classification repeated"))
    monkeypatch.setattr(adapter, "deliverable", lambda *args: pytest.fail("captured deliverable extracted again"))
    successor = None
    try:
        active = replay(daemon, harness, attempt, finalizing, restart, monkeypatch)
        successor = active if restart else None
        job = active.store.get_job(job_id)
        row = active.store.get_attempt(attempt["attempt_id"])
        assert row["outcome_class"] == expected
        assert {name: (adir / name).read_bytes() for name in captured} == captured
        if expected == "unknown":
            assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
            assert row["outcome_detail"] == "empty deliverable with rc 0"
            assert json.loads(row["evidence_json"])["provider_verdict"]["class"] == "ok"
            assert out.read_bytes() == b"previous output\n"
            assert active.store.get_lane(attempt["lane_id"]).enabled
            assert not active.store.query("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                          (attempt["attempt_id"],))
        else:
            assert (job["state"], job["accepted_attempt_id"]) == ("succeeded", attempt["attempt_id"])
            assert out.read_bytes() == contents
    finally:
        if successor is not None:
            successor.close()
