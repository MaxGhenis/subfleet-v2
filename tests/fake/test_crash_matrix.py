"""Crash injection at persisted boundaries proves durable daemon ownership."""

import json
from pathlib import Path
import signal
import subprocess

import pytest


@pytest.mark.parametrize("boundary", ["reserved", "starting", "running", "finalizing",
                                     "terminal", "notice", "export", "salvage"])
def test_c20_3_crash_matrix_recovers_without_duplicate_acceptance(daemon, boundary):
    """C-20.3, C-4.2, C-4.3, C-8.3, C-13.1, C-15.1 SIGKILL at each boundary recovers once."""
    flags = ["--crash-at", boundary]
    if boundary == "starting":
        flags += ["--start-delay", ".3"]
    daemon.start(*flags)
    options = {"out_path": str(daemon.root / "export.md")}
    if boundary == "salvage":
        subprocess.run(["git", "init", "-b", "feature/fake"], cwd=daemon.workdir,
                       check=True, capture_output=True)
        (daemon.workdir / "tracked.txt").write_text("baseline\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=daemon.workdir,
                       check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Fake", "-c", "user.email=fake@example.test",
                        "commit", "-m", "baseline"], cwd=daemon.workdir,
                       check=True, capture_output=True)
        options.update(sandbox="workspace-write", in_place=True, no_preamble=True)
    job_id = daemon.submit("slow", delay_s=.2, **options)
    if boundary == "salvage":
        daemon.attempt_state(job_id, "running")
        (daemon.workdir / "tracked.txt").write_text("changed result\n")
    daemon.process.wait(timeout=5)
    assert daemon.process.returncode == -signal.SIGKILL
    marker = json.loads((daemon.root / f"hook-{boundary}.json").read_text())
    assert marker["job_id"] == job_id
    if boundary in {"terminal", "notice", "export"}:
        assert daemon.job(job_id)["state"] == "succeeded"
        assert len(daemon.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
    daemon.start()
    job = daemon.finished(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0
    attempts = daemon.attempts(job_id)
    assert len(attempts) == (2 if boundary == "reserved" else 1)
    if boundary == "reserved":
        assert attempts[0]["state"] == "failed"
        assert attempts[0]["outcome_detail"] == "reserved-no-launch"
    assert len([row for row in attempts if row["state"] == "succeeded"]) == 1
    assert len(daemon.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
    deliverable = daemon.rows(
        "SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'",
        (job["accepted_attempt_id"],),
    )
    assert len(deliverable) == 1
    assert Path(job["out_path"]).read_bytes() == Path(deliverable[0]["path"]).read_bytes()
    if boundary == "salvage":
        salvage = daemon.rows("SELECT * FROM artifacts WHERE attempt_id=? AND role='salvage'",
                              (job["accepted_attempt_id"],))
        assert len(salvage) == 1
        refs = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage"],
                              cwd=daemon.workdir, check=True, capture_output=True, text=True)
        assert len(refs.stdout.splitlines()) == 1


def test_c4_3_c15_1_sigkill_after_terminal_cannot_lose_notice(daemon):
    """C-4.3, C-15.1 a SIGKILL immediately after terminal state leaves its notice committed."""
    daemon.start("--crash-at", "terminal")
    job_id = daemon.submit()
    daemon.process.wait(timeout=5)
    assert daemon.process.returncode == -signal.SIGKILL
    assert daemon.job(job_id)["state"] == "succeeded"
    assert len(daemon.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
    daemon.start()
    notices = daemon.call("notice.pending", session_id="fake-session")
    assert job_id in json.dumps(notices)
    assert len(daemon.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
