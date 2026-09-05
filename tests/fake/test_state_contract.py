"""Deterministic daemon state tests with no spawned provider or process inspection.

Process identities are stubbed only for constructing the daemon's test fixture;
these tests do not claim to verify guardian survival or operating-system signals.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import threading

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon, DaemonUnavailable
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter


@pytest.fixture
def state_daemon(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root)
    try:
        yield daemon, harness
    finally:
        daemon.close()


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


def test_c15_3_state_notice_ack_is_session_scoped_and_show_acknowledges(state_daemon):
    """C-15.3 notice ack belongs to the caller session and show acknowledges the job's notice once."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon.dispatch("kill", {"job_id": job_id})
    notice = daemon.dispatch("notice.pending", {"session_id": "fake-session"})["notices"][0]
    daemon.dispatch("notice.ack", {"session_id": "other-session", "notice_ids": [notice["notice_id"]]})
    assert daemon.store.list_notices()[0]["state"] == "pending"
    daemon.dispatch("show", {"job_id": job_id})
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


def test_c16_1_state_socket_handler_recovers_after_malformed_line(state_daemon):
    """C-16.1 malformed input returns code 2 while the same socket handler accepts another line."""
    daemon, _ = state_daemon
    server, client = socket.socketpair()
    server.settimeout(2)
    client.settimeout(2)
    thread = threading.Thread(target=daemon._connection, args=(server,))
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
