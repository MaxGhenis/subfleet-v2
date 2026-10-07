"""Failed group confirmation retains evidence until the shared census empties."""

import json

import pytest

from subfleet import procs
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_review_pr131_round3 import resolve
from tests.fake.test_review_pr131_round5 import attempt_with_retained_lease, late_scan
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("source", ["marker", "cwd"])
@pytest.mark.parametrize("confirmation", ["reaped", "zombie", "reused", "unavailable"])
def test_failed_confirmation_group_holds_until_verified_empty_without_signals(
        state_daemon, monkeypatch, operator, source, confirmation):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = attempt_with_retained_lease(daemon, harness)
    writer = procs.ProcessIdentity(99, BOOT, "observed-writer")
    replacement = procs.ProcessIdentity(99, BOOT, "unrelated-replacement")
    confirmed = replacement if confirmation == "reused" else None
    group = 700 if confirmation == "reused" else 99
    late_scan(monkeypatch, a, [writer, confirmed], group=group, source=source)
    if confirmation == "unavailable":
        reads = iter([writer, procs.InspectionError("confirmation unavailable")])

        def identity(pid):
            value = next(reads)
            if isinstance(value, procs.InspectionError):
                raise value
            return value

        monkeypatch.setattr(procs, "identity", identity)

    def refuse_signal(*args, **kwargs):
        raise AssertionError("conservative census evidence must never authorize signals")

    monkeypatch.setattr(procs, "signal_group", refuse_signal)
    monkeypatch.setattr(procs, "signal_process", refuse_signal)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases
    roots = json.loads(actual["evidence_json"])["lineage_roots"]
    assert {"pid": 99, "boot_id": BOOT, "proc_start": writer.proc_start, "pgid": group} in roots

    # With every direct source gone, the retained group alone finds the child.
    script_table(monkeypatch, {200: (1, group, "S", "writer-child")})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases

    # A failed table read cannot prove the group's death.
    script_table(monkeypatch, {}, table_fails=True)
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "quarantined" and leases

    # Both paths release on the next pace once the same census verifies empty.
    script_table(monkeypatch, {})
    clock.advance()
    actual, leases = resolve(daemon, a, operator)
    assert actual["state"] == "lost" and not leases
