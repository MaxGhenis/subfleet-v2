"""Frozen machine_hold from 473fe2941, the D-MG2 legacy differential oracle.

LEGACY_GUARD keeps load-era admission fixtures explicit when the proposal changes.
"""
from collections.abc import Mapping
from typing import Any
from subfleet.policy import MEMORY_PRESSURE_LEVELS, admission_settings

LEGACY_GUARD = {
    "background": {"load_per_cpu": 6.0, "memory_pressure": "warn"},
    "session": {"load_per_cpu": 10.0, "memory_pressure": "critical"},
}

def machine_hold(policy: Mapping[str, Any], machine: Mapping[str, Any] | None, klass: str) -> dict[str, Any] | None:
    """C-6.13: the `machine-busy` hold for a job of `klass`, or None to go on.

    A class's threshold is met while the larger of the 1- and 5-minute load
    averages per logical CPU is at or above its `load_per_cpu`, or the kernel's
    memory pressure is at or above its `memory_pressure`. The larger average
    holds quickly and lets go slowly, so a dip does not release a burst. An
    `attended` turn or `priority` job is never held, nor is any class the guard
    does not name, and nothing is held on a reading that is missing.
    """
    guard = admission_settings(policy).get("machine_guard")
    limits = (guard or {}).get(klass) if klass not in ("attended", "priority") else None
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
