"""Helpers for the task-hold tests (C-11.10, D-TASKHOLD1): an in-process daemon on
a virtual clock, its holds, and what admission placed.

The daemon is `tests.fake.test_state_contract.state_daemon`'s: fake adapters,
stubbed process identity, no guardian and no provider, so a placement is an
attempt row in `reserved` and nothing ever launches.
"""

from __future__ import annotations

from typing import Any

from subfleet import protocol
from subfleet.adapters.registry import register
from subfleet.contracts import Outcome, OutcomeClass
from tests.disk_floor_model import stamp
from tests.fake.test_admission_latency import submit_turn
from tests.fake_adapter import FakeAdapter
# Loaded here, as the test modules are collected, not first inside a Hypothesis example
# (`fresh_daemon` uses its clock): it imports `tests.unit.test_disk_admission`, which
# applies `@given` as it loads, and Hypothesis refuses that inside a running example
# (`nested_given`), so a property file run alone failed its first example.
import tests.fake.test_admission_ramp  # noqa: E402,F401

NOTE = "uk-data batch release: no new builds"


def hold(service, task: str, until: str, note: str | None = NOTE, **extra) -> dict:
    args = {"action": "hold", "task": task, "until": until, **({"note": note} if note is not None else {}), **extra}
    return service.dispatch("tasks", args)


def release(service, task: str) -> dict:
    return service.dispatch("tasks", {"action": "release", "task": task})


def held_now(service) -> list[dict]:
    return service.dispatch("tasks", {"action": "list"})["task_holds"]


def submit(service, harness, task: str | None, name: str, **changes) -> str:
    """A detached read-only job of `task`, pinned to Astra (the fixture's codex lane)."""
    args = harness.submit_args(request_id=name, name=name, **({"task": task, "tier": "standard"} if task else {}),
                               **changes)
    return service.dispatch("submit", args)["job_id"]


def turn(service, harness, n: int, task: str | None = None) -> str:
    """A conversation turn's job, of `task` when one is named (a turn is never held)."""
    return submit_turn(service, harness, n, **({"task": task, "tier": "standard"} if task else {}))


def attempts(service, job_id: str | None = None) -> list[dict]:
    if job_id is None:
        return service.store.query("SELECT * FROM attempts ORDER BY rowid")
    return service.store.query("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (job_id,))


def job(service, job_id: str) -> dict:
    return service.store.get_job(job_id)


def finish(service, attempt: dict, *, retry: bool = False) -> None:
    """End `attempt` as its finalization would: `ok`, or `transient` with the job queued again."""
    with service.store.transaction("test.task_hold_attempt_ended", job_id=attempt["job_id"]) as tx:
        tx.execute("UPDATE attempts SET state=?,finished_at=?,outcome_class=? WHERE attempt_id=?",
                   ("failed" if retry else "succeeded", stamp(0), "transient" if retry else "ok",
                    attempt["attempt_id"]))
        tx.execute("UPDATE jobs SET state=?,wait_reason=NULL,next_check_at=NULL,finished_at=? WHERE job_id=?",
                   ("queued" if retry else "succeeded", None if retry else stamp(0), attempt["job_id"]))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt["attempt_id"], attempt["job_id"]))


class Disconnected(FakeAdapter):
    """A provider that exits on a dropped stream: `transient`, retried on its own lane after
    C-4.5's backoff (`TRANSIENT_RETRY_DELAY_S`), as the finalizer sets it."""

    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.TRANSIENT, "Temporary Codex transport failure",
                       evidence={"rc": exit_info.rc, "model_answered": True,
                                 "admission": "stream disconnected before completion"})


def finalize_transient(service, attempt: dict, *, clock: list | None = None,
                       finalized_at: int | None = None) -> dict:
    """End `attempt` through the daemon's own finalizer (`_finalize`) as a `transient` exit:
    the job waits on `capacity` with its retry due `TRANSIENT_RETRY_DELAY_S` after the
    finalizer's clock. With `clock` and `finalized_at`, the provider exits at the clock's
    reading and the finalizer runs at `finalized_at`, as when load or a restart delays it
    (review r3 of #260, R3-2): the attempt's end stays the provider's exit, and the retry is
    due 60 s after the finalizer. The job's row, after."""
    from tests.fake.test_state_contract import receipt_fixture
    adir = service.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700, parents=True, exist_ok=True)
    service._pending_launches.discard(attempt["attempt_id"])
    register("codex", Disconnected)
    try:
        receipt = receipt_fixture(service, attempt, adir, rc=1, stdout=b"")
        if clock is not None and finalized_at is not None:
            clock[0] = finalized_at
        service._finalize(receipt)
    finally:
        register("codex", FakeAdapter)
    return service.store.get_job(attempt["job_id"])


def legacy_retry(service, harness, clock, finalized_at):
    """A pre-round-3 row: real finalization and audit, without the new evidence field."""
    import json

    build = submit(service, harness, "build", "legacy-retry")
    service._admit()
    [attempt] = attempts(service, build)
    finalize_transient(service, attempt, clock=clock, finalized_at=finalized_at)
    evidence = json.loads(attempts(service, build)[0]["evidence_json"])
    evidence.pop("retry_due_at")
    with service.store.transaction("test.legacy_attempt", attempt_id=attempt["attempt_id"]) as tx:
        tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?",
                   (json.dumps(evidence), attempt["attempt_id"]))
    accepted = service.store.one("SELECT ts FROM events WHERE kind='attempt.accepted' AND attempt_id=?",
                                 (attempt["attempt_id"],))
    assert accepted["ts"] == stamp(finalized_at)
    assert attempts(service, build)[0]["finished_at"] == stamp(0)
    return build


def restart(service, harness):
    """The daemon stops and its successor opens on the same state root, as a restart leaves
    it: what it knew in memory it reads from the store again (`_load_pin_episodes`). Each
    of `harness.task_hold_configure` (policy changes a test made in memory) runs on it
    again. `fresh_daemon` and `restarts` close the successors."""
    from subfleet.daemon import Daemon
    service.close()
    successor = Daemon(harness.root)

    def refuse_real_launch(*args):
        raise AssertionError("state-only fixtures must never launch a guardian")
    successor._launch = refuse_real_launch
    for configure in getattr(harness, "task_hold_configure", ()):
        configure(successor)
    harness.__dict__.setdefault("task_hold_successors", []).append(successor)
    return successor


def configure(service, harness, change) -> None:
    """Make a policy change now and on every restart (`restart`)."""
    change(service)
    harness.__dict__.setdefault("task_hold_configure", []).append(change)


def close_successors(harness) -> None:
    for successor in harness.__dict__.pop("task_hold_successors", []):
        successor.close()


def refused(service, args: dict[str, Any]) -> protocol.ProtocolError:
    try:
        service.dispatch("tasks", args)
    except protocol.ProtocolError as exc:
        return exc
    raise AssertionError(f"tasks {args} was not refused")


def store_image(service) -> dict[str, list[tuple]]:
    """Every row of the tables a refused hold must leave alone."""
    tables = ("leases", "jobs", "attempts", "events", "closures")
    return {table: [tuple(row) for row in service.store.query(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables}


# --- a fresh daemon per example, for the property and differential tests ------------------------

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest


@contextlib.contextmanager
def fresh_daemon(*, lanes: int = 2, caps: dict | None = None):
    """`state_daemon` and `fake_clock`, built inside one Hypothesis example and torn down after it."""
    from subfleet import daemon as daemon_module
    from subfleet.adapters.registry import register
    from subfleet.daemon import Daemon
    from subfleet.procs import Containment
    from tests.fake.conftest import Harness
    from tests.fake.test_admission_latency import add_codex_lanes
    from tests.fake.test_admission_ramp import fake_clock
    from tests.fake_adapter import FakeAdapter

    base = Path(tempfile.mkdtemp(prefix="sf-th-"))
    root = base / "state"
    root.mkdir()
    service = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            harness = Harness(root)
            patch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
            patch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
            patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
            patch.setattr(daemon_module.procs, "snapshot", lambda: daemon_module.procs.ProcessTable(
                {os.getpid(): (1, os.getpgrp(), "S", "unit-test-start")}, "unit-test-boot"))
            patch.setattr(daemon_module.procs, "identity", lambda pid: daemon_module.procs.snapshot().identity(pid))
            patch.setattr(daemon_module.procs, "group_members", lambda *args: {})
            patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
            patch.setattr(daemon_module, "git_head", lambda *args, **kwargs: None)
            patch.setattr(daemon_module, "git_toplevel", lambda *args, **kwargs: None)
            signals: list[tuple] = []
            patch.setattr(daemon_module.os, "killpg", lambda *args: signals.append(("killpg", *args)))
            patch.setattr(daemon_module.os, "kill", lambda *args: signals.append(("kill", *args)))
            register("codex", FakeAdapter)
            clock = fake_clock(patch)
            service = Daemon(harness.root)

            def refuse_real_launch(*args):
                raise AssertionError("state-only fixtures must never launch a guardian")
            patch.setattr(service, "_launch", refuse_real_launch)
            add_codex_lanes(service, *(f"codex-{n}" for n in range(2, lanes + 1)))
            if caps:
                configure(service, harness, lambda daemon: daemon.policy["caps"].update(caps))
            yield service, harness, clock, signals
            service.close()
            close_successors(harness)
            service = None
            harness.check_notices()
    finally:
        if service is not None:
            service.close()
            close_successors(harness)
        if "/sf-" in str(base):
            shutil.rmtree(base, ignore_errors=True)
