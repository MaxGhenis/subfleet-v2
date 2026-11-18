"""D-KILL3: deterministic daemon decisions when the sandbox denies ps/sysctl.

The daemon's inspection, deciding census, persistence and finalization are real;
only the kernel observations and the provider's exit/interrupt are stand-ins.
The companion self_exit_daemon file runs the shell/lockf shape on a permitted host.
"""

import json
import signal
from types import SimpleNamespace

import pytest

from subfleet import procs
from subfleet.contracts import attempt_dir
from tests.fake_kernel import FakeKernel, procargs
from tests.fake.test_census_self_exit_daemon import interrupt_at_ten_seconds
from tests.unit.test_daemon_settle import (
    ATTEMPT, JOB, STARTED, attempt, daemon, publish_receipt, shared, table_showing, with_launch,
)  # noqa: F401


def observation(daemon, monkeypatch):
    table = table_showing((4242, 1, 4242, "Ss"), (4243, 4242, 4242, "S"), (4401, 4243, 4401, "S"))
    current = [table]
    monkeypatch.setattr(procs, "boot_id", lambda: "boot")
    monkeypatch.setattr(procs, "snapshot", lambda: current[0])
    kernel = FakeKernel({4401: procargs(["/usr/bin/lockf", "scratch.lock"])})
    monkeypatch.setattr(procs, "kernel", kernel)
    daemon._process_table = lambda due: (current[0], due + 1)
    with_launch(daemon, monkeypatch)
    (attempt_dir(daemon.root, JOB, 1) / "stream.jsonl").unlink()
    daemon.exit_settle_s = 0
    daemon.store.acquire_lease("out:/scratch/deliverable", ATTEMPT)
    daemon.store.acquire_lease("worktree:/scratch/owned", JOB)
    return current, kernel


@pytest.mark.parametrize("ending", ["self-exit", "stopped-turn"])
def test_d_kill3_an_inspected_hidden_runner_quarantines_after_provider_exit(daemon, monkeypatch, ending):
    current, kernel = observation(daemon, monkeypatch)
    daemon._inspect_running(attempt(daemon), attempt_dir(daemon.root, JOB, 1), 0)
    assert kernel.calls == [], "steady-state inspection must use only its shared table"
    assert not daemon.store.query("SELECT 1 FROM events WHERE kind='attempt.kill_started'")

    def provider_ends(*args):
        current[0] = table_showing((4401, 1, 4401, "S"))
        publish_receipt(daemon)

    if ending == "stopped-turn":
        from subfleet.conversations.classify import TurnAdapter
        daemon.store.update_job(JOB, kind="turn")
        daemon.conversations = SimpleNamespace(
            adapter=lambda provider: TurnAdapter(provider),
            release_socket=lambda aid: pytest.fail("uncontained stopped turn released its relay"),
        )
        signals = []
        interrupt_at_ten_seconds(4243, lambda pid, sig: (signals.append((pid, sig)), provider_ends()))
        assert signals == [(4243, signal.SIGINT)]
    else:
        provider_ends()
    daemon._finalize(attempt(daemon))
    row = attempt(daemon)
    assert row["state"] == "quarantined", row
    evidence = json.loads(row["evidence_json"])
    assert evidence["counted_identities"]["4401"] == {
        "pid": 4401, "boot_id": "boot", "proc_start": STARTED}
    assert "4401" not in evidence.get("owned_identities", {})
    census = json.loads(row["quarantine_reason"])
    assert census["descendant_pids"] == [4401] and not census["marker_pids"]
    assert census["reason"] == "writers remain after exit receipt"
    assert len(daemon.store.query("SELECT 1 FROM leases WHERE holder IN (?,?)", (JOB, ATTEMPT))) == 2


@pytest.mark.documented_limitation
def test_d_kill3_no_inspection_before_the_parent_leaves_still_releases(daemon, monkeypatch):
    """A change closing the between-inspection window must deliberately change this."""
    current, kernel = observation(daemon, monkeypatch)
    current[0] = table_showing((4401, 1, 4401, "S"))
    publish_receipt(daemon)
    assert daemon._contain(attempt(daemon)).verified_empty
    daemon._finalize(attempt(daemon))
    row = attempt(daemon)
    assert row["state"] == "failed" and not row["quarantine_reason"]
    assert 4401 in kernel.processes
    assert not json.loads(row["evidence_json"]).get("counted_identities")
