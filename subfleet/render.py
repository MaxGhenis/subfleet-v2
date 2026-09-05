"""Human-readable, side-effect-free status and decisions (C-9.1, C-11.5)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from .capacity import ACTIVE_ATTEMPT_STATES, build_view
from .contracts import READING_TTL_S


def _row(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else dict(value)


def _label(value: Any) -> str:
    return str(getattr(value, "value", value))


def reading_text(value: Any) -> str:
    """C-9.1: provider numbers are percentages; all other evidence is words."""
    reading = _row(value)
    label = _label(reading.get("label", "unknown"))
    utilization = reading.get("utilization")
    numeric = (label in {"provider", "stale-provider"} and isinstance(utilization, (int, float))
               and not isinstance(utilization, bool) and math.isfinite(utilization) and utilization >= 0)
    result = f"{reading.get('scope', 'account')}/{reading.get('window', 'unknown')} "
    result += f"{utilization * 100:g}% used" if numeric else label
    details = [label] if numeric else []
    if reading.get("source"):
        details.append(f"source={reading['source']}")
    if reading.get("age_s") is not None:
        details.append(f"age={reading['age_s']:g}s")
    elif reading.get("observed_at"):
        details.append(f"observed={reading['observed_at']}")
    if reading.get("resets_at"):
        details.append(f"reset={reading['resets_at']}")
    if label == "stale-provider":
        details.append("stale")
    return result + (" [" + "; ".join(details) + "]" if details else "")


def closure_text(value: Any) -> str:
    """C-9.6: closures show their scope, reason, source, and explicit clock."""
    closure = _row(value)
    result = f"{closure['scope']} until {closure['until_at']} ({_label(closure.get('reason', 'closed'))}"
    if closure.get("clock_source"):
        result += f"; {_label(closure['clock_source'])} clock"
    if closure.get("source_event"):
        result += f"; event={closure['source_event']}"
    return result + ")"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(header), *(len(row[index]) for row in rows)) if rows else len(header)
              for index, header in enumerate(headers)]
    def line(row: list[str]) -> str:
        return "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * width for width in widths]), *(line(row) for row in rows)])


def status(view: Mapping[str, Any]) -> str:
    """C-9.1, C-11.3: show evidence and attempts in the Codex reset waterfall."""
    # Rebuild only from supplied rows, for the same time, so offline and online
    # callers share latest-evidence selection and display ordering.
    lanes = view.get("lanes", ())
    readings = view.get("readings", [row for lane in lanes for row in lane.get("readings", ())])
    closures = view.get("closures", [row for lane in lanes for row in lane.get("closures", ())])
    snapshot = build_view(lanes, readings, closures, view.get("attempts", ()), view.get("jobs", ()),
                          now=view.get("now"), reading_ttl_s=view.get("reading_ttl_s", READING_TTL_S))
    lines = [f"Capacity at {snapshot['now']}", "Codex order: weekly reset ascending, then lane id; unmeasured last."]
    rows = []
    for lane in snapshot["lanes"]:
        flags = [name for name, enabled in (("desktop", lane.get("desktop")),
                 ("disabled", not lane.get("enabled", True))) if enabled]
        if lane.get("probe_state"):
            flags.append(f"probe={_label(lane['probe_state'])}")
        weekly = [row["resets_at"] for row in lane["readings"]
                  if row["window"] == "seven_day" and row["label"] in {"provider", "stale-provider"}
                  and row.get("resets_at")]
        rows.append([lane["lane_id"], lane["provider"], lane.get("account_key", "unknown"),
                     _label(lane.get("owner", "unknown")), ", ".join(flags) or "-",
                     str(lane["in_flight"]), min(weekly, default="unknown"),
                     "; ".join(reading_text(row) for row in lane["readings"]) or "unknown",
                     "; ".join(closure_text(row) for row in lane["closures"]) or "none"])
    lines.append(_table(["Lane", "Provider", "Account", "Owner", "Flags", "In-flight",
                         "Weekly reset", "Readings", "Closures"], rows))
    attempts = [row for row in snapshot["attempts"] if row["state"] in ACTIVE_ATTEMPT_STATES]
    jobs = {row["job_id"]: row for row in snapshot["jobs"]}
    running_rows = [[row.get("job_id", "unknown"), row.get("attempt_id", "unknown"), row["lane_id"],
                     row.get("model_requested", "unknown"), _label(row["state"])] for row in attempts]
    represented = {row.get("job_id") for row in attempts}
    running_rows.extend([job_id, "unknown", "unknown", "unknown", "running"]
                        for job_id, job in jobs.items() if job.get("state") == "running" and job_id not in represented)
    lines.extend(["", "Running jobs", _table(["Job", "Attempt", "Lane", "Model", "State"], running_rows)
                  if running_rows else "none"])
    waiting = [[job_id, _label(job.get("wait_reason", "unknown")), job.get("next_check_at") or "unknown"]
               for job_id, job in jobs.items() if job.get("state") == "waiting"]
    if waiting:
        lines.extend(["", "Waiting jobs", _table(["Job", "Reason", "Next check"], waiting)])
    return "\n".join(lines)


def why(decision: Any) -> str:
    """C-11.5–6: render the recorded model walk without another evaluation."""
    value = _row(decision)
    if "decision_json" in value:
        value = json.loads(value["decision_json"])
    lines = [f"Policy: {value.get('policy_hash', 'unknown')}",
             "Walk: " + (" -> ".join(value.get("chain", ())) or "none")]
    for item in value.get("evaluations", ()):
        evaluation = _row(item)
        model = evaluation["model"]
        reason = evaluation.get("reason") or ("candidates found" if evaluation.get("candidates") else "no candidate lanes")
        lines.append(reason if reason.startswith(f"{model}:") else f"{model}: {reason}")
        details = evaluation.get("candidate_details", {})
        for candidate in evaluation.get("candidates", ()):
            identity = candidate if isinstance(candidate, str) else candidate["lane_id"]
            info = details.get(identity, {}) if isinstance(details, Mapping) else {}
            state = info.get("status") or ("measured" if info.get("measured") else "eligible but unmeasured")
            line = f"  candidate {identity}: {state}"
            if info.get("in_flight") is not None:
                line += f"; in-flight={info['in_flight']}"
            if info.get("seven_day_reset"):
                line += f"; weekly reset={info['seven_day_reset']}"
            lines.append(line)
        for rejection in evaluation.get("rejections", ()):
            reasons = rejection.get("reasons") or [rejection.get("reason", "unknown")]
            lines.append(f"  rejected {rejection['lane_id']}: {', '.join(reasons)}")
        for reading in evaluation.get("readings", ()):
            lines.append(f"  reading {reading['lane_id']}: {reading_text(reading)}")
        for reading in evaluation.get("capacity_readings", ()):
            if reading not in evaluation.get("readings", ()):
                lines.append(f"  capacity reading {reading['lane_id']}: {reading_text(reading)}")
        for closure in evaluation.get("closures", ()):
            lines.append(f"  closure {closure['lane_id']}: {closure_text(closure)}")
    if value.get("chosen_lane"):
        lines.append(f"Chosen: {value['chosen_model']} on {value['chosen_lane']}")
    else:
        lines.append("Chosen: no lane")
    if value.get("reason"):
        lines.append(f"Reason: {value['reason']}")
    return "\n".join(lines)


render_status = status
render_why = why
