"""Exact leases survive rollback and alias guards survive upgrading again."""
import contextlib
import sqlite3

import pytest

from subfleet import folders, resource_leases
from subfleet.adapters.base import AdapterError
from subfleet.store import Store
from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_canonical_identity import MINIMAL, accept_for_export, revive
from tests.fake.test_canonical_identity_fix2 import configure
from tools.prepare_identity_rollback import prepare


@contextlib.contextmanager
def reopen(root):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        service = daemon_module.Daemon(root)
        harness = Harness.__new__(Harness)
        harness.root, harness.workdir = root, root / "work"
        try:
            yield service, harness, patch
        finally:
            service.close()


def test_rollback_repair_and_upgrade_preserve_output_and_native_guards(tmp_path):
    root = tmp_path / "state"
    with fleet_daemon(root) as (service, harness, patch):
        configure(service, patch)
        raw_session = MINIMAL.upper()
        path = str(tmp_path / "Result.md")
        job = submit(service, harness, kind="revive", caller_session=raw_session,
                     pinned_model="haiku", pinned_lane="claude-1", out_path=path)
        service._admit()
        attempt = service.store.list_attempts(job)[0]
        accept_for_export(service, job, b"done\n")
        before = service.store.list_leases()
        assert "out:" + folders.identity(path) in {row["lease_key"] for row in before}
        assert f"native:claude:{MINIMAL}" in {row["lease_key"] for row in before}
        with pytest.raises(ValueError, match="daemon is running"):
            prepare(root, apply=True)
    # Real stopped-daemon repair, including the preview and an idempotent rerun.
    changes = prepare(root)
    assert changes
    with Store(root / "state.sqlite3") as store:
        assert store.list_leases() == before
    assert prepare(root, apply=True) == changes
    assert prepare(root, apply=True) == []
    with Store(root / "state.sqlite3") as store:
        rows = store.list_leases()
        exact = {row["lease_key"] for row in rows}
        assert f"out:{path}" in exact
        assert f"native:claude:{raw_session}" in exact
        assert f"native-session:{attempt['lane_id']}:{raw_session}" in exact
        assert f"session:{raw_session}:revive" in exact
        assert {row["holder"] for row in rows} == {row["holder"] for row in before}
        assert {(row["holder"], row["acquired_at"], row["expires_at"]) for row in rows} == {
            (row["holder"], row["acquired_at"], row["expires_at"]) for row in before}
        # The release's exact lookup finds its own guard after this repair.
        assert store.one("SELECT holder FROM leases WHERE lease_key=?", (f"out:{path}",))["holder"] == job
        assert resource_leases.native_holds(store.query, f"native:claude:{MINIMAL}") == [
            (f"native:claude:{raw_session}", job)]
        claim = resource_leases.OutputClaim.prepare(store.query, str(tmp_path / "result.md"))
        assert (f"out:{path}", job) in claim.holds(store.query)
    # New daemon reopening the repaired store keeps the old keys as guards,
    # can finish/export, frees all four namespaces, and permits a new revive.
    with reopen(root) as (service, harness, patch):
        configure(service, patch)
        with pytest.raises(AdapterError):
            revive(service, harness, MINIMAL)
        service._export(job)
        assert (tmp_path / "Result.md").read_bytes() == b"done\n"
        assert not service.store.list_leases(job)
        assert not service.store.list_leases(attempt["attempt_id"])
        revived = revive(service, harness, MINIMAL)
        service._admit()
        assert service.store.list_attempts(revived)


def test_rollback_collision_refuses_atomically(tmp_path):
    root = tmp_path / "state"
    with fleet_daemon(root) as (service, harness, patch):
        configure(service, patch)
        path = str(tmp_path / "Result.md")
        first = submit(service, harness, out_path=path, caller_session=None)
        second = submit(service, harness, out_path=str(tmp_path / "other.md"), caller_session=None)
        service._admit()
        service.store.update_job(second, out_path=path)
        before = service.store.list_leases()
    with pytest.raises(ValueError, match="multiple holders"):
        prepare(root, apply=True)
    with sqlite3.connect(root / "state.sqlite3") as conn:
        assert list(conn.execute("SELECT lease_key,holder,acquired_at,expires_at FROM leases ORDER BY lease_key")) == sorted(
            tuple(row[key] for key in ("lease_key", "holder", "acquired_at", "expires_at")) for row in before)


@pytest.mark.parametrize("requested_name,peer_name", [
    ("state%2Fpeer", "state/peer"),
    ("peer/state.sqlite3?ignored#%2Ftail", "peer"),
    ("peer/state.sqlite3#ignored?%2Ftail", "peer"),
], ids=["percent", "query", "fragment"])
def test_rollback_uri_repairs_only_the_requested_locked_store(tmp_path, requested_name, peer_name):
    requested, peer = tmp_path / requested_name, tmp_path / peer_name

    def snapshot(root):
        with sqlite3.connect(root / "state.sqlite3") as conn:
            return (list(conn.execute("SELECT * FROM leases ORDER BY lease_key")),
                    list(conn.execute("SELECT * FROM events ORDER BY event_id")))

    with fleet_daemon(peer) as (service, harness, patch):
        configure(service, patch)
        peer_job = submit(service, harness, out_path=str(tmp_path / "Peer-Result.md"), caller_session=None)
        service._admit()
        accept_for_export(service, peer_job, b"peer\n")
        peer_before = snapshot(peer)
        with fleet_daemon(requested) as (intended, harness, patch):
            configure(intended, patch)
            job = submit(intended, harness, out_path=str(tmp_path / "Intended-Result.md"), caller_session=None)
            intended._admit()
            accept_for_export(intended, job, b"intended\n")
            stored_path = intended.store.get_job(job)["out_path"]
        intended_before = snapshot(requested)
        with pytest.raises(ValueError, match="daemon is running"):
            prepare(peer, apply=True)
        expected = [("out:" + folders.identity(stored_path), "out:" + stored_path)]
        assert prepare(requested) == expected
        assert snapshot(requested) == intended_before
        assert snapshot(peer) == peer_before
        assert prepare(requested, apply=True) == expected
        assert prepare(requested, apply=True) == []
        intended_after = snapshot(requested)
        assert "out:" + stored_path in {row[0] for row in intended_after[0]}
        assert len(intended_after[1]) == len(intended_before[1]) + 1
        assert snapshot(peer) == peer_before, "repair changed a different daemon's live store"
