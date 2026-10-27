"""C-6.18/R5: retention cannot erase a start still needed after restart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from subfleet import daemon as daemon_module, retention, retention_archive
from subfleet.ramp import PlacementRamp
from tests.fake.test_state_contract import state_daemon  # noqa: F401


BASE = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)


def stamp(seconds=0):
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def clock_at(monkeypatch, initial):
    clock = [initial]
    monkeypatch.setattr(daemon_module, "utcnow", lambda: stamp(clock[0]))
    return clock


def seed_completed(service, harness, identity, *, kind="dispatch", starts=(0,)):
    """A finished read-only job has no leases, Git checkout, or provider process."""
    service.store.add_job(
        job_id=identity, request_id=identity, payload_digest="digest", kind=kind,
        workdir=str(harness.workdir), prompt_path="/prompt", sandbox="read-only",
        state="succeeded", created_at=stamp(-100), finished_at=stamp(max(starts) + 1),
    )
    directory = harness.root / "jobs" / identity
    directory.mkdir(parents=True)
    (directory / "stdout").write_text("finished output\n")
    for seq, when in enumerate(starts, start=1):
        retry = seq < len(starts)
        service.store.add_attempt(
            attempt_id=f"{identity}/a{seq}", job_id=identity, seq=seq,
            lane_id="codex-1", model_requested="gpt-6-astra",
            state="failed" if retry else "succeeded",
            outcome_class="transient" if retry else "ok",
            reserved_at=stamp(when), finished_at=stamp(when + 1),
        )


def enable(service, enabled=True):
    service.policy["admission"]["ramp"] = {
        "enabled": enabled, "per_minute": 12, "window_s": 60,
    }


def test_recent_completed_and_retry_starts_pin_jobs_but_attended_starts_do_not(state_daemon, monkeypatch):
    service, harness = state_daemon
    clock = clock_at(monkeypatch, 59)
    enable(service)
    for identity, options in (
        ("completed", {}), ("retried", {"starts": (0, 1)}),
        ("expired", {"starts": (-1,)}), ("attended", {"kind": "turn"}),
        ("future", {"starts": (70,)}),
    ):
        seed_completed(service, harness, identity, **options)
    monkeypatch.setattr(service.conversations, "retention_pins", lambda: {"conversation-owned"})
    assert service._retention_pins() == {"conversation-owned", "completed", "retried", "future"}
    clock[0] = 60
    assert service._retention_pins() == {"conversation-owned", "retried", "future"}
    clock[0] = 61
    assert service._retention_pins() == {"conversation-owned", "future"}


def test_fractional_start_inside_cutoff_second_remains_pinned(state_daemon, monkeypatch):
    service, harness = state_daemon
    clock_at(monkeypatch, 60)
    enable(service)
    seed_completed(service, harness, "boundary", starts=(0,))
    seed_completed(service, harness, "fractional", starts=(0.5,))
    assert service._retention_pins() == {"fractional"}


@pytest.mark.parametrize("absent", [False, True], ids=["disabled", "absent"])
def test_disabled_retention_returns_existing_pins_without_clock_or_history_reads(state_daemon, monkeypatch, absent):
    service, _ = state_daemon
    if absent:
        service.policy["admission"].pop("ramp")
    else:
        enable(service, enabled=False)
    existing = iter(("conversation-owned",))
    monkeypatch.setattr(service.conversations, "retention_pins", lambda: existing)

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled ramp must not read its clock or ledger for retention")

    monkeypatch.setattr(daemon_module, "utcnow", forbidden)
    monkeypatch.setattr(service, "_ramp_rows", forbidden)
    assert service._retention_pins() is existing


def test_daemon_retention_preserves_restart_history_until_window_expires(state_daemon, monkeypatch):
    """The real retention entry point retains rows at 59 s and prunes at 60 s."""
    service, harness = state_daemon
    clock = clock_at(monkeypatch, 59)
    enable(service)
    seed_completed(service, harness, "completed")
    seed_completed(service, harness, "retried", starts=(-1, 0))
    seed_completed(service, harness, "attended", kind="turn")
    service.policy["retention"] = {
        "jobs": 0, "bytes": 0, "turn_jobs": 0, "turn_bytes": 0, "turn_keep_days": 0,
    }
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    real_maintenance = daemon_module.maintenance

    def maintenance_without_process_scan(*args, **kwargs):
        kwargs["holders"] = lambda watches, **options: {}
        return real_maintenance(*args, **kwargs)

    monkeypatch.setattr(daemon_module, "maintenance", maintenance_without_process_scan)
    service._retention()
    assert {row["job_id"] for row in service.store.list_jobs()} == {"completed", "retried"}
    gate = PlacementRamp()
    gate.begin_pass(service.policy, service._ramp_rows(""), stamp(59))
    assert gate.snapshot["used"] == 2
    assert len(service.store.list_attempts()) == 3
    clock[0] = 60
    service._retention()
    assert service.store.list_jobs() == []
    assert service.store.list_attempts() == []


def test_enabling_ramp_after_archive_selection_is_rechecked_inside_delete(state_daemon, monkeypatch):
    """R5: policy enabled during archive pins its rows in the delete transaction."""
    service, harness = state_daemon
    clock_at(monkeypatch, 59)
    enable(service, enabled=False)
    seed_completed(service, harness, "late-policy")
    callbacks = []

    def conversation_pins():
        callbacks.append(service.store.connection.in_transaction)
        return set()

    monkeypatch.setattr(service.conversations, "retention_pins", conversation_pins)
    real_commit = retention_archive.Retirement.commit

    def enable_at_commit(retirement, pool_bytes):
        enable(service)
        return real_commit(retirement, pool_bytes)

    monkeypatch.setattr(retention_archive.Retirement, "commit", enable_at_commit)
    result = retention.maintenance(
        service.store, harness.root, max_jobs=0, max_bytes=0, turn_keep_s=0,
        pins=service._retention_pins, holders=lambda watches, **kwargs: {},
    )
    assert result["pruned"] == []
    assert callbacks[-1] is True
    assert service.store.get_job("late-policy") is not None
    assert service.store.get_attempt("late-policy/a1") is not None
    assert (harness.root / "jobs" / "late-policy").is_dir()


@pytest.mark.parametrize("kind", ["dispatch", "resume", "revive", "gate-review", "handoff"])
def test_every_detached_kind_enters_census_and_stays_retention_pinned(
        state_daemon, monkeypatch, kind):
    service, harness = state_daemon
    clock = clock_at(monkeypatch, 59)
    enable(service)
    seed_completed(service, harness, "detached-start", kind=kind)
    seed_completed(service, harness, "attended-start", kind="turn")
    seed_completed(service, harness, "synthetic-probe", kind="probe")
    monkeypatch.setattr(service.conversations, "retention_pins", lambda: set())

    assert [row["job_id"] for row in service._ramp_rows(stamp(-1))] == ["detached-start"]
    service._begin_ramp_pass()
    assert service._ramp.snapshot["used"] == 1
    assert service._retention_pins() == {"detached-start"}
    clock[0] = 60
    service._begin_ramp_pass()
    assert service._ramp.snapshot["used"] == 0
    assert service._retention_pins() == set()
