"""Bounded, durable gifted-reset actions and reconciliation (C-19, C-23.13)."""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .adapters.codex import CodexAdapter
from .capacity import _iso, _time

DEFAULTS = {"enabled": True, "headroom_floor_pct": 15.0, "min_interval_min": 30.0}
TERMINAL = frozenset({"confirmed", "failed", "unknown"})


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=lambda item: asdict(item) if is_dataclass(item) else str(item))


def _number(value: Any) -> float | None:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def gifted_credits(listed: dict) -> list[dict]:
    """C-23.7, C-23.16: concrete available gifts, never purchased capacity."""
    credits = listed.get("credits")
    if listed.get("status") != "ok" or not isinstance(credits, list):
        return []
    return [credit for credit in credits if isinstance(credit, dict)
            and credit.get("reset_type") == "codex_rate_limits"
            and credit.get("status") == "available" and isinstance(credit.get("id"), str)
            and credit["id"] and credit.get("source") not in ("paid", "purchase", "purchased")
            and credit.get("gifted") is not False]


def fleet_credits_remaining(rows: list[dict], *, spent_lane: str | None = None) -> int | None:
    """C-23.18: a partial fleet count is unknown, even after one confirmed spend."""
    counts, seen = [], set()
    for row in rows:
        if row.get("provider") != "codex" or row.get("duplicate_of") or row.get("canonical") is False:
            continue
        key = row.get("account_key") or row["lane_id"]
        if key in seen:
            continue
        seen.add(key)
        probe = row.get("probe") or {}
        count = (row.get("reset_credits") or probe.get("reset_credits") or {}).get("available")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        counts.append(count)
    return max(0, sum(counts) - (1 if spent_lane is not None else 0)) if seen else None


def _weekly(row: dict) -> dict:
    readings = [asdict(item) if is_dataclass(item) else item for item in row.get("readings", ())]
    values = [item for item in readings if item.get("window") == "seven_day"
              and item.get("scope") == "account" and item.get("label") in ("provider", "stale-provider")]
    return max(values, key=lambda item: item.get("observed_at", ""), default={})


def _order(row: dict) -> tuple:
    try:
        reset = _time(_weekly(row)["resets_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        reset = float("-inf")
    number = re.search(r"(?:codex-|lane-)(\d+)$", str(row.get("home") or row["lane_id"]))
    return (-reset, max(0, row.get("in_flight", 0)),
            int(number.group(1)) if number else 1_000_000, row["lane_id"])


def _bounded(fn: Callable[[], dict], cancel: threading.Event | None,
             deadline: float, guard: threading.Lock | None = None) -> dict:
    """C-16.4: providers ignoring their timeout cannot hold a timer worker."""
    if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
        return {"status": "timeout", "error_type": "CancelledError"}
    if guard is not None and not guard.acquire(blocking=False):
        return {"status": "network-error", "error_type": "PreviousRequestRunning"}
    done = threading.Event()
    result: list[dict] = []
    def run():
        try:
            result.append(fn())
        except Exception as exc:
            result.append({"status": "timeout" if isinstance(exc, TimeoutError) else "network-error",
                           "error_type": type(exc).__name__})
        finally:
            if guard is not None:
                guard.release()
            done.set()
    threading.Thread(target=run, name="reset-credit-http", daemon=True).start()
    while not done.wait(min(.05, max(0, deadline - time.monotonic()))):
        if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
            return {"status": "timeout", "error_type": "CancelledError" if cancel and cancel.is_set() else "TimeoutError"}
    return result[0]


class Actions:
    """C-23.13: compare the durable holder before accepting one terminal result."""

    def __init__(self, store):
        self.store = store

    def claim(self, action_id: str, holder: str, *, now: str) -> bool:
        with self.store.transaction("action.executing", data={"action_id": action_id, "holder": holder}) as conn:
            action = self.store.get_action(action_id)
            if not action or action["state"] != "pending":
                return False
            request = json.loads(action["request_json"] or "{}")
            request["holder"] = holder
            conn.execute("UPDATE actions SET state='executing',request_json=?,updated_at=? WHERE action_id=? AND state='pending'",
                         (_json(request), now, action_id))
            return True

    def publish(self, action_id: str, holder: str, state: str, result: dict, *, now: str) -> bool:
        if state not in TERMINAL:
            raise ValueError("action result must be terminal")
        with self.store.transaction("action.result", data={"action_id": action_id, "state": state}) as conn:
            action = self.store.get_action(action_id)
            request = json.loads(action["request_json"] or "{}") if action else {}
            if not action or action["state"] != "executing" or request.get("holder") != holder:
                self.store.add_event("action.result-discarded", data={"action_id": action_id,
                                     "holder": holder, "offered_state": state})
                return False
            conn.execute("UPDATE actions SET state=?,result_json=?,updated_at=? WHERE action_id=?",
                         (state, _json(result), now, action_id))
            return True


class ResetCredits:
    def __init__(self, store, policy: dict, adapter_factory: Callable | None = None):
        self.store, self.policy = store, policy
        self.adapter_factory = adapter_factory or (lambda lane: CodexAdapter())
        self.actions = Actions(store)
        self._lock = threading.Lock()
        self._http_slots: dict[str, threading.Lock] = {}

    def recover(self, *, now: str | datetime | None = None) -> dict:
        """C-19.1, C-23.13: orphan execution is unknown; no holder result is forged.

        Call only after daemon singleton/recovery, before starting timer workers.
        Pending intents have not called the provider and can be claimed and failed.
        An executing orphan changes classification without writing result JSON.
        """
        stamp = _iso(_time(now or datetime.now(timezone.utc)))
        recovered = []
        for action in self._history():
            if action["state"] == "pending":
                holder = "recovery:" + str(uuid.uuid4())
                if self.actions.claim(action["action_id"], holder, now=stamp):
                    self.actions.publish(action["action_id"], holder, "failed",
                                         {"status": "aborted-before-submission"}, now=stamp)
                    recovered.append(action["action_id"])
            elif action["state"] == "executing":
                with self.store.transaction("action.holder-lost", data={"action_id": action["action_id"],
                                             "effective_state": "unknown"}) as conn:
                    conn.execute("UPDATE actions SET state='unknown' WHERE action_id=? AND state='executing'",
                                 (action["action_id"],))
                recovered.append(action["action_id"])
        return {"recovered": recovered}

    def _settings(self) -> dict:
        values = self.policy.get("reset_credits", {})
        result = dict(DEFAULTS)
        if isinstance(values.get("enabled"), bool):
            result["enabled"] = values["enabled"]
        for key in ("headroom_floor_pct", "min_interval_min"):
            value = _number(values.get(key))
            if value is not None and value >= 0:
                result[key] = value
        return result

    def _history(self) -> list[dict]:
        return self.store.query("SELECT * FROM actions WHERE kind='reset-credit' ORDER BY created_at,action_id")

    def _reconciled(self) -> set[str]:
        return {json.loads(row["data_json"]).get("action_id") for row in self.store.query(
            "SELECT data_json FROM events WHERE kind='action.reconciled'")}

    def evaluate(self, snapshot: dict, *, now: str | datetime | None = None,
                 cancel: threading.Event | None = None, deadline: float | None = None) -> dict:
        """C-18.1, C-23.16–18, C-23.38, C-23.46: at most one credit per pass."""
        instant = _time(now or snapshot.get("now") or datetime.now(timezone.utc))
        stamp, settings = _iso(instant), self._settings()
        deadline = deadline if deadline is not None else time.monotonic() + 60
        if not self._lock.acquire(blocking=False):
            return {"status": "evaluation-running"}
        try:
            rows = [dict(row) for row in snapshot.get("lanes", ()) if row.get("provider") == "codex"
                    and row.get("owner") == "v2" and row.get("enabled", True)
                    and row.get("canonical") is not False and not row.get("duplicate_of")]
            result = {"status": "disabled", "fleet_credits_remaining": fleet_credits_remaining(rows)}
            if not settings["enabled"]:
                return result
            history, reconciled = self._history(), self._reconciled()
            if any(row["state"] in ("pending", "executing", "unknown") and row["action_id"] not in reconciled for row in history):
                return {**result, "status": "unsettled-action"}
            last = max((_time(row["updated_at"]) for row in history
                        if row["state"] == "confirmed" or row["action_id"] in reconciled), default=None)
            if last is not None and (instant - last).total_seconds() < settings["min_interval_min"] * 60:
                return {**result, "status": "interval-blocked"}
            dispatchable = [row for row in rows if row.get("dispatchable", not row.get("closures")
                            and (row.get("probe") or {}).get("status") == "ok")]
            headroom = sum(max(0, 1 - (_number(_weekly(row).get("utilization")) or 0)) * 100
                           for row in dispatchable if _number(_weekly(row).get("utilization")) is not None)
            trigger = "no-dispatchable-lanes" if not dispatchable else "weekly-headroom-below-floor"
            if dispatchable and headroom >= settings["headroom_floor_pct"]:
                return {**result, "status": "not-triggered", "weekly_headroom_pct": headroom}
            result.update(trigger_reason=trigger, weekly_headroom_pct=headroom)
            candidates = sorted([row for row in rows if (row.get("probe") or {}).get("limit_reached") is True
                                 and (row.get("probe") or {}).get("status") in ("ok", "limited")], key=_order)
            # Shadowing excludes while ANY eligible unshadowed lane has a concrete gift.
            candidates.sort(key=lambda row: bool(row.get("app_shadowed") or row.get("shadowed_by_app")))
            selected = None
            for row in candidates:
                if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                    return {**result, "status": "cancelled"}
                lane = self.store.get_lane(row["lane_id"])
                if lane is None:
                    continue
                adapter = self.adapter_factory(lane)
                timeout = max(.001, min(15., deadline - time.monotonic()))
                guard = self._http_slots.setdefault(lane.lane_id, threading.Lock())
                listed = _bounded(lambda: adapter.list_reset_credits(lane, {}, timeout=timeout), cancel,
                                  min(deadline, time.monotonic() + timeout), guard)
                concrete = [credit for credit in gifted_credits(listed) if not self.store.one(
                    "SELECT action_id FROM actions WHERE op_key=?", (lane.account_key + ":" + credit["id"],))]
                if concrete:
                    selected = (row, lane, adapter, concrete[0])
                    break
            if selected is None:
                return {**result, "status": "no-concrete-credit"}
            row, lane, adapter, credit = selected
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                return {**result, "status": "cancelled"}
            action_id, holder = str(uuid.uuid4()), str(uuid.uuid4())
            request = {"credit_id": credit["id"], "redeem_request_id": str(uuid.uuid4()),
                       "account_key": lane.account_key, "lane_id": lane.lane_id,
                       "trigger_reason": trigger, "usage_before": {"limit_reached": True,
                       "weekly_reset_at": _weekly(row).get("resets_at")}}
            with self.store.transaction("action.pending", lane_id=lane.lane_id,
                                        data={"action_id": action_id}) as conn:
                # A second ResetCredits component cannot cross the persisted fleet gate.
                current = self._history()
                if any(item["state"] in ("pending", "executing", "unknown") and item["action_id"] not in reconciled for item in current):
                    return {**result, "status": "unsettled-action"}
                current_last = max((_time(item["updated_at"]) for item in current
                                    if item["state"] == "confirmed" or item["action_id"] in reconciled), default=None)
                if current_last is not None and (instant - current_last).total_seconds() < settings["min_interval_min"] * 60:
                    return {**result, "status": "interval-blocked"}
                if conn.execute("SELECT 1 FROM actions WHERE op_key=?", (lane.account_key + ":" + credit["id"],)).fetchone():
                    return {**result, "status": "already-attempted"}
                self.store.add_action(action_id=action_id, kind="reset-credit", op_key=lane.account_key + ":" + credit["id"],
                                      subject=lane.lane_id, request_json=_json(request), created_at=stamp, updated_at=stamp)
            if not self.actions.claim(action_id, holder, now=stamp):
                return {**result, "status": "claim-lost"}
            timeout = max(.001, min(15., deadline - time.monotonic()))
            consumed = _bounded(lambda: adapter.consume_reset_credit(lane, credit, request["redeem_request_id"],
                                {}, timeout=timeout), cancel, min(deadline, time.monotonic() + timeout),
                                self._http_slots[lane.lane_id])
            windows = consumed.get("windows_reset")
            confirmed = consumed.get("status") == "ok" and consumed.get("code") == "reset" and (
                isinstance(windows, int) and not isinstance(windows, bool) and windows > 0)
            state = "confirmed" if confirmed else "unknown" if consumed.get("status") in (
                "timeout", "network-error", "invalid-response") else "failed"
            if not self.actions.publish(action_id, holder, state, consumed, now=stamp):
                return {**result, "status": "result-discarded", "action_id": action_id}
            if confirmed:
                self._release(lane.lane_id, stamp, action_id=action_id)
                override = {"action_id": action_id, "confirmed_at": stamp,
                            "weekly_reset_at": _iso(instant + timedelta(days=7)), "clock_source": "guessed"}
                self.store.add_event("reset-credit.confirmed", lane_id=lane.lane_id, data=override)
                result.update(override=override, fleet_credits_remaining=fleet_credits_remaining(rows, spent_lane=lane.lane_id))
            return {**result, "status": state, "action_id": action_id, "lane_id": lane.lane_id}
        finally:
            self._lock.release()

    def _release(self, lane_id: str, now: str, *, action_id: str) -> None:
        with self.store.transaction("reset-credit.closures-released", lane_id=lane_id,
                                    data={"action_id": action_id}) as conn:
            conn.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND released_at IS NULL "
                         "AND (reason IN ('local-backoff','cooldown') OR (scope='account' AND reason IN ('provider-limit','credits')))",
                         (now, lane_id))

    def settle_by_usage(self, lane_id: str, probe: dict, *, now: str | datetime | None = None) -> dict | None:
        """C-19.1, C-23.13: append read reconciliation; preserve the original result."""
        instant = _time(now or datetime.now(timezone.utc))
        if probe.get("status") != "ok" or probe.get("limit_reached") is not False or probe.get("allowed") is False:
            return None
        observed = _time(probe.get("checked_at") or instant)
        reconciled, result = self._reconciled(), None
        for action in self._history():
            if action["subject"] != lane_id or action["action_id"] in reconciled or action["state"] not in ("unknown", "confirmed"):
                continue
            if observed < _time(action["updated_at"]):
                continue
            result = {"action_id": action["action_id"], "effective_state": "settled",
                      "outcome": "usage-open", "observed_at": _iso(observed),
                      "original_state": action["state"]}
            with self.store.transaction("action.reconciliation", lane_id=lane_id):
                self.store.add_event("action.reconciled", lane_id=lane_id, data=result)
                self._release(lane_id, _iso(instant), action_id=action["action_id"])
        return result

    def confirmed_override(self, lane_id: str, *, now: str | datetime | None = None) -> dict | None:
        """C-23.17: reopen during usage propagation without inventing percentages."""
        instant = _time(now or datetime.now(timezone.utc))
        reconciled = self._reconciled()
        for action in reversed(self._history()):
            if action["subject"] != lane_id or action["state"] != "confirmed" or action["action_id"] in reconciled:
                continue
            confirmed = _time(action["updated_at"])
            if confirmed + timedelta(days=7) <= instant:
                return None
            return {"action_id": action["action_id"], "confirmed_at": _iso(confirmed),
                    "weekly_reset_at": _iso(confirmed + timedelta(days=7)), "clock_source": "guessed"}
        return None
