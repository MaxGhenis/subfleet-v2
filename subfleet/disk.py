"""Disk admission and provisional placement reservations (C-6.17).

Only the detached pass writes this state. Published snapshots are replaced
whole, so status and why can read them alongside admission without taking a
lock. A store reconciliation at each pass releases ended attempts and rebuilds
the same reservations after a restart; no new durable table is needed.
"""
from __future__ import annotations

import errno
import json
import math
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .policy import disk_settings
from .retention_fs import O_FILE, same_content_signature

GB = 1_000_000_000
LIVE = frozenset({"reserved", "starting", "running", "finalizing"})
RULING_LIMIT = 16 * 1024
RulingReader = Callable[[str | Path], tuple[bytes, float]]


def read_ruling(path: str | Path) -> tuple[bytes, float]:
    """Bounded nonblocking, no-follow read and mtime from the same descriptor.

    Reject special files before reading, and concurrent edits rather than
    pairing one version's bytes with another version's writing time.
    """
    fd = os.open(path, O_FILE)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        if before.st_size > RULING_LIMIT:
            raise OSError(errno.EFBIG, "ruling file too large", str(path))
        data = os.read(fd, RULING_LIMIT + 1)
        if len(data) > RULING_LIMIT:
            raise OSError(errno.EFBIG, "ruling file too large", str(path))
        if len(data) != before.st_size or not same_content_signature(before, os.fstat(fd)):
            raise OSError(errno.EAGAIN, "ruling changed while reading", str(path))
        return data, before.st_mtime
    finally:
        os.close(fd)


def _until(value: Any) -> datetime:
    until = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return until.astimezone() if until.tzinfo is None else until


def floor_ruling(settings: Mapping[str, Any], now: str, reader: RulingReader) -> dict[str, Any]:
    """The external agent's floor_gb/lower_override/pass_once precedence.

    Keep its numeric and ruling coercions, including drop-field validation.
    Drop values never trigger native holds: placement reservations supply pacing.
    """
    clock = datetime.fromtimestamp(epoch(now), timezone.utc)
    info: dict[str, Any] = {"floor_gb": settings["floor_gb"],
                            "resume_margin_gb": settings["resume_margin_gb"], "floor_source": "policy"}
    live: dict[str, dict] = {}
    for name, key in (("lower", "lower_path"), ("override", "raise_path")):
        if settings[key] is None:
            continue
        try:
            data, mtime = reader(settings[key])
            ov = json.loads(data.decode("utf-8"))
            until = _until(ov["until"])
            floor = float(ov["floor_gb"])
            parsed = {"floor_gb": floor, "until": str(ov["until"]), "why": ov.get("why")}
            if not math.isfinite(floor):
                parsed["floor_gb_raw"] = str(ov["floor_gb"])
            if name == "lower":
                parsed.update(ruling=str(ov.get("ruling") or "").strip(),
                              release_margin_gb=max(0.0, float(ov.get("release_margin_gb", 0.0))))
                if not math.isfinite(parsed["release_margin_gb"]):
                    parsed["resume_margin_gb_raw"] = str(ov["release_margin_gb"])
                if ov.get("drop_gb") is not None:
                    float(ov["drop_gb"])
                float(ov.get("drop_window_min", 10.0))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
            info[name + "_error"] = f"{type(exc).__name__}: {exc}"[:120]
            continue
        if until <= clock or (name == "override" and floor != floor):
            info[name + "_expired"] = str(ov.get("until"))
            continue
        if name == "lower":
            try:
                written = datetime.fromtimestamp(mtime, timezone.utc)
                refused = ((until - min(clock, written)).total_seconds() > settings["max_lower_h"] * 3600
                           or not parsed["ruling"] or not floor >= settings["min_floor_gb"])
            except (ValueError, TypeError, OSError, OverflowError) as exc:
                info["lower_error"] = f"{type(exc).__name__}: {exc}"[:120]
                continue
            if refused:
                info["lower_refused"] = "needs a ruling, floor_gb >= %g and until within %g h" % (
                    settings["min_floor_gb"], settings["max_lower_h"])
                continue
        live[name] = parsed
    lower, raised = live.get("lower"), live.get("override")
    if lower is not None and (raised is None or raised["floor_gb"] <= lower["floor_gb"]):
        info.update(floor_gb=lower["floor_gb"], resume_margin_gb=lower["release_margin_gb"],
                    floor_source=f"lowered until {lower['until']} by {lower['ruling']}", floor_why=lower["why"])
        info.update({key: lower[key] for key in ("floor_gb_raw", "resume_margin_gb_raw") if key in lower})
    elif raised is not None:
        # pass_once uses the raw raise when it beats a live lowering, even
        # below the policy floor. With no lowering, floor_gb only ever raises.
        info.update(floor_gb=raised["floor_gb"] if lower is not None else max(settings["floor_gb"], raised["floor_gb"]))
        if lower is not None or raised["floor_gb"] > settings["floor_gb"]:
            info.update(floor_source=f"raised until {raised['until']}", floor_why=raised["why"])
            if "floor_gb_raw" in raised:
                info["floor_gb_raw"] = raised["floor_gb_raw"]
    return info


def reported_evidence(value: Any) -> Any:
    """Copy ruling evidence into strict JSON values without changing admission.

    Keep non-finite numbers as text and replace lone surrogates in strings,
    including nested why metadata and object keys, for UTF-8 wire readers.
    The original floor/margin and source remain available to the latch.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str):
        return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value)
    if isinstance(value, dict):
        return {reported_evidence(key): reported_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [reported_evidence(item) for item in value]
    return value


def free_bytes(path: str | Path) -> int:
    """Decimal GB, available to this user, matching subfleet-disk-hold."""
    reading = os.statvfs(path)
    return reading.f_bavail * reading.f_frsize


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


class DiskAdmission:
    def __init__(self, state_root: Path, *, read_free: Callable[[str | Path], int] | None = None,
                 holding: bool = False, read_ruling: RulingReader | None = None,
                 recheck_latch: bool = False):
        self.state_root = state_root
        self.read_free = read_free
        self.read_ruling = read_ruling
        self.settings = disk_settings({})
        self.floor_info: dict[str, Any] = {"floor_source": "policy"}
        self._recheck_latch = recheck_latch
        self.reservations: dict[str, tuple[float, int]] = {}
        self.measured: int | None = None
        self.holding = holding
        self.error: str | None = None
        self.snapshot: dict[str, Any] = self._snapshot()

    @property
    def reserved_bytes(self) -> int:
        return sum(amount for _, amount in self.reservations.values())

    def _bytes(self, name: str) -> int | float:
        """A setting in whole bytes. Every comparison is in integers, so a
        fractional setting cannot put a placement a rounding error past the
        floor or refuse one at the exact progress threshold (review of #161, P3)."""
        amount = self.settings[name] * GB
        # The agent accepts +Infinity. It must hold work rather than overflow
        # an integer conversion; policy settings themselves are finite.
        return round(amount) if math.isfinite(amount) else amount

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
        previous = (self.settings["floor_gb"], self.settings["resume_margin_gb"])
        previous_source = self.floor_info["floor_source"]
        had_ruling = previous_source != "policy"
        recheck_latch, self._recheck_latch = self._recheck_latch, False
        self.settings = disk_settings(policy)
        self.floor_info = {"floor_source": "policy"}
        if not self.settings["enabled"]:
            self.reservations = {}
            self.measured, self.error, self.holding = None, None, False
        else:
            self.floor_info = floor_ruling(self.settings, now, self.read_ruling or read_ruling)
            self.settings.update(floor_gb=self.floor_info["floor_gb"],
                                 resume_margin_gb=self.floor_info["resume_margin_gb"])
            self.rebuild(attempts, now)
            try:
                self.measured = (self.read_free or free_bytes)(self.settings["path"] or self.state_root)
                self.error = None
            except OSError as exc:
                self.measured = None
                self.error = f"{type(exc).__name__}: {exc}"
            # Recovery rechecks the saved latch using this pass's effective
            # numbers. A source event need not contain the last same-source edit.
            if (recheck_latch or ((had_ruling or self.settings["lower_path"] or self.settings["raise_path"])
                    and (previous != (self.settings["floor_gb"], self.settings["resume_margin_gb"])
                         or previous_source != self.floor_info["floor_source"]))):
                self.hold("background")
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
        return reported_evidence({"enabled": self.settings["enabled"],
                "free_gb": None if self.measured is None else self.measured / GB,
                "reserved_gb": self.reserved_bytes / GB,
                "floor_gb": self.settings["floor_gb"],
                "resume_margin_gb": self.settings["resume_margin_gb"],
                "placement_reserve_gb": self.settings["placement_reserve_gb"],
                "holding": self.holding, "path": str(self.settings["path"] or self.state_root),
                **self.floor_info,
                **({"error": self.error} if self.error else {})})
