"""Snapshot-based alert conditions and durable event latches (C-23.27, C-23.52).

An alert in force is shown by `subfleet status` and `status.json` (C-18.4),
whatever else delivers it: a notice for `alerts.operator_session` when one is
configured (C-15.8, C-18.1), and nothing more when none is.
"""

from __future__ import annotations

import json
import shlex
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from . import lane_identity
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


def _named(lane: Mapping[str, Any]) -> str:
    label = lane.get("label") or str(lane.get("account_key") or "").partition(":")[2]
    return f"{lane['lane_id']} ({label})" if label else str(lane["lane_id"])


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def identity_conditions(lanes: list[Mapping[str, Any]],
                        readings: Iterable[Mapping[str, Any]],
                        twins: lane_identity.TwinSettings | None = None) -> list[dict[str, Any]]:
    """C-10.8, C-10.9: the lanes whose credentials are one account, and the lanes
    whose readings match too well to be two (D-ID1, 2026-10-09)."""
    found: list[dict[str, Any]] = []
    for group in lane_identity.shared_groups(lanes):
        # Keyed by the identity of the lane that takes the work, not by
        # organization: two seats of one Team are two accounts, each its own
        # group and its own alert (review of #159, finding 14).
        head, account = group[0], str(group[0].get("identity") or "?")
        org = lane_identity.identity_org(account) or "?"
        what = (f"organization {org}" if not lane_identity.account_level(account)
                else f"account {lane_identity.split_identity(account)[0]} in organization {org}")
        others = group[1:]
        found.append({
            "key": f"claude-identity-shared:{account}", "severity": "critical",
            "subject": f"claude: {len(group)} lanes hold one account's token",
            "body": (f"{', '.join(_named(lane) for lane in group)} answer as one account "
                     f"({what}). Only {head['lane_id']} takes its work; "
                     f"{', '.join(str(lane['lane_id']) for lane in others)} "
                     f"{'is' if len(others) == 1 else 'are'} refused (identity-shared), so no work "
                     f"reaches the {'account its label names' if len(others) == 1 else 'accounts their labels name'} "
                     f"through {'it' if len(others) == 1 else 'them'}. For each: sign in to claude.ai as its label, run claude "
                     f"setup-token, store the token as its keychain item, then subfleet lanes "
                     f"enroll <item>."),
            "home": f"claude-identity:{account}", "homes": [f"claude-identity:{account}"]})
    rows = {str(lane["lane_id"]): lane for lane in lanes}
    for twin in lane_identity.reading_twins(lanes, readings, twins):
        first, second = (rows[lane_id] for lane_id in twin["lanes"])
        provider = twin["provider"]
        values = ", ".join(f"{row['window']}{'' if row['scope'] == 'account' else ' ' + row['scope']} "
                           f"at {_percent(row['utilization'])}" for row in twin["values"])
        hint = ("Subfleet says for certain once it has read each token's organization; "
                "subfleet lanes list shows it." if provider == "claude"
                else "Check which account each home is signed in to.")
        found.append({
            "key": f"{provider}-reading-twins:{twin['lanes'][0]}+{twin['lanes'][1]}", "severity": "warn",
            "subject": f"{provider}: {first['lane_id']} and {second['lane_id']} report the same usage",
            "body": (f"{_named(first)} and {_named(second)} reported the same resets and utilization "
                     f"within minutes ({values}). Two accounts rarely match like this; their "
                     f"credentials are probably one account's. {hint}"),
            "home": f"{provider}-twins:{'+'.join(twin['lanes'])}",
            "homes": [f"{provider}-twins:{'+'.join(twin['lanes'])}"]})
    return found


def evaluate_conditions(snapshot: Mapping[str, Any], *, now: str | datetime | None = None,
                        twins: lane_identity.TwinSettings | None = None) -> list[dict[str, Any]]:
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

    readings = [row for lane in lanes for row in lane.get("readings", ())]
    readings += list(snapshot.get("weekly_samples") or ())
    for condition in identity_conditions(lanes, readings, twins):
        add(**condition)

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

    for warning in (snapshot.get("claude_cards") or {}).get("warnings") or ():
        condition = card_condition(warning, at)
        if condition:
            add(**condition)

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


def _when(value: Any, now: datetime) -> str:
    if not isinstance(value, str) or not value:
        return "an unknown time"
    left = instant(value) - now
    days = left.total_seconds() / 86400
    return f"{value} (in {days:.1f} days)" if days >= 0 else value


def card_condition(warning: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
    """C-9.10: one card or credit warning as an alert condition.

    Every body says what is at risk and when, and that spending it is the
    operator's act: Subfleet reads cards and credits and never redeems one.
    """
    kind, login = warning.get("kind"), str(warning.get("login") or "?")
    lanes = ", ".join(warning.get("lanes") or ()) or "no lane"
    who = f"{login} ({lanes})"
    home = f"claude-cards:{login}"
    never = "Subfleet never redeems or claims; using it is your call."
    # A card kept through a read that listed none is as last listed, not as read now.
    listed = (f"This is the card as last listed {warning.get('listed_at') or 'before'}; the read at "
              f"{warning.get('unlisted_at') or 'a later time'} listed none ({warning.get('unlisted')}), so check "
              f"Settings, Usage. " if warning.get("unlisted") else "")
    if kind == "card-expiring":
        subject = f"claude: unused limit reset on {login} expires soon"
        body = (f"{who} holds {warning.get('resets_left')} unused limit reset(s) ({warning.get('grant')}) "
                f"that expire at {_when(warning.get('at'), now)}. Use it from Settings, Usage, Reset for free "
                f"(web or desktop) before then, or it is lost. {listed}{never}")
    elif kind == "card-lapse-risk":
        reasons = "; ".join(warning.get("reasons") or ()) or "plan lapsing"
        subject = f"claude: unused limit reset on {login} is lost if its plan lapses"
        body = (f"{who} holds {warning.get('resets_left')} unused limit reset(s) ({warning.get('grant')}); "
                f"{reasons}. A card is lost when the plan is cancelled or downgraded before it is used. "
                f"{listed}{never}")
    elif kind == "credit-expiring":
        subject = f"claude: {warning.get('label')} on {login} expires soon"
        body = (f"{who} has ${warning.get('remaining_dollars'):.2f} of {warning.get('label')} left, "
                f"expiring at {_when(warning.get('at'), now)}; what is unspent then is lost. {never}")
    elif kind == "credit-lapse-risk":
        reasons = "; ".join(warning.get("reasons") or ()) or "plan lapsing"
        subject = f"claude: {warning.get('label')} on {login} may go with its plan"
        body = (f"{who} has ${warning.get('remaining_dollars'):.2f} of {warning.get('label')} left; {reasons}. "
                f"When two of these accounts' plans ended (2026-09-30, 2026-10-04) their usage stopped showing "
                f"the credit. {never}")
    elif kind == "card-lost":
        why = "with its plan" if warning.get("reason") == "lapse" else "unused at its end"
        subject = f"claude: a limit reset on {login} was lost {why}"
        body = (f"{who} held {', '.join(warning.get('grants') or ())} unused at the last read; it was lost "
                f"{why} (seen {warning.get('at')}).")
    elif kind == "credit-lost":
        credits = ", ".join(f"{row.get('label')} (${(row.get('remaining_dollars') or 0):.2f})"
                            for row in warning.get("credits") or ())
        if warning.get("reason") == "lapse":
            subject = f"claude: {login}'s plan lapsed with promotional credit unspent"
            body = (f"{who} had {credits} left at the last read and its plan has lapsed (seen "
                    f"{warning.get('at')}). When two of these accounts' plans ended (2026-09-30, 2026-10-04) "
                    f"their usage stopped showing the credit.")
        else:
            subject = f"claude: a promotional credit on {login} ended unspent"
            body = f"{who} had {credits} left at the last read; a read after its end shows it unspent or gone (seen {warning.get('at')})."
    elif kind == "credit-claimable":
        subject = f"claude: {login} has an unclaimed promotional credit"
        body = (f"{who} is eligible for the cloud-session credit and has not claimed it. Claim it in the Claude "
                f"app, at claude.ai/code/claim-credit, or with /claim-credit in Claude Code. {never}")
    else:
        return None
    # A warning that stops because the card or credit was used or lost is not
    # a recovery: its latch clears without a "recovered" notice. A loss is told
    # once.
    lost = kind in ("card-lost", "credit-lost")
    return {"key": f"claude-{kind}:{warning.get('key')}", "severity": "warn", "subject": subject,
            "body": body, "home": home, "recover": False, **({"once": True} if lost else {"daily": True})}


def operator_session(policy: Mapping[str, Any]) -> str | None:
    """C-15.8: the session `alerts.operator_session` names, or None when it names none.

    There is no default: a notice for a session id nobody holds is never read
    (2026-10-03, 1,637 alerts parked for the literal `operator` since 9/19).
    """
    value = (policy.get("alerts") or {}).get("operator_session")
    return value.strip() if isinstance(value, str) and value.strip() else None


#: C-18.4: the order `status` lists alerts in; an unknown severity sorts as `warn`.
SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}

LATCH_QUERY = "SELECT data_json FROM events WHERE kind='alert-latch' ORDER BY event_id"


def load_latches(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each alert key's newest latch state, from `alert-latch` events in event order."""
    latches: dict[str, dict[str, Any]] = {}
    for event in rows:
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


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def active_alerts(latches: Mapping[str, Mapping[str, Any]],
                  current: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """C-18.4: every alert in force, as `status` and `status.json` show it.

    An alert is in force while its latch is active: it fired and has not been
    cleared, which an offline cycle never does (C-23.27). Its words are the
    latest cycle's (`current`) when that cycle saw the condition, else the ones
    it last fired with; a latch written before alerts kept their words shows
    its key. `since` is when the latch last became active, null when that was
    before latches recorded it.
    """
    rows = []
    for key, state in latches.items():
        if not state.get("active"):
            continue
        fresh = (current or {}).get(key) or {}
        rows.append({"key": key,
                     "severity": _text(fresh.get("severity")) or _text(state.get("severity")),
                     "subject": _text(fresh.get("subject")) or _text(state.get("subject")) or key,
                     "body": _text(fresh.get("body")) or _text(state.get("body")) or "",
                     "home": _text(fresh.get("home")) or _text(state.get("home")),
                     "since": _text(state.get("since")), "last_sent": _text(state.get("last_sent"))})
    rows.sort(key=lambda row: (SEVERITY_ORDER.get(row["severity"] or "warn", 1),
                               row["since"] or "", row["key"]))
    return rows


class Alerts:
    """Persist latches in events; delivery is the daemon's callback (C-15.8)."""

    def __init__(self, store: Any, policy: Mapping[str, Any], deliver: Callable[[dict[str, Any]], Any]):
        self.store, self.policy, self.deliver = store, policy, deliver
        # `active` is read from request threads while a cycle writes; every
        # change to the two maps below is made under this lock.
        self._lock = threading.Lock()
        self.latches = self._load_latches()
        self._current: dict[str, dict[str, Any]] = {}

    def _load_latches(self) -> dict[str, dict[str, Any]]:
        return load_latches(self.store.query(LATCH_QUERY))

    def _persist(self, key: str, state: dict[str, Any]) -> None:
        self.store.add_event("alert-latch", data={"key": key, **state})
        with self._lock:
            self.latches[key] = state

    def active(self) -> list[dict[str, Any]]:
        """C-18.4: the alerts in force now (`active_alerts`)."""
        with self._lock:
            latches = {key: dict(state) for key, state in self.latches.items() if state.get("active")}
            current = dict(self._current)
        return active_alerts(latches, current)

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

        conditions = evaluate_conditions(snapshot, now=at,
                                         twins=lane_identity.TwinSettings.from_policy(self.policy))
        current = {condition["key"]: condition for condition in conditions}
        with self._lock:
            self._current = current
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
            if not previous.get("active"):
                state["since"] = timestamp(at)          # C-18.4
            if due:
                if self.deliver(dict(condition)) is False:
                    continue
                state["last_sent"] = timestamp(at)
                # C-18.4: the words it fired with, for a `status` read before
                # the next cycle sees the condition (or from the store, offline).
                state.update(severity=condition["severity"], subject=condition["subject"],
                             body=condition["body"])
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
