"""Independent round-five scripted races; production census/store/resolvers.

No OS processes are started. A failure means the stated safety invariant is
violated, rather than recording the current implementation as the expectation.
"""

import json

import pytest

from subfleet import procs
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_review_pr131_probes import (
    BOOT, NEXT_BOOT, ORIGINAL_CENSUS, quarantine, script_table,
)
from tests.fake.test_review_pr131_round3 import resolve
from tests.fake.test_state_contract import state_daemon  # noqa: F401


def attempt_with_retained_lease(daemon, harness):
    a = quarantine(daemon, harness)
    daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
    return a


def late_scan(monkeypatch, a, identities, *, group=700, source="marker", rows=None, root=None):
    reads = iter(identities)
    markers = (f"99 writer SUBFLEET_ATTEMPT={a['attempt_id']}"
               + (f" SUBFLEET_ROOT={root}" if root is not None else "") + "\n")
    monkeypatch.setattr(procs, "containment", ORIGINAL_CENSUS)
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(rows or {}, BOOT))
    monkeypatch.setattr(procs, "identity", lambda pid: next(reads))
    monkeypatch.setattr(procs, "process_group", lambda pid: group)
    monkeypatch.setattr(procs, "_read", lambda argv, **kw: markers if source == "marker" else "")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: {99} if source == "cwd" else set())


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["marker", "cwd"])
@pytest.mark.parametrize("confirmation", ["reaped", "zombie", "reused", "stable"])
def test_group_capture_race_keeps_children_of_observed_writer(
        state_daemon, monkeypatch, operator, source, confirmation):
    """An observed writer forks in its sampled group, then disappears/reuses PID.

    The child is outside the workdir and has hidden markers. It remains in the
    observed writer's group, which cannot be reused while the child is alive.
    """
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = attempt_with_retained_lease(daemon, harness)
    writer = procs.ProcessIdentity(99, BOOT, "observed-writer")
    replacement = procs.ProcessIdentity(99, BOOT, "unrelated-replacement")
    # Both a zombie and an absent/reaped process yield None from identity().
    confirmed = (writer if confirmation == "stable" else
                 replacement if confirmation == "reused" else None)
    # A reaped/zombie writer leads group 99. For PID reuse it is a member of
    # group 700, since XNU cannot reuse a live group's own leader number.
    captured_group = 700 if confirmation == "reused" else 99
    late_scan(monkeypatch, a, [writer, confirmed], group=captured_group, source=source)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    assert json.loads(actual["quarantine_reason"])["unverifiable"] == (confirmation != "stable")
    roots = json.loads(actual["evidence_json"])["lineage_roots"]
    print("CAPTURE", source, confirmation, operator, roots)

    # The previously observed process is now gone. Its child continues writing
    # in the sampled group. Any unrelated replacement of PID 99 also exits
    # before the next pace, leaving the child as the only surviving process.
    rows = {200: (1, captured_group, "S", "writer-child")}
    script_table(monkeypatch, rows)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    print("RESOLVE", source, confirmation, operator, actual["state"], bool(leases))
    assert actual["state"] == "quarantined" and leases, (
        "the group observed before writer death/reuse still contains a writer", roots, actual["state"])


@pytest.mark.parametrize("operator", [False, True])
def test_boot_changes_during_capture_retains_new_boot_writer_on_next_pace(
        state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = attempt_with_retained_lease(daemon, harness)
    first = procs.ProcessIdentity(99, BOOT, "same-start")
    second = procs.ProcessIdentity(99, NEXT_BOOT, "same-start")
    late_scan(monkeypatch, a, [first, second])
    census = daemon._contain(a)
    assert census.unverifiable and not census.verified_empty
    script_table(monkeypatch, {99: (1, 900, "S", "same-start")}, boot=NEXT_BOOT)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases


@pytest.mark.parametrize("operator", [False, True])
def test_zombie_reaped_before_fallback_state_read_keeps_replacement_uncertain(
        state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = attempt_with_retained_lease(daemon, harness)
    late_scan(monkeypatch, a, [None], rows={99: (1, 700, "Z", "old-zombie")})
    # The zombie seen by the identity read is reaped and a live writer replaces
    # it before the fallback stat read. Its start is still unknown to this read.
    monkeypatch.setattr(procs, "_stat", lambda pid: "S")
    census = daemon._contain(a)
    assert census.unverifiable and not census.verified_empty
    script_table(monkeypatch, {99: (1, 800, "S", "replacement-writer")})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases


@pytest.mark.parametrize("operator", [False, True])
def test_reused_pid_in_same_group_retains_new_identity_after_marker_disappears(
        state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = attempt_with_retained_lease(daemon, harness)
    writer = procs.ProcessIdentity(99, BOOT, "replacement-writer")
    late_scan(monkeypatch, a, [writer, writer],
              rows={99: (1, 700, "Z", "old-unrelated")})
    census = daemon._contain(a)
    assert not census.verified_empty and not census.unverifiable
    roots = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])["lineage_roots"]
    assert roots == [{"pid": 99, "boot_id": BOOT, "proc_start": "replacement-writer", "pgid": 700}]
    script_table(monkeypatch, {99: (1, 700, "S", "replacement-writer")})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases


@pytest.mark.parametrize("operator", [False, True])
def test_dq4_exact_zombie99_then_marked_writer99_witness_holds(
        state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = attempt_with_retained_lease(daemon, harness)
    writer = procs.ProcessIdentity(99, BOOT, "Tue Oct  6 12:00:00 2026")
    late_scan(monkeypatch, a, [writer, writer], group=99, root=daemon.root, rows={
        88: (1, 88, "S", "Tue Oct  6 10:00:00 2026"),
        99: (1, 99, "Z", "Tue Oct  6 11:00:00 2026"),
    })
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    roots = json.loads(actual["evidence_json"])["lineage_roots"]
    assert roots == [{"pid": 99, "boot_id": BOOT, "proc_start": writer.proc_start, "pgid": 99}]
