"""The live daemon's OS ENOSPC at baseline preparation is a workspace deferral."""

import errno
import os

from subfleet import daemon as daemon_module
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository
from tests.fake.test_workspace_transient import due, events


def test_os_enospc_preparing_the_baseline_waits_then_recovers(state_daemon, monkeypatch):
    """daemon.log:57905-57906: creating the snapshot's temporary directory ran out
    of space. Real Git first validates the checkout and reads HEAD; the snapshot
    then raises the same OS error, before any attempt or lease can be reserved."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(
        sandbox="workspace-write", in_place=True))["job_id"]
    snapshot = daemon_module.working_tree
    calls = []

    def full(workspace, head, **kwargs):
        calls.append((workspace, head))
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC),
                      str(workdir / ".git" / "subfleet-salvage-enospc"))

    monkeypatch.setattr(daemon_module, "working_tree", full)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"], job["rc"]) == ("waiting", "workspace", None)
    assert len(calls) == 1 and calls[0][1]
    assert daemon.store.list_attempts(job_id) == [] and daemon.store.list_leases() == []
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert record["error_type"] == "OSError" and "[Errno 28]" in record["error"]
    assert "No space left on device" in record["error"]
    assert daemon.store.list_notices() == []

    monkeypatch.setattr(daemon_module, "working_tree", snapshot)
    due(daemon, job_id)
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved" and attempt["baseline_tree"]
    assert daemon.store.get_job(job_id)["state"] == "running"
