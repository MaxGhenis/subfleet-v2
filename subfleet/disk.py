"""Disk admission and provisional placement reservations (C-6.17).

Only the detached pass writes this state. Published snapshots are replaced
whole, so status and why can read them alongside admission without taking a
lock. A store reconciliation at each pass releases ended attempts and rebuilds
the same reservations after a restart; no new durable table is needed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .policy import disk_settings

GB = 1_000_000_000
LIVE = frozenset({"reserved", "starting", "running", "finalizing"})


def free_bytes(path: str | Path) -> int:
    """Decimal GB, available to this user, matching subfleet-disk-hold."""
    reading = os.statvfs(path)
    return reading.f_bavail * reading.f_frsize


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


class DiskAdmission:
    def __init__(self, state_root: Path, *, read_free: Callable[[str | Path], int] | None = None,
                 holding: bool = False):
        self.state_root = state_root
        self.read_free = read_free
        self.settings = disk_settings({})
        self.reservations: dict[str, tuple[float, int]] = {}
        self.measured: int | None = None
        self.holding = holding
        self.error: str | None = None
        self.snapshot: dict[str, Any] = self._snapshot()

    @property
    def reserved_bytes(self) -> int:
        return sum(amount for _, amount in self.reservations.values())

    def _bytes(self, name: str) -> int:
        """A setting in whole bytes. Every comparison is in integers, so a
        fractional setting cannot put a placement a rounding error past the
        floor or refuse one at the exact progress threshold (review of #161, P3)."""
        return round(self.settings[name] * GB)

    def rebuild(self, attempts: Iterable[Mapping[str, Any]], now: str) -> None:
        """Reservations start at placement, even while the attempt awaits launch.

        `finished_at` releases a finalizing attempt as soon as its execution
        ends. Older attempts without disk evidence use the configured budget,
        so enabling the rule also accounts for recent existing placements.
        """
        self.reservations = self._reservations_for(attempts, now)
        self.snapshot = self._snapshot()

    def _reservations_for(self, attempts: Iterable[Mapping[str, Any]], now: str) -> dict[str, tuple[float, int]]:
        clock = epoch(now)
        current = {}
        for row in attempts:
            if row["state"] not in LIVE or row.get("finished_at") or row.get("kind") == "turn":
                continue
            if "disk_reservation" in row:
                reservation = json.loads(row["disk_reservation"] or "null") or self.evidence()
            else:
                evidence = json.loads(row.get("evidence_json") or "{}")
                reservation = evidence.get("disk_reservation") or self.evidence()
            expires = epoch(row["reserved_at"]) + reservation["ttl_s"]
            if expires > clock:
                current[row["attempt_id"]] = (expires, round(reservation["bytes"]))
        return current

    def status(self, attempts: Iterable[Mapping[str, Any]], now: str) -> dict[str, Any]:
        """Use the pass's measurement, but report ends and expiry immediately.

        Status never writes the detached pass's mutable reservation map.
        """
        snapshot = self.snapshot
        if not snapshot["enabled"]:
            return dict(snapshot)
        current = self._reservations_for(attempts, now)
        return {**snapshot, "reserved_gb": sum(amount for _, amount in current.values()) / GB}

    def begin_pass(self, policy: Mapping[str, Any], attempts: Iterable[Mapping[str, Any]], now: str) -> None:
        self.settings = disk_settings(policy)
        if not self.settings["enabled"]:
            self.reservations = {}
            self.measured, self.error, self.holding = None, None, False
        else:
            self.rebuild(attempts, now)
            try:
                self.measured = (self.read_free or free_bytes)(self.settings["path"] or self.state_root)
                self.error = None
            except OSError as exc:
                self.measured = None
                self.error = f"{type(exc).__name__}: {exc}"
        self.snapshot = self._snapshot()

    def evidence(self) -> dict[str, float]:
        return {"bytes": self._bytes("placement_reserve_gb"),
                "ttl_s": self.settings["reserve_ttl_s"]}

    def reserve(self, attempt_id: str, reserved_at: str) -> None:
        if self.settings["enabled"]:
            budget = self.evidence()
            self.reservations[attempt_id] = (epoch(reserved_at) + budget["ttl_s"], budget["bytes"])
            self.snapshot = self._snapshot()

    def hold(self, klass: str) -> dict[str, Any] | None:
        """Priority is subject to the floor; attended turns and probes are exempt."""
        if not self.settings["enabled"] or klass in ("attended", "probe"):
            return None
        effective = None if self.measured is None else self.measured - self.reserved_bytes
        floor = self._bytes("floor_gb")
        resume = floor + self._bytes("resume_margin_gb")
        if effective is None or (self.holding and effective < resume):
            self.holding = True
        else:
            self.holding = effective - self._bytes("placement_reserve_gb") < floor
        self.snapshot = self._snapshot()
        return {"reason": "disk", **self.snapshot} if self.holding else None

    def _snapshot(self) -> dict[str, Any]:
        return {"enabled": self.settings["enabled"],
                "free_gb": None if self.measured is None else self.measured / GB,
                "reserved_gb": self.reserved_bytes / GB,
                "floor_gb": self.settings["floor_gb"],
                "resume_margin_gb": self.settings["resume_margin_gb"],
                "placement_reserve_gb": self.settings["placement_reserve_gb"],
                "holding": self.holding, "path": str(self.settings["path"] or self.state_root),
                **({"error": self.error} if self.error else {})}
