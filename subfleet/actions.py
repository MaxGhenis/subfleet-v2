"""Bounded, durable gifted-reset actions and reconciliation (C-19, C-23.13, C-23.16)."""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .adapters.codex import CodexAdapter
from .capacity import _iso, _time, build_view, fresh_provider, identity_blocked
from .contracts import HEADROOM_FLOOR, READING_TTL_S

#: C-23.16 (f): automatic redemption is off until an operator turns it on.
#: `headroom_floor_pct` is no longer read: no supply-side sum triggers a spend.
DEFAULTS = {"enabled": False, "min_interval_min": 30.0}
TERMINAL = frozenset({"confirmed", "failed", "unknown"})
#: C-23.16 (f): a file of this name in the state root refuses every list and
#: consume, automatic or an operator's, for as long as it exists.
INHIBIT_MARKER = "no-reset"
#: C-23.16 (d): a lane reset this recently is room until it is shown limited.
RESET_WINDOW = timedelta(days=7)

CAPACITY, LIMITED, CLOSED, UNUSABLE = "capacity", "limited", "closed", "unusable"
#: A lane's best state across a route's models: room beats a limit a credit
#: clears, which beats a limit it does not, which beats no use at all.
_RANK = {UNUSABLE: 0, CLOSED: 1, LIMITED: 2, CAPACITY: 3}
#: Rejections no quota can change: the lane could not take the job anyway.
UNUSABLE_REASONS = frozenset({"excluded", "desktop", "config-dir", "owner-v1", "disabled", "identity-mismatch"})
#: Closures a reset credit is for (C-23.17 releases them) and closures it is not.
LIMIT_CLOSURES = frozenset({"provider-limit", "credits", "cooldown"})
HOLD_CLOSURES = frozenset({"auth-dead", "operator-hold"})
LATCHED_PROBES = frozenset({"auth-dead", "revoked", "auth-revoked", "expired-token", "no-auth"})


def inhibit_path(root: str | os.PathLike) -> Path:
    """C-23.16 (f): the no-reset marker of the state root `root`."""
    return Path(root) / INHIBIT_MARKER


def reset_credit_op_keys(account_key: str, credit_id: str) -> tuple[str, str]:
    """Canonical identity plus the prefix used by older v1 imports.

    Existing action evidence is immutable; both spellings must fence the same
    credit even after its temporary reopening and minimum interval expire.
    """
    canonical = account_key + ":" + credit_id
    return canonical, "reset-credit:" + canonical


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
        if row.get("provider") != "codex" or row.get("duplicate_of") or row.get("canonical") is False or row.get("superseded_by"):
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


def _usage_observed(probe: dict) -> datetime | None:
    """C-19.1: a stored usage verdict retains its real observation timestamp."""
    timestamp = probe.get("checked_at")
    if timestamp is None:
        readings = [asdict(item) if is_dataclass(item) else item for item in probe.get("readings", ())]
        observed = [item.get("observed_at") for item in readings
                    if isinstance(item, dict) and item.get("label") in ("provider", "stale-provider")
                    and item.get("observed_at")]
        # Completion of a new monitoring pass must not freshen retained readings.
        timestamp = min(observed) if observed else probe.get("probed_at")
    try:
        return _time(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        return None


def _order(row: dict) -> tuple:
    """C-23.38: furthest weekly reset first, then fewest in flight, then lowest lane number."""
    try:
        reset = _time(_weekly(row)["resets_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        reset = float("-inf")
    number = re.search(r"(?:codex-|lane-)(\d+)$", str(row.get("home") or row["lane_id"]))
    return (-reset, max(0, row.get("in_flight", 0)),
            int(number.group(1)) if number else 1_000_000, row["lane_id"])


def _shadowed(row: Mapping[str, Any]) -> bool:
    return bool(row.get("app_shadowed") or row.get("shadowed_by_app"))


def _redeemable(rows: Iterable[dict]) -> list[dict]:
    """The canonical, enabled, v2-owned Codex lanes: where a credit can go, and (d) looks."""
    return [row for row in rows if row.get("provider") == "codex" and row.get("owner") == "v2"
            and row.get("enabled", True) and row.get("canonical") is not False and not row.get("duplicate_of")]


def _moment(value: Any) -> datetime | None:
    """A stored timestamp, or None: a reader must not raise on one."""
    try:
        return _time(value) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def store_holds(store, *, now: str | datetime) -> dict[str, str]:
    """C-23.16 (e): lane id -> the operator or auth hold in force on it at `now`, read from the store.

    An open `operator-hold` or `auth-dead` closure of any scope, and an
    operator hold's own `lane.held` and `lane.released` events:
    `Store.put_closure` keeps one open closure per lane and scope, so a hold
    that ends before an open provider limit on the account scope leaves no
    `operator-hold` row, though it is in force (as `Timers._card_lanes` reads
    it). Releasing such a hold changes no row and so writes no `lane.released`
    event; the hold then lasts until its own end, which errs toward spending
    nothing.
    """
    stamp = _iso(_time(now))
    found = {row["lane_id"]: row["reason"] for row in store.query(
        "SELECT lane_id,reason FROM closures WHERE reason IN ('auth-dead','operator-hold') "
        "AND released_at IS NULL AND until_at>? ORDER BY closure_id", (stamp,))}
    until: dict[str, str] = {}
    for row in store.query("SELECT lane_id,kind,data_json FROM events WHERE kind IN ('lane.held','lane.released') "
                           "ORDER BY event_id"):
        if not row["lane_id"]:
            continue
        if row["kind"] == "lane.released":
            until.pop(row["lane_id"], None)
            continue
        try:
            value = json.loads(row["data_json"] or "{}").get("until")
        except ValueError:
            continue
        if isinstance(value, str) and value > until.get(row["lane_id"], ""):
            until[row["lane_id"]] = value
    for lane_id, value in until.items():
        if (moment := _moment(value)) is not None and moment > _time(stamp):
            found.setdefault(lane_id, "operator-hold")
    return found


def lane_hold(row: Mapping[str, Any], *, now: str | datetime, held: Mapping[str, str] | None = None) -> str | None:
    """C-23.16 (e): why no credit may go on this lane whatever waits, or None.

    A disabled lane or one another owner holds; an `operator-hold` or
    `auth-dead` hold in force (2026-09-30: a credit was redeemed on a lane under
    an operator hold), from the row's own active closures of any scope or from
    `held` (`store_holds`); a latched credential; a credential that holds
    another account. The row is a lane as a view has it: its `closures` are
    the ones active at `now`.
    """
    if not row.get("enabled", True):
        return "disabled"
    if row.get("owner", "v2") != "v2":
        return "owner"
    if held and row.get("lane_id") in held:
        return held[row["lane_id"]]
    instant = _time(now)
    for item in row.get("closures") or ():
        closure = asdict(item) if is_dataclass(item) else dict(item)
        reason = str(getattr(closure.get("reason"), "value", closure.get("reason")) or "")
        until = _moment(closure.get("until_at"))
        if reason in HOLD_CLOSURES and not closure.get("released_at") and (until is None or until > instant):
            return reason
    probe = row.get("probe") or {}
    if row.get("probe_status") in LATCHED_PROBES or probe.get("status") in LATCHED_PROBES \
            or row.get("revoked_epoch") is not None:
        return "latched"
    if identity_blocked(row):
        return "identity-mismatch"
    return None


def route_lane_state(rejection: Mapping[str, Any], closures: Iterable[Mapping[str, Any]] = ()) -> str:
    """C-23.16 (b): what a lane a job's route rejected is to that job.

    `unusable`: no quota would let it take the job (excluded, the desktop login,
    a config directory a turn cannot use, another owner, disabled, a credential
    that holds another account or is latched, the reserve holding it for
    another model, an auth-dead or operator hold). `limited`: out of quota in a
    way a confirmed reset clears (C-23.17 releases account-wide `provider-limit`
    and `credits` closures and `cooldown` closures of any scope): an
    account-wide limit closure, an imported cooldown, or a fresh measured
    reading below the floor. `closed`: out of quota in a way a reset leaves
    standing, a `provider-limit` or `credits` closure scoped to the job's
    model; no room, but no candidate either. Anything else is `capacity`: a
    lane that is only busy (no free slot, a probe or a pilot holding it, the
    fleet or a parent at its cap) or not measured yet is where the job will run
    once there is room, so it waits. `closures` are the lane's active closures
    in the job's scopes, as the scheduler recorded them beside the rejection.
    """
    reasons = [str(reason) for reason in (rejection.get("reasons") or [rejection.get("reason")]) if reason]
    if any(reason in UNUSABLE_REASONS for reason in reasons) or rejection.get("slot_block") == "credential-latched":
        return UNUSABLE
    if any(reason.startswith("reserve:") and not reason.endswith(":unmeasured") for reason in reasons):
        return UNUSABLE
    closures = list(closures)
    kinds = {str(getattr(row.get("reason"), "value", row.get("reason")) or "") for row in closures}
    if kinds & HOLD_CLOSURES:
        return UNUSABLE
    if any(row.get("scope") != "account" and str(getattr(row.get("reason"), "value", row.get("reason")))
           in ("provider-limit", "credits") for row in closures):
        return CLOSED
    if "below-floor" in reasons or kinds & LIMIT_CLOSURES:
        return LIMITED
    if any(reason.startswith("closed:") for reason in reasons):
        return CLOSED           # a closure this decision carried no row for: no room, no candidate
    return CAPACITY


def demand_verdict(decision: Any) -> dict[str, Any]:
    """C-23.16 (a), (b): does this job's own admission decision make it Codex demand?

    `placeable`: the decision chose a lane. `lane-has-capacity`: some lane of
    its route, of either provider, is capacity by `route_lane_state`, so the
    job waits for it. `codex-demand`: every lane it could use is limited,
    closed or unusable, and at least one is a Codex lane `limited` in a way a
    credit clears (the lanes a credit could reopen for it). `no-codex-demand`:
    nothing a credit can fix.
    A lane is judged by its best state across the models of the route: a
    lane closed for Astra but with a slot coming for Terra is capacity to a
    job whose chain holds both.
    """
    value = asdict(decision) if is_dataclass(decision) else dict(decision)
    chosen = value.get("chosen_lane")
    if chosen:
        return {"verdict": "placeable", "capacity_lanes": [chosen], "limited_lanes": []}
    states: dict[str, tuple[str | None, str]] = {}
    for evaluation in value.get("evaluations") or ():
        closures = [row for row in evaluation.get("closures") or ()]
        for rejection in evaluation.get("rejections") or ():
            lane_id = rejection.get("lane_id")
            state = route_lane_state(rejection, [row for row in closures if row.get("lane_id") == lane_id])
            prior = states.get(lane_id)
            if prior is None or _RANK[state] > _RANK[prior[1]]:
                states[lane_id] = (evaluation.get("provider"), state)
    capacity = sorted(lane for lane, (_, state) in states.items() if state == CAPACITY)
    limited = sorted(lane for lane, (provider, state) in states.items()
                     if provider == "codex" and state == LIMITED)
    verdict = "lane-has-capacity" if capacity else "codex-demand" if limited else "no-codex-demand"
    return {"verdict": verdict, "capacity_lanes": capacity, "limited_lanes": limited}


def lane_condition(row: Mapping[str, Any], *, now: str | datetime, override: bool = False,
                   floor: float = HEADROOM_FLOOR, reading_ttl_s: int = READING_TTL_S,
                   held: Mapping[str, str] | None = None) -> str:
    """C-23.16 (d): a lane's own state, whatever job asks: capacity, limited or unusable.

    Limited only by an account-wide limit closure or, outside a confirmed
    reset's override (C-23.17), a fresh measured account reading below the
    floor. A lane closed for one model still has room for another, a busy lane
    has room once its slot frees, and a lane with no fresh reading has not been
    shown limited, so all three are capacity.
    """
    if (not row.get("enabled", True) or row.get("owner", "v2") != "v2" or row.get("desktop")
            or lane_hold(row, now=now, held=held) is not None):
        return UNUSABLE
    instant = _time(now)
    closures = [item for item in (asdict(c) if is_dataclass(c) else c for c in row.get("closures") or ())
                if item.get("scope") == "account" and not item.get("released_at")
                and _time(item["until_at"]) > instant]
    kinds = {str(getattr(item.get("reason"), "value", item.get("reason")) or "") for item in closures}
    if kinds & LIMIT_CLOSURES:
        return LIMITED
    readings = [asdict(r) if is_dataclass(r) else r for r in row.get("readings") or ()]
    if not override and any(item.get("scope") == "account"
                            and fresh_provider(item, now=instant, reading_ttl_s=reading_ttl_s)
                            and item["utilization"] >= 1 - floor for item in readings):
        return LIMITED
    return CAPACITY


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
    """C-23.16: spend a gifted reset only for a job waiting on exactly that lane, one at a time."""

    def __init__(self, store, policy: dict, adapter_factory: Callable | None = None, *,
                 inhibit: str | os.PathLike | None = None):
        self.store, self.policy = store, policy
        self.adapter_factory = adapter_factory or (lambda lane: CodexAdapter())
        self.actions = Actions(store)
        self.inhibit = Path(inhibit) if inhibit is not None else None
        self._lock = threading.Lock()
        self._http_slots: dict[str, threading.Lock] = {}

    def describe(self) -> dict:
        """C-23.16 (f): what an operator needs to know before trusting the timer."""
        return {"automatic": self._settings()["enabled"], "inhibited_by": self.inhibited()}

    def inhibited(self) -> str | None:
        """C-23.16 (f): the no-reset marker's path while it exists, else None.

        `lexists`, as v1's hold did: a dangling symlink still refuses.
        """
        if self.inhibit is not None and os.path.lexists(self.inhibit):
            return str(self.inhibit)
        return None

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
        values = self.policy.get("reset_credits") or {}
        result = dict(DEFAULTS)
        if isinstance(values.get("enabled"), bool):
            result["enabled"] = values["enabled"]
        value = _number(values.get("min_interval_min"))
        if value is not None and value >= 0:
            result["min_interval_min"] = value
        return result

    def _history(self) -> list[dict]:
        return self.store.query("SELECT * FROM actions WHERE kind='reset-credit' ORDER BY created_at,action_id")

    def _fresh_usage(self, probe: dict, now: datetime) -> bool:
        observed = _usage_observed(probe)
        ttl = self.policy.get("caps", {}).get("reading_ttl_s", 120)
        return observed is not None and 0 <= (now - observed).total_seconds() <= ttl

    def _reconciled(self) -> set[str]:
        return {json.loads(row["data_json"]).get("action_id") for row in self.store.query(
            "SELECT data_json FROM events WHERE kind='action.reconciled'")}

    def _belongs_to_lane(self, action: dict, lane_id: str, lanes: dict | None = None) -> bool:
        """C-1.4, C-19.1: imported home/account subjects retain account authority.

        `lanes` memoizes the lane rows across one pass over many lanes (C-3.7)."""
        if lanes is None:
            lane = self.store.get_lane(lane_id)
        else:
            if lane_id not in lanes:
                lanes[lane_id] = self.store.get_lane(lane_id)
            lane = lanes[lane_id]
        if lane is None:
            return action["subject"] == lane_id
        request = json.loads(action.get("request_json") or "{}")
        account = request.get("account_key")
        if account is not None and account != lane.account_key:
            return False
        if request.get("lane_id") == lane_id or account == lane.account_key:
            return True
        if action["subject"] in (lane_id, lane.account_key):
            return True
        # A home can be rebound to another account; its operation key must
        # still identify this account before an imported action can reopen it.
        return (action["subject"] == lane.home and
                action["op_key"].startswith(lane.account_key + ":"))

    def _floor(self) -> float:
        floor = _number(self.policy.get("headroom_floor"))
        return floor if floor is not None and 0 <= floor <= 1 else HEADROOM_FLOOR

    def _ttl(self) -> int:
        return (self.policy.get("caps") or {}).get("reading_ttl_s", READING_TTL_S)

    def _gate(self, history: list[dict], reconciled: set[str], instant: datetime, settings: dict) -> str | None:
        """C-23.16 (d): one unsettled action, and the minimum interval, from the store's action rows.

        Read from the `actions` table every time, never from memory, so a
        restart grants nothing: the last spend (a `confirmed` consume, or one a
        usage read reconciled) is as far back after a restart as before it.
        """
        if any(row["state"] in ("pending", "executing", "unknown") and row["action_id"] not in reconciled
               for row in history):
            return "unsettled-action"
        last = max((moment for row in history
                    if (row["state"] == "confirmed" or row["action_id"] in reconciled)
                    and (moment := _moment(row["updated_at"])) is not None), default=None)
        if last is not None and (instant - last).total_seconds() < settings["min_interval_min"] * 60:
            return "interval-blocked"
        return None

    def _recent_resets(self, instant: datetime) -> list[dict]:
        """C-23.16 (d): resets that landed inside the last seven days.

        A confirmed consume, or an unknown one whose lane later read open
        (C-19.1's reconciliation): either way the lane was very likely reset.
        """
        reconciled = self._reconciled()
        return [action for action in self._history()
                if (action["state"] == "confirmed"
                    or action["state"] == "unknown" and action["action_id"] in reconciled)
                and (moment := _moment(action["updated_at"])) is not None and moment > instant - RESET_WINDOW]

    def reset_lanes_open(self, rows: Iterable[dict], *, now: str | datetime,
                         held: Mapping[str, str] | None = None) -> list[str]:
        """C-23.16 (d): lanes reset in the last seven days that still have room.

        While one exists no credit is spent, automatic or an operator's,
        whatever the minimum interval says: the lane already reset is where
        waiting work goes next. Busy, unmeasured and freshly reset all read as
        room (`lane_condition`).
        """
        instant = _time(now)
        recent = self._recent_resets(instant)
        if not recent:
            return []
        held = store_holds(self.store, now=instant) if held is None else held
        opened, lanes = [], {}
        context = self.override_context()
        for row in rows:
            if not any(self._belongs_to_lane(action, row["lane_id"], lanes) for action in recent):
                continue
            override = self.confirmed_override(row["lane_id"], now=instant, context=context) is not None
            if lane_condition(row, now=instant, override=override, floor=self._floor(),
                              reading_ttl_s=self._ttl(), held=held) == CAPACITY:
                opened.append(row["lane_id"])
        return sorted(opened)

    def _weekly_has_room(self, row: dict, instant: datetime) -> bool:
        """C-23.16 (c): a fresh measured weekly window with headroom: only a shorter window is full.

        `limit_reached` does not say which window it is; a lane whose own fresh
        weekly reading is under the floor reopens when its five-hour window
        does, and a weekly credit spent there buys a few hours at most.
        """
        readings = [asdict(item) if is_dataclass(item) else item for item in row.get("readings") or ()]
        return any(item.get("scope") == "account" and item.get("window") == "seven_day"
                   and fresh_provider(item, now=instant, reading_ttl_s=self._ttl())
                   and item["utilization"] < 1 - self._floor() for item in readings)

    def _eligible(self, rows: list[dict], instant: datetime, held: Mapping[str, str]) -> list[dict]:
        """C-23.16 (c), (e): no hold on the lane, and its account reports `limit_reached` in a fresh read."""
        context = self.override_context()
        return sorted([row for row in rows if lane_hold(row, now=instant, held=held) is None
                       and (row.get("probe") or {}).get("limit_reached") is True
                       and (row.get("probe") or {}).get("status") in ("ok", "limited")
                       and self._fresh_usage(row.get("probe") or {}, instant)
                       and (row.get("probe") or {}).get("account_key", row["account_key"]) == row["account_key"]
                       and self.confirmed_override(row["lane_id"], now=instant, context=context) is None],
                      key=_order)

    def evaluate(self, snapshot: dict, *, now: str | datetime | None = None,
                 cancel: threading.Event | None = None, deadline: float | None = None,
                 target_lane_id: str | None = None, dry_run: bool = False,
                 demand: list[dict] | Callable[[], list[dict] | None] | None = None,
                 clock: Callable[[], datetime] | None = None) -> dict:
        """C-18.1, C-23.16, C-23.38, C-23.46: at most one credit, on one lane, per pass.

        A credit is spent only for a job in `demand`, the admission view of jobs
        waiting on capacity (`Daemon._reset_demand`), whose own route has no
        lane with room, and only on a limited lane of that route. `demand=None`
        means that view is unknown, and nothing is spent; a callable is asked
        only once every cheaper gate has passed, so a disabled policy never
        evaluates the queue. A confirmed reset makes its job due at once.

        Nothing is spent while `reset_credits.enabled` is false. Automatic: no
        `target_lane_id`. Operator (`target_lane_id`, the `reset codex <lane>`
        verb): the same rule, but only that lane is a candidate; if no waiting
        job could use it, or it is not eligible, nothing is spent and no other
        lane is tried. Both paths keep every guard: the switch, the no-reset
        marker, one unsettled action at a time, the minimum interval, no spend
        while a lane reset in the last seven days still has room (d), no hold on
        the lane (e), a fresh `limit_reached` read of the lane's own account, a
        concrete gifted credit never attempted before, and C-23.46's shadow
        rule. Only the timer refuses a lane whose fresh weekly reading has room
        (only its five-hour window is full).

        Listing takes seconds per lane, so what the pass opened with is judged
        again just before the spend and once more in the transaction that
        writes the action, on the lanes as the store then has them and the
        time `clock` then reads (by default `now` plus the time elapsed): (d),
        and the chosen lane's own eligibility and holds (`lane-changed`). The
        minimum interval stays measured from `now`, the stricter of the two.
        """
        instant = _time(now or snapshot.get("now") or datetime.now(timezone.utc))
        stamp, settings = _iso(instant), self._settings()
        started = time.monotonic()
        deadline = deadline if deadline is not None else started + 60

        def current() -> datetime:
            """C-23.16 (d): the time now, never before the pass's own `now`."""
            read = clock() if clock is not None else instant + timedelta(seconds=time.monotonic() - started)
            return max(instant, _time(read))
        if not self._lock.acquire(blocking=False):
            return {"status": "evaluation-running"}
        try:
            all_rows = [dict(row) for row in snapshot.get("lanes", ())]
            rows = _redeemable(all_rows)
            manual = target_lane_id is not None
            trigger = "operator" if manual else "waiting-demand"
            result = {"status": "disabled", "trigger_reason": trigger,
                      "fleet_credits_remaining": fleet_credits_remaining(all_rows),
                      **({"dry_run": True, "credit_verified": False} if dry_run else {}),
                      **({"target_lane_id": target_lane_id} if manual else {})}
            hold = self.inhibited()
            if hold:
                return {**result, "status": "inhibited", "inhibited_by": hold}
            if not settings["enabled"]:
                # Off is off, for the timer and an operator's lane alike, as before.
                return result
            gate = self._gate(self._history(), self._reconciled(), instant, settings)
            if gate:
                return {**result, "status": gate}
            holds = store_holds(self.store, now=instant)
            opened = self.reset_lanes_open(rows, now=instant, held=holds)
            if opened:
                return {**result, "status": "reset-lane-open", "reset_lanes": opened}
            eligible = self._eligible(rows, instant, holds)
            if manual:
                target = next((row for row in all_rows if row.get("lane_id") == target_lane_id), None)
                held = lane_hold(target, now=instant, held=holds) if target is not None else None
                if held:
                    return {**result, "status": "lane-held", "lane_id": target_lane_id, "hold": held}
                eligible = [row for row in eligible if row["lane_id"] == target_lane_id]
            else:
                # C-23.16 (c): the timer spends no weekly credit where only a shorter
                # window is full; an operator naming the lane may.
                eligible = [row for row in eligible if not self._weekly_has_room(row, instant)]
            if not eligible:
                # No lane a credit could go on: the queue need not be read.
                return {**result, "status": "no-eligible-lane", "candidate_lanes": []}
            reader = demand if callable(demand) else None
            if reader is not None:
                demand = reader()
            if demand is None:
                return {**result, "status": "no-demand",
                        "detail": "No admission view of waiting jobs; nothing is spent without one."}
            waiting = [dict(item) for item in demand]
            result["waiting"] = [{key: item.get(key) for key in
                                  ("job_id", "verdict", "capacity_lanes", "limited_lanes")}
                                 for item in waiting]
            by_lane = {row["lane_id"]: row for row in eligible}
            plan = [(item, sorted([by_lane[lane] for lane in item.get("limited_lanes") or ()
                                   if lane in by_lane], key=_order))
                    for item in waiting if item.get("verdict") == "codex-demand"]
            if not plan:
                return {**result, "status": "no-demand"}
            if not any(candidates for _, candidates in plan):
                return {**result, "status": "no-eligible-lane", "candidate_lanes": []}
            if dry_run:
                job, candidates = next((job, candidates) for job, candidates in plan if candidates)
                return {**result, "status": "would-evaluate",
                        "candidate_lanes": [row["lane_id"] for row in candidates], "job_id": job.get("job_id"),
                        "detail": "Cached eligibility only; execution must list a concrete gifted credit."}
            listed: dict[str, tuple[Any, list[dict]]] = {}
            selected, missed = None, None
            for job, candidates in plan:
                if not candidates:
                    continue
                choice, status = self._select(candidates, rows, listed, cancel, deadline, result)
                if status == "cancelled":
                    return {**result, "status": "cancelled"}
                if choice is not None:
                    selected = (job, *choice)
                    break
                missed = missed or status
            if selected is None:
                return {**result, "status": missed or "no-concrete-credit"}
            job, row, lane, adapter, credit = selected
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                return {**result, "status": "cancelled"}
            job_id = job.get("job_id")
            # C-23.16 (c)-(e): while entitlements were listed a limit closure can
            # have expired, a hold landed, or a reading or release landed. Judge
            # the lanes again as they stand now; the job's own route is judged after.
            refused = self._refused(all_rows, current(), lane.lane_id, manual=manual)
            if refused:
                return {**result, **refused, "job_id": job_id}
            if reader is not None:
                # Listing can take seconds per lane. Judge the job again just
                # before spending: a lane on its route may have opened meanwhile.
                again = next((item for item in reader() or () if item.get("job_id") == job_id), None)
                if (again is None or again.get("verdict") != "codex-demand"
                        or lane.lane_id not in (again.get("limited_lanes") or ())):
                    return {**result, "status": "demand-changed", "job_id": job_id,
                            **({"verdict": again.get("verdict")} if again else {})}
            action_id, holder = str(uuid.uuid4()), str(uuid.uuid4())
            request = {"credit_id": credit["id"], "redeem_request_id": str(uuid.uuid4()),
                       "account_key": lane.account_key, "lane_id": lane.lane_id,
                       "trigger_reason": trigger, "usage_before": {"limit_reached": True,
                       "weekly_reset_at": _weekly(row).get("resets_at")},
                       "job_id": job_id, "demand": {key: job.get(key) for key in
                                                    ("verdict", "capacity_lanes", "limited_lanes")}}
            with self.store.transaction("action.pending", lane_id=lane.lane_id,
                                        data={"action_id": action_id}) as conn:
                bound = self.store.get_lane(lane.lane_id)
                if (bound is None or bound.owner != 'v2' or not bound.enabled
                        or bound.account_key != lane.account_key
                        or bound.credential != lane.credential):
                    return {**result, 'status': 'lane-changed', "lane_id": lane.lane_id}
                hold = self.inhibited()
                if hold:
                    return {**result, "status": "inhibited", "inhibited_by": hold}
                # A second ResetCredits component cannot cross the persisted fleet gate.
                gate = self._gate(self._history(), self._reconciled(), instant, settings)
                if gate:
                    return {**result, "status": gate}
                # C-23.16 (c)-(e): once more where the action is written, so a lane
                # that opened, or a hold that landed, after the last look still
                # stops it and fences nothing.
                refused = self._refused(all_rows, current(), lane.lane_id, manual=manual)
                if refused:
                    return {**result, **refused, "job_id": job_id}
                waiting_row = conn.execute("SELECT state,wait_reason,cancel_requested_at FROM jobs WHERE job_id=?",
                                           (job_id,)).fetchone()
                if waiting_row is None or waiting_row[0] != "waiting" or waiting_row[1] != "capacity" or waiting_row[2]:
                    return {**result, "status": "demand-gone", "job_id": job_id}
                if conn.execute("SELECT 1 FROM actions WHERE op_key IN (?,?)",
                                reset_credit_op_keys(lane.account_key, credit["id"])).fetchone():
                    return {**result, "status": "already-attempted"}
                self.store.add_action(action_id=action_id, kind="reset-credit", op_key=lane.account_key + ":" + credit["id"],
                                      subject=lane.lane_id, request_json=_json(request), created_at=stamp, updated_at=stamp)
            if not self.actions.claim(action_id, holder, now=stamp):
                return {**result, "status": "claim-lost"}
            timeout = max(.001, min(15., deadline - time.monotonic()))

            def consume() -> dict:
                # The consume boundary itself refuses while the marker exists (C-23.16 (f)).
                hold = self.inhibited()
                if hold:
                    return {"status": "inhibited", "inhibited_by": hold}
                return adapter.consume_reset_credit(lane, credit, request["redeem_request_id"], {}, timeout=timeout)

            consumed = _bounded(consume, cancel, min(deadline, time.monotonic() + timeout),
                                self._http_slots[lane.lane_id])
            if consumed.get("error_type"):
                result["error_type"] = consumed["error_type"]
            windows = consumed.get("windows_reset")
            confirmed = consumed.get("status") == "ok" and consumed.get("code") == "reset" and (
                isinstance(windows, int) and not isinstance(windows, bool) and windows > 0)
            state = "confirmed" if confirmed else "unknown" if consumed.get("status") in (
                "timeout", "network-error", "invalid-response") else "failed"
            with self.store.transaction("reset-credit.result", lane_id=lane.lane_id, data={"action_id": action_id}) as conn:
                if not self.actions.publish(action_id, holder, state, consumed, now=stamp):
                    return {**result, "status": "result-discarded", "action_id": action_id}
                if confirmed:
                    self._release(lane.lane_id, stamp, action_id=action_id)
                    override = {"action_id": action_id, "confirmed_at": stamp,
                                "weekly_reset_at": _iso(instant + timedelta(days=7)), "clock_source": "guessed"}
                    self.store.add_event("reset-credit.confirmed", lane_id=lane.lane_id, data=override)
                    result.update(override=override, fleet_credits_remaining=fleet_credits_remaining(all_rows, spent_lane=lane.lane_id))
                    # C-23.16 (c): the reset is for this job; it is looked at again at once.
                    self._job_due(conn, action_id, job_id, lane.lane_id, stamp)
            return {**result, "status": state, "action_id": action_id, "lane_id": lane.lane_id, "job_id": job_id}
        finally:
            self._lock.release()

    def _job_due(self, conn, action_id: str, job_id: str, lane_id: str, stamp: str, *,
                 original_state: str | None = None) -> None:
        """C-23.16 (c): the job a reset was spent for is due now, if it still waits on capacity.

        Only a capacity wait is brought forward: a workspace wait's clock counts
        its retries (C-6.10)."""
        changed = conn.execute("UPDATE jobs SET next_check_at=? WHERE job_id=? AND state='waiting' "
                               "AND wait_reason='capacity' AND cancel_requested_at IS NULL", (stamp, job_id)).rowcount
        if changed:
            self.store.add_event("reset-credit.job-due", lane_id=lane_id, job_id=job_id,
                                 data={"action_id": action_id, "job_id": job_id, "lane_id": lane_id,
                                       **({"original_state": original_state} if original_state else {})})

    def _lanes_now(self, rows: list[dict], instant: datetime) -> list[dict]:
        """C-23.16 (c)-(e): the redeemable lanes as the store has them at `instant`.

        Each Codex lane's row, readings and closures are read again, so a limit
        closure that expired, a hold or a reading that landed, or a lane an
        operator released or disabled is seen. What only the pass's snapshot
        carries (the probe's verdict, the app login's shadow) is kept from `rows`.
        """
        known = {row["lane_id"]: row for row in rows}
        lanes = [lane for lane in self.store.lane_rows() if lane["provider"] == "codex"]
        view = build_view(lanes, [reading for lane in lanes for reading in self.store.list_readings(lane["lane_id"])],
                          [closure for lane in lanes for closure in self.store.list_closures(lane["lane_id"])],
                          now=instant, reading_ttl_s=self._ttl())
        merged = []
        for row in view["lanes"]:
            prior = known.get(row["lane_id"], {})
            merged.append({**prior, **row, "in_flight": prior.get("in_flight", row.get("in_flight", 0))})
        return _redeemable(merged)

    def _refused(self, rows: list[dict], instant: datetime, lane_id: str, *, manual: bool) -> dict | None:
        """C-23.16 (c)-(e): what, as the lanes stand at `instant`, now forbids a spend on `lane_id`.

        (d) for either path; for either, the lane's own eligibility: no hold, a
        fresh `limit_reached` read of its account, no override standing and,
        for the timer, no fresh weekly reading with headroom.
        """
        rows = self._lanes_now(rows, instant)
        holds = store_holds(self.store, now=instant)
        opened = self.reset_lanes_open(rows, now=instant, held=holds)
        if opened:
            return {"status": "reset-lane-open", "reset_lanes": opened}
        row = next((row for row in rows if row["lane_id"] == lane_id), None)
        if (row is None or not self._eligible([row], instant, holds)
                or (not manual and self._weekly_has_room(row, instant))):
            return {"status": "lane-changed", "lane_id": lane_id,
                    **({"hold": held} if row is not None and (held := lane_hold(row, now=instant, held=holds)) else {})}
        return None

    def _select(self, candidates: list[dict], rows: list[dict], listed: dict,
                cancel: threading.Event | None, deadline: float, result: dict):
        """C-23.38, C-23.46: the first concrete gift on one job's ordered candidate lanes.

        Returns ((row, lane, adapter, credit), "selected") or (None, status).
        Unshadowed candidates come first, furthest weekly reset first. A
        shadowed candidate is tried only after every unshadowed lane, candidate
        or not, has been listed without a gift: a gift on any unshadowed lane
        excludes it. `listed` caches each lane's entitlements for the pass, so
        no lane is asked twice however many waiting jobs name it. The marker
        refuses listing too (C-23.16 (f)).
        """
        candidates = sorted(candidates, key=_shadowed)
        candidate_ids = {row["lane_id"] for row in candidates}
        if any(_shadowed(row) for row in candidates):
            blockers = sorted([row for row in rows if not _shadowed(row)
                               and row["lane_id"] not in candidate_ids], key=_order)
            candidates = ([row for row in candidates if not _shadowed(row)] + blockers
                          + [row for row in candidates if _shadowed(row)])
        unshadowed_has_gifts = False
        for row in candidates:
            if _shadowed(row) and unshadowed_has_gifts:
                return None, "shadow-excluded"
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                return None, "cancelled"
            if self.inhibited():
                return None, "inhibited"
            lane = self.store.get_lane(row["lane_id"])
            if lane is None or lane.owner != 'v2' or not lane.enabled:
                continue
            if lane.lane_id not in listed:
                adapter = self.adapter_factory(lane)
                timeout = max(.001, min(15., deadline - time.monotonic()))
                guard = self._http_slots.setdefault(lane.lane_id, threading.Lock())
                answer = _bounded(lambda adapter=adapter, lane=lane, timeout=timeout:
                                  adapter.list_reset_credits(lane, {}, timeout=timeout), cancel,
                                  min(deadline, time.monotonic() + timeout), guard)
                if answer.get("error_type"):
                    result["error_type"] = answer["error_type"]
                listed[lane.lane_id] = (adapter, gifted_credits(answer))
            adapter, gifts = listed[lane.lane_id]
            unshadowed_has_gifts = unshadowed_has_gifts or (not _shadowed(row) and bool(gifts))
            if row["lane_id"] not in candidate_ids and gifts:
                return None, "shadow-excluded"
            concrete = [credit for credit in gifts if not self.store.one(
                "SELECT action_id FROM actions WHERE op_key IN (?,?)",
                reset_credit_op_keys(lane.account_key, credit["id"]))]
            if concrete:
                return (row, lane, adapter, concrete[0]), "selected"
        return None, "no-concrete-credit"

    def _release(self, lane_id: str, now: str, *, action_id: str) -> None:
        with self.store.transaction("reset-credit.closures-released", lane_id=lane_id,
                                    data={"action_id": action_id}) as conn:
            conn.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND released_at IS NULL "
                         "AND (reason IN ('local-backoff','cooldown') OR (scope='account' AND reason IN ('provider-limit','credits')))",
                         (now, lane_id))

    def settle_by_usage(self, lane_id: str, probe: dict, *, now: str | datetime | None = None) -> dict | None:
        """C-19.1, C-23.13: append read reconciliation; preserve the original result.

        C-23.16 (c): an `unknown` consume spent for a waiting job is a spend
        once its lane reads open; in the same transaction the job, if it still
        waits on capacity, is made due at once, exactly as a confirmation would.
        """
        instant = _time(now or datetime.now(timezone.utc))
        stamp = _iso(instant)
        if probe.get("status") != "ok" or probe.get("limit_reached") is not False or probe.get("allowed") is False:
            return None
        lane = self.store.get_lane(lane_id)
        if lane is not None and probe.get("account_key") is not None and probe["account_key"] != lane.account_key:
            return None
        if not self._fresh_usage(probe, instant):
            return None
        observed = _usage_observed(probe)
        reconciled, result = self._reconciled(), None
        for action in self._history():
            if not self._belongs_to_lane(action, lane_id) or action["action_id"] in reconciled or action["state"] not in ("unknown", "confirmed"):
                continue
            if observed < _time(action["updated_at"]):
                continue
            result = {"action_id": action["action_id"], "effective_state": "settled",
                      "outcome": "usage-open", "observed_at": _iso(observed),
                      "original_state": action["state"], "reconciled_at": stamp}
            with self.store.transaction("action.reconciliation", lane_id=lane_id) as conn:
                self.store.add_event("action.reconciled", lane_id=lane_id, data=result)
                self._release(lane_id, stamp, action_id=action["action_id"])
                if action["state"] == "unknown":
                    try:
                        job_id = json.loads(action.get("request_json") or "{}").get("job_id")
                    except ValueError:
                        job_id = None
                    if isinstance(job_id, str) and not self.store.one(
                            "SELECT 1 FROM attempts WHERE job_id=? AND reserved_at>=?", (job_id, action["updated_at"])):
                        self._job_due(conn, action["action_id"], job_id, lane_id, stamp, original_state="unknown")
        return result

    def override_context(self) -> dict:
        """What `confirmed_override` reads, read once for a pass over every lane.

        A view asks about each of its ~20 lanes, and each question read the
        whole reset-credit history, every reconciliation event and a lane row
        per action: hundreds of statements per view, several views per
        admission pass (C-3.7, 2026-09-25)."""
        return {"reconciled": self._reconciled(), "history": self._history(), "lanes": {}}

    def confirmed_override(self, lane_id: str, *, now: str | datetime | None = None,
                           context: dict | None = None) -> dict | None:
        """C-23.17: reopen during usage propagation without inventing percentages."""
        instant = _time(now or datetime.now(timezone.utc))
        context = context or self.override_context()
        reconciled, lanes = context["reconciled"], context["lanes"]
        for action in reversed(context["history"]):
            if not self._belongs_to_lane(action, lane_id, lanes) or action["state"] != "confirmed" or action["action_id"] in reconciled:
                continue
            confirmed = _time(action["updated_at"])
            if confirmed + timedelta(days=7) <= instant:
                return None
            return {"action_id": action["action_id"], "confirmed_at": _iso(confirmed),
                    "weekly_reset_at": _iso(confirmed + timedelta(days=7)), "clock_source": "guessed"}
        return None
