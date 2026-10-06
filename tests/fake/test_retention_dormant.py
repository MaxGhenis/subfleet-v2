"""2.1.11 ships retention by archive dormant (hub, 2026-10-06).

With SUBFLEET_RETENTION_DORMANT=1 in the daemon's environment (the installer
sets it in the launchd plist, never in policy.json, d574), a pass selects,
archives and deletes nothing, still prunes delivered service notices, and
waits the hour without an error. Any other value, or none, runs retention.
"""
import time

import pytest

from subfleet import daemon as daemon_module
from tests.fake.test_state_contract import state_daemon  # noqa: F401


def _calls(monkeypatch, service):
    ran, pruned = [], []
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **k: ran.append(1) or {"pruned": []})
    monkeypatch.setattr(service, "_prune_service_notices", lambda: pruned.append(1) or 0)
    service._last_maintenance = time.monotonic() - 7200
    return ran, pruned


def test_a_dormant_pass_runs_no_maintenance_and_waits_the_hour(state_daemon, monkeypatch, caplog):
    service, _ = state_daemon
    ran, pruned = _calls(monkeypatch, service)
    monkeypatch.setenv(daemon_module.RETENTION_DORMANT_ENV, "1")
    with caplog.at_level("WARNING"):
        service._retention()
        service._retention()
    assert ran == [] and pruned == [1, 1]
    assert service.timers.status()["retention"].get("last_error_type") is None
    assert time.monotonic() - service._last_maintenance < daemon_module.RETENTION_INTERVAL_S
    said = [r.getMessage() for r in caplog.records if "dormant" in r.getMessage()]
    assert len(said) == 1, said          # said once per daemon, not every pass


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", " 1"])
def test_retention_runs_unless_the_switch_is_exactly_one(state_daemon, monkeypatch, value):
    service, _ = state_daemon
    ran, pruned = _calls(monkeypatch, service)
    if value is None:
        monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    else:
        monkeypatch.setenv(daemon_module.RETENTION_DORMANT_ENV, value)
    service._retention()
    assert ran == [1] and pruned == [1]
