"""Independent round-12 ownership probes; all process inspection is scripted."""
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import procs
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import BOOT, NEXT_BOOT, script_table
from tests.fake.test_review_pr131_round11 import (
    test_own_recorded_ownership_survives_arbitrary_foreign_histories as _ownership_property,
)
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401


@settings(max_examples=800, derandomize=True, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(writers=st.sets(st.integers(202, 207), min_size=1),
       foreign_history=st.lists(st.tuples(
           st.integers(200, 210), st.sampled_from([BOOT, "old-boot", ""]),
           st.booleans()), max_size=60),
       foreign_state=st.sampled_from(["running", "finalizing", "quarantined"]),
       guardian_live=st.booleans(), detached=st.booleans())
def test_ownership_invariant_800_examples(state_daemon, monkeypatch, writers,
                                        foreign_history, foreign_state,
                                        guardian_live, detached):
    _ownership_property.hypothesis.inner_test(
        state_daemon, monkeypatch, writers, foreign_history,
        foreign_state, guardian_live, detached)


@pytest.mark.parametrize("foreign_first", [False, True])
@pytest.mark.parametrize("foreign_boot", [BOOT, NEXT_BOOT])
@pytest.mark.parametrize("same_start", [False, True])
def test_collision_order_and_reboot_preserve_signal_and_lease(
        state_daemon, monkeypatch, foreign_first, foreign_boot, same_start):
    daemon, harness = state_daemon
    other, _ = running(daemon, harness, 100, 101)
    ours, adir = running(daemon, harness, 200, 201)
    # The local writer was started on the next boot. The old foreign history
    # may have exactly the same PID/start second on either boot.
    daemon.store.update_attempt(ours["attempt_id"], boot_id=NEXT_BOOT)
    start = json.loads((adir / "start.json").read_text())
    start["boot_id"] = NEXT_BOOT
    start["child_identity"]["boot_id"] = NEXT_BOOT
    (adir / "start.json").write_text(json.dumps(start))
    ours = daemon.store.get_attempt(ours["attempt_id"])
    daemon.store.acquire_lease("native:" + ours["attempt_id"], ours["attempt_id"])
    rows = {200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", "p202")}
    foreign = {"owned_identity_history": {"202": [{
        "pid": 202, "boot_id": foreign_boot,
        "proc_start": "p202" if same_start else "old-p202",
    }]}}
    if foreign_first:
        daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps(foreign))
    script_table(monkeypatch, rows, boot=NEXT_BOOT)
    daemon._record_owned(ours, procs.snapshot())
    local_before = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    current = {"pid": 202, "boot_id": NEXT_BOOT, "proc_start": "p202"}
    preexisting_collision = foreign_first and same_start and foreign_boot == NEXT_BOOT
    if preexisting_collision:
        assert "202" not in local_before["owned_identities"]
        assert current in local_before["ambiguous_identities"]
    else:
        assert local_before["owned_identities"]["202"] == current
    if not foreign_first:
        daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps(foreign))
    # Parent disappearance strips all current ancestry. The local authority
    # must still reach both the containment census and the actual kill path.
    rows.pop(200)
    rows.pop(201)
    rows[202] = (1, 202, "Ss", "p202")
    script_table(monkeypatch, rows, boot=NEXT_BOOT)
    monkeypatch.setattr(procs, "same_process", lambda pid, boot, start:
                        pid in rows and rows[pid][3] == start and boot == NEXT_BOOT)
    sent = []
    monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: False)
    monkeypatch.setattr(procs, "signal_process", lambda known, sig, **kwargs:
                        sent.append(known) or True)
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    actual = daemon.store.get_attempt(ours["attempt_id"])
    local_after = json.loads(actual["evidence_json"])
    assert actual["state"] == "quarantined"
    assert local_after["owned_identities"].get("202") == local_before["owned_identities"].get("202")
    if not preexisting_collision:
        assert current in local_after["owned_identity_history"]["202"]
    assert (procs.ProcessIdentity(202, NEXT_BOOT, "p202") in sent) is not (same_start and foreign_boot == NEXT_BOOT)
    assert any(row["lease_key"] == "native:" + ours["attempt_id"]
               for row in daemon.store.list_leases())


def test_guardian_exit_after_table_snapshot_keeps_collision_without_signal(
        state_daemon, monkeypatch):
    daemon, harness = state_daemon
    other, _ = running(daemon, harness, 100, 101)
    ours, _ = running(daemon, harness, 200, 201)
    daemon.store.acquire_lease("native:" + ours["attempt_id"], ours["attempt_id"])
    rows = {200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", "p202")}
    script_table(monkeypatch, rows)
    daemon._record_owned(ours, procs.snapshot())
    daemon.store.update_attempt(other["attempt_id"], evidence_json=json.dumps({
        "owned_identity_history": {"202": [{
            "pid": 202, "boot_id": BOOT, "proc_start": "p202"}]},
    }))
    # The full census snapshots the guardian, then it exits before the marker
    # scan and before _kill_attempt's fresh signal-ownership check.
    reader = procs._read

    def exit_at_markers(argv, **kwargs):
        if "pid=,command=" in argv:
            rows.pop(200, None)
            rows.pop(201, None)
            rows[202] = (1, 202, "Ss", "p202")
        return reader(argv, **kwargs)

    monkeypatch.setattr(procs, "_read", exit_at_markers)
    monkeypatch.setattr(procs, "same_process", lambda pid, boot, start:
                        pid in rows and rows[pid][3] == start and boot == BOOT)
    sent = []
    monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: False)
    monkeypatch.setattr(procs, "signal_process", lambda known, sig, **kwargs:
                        sent.append(known.pid) or True)
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    assert 202 not in sent
    assert daemon.store.get_attempt(ours["attempt_id"])["state"] == "quarantined"
    assert any(row["lease_key"] == "native:" + ours["attempt_id"]
               for row in daemon.store.list_leases())


@pytest.mark.parametrize("launch_kind", ["guardian", "child"])
@pytest.mark.parametrize("same_start", [False, True])
@pytest.mark.parametrize("cleanup", ["finalize", "kill"])
def test_reused_foreign_launch_pid_does_not_release_or_skip_local_writer(
        state_daemon, monkeypatch, launch_kind, same_start, cleanup):
    daemon, harness = state_daemon
    # The old foreign guardian exited and PID 202 was reused by a newly forked
    # local writer within the same ps start second. A live guardian is a session
    # leader, so its old launch PGID 202 and the current writer PGID 200 also
    # distinguish this current local group from the stale foreign launch.
    other, _ = running(daemon, harness, 202, 203) if launch_kind == "guardian" else running(
        daemon, harness, 100, 202)
    ours, adir = running(daemon, harness, 200, 201)
    daemon.store.update_attempt(other["attempt_id"], state="quarantined")
    lease = "native:" + ours["attempt_id"]
    daemon.store.acquire_lease(lease, ours["attempt_id"])
    writer_start = "p202" if same_start else "p202-new"
    rows = {200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", writer_start)}
    script_table(monkeypatch, rows)
    daemon._record_owned(ours, procs.snapshot())
    evidence = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    assert ("202" in evidence.get("owned_identities", {})) is not same_start
    if same_start:
        assert {"pid": 202, "boot_id": BOOT, "proc_start": writer_start} in evidence["ambiguous_identities"]
    # Once the authentic local guardian/provider exit, their still-live group
    # member must retain containment even without markers or cwd evidence.
    rows.pop(200)
    rows.pop(201)
    rows[202] = (1, 200, "S", writer_start)
    sent = []
    if cleanup == "finalize":
        finalizing = receipt_fixture(daemon, ours, adir)
        daemon.exit_settle_s = 0
        daemon._finalize(finalizing)
    else:
        monkeypatch.setattr(procs, "same_process", lambda pid, boot, start:
                            pid in rows and rows[pid][3] == start and boot == BOOT)
        monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: False)
        monkeypatch.setattr(procs, "signal_process", lambda known, sig, **kwargs:
                            sent.append(known.pid) or True)
        daemon.term_grace_s = daemon.kill_settle_s = 0
        daemon._kill_attempt(ours)
        assert (202 in sent) is not same_start, sent
    state = daemon.store.get_attempt(ours["attempt_id"])["state"]
    lease_kept = any(row["lease_key"] == lease for row in daemon.store.list_leases())
    assert state == "quarantined" and lease_kept, (state, lease_kept)
