"""Round-11 regressions for ownership collisions, isolation and disk holds.

All process inspection is scripted. No guardian/provider is launched.
"""
import itertools
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as dm, procs
from subfleet.disk import DiskAdmission, GB
from tests.fake.test_admission_disk import enable, fake_clock, placed, stamp, submit
from tests.fake.test_quarantine_attempt_isolation import (
    running,
    test_no_lineage_root_intersects_another_live_attempts_owned_identities as isolation_property,
)
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401

POLICY = {"admission": {"disk": {"enabled": True, "floor_gb": 40,
                               "resume_margin_gb": 5, "placement_reserve_gb": 1.5,
                               "reserve_ttl_s": 600}}}


def owned_writer(state_daemon, monkeypatch, *, same_start=True):
    daemon, harness = state_daemon
    other, _ = running(daemon, harness, 100, 101)
    ours, adir = running(daemon, harness, 200, 201)
    daemon.store.acquire_lease("native:" + ours["attempt_id"], ours["attempt_id"])
    rows = {100: (1, 100, "Ss", "p100"), 101: (100, 100, "S", "p101"),
            200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", "p202")}
    script_table(monkeypatch, rows)
    daemon._record_owned(ours, procs.snapshot())
    assert "202" in json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])["owned_identities"]
    # An older store or delayed foreign observation remembers the former
    # incarnation of PID 202. A reused PID can have the same second-resolution
    # lstart; our current guardian/group and own record still prove a writer.
    daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({
        "owned_identity_history": {"202": [{"pid": 202, "boot_id": BOOT,
                                            "proc_start": "p202" if same_start else "old-p202"}]},
    }))
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({202}))
    script_table(monkeypatch, rows,
                 markers=f"202 writer SUBFLEET_ATTEMPT={ours['attempt_id']} SUBFLEET_ROOT={daemon.root}\n")
    return daemon, ours, adir, rows


@pytest.mark.parametrize("same_start", [True, False])
@pytest.mark.parametrize("early_guardian_exit", [False, True])
def test_local_writer_survives_foreign_history_collision(
        state_daemon, monkeypatch, same_start, early_guardian_exit):
    daemon, ours, _, rows = owned_writer(state_daemon, monkeypatch, same_start=same_start)
    if early_guardian_exit:
        rows.pop(200)
        rows.pop(201)
        rows[202] = (1, 200, "S", "p202")
    census = daemon._contain(ours)
    assert 202 in census.live_pids, census.to_dict()


@pytest.mark.parametrize("same_start", [True, False])
def test_finalization_retains_leases_despite_foreign_history_collision(
        state_daemon, monkeypatch, same_start):
    daemon, ours, adir, rows = owned_writer(state_daemon, monkeypatch, same_start=same_start)
    rows.pop(200)
    rows.pop(201)
    rows[202] = (1, 200, "S", "p202")
    leases = {r["lease_key"] for r in daemon.store.list_leases()
              if r["holder"] in {ours["attempt_id"], ours["job_id"]}
              and not r["lease_key"].startswith("lane:")}
    assert leases
    finalizing = receipt_fixture(daemon, ours, adir)
    daemon.exit_settle_s = 0
    daemon._finalize(finalizing)
    after = daemon.store.get_attempt(ours["attempt_id"])
    retained = {r["lease_key"] for r in daemon.store.list_leases()
                if r["holder"] in {ours["attempt_id"], ours["job_id"]}}
    assert after["state"] == "quarantined", (after["state"], leases, retained)
    assert leases <= retained


def test_kill_signals_local_writer_despite_foreign_history_collision(state_daemon, monkeypatch):
    daemon, ours, _, rows = owned_writer(state_daemon, monkeypatch)
    rows.pop(200)
    rows.pop(201)
    rows[202] = (1, 200, "S", "p202")
    monkeypatch.setattr(procs, "same_process", lambda pid, boot, start:
                        pid in rows and rows[pid][3] == start and boot == BOOT)
    sent = []
    monkeypatch.setattr(procs, "signal_group", lambda pgid, sig, **kwargs:
                        sent.append(("group", pgid)) or False)
    monkeypatch.setattr(procs, "signal_process", lambda known, sig:
                        sent.append(("pid", known.pid)) or True)
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    assert ("pid", 202) in sent, sent
    assert daemon.store.get_attempt(ours["attempt_id"])["state"] == "quarantined"


@pytest.mark.parametrize("foreign_state", ["running-detached", "quarantined"])
def test_finished_child_ignores_known_foreign_writer(state_daemon, monkeypatch, foreign_state):
    daemon, harness = state_daemon
    parent, _ = running(daemon, harness, 100, 101)
    rows = {100: (1, 100, "Ss", "p100"), 101: (100, 100, "S", "p101"),
            102: (101, 102 if foreign_state == "running-detached" else 100, "S", "p102")}
    if foreign_state == "running-detached":
        rows[103] = (101, 100, "S", "p103")
        rows[102] = (103, 102, "Ss", "p102")
    script_table(monkeypatch, rows)
    daemon._record_owned(parent, procs.snapshot())
    evidence = json.loads(daemon.store.get_attempt(parent["attempt_id"])["evidence_json"])
    assert any(root["pid"] == 102 for root in evidence["lineage_roots"])
    if foreign_state == "running-detached":
        assert "102" not in evidence["owned_identities"]
        # An intermediate parent exits; the guardian and provider stay live.
        rows.pop(103)
        rows[102] = (1, 102, "Ss", "p102")
    else:
        daemon._quarantine(parent, daemon._contain(parent), "foreign writer remains")
        assert daemon.store.get_attempt(parent["attempt_id"])["state"] == "quarantined"
    child, adir = running(daemon, harness, 200, 201)
    rows.update({200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201")})
    script_table(monkeypatch, rows, markers="")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({102}))
    daemon._record_owned(child, procs.snapshot())
    rows.pop(200)
    rows.pop(201)
    finalizing = receipt_fixture(daemon, child, adir)
    daemon.exit_settle_s = 0
    daemon._finalize(finalizing)
    actual = daemon.store.get_attempt(child["attempt_id"])
    job = daemon.store.get_job(child["job_id"])
    assert (actual["state"], job["state"], job["rc"]) == ("succeeded", "succeeded", 0)


def test_quarantined_writer_keeps_unexpired_disk_reservation(tmp_path):
    gate = DiskAdmission(tmp_path, read_free=lambda path: 43 * GB)
    gate.begin_pass(POLICY, [], stamp(0))
    row = {"attempt_id": "attempt-a", "state": "quarantined", "kind": "dispatch",
           "reserved_at": stamp(0), "finished_at": stamp(1),
           "evidence_json": json.dumps({"disk_reservation": gate.evidence()})}
    gate.begin_pass(POLICY, [row], stamp(2))
    assert gate.reserved_bytes == 1.5 * GB


def test_quarantine_does_not_allow_extra_disk_placement(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    monkeypatch.setattr(dm, "git_head", lambda *args, **kwargs: None)
    monkeypatch.setattr(dm, "git_toplevel", lambda *args, **kwargs: None)
    clock = fake_clock(monkeypatch)
    reading = enable(daemon, monkeypatch, free=46)
    first = submit(daemon, harness)
    daemon._admit()
    a = daemon.store.list_attempts(first)[0]
    assert daemon._disk.reserved_bytes == 1.5 * GB
    clock[0] = 1
    ident = procs.ProcessIdentity(42099, "test-boot", "test-start")
    live = procs.Containment(marker_pids=frozenset({ident.pid}), identities={ident.pid: ident})
    daemon._quarantine(a, live, "owned writer remains alive")
    quarantined = daemon.store.get_attempt(a["attempt_id"])
    assert quarantined["state"] == "quarantined"
    assert quarantined["finished_at"] == stamp(1)
    reading["gb"] = 43
    waiting = {submit(daemon, harness), submit(daemon, harness)}
    daemon._admit()
    admitted = [row for row in placed(daemon) if row["job_id"] in waiting]
    # One new 1.5 GB placement fits beside quarantine's 1.5 GB at a 40 GB floor.
    assert len(admitted) == 1


@pytest.mark.parametrize("order", list(itertools.permutations([0, 1, 2])))
@pytest.mark.parametrize("source", ["cwd", "marker", "both"])
@pytest.mark.parametrize("detached", [False, True])
def test_all_36_existing_isolation_cases(state_daemon, monkeypatch, order, source, detached):
    isolation_property.hypothesis.inner_test(state_daemon, monkeypatch, order, source, detached)


@settings(max_examples=120, derandomize=True, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(writers=st.sets(st.integers(202, 207), min_size=1),
       foreign_history=st.lists(st.tuples(
           st.integers(200, 210), st.sampled_from([BOOT, "old-boot", ""]),
           st.booleans()), max_size=30),
       foreign_state=st.sampled_from(["running", "finalizing", "quarantined"]),
       guardian_live=st.booleans(), detached=st.booleans())
def test_own_recorded_ownership_survives_arbitrary_foreign_histories(
        state_daemon, monkeypatch, writers, foreign_history, foreign_state,
        guardian_live, detached):
    daemon, harness = state_daemon
    daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
    attempts = getattr(daemon, "_collision_attempts", None)
    if attempts is None:
        attempts = daemon._collision_attempts = (
            running(daemon, harness, 100, 101)[0],
            running(daemon, harness, 200, 201)[0],
        )
    other, ours = attempts
    for attempt in attempts:
        daemon.store.update_attempt(attempt["attempt_id"], state="running", evidence_json="{}")
    rows = {100: (1, 100, "Ss", "p100"), 101: (100, 100, "S", "p101"),
            200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201")}
    rows.update({pid: (201, 200, "S", f"p{pid}") for pid in writers})
    script_table(monkeypatch, rows, markers="")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset())
    daemon._record_owned(ours, procs.snapshot())
    before = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    local = {str(pid): before["owned_identities"][str(pid)] for pid in writers}
    history = {}
    # Every example includes a full collision, plus arbitrary stale or
    # incomplete foreign observations of both matching and unrelated PIDs.
    for pid, boot, same_start in [(min(writers), BOOT, True), *foreign_history]:
        history.setdefault(str(pid), []).append({
            "pid": pid, "boot_id": boot, "proc_start": f"p{pid}" if same_start else f"old-p{pid}",
        })
    daemon.store.update_attempt(other["attempt_id"], state=foreign_state,
                                evidence_json=json.dumps({"owned_identity_history": history}))
    if not guardian_live:
        rows.pop(200)
        rows.pop(201)
    for pid in writers:
        if detached or not guardian_live:
            rows[pid] = (1, pid if detached else 200, "S", f"p{pid}")
    # Both paced recording and repeated full cleanup must preserve the
    # attempt's own authority, including after parent loss and group escape.
    daemon._record_owned(ours, procs.snapshot())
    for _ in range(2):
        census = daemon._contain(ours)
        after = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
        assert writers <= census.live_pids
        assert all(after["owned_identities"].get(pid) == value for pid, value in local.items())
        for pid, value in local.items():
            assert value in after["owned_identity_history"][pid]


def test_cwd_lineage_cannot_claim_an_independent_writer(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    parent, _ = running(daemon, harness, 100, 101)
    rows = {100: (1, 100, "Ss", "p100"), 101: (100, 100, "S", "p101"),
            102: (1, 102, "Ss", "p102")}
    script_table(monkeypatch, rows, markers="")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({102}))
    assert 102 in daemon._contain(parent).live_pids
    evidence = json.loads(daemon.store.get_attempt(parent["attempt_id"])["evidence_json"])
    assert any(root["pid"] == 102 for root in evidence["lineage_roots"])
    child, _ = running(daemon, harness, 200, 201)
    rows.update({200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201")})
    # A foreign run's cwd observation alone cannot discharge this writer.
    assert 102 in daemon._contain(child).live_pids


@pytest.mark.parametrize("foreign_state", ["running", "quarantined"])
def test_full_census_preserves_foreign_descendant_after_reparenting(
        state_daemon, monkeypatch, foreign_state):
    daemon, harness = state_daemon
    parent, _ = running(daemon, harness, 100, 101)
    rows = {100: (1, 100, "Ss", "p100"), 101: (100, 100, "S", "p101"),
            102: (103, 102, "Ss", "p102"), 103: (101, 100, "S", "p103")}
    script_table(monkeypatch, rows, markers="")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset())
    assert 102 in daemon._contain(parent).live_pids
    if foreign_state == "quarantined":
        daemon._quarantine(parent, daemon._contain(parent), "foreign descendant remains")
    rows.pop(103)
    rows[102] = (1, 102, "Ss", "p102")
    child, _ = running(daemon, harness, 200, 201)
    rows.update({200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201")})
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({102}))
    assert daemon._contain(child).live_pids == {200, 201}


def test_incomplete_local_identity_holds_against_complete_foreign_history(state_daemon, monkeypatch):
    daemon, ours, _, rows = owned_writer(state_daemon, monkeypatch)
    rows.pop(200)
    rows.pop(201)
    rows[202] = (1, 202, "Ss", "p202")
    # Legacy local evidence with an unreadable boot remains ambiguous. A
    # complete foreign observation of the same PID/start cannot disprove it.
    local = {"pid": 202, "boot_id": "", "proc_start": "p202"}
    daemon.store.update_attempt(ours["attempt_id"], evidence_json=json.dumps({
        "owned_identities": {"202": local}, "owned_identity_history": {"202": [local]},
    }))
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset())
    script_table(monkeypatch, rows, markers="")
    census = daemon._contain(ours)
    assert not census.verified_empty
    assert census.unverifiable
    assert 202 in census.live_pids
    evidence = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    assert evidence["owned_identities"]["202"] == local
    assert local in evidence["owned_identity_history"]["202"]


def test_adopted_guardian_incarnation_cannot_claim_foreign_descendants(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    parent, _ = running(daemon, harness, 100, 101)
    replacement = {"pid": 100, "boot_id": BOOT, "proc_start": "replacement"}
    daemon.store.update_attempt(parent["attempt_id"], state="quarantined",
                                quarantine_reason=json.dumps({"identities": {"100": replacement}}))
    rows = {100: (1, 100, "Ss", "replacement"), 102: (100, 102, "Ss", "p102")}
    script_table(monkeypatch, rows, markers="")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset())
    # The old guardian's PID was reused. A held census of its replacement
    # retains its descendants conservatively but cannot claim their ancestry.
    assert 102 in daemon._contain(parent).live_pids
    child, _ = running(daemon, harness, 200, 201)
    rows.update({200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201")})
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({102}))
    assert 102 in daemon._contain(child).live_pids
