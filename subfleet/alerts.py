"""Snapshot-based alert conditions and durable event latches (C-23.27, C-23.52)."""

from __future__ import annotations

import json
import shlex
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from .status_json import dispatchable, instant, lane_verdict, timestamp


def _home(lane: Mapping[str, Any]) -> str:
    return str(lane.get("home") or lane.get("credential_ref") or lane["lane_id"])


def _login(lane: Mapping[str, Any]) -> str:
    reference = shlex.quote(str(lane.get("credential_ref") or _home(lane)))
    if lane["provider"] == "codex":
        return f"CODEX_HOME={shlex.quote(_home(lane))} codex login; subfleet lanes enroll {reference}"
    return f"claude setup-token; subfleet lanes enroll {reference}"


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def evaluate_conditions(snapshot: Mapping[str, Any], *, now: str | datetime | None = None) -> list[dict[str, Any]]:
    """C-23.27, C-23.45, C-23.52: derive every condition from one post-heal snapshot."""
    at = instant(now or snapshot.get("now"))
    conditions: dict[str, dict[str, Any]] = {}

    def add(key: str, severity: str, subject: str, body: str, *, home: str, **extra: Any) -> None:
        conditions[key] = {"key": key, "severity": severity, "subject": subject,
                           "body": body, "home": home, **extra}

    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    lanes = [lane for lane in snapshot.get("lanes", ()) if lane.get("owner", "v2") == "v2" and not lane.get("superseded_by")]
    for lane in lanes:
        provider, home, verdict = lane["provider"], _home(lane), lane_verdict(lane)
        email = lane.get("email") or str(lane.get("account_key", home)).removeprefix(f"{provider}:")
        if lane.get("enabled", True) or lane.get("duplicate_of") or lane.get("identity_status") == "non-canonical":
            groups[(provider, str(lane.get("account_key", lane["lane_id"])))].append(lane)
        if verdict in {"auth-dead", "auth-revoked", "revoked", "no-auth", "auth-suspect",
                       "token-invalid", "secret-missing", "no-token", "expired-token", "free-plan"}:
            if provider == "codex":
                kind = ("noauth" if verdict == "no-auth" else "suspect" if verdict in {"auth-suspect", "expired-token"}
                        else "free-plan" if verdict == "free-plan" else "revoked")
                key = f"codex-{kind}:{home}"
            else:
                key = f"claude-lane-auth:{email}"
            add(key, "critical" if verdict in {"auth-dead", "auth-revoked", "revoked"} else "warn",
                f"{provider} credentials: {home} is {verdict}",
                f"{home} ({email}): {verdict}. Run: {_login(lane)}", home=home)
        if provider == "codex" and lane.get("app_shadowed"):
            add(f"codex-app-shadow:{home}", "warn", f"codex: app shares {home}'s account",
                f"The app and {home} share {email}. Automatic reset credits prefer unshadowed lanes. "
                f"If the credential is revoked, run: {_login(lane)}", home=home, once=True, recover=False)
        if provider == "codex":
            weekly = [row for row in lane.get("readings", ()) if row.get("window") == "seven_day"
                      and row.get("scope") == "account" and row.get("label") == "provider"]
            for reading in weekly:
                utilization, reset_at = reading.get("utilization"), reading.get("resets_at")
                if (_number(utilization) and 0 <= utilization < 1 and reset_at
                        and timedelta(0) < instant(reset_at) - at <= timedelta(days=1)):
                    add(f"codex-capacity-expiring:{home}", "warn", f"codex: unused capacity resets in {home}",
                        f"{home} has provider-observed unused weekly capacity that resets at {reset_at}. "
                        "Queue Codex work before that reset; inspect with subfleet status.", home=home, daily=True)
        for closure in lane.get("closures", ()):
            if closure.get("reason") == "auth-dead":
                continue
            scope = str(closure.get("scope", "account"))
            until = closure.get("until_at") or "unknown"
            key = (f"claude-limit:{until}" if provider == "claude" and scope == "account"
                   else f"{provider}-scoped-limit:{email}:{scope}")
            add(key, "warn" if scope == "account" else "critical", f"{provider}: {home} {scope} limited",
                f"{home}: {scope} is closed ({closure.get('reason', 'provider-limit')}); resets {until}.", home=home)

    for (provider, account), members in groups.items():
        homes = sorted({_home(lane) for lane in members} | {str(lane["duplicate_of"]) for lane in members if lane.get("duplicate_of")})
        if len(homes) > 1:
            key = f"{provider}-dup:{account.removeprefix(provider + ':')}"
            add(key, "critical", f"{provider}: one account is bound to multiple homes",
                f"Account {account} is bound to: {', '.join(homes)}. Rebind the later home to a distinct account. "
                f"Run: {_login(members[-1])}", home=homes[-1], homes=homes)

    for provider in ("codex", "claude"):
        members = [lane for lane in lanes if lane["provider"] == provider]
        available = [lane for lane in members if dispatchable(lane)]
        if members and not available:
            key = "codex-fleet-empty" if provider == "codex" else "claude-lanes-empty"
            add(key, "critical" if provider == "codex" else "warn", f"{provider}: no dispatchable lanes",
                "All enrolled lanes are exhausted, unavailable, or unknown. Run: subfleet status",
                home=f"fleet:{provider}")
        elif provider == "codex" and len(available) == 1:
            add("codex-fleet-low", "warn", "codex: only one dispatchable lane",
                f"Only {_home(available[0])} has observed headroom. Run: subfleet status", home="fleet:codex")

    expiry = snapshot.get("capacity_expiry") or {}
    unused = expiry.get("projected_unused_windows_raw", expiry.get("projected_unused_windows"))
    if not _number(unused):
        unused = expiry.get("known_projected_unused_windows_raw", expiry.get("known_projected_unused_windows"))
    reset = expiry.get("earliest_reset_at")
    if _number(unused) and unused > 1 and reset and timedelta(0) < instant(reset) - at < timedelta(days=3):
        add("codex-capacity-expiring", "warn", "codex: capacity expiring unused",
            f"More than one observed weekly window of capacity is projected to expire unused by {reset}. "
            "Queue Codex work before the reset; inspect with subfleet status.", home="fleet:codex", daily=True)
    return list(conditions.values())


class Alerts:
    """Persist latches in events; delivery is the daemon's ping notice callback."""

    def __init__(self, store: Any, policy: Mapping[str, Any], deliver: Callable[[dict[str, Any]], Any]):
        self.store, self.policy, self.deliver = store, policy, deliver
        self.latches = self._load_latches()

    def _load_latches(self) -> dict[str, dict[str, Any]]:
        latches: dict[str, dict[str, Any]] = {}
        for event in self.store.query("SELECT data_json FROM events WHERE kind='alert-latch' ORDER BY event_id"):
            try:
                data = json.loads(event["data_json"])
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            key = data.get("key") or data.get("alert_key")
            if isinstance(key, str):
                state = data.get("latch") or data.get("state") or data
                if isinstance(state, dict):
                    latches[key] = dict(state)
            else:
                # Importers may preserve the entire v1 alerts.json map in one
                # event, or one row per v1 key. Both are append-only evidence.
                states = data.get("latches", data)
                if isinstance(states, dict):
                    for key, state in states.items():
                        if isinstance(state, dict) and "active" in state:
                            latches[key] = dict(state)
        return latches

    def _persist(self, key: str, state: dict[str, Any]) -> None:
        self.store.add_event("alert-latch", data={"key": key, **state})
        self.latches[key] = state

    @staticmethod
    def _homes(key: str, state: Mapping[str, Any]) -> set[str]:
        if state.get("homes"):
            return set(state["homes"])
        if state.get("home"):
            return {str(state["home"])}
        if key.startswith(("codex-fleet-", "codex-capacity-", "codex-resets-")):
            return {"fleet:codex"}
        if key == "claude-lanes-empty":
            return {"fleet:claude"}
        return {key.partition(":")[2] or key}

    def evaluate(self, snapshot: Mapping[str, Any], *, now: str | datetime | None = None,
                 offline: bool = False) -> dict[str, Any]:
        """C-18.1, C-23.27, C-23.52: transition/re-alert once, recover once per home."""
        at, sent, recovered = instant(now or snapshot.get("now")), [], []
        statuses = [lane.get("probe_status") or (lane.get("probe") or {}).get("status")
                    for lane in snapshot.get("lanes", ()) if lane.get("provider") == "codex"
                    and lane.get("probed", True)]
        statuses = [status for status in statuses if status is not None]
        inferred_offline = bool(statuses) and all(status == "network-error" for status in statuses)
        if offline or snapshot.get("offline", inferred_offline):
            self.store.add_event("monitoring.offline", data={"observed_at": timestamp(at)})
            return {"conditions": [], "alerts_sent": [], "recovered": [], "offline": True}

        conditions = evaluate_conditions(snapshot, now=at)
        current = {condition["key"]: condition for condition in conditions}
        active_homes = set().union(*(self._homes(key, value) for key, value in current.items())) if current else set()
        config = self.policy.get("alerts", {})
        interval = max(6.0, float(config.get("realert_hours", 6))) * 3600
        for key, condition in current.items():
            previous = self.latches.get(key, {})
            last_sent = previous.get("last_sent")
            elapsed = (at - instant(last_sent)).total_seconds() if last_sent else float("inf")
            daily = condition.get("daily") and config.get("expiring_capacity_daily", True)
            required = max(interval, 86400) if daily else interval
            due = elapsed >= required or not previous.get("active") and not daily
            if condition.get("once") and previous.get("active"):
                due = False
            state = {**previous, "active": True, "home": condition["home"],
                     "homes": sorted(self._homes(key, condition)), "recover": condition.get("recover", True)}
            if due:
                if self.deliver(dict(condition)) is False:
                    continue
                state["last_sent"] = timestamp(at)
                sent.append(key)
            if state != previous:
                self._persist(key, state)

        clear: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for key, state in self.latches.items():
            if not state.get("active") or key in current:
                continue
            homes = self._homes(key, state)
            # Imported Claude auth keys use emails. Map them to the current
            # lane home so a credential condition switch cannot look healed.
            if key.startswith("claude-lane-auth:"):
                email = key.partition(":")[2]
                homes = {_home(lane) for lane in snapshot.get("lanes", ())
                         if lane.get("email") == email or lane.get("account_key") == f"claude:{email}"} or homes
            group = sorted(homes)[0]
            eligible = state.get("recover", not key.startswith("codex-app-shadow:")) and not homes & active_homes
            if eligible:
                clear[group].append((key, state))
            else:
                self._persist(key, {**state, "active": False, "cleared_at": timestamp(at)})
        for home, entries in clear.items():
            notice = {"key": f"recovered:{home}", "severity": "info", "subject": f"recovered: {home}",
                      "body": f"All monitored conditions for {home} have cleared.", "home": home, "recovery": True}
            if self.deliver(notice) is False:
                continue
            recovered.append(home)
            for key, state in entries:
                self._persist(key, {**state, "active": False, "cleared_at": timestamp(at)})
        return {"conditions": list(current), "alerts_sent": sent, "recovered": recovered, "offline": False}
