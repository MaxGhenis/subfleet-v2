"""Round-14 adversarial inputs; all process observations and signals are fake."""
import json

import pytest

from subfleet import procs
from subfleet.salvage import SalvageError
from tests.fake.test_quarantine_ambiguity import REAL_SAME_PROCESS
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository


@pytest.mark.parametrize("initial_parent", [201, 1])
def test_claimed_identity_joining_group_after_census_is_not_signalled(state_daemon, monkeypatch, initial_parent):
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
    contain = daemon._contain
    calls = 0

    def move_after_census(*args, **kwargs):
        nonlocal calls
        result = contain(*args, **kwargs)
        calls += 1
        if calls == 1:
            rows[202] = (initial_parent, 200, "S", "p202")
        return result

    monkeypatch.setattr(daemon, "_contain", move_after_census)
    monkeypatch.setattr(procs, "same_process", REAL_SAME_PROCESS)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: rows[pid][1])
    delivered = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: delivered.append((pid, int(sig))))
    monkeypatch.setattr(procs.os, "killpg", lambda pgid, sig:
                        delivered.extend((pid, int(sig)) for pid, row in rows.items() if row[1] == pgid))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
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


@pytest.mark.parametrize("publication", ["receipt", "indexed-claim"])
def test_live_claim_during_scalar_confirmation_blocks_delivery(state_daemon, monkeypatch, publication):
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
    daemon._kill_attempt(ours)
    assert published
    assert daemon.store.get_attempt(other["attempt_id"])["state"] == "running"
    assert not [signal for signal in delivered if signal[0] == 200], delivered


def test_reserve_fails_after_a_legitimate_workspace_deferral(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    repository(daemon, harness)
    workspace = daemon._workspace

    def timed_out(job):
        raise SalvageError("scripted git rev-parse timed out", timed_out=True)

    monkeypatch.setattr(daemon, "_workspace", timed_out)
    with pytest.raises(IndexError):
        reserve(daemon, harness, sandbox="workspace-write")
    job = daemon.store.list_jobs()[-1]
    assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    assert daemon.store.list_attempts(job["job_id"]) == []
    print("R14 reservation diagnostic:", job["state"], job["wait_reason"], daemon._holds[job["job_id"]])
    monkeypatch.setattr(daemon, "_workspace", workspace)
    daemon.store.update_job(job["job_id"], next_check_at="")
    daemon._admit()
    assert len(daemon.store.list_attempts(job["job_id"])) == 1


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
