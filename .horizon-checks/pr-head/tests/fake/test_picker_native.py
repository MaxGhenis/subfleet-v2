"""Native picks use the daemon's real policy/store boundary without providers."""

import json

import pytest

from subfleet import cli, compat
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_state_contract import state_daemon


def seed(service):
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2,
        after(3600), ReadingLabel.PROVIDER, "wham", utcnow()))


def test_daemon_picker_uses_loaded_policy_without_jobs_actions_or_probes(state_daemon, monkeypatch, capsys):
    """C-10/C-11: CLI pick uses authoritative v2 state with no side effects."""
    service, harness = state_daemon
    seed(service)
    monkeypatch.setattr(service, "_execute_probe", lambda *args: pytest.fail("picker launched probe"))
    class Client:
        def call(self, op, args):
            return service.dispatch(op, args)
    monkeypatch.setattr(cli, "_client", lambda args: Client())
    monkeypatch.setattr(compat, "v1_binary", lambda: pytest.fail("v1 fallback"))
    before = {table: service.store.query(f"SELECT * FROM {table}")
              for table in ("jobs", "attempts", "actions", "leases", "readings", "decisions", "events")}
    assert compat.dispatch(["pick", "codex", "--model", "astra", "--json"], env={}) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["best"] == str(harness.root / "home")
    after_rows = {table: service.store.query(f"SELECT * FROM {table}") for table in before}
    assert after_rows == before
    # A daemon policy stronger than the on-disk file remains authoritative.
    service.policy["headroom_floor"] = .9
    assert service.dispatch("pick", {"model": "astra"})["best"] is None


def test_daemon_picker_respects_ownership_and_quarantined_slot_lease(state_daemon):
    """C-5.7/C-10.4: retained leases and owner transitions fence raw picks."""
    service, harness = state_daemon
    seed(service)
    assert service.dispatch("pick", {})["best"]
    service.store.acquire_lease("lane:codex-1:slot:0", "held-quarantine/a1")
    result = service.dispatch("pick", {})
    assert result["best"] is None
    assert "busy" in result["excluded"][0]["reasons"]
    service.store.release_leases("held-quarantine/a1")
    with service.store.transaction("test.owner") as tx:
        tx.execute("UPDATE lanes SET owner='v1' WHERE lane_id='codex-1'")
    result = service.dispatch("pick", {})
    assert result["best"] is None
    assert "owner-v1" in result["excluded"][0]["reasons"]


def test_daemon_picker_refuses_unmeasured_without_creating_admission_probe(state_daemon):
    """C-11.4: a plain path cannot substitute for a supervised probe."""
    service, harness = state_daemon
    result = service.dispatch("pick", {"model": "astra"})
    assert result["best"] is None
    assert not service.store.list_leases()
    assert not service.store.list_jobs()
    assert not list((service.root / "probes").glob("*/probe.json"))
