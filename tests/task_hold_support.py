"""Helpers for the task-hold tests (C-11.10, D-TASKHOLD1): an in-process daemon on
a virtual clock, its holds, and what admission placed.

The daemon is `tests.fake.test_state_contract.state_daemon`'s: fake adapters,
stubbed process identity, no guardian and no provider, so a placement is an
attempt row in `reserved` and nothing ever launches.
"""

from __future__ import annotations

from typing import Any

from subfleet import protocol
from tests.disk_floor_model import stamp
from tests.fake.test_admission_latency import submit_turn

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
                service.policy["caps"].update(caps)
            yield service, harness, clock, signals
            service.close()
            service = None
            harness.check_notices()
    finally:
        if service is not None:
            service.close()
        if "/sf-" in str(base):
            shutil.rmtree(base, ignore_errors=True)
