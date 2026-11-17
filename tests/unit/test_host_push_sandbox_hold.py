"""Push preparation rechecks sandbox holds on the real store transaction.

Fix r1 of #309 (review r1's last P3): the checkout starts only inside that
transaction, after the hold is read again. A hold committed after the door's look
and before the build starts takes the job, and no build is queued for it, whether
this is the job's first look or a later pass re-offering a build to a job already
waiting `workspace`.
"""

from concurrent.futures import Future
import json
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon
from subfleet.store import Store


@pytest.mark.parametrize("waiting", [None, "workspace"], ids=["first-look", "re-offer"])
@pytest.mark.parametrize("held", [False, True], ids=["preparing", "sandbox-held"])
def test_push_workspace_preparation_reads_sandbox_hold_in_real_store(tmp_path, monkeypatch, held, waiting):
    now, until = "2026-11-13T00:00:00Z", "2026-11-13T01:00:00Z"
    monkeypatch.setattr(daemon_module, "utcnow", lambda: now)
    with Store(tmp_path / "state.sqlite3") as store:
        job_id = store.add_job(
            job_id="push-build", request_id="push-build", payload_digest="push-build",
            kind="dispatch", task="build", sandbox="workspace-write", push_branch="jobs/build",
            workdir=str(tmp_path), prompt_path=str(tmp_path / "prompt.md"),
        )
        if waiting:
            with store.transaction("test.waiting_workspace") as tx:
                tx.execute("UPDATE jobs SET state='waiting',wait_reason=? WHERE job_id=?", (waiting, job_id))
        # Admission can have sampled the job before the hold committed. Its
        # workspace-preparing write must judge the current transactional rows.
        job = store.get_job(job_id)
        if held:
            with store.transaction("test.sandbox_hold") as tx:
                tx.execute("INSERT INTO leases(lease_key,holder,acquired_at,expires_at) VALUES (?,?,?,?)",
                           ("sandbox-hold:workspace-write", "operator: disk brake", now, until))

        submitted = []

        def queue(function, *args):
            future = Future()
            submitted.append((function, args, future))
            return future

        # Queue construction deterministically without a worker or Git process;
        # the transaction and every hold reader below are production code.
        core = object.__new__(Daemon)
        core.root, core.store, core.pushes = tmp_path, store, SimpleNamespace(submit=queue)
        core._push_preparation_offered = False
        core._push_workspaces, core._push_unused_workspaces = {}, set()
        core._push_workspace_store_errors = {}
        core._push_workspace_store_failures = {}
        core._push_workspace_store_first_failure = {}
        core._capacity_waits, core._pin_stale = {}, set()

        assert core._push_workspace_ready(job) is False
        assert len(submitted) == (0 if held else 1), "a build was queued for a held job"
        assert core._push_workspaces == ({} if held else {job_id: submitted[0][2]})
        assert all(not future.done() for _, _, future in submitted)
        assert not (tmp_path / "worktrees" / job_id).exists()
        current = store.get_job(job_id)
        assert current["state"] == "waiting"
        assert current["wait_reason"] == ("task-held" if held else "workspace")
        assert current["next_check_at"] == (until if held else None)
        assert current["worktree"] is None
        assert store.list_attempts(job_id) == []
        events = store.query("SELECT kind,data_json FROM events WHERE job_id=? "
                             "AND kind IN ('job.workspace_preparing','job.task_held') ORDER BY event_id",
                             (job_id,))
        expected = ([("job.task_held", {"sandbox": "workspace-write", "until": until, "since": now,
                                        "taken": True, "kept": None})] if held
                    else [] if waiting else [("job.workspace_preparing", {})])
        assert [(event["kind"], json.loads(event["data_json"])) for event in events] == expected
