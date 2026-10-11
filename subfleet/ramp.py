"""Restart-safe placement pacing for detached attempts (C-6.18).

The attempt ledger supplies the rolling window once per detached pass. A
committed placement is added immediately, so neither a long pass nor a daemon
restart can grant an extra burst. No durable bucket or lane lease is needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .policy import ramp_settings

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = 1_000_000
_EXEMPT = frozenset({"attended", "turn", "probe"})


def _tick(stamp: str) -> int:
    """Integer microseconds preserve the exact open-left window boundary."""
    instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.astimezone()
    delta = instant.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * _US + delta.microseconds


def _stamp(tick: int) -> str:
    try:
        instant = _EPOCH + timedelta(microseconds=tick)
    except OverflowError:
        # Policy integers have no artificial upper bound. Beyond the clock's
        # representable horizon, retain a valid future timestamp for reporting
        # and timers; the full integer window still governs admission.
        instant = datetime.max.replace(tzinfo=timezone.utc)
    return instant.isoformat().replace("+00:00", "Z")


class PlacementRamp:
    def __init__(self) -> None:
        self.settings = ramp_settings({})
        self.reservations: dict[str, int] = {}
        self.snapshot: dict[str, Any] = self._snapshot(0)

    def begin_pass(self, policy: Mapping[str, Any], attempts: Iterable[Mapping[str, Any]], now: str) -> None:
        """Reconstruct all detached starts, including ended and retry attempts.

        An absent or disabled rule never consumes the attempt iterable or
        parses its clock. Attended admission therefore needs no ledger read.
        """
        self.settings = ramp_settings(policy)
        if not self.settings["enabled"]:
            self.reservations = {}
            self.snapshot = self._snapshot(0)
            return
        clock = _tick(now)
        reservations = {}
        for row in attempts:
            if row.get("kind") in ("turn", "probe"):
                continue
            when = _tick(row["reserved_at"])
            # Preserve every supplied row for this pass: an expired row can
            # become relevant again if a later look uses a backwards clock.
            reservations[row["attempt_id"]] = when
        self.reservations = reservations
        self.snapshot = self._snapshot(clock)

    def reserve(self, attempt_id: str, reserved_at: str, klass: str = "dispatch") -> None:
        """Count a committed detached attempt once; priority starts also count."""
        if not self.settings["enabled"] or klass in _EXEMPT:
            return
        clock = _tick(reserved_at)
        self.reservations = {**self.reservations, attempt_id: clock}
        self.snapshot = self._snapshot(clock)

    def hold(self, klass: str, now: str) -> dict[str, Any] | None:
        """Priority may pass a full window, while attended/probe work bypasses it."""
        if not self.settings["enabled"] or klass in _EXEMPT:
            return None
        clock = _tick(now)
        self.snapshot = self._snapshot(clock)
        if klass == "priority" or not self.snapshot["holding"]:
            return None
        return {"reason": "ramp", **self.snapshot}

    def status(self, now: str) -> dict[str, Any]:
        """Refresh the published window without reading attempts or mutating it."""
        return self._snapshot(_tick(now)) if self.settings["enabled"] else dict(self.snapshot)

    def _snapshot(self, clock: int) -> dict[str, Any]:
        settings = self.settings
        product = settings["per_minute"] * settings["window_s"]
        # The loader refuses a fractional allowance. Raw settings that skip it
        # keep the literal comparison, which admits the allowance's ceiling.
        if product % 60 == 0:
            limit = product // 60
        else:
            try:
                limit = product / 60
            except OverflowError:
                limit = f"{product}/60"
        cutoff = clock - settings["window_s"] * _US
        # Future-dated starts also consume slots: a backwards wall-clock step
        # must make admission stricter, including after restart. Keep this pass's
        # history; the daemon rebuilds from durable rows at reservation time too.
        starts = sorted(when for when in self.reservations.values() if cutoff < when)
        used = len(starts) if settings["enabled"] else 0
        holding = settings["enabled"] and used * 60 >= product
        next_slot_at = None
        if holding:
            # Priority can take usage above the quota. The next usable slot
            # opens after enough starts expire to make the literal comparison
            # `used < per_minute * window_s / 60` true again.
            expires_needed = used - (product - 1) // 60
            next_slot_at = _stamp(starts[expires_needed - 1] + settings["window_s"] * _US)
        return {"enabled": settings["enabled"], "per_minute": settings["per_minute"],
                "window_s": settings["window_s"], "used": used, "limit": limit,
                "holding": holding, "next_slot_at": next_slot_at}
