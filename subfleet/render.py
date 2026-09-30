"""Human-readable, side-effect-free status, decisions and notice headers (C-9.1, C-11.5, C-15.1)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .capacity import ACTIVE_ATTEMPT_STATES, build_view
from .contracts import READING_TTL_S, attempt_dir


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


def notice_header(job: Mapping[str, Any], state_root: str | Path, *,
                  require_file: bool = False) -> str:
    """C-15.1: the first line of a job's notice, from the job row alone.

    `<job id>: <state>; rc=<rc>; deliverable=<path>; out=<-o path>`. State and
    rc are the job's, as the transaction that made it terminal wrote them: the
    same pair `wait`, `runs` and `runs show` report, never the final attempt's
    outcome class and return code, which the summary line carries. The two
    paths name files only for an accepted job, the one job whose deliverable
    is its result and whose `-o` file the daemon writes (C-4.3, C-8.3); any
    other job prints `-` for both, whatever its attempt left on disk
    (incident: 2026-09-24, three Codex jobs cancelled while running were
    announced `ok; rc=0; deliverable=...; out=...` from the attempt, with an
    interim progress message as the deliverable, while the job was `cancelled`
    with rc 130 and nothing was exported; the hook fallback then announced the
    same jobs `cancelled; rc=130` from the job row).

    The daemon's notice and the PostToolUse hook's fallback line both call
    this, so one terminal state cannot be rendered two ways. `state_root` is
    the resolved state root the daemon writes attempt directories under
    (C-2.3); the deliverable of an accepted job is that attempt's
    `deliverable.md` (C-8.2). `require_file` names that path only when the
    file is there: the hook's fallback may be rendering a job the importer
    brought from v1, whose deliverable stayed in its v1 run directory, and a
    path that does not exist is not one to offer. The daemon's notice never
    needs it: the transaction that accepts an attempt follows its published
    deliverable.
    """
    job_id = str(job.get("job_id"))
    rc = job.get("rc")
    accepted = job.get("accepted_attempt_id") if job.get("state") == "succeeded" else None
    deliverable = out = "-"
    if accepted:
        seq = str(accepted).rpartition("/a")[2]
        if seq.isdigit():
            path = attempt_dir(Path(state_root), job_id, int(seq)) / "deliverable.md"
            if not require_file or path.is_file():
                deliverable = str(path)
        out = job.get("out_path") or "-"
    return (f"{job_id}: {job.get('state')}; rc={'-' if rc is None else rc}; "
            f"deliverable={deliverable}; out={out}")


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
        # C-10.6: an operator looking at a lane with no readings has to be able
        # to see that it was refused rather than merely quiet.
        flags = [name for name, enabled in (
            ("desktop", lane.get("desktop")),
            ("disabled", not lane.get("enabled", True)),
            ("identity-mismatch", lane.get("identity_status") == "mismatch"),
            ("identity-unverified", lane.get("identity_status") == "unverified"),
        ) if enabled]
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
    jobs = {row["job_id"]: row for row in snapshot["jobs"]}
    # C-26.12: turn jobs hold lane slots like any job, so they are shown, but
    # under their own heading: they are conversations' turns, not detached work.
    turn_ids = {job_id for job_id, job in jobs.items() if job.get("kind") == "turn"}
    attempts = [row for row in snapshot["attempts"] if row["state"] in ACTIVE_ATTEMPT_STATES]

    def live_rows(turns: bool) -> list[list[str]]:
        selected = [row for row in attempts if (row.get("job_id") in turn_ids) == turns]
        rows = [[row.get("job_id", "unknown"), row.get("attempt_id", "unknown"), row["lane_id"],
                 row.get("model_requested", "unknown"), _label(row["state"])] for row in selected]
        represented = {row.get("job_id") for row in selected}
        rows.extend([job_id, "unknown", "unknown", "unknown", "running"]
                    for job_id, job in jobs.items() if job.get("state") == "running"
                    and job_id not in represented and (job_id in turn_ids) == turns)
        return rows

    running_rows = live_rows(False)
    lines.extend(["", "Running jobs", _table(["Job", "Attempt", "Lane", "Model", "State"], running_rows)
                  if running_rows else "none"])
    waiting = [[job_id, _label(job.get("wait_reason", "unknown")), job.get("next_check_at") or "unknown"]
               for job_id, job in jobs.items() if job.get("state") == "waiting" and job_id not in turn_ids]
    if waiting:
        lines.extend(["", "Waiting jobs", _table(["Job", "Reason", "Next check"], waiting)])
    turn_rows = live_rows(True)
    shown = {row[0] for row in turn_rows}
    turn_rows.extend([job_id, "-", "-", "-", _label(job["state"])
                      + (f" ({_label(job['wait_reason'])})" if job.get("wait_reason") else "")]
                     for job_id, job in jobs.items() if job_id in turn_ids and job_id not in shown
                     and job.get("state") in ("queued", "waiting"))
    if turn_rows:
        lines.extend(["", "Conversation turns", _table(["Job", "Attempt", "Lane", "Model", "State"], turn_rows)])
    return "\n".join(lines)


#: C-6.11: one line per reason admission can leave a job unplaced.
_HOLD_TEXT = {
    "behind-older-job": "held behind {behind}, an older {tier} job that is waiting{where} (C-6.9)",
    "fleet-full": "the fleet is at max_active_attempts ({max_active_attempts}); nothing later is evaluated until a slot frees",
    "slot-kept": "{live} of {max_active_attempts} attempts are running and the last slot is kept for {kept_for}, an older {tier} job that is waiting (C-6.9)",
    "parent-cap": "its parent job already has as many attempts running as max_active_attempts_per_parent allows",
    "lease-held": "a lease this job needs is held by another job: {leases}",
    "probe-pending": "its lane is being probed before the job may start on it",
    "attempt-live": "an earlier attempt of this job is still live or quarantined; the next waits for it",
    "approval": "waiting for an operator's approval",
    "uncertain": "a probe was quarantined; an operator must resolve it",
    "workspace": "its workspace could not be prepared; it is retried with backoff (C-6.8)",
    "route": "its route could not be evaluated ({error_type}: {error}); it is rechecked with backoff "
             "and holds no other job back (C-6.12)",
    "conversation-blocked": "its conversation {conversation_id} is blocked ({blocked}); the turn is placed once "
                            "that clears and holds no other job back (C-24.5)",
    "message-settled": "its message was withdrawn after the job was made; the job is cancelled while it has no "
                       "attempt, never run (C-24.7)",
    "route-moved": "its route could not be settled in {tries} reservations in a row (commits changed it, or its "
                   "clock ran out); it keeps its place and the next pass looks again (C-6.3)",
    "machine-busy": "the machine is saturated ({machine}) and the guard holds {class} jobs at the door until it "
                    "is not; admission.machine_guard sets the thresholds (C-6.13)",
}


def _machine(hold: Mapping[str, Any]) -> str:
    """What the machine guard read, for `machine-busy`."""
    parts = [f"load {hold['load_per_cpu']} per CPU, threshold {hold['load_threshold']}"
             if hold.get("load_per_cpu") is not None else "",
             f"memory pressure {hold['memory_pressure']}, threshold {hold['memory_threshold']}"
             if hold.get("memory_pressure") is not None else ""]
    return "; ".join(part for part in parts if part) or "busy"


def _blocked(hold: Mapping[str, Any]) -> str:
    """What blocks a turn's conversation, for `conversation-blocked`."""
    parts = [f"blocked_by {hold['blocked_by']}" if hold.get("blocked_by") else "",
             f"held by the legacy import: {hold['legacy_hold']}" if hold.get("legacy_hold") else "",
             f"it could not be checked: {hold.get('error_type')}: {hold.get('error')}" if hold.get("error_type") else ""]
    return "; ".join(part for part in parts if part) or "blocked"


def why_queue(standing: Mapping[str, Any]) -> list[str]:
    """C-6.11: where a job stands in admission, in lines a person can act on.

    `standing` is the `job` object of the `why` result: the job's state, the
    hold the last admission pass recorded for it, and its recheck history.
    """
    state = standing.get("state")
    reason = f" ({standing['wait_reason']})" if standing.get("wait_reason") else ""
    if standing.get("kind") == "turn":
        # C-26.12: a turn is its conversation's, never detached work.
        conversation = standing.get("conversation_id")
        lines = [f"Conversation turn: {standing.get('job_id')}"
                 + (f" of {conversation}" if conversation else "") + f" is {state}{reason}",
                 "Its outcome goes to the conversation, not to a notice or a deliverable (C-26.12)"]
    else:
        lines = [f"Job: {standing.get('job_id')} is {state}{reason}"]
    hold, recheck = standing.get("hold"), standing.get("recheck")
    if state not in ("queued", "waiting"):
        return lines
    if hold:
        reason = hold.get("reason", "unknown")
        template = _HOLD_TEXT.get(reason)
        if reason == "lease-held" and hold.get("queued") and not hold.get("leases"):
            # C-6.9, C-26.9: FIFO on a lease; nothing holds it, an older job is waiting for it.
            template = "a lease this job needs is kept for an older job that is waiting for it: {queued}"
        if template:
            fields = {**hold, "leases": ", ".join(hold.get("leases", ())) or "-",
                      # C-6.9: the lane and model this job would take are ones the older job could.
                      # Only a hold made on a lane says the older job could run there; the others
                      # say it competes (code review of this change).
                      "where": (f" and could run on {hold['lane']}, where this one would" if hold.get("lane")
                                else " ahead of it that competes with it"),
                      "queued": ", ".join(hold.get("queued", ())) or "-",
                      "pids": ", ".join(str(pid) for pid in hold.get("pids", ())) or "?", "blocked": _blocked(hold),
                      "machine": _machine(hold)}
            lines.append("Held: " + template.format_map({**dict.fromkeys(
                ("behind", "tier", "max_active_attempts", "kept_for", "live", "error_type", "error",
                 "conversation_id", "native_session_id", "tries", "class"), "?"),
                **{k: v for k, v in fields.items() if v is not None}}))
            if reason == "lease-held" and hold.get("queued_behind"):
                # C-6.9, C-26.9: FIFO on a lease; a lease an older job waits for is kept for it.
                lines.append("Queued behind: " + ", ".join(hold["queued_behind"])
                             + " (an older job waiting for " + ", ".join(hold.get("queued") or ["the same lease"])
                             + " takes it first)")
        else:
            lines.append(f"Held: no lane admits it ({reason})")
    else:
        lines.append("Held: no admission pass has reached this job yet")
    if recheck:
        lines.append(f"Rechecks: same verdict {recheck['rechecks'] + 1} times since {recheck['since']}, "
                     f"last {recheck['checked_at']}")
    earlier = standing.get("attempts") or ()
    if earlier:
        # C-4.5, C-23.44: a job that moved on from a lane says which and why; an
        # auth-dead lane was disabled and is enrolled again by `subfleet lanes enroll`.
        lines.append("Earlier attempts: " + ", ".join(
            f"a{row['seq']} {row.get('outcome_class') or row.get('state')} on {row['lane_id']}"
            + (" (that lane is disabled; subfleet lanes enroll)" if row.get("outcome_class") == "auth-dead" else "")
            for row in earlier))
    if standing.get("next_check_at"):
        lines.append(f"Next check: {standing['next_check_at']}")
    if standing.get("decision_source") == "evaluated-now":
        lines.append("Decision: none recorded; evaluated now for this answer, not by admission")
    elif standing.get("decided_at"):
        lines.append(f"Decision: recorded {standing['decided_at']}")
    return lines


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
