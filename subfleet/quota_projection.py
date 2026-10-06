"""Pure weekly-quota forecasts from provider utilization observations."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal


def instant(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Projection:
    used: float
    projected_unused: float
    rate_per_hour: float | None
    resets_at: str
    basis: Literal["trend", "rate unknown"]


def project_unused(samples: Iterable[tuple[str | datetime, float] | Mapping[str, Any]],
                   resets_at: str | datetime, now: str | datetime) -> Projection | None:
    """Forecast unused quota at reset, independent of sample order.

    Pairs belong to the supplied weekly window. Full reading rows are also
    accepted, filtering on seven_day, provider/stale-provider and reset instant;
    callers must keep lanes and scopes separate. Future observations are ignored.
    The newest observation supplies `used`, even when older than 24 hours.

    Use the first-to-last slope over the trailing 24 hours: net utilization
    gained is simple to audit and duplicate polls cannot weight the trend.
    Less than two observations or one hour of span means rate unknown (None).
    A decreasing utilization is a zero rate, never quota gained back.

    For the forecast, advance utilization along the trend from its observation
    to now, then apply clamp(1 - u_now - rate * hours_to_reset, 0, 1 - u_now).
    Algebraically the horizon starts at the latest observation. This avoids
    floating cancellation and a forecast increasing solely because time passed.
    `used` remains the provider's reported fraction, not extrapolated usage.
    """
    reset, at = instant(resets_at), instant(now)
    if reset <= at:
        return None
    observations = []
    for sample in samples:
        if isinstance(sample, Mapping):
            if (sample.get("window") != "seven_day"
                    or sample.get("label") not in {"provider", "stale-provider"}):
                continue
            try:
                if instant(sample.get("resets_at")) != reset:
                    continue
            except (AttributeError, TypeError, ValueError):
                continue
            observed_at, utilization = sample.get("observed_at"), sample.get("utilization")
        else:
            observed_at, utilization = sample
        if (not isinstance(utilization, (int, float)) or isinstance(utilization, bool)
                or not math.isfinite(utilization) or not 0 <= utilization <= 1):
            continue
        try:
            observed = instant(observed_at)
        except (AttributeError, TypeError, ValueError):
            continue
        if observed <= at:
            observations.append((observed, float(utilization)))
    if not observations:
        return None
    # At equal timestamps, use the greater utilization deterministically.
    by_time = {}
    for observed, utilization in observations:
        by_time[observed] = max(utilization, by_time.get(observed, utilization))
    ordered = sorted(by_time.items())
    latest_at, used = ordered[-1]
    recent = [(observed, utilization) for observed, utilization in ordered
              if observed >= at - timedelta(hours=24)]
    rate = None
    if len(recent) >= 2:
        span = (recent[-1][0] - recent[0][0]).total_seconds() / 3600
        if span >= 1:
            rate = max(0.0, (recent[-1][1] - recent[0][1]) / span)
    hours = (reset - latest_at).total_seconds() / 3600
    unused = min(1 - used, max(0.0, 1 - used - (rate or 0.0) * hours))
    return Projection(used, unused, rate, reset.isoformat().replace("+00:00", "Z"),
                      "rate unknown" if rate is None else "trend")


def weekly_projections(lane: Mapping[str, Any], *, now: str | datetime,
                       samples: Iterable[Mapping[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Forecast current weekly scopes separately, keeping reported usage numeric."""
    readings = lane.get("readings", ())
    history = list(samples) if samples is not None else readings
    result = {}
    for row in readings:
        used = row.get("utilization")
        if (row.get("window") != "seven_day" or row.get("label") not in {"provider", "stale-provider"}
                or not row.get("resets_at") or not isinstance(used, (int, float))
                or isinstance(used, bool) or not math.isfinite(used) or not 0 <= used <= 1):
            continue
        scope = row.get("scope", "account")
        try:
            observed = instant(row["observed_at"])
            if observed > instant(now):
                continue
            matching = []
            for sample in history:
                if (sample.get("lane_id") == lane["lane_id"]
                        and sample.get("scope", "account") == scope):
                    try:
                        # The snapshot selected its current row by reading id
                        # too; an older poll at the same instant cannot replace it.
                        if instant(sample["observed_at"]) < observed:
                            matching.append(sample)
                    except (AttributeError, KeyError, TypeError, ValueError):
                        continue
            projection = project_unused([*matching, row], row["resets_at"], now)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if projection is not None:
            result[scope] = asdict(projection)
    return result
