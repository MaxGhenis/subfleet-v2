"""The menu bar's read-only snapshot projection (C-9.1, C-18.1)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .guardian import atomic_publish


def instant(value: str | datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def timestamp(value: str | datetime | None = None) -> str:
    return instant(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def lane_verdict(lane: Mapping[str, Any]) -> str:
    """Keep the cycle's post-heal verdict, falling back to durable evidence."""
    explicit = lane.get("verdict") or lane.get("outcome")
    if isinstance(explicit, str):
        return explicit
    if any(row.get("reason") == "auth-dead" for row in lane.get("closures", ())):
        return "auth-dead"
    if not lane.get("enabled", True):
        return "disabled"
    if any(row.get("scope") == "account" for row in lane.get("closures", ())):
        return "limited"
    readings = lane.get("readings", ())
    if any(row.get("label") == "provider" for row in readings):
        return "ok"
    for label in ("stale-provider", "admission-observed", "local-backoff"):
        if any(row.get("label") == label for row in readings):
            return label
    return "unknown"


def dispatchable(lane: Mapping[str, Any]) -> bool:
    if (not lane.get("enabled", True) or lane.get("owner", "v2") != "v2"
            or lane.get("canonical") is False or lane.get("duplicate_of")
            or lane.get("provider") == "claude" and lane.get("desktop")):
        return False
    if "dispatchable" in lane:
        return bool(lane["dispatchable"])
    return (lane_verdict(lane) in {"ok", "ready", "provider", "stale-provider", "admission-observed"}
            and not any(row.get("scope") == "account" for row in lane.get("closures", ())))


def _windows(lane: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """C-9.1: utilization from any other evidence label is discarded."""
    windows: dict[str, dict[str, Any]] = {}
    for reading in lane.get("readings", ()):
        if reading.get("scope") != "account":
            continue
        key = reading["window"]
        label = reading.get("label", "unknown")
        result = {"status": label, "source": reading.get("source"),
                  "as_of": reading.get("observed_at"), "age_s": reading.get("age_s"),
                  "stale": label == "stale-provider"}
        utilization = reading.get("utilization")
        if (label in {"provider", "stale-provider"}
                and isinstance(utilization, (int, float)) and not isinstance(utilization, bool)
                and math.isfinite(utilization) and 0 <= utilization <= 1):
            result.update(used_percent=utilization * 100, reset_at=reading.get("resets_at"))
        windows[key] = result
    return windows


def build_status(snapshot: Mapping[str, Any], *, now: str | datetime | None = None) -> dict[str, Any]:
    """C-18.1: retain Swift's Codex/Claude JSON shape with explicit evidence labels."""
    codex, claude = [], []
    for lane in snapshot.get("lanes", ()):
        verdict, windows = lane_verdict(lane), _windows(lane)
        common = {"lane_id": lane["lane_id"], "verdict": verdict,
                  "enabled": bool(lane.get("enabled", True)), "owner": lane.get("owner", "v2"),
                  "dispatchable": dispatchable(lane)}
        if lane.get("identity_status") is not None:
            common["identity_status"] = lane["identity_status"]
        email = lane.get("email") or str(lane.get("account_key", "unknown")).partition(":")[2] or lane.get("account_key", "unknown")
        if lane["provider"] == "codex":
            if verdict == "auth-dead":
                # Swift's existing auth warning recognizes auth-revoked; keep
                # the daemon's stronger outcome alongside that display alias.
                common.update(verdict="auth-revoked", outcome="auth-dead")
            # v1 primary/secondary are compatibility aliases of duration keys;
            # provider slot order is never an authority (C-9.7).
            codex_windows = dict(windows)
            for source, alias in (("five_hour", "primary"), ("seven_day", "secondary"), ("seven_day", "weekly")):
                if source in windows:
                    codex_windows[alias] = windows[source]
            numeric = [row for row in windows.values() if "used_percent" in row]
            codex_windows["source"] = ("stale-provider" if any(row["stale"] for row in numeric)
                                       else "provider" if numeric else verdict)
            codex_windows["stale"] = any(row["stale"] for row in numeric)
            codex_windows["as_of"] = max((row["as_of"] for row in numeric if row["as_of"]), default=None)
            counts = lane.get("reset_credits") or (lane.get("probe") or {}).get("reset_credits") or {}
            credit_count = lane.get("reset_credits_remaining", counts.get("available"))
            codex.append({**common, "home": lane.get("home") or lane.get("credential_ref") or lane["lane_id"],
                          "email": email, "windows": codex_windows, "duplicate_of": lane.get("duplicate_of"),
                          "account_key": lane.get("account_key", lane["lane_id"]),
                          "app_shadowed": bool(lane.get("app_shadowed", False)),
                          "reset_credits_remaining": credit_count})
        elif lane["provider"] == "claude":
            probe: dict[str, Any] = {"status": verdict}
            live: dict[str, Any] = {"source": verdict, "stale": False}
            for key in ("five_hour", "seven_day"):
                if key not in windows:
                    continue
                row = dict(windows[key])
                if row.get("reset_at"):
                    row["reset_at"] = instant(row["reset_at"]).timestamp()
                probe[key] = row
                if "used_percent" in row:
                    live[f"{key}_pct"] = row["used_percent"]
                    live["stale"] = live["stale"] or row["stale"]
                    live["source"] = "stale-provider" if live["stale"] else "provider"
            claude.append({**common, "email": str(email), "active": bool(lane.get("desktop", False)),
                           "enrolled": bool(lane.get("enabled", True)), "probe": probe, "live": live,
                           "oauth_status": verdict})
    available = [lane for lane in codex if lane["dispatchable"]]
    reset_times = [window["reset_at"] for lane in codex for key, window in lane["windows"].items()
                   if key in {"five_hour", "seven_day"} and window.get("reset_at")]
    accounts = {lane["account_key"]: lane for lane in codex if not lane.get("duplicate_of")}
    credit_counts = [lane["reset_credits_remaining"] for lane in accounts.values()]
    credits = (sum(credit_counts) if credit_counts and all(isinstance(count, int) and not isinstance(count, bool) and count >= 0
                                       for count in credit_counts) else None)
    return {"generated_at": timestamp(now or snapshot.get("now")), "offline": bool(snapshot.get("offline", False)),
            "codex": {"homes": codex, "fleet": {"total_homes": len(codex), "dispatchable_now": len(available),
                       "best_home": available[0]["home"] if available else None,
                       "earliest_reset": min(reset_times, default=None), "reset_credits_remaining": credits}},
            "claude": {"accounts": claude, "lanes": {"enrolled": sum(row["enrolled"] for row in claude),
                        "dispatchable_now": sum(row["dispatchable"] for row in claude)}}}


def write_status(root: str | Path, snapshot: Mapping[str, Any], *,
                 now: str | datetime | None = None) -> dict[str, Any]:
    """C-8.1, C-18.1: publish one complete post-heal snapshot per probe cycle."""
    result = build_status(snapshot, now=now)
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_publish(root / "status.json", (json.dumps(result, sort_keys=True, allow_nan=False) + "\n").encode())
    return result
