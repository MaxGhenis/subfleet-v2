"""Milestone 1 through the real CLI, daemon, guardian, and Codex adapter."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import pytest


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "success"


def job_id(result):
    value = result.stdout.strip()
    assert re.fullmatch(r"\d{8}-\d{6}-[a-z0-9-]+", value), result
    return value


def running(e2e, identity):
    def observed():
        attempts = e2e.attempts(identity)
        return attempts[0] if attempts and attempts[0]["state"] == "running" else None
    return e2e.until(observed)


def git(e2e, workdir, *argv):
    result = subprocess.run(["git", "-C", str(workdir), *argv], env=e2e.env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_success_exports_by_rename_and_exposes_notices_and_artifacts(e2e):
    """C-6.7, C-8.1–C-8.3, C-12.5, C-15.1, C-17.1, C-17.4: one accepted CLI deliverable."""
    old_contents = b"previous published deliverable\n"
    e2e.out.write_bytes(old_contents)
    e2e.start()
    with e2e.out.open("rb") as previous:
        old_inode = os.fstat(previous.fileno()).st_ino
        submitted = e2e.cli(*e2e.run_args("astra", "-o", e2e.out, "--wait"))
        assert submitted.rc == 0, submitted
        identity = job_id(submitted)
        expected = (FIXTURE / "last.md").read_bytes()
        assert e2e.out.read_bytes() == expected
        assert e2e.out.stat().st_ino != old_inode
        assert previous.read() == old_contents

    notice, = e2e.rows("SELECT * FROM notices WHERE job_id=?", (identity,))
    assert notice["session_id"] == e2e.env["CLAUDE_CODE_SESSION_ID"]
    # C-15.3, C-23.50: `run --wait` printed the job's end to the session that
    # ran it, so it acknowledged that session's notice; no hook and no push
    # (C-15.7) tells the session again.
    assert notice["state"] == "acknowledged"
    assert identity in notice["text"] and "ok" in notice["text"]
    assert str(e2e.out) in notice["text"]
    listed = e2e.cli("runs")
    assert listed.rc == 0 and identity in listed.stdout, listed
    shown = e2e.show(identity)
    assert shown["job"]["state"] == "succeeded"
    assert Path(shown["job"]["prompt_path"]).read_bytes() == e2e.prompt.read_bytes()
    attempt, = shown["attempts"]
    assert attempt["attempt_id"] == shown["job"]["accepted_attempt_id"]
    assert attempt["outcome_class"] == "ok" and attempt["rc"] == 0
    # This replay has no Codex rollout; it must expose uncertainty honestly.
    assert attempt["attestation"] == "unattested"
    artifacts = {artifact["role"]: artifact for artifact in shown["artifacts"]}
    assert {"deliverable", "stdout", "stderr", "raw-stream", "launch", "export", "prompt-sent"} <= artifacts.keys()
    deliverable = Path(artifacts["deliverable"]["path"])
    assert deliverable.read_bytes() == expected == e2e.out.read_bytes()
    assert artifacts["deliverable"]["bytes"] == len(expected)
    assert artifacts["deliverable"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert Path(artifacts["raw-stream"]["path"]).read_bytes() == (FIXTURE / "stdout").read_bytes()
    launch = json.loads(Path(artifacts["launch"]["path"]).read_text())
    assert launch["stdin_path"] == artifacts["prompt-sent"]["path"]
    assert Path(launch["stdin_path"]).read_bytes() == e2e.prompt.read_bytes()
    assert launch["argv"][:3] == ["codex", "exec", "--json"]
    assert launch["argv"][launch["argv"].index("-m") + 1] == "gpt-6-astra"
    output = e2e.cli("runs", "show", identity, "--out")
    assert output.rc == 0, output
    assert output.stdout.encode() == expected.rstrip(b"\n") + b"\n"

    operations = [json.loads(line) for line in (e2e.root / "publication.jsonl").read_text().splitlines()]
    for published in (deliverable, e2e.out):
        index, rename = next((index, row) for index, row in enumerate(operations)
                             if row["op"] == "rename" and Path(row["target"]) == published)
        assert any(row["op"] == "fsync" and row["ino"] == rename["ino"]
                   and not row["directory"] for row in operations[:index])
        assert any(row["op"] == "fsync" and row["ino"] == rename["dir_ino"]
                   and row["directory"] for row in operations[index + 1:])


def test_detached_cli_exit_does_not_end_provider_work(e2e):
    """C-5.1, C-5.2, C-15.4: the CLI process exits while the guardian keeps working."""
    e2e.start(scenario="slow", delay_s=2)
    submitted = e2e.cli(*e2e.run_args("astra", "-d"))
    assert submitted.rc == 0, submitted
    identity = job_id(submitted)
    running(e2e, identity)
    waited = e2e.cli("wait", identity, "--timeout", "10")
    assert waited.rc == 0, waited
    assert e2e.job(identity)["state"] == "succeeded"
    assert len(e2e.attempts(identity)) == 1
    assert len(e2e.rows("SELECT * FROM notices WHERE job_id=?", (identity,))) == 1


def test_kill_slow_job_interrupts_attempt_and_records_owner(e2e):
    """C-5.6, C-7.1, C-7.2, C-17.3: kill --wait interrupts and returns cancellation."""
    e2e.start(scenario="slow", delay_s=30)
    submitted = e2e.cli(*e2e.run_args("astra", "-d"))
    assert submitted.rc == 0, submitted
    identity = job_id(submitted)
    running(e2e, identity)
    e2e.until((e2e.root / "codex-env.json").exists)
    killed = e2e.cli("kill", identity, "--wait", timeout=25)
    assert killed.rc == 130, killed
    assert e2e.job(identity)["state"] == "cancelled"
    attempt, = e2e.attempts(identity)
    assert attempt["state"] == "interrupted"
    assert attempt["killed_by"] == "operator"
    assert attempt["signal"] in (15, 9)
    assert not e2e.rows("SELECT * FROM leases WHERE holder IN (?,?)", (identity, attempt["attempt_id"]))


def test_kill_dirty_allocated_worktree_publishes_salvage(e2e):
    """C-5.6, C-6.6, C-13.1: cancel preserves the allocated worktree in a private ref."""
    baseline = git(e2e, e2e.workdir, "rev-parse", "HEAD")
    e2e.start(scenario="slow", delay_s=30, env={"SUBFLEET_FAKE_DIRTY": "1"})
    submitted = e2e.cli(*e2e.run_args("astra", "-s", "workspace-write", "-d"))
    assert submitted.rc == 0, submitted
    identity = job_id(submitted)
    running(e2e, identity)
    worktree = Path(e2e.job(identity)["worktree"])
    assert worktree == e2e.root / "worktrees" / identity
    dirty = worktree / "fake-dirty.txt"
    e2e.until(dirty.exists)
    index = Path(git(e2e, worktree, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    original_index = index.read_bytes()
    killed = e2e.cli("kill", identity, "--wait", timeout=25)
    assert killed.rc == 130, killed
    attempt, = e2e.attempts(identity)
    assert attempt["state"] == "interrupted" and attempt["killed_by"] == "operator"
    saved, = e2e.rows("SELECT * FROM artifacts WHERE attempt_id=? AND role='salvage'", (attempt["attempt_id"],))
    ref = saved["path"]
    assert ref.startswith("refs/subfleet-salvage/")
    assert git(e2e, worktree, "rev-parse", ref + "^") == baseline
    assert git(e2e, worktree, "show", ref + ":fake-dirty.txt") == "unfinished provider work"
    assert git(e2e, worktree, "rev-parse", "HEAD") == baseline
    assert index.read_bytes() == original_index
    assert dirty.read_text() == "unfinished provider work\n"
    assert not (e2e.workdir / "fake-dirty.txt").exists()
    assert not e2e.rows("SELECT * FROM leases WHERE holder IN (?,?)", (identity, attempt["attempt_id"]))


@pytest.mark.parametrize("flag", ["--dry-run", "--why"])
def test_decision_preview_dispatches_nothing(e2e, flag):
    """C-11.5, C-17.2: --dry-run and --why print decisions without creating jobs."""
    e2e.start()
    result = e2e.cli(*e2e.run_args("astra", flag))
    assert result.rc == 0, result
    assert "astra" in result.stdout and "codex-1" in result.stdout
    assert not e2e.rows("SELECT * FROM jobs")
    assert not e2e.rows("SELECT * FROM attempts")
    assert not e2e.rows("SELECT * FROM leases")
    assert not (e2e.root / "codex-env.json").exists()


def test_tmp_workdir_without_allow_flag_is_refused(e2e):
    """C-2.4, C-6.1, C-6.5, C-17.3: /tmp admission requires --allow-tmp."""
    e2e.start()
    args = e2e.run_args("astra", "--wait")
    args.remove("--allow-tmp")
    refused = e2e.cli(*args)
    assert refused.rc == 7, refused
    assert "/tmp" in refused.stderr and "--allow-tmp" in refused.stderr
    assert not e2e.rows("SELECT * FROM jobs")
    assert not e2e.rows("SELECT * FROM attempts")


def test_request_id_reuses_job_and_conflicting_payload_exits_two(e2e):
    """C-6.2, C-16.3, C-17.3: a repeated request returns its job; changed bytes refuse."""
    e2e.start()
    request_id = str(uuid.uuid4())
    args = e2e.run_args("astra", "--request-id", request_id, "--wait")
    first = e2e.cli(*args)
    assert first.rc == 0, first
    identity = job_id(first)
    second = e2e.cli(*args)
    assert second.rc == 0 and job_id(second) == identity, second
    e2e.prompt.write_text("A different payload.\n")
    conflict = e2e.cli(*args)
    assert conflict.rc == 2, conflict
    assert "request" in conflict.stderr.lower()
    assert len(e2e.rows("SELECT * FROM jobs")) == 1
    assert len(e2e.attempts(identity)) == 1
    assert len(e2e.rows("SELECT * FROM notices WHERE job_id=?", (identity,))) == 1


def test_offline_reads_after_daemon_stop_and_run_names_start_command(e2e):
    """C-17.1, C-17.3, C-17.5: stopped-daemon reads work and submissions exit 69."""
    e2e.start()
    submitted = e2e.cli(*e2e.run_args("astra", "--wait"))
    assert submitted.rc == 0, submitted
    identity = job_id(submitted)
    stopped = e2e.cli("daemon", "stop", timeout=20)
    assert stopped.rc == 0, stopped
    e2e.process.wait(timeout=3)
    assert not (e2e.root / "daemon.sock").exists()
    listed = e2e.cli("runs")
    assert listed.rc == 0 and identity in listed.stdout, listed
    assert "offline" in listed.stderr.lower()
    shown = e2e.cli("runs", "show", identity)
    assert shown.rc == 0 and identity in shown.stdout, shown
    output = e2e.cli("runs", "show", identity, "--out")
    assert output.rc == 0, output
    assert output.stdout.encode() == (FIXTURE / "last.md").read_bytes().rstrip(b"\n") + b"\n"
    status = e2e.cli("status")
    assert status.rc == 0 and "codex-1" in status.stdout, status
    refused = e2e.cli(*e2e.run_args("astra", "--wait"))
    assert refused.rc == 69, refused
    assert "subfleet daemon start" in refused.stderr
