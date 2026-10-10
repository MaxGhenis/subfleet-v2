"""A parent's live waiter is never a child dispatch's writer (no real ps)."""

import json

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import procs
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401


def running(daemon, harness, guardian, provider, **kwargs):
    job_id, attempt, adir = reserve(daemon, harness, **kwargs)
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=guardian,
                                child_pid=provider, pgid=guardian, boot_id=BOOT,
                                proc_start=f"p{guardian}")
    (adir / "start.json").write_text(json.dumps({
        "guardian_pid": guardian, "pgid": guardian, "boot_id": BOOT,
        "proc_start": f"p{guardian}", "child_pid": provider,
        "child_identity": {"pid": provider, "boot_id": BOOT, "proc_start": f"p{provider}"},
    }))
    return daemon.store.get_attempt(attempt["attempt_id"]), adir


def assert_disjoint(daemon):
    attempts = daemon.store.query("SELECT * FROM attempts WHERE state IN "
                                  "('starting','running','finalizing','quarantined')")
    for attempt in attempts:
        evidence = json.loads(attempt["evidence_json"] or "{}")
        roots = {(r["pid"], r["boot_id"], r["proc_start"]) for r in evidence.get("lineage_roots", [])}
        for other in attempts:
            if other["attempt_id"] == attempt["attempt_id"]:
                continue
            owned = json.loads(other["evidence_json"] or "{}").get("owned_identities", {})
            assert roots.isdisjoint((r["pid"], r["boot_id"], r["proc_start"]) for r in owned.values())


@pytest.mark.parametrize("markers", ["hidden", "root-only", "foreign"])
def test_child_dispatch_succeeds_while_parent_turn_waiter_is_live(state_daemon, monkeypatch, markers):
    daemon, harness = state_daemon
    parent, _ = running(daemon, harness, 100, 101, caller_session="turn-native")
    daemon.store.update_job(parent["job_id"], kind="turn")
    rows = {100: (1, 100, "Ss", "p100"), 101: (100, 100, "S", "p101"),
            102: (101, 100, "S", "p102"), 103: (102, 100, "S", "p103")}
    script_table(monkeypatch, rows)
    daemon._record_owned(parent, procs.snapshot())
    # Submission occurs inside the parent's provider tree; its background
    # waiter remains in the shared cwd until this child finishes.
    child, adir = running(daemon, harness, 200, 201, parent_job_id=parent["job_id"],
                          caller_session="turn-native")
    rows.update({200: (101, 200, "Ss", "p200"), 201: (200, 200, "S", "p201")})
    marker = f"SUBFLEET_ROOT={daemon.root}"
    if markers == "foreign":
        marker += f" SUBFLEET_ATTEMPT={parent['attempt_id']}"
    listing = "" if markers == "hidden" else "".join(f"{pid} process {marker}\n" for pid in range(100, 104))
    script_table(monkeypatch, rows, markers=listing)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset({102}))
    daemon._record_owned(child, procs.snapshot())
    daemon._contain(child)
    daemon._contain(child)  # Retained cwd roots used to adopt the entire parent group.
    rows.pop(200)
    rows.pop(201)
    finalizing = receipt_fixture(daemon, child, adir)
    daemon.exit_settle_s = 0
    daemon._finalize(finalizing)
    assert daemon.store.get_attempt(child["attempt_id"])["state"] == "succeeded"
    assert daemon.store.get_job(child["job_id"])["state"] == "succeeded"
    assert daemon.store.get_attempt(parent["attempt_id"])["state"] == "running"
    assert {102, 103} <= rows.keys()
    assert_disjoint(daemon)
    assert not daemon.store.one("SELECT 1 FROM events WHERE kind='attempt.quarantined' AND attempt_id=?",
                                (child["attempt_id"],))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(order=st.permutations([0, 1, 2]), source=st.sampled_from(["cwd", "marker", "both"]),
       detached=st.booleans())
def test_no_lineage_root_intersects_another_live_attempts_owned_identities(
        state_daemon, monkeypatch, order, source, detached):
    daemon, harness = state_daemon
    daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
    attempts = getattr(daemon, "_isolation_attempts", None)
    if attempts is None:
        parent, _ = running(daemon, harness, 100, 101)
        child, _ = running(daemon, harness, 200, 201, parent_job_id=parent["job_id"])
        sibling, _ = running(daemon, harness, 300, 301, parent_job_id=parent["job_id"])
        attempts = daemon._isolation_attempts = [parent, child, sibling]
    for attempt in attempts:
        daemon.store.update_attempt(attempt["attempt_id"], evidence_json="{}")
    rows = {100: (1, 100, "Ss", "p100"), 101: (100, 100, "S", "p101"),
            102: (101, 102 if detached else 100, "S", "p102"),
            200: (101, 200, "Ss", "p200"), 201: (200, 200, "S", "p201"),
            300: (101, 300, "Ss", "p300"), 301: (300, 300, "S", "p301")}
    script_table(monkeypatch, rows)
    for index in order:
        daemon._record_owned(attempts[index], procs.snapshot())
        assert_disjoint(daemon)
    for index in order:
        attempt = attempts[index]
        listing = "" if source == "cwd" else "".join(
            f"{pid} process SUBFLEET_ROOT={daemon.root}\n" for pid in rows)
        script_table(monkeypatch, rows, markers=listing)
        monkeypatch.setattr(procs, "cwd_pids", lambda workdir:
                            frozenset(rows) if source != "marker" else frozenset())
        census = daemon._contain(attempt)
        assert {attempt["guardian_pid"], attempt["child_pid"]} <= census.live_pids
        assert_disjoint(daemon)
