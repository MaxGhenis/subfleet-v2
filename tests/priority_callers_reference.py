"""Frozen admission functions from 6b0be1d471e9 for differential properties.

These are the scheduler functions before C-6.15, copied without changes.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from typing import Any
from subfleet.policy import MEMORY_PRESSURE_LEVELS, admission_settings
from subfleet.scheduler import Liveness, _row

PRIORITY_CLASSES = ("attended", "session", "background")

def priority_class(job: Any, live: Liveness | None = None) -> str:
    """C-6.9: a job's class, from what it records and who is live now.

    A turn is `attended`. A gate round is `session`: a gate is always waited on
    by the `subfleet gate` that asked for it. Any other job is `session` while its
    caller's Claude Code session is live (a validated registry row names
    `caller_session`) or its parent job is unfinished; otherwise `background`. A
    caller's pid alone is not evidence: a live pid proves a process, not the
    caller. With no liveness to read (`live` None) every detached job is
    `session`, which orders as before.
    """
    job = _row(job)
    kind = job.get("kind")
    if kind == "turn":
        return "attended"
    if kind == "gate-review" or live is None:
        return "session"
    session = str(job.get("caller_session") or "").strip().lower()
    if session and session in live.sessions:
        return "session"
    if job.get("parent_job_id") and job["parent_job_id"] in live.jobs:
        return "session"
    return "background"


def ordered_jobs(policy: Mapping[str, Any], jobs: Iterable[Any], live: Liveness | None = None) -> list[dict[str, Any]]:
    """C-4.1, C-6.9, C-26.9; plan amendment 11: class, then tier, then FIFO.

    Classes go `attended`, `session`, `background` (`priority_class`); tiers
    follow the policy's declared order; within both, oldest first. Stable
    sorting preserves the store's submission order when second-precision
    timestamps are tied.
    """
    tiers = policy["tiers"]
    default = "standard" if "standard" in tiers else tiers[0]
    rank = {tier: index for index, tier in enumerate(tiers)}
    classes = {name: index for index, name in enumerate(PRIORITY_CLASSES)}
    return sorted((_row(job) for job in jobs), key=lambda job: (
        classes[priority_class(job, live)], rank.get(job.get("tier") or default, len(tiers)),
        job.get("created_at") or ""))


def machine_hold(policy: Mapping[str, Any], machine: Mapping[str, Any] | None, klass: str) -> dict[str, Any] | None:
    """C-6.13: the `machine-busy` hold for a job of `klass`, or None to go on.

    A class's threshold is met while the larger of the 1- and 5-minute load
    averages per logical CPU is at or above its `load_per_cpu`, or the kernel's
    memory pressure is at or above its `memory_pressure`. The larger average
    holds quickly and lets go slowly, so a dip does not release a burst. A turn
    is never held, nor is any class the guard does not name, and nothing is
    held on a reading that is missing.
    """
    guard = admission_settings(policy).get("machine_guard")
    limits = (guard or {}).get(klass) if klass != "attended" else None
    if not limits or not machine:
        return None
    hold: dict[str, Any] = {}
    loads = [value for value in (machine.get("load1"), machine.get("load5")) if isinstance(value, (int, float))]
    cpus = machine.get("cpus")
    threshold = limits.get("load_per_cpu")
    if threshold is not None and loads and isinstance(cpus, int) and cpus > 0:
        per_cpu = round(max(loads) / cpus, 2)
        if per_cpu >= threshold:
            hold.update(load_per_cpu=per_cpu, load_threshold=threshold)
    level = machine.get("memory_pressure")
    named = limits.get("memory_pressure")
    if named is not None and isinstance(level, int) and level >= MEMORY_PRESSURE_LEVELS[named]:
        hold.update(memory_pressure=level, memory_threshold=named)
    return {"reason": "machine-busy", "class": klass, **hold} if hold else None
