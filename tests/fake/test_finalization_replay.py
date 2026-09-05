"""Replay checks using durable rows and files, without provider processes."""
import errno
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from tests.fake.test_state_contract import state_daemon, reserve, receipt_fixture
from tests.fake_adapter import FakeAdapter


@pytest.mark.parametrize("provider,model,scenario", [
    ("codex", "astra", "success"), ("claude", "haiku", "success-allowed"),
])
def test_c8_2_real_adapter_artifacts_survive_launch_reload(state_daemon, monkeypatch, provider, model, scenario):
    """C-8.2, C-12.2: real adapter streams and sent prompts publish after launch reload."""
    from subfleet import daemon as module
    from subfleet.adapters.claude import ClaudeAdapter
    from subfleet.adapters.codex import CodexAdapter
    from subfleet.contracts import Credential

    daemon, harness = state_daemon
    lane = daemon.store.get_lane("codex-1")
    lane = replace(lane, lane_id=f"{provider}-real", provider=provider, account_key=f"{provider}:fixture",
                   credential=Credential(provider, lane.credential.ref, "home"))
    daemon.store.put_lane(lane)
    job_id, attempt, adir = reserve(daemon, harness, pinned_model=model, pinned_lane=lane.lane_id)
    adapter = CodexAdapter() if provider == "codex" else ClaudeAdapter(projects_dir=adir / "projects")
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    spec = daemon._spec(daemon.store.get_job(job_id))
    launch = adapter.build_launch(spec, attempt["attempt_id"], adir, lane, {},
                                  attempt["model_requested"], None, Path(spec.prompt_path), None)
    saved = asdict(launch)
    saved.pop("env_add")
    (adir / "launch.json").write_text(json.dumps(saved))
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / provider / scenario
    finalizing = receipt_fixture(daemon, attempt, adir, stdout=(fixture / "stdout").read_bytes())
    assert daemon._saved_launch(attempt).notes == launch.notes
    daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    artifacts = {row["role"]: row for row in daemon.store.list_artifacts(attempt["attempt_id"])}
    assert {"deliverable", "stdout", "stderr", "raw-stream", "launch"} <= artifacts.keys()
    assert Path(artifacts["raw-stream"]["path"]).read_bytes() == (fixture / "stdout").read_bytes()
    if provider == "claude":
        assert artifacts["prompt-sent"]["path"] == launch.stdin_path
        readings = daemon.store.query("SELECT * FROM readings")
        assert len(readings) == 2
        assert {row["attempt_id"] for row in readings} == {attempt["attempt_id"]}


def test_c4_2_classification_and_attestation_are_frozen_before_acceptance(state_daemon, monkeypatch):
    """C-4.2 finalizing, C-8.2 replay reuses classification, attestation and captured deliverable."""
    daemon, harness = state_daemon
    job_id, a, adir = reserve(daemon, harness)
    finalizing = receipt_fixture(daemon, a, adir)
    notice = daemon._notice
    def interrupted(*args):
        raise RuntimeError("acceptance interrupted")
    monkeypatch.setattr(daemon, "_notice", interrupted)
    with pytest.raises(RuntimeError, match="acceptance interrupted"):
        daemon._finalize(finalizing)
    assert (adir / "finalization.json").is_file()
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    def reread(*args):
        raise AssertionError("completed adapter step was repeated")
    monkeypatch.setattr(FakeAdapter, "classify", reread)
    monkeypatch.setattr(FakeAdapter, "attest", reread)
    monkeypatch.setattr(FakeAdapter, "deliverable", reread)
    (adir / "stdout").write_bytes(b"later external append\n")
    monkeypatch.setattr(daemon, "_notice", notice)
    daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    assert (adir / "deliverable.md").read_bytes() == b"fixture result\n"
    assert len(daemon.store.list_notices()) == 1


def test_c4_3_stale_attempt_cannot_publish_or_accept(state_daemon):
    """C-4.3 an older sequence cannot run publication steps or accept over the current attempt."""
    daemon, harness = state_daemon
    job_id, old, old_dir = reserve(daemon, harness)
    old = receipt_fixture(daemon, old, old_dir)
    current_id = job_id + "/a2"
    daemon.store.add_attempt(attempt_id=current_id, job_id=job_id, seq=2,
        lane_id=old["lane_id"], model_requested=old["model_requested"], state="finalizing")
    daemon._finalize(old)
    assert not (old_dir / "deliverable.md").exists()
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    assert daemon.store.list_artifacts(old["attempt_id"]) == []
    assert daemon.store.list_notices() == []


def test_c20_3_finalization_metadata_disk_full_is_recoverable(state_daemon):
    """C-20.3, C-4.2 ENOSPC while freezing adapter evidence cannot commit partial acceptance."""
    daemon, harness = state_daemon
    job_id, a, adir = reserve(daemon, harness)
    a = receipt_fixture(daemon, a, adir)
    def full(role, path):
        if role == "finalization":
            raise OSError(errno.ENOSPC, "injected metadata write failure")
    daemon.publish_hook = full
    with pytest.raises(OSError) as error:
        daemon._finalize(a)
    assert error.value.errno == errno.ENOSPC
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "finalizing"
    assert daemon.store.list_notices() == []
    daemon.publish_hook = None
    daemon._finalize(a)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"


def test_c14_2_guard_refusal_reaches_cli_wait_with_fix(state_daemon, monkeypatch):
    """C-14.2, C-17.3: a prelaunch refusal retains code 7 and its actionable fix."""
    from subfleet import daemon as module, protocol
    from subfleet.adapters.codex import CodexAdapter
    from subfleet.cli import _wait_summary, exit_for_job

    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)
    monkeypatch.setattr(module, "get_adapter", lambda _: CodexAdapter())
    detail = "Guard preflight refused: SHA-256 does not match TRUST; fix: restore guard files and rerun subfleet doctor"
    daemon._launch_failure(attempt, detail, rc=7)
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    payload, = daemon.wait(protocol.WaitArgs(job_ids=[job_id], deadline_s=0))["jobs"]
    assert exit_for_job(payload) == 7
    assert detail in _wait_summary(payload)


@pytest.mark.parametrize("classification,pinned,expected", [
    ("limited", True, 4), ("limited", False, 3),
    ("auth-dead", True, 5), ("cli-too-old", True, 6),
])
def test_c17_3_provider_rc_and_cli_outcome_have_distinct_codes(state_daemon, monkeypatch, classification, pinned, expected):
    """C-9.2, C-17.3: terminal CLI codes preserve the raw provider rc on the attempt."""
    from subfleet import daemon as module, protocol
    from subfleet.cli import exit_for_job
    from subfleet.contracts import Outcome, OutcomeClass

    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1,
                                    pinned_lane="codex-1" if pinned else None)
    class ClassifiedAdapter(FakeAdapter):
        def classify(self, *args):
            return Outcome(OutcomeClass(classification), "provider fixture outcome")
    monkeypatch.setattr(module, "get_adapter", lambda _: ClassifiedAdapter())
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=1))
    assert daemon.store.get_attempt(attempt["attempt_id"])["rc"] == 1
    payload, = daemon.wait(protocol.WaitArgs(job_ids=[job_id], deadline_s=0))["jobs"]
    assert exit_for_job(payload) == expected
