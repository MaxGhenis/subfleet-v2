"""Round-14 adversarial inputs; all process observations and signals are fake."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import procs
from subfleet.salvage import SalvageError
from tests.fake.test_quarantine_ambiguity import REAL_SAME_PROCESS
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository


def kill_consumer(daemon, ours, consumer):
    if consumer == "attempt":
        daemon._kill_attempt(ours)
    else:
        evidence = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
        record = {**evidence, **{k: ours[k] for k in ("job_id", "lane_id", "guardian_pid", "pgid", "boot_id", "proc_start")},
                  "holder": ours["attempt_id"], "directory": str(daemon.root), "state": "running"}
        daemon._contain_probe(record)


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
@pytest.mark.parametrize("initial_parent", [201, 1])
def test_claimed_identity_joining_group_after_census_is_not_signalled(state_daemon, monkeypatch, initial_parent, consumer):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    other, _ = running(daemon, harness, 100, 101)
    claimed = {"pid": 202, "boot_id": BOOT, "proc_start": "p202"}
    daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({
        "owned_identity_history": {"202": [claimed]}}))
    rows = {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201"),
            202: (initial_parent, 202, "S", "p202")}
    script_table(monkeypatch, rows)
    daemon._record_owned(ours, procs.snapshot())
    contain = daemon._contain if consumer == "attempt" else daemon._probe_census
    calls = 0

    def move_after_census(*args, **kwargs):
        nonlocal calls
        result = contain(*args, **kwargs)
        calls += 1
        if calls == 1:
            rows[202] = (initial_parent, 200, "S", "p202")
        return result

    monkeypatch.setattr(daemon, "_contain" if consumer == "attempt" else "_probe_census", move_after_census)
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: rows[pid][1])
    delivered = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: delivered.append((pid, int(sig))))
    monkeypatch.setattr(procs.os, "killpg", lambda pgid, sig:
                        delivered.extend((pid, int(sig)) for pid, row in rows.items() if row[1] == pgid))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    kill_consumer(daemon, ours, consumer)
    assert daemon.store.get_attempt(other["attempt_id"])["state"] == "running"
    assert not [signal for signal in delivered if signal[0] == 202], delivered


@pytest.mark.parametrize("availability", ["late", "read-outage"])
def test_pending_terminal_receipt_is_not_forgotten(state_daemon, monkeypatch, availability):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    other, directory = running(daemon, harness, 100, 101)
    daemon.store.update_attempt(other["attempt_id"], evidence_json="{}", state="succeeded")
    receipt = directory / "start.json"
    child = json.dumps({"child_identity": {"pid": 200, "boot_id": BOOT, "proc_start": "p200"}})
    receipt.write_text(child if availability == "read-outage" else "{}")
    read_json = daemon._read_json
    unreadable = availability == "read-outage"

    def read(path):
        nonlocal unreadable
        if unreadable and path == receipt:
            unreadable = False
            return None
        return read_json(path)

    monkeypatch.setattr(daemon, "_read_json", read)
    script_table(monkeypatch, {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201")})
    target = procs.ProcessIdentity(200, BOOT, "p200")
    assert not daemon._foreign_ownership(ours, procs.snapshot()).claimed(target)
    receipt.write_text(child)
    foreign = daemon._foreign_ownership(ours, procs.snapshot())
    assert foreign.claimed(target), "empty receipt read removed the terminal attempt from the pending queue"


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
@pytest.mark.parametrize("publication", ["receipt", "indexed-claim"])
def test_live_claim_during_scalar_confirmation_blocks_delivery(state_daemon, monkeypatch, publication, consumer):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    other, directory = running(daemon, harness, 100, 101)
    daemon.store.update_attempt(other["attempt_id"], evidence_json="{}")
    (directory / "start.json").write_text("{}")
    rows = {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201")}
    script_table(monkeypatch, rows)
    daemon._record_owned(ours, procs.snapshot())
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    target = {"pid": 200, "boot_id": BOOT, "proc_start": "p200"}
    published = False

    def group(pid):
        nonlocal published
        if not published:
            published = True
            if publication == "receipt":
                (directory / "start.json").write_text(json.dumps({"child_identity": target}))
            else:
                daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({"child_identity": target}))
        return rows[pid][1]

    monkeypatch.setattr(procs.os, "getpgid", group)
    delivered = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: delivered.append((pid, int(sig))))
    monkeypatch.setattr(procs.os, "killpg", lambda pgid, sig:
                        delivered.extend((pid, int(sig)) for pid, row in rows.items() if row[1] == pgid))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    kill_consumer(daemon, ours, consumer)
    assert published
    assert daemon.store.get_attempt(other["attempt_id"])["state"] == "running"
    assert not [signal for signal in delivered if signal[0] == 200], delivered


def test_reserve_retries_a_legitimate_workspace_deferral(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    repository(daemon, harness)
    workspace = daemon._workspace
    deferred = []

    def timed_out_once(job):
        if not deferred:
            deferred.append(job["job_id"])
            raise SalvageError("scripted git rev-parse timed out", timed_out=True)
        assert job["job_id"] == deferred[0]
        return workspace(job)

    monkeypatch.setattr(daemon, "_workspace", timed_out_once)
    job_id, attempt, directory = reserve(daemon, harness, sandbox="workspace-write")
    assert deferred == [job_id]
    assert len(daemon.store.list_jobs()) == 1
    assert daemon.store.list_attempts(job_id) == [attempt]
    assert directory.is_dir()
    assert daemon.store.get_job(job_id)["state"] == "running"


def test_failed_startup_receipt_import_is_retried(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    other, directory = running(daemon, harness, 100, 101)
    daemon.store.update_attempt(other["attempt_id"], state="succeeded", evidence_json="{}", child_pid=None)
    receipt = directory / "start.json"
    child = {"pid": 200, "boot_id": BOOT, "proc_start": "p200"}
    receipt.write_text(json.dumps({"child_identity": child}))
    with daemon.store.transaction("fixture.retry_receipt_import", audit=False) as tx:
        tx.execute("UPDATE process_claim_receipts SET seeded=0 WHERE singleton=1")
    read_json = daemon._read_json
    unreadable = True

    def read(path):
        nonlocal unreadable
        if unreadable and path == receipt:
            unreadable = False
            return None
        return read_json(path)

    monkeypatch.setattr(daemon, "_read_json", read)
    daemon._seed_process_claim_receipts()
    # The restart path invokes this same importer using the durable seeded bit.
    daemon._seed_process_claim_receipts()
    script_table(monkeypatch, {200: (1, 200, "S", "p200")})
    foreign = daemon._foreign_ownership(ours, procs.snapshot())
    assert foreign.claimed(procs.ProcessIdentity(**child))


def test_reserve_reports_a_persistent_admission_hold(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    repository(daemon, harness)
    calls = []
    def timeout(job):
        calls.append(job["job_id"])
        raise SalvageError("scripted git timeout", timed_out=True)
    monkeypatch.setattr(daemon, "_workspace", timeout)
    with pytest.raises(AssertionError) as error:
        reserve(daemon, harness, sandbox="workspace-write")
    assert len(calls) == 4 and len(set(calls)) == 1
    assert all(word in str(error.value) for word in ("workspace", "scripted git timeout", "readings", "leases"))
    assert daemon.store.list_attempts() == []


def test_world_constructor_cleans_up_after_setup_failure(monkeypatch):
    from tests.fake.test_quarantine_process_world import ProcessWorldMachine
    before = procs.boot_id
    cleanup = []
    directories = []
    def fail(machine):
        directories.append(Path(machine.directory.name))
        machine.fixture = SimpleNamespace(close=lambda: cleanup.append("fixture"))
        machine.daemon = SimpleNamespace(close=lambda: cleanup.append("daemon"))
        machine.patch.setattr(procs, "boot_id", lambda: "leaked-patch")
        raise RuntimeError("constructor setup failed")
    monkeypatch.setattr(ProcessWorldMachine, "setup_world", fail)
    with pytest.raises(RuntimeError, match="constructor setup failed"):
        ProcessWorldMachine()
    assert cleanup == ["daemon", "fixture"]
    assert procs.boot_id is before
    assert not directories[0].exists()


@pytest.mark.parametrize("publication", ["receipt", "indexed-claim"])
def test_publication_serializes_with_authorized_syscall(state_daemon, monkeypatch, publication):
    from subfleet.guardian import _receipt
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    other, directory = running(daemon, harness, 100, 101)
    daemon.store.update_attempt(other["attempt_id"], evidence_json="{}")
    (directory / "start.json").write_text("{}")
    rows = {200: (1, 200, "S", "p200"), 201: (200, 200, "S", "p201")}
    script_table(monkeypatch, rows)
    foreign = daemon._foreign_ownership(ours, procs.snapshot())
    # Publication completes before authorization. Keep the snapshot's pending
    # entry so the dispatch must take its sidecar, even after indexing succeeds.
    _receipt(directory / "start.json", {"child_identity": {"pid": 101, "boot_id": BOOT, "proc_start": "p101"}})
    daemon._refresh_process_claim_receipts(ours["attempt_id"], candidates=foreign.receipt_candidates)
    foreign = __import__("dataclasses").replace(foreign, generation=daemon.store.one(
        "SELECT generation FROM process_claim_clock WHERE singleton=1")["generation"])
    attempted, finished = threading.Event(), threading.Event()
    target = {"pid": 200, "boot_id": BOOT, "proc_start": "p200"}
    def publish():
        attempted.set()
        try:
            if publication == "receipt":
                _receipt(directory / "start.json", {"child_identity": target})
            else:
                daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({"child_identity": target}))
        finally:
            finished.set()
    publisher = threading.Thread(target=publish)
    delivered = []
    def syscall(pid, sig):
        publisher.start()
        assert attempted.wait(2)
        assert not finished.wait(.05), "claim publisher crossed the authorized syscall boundary"
        delivered.append(pid)
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: rows[pid][1])
    monkeypatch.setattr(procs.os, "kill", syscall)
    try:
        daemon._signal_owned(ours["attempt_id"], procs.ProcessIdentity(200, BOOT, "p200"), 15,
                             procs.Containment(), {}, foreign)
    finally:
        if publisher.ident is not None:
            publisher.join(timeout=5)
    assert delivered == [200]
    assert finished.is_set() and not publisher.is_alive()
