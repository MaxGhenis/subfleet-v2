"""A child run can finish while its parent's turn and background waiter live."""
import pytest

from subfleet import procs
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.unit.test_ci131_marker_scope import world

CENSUS = procs.containment


@pytest.mark.parametrize("waiter", [False, True], ids=["turn", "turn-and-waiter"])
def test_completed_run_finalizes_with_parent_attempt_still_live(state_daemon, monkeypatch, waiter):
    daemon, harness = state_daemon
    job, attempt, adir = reserve(daemon, harness)
    receipt_fixture(daemon, attempt, adir)
    rows = {77: (1, 77, "S", "parent-turn")}
    markers = f"77 turn SUBFLEET_ATTEMPT=parent/a1 SUBFLEET_ROOT={daemon.root}\n"
    if waiter:
        rows[78] = (77, 77, "S", "waiter")
        markers += f"78 wait SUBFLEET_ATTEMPT=parent/a1 SUBFLEET_ROOT={daemon.root}\n"
    world(monkeypatch, markers, rows=rows)
    monkeypatch.setattr(procs, "containment", CENSUS)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset(rows))
    daemon.exit_settle_s = 0
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    assert daemon.store.get_job(job)["state"] == "succeeded"
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "succeeded"
    assert not daemon.store.list_leases()
