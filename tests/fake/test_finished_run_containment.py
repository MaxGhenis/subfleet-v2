"""A completed child run must not adopt its background waiter's lineage."""

import json

import pytest

from subfleet import daemon as dm, procs
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401


ORIGINAL_CENSUS = procs.containment
BOOT = "6F1C0F2E-1111-4222-8333-944455556666"


@pytest.mark.parametrize("earlier", ["absent", "reused"])
def test_finished_run_does_not_adopt_a_waiter_born_during_census(
        state_daemon, monkeypatch, earlier):
    daemon, harness = state_daemon
    job_id, a, adir = reserve(daemon, harness)
    a = receipt_fixture(daemon, a, adir)
    now = [0.0]
    monkeypatch.setattr(dm.time, "monotonic", lambda: now[0])
    waiter = procs.ProcessIdentity(200, BOOT, "waiter-start")
    snapshots = []

    def snapshot():
        # The turn's background waiter starts (or reuses a PID) after the
        # first table, before the environment and cwd listings. It waits for
        # this finished run, so including it creates a circular dependency.
        rows = ({200: (1, 500, "S", "old-start")} if earlier == "reused" else {})
        if snapshots:
            rows = {200: (500, 500, "S", waiter.proc_start)}
        snapshots.append(rows)
        return procs.ProcessTable(rows, BOOT)

    def read(argv, **kwargs):
        command = f"wait SUBFLEET_ATTEMPT=parent/a1 SUBFLEET_ROOT={daemon.root}\n"
        if "pid=,command=" in argv:
            return "200 " + command
        assert argv == ["/bin/ps", "-p", "200", "-Eww", "-o", "command="]
        return command

    monkeypatch.setattr(procs, "containment", ORIGINAL_CENSUS)
    monkeypatch.setattr(procs, "snapshot", snapshot)
    monkeypatch.setattr(procs, "identity", lambda pid: waiter)
    monkeypatch.setattr(procs, "process_group", lambda pid: 500)
    monkeypatch.setattr(procs, "_read", read)
    # release/217 has no cwd source; run the identical world on both versions.
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({200}), raising=False)

    daemon._finalize(a)
    now[0] = daemon.exit_settle_s + 1
    daemon._finalize(daemon.store.get_attempt(a["attempt_id"]))
    job = daemon.store.get_job(job_id)
    actual = daemon.store.get_attempt(a["attempt_id"])
    assert (job["state"], job["rc"]) == ("succeeded", 0), actual["quarantine_reason"]
    assert actual["state"] == "succeeded"
    assert not json.loads(actual["evidence_json"] or "{}").get("lineage_roots")
