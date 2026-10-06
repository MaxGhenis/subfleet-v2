"""Named core daemon acceptance tests; no real provider is invoked."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

import pytest

from tests import waits


def test_c6_2_request_id_is_idempotent_and_digest_conflicts_are_code_2(daemon):
    """C-6.2 equal request digests reuse a job; different digests return code 2."""
    daemon.start()
    args = daemon.submit_args()
    first = daemon.call("submit", **args)
    again = daemon.call("submit", **args)
    assert first["created"] is True
    assert again == {"job_id": first["job_id"], "request_id": args["request_id"], "created": False}
    Path(args["prompt_path"]).write_text("a different prompt")
    conflict = daemon.request("submit", **args)
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == 2
    assert len(daemon.rows("SELECT * FROM jobs")) == 1
    daemon.finished(first["job_id"])


def test_c6_3_concurrent_submits_take_exactly_one_unmeasured_lane_slot(daemon):
    """C-6.3, C-6.4 concurrent one-slot admission leaves the other job at capacity."""
    daemon.start()
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait()
        return daemon.submit("slow", delay_s=.75)

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = list(workers.map(lambda _: submit(), range(2)))
    daemon.until(lambda: len(daemon.rows("SELECT * FROM attempts")) == 1
                 and any(daemon.job(job)["wait_reason"] == "capacity" for job in jobs))
    assert len(daemon.rows("SELECT * FROM attempts")) == 1
    assert len(daemon.rows("SELECT * FROM leases WHERE lease_key LIKE 'lane:%'")) == 1
    waiting = [daemon.job(job) for job in jobs if daemon.job(job)["state"] == "waiting"]
    assert len(waiting) == 1 and waiting[0]["wait_reason"] == "capacity"
    for job in jobs:
        daemon.call("kill", job_id=job)
    for job in jobs:
        daemon.finished(job)


def test_c5_2_job_survives_submitting_client_exit(daemon):
    """C-5.2 the guardian outlives the submitting socket client's process."""
    daemon.start()
    args = daemon.submit_args("slow", delay_s=.45)
    script = (
        "import json,socket,sys; s=socket.socket(socket.AF_UNIX); "
        "s.connect(sys.argv[1]); s.sendall((sys.argv[2]+'\\n').encode()); "
        "print(s.makefile('rb').readline().decode().strip())"
    )
    submitted = subprocess.run(
        [sys.executable, "-c", script, str(daemon.root / "daemon.sock"),
         json.dumps({"v": 1, "id": "departing-client", "op": "submit", "args": args})],
        capture_output=True, text=True, check=True,
    )
    reply = json.loads(submitted.stdout)
    assert reply["ok"]
    job_id = reply["result"]["job_id"]
    daemon.attempt_state(job_id, "running")
    finished = daemon.finished(job_id)
    assert finished["state"] == "succeeded" and finished["rc"] == 0
    assert len(daemon.attempts(job_id)) == 1


def test_c4_2_running_daemon_sigkill_readopts_and_preserves_rc(daemon):
    """C-4.2 running, C-5.2 restarting a killed daemon re-adopts the same attempt."""
    daemon.start()
    job_id = daemon.submit("slow", delay_s=.65)
    running = daemon.attempt_state(job_id, "running")
    daemon.crash()
    daemon.start()
    finished = daemon.finished(job_id)
    assert finished["state"] == "succeeded" and finished["rc"] == 0
    attempts = daemon.attempts(job_id)
    assert len(attempts) == 1
    assert attempts[0]["guardian_pid"] == running["guardian_pid"]
    assert attempts[0]["rc"] == 0


@pytest.mark.parametrize("missing_start", [False, True], ids=["late-receipt", "no-receipt"])
def test_c4_2_starting_recovers_late_or_missing_receipt(daemon, missing_start):
    """C-4.2 starting adopts a late receipt, or releases and retries after verified empty."""
    flags = ["--crash-at", "starting", "--start-delay", ".45"]
    if missing_start:
        flags.append("--missing-start")
    daemon.start(*flags)
    job_id = daemon.submit("slow", delay_s=.2)
    waits.wait_process(daemon.process, 3, watch=daemon.daemon_tree)
    assert daemon.process.returncode == -signal.SIGKILL
    first = daemon.attempts(job_id)[0]
    assert first["state"] == "starting"
    assert not (daemon.root / "jobs" / job_id / "a1" / "start.json").exists()
    daemon.start()
    finished = daemon.finished(job_id)
    assert finished["state"] == "succeeded" and finished["rc"] == 0
    attempts = daemon.attempts(job_id)
    assert len(attempts) == (2 if missing_start else 1)
    if missing_start:
        assert attempts[0]["state"] == "failed"
        assert attempts[0]["outcome_class"] == "unknown"
        assert not daemon.rows("SELECT * FROM leases WHERE holder=?", (first["attempt_id"],))


def test_c5_6_kill_escalates_ignored_sigterm_and_records_killed_by(daemon):
    """C-5.6 ignored SIGTERM escalates to SIGKILL, verifies emptiness, and records ownership."""
    daemon.start()
    job_id = daemon.submit("ignore-sigterm")
    attempt = daemon.attempt_state(job_id, "running")
    stdout = daemon.root / "jobs" / job_id / "a1" / "stdout"
    daemon.until(lambda: stdout.exists() and "ready" in stdout.read_text())
    daemon.call("kill", job_id=job_id)
    assert daemon.finished(job_id)["state"] == "cancelled"
    killed = daemon.attempts(job_id)[0]
    assert killed["state"] == "interrupted"
    assert killed["killed_by"]
    assert killed["signal"] == signal.SIGKILL
    assert not daemon.rows("SELECT * FROM leases WHERE holder IN (?,?)",
                           (job_id, attempt["attempt_id"]))


def test_c5_6_c13_1_writable_kill_salvages_dirty_workspace_after_verified_containment(daemon):
    """C-5.6, C-13.1 kill verifies containment and salvages dirty writable work without changing HEAD or index."""
    from subfleet.procs import containment

    def git(*args):
        return subprocess.run(["git", *args], cwd=daemon.workdir, capture_output=True,
                              text=True, check=True).stdout.strip()

    git("init", "-b", "feature/kill-salvage")
    tracked = daemon.workdir / "tracked.txt"
    tracked.write_text("baseline\n")
    git("add", "tracked.txt")
    git("-c", "user.name=Fake", "-c", "user.email=fake@example.test", "commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")
    index = daemon.workdir / ".git" / "index"
    original_index = index.read_bytes()
    daemon.start()
    job_id = daemon.submit("ignore-sigterm", sandbox="workspace-write", in_place=True)
    daemon.attempt_state(job_id, "running")
    stdout = daemon.root / "jobs" / job_id / "a1" / "stdout"
    daemon.until(lambda: stdout.exists() and "ready: ignore-sigterm" in stdout.read_text())
    tracked.write_text("retained tracked change\n")
    untracked = daemon.workdir / "untracked.txt"
    untracked.write_text("retained untracked change\n")
    daemon.call("kill", job_id=job_id)
    assert daemon.finished(job_id)["state"] == "cancelled"
    attempt = daemon.attempts(job_id)[0]
    assert attempt["state"] == "interrupted" and attempt["killed_by"]
    assert containment(attempt["pgid"], attempt["guardian_pid"], attempt["child_pid"],
                       attempt["attempt_id"]).verified_empty
    saved = daemon.rows("SELECT * FROM artifacts WHERE attempt_id=? AND role='salvage'",
                         (attempt["attempt_id"],))
    assert len(saved) == 1
    ref = saved[0]["path"]
    assert ref.startswith("refs/subfleet-salvage/")
    assert git("rev-parse", ref + "^") == baseline
    assert git("show", ref + ":tracked.txt") == "retained tracked change"
    assert git("show", ref + ":untracked.txt") == "retained untracked change"
    assert git("rev-parse", "HEAD") == baseline
    assert index.read_bytes() == original_index
    assert tracked.read_text() == "retained tracked change\n"
    assert untracked.read_text() == "retained untracked change\n"
    assert not daemon.rows("SELECT * FROM leases WHERE holder IN (?,?)", (job_id, attempt["attempt_id"]))


def test_c5_5_nested_setsid_quarantines_and_force_release_records_override(daemon):
    """C-5.5, C-5.6, C-5.7 escaped writers quarantine; force-release preserves override evidence."""
    daemon.start()
    job_id = daemon.submit("nested-setsid", out_path=str(daemon.root / "export.md"))
    daemon.attempt_state(job_id, "running")
    marker = daemon.root / "escaped.pid"
    daemon.until(lambda: marker.exists() and marker.read_text())
    escaped = int(marker.read_text())
    daemon.call("kill", job_id=job_id)
    attempt = daemon.attempt_state(job_id, "quarantined")
    assert attempt["quarantine_reason"]
    assert daemon.rows("SELECT * FROM leases WHERE lease_key=?",
                       (f"out:{daemon.root / 'export.md'}",))
    shown = daemon.call("show", job_id=job_id)
    assert str(escaped) in json.dumps(shown)
    os.kill(escaped, 0)
    note = "test operator accepts escaped-fixture responsibility"
    daemon.call("kill", job_id=job_id, force_release=True, operator_note=note)
    daemon.until(lambda: not daemon.rows("SELECT * FROM leases WHERE holder IN (?,?)",
                                        (job_id, attempt["attempt_id"])))
    events = daemon.rows("SELECT * FROM events WHERE job_id=?", (job_id,))
    assert note in json.dumps(events)


def test_c7_2_cancel_before_acceptance_beats_provider_success(daemon):
    """C-7.2 cancel committed before acceptance keeps an rc-0 attempt interrupted."""
    daemon.start("--hold-at", "finalizing")
    job_id = daemon.submit()
    daemon.until(lambda: (daemon.root / "hook-finalizing.json").exists())
    daemon.call("kill", job_id=job_id)
    (daemon.root / "release-hook").touch()
    assert daemon.finished(job_id)["state"] == "cancelled"
    attempt = daemon.attempts(job_id)[0]
    assert attempt["state"] == "interrupted" and attempt["rc"] == 0
    assert daemon.rows("SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'",
                       (attempt["attempt_id"],))


def test_c7_2_acceptance_before_cancel_reports_already_finished(daemon):
    """C-7.2 acceptance committed first makes a later kill succeed as already finished."""
    daemon.start()
    job_id = daemon.submit()
    assert daemon.finished(job_id)["state"] == "succeeded"
    result = daemon.call("kill", job_id=job_id)
    assert "already finished" in json.dumps(result).lower()
    assert daemon.job(job_id)["state"] == "succeeded"


def test_c7_3_parent_cancel_propagates_except_independent_children(daemon):
    """C-7.3 cancelling a parent cancels dependent children and preserves independent children."""
    daemon.start()
    parent = daemon.submit("slow", delay_s=.7)
    daemon.attempt_state(parent, "running")
    dependent = daemon.submit(parent_job_id=parent)
    independent = daemon.submit(parent_job_id=parent, independent=True)
    daemon.call("kill", job_id=parent)
    assert daemon.job(dependent)["cancel_requested_at"] == daemon.job(parent)["cancel_requested_at"]
    assert daemon.finished(parent)["state"] == "cancelled"
    assert daemon.finished(dependent)["state"] == "cancelled"
    assert daemon.finished(independent)["state"] == "succeeded"


def test_c8_1_deliverable_and_export_use_temp_fsync_rename(daemon):
    """C-8.1, C-8.2, C-8.3 deliverable and export sync the file before rename and directory after."""
    daemon.start("--publication-audit")
    exported = daemon.root / "export.md"
    job_id = daemon.submit(out_path=str(exported))
    assert daemon.finished(job_id)["state"] == "succeeded"
    deliverable = daemon.root / "jobs" / job_id / "a1" / "deliverable.md"
    assert exported.read_bytes() == deliverable.read_bytes()
    operations = [json.loads(line) for line in (daemon.root / "publication.jsonl").read_text().splitlines()]
    for published in (deliverable, exported):
        index, rename = next((index, row) for index, row in enumerate(operations)
                             if row["op"] == "rename" and Path(row["target"]) == published)
        assert any(row["op"] == "fsync" and row["ino"] == rename["ino"]
                   and not row["directory"] for row in operations[:index])
        assert any(row["op"] == "fsync" and row["ino"] == rename["dir_ino"]
                   and row["directory"] for row in operations[index + 1:])
        assert published.stat().st_mode & 0o777 == 0o600


def test_c8_3_unwritable_export_keeps_success_and_records_error(daemon):
    """C-8.3 an unwritable export preserves the successful job and records export_error."""
    daemon.start("--hold-at", "finalizing")
    destination = daemon.root / "destination"
    destination.mkdir()
    job_id = daemon.submit(out_path=str(destination / "out.md"))
    daemon.until(lambda: (daemon.root / "hook-finalizing.json").exists())
    destination.chmod(0o500)
    try:
        (daemon.root / "release-hook").touch()
        finished = daemon.finished(job_id)
    finally:
        destination.chmod(0o700)
    assert finished["state"] == "succeeded"
    assert finished["export_error"]
    assert not (destination / "out.md").exists()
    assert "export" in json.dumps(daemon.rows("SELECT * FROM events WHERE job_id=?", (job_id,)))


def test_c5_8_second_daemon_exits_69_and_stale_lock_is_taken_over(daemon):
    """C-5.8 a live singleton rejects the second daemon with 69; a stale lock can be acquired."""
    daemon.start()
    duplicate = waits.run(
        [sys.executable, "-m", "tests.fake.run_daemon", "--state-root", str(daemon.root)],
        capture_output=True, text=True, timeout=3,
    )
    assert duplicate.returncode == 69, duplicate.stderr
    daemon.crash()
    daemon.start()
    assert daemon.call("daemon.status")


def test_c16_1_malformed_line_keeps_connection_handler_alive(daemon):
    """C-16.1 malformed JSON returns code 2 and the same connection accepts the next request."""
    daemon.start()
    with daemon.connect() as client:
        client.sendall(b"not-json\n")
        error = json.loads(waits.recv_line(client, 5, watch=daemon.daemon_tree))
        assert error["ok"] is False and error["error"]["code"] == 2
        client.sendall(b'{"v":1,"id":"after-error","op":"daemon.status","args":{}}\n')
        response = json.loads(waits.recv_line(client, 5, watch=daemon.daemon_tree))
        assert response["ok"] is True and response["id"] == "after-error"


@pytest.mark.parametrize("scenario,rc", [("ok", 0), ("rc1-crash-after-output", 1),
                                        ("rc4-limit-with-clock", 4), ("spawn-fail", 127)])
def test_c5_2_provider_receipts_preserve_raw_return_codes(daemon, scenario, rc):
    """C-5.2, C-9.2, C-9.4 fake success, crash, reported limit, and spawn failure keep raw rc."""
    daemon.start()
    job_id = daemon.submit(scenario, max_attempts=1)
    finished = daemon.finished(job_id)
    assert finished["rc"] == rc
    attempt = daemon.attempts(job_id)[0]
    assert attempt["rc"] == rc
    receipt = json.loads((daemon.root / "jobs" / job_id / "a1" / "exit.json").read_text())
    assert receipt["rc"] == rc
    if rc == 4:
        closure = daemon.rows("SELECT * FROM closures")[0]
        assert closure["clock_source"] == "reported"
        assert closure["scope"] == "account"
    if rc == 127:
        assert receipt["spawn_error"]
