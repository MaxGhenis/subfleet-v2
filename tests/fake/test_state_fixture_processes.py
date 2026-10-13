"""Borrowed state fixtures must honor each behavioral test's process seam."""

import os

import pytest

from subfleet import procs
from subfleet.sessions import registry
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize("state,expected", [("live", True), ("absent", False), ("unreadable", None)])
def test_identity_override_drives_real_writer_liveness(state_daemon, monkeypatch, state, expected):
    daemon, _ = state_daemon
    record = procs.ProcessIdentity(101, "unit-test-boot", "unit-test-start")
    def identity(pid):
        if state == "unreadable":
            raise procs.InspectionError("inspection unavailable")
        return record if state == "live" else None
    monkeypatch.setattr(procs, "identity", identity)
    assert daemon._instance_alive(record.__dict__) is expected


def test_default_table_keeps_simulated_desktop_caller_live(state_daemon):
    daemon, _ = state_daemon
    row = registry.SessionRow(session_id="desktop", pid=os.getpid(), socket=None,
                              name=None, cwd=None, started_at=0, alive=True,
                              socket_present=False, registry_path="fixture",
                              entrypoint="claude-desktop", status="busy",
                              status_updated_at=0)
    table = procs.snapshot()
    starts = {pid: value[3] for pid, value in table.rows.items() if table.live(pid)}
    assert registry.validated([row], starts)[0].alive
    assert procs.liveness(os.getpid(), "unit-test-boot", "unit-test-start") == "alive"
