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


#: C-6.11: one line per reason admission can leave a job unplaced.
_HOLD_TEXT = {
    "behind-older-job": "held behind {behind}, an older {tier} job that is waiting and could run on the same model (C-6.9)",
    "fleet-full": "the fleet is at max_active_attempts ({max_active_attempts}); nothing later is evaluated until a slot frees",
    "slot-kept": "{live} of {max_active_attempts} attempts are running and the last slot is kept for {kept_for}, an older {tier} job that is waiting (C-6.9)",
    "parent-cap": "its parent job already has as many attempts running as max_active_attempts_per_parent allows",
    "lease-held": "a lease this job needs is held by another job: {leases}",
    "probe-pending": "its lane is being probed before the job may start on it",
    "attempt-live": "an earlier attempt of this job is still live or quarantined; the next waits for it",
    "approval": "waiting for an operator's approval",
    "uncertain": "a probe was quarantined; it is released when a census finds it contained, "
                 "or when an operator resolves it (C-5.7a)",
    "workspace": "its workspace could not be prepared; it is retried with backoff (C-6.8)",
    "route": "its route could not be evaluated ({error_type}: {error}); it is rechecked with backoff "
             "and holds no other job back (C-6.12)",
}


def operator_looks(rows) -> list[dict[str, Any]]:
    """C-5.7a: a probe's latest operator looks, newest first, as `--wait` reads
    them: each look's event id and the ids of the requests it acted on."""
    looks = []
    for row in rows:
        try:
            said = json.loads(row["data_json"])
            ids = [entry.get("id") for entry in said.get("requests") or () if isinstance(entry, dict)]
        except Exception:                   # noqa: BLE001 - a look that cannot be read names no request
            ids = []
        looks.append({"event_id": row["event_id"], "at": row["ts"], "ids": ids})
    return looks


def probe_resolutions(lane_id: str, job_id: str | None) -> list[str]:
    """C-5.7a: the two commands that resolve a quarantined probe, as an operator types them.

    An admission probe is named by its job; a timer's or a re-enrolment's turn,
    which has none, by the lane slot it holds (`lanes release-probe` also takes
    an admission probe's lane, or any probe's holder).
    """
    target = f"kill {job_id}" if job_id else f"lanes release-probe {lane_id}"
    return [f"subfleet {target} --confirm-dead", f"subfleet {target} --force-release"]


def probe_lines(probe: Mapping[str, Any]) -> list[str]:
    """C-5.7a: one probe that holds a lane slot, and for a quarantined one what an operator can do.

    `probe` is a row of the daemon's `_probe_rows` (in `why`, `runs show`,
    `status` and `lanes`): a quarantined probe keeps its slot until a census
    comes back verified empty or an operator resolves it.
    """
    lanes = ", ".join(probe.get("lane_ids") or [probe.get("lane_id") or "?"])
    owner = f"job {probe['job_id']}" if probe.get("job_id") else f"{probe.get('kind') or 'unknown'} turn"
    state = probe.get("state") or "unrecorded"
    head = f"probe {probe.get('holder')} ({owner}) holds {lanes}: {state}"
    if state != "quarantined":
        return [head]
    found = []
    if probe.get("live_pids"):
        found.append("live pids " + ", ".join(str(pid) for pid in probe["live_pids"]))
    if probe.get("unverifiable"):
        found.append("census unverifiable" + (f" ({'; '.join(probe.get('errors') or ())})"
                                             if probe.get("errors") else ""))
    head += " since " + str(probe.get("recorded_at") or "?") + (f"; {'; '.join(found)}" if found else "")
    if probe.get("next_look_in_s") is not None:
        head += f"; next look in {probe['next_look_in_s']:g} s"
    lines = [head]
    look = probe.get("operator_look")
    if isinstance(look, Mapping):
        evidence = look.get("containment") if isinstance(look.get("containment"), Mapping) else {}
        seen = ", ".join(str(pid) for pid in evidence.get("live_pids") or ()) or "none"
        lines.append(f"  operator's last --confirm-dead at {look.get('at')}: still quarantined "
                     f"(live pids {seen}" + ("; census unverifiable" if evidence.get("unverifiable") else "") + ")")
    requested = probe.get("requested")
    if isinstance(requested, Mapping):
        lines.append(f"  --{requested.get('mode')} requested at {requested.get('at')}; "
                     f"the next admission pass acts on it")
    resolve = list(probe.get("resolve") or ())
    if len(resolve) == 2:
        lines.append(f"  after checking those processes are gone: {resolve[0]} "
                     f"(re-runs the census; releases only on verified empty)")
        lines.append(f"  or override: {resolve[1]} (records the override; releases without containment)")
    if probe.get("unverifiable"):
        # A census that cannot complete now will not verify the next probe on
        # this lane either; a hold (C-9.6) closes the lane to admission and timers.
        lines.append(f"  while the census cannot complete, a new probe there can be quarantined the same way: "
                     f"subfleet lanes hold {probe.get('lane_id')} --until <time> keeps work off the lane")
    return lines


def why_queue(standing: Mapping[str, Any]) -> list[str]:
    """C-6.11: where a job stands in admission, in lines a person can act on.

    `standing` is the `job` object of the `why` result: the job's state, the
    hold the last admission pass recorded for it, and its recheck history.
    """
    state = standing.get("state")
    lines = [f"Job: {standing.get('job_id')} is {state}"
             + (f" ({standing['wait_reason']})" if standing.get("wait_reason") else "")]
    hold, recheck = standing.get("hold"), standing.get("recheck")
    for probe in standing.get("probes") or ():
        # C-5.7a: a finished job's quarantined probe still holds its lane slot.
        lines.extend(probe_lines(probe))
    if state not in ("queued", "waiting"):
        return lines
    if hold:
        reason = hold.get("reason", "unknown")
        template = _HOLD_TEXT.get(reason)
        if template:
            fields = {**hold, "leases": ", ".join(hold.get("leases", ())) or "-"}
            lines.append("Held: " + template.format_map({**dict.fromkeys(
                ("behind", "tier", "max_active_attempts", "kept_for", "live", "error_type", "error"), "?"), **fields}))
        else:
            lines.append(f"Held: no lane admits it ({reason})")
    else:
        lines.append("Held: no admission pass has reached this job yet")
    if recheck:
        lines.append(f"Rechecks: same verdict {recheck['rechecks'] + 1} times since {recheck['since']}, "
                     f"last {recheck['checked_at']}")
    fleet = list(standing.get("fleet_probes") or ())
    if fleet:
        # C-5.7a: each probe lease counts toward max_active_attempts (C-11.4).
        lines.append(f"Probes hold {sum(len(probe.get('lane_ids') or [1]) for probe in fleet)} lane slot(s), "
                     f"and each counts toward max_active_attempts:")
        lines.extend("  " + line for probe in fleet for line in probe_lines(probe))
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
