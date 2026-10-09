"""The menu bar's read-only snapshot projection (C-9.1, C-18.1)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capacity import desktop_excluded, identity_blocked
from .lane_identity import identity_shadowed
from .guardian import atomic_publish
from .quota_projection import weekly_projections


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
            or lane.get("provider") == "claude" and (desktop_excluded(lane) or identity_blocked(lane)
                                                     or identity_shadowed(lane))):
        return False
    if lane.get("probe_state") is not None:
        # C-5.7a, C-18.1: a probe holds this lane's slot, and admission places
        # nothing here until its lease is released, however fresh the readings.
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


#: C-9.1: the only labels a percentage is ever rendered from.
PERCENT_LABELS = ("provider", "stale-provider")
_ACCOUNT_WINDOW_ORDER = {"five_hour": 0, "seven_day": 1}


def _fraction(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def _iso_or_none(value: Any) -> str | None:
    if not value:
        return None
    try:
        return timestamp(value)
    except (AttributeError, TypeError, ValueError):      # not an ISO string or a datetime
        return None


def scoped_windows(lane: Mapping[str, Any], model_names: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """C-9.1, C-29.6: every window a provider reported for this lane, keyed by (scope, window).

    A reading's scope is `account` or a model id (C-9.8, C-9.9: the Fable weekly
    window is scope `claude-fable-5-1`, window `seven_day`), so a model-scoped
    window and the account window of the same name are two rows and neither can
    replace the other. Only `provider` and `stale-provider` readings with a
    fraction in [0, 1] become rows; `admission-observed` evidence (window
    `admission`) and every other label carry no percentage and are left out.
    Two readings for one key keep the newer, so the list is keyed however the
    caller assembled the lane's readings.
    """
    names = model_names or {}
    newest: dict[tuple[str, str], tuple[tuple[str, int], dict[str, Any]]] = {}
    for reading in lane.get("readings", ()):
        label = reading.get("label")
        if label not in PERCENT_LABELS or not _fraction(reading.get("utilization")):
            continue
        scope, window = str(reading.get("scope") or "account"), str(reading.get("window"))
        order = (str(reading.get("observed_at") or ""), int(reading.get("reading_id") or 0))
        row = {"scope": scope, "window": window,
               "model": None if scope == "account" else names.get(scope),
               "status": label, "stale": label == "stale-provider",
               "used_percent": reading["utilization"] * 100,
               "reset_at": _iso_or_none(reading.get("resets_at")),
               "source": reading.get("source"), "as_of": reading.get("observed_at"),
               "age_s": reading.get("age_s")}
        if (scope, window) not in newest or order > newest[(scope, window)][0]:
            newest[(scope, window)] = (order, row)

    def key(row: dict[str, Any]) -> tuple:
        if row["scope"] == "account":
            return (0, _ACCOUNT_WINDOW_ORDER.get(row["window"], 2), row["window"], "")
        return (1, 0, row["model"] or row["scope"], row["window"])
    return sorted((row for _, row in newest.values()), key=key)


def claude_earliest_reset(accounts: list[Mapping[str, Any]], now: datetime) -> str | None:
    """C-29.6, D-27: the soonest future reset of an account window on a Claude lane
    admission could use (enabled, owned by v2, not the desktop login while Claude
    Code uses it (C-10.3), identity not mismatched, and not a lane whose account
    another lane takes the work of (C-10.8)). A reset already past says the
    reading is old, not when capacity returns.

    A lane whose credential proved to hold another account (C-10.6) stays enabled
    (`daemon._record_identity` only marks it) and keeps its last bound readings as
    `stale-provider`, but admission never places work there (`capacity.open_lanes`,
    `scheduler` reason `identity-mismatch`), so its reset is not when capacity
    returns. An auth-dead lane is already disabled wherever auth-dead is found."""
    resets = [instant(window["reset_at"]) for account in accounts
              if account.get("enrolled") and account.get("owner", "v2") == "v2"
              and not (account.get("active") and account.get("desktop_in_use", True) is not False)
              and not identity_blocked(account) and not identity_shadowed(account)
              for window in account.get("windows", ())
              if window["scope"] == "account" and window.get("reset_at")]
    future = [value for value in resets if value > now]
    return timestamp(min(future)) if future else None


#: C-18.2: how many finished jobs the menu keeps in view.
RECENT_JOBS = 8
LIVE_JOB_STATES = ("queued", "running", "waiting")
_LIVE_ORDER = {"running": 0, "waiting": 1, "queued": 2}
#: C-26.1: the kind of job the conversation dispatcher creates. Turn jobs are the
#: conversation's (C-26.12), so the jobs section leaves them out (C-18.2) and
#: the conversations section carries them (C-29.6).
TURN_KIND = "turn"
#: Design §3: a turn job is named `turn-<conversation id>`.
TURN_NAME_PREFIX = "turn-"


def displayed_job_ids(snapshot: Mapping[str, Any]) -> list[str]:
    """The jobs `build_status` will show, so a caller can fetch only their batch labels."""
    live, recent = _select_jobs(snapshot)
    return [row["job_id"] for row in (*live, *recent)]


def attach_batches(store: Any, snapshot: dict[str, Any]) -> None:
    """C-18.2: batch labels live in `job.submitted` events (C-17.7), not on the job row."""
    ids = displayed_job_ids(snapshot)
    if not ids:
        snapshot["batches"] = {}
        return
    marks = ",".join("?" for _ in ids)
    # `+kind`: by job id (events_job), not a walk of every job.submitted event (C-3.7).
    rows = store.query(f"SELECT job_id,data_json FROM events WHERE +kind='job.submitted' "
                       f"AND job_id IN ({marks}) AND data_json LIKE '%\"batch\"%'", ids)
    found = {row["job_id"]: json.loads(row["data_json"] or "{}").get("batch") for row in rows}
    snapshot["batches"] = {job_id: batch for job_id, batch in found.items() if isinstance(batch, dict)}


def _select_jobs(snapshot: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """C-18.2: detached work only; a turn job is its conversation's (C-26.12)."""
    jobs = [row for row in snapshot.get("jobs", ()) if row.get("kind") != TURN_KIND]
    live = sorted((row for row in jobs if row.get("state") in LIVE_JOB_STATES),
                  key=lambda row: (_LIVE_ORDER[row["state"]], row.get("created_at") or "", row["job_id"]))
    done = sorted((row for row in jobs if row.get("state") not in LIVE_JOB_STATES and row.get("finished_at")),
                  key=lambda row: (row["finished_at"], row["job_id"]), reverse=True)
    return live, done[:RECENT_JOBS]


def _latest_attempts(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for attempt in snapshot.get("attempts", ()):
        held = latest.get(attempt["job_id"])
        if held is None or (attempt.get("seq") or 0) >= (held.get("seq") or 0):
            latest[attempt["job_id"]] = attempt
    return latest


def _job_row(job: Mapping[str, Any], latest: Mapping[str, Mapping[str, Any]],
             batches: Mapping[str, Any]) -> dict[str, Any]:
    """C-18.2, C-29.6: one job as the menu shows it, with its `kind`."""
    attempt = latest.get(job["job_id"], {})
    return {"job_id": job["job_id"], "kind": job.get("kind"), "name": job.get("name"), "state": job["state"],
            "wait_reason": job.get("wait_reason") if job["state"] == "waiting" else None,
            "next_check_at": job.get("next_check_at") if job["state"] == "waiting" else None,
            "sandbox": job.get("sandbox"), "workdir": job.get("worktree") or job.get("workdir"),
            "model": attempt.get("model_requested") or job.get("pinned_model"),
            "lane_id": attempt.get("lane_id"), "attempts": attempt.get("seq") or 0,
            "created_at": job.get("created_at"), "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"), "rc": job.get("rc"),
            "batch": batches.get(job["job_id"])}


def _jobs(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """C-18.2: what is running, what is waiting and why, and what just finished."""
    live, recent = _select_jobs(snapshot)
    batches = snapshot.get("batches") or {}
    latest = _latest_attempts(snapshot)
    counts = {state: sum(1 for job in live if job["state"] == state) for state in LIVE_JOB_STATES}
    return {"live": [_job_row(job, latest, batches) for job in live],
            "recent": [_job_row(job, latest, batches) for job in recent], "counts": counts}


def _conversations(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """C-29.6, D-26: conversations apart from detached work.

    `snapshot["conversations"]` is what `conversations.store.status_summary`
    read from `conversations.sqlite3`. Each listed conversation gets its live
    turn job (at most one, C-24.5), found by the turn job's name; `turns` counts
    every live turn job by state. A snapshot nobody read the conversation store
    for says `available: false` and has no counts, so the menu never shows a
    zero it did not observe.
    """
    turns = sorted((job for job in snapshot.get("jobs", ())
                    if job.get("kind") == TURN_KIND and job.get("state") in LIVE_JOB_STATES),
                   key=lambda job: (job.get("created_at") or "", job["job_id"]))
    turn_counts = {state: sum(1 for job in turns if job["state"] == state) for state in LIVE_JOB_STATES}
    by_conversation = {str(job.get("name"))[len(TURN_NAME_PREFIX):]: job for job in turns
                       if str(job.get("name") or "").startswith(TURN_NAME_PREFIX)}
    summary = snapshot.get("conversations")
    if not isinstance(summary, Mapping) or not summary.get("available"):
        error = summary.get("error") if isinstance(summary, Mapping) else None
        return {"available": False, "error": error or "not-read", "counts": None, "items": [],
                "truncated": False, "turns": turn_counts}
    latest, batches = _latest_attempts(snapshot), snapshot.get("batches") or {}
    items = []
    for item in summary.get("items", ()):
        job = by_conversation.get(item.get("conversation_id"))
        items.append({**item, "turn": _job_row(job, latest, batches) if job else None})
    return {"available": True, "error": None, "counts": dict(summary.get("counts") or {}), "items": items,
            "truncated": bool(summary.get("truncated")), "turns": turn_counts}


#: C-18.4: what `status.json` says of each alert in force.
ALERT_FIELDS = ("key", "severity", "subject", "body", "since", "last_sent")


def _alerts(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """C-18.4: the alerts in force, in the order `Alerts.active` gives them."""
    return [{field: row.get(field) for field in ALERT_FIELDS}
            for row in snapshot.get("alerts") or () if isinstance(row, Mapping) and row.get("key")]


#: C-9.10: what `status.json` says of each account's cards and credits. No
#: identity, token or path: the menu needs what is at risk and when.
CARD_ACCOUNT_FIELDS = ("login", "lanes", "lanes_by", "status", "detail", "read_at", "unused_cards", "plan",
                       "plan_ends_at", "credits", "cloud_credit_claim", "claimable", "recently_lost",
                       "cards_unlisted")


def _cards(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """C-9.10: Claude limit-reset cards and promotional credits, read and never redeemed."""
    view = snapshot.get("claude_cards") or {}
    accounts = []
    for account in view.get("accounts") or ():
        if not isinstance(account, Mapping):
            continue
        row = {field: account.get(field) for field in CARD_ACCOUNT_FIELDS}
        row["cards"] = list(((account.get("cards") or {}).get("grants")) or [])
        accounts.append(row)
    return {"read_at": view.get("read_at"), "disabled": bool(view.get("disabled")), "accounts": accounts,
            "warnings": [dict(row) for row in view.get("warnings") or () if isinstance(row, Mapping)]}


def build_status(snapshot: Mapping[str, Any], *, now: str | datetime | None = None) -> dict[str, Any]:
    """C-18.1: retain Swift's Codex/Claude JSON shape with explicit evidence labels.

    C-29.6 adds, beside the shapes the menu already decodes: `windows` on each
    Claude account (every provider-reported window keyed by scope and window),
    `claude.earliest_reset`, `kind` on every job row, and `conversations`.
    `snapshot["model_names"]` (policy model id to short name) labels the
    model-scoped windows; without it they carry only their scope. C-18.4 adds
    `alerts`, the alerts in force; the section is always present.
    C-9.10 adds `claude.cards`, every login's reset cards and credits.
    """
    at = instant(now or snapshot.get("now"))
    model_names = snapshot.get("model_names") or {}
    codex, claude = [], []
    for lane in snapshot.get("lanes", ()):
        if lane.get("superseded_by"):
            continue
        verdict, windows = lane_verdict(lane), _windows(lane)
        common = {"lane_id": lane["lane_id"], "verdict": verdict,
                  "enabled": bool(lane.get("enabled", True)), "owner": lane.get("owner", "v2"),
                  "dispatchable": dispatchable(lane),
                  # C-18.1: the probe holding this lane's slot, if one does.
                  "probe_state": lane.get("probe_state"), "probe_holder": lane.get("probe_holder")}
        common["weekly_projections"] = weekly_projections(lane, now=at, samples=snapshot.get("weekly_samples"))
        if lane.get("identity_status") is not None:
            common["identity_status"] = lane["identity_status"]
        if lane.get("identity_shared_with"):
            # C-10.8: the lanes this credential is one account with, and the one
            # of them that takes the account's work.
            common["identity_shared_with"] = list(lane["identity_shared_with"])
            common["identity_shadowed_by"] = lane.get("identity_shadowed_by")
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
                           "desktop_in_use": lane.get("desktop_in_use"),
                           "enrolled": bool(lane.get("enabled", True)), "probe": probe, "live": live,
                           "oauth_status": verdict, "windows": scoped_windows(lane, model_names)})
    available = [lane for lane in codex if lane["dispatchable"]]
    reset_times = [window["reset_at"] for lane in codex for key, window in lane["windows"].items()
                   if key in {"five_hour", "seven_day"} and window.get("reset_at")]
    accounts = {lane["account_key"]: lane for lane in codex if not lane.get("duplicate_of")}
    credit_counts = [lane["reset_credits_remaining"] for lane in accounts.values()]
    credits = (sum(credit_counts) if credit_counts and all(isinstance(count, int) and not isinstance(count, bool) and count >= 0
                                       for count in credit_counts) else None)
    return {"generated_at": timestamp(at), "offline": bool(snapshot.get("offline", False)),
            "jobs": _jobs(snapshot), "conversations": _conversations(snapshot), "alerts": _alerts(snapshot),
            "codex": {"homes": codex, "fleet": {"total_homes": len(codex), "dispatchable_now": len(available),
                       "best_home": available[0]["home"] if available else None,
                       "earliest_reset": min(reset_times, default=None), "reset_credits_remaining": credits,
                       "probe_held": _probe_held(codex)}},
            "claude": {"accounts": claude, "earliest_reset": claude_earliest_reset(claude, at),
                       "lanes": {"enrolled": sum(row["enrolled"] for row in claude),
                                 "dispatchable_now": sum(row["dispatchable"] for row in claude),
                                 "probe_held": _probe_held(claude)},
                       "cards": _cards(snapshot)}}


def _probe_held(rows: list[dict[str, Any]]) -> int:
    """C-18.1: how many of a section's lanes a probe holds."""
    return sum(row["probe_state"] is not None for row in rows)


def write_status(root: str | Path, snapshot: Mapping[str, Any], *,
                 now: str | datetime | None = None) -> dict[str, Any]:
    """C-8.1, C-18.1: publish one complete post-heal snapshot per probe cycle."""
    result = build_status(snapshot, now=now)
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_publish(root / "status.json", (json.dumps(result, sort_keys=True, allow_nan=False) + "\n").encode())
    return result
