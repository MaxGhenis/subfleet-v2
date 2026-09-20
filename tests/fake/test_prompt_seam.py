"""Original submission bytes and real adapter inputs, with no spawned processes."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from subfleet import daemon as module
from subfleet import ids
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, HEADLESS_MARKER
from tests.fake.test_daemon_launch import launch_state


def prepare(launch_state, monkeypatch, model, sandbox):
    daemon, harness, calls = launch_state
    lane = daemon.store.get_lane("codex-1")
    if model == "haiku":
        lane = replace(lane, lane_id="claude-1", provider="claude",
                       account_key="claude:fixture",
                       credential=Credential("claude", lane.credential.ref, "home"))
        daemon.store.put_lane(lane)
    adapter = ClaudeAdapter() if model == "haiku" else CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(module, "validate_writable_workdir", lambda _, **__: None)
    head = "fixture-baseline" if sandbox == "workspace-write" else None
    monkeypatch.setattr(module, "git_head", lambda _, **__: head)
    monkeypatch.setattr(daemon, "_workspace", lambda job: (str(harness.workdir), head, None))
    monkeypatch.setattr(daemon, "_guard_override", lambda *args: "hooks={}")
    return daemon, harness, calls, lane


@pytest.mark.parametrize("model", ["astra", "haiku"])
@pytest.mark.parametrize("sandbox,no_preamble", [
    ("read-only", False), ("workspace-write", False), ("workspace-write", True),
])
def test_c6_7_original_digest_and_actual_adapter_prompt(launch_state, monkeypatch, model, sandbox, no_preamble):
    """C-6.2, C-6.7: exact caller bytes are hashed/stored; preparation changes only sent text."""
    daemon, harness, calls, lane = prepare(launch_state, monkeypatch, model, sandbox)
    args = harness.submit_args(pinned_model=model, sandbox=sandbox,
                               no_preamble=no_preamble, in_place=True)
    original = "Caller prompt café.  \n\n".encode()
    Path(args["prompt_path"]).write_bytes(original)
    job_id = daemon.dispatch("submit", args)["job_id"]
    job = daemon.store.get_job(job_id)
    original_path = Path(job["prompt_path"])
    assert original_path.read_bytes() == original
    assert job["payload_digest"] == ids.payload_digest(
        original, workdir=job["workdir"], workdir_head=job["workdir_head"],
        pinned_model=model, sandbox=sandbox, policy_hash=job["policy_hash"],
    )
    manifest = json.loads(original_path.with_name("manifest.json").read_text())
    prepared = original
    if sandbox == "workspace-write" and not no_preamble:
        prepared = module.WRITE_PREAMBLE.encode() + original
        assert Path(manifest["prepared_prompt_path"]).read_bytes() == prepared
    else:
        assert "prepared_prompt_path" not in manifest
        assert not original_path.with_name("prompt.prepared.md").exists()

    daemon._admit()
    attempt, = daemon.store.list_attempts(job_id)
    daemon._launch(attempt)
    launch = daemon._saved_launch(attempt)
    sent_path = Path(launch.stdin_path)
    assert sent_path.name == "prompt.sent.md"
    assert sent_path.parent.name == "a1"
    sent = sent_path.read_bytes()
    if lane.provider == "claude":
        assert sent.count(HEADLESS_MARKER.encode()) == 1
        assert sent.endswith(prepared)
    else:
        assert sent == prepared
    assert original_path.read_bytes() == original
    command, = [argv for argv, _ in calls]
    assert command[command.index("--stdin-path") + 1] == str(sent_path)
    again = daemon.dispatch("submit", args)
    assert again["job_id"] == job_id and again["created"] is False


@pytest.mark.parametrize("no_preamble", [False, True])
def test_c6_7_retry_keeps_prepared_write_prompt(launch_state, monkeypatch, no_preamble):
    """C-6.7, C-13.3: retry checkpoints extend the prepared input and preserve original bytes."""
    daemon, harness, _, _ = prepare(launch_state, monkeypatch, "astra", "workspace-write")
    args = harness.submit_args(sandbox="workspace-write", no_preamble=no_preamble, in_place=True)
    original = b"Continue useful work.\n"
    Path(args["prompt_path"]).write_bytes(original)
    job_id = daemon.dispatch("submit", args)["job_id"]
    daemon._admit()
    first, = daemon.store.list_attempts(job_id)
    daemon._launch(first)
    prepared = original if no_preamble else module.WRITE_PREAMBLE.encode() + original
    assert Path(daemon._saved_launch(first).stdin_path).read_bytes() == prepared
    # This is a launch-construction fixture: model a completed transient attempt
    # without executing a provider or asserting physical process recovery.
    with daemon.store.transaction("fixture.retry", job_id=job_id) as tx:
        tx.execute("UPDATE attempts SET state='failed',outcome_class='transient' WHERE attempt_id=?",
                   (first["attempt_id"],))
        tx.execute("DELETE FROM leases WHERE holder=?", (first["attempt_id"],))
        tx.execute("UPDATE jobs SET state='queued' WHERE job_id=?", (job_id,))
    daemon._admit()
    _, second = daemon.store.list_attempts(job_id)
    daemon._launch(second)
    sent = Path(daemon._saved_launch(second).stdin_path).read_bytes()
    assert sent.startswith(prepared)
    assert sent.count(b"Continue from checkpoint fixture-baseline") == 1
    assert Path(daemon.store.get_job(job_id)["prompt_path"]).read_bytes() == original
