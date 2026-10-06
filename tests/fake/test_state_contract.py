"""Deterministic daemon state tests with no spawned provider or process inspection.

Process identities are stubbed only for constructing the daemon's test fixture;
these tests do not claim to verify guardian survival or operating-system signals.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon, DaemonUnavailable
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Outcome, OutcomeClass
from subfleet.procs import Containment, ProcessIdentity, ProcessTable
from tests.caps import capped
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter


@pytest.fixture
def state_daemon(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
    monkeypatch.setattr(daemon_module.procs, "cwd_pids", lambda workdir: frozenset())
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root)
    def refuse_real_launch(*args):
        raise AssertionError("state-only fixtures must never launch a guardian")
    monkeypatch.setattr(daemon, "_launch", refuse_real_launch)
    try:
        yield daemon, harness
    finally:
        daemon.close()
    harness.check_notices()                 # C-15.1, after every in-process test too


@pytest.fixture
def dead_guardian(monkeypatch):
    """C-5.12: model an absent guardian for the explicit loss scenarios."""
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: ProcessTable({}, boot_id="unit-test-boot"))
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "dead")


def test_c6_2_state_submission_deduplicates_and_rejects_digest_conflicts(state_daemon):
    """C-6.2 committed daemon submission reuses a request id only for an equal payload digest."""
    daemon, harness = state_daemon
    args = harness.submit_args()
    first = daemon.dispatch("submit", args)
    second = daemon.dispatch("submit", args)
    assert first["created"] is True and second["created"] is False
    assert first["job_id"] == second["job_id"]
    Path(args["prompt_path"]).write_text("different")
    with pytest.raises(protocol.ProtocolError) as error:
        daemon.dispatch("submit", args)
    assert error.value.code == 2
    assert len(daemon.store.list_jobs()) == 1


def test_c6_2_state_concurrent_duplicate_requests_create_one_job(state_daemon):
    """C-6.2 concurrent duplicate submissions serialize request-id ownership into one durable row."""
    daemon, harness = state_daemon
    args = harness.submit_args()
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait()
        return daemon.dispatch("submit", args)

    with ThreadPoolExecutor(max_workers=2) as workers:
        replies = list(workers.map(lambda _: submit(), range(2)))
    assert len({reply["job_id"] for reply in replies}) == 1
    assert sum(reply["created"] for reply in replies) == 1
    assert len(daemon.store.list_jobs()) == 1


def test_c6_4_a_parent_has_no_child_budget_unless_a_policy_sets_one(state_daemon):
    """C-6.4 (2026-09-28, Max: "remove *all* caps"): `max_child_jobs` is null by
    default, so a parent may have any number of children; set, it refuses the next."""
    from subfleet.adapters.base import AdapterError
    daemon, harness = state_daemon
    parent = daemon.dispatch("submit", harness.submit_args())["job_id"]
    children = [daemon.dispatch("submit", harness.submit_args(parent_job_id=parent))["job_id"] for _ in range(12)]
    assert len(set(children)) == 12
    daemon.policy["caps"]["max_child_jobs"] = 12
    with pytest.raises(AdapterError, match="max_child_jobs"):
        daemon.dispatch("submit", harness.submit_args(parent_job_id=parent))


def test_c7_3_state_parent_cancel_covers_descendants_except_independent_branches(state_daemon):
    """C-7.3, C-7.4 cancelling a queued parent atomically cancels dependent descendants only."""
    daemon, harness = state_daemon

    def submit(**extra):
        return daemon.dispatch("submit", harness.submit_args(**extra))["job_id"]

    parent = submit()
    child = submit(parent_job_id=parent)
    grandchild = submit(parent_job_id=child)
    independent = submit(parent_job_id=parent, independent=True)
    daemon.dispatch("kill", {"job_id": parent})
    cancelled = [daemon.store.get_job(job) for job in (parent, child, grandchild)]
    assert {job["state"] for job in cancelled} == {"cancelled"}
    assert len({job["cancel_requested_at"] for job in cancelled}) == 1
    assert daemon.store.get_job(independent)["state"] == "queued"
    assert daemon.store.get_job(independent)["cancel_requested_at"] is None
    assert len(daemon.store.list_notices()) == 3


def test_c15_1_state_terminal_and_notice_roll_back_together(state_daemon, monkeypatch):
    """C-3.2, C-7.4, C-15.1 a failure between terminal state and notice rolls back the whole change."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    before = daemon.store.query("SELECT * FROM events")
    notice = daemon._notice

    def fail_notice(*args):
        assert daemon.store.get_job(job_id)["state"] == "cancelled"
        assert harness.job(job_id)["state"] == "queued"  # independent read-only connection
        raise RuntimeError("injected failure before notice")

    monkeypatch.setattr(daemon, "_notice", fail_notice)
    with pytest.raises(RuntimeError, match="injected failure"):
        daemon.dispatch("kill", {"job_id": job_id})
    assert daemon.store.get_job(job_id)["state"] == "queued"
    assert daemon.store.get_job(job_id)["cancel_requested_at"] is None
    assert daemon.store.list_notices() == []
    assert daemon.store.query("SELECT * FROM events") == before
    monkeypatch.setattr(daemon, "_notice", notice)
    daemon.dispatch("kill", {"job_id": job_id})
    assert harness.job(job_id)["state"] == "cancelled"
    assert len(daemon.store.list_notices()) == 1


def test_c15_3_state_notice_ack_is_session_scoped_and_show_is_read_only(state_daemon):
    """C-15.3 only the recipient's explicit acknowledgement consumes a notice; show may serve other callers."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon.dispatch("kill", {"job_id": job_id})
    notice = daemon.dispatch("notice.pending", {"session_id": "fake-session"})["notices"][0]
    daemon.dispatch("notice.ack", {"session_id": "other-session", "notice_ids": [notice["notice_id"]]})
    assert daemon.store.list_notices()[0]["state"] == "pending"
    daemon.dispatch("show", {"job_id": job_id})
    assert daemon.store.list_notices()[0]["state"] == "pending"
    daemon.dispatch("notice.ack", {"session_id": "fake-session", "notice_ids": [notice["notice_id"]]})
    assert daemon.store.list_notices()[0]["state"] == "acknowledged"
    assert daemon.dispatch("notice.pending", {"session_id": "fake-session"})["notices"] == []


@pytest.mark.parametrize("bad_field,bad_value,code", [
    ("sandbox", "unrestricted", 2), ("allow_tmp", False, 7),
    ("prompt_path", "/a-nonexistent-subfleet-test-prompt", 2),
    ("out_path", "/a-nonexistent-subfleet-test-directory/result", 2),
    ("pinned_model", "no-such-model", 2),
])
def test_c6_1_state_validation_rejects_before_job_rows(state_daemon, bad_field, bad_value, code):
    """C-6.1, C-6.5 validation rejects invalid inputs before creating any job or attempt rows."""
    daemon, harness = state_daemon
    args = harness.submit_args(**{bad_field: bad_value})
    if bad_field == "allow_tmp":
        args["workdir"] = "/tmp"
    with pytest.raises((protocol.ProtocolError, AdapterError)) as error:
        daemon.dispatch("submit", args)
    assert error.value.code == code
    assert daemon.store.list_jobs() == []
    assert daemon.store.list_attempts() == []


def test_c5_8_state_exclusive_flock_and_reacquisition(state_daemon):
    """C-5.8 the exclusive lock rejects a second owner with 69 and releases on daemon close."""
    daemon, harness = state_daemon
    with pytest.raises(DaemonUnavailable) as error:
        Daemon(harness.root)
    assert error.value.code == 69
    daemon.close()
    successor = Daemon(harness.root)
    successor.close()


def admitted_reader(daemon, conn):
    """A reader thread for `conn`, held as `_hold_connection` holds one (C-16.7), so
    `_connection` lets it go when its last reply is out."""
    with daemon._connection_lock:
        daemon._connections.add(conn)
    return threading.Thread(target=daemon._connection, args=(conn,))


def test_c16_1_state_socket_handler_recovers_after_malformed_line(state_daemon):
    """C-16.1, C-16.7 malformed input returns code 2 while the same socket handler accepts another line."""
    daemon, _ = state_daemon
    server, client = socket.socketpair()
    server.settimeout(2)
    client.settimeout(2)
    thread = admitted_reader(daemon, server)
    thread.start()
    try:
        with client.makefile("rb") as stream:
            client.sendall(b"bad-json\n")
            error = json.loads(stream.readline())
            assert error["ok"] is False and error["error"]["code"] == 2
            client.sendall(protocol.encode(protocol.Request("daemon.status", id="still-alive")))
            reply = json.loads(stream.readline())
            assert reply["ok"] is True and reply["id"] == "still-alive"
    finally:
        client.close()
        thread.join(timeout=3)
        server.close()
    assert not thread.is_alive()


def reserve(daemon, harness, **submit_overrides):
    job_id = daemon.dispatch("submit", harness.submit_args(**submit_overrides))["job_id"]
    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[-1]
    daemon._pending_launches.discard(attempt["attempt_id"])
    adir = daemon.root / "jobs" / job_id / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700)
    return job_id, attempt, adir


def receipt_fixture(daemon, attempt, adir, *, rc=0, stdout=b"fixture result\n"):
    """Make already-exited-provider evidence without starting a process."""
    (adir / "stdout").write_bytes(stdout)
    (adir / "stderr").write_bytes(b"")
    (adir / "lane.log").write_bytes(b"")
    receipt = {"rc": rc, "signal": None, "wall_s": .1, "child_pid": None,
               "finished_at": daemon_module.utcnow()}
    (adir / "exit.json").write_text(json.dumps(receipt))
    daemon._begin_finalizing(attempt, receipt)
    return daemon.store.get_attempt(attempt["attempt_id"])


def test_c6_3_state_concurrent_submit_admission_owns_one_slot(state_daemon):
    """C-6.3, C-6.4 simultaneous submissions reserve exactly one attempt on an unmeasured lane."""
    daemon, harness = state_daemon
    capped(daemon.policy)                          # C-6.4: the caps of before 2026-09-27 (tests/caps.py)
    barrier = threading.Barrier(2)
    args = [harness.submit_args(), harness.submit_args()]

    def submit(payload):
        barrier.wait()
        return daemon.dispatch("submit", payload)["job_id"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = list(workers.map(submit, args))
    daemon._admit()
    assert len(daemon.store.list_attempts()) == 1
    assert len(daemon.store.query("SELECT * FROM leases WHERE lease_key LIKE 'lane:%'")) == 1
    waiting = [daemon.store.get_job(job) for job in jobs if daemon.store.get_job(job)["state"] == "waiting"]
    assert len(waiting) == 1 and waiting[0]["wait_reason"] == "capacity"
    assert waiting[0]["next_check_at"]
    decision = daemon.dispatch("why", {"job_id": daemon.store.list_attempts()[0]["job_id"]})["decision"]
    assert decision["chosen_lane"] == "codex-1"


def test_c4_2_state_reserved_recovery_releases_and_retries(state_daemon):
    """C-4.2 reserved recovery fails the unlaunched attempt and frees its leases before retry."""
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    daemon._process_attempt(attempt["attempt_id"])
    abandoned = daemon.store.get_attempt(attempt["attempt_id"])
    assert abandoned["state"] == "failed"
    assert abandoned["outcome_class"] == "unknown"
    assert abandoned["outcome_detail"] == "reserved-no-launch"
    assert daemon.store.query("SELECT * FROM leases") == []
    assert daemon.store.get_job(job_id)["state"] == "queued"
    daemon._admit()
    assert [row["seq"] for row in daemon.store.list_attempts(job_id)] == [1, 2]


@pytest.mark.parametrize("receipt_appears", [False, True])
def test_c4_2_state_starting_uses_receipt_or_verified_empty_retry(state_daemon, monkeypatch, receipt_appears):
    """C-4.2 starting recovers to running with a receipt, or retries after grace and empty containment."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)
    daemon.store.update_attempt(attempt["attempt_id"], state="starting", guardian_pid=42001,
                                pgid=42001, boot_id="unit-test-boot", proc_start="unit-test-start")
    if receipt_appears:
        (adir / "start.json").write_text(json.dumps({
            "guardian_pid": 42001, "pgid": 42001, "boot_id": "unit-test-boot",
            "proc_start": "unit-test-start", "started_at": daemon_module.utcnow(),
        }))
        monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: True)
        monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "alive")
    else:
        daemon._starting_deadlines[attempt["attempt_id"]] = time.monotonic() - 1
    daemon._process_attempt(attempt["attempt_id"])
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == ("running" if receipt_appears else "failed")
    assert daemon.store.get_job(job_id)["state"] == ("running" if receipt_appears else "queued")
    assert bool(daemon.store.query("SELECT * FROM leases")) is receipt_appears


def test_c4_2_state_unverifiable_starting_quarantines_and_keeps_workspace(state_daemon, monkeypatch):
    """C-4.2 starting, C-5.5, C-5.7 failed containment inspection quarantines and retains output ownership."""
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    daemon.store.update_attempt(attempt["attempt_id"], state="starting", guardian_pid=42001)
    daemon._starting_deadlines[attempt["attempt_id"]] = time.monotonic() - 1
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment(unverifiable=True))
    daemon._process_attempt(attempt["attempt_id"])
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
    assert daemon.store.query("SELECT * FROM leases WHERE lease_key LIKE 'out:%'")
    assert not daemon.store.query("SELECT * FROM leases WHERE lease_key LIKE 'lane:%'")
    assert "unverifiable" in json.dumps(daemon.dispatch("show", {"job_id": job_id}))


def test_c4_4_state_missing_exit_receipt_never_accepts_success(state_daemon, dead_guardian):
    """C-4.2 running, C-4.4 loss without a receipt remains lost with no accepted attempt or rc."""
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness, max_attempts=1)
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=42001,
                                pgid=42001, boot_id="unit-test-boot", proc_start="unit-test-start")
    daemon._process_attempt(attempt["attempt_id"])
    job = daemon.store.get_job(job_id)
    assert job["state"] == "lost" and job["accepted_attempt_id"] is None
    lost = daemon.store.get_attempt(attempt["attempt_id"])
    assert lost["state"] == "lost" and lost["rc"] is None
    assert len(daemon.store.list_notices()) == 1


@pytest.mark.parametrize("cancel_first", [False, True])
def test_c7_2_state_acceptance_and_cancellation_commit_order(state_daemon, cancel_first):
    """C-4.3, C-7.2, C-15.1 cancel before acceptance interrupts rc-0 evidence; acceptance first wins;
    either way the notice header is the job's terminal state and rc."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)
    finalizing = receipt_fixture(daemon, attempt, adir)
    if cancel_first:
        daemon.dispatch("kill", {"job_id": job_id})
    daemon._finalize(finalizing)
    job = daemon.store.get_job(job_id)
    accepted = daemon.store.get_attempt(attempt["attempt_id"])
    assert job["state"] == ("cancelled" if cancel_first else "succeeded")
    assert accepted["state"] == ("interrupted" if cancel_first else "succeeded")
    assert accepted["rc"] == 0
    assert bool(job["accepted_attempt_id"]) is not cancel_first
    assert len([row for row in daemon.store.list_artifacts(attempt["attempt_id"])
                if row["role"] == "deliverable"]) == 1
    notice, = daemon.store.list_notices()
    # C-15.1: the header is the job's state and rc, not the attempt's `ok; rc=0`
    # (incident: 2026-09-24); the summary names the attempt's class and rc.
    header, summary, *kept = notice["text"].splitlines()
    if cancel_first:
        assert header == f"{job_id}: cancelled; rc=130; deliverable=-; out=-"
        assert kept == [f"output kept, not accepted: {adir / 'deliverable.md'}"]
    else:
        assert header == f"{job_id}: succeeded; rc=0; deliverable={adir / 'deliverable.md'}; out=-"
        assert kept == []
    assert summary.startswith("attempt a1: ok, rc=0: ")
    if not cancel_first:
        result = daemon.dispatch("kill", {"job_id": job_id})
        assert result["status"] == "already finished"


def test_c4_3_state_acceptance_and_notice_share_one_transaction(state_daemon, monkeypatch):
    """C-4.3, C-15.1 a failure before notice rolls back acceptance and allows one clean finalization."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)
    finalizing = receipt_fixture(daemon, attempt, adir)
    notice = daemon._notice

    def fail_notice(*args):
        assert daemon.store.get_job(job_id)["state"] == "succeeded"
        assert harness.job(job_id)["state"] == "running"
        raise RuntimeError("injected acceptance-notice failure")

    monkeypatch.setattr(daemon, "_notice", fail_notice)
    with pytest.raises(RuntimeError, match="acceptance-notice"):
        daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    assert daemon.store.list_artifacts(attempt["attempt_id"]) == []
    assert daemon.store.list_notices() == []
    monkeypatch.setattr(daemon, "_notice", notice)
    daemon._finalize(finalizing)
    daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    assert len(daemon.store.list_notices()) == 1


def test_c8_2_state_deliverable_is_frozen_during_finalization_replay(state_daemon):
    """C-8.2 finalization replay preserves the captured deliverable despite later stdout appends."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    finalizing = receipt_fixture(daemon, attempt, adir)
    daemon._finalize(finalizing)
    (adir / "stdout").write_bytes(b"appended later transcript content\n")
    daemon._finalize(finalizing)
    assert (adir / "deliverable.md").read_bytes() == b"fixture result\n"
    assert Path(daemon.store.get_job(job_id)["out_path"]).read_bytes() == b"fixture result\n"
    artifacts = daemon.store.list_artifacts(attempt["attempt_id"])
    assert len([row for row in artifacts if row["role"] == "deliverable"]) == 1


@pytest.mark.parametrize("role", ["prompt", "manifest", "deliverable", "export"])
def test_c20_3_state_disk_full_at_publication_preserves_committed_truth(state_daemon, role):
    """C-20.3, C-8.1, C-8.3 ENOSPC publication leaves preacceptance retryable or accepted success intact."""
    daemon, harness = state_daemon

    def full(target_role, path):
        if target_role == role:
            raise OSError(errno.ENOSPC, "simulated disk full", str(path))

    if role in {"prompt", "manifest"}:
        daemon.publish_hook = full
        with pytest.raises(OSError) as error:
            daemon.dispatch("submit", harness.submit_args())
        assert error.value.errno == errno.ENOSPC
        assert daemon.store.list_jobs() == []
        assert daemon.store.list_attempts() == []
        return
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    finalizing = receipt_fixture(daemon, attempt, adir)
    daemon.publish_hook = full
    if role == "deliverable":
        with pytest.raises(OSError) as error:
            daemon._finalize(finalizing)
        assert error.value.errno == errno.ENOSPC
        assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
        assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
        daemon.publish_hook = None
        daemon._finalize(finalizing)
        assert daemon.store.get_job(job_id)["state"] == "succeeded"
    else:
        daemon._finalize(finalizing)
        job = daemon.store.get_job(job_id)
        assert job["state"] == "succeeded" and "errno=28" in job["export_error"]
        assert "export failed" in daemon.store.list_notices()[0]["text"]
        assert not Path(job["out_path"]).exists()
        assert daemon.store.query("SELECT * FROM leases") == []


def test_c8_3_state_export_recovery_publishes_after_terminal_commit(state_daemon):
    """C-4.2 finalizing, C-8.3 export resumes from an accepted result without another acceptance or notice."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    finalizing = receipt_fixture(daemon, attempt, adir)

    def boundary(name, job_id, attempt_id):
        if name == "terminal":
            raise RuntimeError("simulated daemon crash after terminal")

    daemon.crash_hook = boundary
    with pytest.raises(RuntimeError, match="after terminal"):
        daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    assert len(daemon.store.list_notices()) == 1
    assert daemon.store.query("SELECT * FROM leases WHERE lease_key LIKE 'out:%'")
    assert not Path(daemon.store.get_job(job_id)["out_path"]).exists()
    daemon.crash_hook = None
    daemon._export(job_id)
    daemon._export(job_id)
    assert Path(daemon.store.get_job(job_id)["out_path"]).read_bytes() == b"fixture result\n"
    assert len(daemon.store.list_notices()) == 1
    assert len([row for row in daemon.store.list_artifacts(attempt["attempt_id"])
                if row["role"] == "export"]) == 1


def test_c5_6_state_kill_escalation_signals_only_owned_survivors(state_daemon, monkeypatch):
    """C-5.4, C-5.6 kill escalates and individually signals only previously verified group members."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=42001,
                                child_pid=42002, pgid=42001, boot_id="unit-test-boot",
                                proc_start="unit-test-start", started_at=daemon_module.utcnow())
    attempt = daemon.store.get_attempt(attempt["attempt_id"])
    guardian = ProcessIdentity(42001, "unit-test-boot", "unit-test-start")
    child = ProcessIdentity(42002, "unit-test-boot", "unit-test-start")
    members = Containment(group_pids=frozenset({42001, 42002}),
                          identities={42001: guardian, 42002: child})
    census = iter([members, Containment(group_pids=frozenset({42002}), identities={42002: child}),
                   Containment()])
    monkeypatch.setattr(daemon, "_contain", lambda attempt: next(census))
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: True)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "alive")
    signals = []
    monkeypatch.setattr(daemon_module.procs, "signal_group",
                        lambda pgid, sig, **identity: signals.append(("group", pgid, sig)))
    monkeypatch.setattr(daemon_module.procs, "signal_process",
                        lambda identity, sig: signals.append(("process", identity.pid, sig)))
    daemon.term_grace_s = 0
    daemon.dispatch("kill", {"job_id": job_id})
    daemon._kill_attempt(attempt)
    assert signals == [("group", 42001, signal.SIGTERM), ("group", 42001, signal.SIGKILL),
                       ("process", 42002, signal.SIGKILL)]
    assert daemon.store.get_attempt(attempt["attempt_id"])["killed_by"] == "operator"
    assert json.loads((adir / "exit.json").read_text())["signal"] == signal.SIGKILL
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    assert daemon.store.get_job(job_id)["state"] == "cancelled"
    assert daemon.store.query("SELECT * FROM leases") == []


def test_c5_7_state_quarantine_confirm_dead_requires_empty_and_override_is_audited(state_daemon, monkeypatch):
    """C-5.5, C-5.7 quarantine keeps evidence and leases until verified empty or an audited override."""
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    survivor = Containment(marker_pids=frozenset({42099}))
    daemon._quarantine(attempt, survivor, "escaped fixture")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: survivor)
    args = protocol.KillArgs(job_id, confirm_dead=True)
    daemon._resolve_quarantine(attempt, args)
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
    assert daemon.store.query("SELECT * FROM leases WHERE lease_key LIKE 'out:%'")
    args = protocol.KillArgs(job_id, force_release=True, operator_note="reviewed fixture override")
    daemon._resolve_quarantine(attempt, args)
    assert daemon.store.query("SELECT * FROM leases") == []
    event = daemon.store.one("SELECT * FROM events WHERE kind='quarantine.force_release'")
    evidence = json.loads(event["data_json"])
    assert evidence["override"] is True and evidence["containment"]["live_pids"] == [42099]
    assert evidence["operator_note"] == "reviewed fixture override"


def test_c8_1_state_deliverable_and_export_sync_before_and_after_rename(state_daemon, monkeypatch):
    """C-8.1 deliverable and export publication perform file fsync, rename, then directory fsync."""
    from tests.fake.run_daemon import audit_publication
    daemon, harness = state_daemon
    export = harness.root / "export.md"
    _, attempt, adir = reserve(daemon, harness, out_path=str(export))
    finalizing = receipt_fixture(daemon, attempt, adir)
    for name in ("fsync", "rename", "replace"):
        monkeypatch.setattr(os, name, getattr(os, name))  # restore instrumentation at fixture teardown
    audit = harness.root / "publication.jsonl"
    audit_publication(audit)
    daemon._finalize(finalizing)
    operations = [json.loads(line) for line in audit.read_text().splitlines()]
    for target in (adir / "deliverable.md", export):
        index, rename = next((index, row) for index, row in enumerate(operations)
                             if row["op"] == "rename" and row["target"] == str(target))
        assert any(row["op"] == "fsync" and row["ino"] == rename["ino"]
                   and not row["directory"] for row in operations[:index])
        assert any(row["op"] == "fsync" and row["ino"] == rename["dir_ino"]
                   and row["directory"] for row in operations[index + 1:])
        assert target.stat().st_mode & 0o777 == 0o600


def test_c8_3_state_concurrent_export_workers_publish_once(state_daemon, monkeypatch):
    """C-8.1, C-8.3 concurrent recovery workers publish one export and one artifact row."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    finalizing = receipt_fixture(daemon, attempt, adir)

    def pause(name, *_):
        if name == "terminal":
            raise RuntimeError("pause before export")

    daemon.crash_hook = pause
    with pytest.raises(RuntimeError, match="pause before export"):
        daemon._finalize(finalizing)
    daemon.crash_hook = None
    publish = daemon._publish
    publications = []

    def observe(role, path, contents):
        if role == "export":
            publications.append(path)
            time.sleep(.02)
        return publish(role, path, contents)

    monkeypatch.setattr(daemon, "_publish", observe)
    barrier = threading.Barrier(2)

    def export():
        barrier.wait()
        daemon._export(job_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: export(), range(2)))
    assert publications == [harness.root / "export.md"]
    assert len([row for row in daemon.store.list_artifacts(attempt["attempt_id"])
                if row["role"] == "export"]) == 1


def test_c8_3_state_stale_failed_export_cannot_overwrite_a_new_owner(state_daemon):
    """C-6.3, C-8.3 a stale failed-export replay cannot overwrite a later owner's accepted output."""
    daemon, harness = state_daemon
    export = harness.root / "export.md"
    old_job, old_attempt, old_dir = reserve(daemon, harness, out_path=str(export))
    old_finalizing = receipt_fixture(daemon, old_attempt, old_dir, stdout=b"old result\n")

    def fail_export(role, path):
        if role == "export":
            raise OSError(errno.ENOSPC, "simulated disk full")

    daemon.publish_hook = fail_export
    daemon._finalize(old_finalizing)
    assert daemon.store.get_job(old_job)["export_error"]
    daemon.publish_hook = None
    _, new_attempt, new_dir = reserve(daemon, harness, out_path=str(export))
    daemon._finalize(receipt_fixture(daemon, new_attempt, new_dir, stdout=b"new result\n"))
    daemon._export(old_job)
    assert export.read_bytes() == b"new result\n"


def test_c4_5_state_limited_retry_moves_to_next_lane_and_records_clock(state_daemon):
    """C-4.5, C-9.4, C-9.6 a reported limit closes its lane and the next attempt uses another lane."""
    daemon, harness = state_daemon
    first_lane = daemon.store.get_lane("codex-1")
    daemon.store.put_lane(replace(first_lane, lane_id="codex-2", account_key="codex:fake-two",
                                  credential=Credential("codex", str(harness.root / "home2"), "home")))
    job_id, attempt, adir = reserve(daemon, harness)
    reset = daemon_module.after(3600)

    class LimitedAdapter(FakeAdapter):
        def classify(self, attempt_dir, launch, exit_info):
            return Outcome(OutcomeClass.LIMITED, "reported fixture limit", closure=Closure(
                "codex-1", "account", reset, ClosureReason.PROVIDER_LIMIT,
                ClockSource.REPORTED, "fixture"))

    register("codex", LimitedAdapter)
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=4))
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    assert daemon.store.query("SELECT * FROM closures")[0]["until_at"] == reset
    daemon._admit()
    attempts = daemon.store.list_attempts(job_id)
    assert [row["lane_id"] for row in attempts] == ["codex-1", "codex-2"]
    assert [row["seq"] for row in attempts] == [1, 2]
    assert json.loads(daemon.dispatch("show", {"job_id": job_id})["job"]["exclusions"]) == ["codex-1"]
    assert daemon.store.list_notices() == []


def test_c4_5_state_transient_retries_same_lane_once_then_another(state_daemon):
    """C-4.5, C-9.5 transient retries preserve the first lane for 60s, then choose another candidate."""
    daemon, harness = state_daemon
    first_lane = daemon.store.get_lane("codex-1")
    daemon.store.update_lane("codex-1", enabled=False)
    daemon.store.put_lane(replace(first_lane, lane_id="codex-2", account_key="codex:fake-two",
                                  credential=Credential("codex", str(harness.root / "home2"), "home")))
    job_id, attempt, adir = reserve(daemon, harness)

    class TransientAdapter(FakeAdapter):
        def classify(self, attempt_dir, launch, exit_info):
            return Outcome(OutcomeClass.TRANSIENT, "fixture transport disconnected")

    register("codex", TransientAdapter)
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=1))
    job = daemon.store.get_job(job_id)
    assert job["state"] == "waiting"
    assert 58 <= -daemon_module.age(job["next_check_at"]) <= 61
    assert daemon.store.query("SELECT * FROM closures") == []
    daemon.store.update_lane("codex-1", enabled=True)
    daemon.store.update_job(job_id, next_check_at=None)
    daemon._admit()
    retry = daemon.store.list_attempts(job_id)[-1]
    assert retry["lane_id"] == "codex-2"
    second_dir = adir.parent / "a2"
    second_dir.mkdir()
    daemon._finalize(receipt_fixture(daemon, retry, second_dir, rc=1))
    daemon.store.update_job(job_id, next_check_at=None)
    daemon._admit()
    assert [row["lane_id"] for row in daemon.store.list_attempts(job_id)] == ["codex-2", "codex-2", "codex-1"]


def test_c6_4_state_wall_limit_cancels_a_job_waiting_for_retry(state_daemon):
    """C-6.4 max_wall_s also bounds a job waiting between attempts and preserves terminal notice."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)

    class TransientAdapter(FakeAdapter):
        def classify(self, attempt_dir, launch, exit_info):
            return Outcome(OutcomeClass.TRANSIENT, "fixture transport disconnected")

    register("codex", TransientAdapter)
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=1))
    daemon.store.update_job(job_id, started_at="2000-01-01T00:00:00Z")
    daemon._admit()
    assert daemon.store.get_job(job_id)["state"] == "cancelled"
    assert len(daemon.store.list_notices()) == 1
    assert len(daemon.store.list_attempts(job_id)) == 1
