"""Independent round-six review interleavings; no OS writer processes."""

import json

import pytest

from subfleet import procs
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_review_pr131_round3 import resolve
from tests.fake.test_review_pr131_round5 import attempt_with_retained_lease, late_scan
from tests.fake.test_state_contract import state_daemon  # noqa: F401


def forbid_signals(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("retained census evidence cannot authorize signals")
    monkeypatch.setattr(procs, "signal_group", refuse)
    monkeypatch.setattr(procs, "signal_process", refuse)


def capture_failed_group(monkeypatch, daemon, harness, clock, operator, source):
    a = attempt_with_retained_lease(daemon, harness)
    writer = procs.ProcessIdentity(99, BOOT, "observed-writer")
    late_scan(monkeypatch, a, [writer, None], group=99, source=source)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    assert {"pid": 99, "boot_id": BOOT, "proc_start": writer.proc_start, "pgid": 99} in json.loads(actual["evidence_json"])["lineage_roots"]
    return a


def pace(monkeypatch, daemon, clock, a, operator, rows, *, held):
    script_table(monkeypatch, rows)
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: set())
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == ("quarantined" if held else "lost"), actual
    assert bool(leases) is held


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["marker", "cwd"])
def test_sampled_group_survives_leader_exit_and_new_member_join(state_daemon, monkeypatch, operator, source):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    forbid_signals(monkeypatch)
    a = capture_failed_group(monkeypatch, daemon, harness, clock, operator, source)
    pace(monkeypatch, daemon, clock, a, operator, {200: (1, 99, "S", "first-child")}, held=True)
    # First child exits after forking a replacement in the leaderless group.
    pace(monkeypatch, daemon, clock, a, operator, {201: (1, 99, "S", "new-member")}, held=True)
    pace(monkeypatch, daemon, clock, a, operator, {}, held=False)


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["marker", "cwd"])
def test_failed_confirmation_group_reuse_waits_for_verified_empty(state_daemon, monkeypatch, operator, source):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    forbid_signals(monkeypatch)
    a = capture_failed_group(monkeypatch, daemon, harness, clock, operator, source)
    # The old group empties between samples; its number is then reused. The
    # unconfirmed identity/group pair is evidence, not an incarnation proof.
    pace(monkeypatch, daemon, clock, a, operator, {
        99: (1, 99, "S", "unrelated-leader"),
        300: (99, 99, "S", "unrelated-member"),
    }, held=True)
    pace(monkeypatch, daemon, clock, a, operator, {300: (1, 99, "S", "unrelated-member")}, held=True)
    pace(monkeypatch, daemon, clock, a, operator, {}, held=False)


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["marker", "cwd"])
def test_group_sampled_after_pid_reuse_is_not_paired_with_the_old_identity(state_daemon, monkeypatch, operator, source):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    forbid_signals(monkeypatch)
    a = attempt_with_retained_lease(daemon, harness)
    writer = procs.ProcessIdentity(99, BOOT, "first-writer")
    # The marker/cwd and first identity read see non-leader writer 99 in
    # group/session 700. It exits; another surviving attempt process spawns
    # writer 99, which calls setsid and leads group/session 99. The kernel
    # group lookup samples that new group, but the confirmation read fails.
    # Neither old-group 700 nor new-group 99 exists in the initial table.
    late_scan(monkeypatch, a, [writer], group=99, source=source)
    reads = iter([writer, procs.InspectionError("confirmation unavailable")])

    def identify(pid):
        value = next(reads)
        if isinstance(value, procs.InspectionError):
            raise value
        return value

    monkeypatch.setattr(procs, "identity", identify)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    roots = json.loads(actual["evidence_json"])["lineage_roots"]
    assert {"pid": 99, "boot_id": BOOT, "proc_start": writer.proc_start, "pgid": 99} in roots
    # Positive group evidence describes the second writer's populated group.
    # Its hidden markers/outside cwd leave the retained group as sole source.
    pace(monkeypatch, daemon, clock, a, operator, {
        99: (1, 99, "S", "second-writer"),
        300: (99, 99, "S", "second-writer-child"),
    }, held=True)
    pace(monkeypatch, daemon, clock, a, operator, {}, held=False)


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["marker", "cwd"])
def test_confirmation_failure_on_both_paces_retains_each_sampled_group(state_daemon, monkeypatch, operator, source):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    forbid_signals(monkeypatch)
    a = capture_failed_group(monkeypatch, daemon, harness, clock, operator, source)
    # A second independent writer also dies after its group is read. Neither
    # census can establish empty, and neither failed bracket erases the first.
    second = procs.ProcessIdentity(101, BOOT, "second-observation")
    late_scan(monkeypatch, a, [second, None], group=700, source=source,
              rows={200: (1, 99, "S", "first-child")})
    monkeypatch.setattr(procs, "_read", lambda *args, **kwargs:
                        f"101 writer SUBFLEET_ATTEMPT={a['attempt_id']}\n" if source == "marker" else "")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: {101} if source == "cwd" else set())
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    assert json.loads(actual["quarantine_reason"])["unverifiable"]
    roots = json.loads(actual["evidence_json"])["lineage_roots"]
    assert {root["pgid"] for root in roots} >= {99, 700}
    pace(monkeypatch, daemon, clock, a, operator, {201: (1, 700, "S", "second-child")}, held=True)
    pace(monkeypatch, daemon, clock, a, operator, {}, held=False)
