"""Push preparation rechecks sandbox holds on the real store transaction."""

from concurrent.futures import Future
import json
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon
from subfleet.store import Store


@pytest.mark.parametrize("held", [False, True], ids=["preparing", "sandbox-held"])
def test_push_workspace_preparation_reads_sandbox_hold_in_real_store(tmp_path, monkeypatch, held):
    now, until = "2026-11-13T00:00:00Z", "2026-11-13T01:00:00Z"
    monkeypatch.setattr(daemon_module, "utcnow", lambda: now)
    with Store(tmp_path / "state.sqlite3") as store:
        job_id = store.add_job(
            job_id="push-build", request_id="push-build", payload_digest="push-build",
            kind="dispatch", task="build", sandbox="workspace-write", push_branch="jobs/build",
            workdir=str(tmp_path), prompt_path=str(tmp_path / "prompt.md"),
        )
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
        assert len(submitted) == 1
        assert core._push_workspaces == {job_id: submitted[0][2]}
        assert not submitted[0][2].done()
        current = store.get_job(job_id)
        assert current["state"] == "waiting"
        assert current["wait_reason"] == ("task-held" if held else "workspace")
        assert current["next_check_at"] == (until if held else None)
        assert current["worktree"] is None
        assert store.list_attempts(job_id) == []
        events = store.query("SELECT kind,data_json FROM events WHERE job_id=? "
                             "AND kind IN ('job.workspace_preparing','job.task_held') ORDER BY event_id",
                             (job_id,))
        assert [(event["kind"], json.loads(event["data_json"])) for event in events] == [
            ("job.task_held", {"sandbox": "workspace-write", "until": until, "since": now,
                               "taken": True, "kept": None}) if held else ("job.workspace_preparing", {})
        ]
