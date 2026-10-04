"""Reap requires positive evidence of death; inspection failure is inconclusive."""

import sys
import types

import pytest

from subfleet import cli, procs


@pytest.mark.parametrize("state,expected", [("alive", True), ("dead", False), ("unknown", None)])
def test_reap_identity_preserves_three_states(monkeypatch, state, expected):
    """C-5.3: only the dead liveness verdict permits an orphan label."""
    monkeypatch.setattr(procs, "liveness", lambda *args: state)
    monkeypatch.setattr(procs, "same_process", lambda *args: pytest.fail("lossy boolean check"))
    checker, source = cli._same_process()
    assert checker(123, "boot", "start") is expected
    assert source == "subfleet.procs"


@pytest.mark.parametrize("expected", [True, False, None])
def test_reap_keeps_legacy_injected_checker(monkeypatch, expected):
    """C-5.3: tests and older core providers can inject the tri-state seam."""
    module = types.ModuleType("subfleet.procs")
    module.same_process = lambda *args: expected
    monkeypatch.setitem(sys.modules, "subfleet.procs", module)
    checker, source = cli._same_process()
    assert checker(123, "boot", "start") is expected
    assert source == "subfleet.procs"


def test_reap_never_reports_unknown_runner_as_gone(monkeypatch, capsys):
    """C-4.2/C-5.3: unknown process inspection emits no orphan or mutation."""
    monkeypatch.setattr(procs, "liveness", lambda *args: "unknown")
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    class Client:
        def call(self, op, args):
            assert op == "daemon.status"
            return {}
    class Offline:
        def list_jobs(self, **kwargs):
            return [{"job_id": "still-running", "state": "running", "guardian_pid": 123,
                     "boot_id": "boot", "proc_start": "start"}]
    monkeypatch.setattr(cli, "_client", lambda args: Client())
    monkeypatch.setattr(cli, "_offline", lambda args: Offline())
    assert cli.main(["runs", "reap", "--json"]) == 0
    assert not capsys.readouterr().out
    assert cli.main(["runs", "reap"]) == 0
    captured = capsys.readouterr()
    assert not captured.out
    assert "0 orphan(s)" in captured.err
