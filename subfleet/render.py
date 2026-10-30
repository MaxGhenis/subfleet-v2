"""Human-readable, side-effect-free status, decisions and notice headers (C-9.1, C-11.5, C-15.1)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from . import task_holds
from .capacity import ACTIVE_ATTEMPT_STATES, build_view
from .contracts import READING_TTL_S, attempt_dir, hold_note_of, hold_source
from .launch_env import refused_reference
from .quota_projection import instant, weekly_projections


def projection_text(projection: Mapping[str, Any]) -> str:
    reset = instant(projection["resets_at"]).strftime("%a %H:%MZ")
    basis = "; rate unknown" if projection["basis"] == "rate unknown" else ""
    return (f"~{projection['projected_unused'] * 100:.0f}% unused at reset {reset} "
            f"(projection{basis})")


def projection_totals(lanes: Any) -> list[str]:
    """Sum account windows once per lane; model scopes do not add lane-weeks."""
    providers: dict[str, list[Mapping[str, Any]]] = {}
    for lane in lanes:
        projection = lane.get("weekly_projections", {}).get("account")
        if projection is not None:
            providers.setdefault(lane["provider"], []).append(projection)
    lines = []
    for provider, projections in sorted(providers.items()):
        total = math.fsum(row["projected_unused"] for row in projections)
        through = max(instant(row["resets_at"]) for row in projections).strftime("%a %H:%MZ")
        unknown = sum(row["basis"] == "rate unknown" for row in projections)
        suffix = f" ({unknown} rate unknown)" if unknown else ""
        lines.append(f"{provider}: ~{total:.1f} of {len(projections)} lane-weeks projected unused by {through}{suffix}")
    return lines


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


#: D-STATUS-SRC1: what `status` calls each kind of limit.
LIMIT_WORDS = {"provider-limit": "provider limit", "credits": "credits limit", "cooldown": "cooldown"}


def lane_home(lane: Mapping[str, Any]) -> str:
    """The same public lane name in alerts, status, notices and acknowledgements."""
    return str(lane.get("home") or lane.get("credential_ref") or lane.get("lane_id") or "the lane")


def _held_text(closure: Mapping[str, Any], *, home: str | None = None) -> str:
    """C-9.6: the operator's end is a safety bound, with the validated stored note."""
    note = hold_note_of(closure.get("source_event"))
    return (f"{home or lane_home(closure)}: held by the operator" + (f": {note}" if note else "")
            + f" (until {closure.get('until_at')} at the latest)")


def closure_line(value: Any, *, home: str | None = None) -> str:
    """D-STATUS-SRC1: one closure as `subfleet status` lists it, saying what it is.

    `<home>: held by the operator[: <note>] (until <end> at the latest)` for an operator hold, else
    `<lane> provider limit on account until <end> (reported)`, with the limit's
    kind, its scope and its clock source."""
    closure = _row(value)
    reason = _label(closure.get("reason", "closed"))
    if reason == "operator-hold":
        return _held_text(closure, home=home)
    clock = f" ({_label(closure['clock_source'])})" if closure.get("clock_source") else ""
    return (f"{closure.get('lane_id')} {LIMIT_WORDS.get(reason, reason)} on {closure.get('scope')} "
            f"until {closure.get('until_at')}{clock}")


def closure_text(value: Any, *, home: str | None = None) -> str:
    """C-9.6: closures show their scope, reason, source, and explicit clock; a hold
    says it is the operator's, with its note (D-STATUS-SRC1)."""
    closure = _row(value)
    if _label(closure.get("reason", "closed")) == "operator-hold":
        return _held_text(closure, home=home)
    result = f"{closure['scope']} until {closure['until_at']} ({_label(closure.get('reason', 'closed'))}"
    if closure.get("clock_source"):
        result += f"; {_label(closure['clock_source'])} clock"
    if closure.get("source_event"):
        result += f"; event={closure['source_event']}"
    return result + ")"


def attempt_shell_text(job: Mapping[str, Any]) -> str:
    """C-12.4: capability of the recorded attempt, unknown for older records.

    A sentence only for `none` and `commands` (design-r2 section 2): a writable
    review says `publish: denied` alone, and a writable job that may publish (a
    build) says nothing (#221 review r1b, P3-3).
    """
    if isinstance(job.get("job"), Mapping):
        job = {**job, **job["job"]}
    attempts = job.get("attempts") or ()
    attempt = job.get("attempt") or (attempts[-1] if attempts else job)
    capability = attempt.get("capability")
    denied = "; publish: denied" if attempt.get("publish_denied") else ""
    if capability is None or capability == "writable":
        return denied.removeprefix("; ")
    try:
        evidence = json.loads(attempt.get("evidence_json") or "{}")
    except (TypeError, ValueError):
        evidence = {}
    model = evidence.get("model_short") or attempt.get("model_requested") or job.get("model") or "unknown"
    sandbox = job.get("sandbox") or attempt.get("sandbox") or "unknown"
    lane = attempt.get("lane_id") or job.get("lane_id") or "unknown"
    text = f"ran {sandbox} on {lane} ({model})"
    if capability == "none":
        text += " with no shell: it could not run any command"
    else:
        text += ": commands ran, but nothing could be written, temp files included, so pytest could not run"
    return text + denied


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


def uncommitted_text(record: Mapping[str, Any] | None) -> str:
    """D-SH1 round 6: what a policy review's worktree holds of its caller's
    uncommitted work, or why it read the checkout in place instead."""
    if not isinstance(record, Mapping) or not record:
        return ""
    if record.get("fallback"):
        return ("ran read-only in the caller's checkout: its uncommitted changes could not be "
                f"copied into a worktree ({record['fallback']})")
    return (f"the worktree included the caller's uncommitted changes as of submit: {record.get('files')} files "
            f"({record.get('tracked')} tracked, {record.get('untracked')} untracked)")


def notice_text(job: Mapping[str, Any], state_root: str | Path, summary: str, *,
                require_file: bool = False) -> str:
    """C-15.1: preserve every existing line; launch evidence is the final line.

    A policy review's workspace line (`uncommitted_text`) comes just before it.
    """
    text = notice_header(job, state_root, require_file=require_file) + "\n" + summary
    workspace = uncommitted_text(job.get("uncommitted"))
    capability = attempt_shell_text(job)
    return text + (f"\n{workspace}" if workspace else "") + (f"\n{capability}" if capability else "")


def append_notice_detail(text: str, detail: str) -> str:
    """Add a later export/push result before recorded launch evidence, if any."""
    body, separator, last = text.rpartition("\n")
    if separator and (last.startswith(("ran read-only on ", "ran workspace-write on "))
                      or last == "publish: denied"):
        return body + "\n" + detail + "\n" + last
    return text + "\n" + detail


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(header), *(len(row[index]) for row in rows)) if rows else len(header)
              for index, header in enumerate(headers)]
    def line(row: list[str]) -> str:
        return "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * width for width in widths]), *(line(row) for row in rows)])


def restricted_runs_text(ids: list[str]) -> str:
    return f"last 24h: {len(ids)} runs with no shell or no writes; ids: {', '.join(ids) or '-'}"


def status(view: Mapping[str, Any]) -> str:
    """C-9.1, C-11.3: show evidence and attempts in the Codex reset waterfall."""
    # Rebuild only from supplied rows, for the same time, so offline and online
    # callers share latest-evidence selection and display ordering.
    lanes = view.get("lanes", ())
    readings = view.get("readings", [row for lane in lanes for row in lane.get("readings", ())])
    closures = view.get("closures", [row for lane in lanes for row in lane.get("closures", ())])
    snapshot = build_view(lanes, readings, closures, view.get("attempts", ()), view.get("jobs", ()),
                          now=view.get("now"), reading_ttl_s=view.get("reading_ttl_s", READING_TTL_S),
                          weekly_samples=view.get("weekly_samples"))
    lines = [f"Capacity at {snapshot['now']}", "Codex order: weekly reset ascending, then lane id; unmeasured last."]
    lines.extend(provider_cap_text(row) for row in view.get("provider_caps", ()))
    if "restricted_runs_24h" in view:
        lines.append(restricted_runs_text(view["restricted_runs_24h"]))
    if view.get("disk") is not None:
        lines.append(disk_line(view["disk"]))
    if view.get("ramp") is not None:
        lines.append(ramp_line(view["ramp"]))
    if view.get("task_holds"):
        # C-11.10: the operator's holds on tasks; lanes stay open to every other task.
        lines.append("Task holds: " + "; ".join(task_hold_line(hold) for hold in view["task_holds"]))
    rows = []
    for lane in snapshot["lanes"]:
        # C-10.6: an operator looking at a lane with no readings has to be able
        # to see that it was refused rather than merely quiet.
        flags = [name for name, enabled in (
            ("desktop", lane.get("desktop")),
            ("disabled", not lane.get("enabled", True)),
            ("identity-mismatch", lane.get("identity_status") == "mismatch"),
            ("identity-unverified", lane.get("identity_status") == "unverified"),
            # D-ENV1: held, its variable every launch inherits (the variable is a name).
            (f"credential-refused={lane.get('credential_ref')}",
             lane.get("credential_kind") == "env" and refused_reference(lane.get("credential_ref"))),
        ) if enabled]
        if lane.get("probe_state"):
            flags.append(f"probe={_label(lane['probe_state'])}")
        weekly = [row["resets_at"] for row in lane["readings"]
                  if row["window"] == "seven_day" and row["label"] in {"provider", "stale-provider"}
                  and row.get("resets_at")]
        rows.append([lane["lane_id"], lane["provider"], lane.get("account_key", "unknown"),
                     _label(lane.get("owner", "unknown")), ", ".join(flags) or "-",
                     str(lane["in_flight"]), min(weekly, default="unknown"),
                     "; ".join(reading_text(row) + (
                         " · " + projection_text(lane["weekly_projections"][row["scope"]])
                         if row["window"] == "seven_day" and row["scope"] in lane["weekly_projections"] else "")
                         for row in lane["readings"]) or "unknown",
                     "; ".join(closure_text(row, home=lane_home(lane)) for row in lane["closures"]) or "none"])
    lines.append(_table(["Lane", "Provider", "Account", "Owner", "Flags", "In-flight",
                         "Weekly reset", "Readings", "Closures"], rows))
    lines.extend(projection_totals(snapshot["lanes"]))
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
    "ramp": "placement ramp: {used} of {limit} starts used in the last {window_s} s; next slot at {next_slot_at}",
    "disk": "disk admission is holding: free {free_gb} GB, reserved {reserved_gb} GB, floor {floor_gb} GB; "
            "resume margin {resume_margin_gb} GB, placement reserve {placement_reserve_gb} GB (C-6.17)",
    "behind-older-job": "held behind {behind}, an older {tier} job that is waiting and could run on the same model (C-6.9)",
    "fleet-full": "the fleet is at max_active_attempts ({max_active_attempts}); nothing later is evaluated until a slot frees",
    "slot-kept": "{live} of {max_active_attempts} attempts are running and the last slot is kept for {kept_for}, an older {tier} job that is waiting (C-6.9)",
    "provider-cap": "{provider}: {live} of {max_active_attempts} running ({key}); waiting for a provider slot",
    "provider-slot-kept": "{provider}: {live} of {max_active_attempts} running ({key}); the last provider slot is kept for {kept_for}, an older {tier} job (C-6.9)",
    "parent-cap": "its parent job already has as many attempts running as max_active_attempts_per_parent allows",
    "lease-held": "a lease this job needs is held by another job: {leases}",
    "lease-held:retiring": "retention is removing a finished job's worktree{where} ({trees}); it waits until "
                           "retention lets go of it (C-8.4){others}",
    "probe-pending": "its lane is being probed before the job may start on it",
    "lane-proving": "every lane that could take it has gone admission.prove_idle_s without a model's answer and "
                    "is being proven by one attempt, its pilot; the job starts once a pilot shows its model "
                    "answering, or goes elsewhere if another lane opens (C-6.14)",
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
    "pin-unadmittable": "its pinned lane {lane_id} can never admit it: {refusals}; it holds no other job back "
                        "and {ends} (C-11.8)",
    "task-held": "the operator holds {task} jobs at admission until {until} ({note}); it starts once the hold "
                 "ends or `{release}`, and holds no other job back (C-11.10)",
}


def task_hold_line(hold: Mapping[str, Any]) -> str:
    """C-11.10: one hold in force, as both status views show it."""
    return f"{hold.get('task')} until {hold.get('until')}: {hold.get('note')}"


def provider_cap_text(row: Mapping[str, Any]) -> str:
    text = f"{row['provider']}: {row['live']} of {row['max_active_attempts']} running ({row['key']})"
    return text + (f"; {row['probes']} exempt probe reservations" if row.get("probes") else "")


def disk_floor_detail(reading: Mapping[str, Any]) -> str:
    parts = [reading.get("floor_source", "policy")]
    if reading.get("floor_why"):
        parts.append(str(reading["floor_why"]))
    for key in ("lower_error", "lower_expired", "lower_refused", "override_error", "override_expired"):
        if key in reading:
            parts.append(f"{key}: {reading[key]}")
    return "; ".join(parts)


def ramp_line(reading: Mapping[str, Any]) -> str:
    """C-6.18: the same one-line placement budget in both status views."""
    return (f"placement ramp: {reading['used']} of {reading['limit']} starts used "
            f"in the last {reading['window_s']} s; next slot at "
            + (reading.get("next_slot_at") or "now"))


def disk_line(reading: Mapping[str, Any]) -> str:
    """C-6.17: one disk line, shared by the capacity and CLI status views."""
    free = reading.get("free_gb")
    free_text = "unknown" if free is None else f"{free:.2f} GB"
    mode = "disabled" if not reading.get("enabled") else "holding" if reading.get("holding") else "open"
    floor = reading["floor_gb"]
    floor_text = floor if isinstance(floor, str) else f"{floor:g}"
    return (f"disk: free {free_text}, reserved {reading['reserved_gb']:.2f} GB, "
            f"floor {floor_text} GB; {mode}"
            + f"; {disk_floor_detail(reading)}"
            + (f" ({reading['error']})" if reading.get("error") else ""))

#: C-11.8: each standing refusal of a pinned lane, in words (`scheduler.STANDING_REFUSALS`).
_PIN_REFUSALS = {
    "unknown": "no lane named {lane} is enrolled",
    "excluded": "the job's own exclusions (-x) name {lane}",
    "desktop": "{lane} is the Claude desktop app's login, and Claude Code is using it or cannot be told not "
               "to be (C-10.3); only --allow-desktop lets a job run there",
    "config-dir": "{lane} has its own config directory, where the conversation's transcript is not (C-26.2)",
    "owner-v1": "{lane} is owned by Subfleet v1",
    "disabled": "{lane} is disabled",
    "auth-dead": "{lane}'s provider rejected its credential; a new login or re-enrolment is needed",
    "identity-mismatch": "{lane}'s credential proved to hold another account (C-10.6)",
    "credential-refused": "{lane} names {credential_ref}, a variable every launch inherits, so it holds no "
                          "credential of its own (D-ENV1); point it at a daemon variable no launch inherits "
                          "in lanes.json, same lane id, provider and account, and restart the daemon",
    "credential-latched": "{lane}'s last probe found its credential {probe_status}, which only a new login "
                          "or a re-enrolment ends",
    "credential-latched:expired-token": "{lane}'s token expired and the one heal the timers allow a Codex "
                                        "login ran and left it so (C-23.47): only a new login or a "
                                        "re-enrolment ends it",
    "no-lanes": "no lane of the model's provider is enrolled",
}


def pin_refusals(stuck: Mapping[str, Any]) -> str:
    """C-11.8: why a pinned lane can never admit its job, in words: each standing
    refusal `scheduler.pin_unadmittable` (or `refused_for_good`) named.

    A hold and a limit keep separate rows (C-9.6), so two closures can share a
    scope and an end, and `scheduler.judge_lane` then names that refusal once
    per row. Keep the operator's hold apart from provider reasons sharing its
    clock (D-RAMP-HOLD-LABEL)."""
    lane = stuck.get("lane_id") or "the lane"
    closures: dict[str, list[Mapping[str, Any]]] = {}
    for row in stuck.get("closures") or ():
        closures.setdefault(f"closed:{row['scope']}:{row['until_at']}", []).append(row)
    parts = []
    reasons = list(dict.fromkeys(stuck.get("reasons") or ()))
    # Canonicalize closure order while preserving the other standing refusals.
    closed_reasons = iter(sorted(reason for reason in reasons if reason in closures))
    for original in reasons:
        reason = next(closed_reasons) if original in closures else original
        rows = closures.get(reason)
        if rows:
            rows = sorted(rows, key=lambda row: (str(_label(row.get("reason"))), str(row.get("source_event"))))
            for row in rows:
                if _label(row.get("reason")) == "operator-hold":
                    # #263 pin evidence has no source field. An explicitly present
                    # source still goes through the unchanged strict note reader.
                    source = row if "source_event" in row else {**row, "source_event": hold_source(None)}
                    parts.append(closure_text(source, home=lane_home(stuck)))
            rows = [row for row in rows if _label(row.get("reason")) != "operator-hold"]
            if not rows:
                continue
            why = ", ".join(sorted({str(getattr(row["reason"], "value", row["reason"]))
                                    for row in rows if row.get("reason")}))
            parts.append(f"{lane} is closed for {rows[0]['scope']}" + (f" ({why})" if why else "")
                         + f" until {rows[0]['until_at']}")
        elif str(reason).startswith("closed:"):
            parts.append(f"{lane} is {reason}")
        else:
            status = stuck.get("probe_status") or "unusable"
            text = _PIN_REFUSALS.get(f"{reason}:{status}") or _PIN_REFUSALS.get(reason, reason)
            parts.append(text.format(lane=lane, probe_status=status,
                                     credential_ref=stuck.get("credential_ref") or "its credential variable"))
    return "; ".join(parts) or "it refuses the job"


def pin_ends(fail_at: str | None) -> str:
    """C-11.8: what becomes of a job whose pinned lane can never admit it."""
    return (f"it fails with rc 3 at {fail_at} unless the lane can take it by then" if fail_at
            else "it waits until that changes")


def pin_notice(job_id: str, stuck: Mapping[str, Any], fail_at: str | None) -> str:
    """C-11.8: the one notice a job gets when its pinned lane can never admit it.

    Not a C-15.1 notice (the job has not ended): a service notice to the
    session that submitted it, which the session hooks surface (C-15.2)."""
    if stuck.get("all_lanes"):
        return (f"{job_id}: waiting; every candidate lane refuses its retry: {pin_refusals(stuck)}.\n"
                f"Fix: restore a usable lane or `subfleet kill {job_id}`. "
                f"Meanwhile {pin_ends(fail_at)}, and it holds no other job back (C-11.8).")
    lane = stuck.get("lane_id") or "its lane"
    return (f"{job_id}: waiting; its pinned lane {lane} can never admit it: {pin_refusals(stuck)}.\n"
            f"Fix: resubmit it unpinned, or pinned to another lane (-a or -H), then `subfleet kill {job_id}`; "
            f"or make {lane} usable again (re-enable or re-enrol it, release its hold, or stop using the desktop "
            f"login in Claude Code, as the reason says). "
            f"Meanwhile {pin_ends(fail_at)}, and it holds no other job back (C-11.8).")


def pin_failure(stuck: Mapping[str, Any], since: str | None) -> str:
    """C-11.8: the summary of the terminal notice of a job failed for its pin (C-15.1)."""
    if stuck.get("all_lanes"):
        return ("no lane: every candidate lane refused its retry"
                + (f" from {since} on" if since else "") + f": {pin_refusals(stuck)}; "
                "fix: restore a usable lane and resubmit (C-11.8)")
    lane = stuck.get("lane_id") or "its lane"
    return (f"no lane: its pinned lane {lane} could never admit it"
            + (f" from {since} on" if since else "") + f": {pin_refusals(stuck)}; "
            "fix: resubmit it unpinned, or pinned to another lane (-a or -H) (C-11.8)")


def _machine(hold: Mapping[str, Any]) -> str:
    """What the machine guard read, for `machine-busy`."""
    parts = [f"load {hold['load_per_cpu']} per CPU, threshold {hold['load_threshold']}"
             if hold.get("load_per_cpu") is not None else "",
             f"memory pressure {hold['memory_pressure']}, threshold {hold['memory_threshold']}"
             if hold.get("memory_pressure") is not None else ""]
    if hold.get("cpu_idle") is not None:
        parts.append(f"CPU idle {hold['cpu_idle']:.1%}, minimum {hold['cpu_idle_min']:.1%}")
    if hold.get("disk_busy") is not None:
        parts.append(f"disk service {hold['disk_busy']:.3f} ms/op, maximum {hold['disk_busy_max']:.3f}")
    for signal, label, direction in (("cpu_idle", "CPU idle", "at least"), ("disk_busy", "disk busy", "at most")):
        if f"{signal}_resume" in hold:
            resume = hold[f"{signal}_resume"]
            target = f"{resume:.1%}" if signal == "cpu_idle" else f"{resume:.3f} ms/op"
            parts.append(f"{label} release needs {direction} {target} "
                         f"for {hold[f'{signal}_release_s']:g} seconds ({hold[f'{signal}_clear_s']:.1f} s clear)")
    return "; ".join(part for part in parts if part) or "busy"


def _blocked(hold: Mapping[str, Any]) -> str:
    """What blocks a turn's conversation, for `conversation-blocked`."""
    parts = [f"blocked_by {hold['blocked_by']}" if hold.get("blocked_by") else "",
             f"held by the legacy import: {hold['legacy_hold']}" if hold.get("legacy_hold") else "",
             f"it could not be checked: {hold.get('error_type')}: {hold.get('error')}" if hold.get("error_type") else ""]
    return "; ".join(part for part in parts if part) or "blocked"


#: C-8.4: what a held job needs in a tree retention is retiring, besides its folder (`dependencies`).
_NEEDS = {"review-root": "review root (-D)", "output": "output path (-o)", "git-storage": "git storage"}


def _retiring(hold: Mapping[str, Any]) -> dict[str, str]:
    """C-8.4: the fields of `lease-held:retiring`. Retention's fence is no job's lease
    but a finished job's worktree being removed, which the held job's folder, or
    another place it needs (`needs`: its review root, `-o` path or git storage), is
    or is in (`retiring`, `folder`); any other key the job waits for is named after it."""
    fences = list(hold.get("retiring") or ())
    trees = ", ".join(str(key).split(":", 1)[1] for key in fences)
    folder = hold.get("folder")
    what = _NEEDS.get(str(hold.get("needs")), "folder")
    others = [key for key in hold.get("leases") or () if key not in fences]
    return {"trees": trees,
            "where": (f", its {what}" if folder and trees == folder else
                      f" that its {what} {folder} is in" if folder else " it works in"),
            "others": "; a lease it needs is also held by another job: " + ", ".join(others) if others else ""}


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
    if standing.get("class") == "priority":
        lines.append("class priority (admission.priority_callers)")
    qos = standing.get("qos") or {}
    if qos.get("requested") == "default" and qos.get("reason"):
        # D-QOS-BOOKED1: a `run --qos default` says whether a launch now honours it, and
        # why; the clamp every other job runs under (C-5.1) has nothing to explain.
        lines.append(qos["reason"])
        started = qos.get("started")
        if started:
            lines.append(f"a{started['seq']}'s provider started at {started['provider_qos']} (start.json)")
    hold, recheck = standing.get("hold"), standing.get("recheck")
    if state not in ("queued", "waiting"):
        return lines
    if hold:
        reason = hold.get("reason", "unknown")
        template = _HOLD_TEXT.get(reason)
        if reason == "behind-older-job" and hold.get("key", "").startswith("providers."):
            template = "held behind {behind}, an older {tier} job waiting for the same provider's capacity (C-6.9)"
        if reason == "lease-held" and hold.get("queued") and not hold.get("leases"):
            # C-6.9, C-26.9: FIFO on a lease; nothing holds it, an older job is waiting for it.
            template = "a lease this job needs is kept for an older job that is waiting for it: {queued}"
        if reason == "lease-held" and hold.get("retiring"):
            template = _HOLD_TEXT["lease-held:retiring"]      # C-8.4: retention's fence, named as such
        if reason == "probe-pending" and hold.get("behind"):
            # C-6.9: FIFO on a probe; an older job waits on the same one and carries it first.
            template = ("{lane} must be probed for {model} before the job may start on it, and {behind}, an "
                        "older job waiting on that probe, carries it first (C-6.9)")
        if template:
            fields = {**hold, "leases": ", ".join(hold.get("leases", ())) or "-",
                      **(_retiring(hold) if hold.get("retiring") else {}),
                      "queued": ", ".join(hold.get("queued", ())) or "-",
                      "pids": ", ".join(str(pid) for pid in hold.get("pids", ())) or "?", "blocked": _blocked(hold),
                      "machine": _machine(hold)}
            if reason == "disk" and hold.get("free_gb") is None:
                fields["free_gb"] = "unknown"
            if reason in ("pin-unadmittable", "lanes-unadmittable"):
                fields.update(refusals=pin_refusals(hold), ends=pin_ends(hold.get("fail_at")))
            if reason == "task-held":
                # C-11.10: a command a shell reads as given, the task one argument after `--`.
                fields["release"] = task_holds.release_command(hold.get("task") or "?")
            lines.append("Held: " + template.format_map({**dict.fromkeys(
                ("behind", "tier", "max_active_attempts", "kept_for", "live", "error_type", "error",
                 "conversation_id", "native_session_id", "tries", "class", "lane_id", "lane", "model",
                 "task", "until", "note"), "?"),
                **{k: v for k, v in fields.items() if v is not None}}))
            if reason == "behind-older-job" and hold.get("key", "").startswith("providers."):
                lines.append(provider_cap_text(hold))
            if reason == "disk" and hold.get("error"):
                lines.append("Disk reading failed: " + hold["error"])
            if reason == "disk":
                lines.append("Disk floor: " + disk_floor_detail(hold))
            if reason == "pin-unadmittable":
                lines.append(f"Fix: resubmit it unpinned, or pinned to another lane (-a or -H), then "
                             f"`subfleet kill {standing.get('job_id')}`; or make {hold.get('lane_id') or 'the lane'} "
                             f"usable again" + (f"; unadmittable since {hold['since']}" if hold.get("since") else ""))
            if reason == "lease-held" and hold.get("queued_behind"):
                # C-6.9, C-26.9: FIFO on a lease; a lease an older job waits for is kept for it.
                lines.append("Queued behind: " + ", ".join(hold["queued_behind"])
                             + " (an older job waiting for " + ", ".join(hold.get("queued") or ["the same lease"])
                             + " takes it first)")
        else:
            lines.append(f"Held: no lane admits it ({reason})")
            if reason == "lanes-unadmittable":
                lines.append(f"Every candidate lane refuses its retry: {pin_refusals(hold)}; "
                             f"{pin_ends(hold.get('fail_at'))}; fix: restore a usable lane")
        if hold.get("for_good"):
            # C-11.8, C-6.9: every lane it could use refuses it for a reason no wait ends.
            lines.append("Every lane it could use refuses it for a reason no wait ends ("
                         + ", ".join(hold["for_good"]) + "); it holds no other job back (C-11.8)")
    else:
        lines.append("Held: no admission pass has reached this job yet")
    if recheck:
        lines.append(f"Rechecks: same verdict {recheck['rechecks'] + 1} times since {recheck['since']}, "
                     f"last {recheck['checked_at']}")
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
            # An operator hold must not escape as a raw closed:<scope>:<end> label.
            shown = []
            rendered_holds = set()
            for reason in reasons:
                matching = [row for row in evaluation.get("closures", ())
                            if row.get("lane_id") == rejection["lane_id"]
                            and reason == f"closed:{row['scope']}:{row['until_at']}"]
                holds = [row for row in matching if _label(row.get("reason")) == "operator-hold"]
                if holds:
                    if reason in rendered_holds:
                        continue
                    rendered_holds.add(reason)
                    shown.extend(closure_text(row) for row in holds)
                    if len(matching) > len(holds):
                        shown.append(reason)
                else:
                    shown.append(reason)
            lines.append(f"  rejected {rejection['lane_id']}: {', '.join(shown)}")
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


# --- Claude limit-reset cards and promotional credits (C-9.10) ----------------

#: What a status other than `ok` tells an operator to do, when there is something.
_CARD_FIX = {
    "login-dead": "sign in again: CLAUDE_CONFIG_DIR={home} claude auth login",
    "no-login": "sign in to read it: CLAUDE_CONFIG_DIR={home} claude auth login",
}


def _money(value: Any) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "$?"


def _holds_something(account: Mapping[str, Any], warned: set[str]) -> bool:
    """An unused card, money on a credit, a claimable credit, a forfeit, or a warning."""
    return bool(account.get("unused_cards")
                or any((credit.get("remaining_dollars") or 0) > 0 for credit in account.get("credits") or [])
                or account.get("claimable") or account.get("recently_lost")
                or account.get("login") in warned)


def card_lines(view: Mapping[str, Any] | None, *, compact: bool = False) -> list[str]:
    """C-9.10: one line per login: its cards, credits and plan, or why they are unknown.

    Cards and credits are shown from the last read that saw them, with that
    read's time when the latest one failed or listed no cards; nothing here is
    ever redeemed.
    `compact` (what `status` prints) shows only the logins holding something
    that can be lost and counts the rest by status; `subfleet cards` shows all.
    """
    view = view or {}
    accounts = [row for row in view.get("accounts") or () if isinstance(row, Mapping)]
    if view.get("disabled"):
        return ["claude reset cards: not read (claude_cards.enabled is false in the policy)"]
    if not view.get("read_at"):
        return ["claude reset cards: not read yet (subfleet cards --refresh)"]
    lines = [f"claude reset cards and credits (read {view['read_at']}; never redeemed by subfleet)"]
    if not accounts:
        lines.append("  no Claude Code logins to read under the logins folder (claude_cards.logins_dir)")
    others: list[str] = []
    if compact:
        warned = {str(row.get("login")) for row in view.get("warnings") or ()}
        rest = [row for row in accounts if not _holds_something(row, warned)]
        accounts = [row for row in accounts if _holds_something(row, warned)]
        if rest:
            tally: dict[str, int] = {}
            for row in rest:
                grants = (row.get("cards") or {}).get("grants") or []
                key = ("card used" if row.get("status") == "ok" and any(g.get("resets_left", 0) == 0 for g in grants)
                       else "card ended unused" if row.get("status") == "ok" and grants
                       else "nothing held" if row.get("status") == "ok" else str(row.get("status") or "unknown"))
                tally[key] = tally.get(key, 0) + 1
            others.append("  others: " + ", ".join(f"{count} {key}" for key, count in sorted(tally.items()))
                          + " (subfleet cards)")
    for account in accounts:
        lanes = account.get("lanes") or []
        by_name = " by name" if account.get("lanes_by") == "label" else ""
        head = f"  {account.get('login')}" + (f" [{', '.join(lanes)}{by_name}]" if lanes else "")
        status = account.get("status") or "unknown"
        plan = account.get("plan") or {}
        parts: list[str] = []
        if status == "lapsed":
            parts.append(f"lapsed ({plan.get('organization_type')}, subscription {plan.get('subscription_status')})")
        elif status != "ok":
            fix = _CARD_FIX.get(status, "").format(home=account.get("home") or "?")
            parts.append(f"{status}: {account.get('detail') or ''}".rstrip(": ") + (f"; {fix}" if fix else ""))
            if account.get("read_at"):
                parts.append(f"as last read {account['read_at']}")
        cards = account.get("cards") or {}
        grants = cards.get("grants") or []
        if status != "lapsed" and account.get("read_at"):
            unused = [grant for grant in grants if grant.get("resets_left", 0) > 0 and not grant.get("ended")]
            ended = [grant for grant in grants if grant.get("resets_left", 0) > 0 and grant.get("ended")]
            for grant in ended:
                parts.append(f"reset card ended unused ({grant['id']}, ended {grant.get('ends_at')})")
            for grant in unused:
                note = ("usable now" if grant.get("usable_now")
                        else "paused" if grant.get("paused") else "not usable now")
                parts.append(f"{grant['resets_left']} unused reset card ({grant['id']}), expires "
                             f"{grant.get('ends_at') or 'unknown'}, {note}"
                             + (", account at its limit" if cards.get("at_limit") else ""))
            # "No reset card" only when none is listed: a card that ended unused is one.
            if grants:
                if not unused and not ended:
                    parts.append("reset card used")
            elif cards and not cards.get("eligible"):
                parts.append(f"no reset card (ineligible: {cards.get('ineligible_reason') or 'unknown'})")
            elif cards:
                parts.append("no reset card")
            unlisted = account.get("cards_unlisted") or {}
            if unlisted:
                why = ("no cards block" if unlisted.get("missing")
                       else f"ineligible: {unlisted.get('ineligible_reason') or 'unknown'}")
                parts.append(f"cards as listed {unlisted.get('listed_at') or 'before'}; "
                             f"the read at {unlisted.get('at')} listed none ({why})")
            for credit in account.get("credits") or []:
                parts.append(f"{credit.get('label')} {_money(credit.get('remaining_dollars'))} of "
                             f"{_money(credit.get('limit_dollars'))} left, expires {credit.get('expires_at') or 'unknown'}")
            if account.get("claimable"):
                parts.append("cloud-session credit claimable, not claimed")
        if account.get("plan_ends_at"):
            parts.append(f"plan ends {account['plan_ends_at']} (declared)")
        for item in account.get("recently_lost") or []:
            if item.get("grant"):
                why = "with the plan" if item.get("reason") == "lapse" else "unused at its end"
                parts.append(f"reset card lost {why} ({item['grant']}, seen {item.get('at')})")
            else:
                why = "plan lapsed" if item.get("reason") == "lapse" else "unspent at its end"
                parts.append(f"{item.get('label')}: {why} with {_money(item.get('remaining_dollars'))} left at "
                             f"the last read (seen {item.get('at')})")
        lines.append(head + ": " + ("; ".join(parts) or status))
    lines.extend(others)
    for warning in view.get("warnings") or ():
        items = _warned_items(warning)
        lines.append(f"  ! {warning.get('kind')}: {warning.get('login')} " + (f"{items} " if items else "")
                     + (f"at {warning['at']}" if warning.get("at") else ""))
    return lines


def _warned_items(warning: Mapping[str, Any]) -> str:
    """The card or credit a warning names: `grant` or `credit`, or a loss's `grants` and `credits`."""
    credits = warning.get("credits") or ()
    names = [warning.get("grant"), warning.get("credit"), *(warning.get("grants") or ()),
             *(row.get("key") if isinstance(row, Mapping) else row for row in credits)]
    return ", ".join(str(name) for name in names if name)
