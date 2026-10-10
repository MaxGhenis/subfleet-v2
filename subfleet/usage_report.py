"""Read-only per-attempt token reports (C-17.8, C-12.10).

Only reported numbers enter sums; an absent observation stays null. Thread
totals marked cumulative are visible but never charged to their attempt.
For every accepted usage record, 0 <= cache_read <= prompt and cache_write <=
prompt; sums equal the per-attempt rows, and share is in [0, 1] or null. Share
requires prompt and cache_read coverage for every attempt contributing usage.
No backfill writes an evidence row, an artifact or any other store record.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .protocol import ProtocolError
from .state_files import open_state, read_state

FIELDS = ("prompt", "cache_read", "cache_write", "output")
GROUPS = ("lane", "conversation", "session")


def _instant(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def parse_since(since: str, *, now: datetime | None = None) -> datetime:
    """C-17.8: positive durations (`24h`, `7d`) or an ISO clock, in UTC."""
    if not isinstance(since, str) or not since.strip():
        raise ProtocolError("usage: since must be a duration such as 24h or 7d, or an ISO timestamp")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    value = since.strip()
    duration = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([smhdw])", value)
    try:
        if duration:
            seconds = float(duration[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[duration[2]]
            if seconds <= 0:
                raise ValueError
            return now - timedelta(seconds=seconds)
        return _instant(value)
    except (ValueError, OverflowError):
        raise ProtocolError("usage: since must be a positive duration such as 24h or 7d, or an ISO timestamp") from None


def _object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            pass
    return {}


def usage_of(attempt: dict) -> dict | None:
    """C-17.8: stored evidence and already materialized report rows share one read."""
    usage = attempt.get("usage")
    if not isinstance(usage, dict):
        usage = _object(attempt.get("evidence_json")).get("usage")
    return usage if isinstance(usage, dict) and usage else None


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def attempt_values(attempt: dict) -> dict:
    """C-17.8: cumulative thread totals cannot masquerade as attempt numbers."""
    usage = usage_of(attempt)
    normalized = _object(usage.get("normalized")) if usage and not usage.get("cumulative_thread") else {}
    return {field: _number(normalized.get(field)) for field in FIELDS}


def aggregate_usage(attempts: Iterable[dict]) -> dict:
    """C-17.8/C-18.5: sums match attempt rows, missing fields stay null.

    Cache shares require complete paired prompt/read coverage. A cumulative
    record is an observed usage record, but supplies no attempt token fields.
    """
    rows = list(attempts)
    usages = [usage_of(row) for row in rows]
    values = [attempt_values(row) for row in rows]
    coverage = {field: sum(value[field] is not None for value in values) for field in FIELDS}
    totals = {field: sum(value[field] for value in values if value[field] is not None)
              if coverage[field] else None for field in FIELDS}
    contributing = sum(bool(usage) and not usage.get("cumulative_thread") for usage in usages)
    prompt, read = totals["prompt"], totals["cache_read"]
    share = (read / prompt if prompt is not None and prompt > 0 and read is not None
             and 0 <= read <= prompt and coverage["prompt"] == coverage["cache_read"] == contributing else None)
    measured = sum(usage is not None for usage in usages)
    return {"attempts": len(rows), "attempts_with_usage": measured, "attempts_without_usage": len(rows) - measured,
            "cumulative_thread_attempts": sum(bool(usage) and bool(usage.get("cumulative_thread")) for usage in usages),
            "backfilled": any(row.get("backfilled") for row in rows),
            "backfilled_attempts": sum(bool(row.get("backfilled")) for row in rows),
            **totals, "cache_hit_share": share, "field_attempts": coverage}


def _read_json(path: Path) -> dict:
    try:
        return _object(read_state(path, limit=1 << 20).decode("utf-8"))
    except (OSError, UnicodeError):
        return {}


def _backfill(attempt: dict, root: Path, artifacts: list[dict]) -> dict | None:
    """C-17.8: parse one retained provider stream with C-12.10's same parser."""
    from .usage import parse_usage
    adir = root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
    paths = [Path(row["path"]) for role in ("raw-stream", "stdout") for row in artifacts
             if row.get("role") == role and row.get("path")]
    paths.extend((adir / "stream.jsonl", adir / "raw-stream", adir / "stdout"))
    turn = _read_json(adir / "turn.json")
    launch = _read_json(adir / "launch.json")
    argv = launch.get("argv")
    resumed = (attempt.get("kind") == "resume" or
               (isinstance(argv, list) and any(part in ("resume", "--resume") for part in argv)))
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            with open_state(path, "r", encoding="utf-8", errors="replace") as stream:
                usage = parse_usage(stream, attempt["provider"], resumed=resumed,
                                    turn_id=turn.get("turn_id"))
        except OSError:
            continue
        if usage is not None:
            return usage
    return None


def build_report(store, root: Path, *, since: str = "24h", by: str = "lane", backfill: bool = False,
                 now: datetime | None = None) -> dict:
    """C-17.8: finalized or live attempts in one window, never a store mutation.

    The attempt's finished clock (otherwise started/reserved clock) selects it.
    Conversation groups include turn jobs only; session groups use provider and
    native session id, isolating an unbound attempt instead of merging strangers.
    """
    if by not in GROUPS:
        raise ProtocolError("usage: by must be lane, conversation or session")
    if not isinstance(backfill, bool):
        raise ProtocolError("usage: backfill must be true or false")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    since_at = parse_since(since, now=now)
    clock = lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    rows = store.query(
        "SELECT a.*, j.kind, l.provider FROM attempts a JOIN jobs j USING(job_id) JOIN lanes l USING(lane_id) "
        "WHERE julianday(COALESCE(a.finished_at,a.started_at,a.reserved_at))>=julianday(?) "
        "AND julianday(COALESCE(a.finished_at,a.started_at,a.reserved_at))<=julianday(?) "
        "ORDER BY a.reserved_at,a.attempt_id", (clock(since_at), clock(now)))
    artifacts: dict[str, list[dict]] = {}
    if backfill:
        for artifact in store.query("SELECT attempt_id,role,path FROM artifacts WHERE role IN ('raw-stream','stdout')"):
            artifacts.setdefault(artifact["attempt_id"], []).append(artifact)
    manifests: dict[str, dict] = {}
    attempts, grouped = [], {}
    for source in rows:
        row = dict(source)
        manifest = {}
        if by == "conversation" or backfill:
            if row["job_id"] not in manifests:
                manifests[row["job_id"]] = _read_json(root / "jobs" / row["job_id"] / "manifest.json")
            manifest = manifests[row["job_id"]]
        conversation = _object(manifest.get("turn")).get("conversation_id")
        if by == "conversation" and row["kind"] != "turn":
            continue
        usage = usage_of(row)
        recovered = False
        if usage is None and backfill:
            usage = _backfill(row, root, artifacts.get(row["attempt_id"], []))
            recovered = usage is not None
        group_id = (row["lane_id"] if by == "lane" else
                    str(conversation or f"unknown:{row['job_id']}") if by == "conversation" else
                    f"{row['provider']}:{row['native_session_id']}" if row.get("native_session_id") else
                    f"unknown:{row['attempt_id']}")
        attempt = {"attempt_id": row["attempt_id"], "job_id": row["job_id"], "lane_id": row["lane_id"],
                   "provider": row["provider"], "native_session_id": row.get("native_session_id"),
                   "conversation_id": conversation, "group": group_id, "usage": usage, "backfilled": recovered,
                   "source": "backfilled" if recovered else "stored" if usage else "missing"}
        attempt.update(attempt_values(attempt))
        prompt, read = attempt["prompt"], attempt["cache_read"]
        attempt["cache_hit_share"] = (read / prompt if prompt is not None and prompt > 0
                                      and read is not None and read <= prompt else None)
        attempts.append(attempt)
        grouped.setdefault(group_id, []).append(attempt)
    report = [{"group": group_id, **aggregate_usage(members)} for group_id, members in sorted(grouped.items())]
    return {"since": clock(since_at), "until": clock(now), "by": by, "backfill": backfill,
            "rows": report, "attempt_rows": attempts, "totals": aggregate_usage(attempts)}
