"""Claude limit-reset cards and promotional credits (C-9.10): read, never redeemed.

Anthropic grants Claude subscribers banked limit resets (help article 17007452:
"Reset for free" under Settings, Usage, on the web or desktop; the first broad
grant came with Opus 5.5 on 2026-09-22, usable until 2026-10-22, and an unused
card is lost if the account cancels or downgrades first) and promotional credits
such as the $250 cloud-session credit (help article 17152539). This module reads
both per account and says what is about to be lost.

It never spends a card or a credit. Its own requests are GETs (`_get` refuses any
other method), and no code in Subfleet calls the claim endpoints Claude Code uses
(`POST /api/organizations/{org}/reset_rate_limits`, the promo claim). The one
thing it may start is a heal (`heal_turn`): an ordinary one-turn Claude Code run
under a login's folder, which posts one message and lets the CLI renew its own
login. Max's rule for Codex reset credits (one at a time, only for a job that is
waiting) applies with more force: a card is worth more held for a model launch,
so using one is always the operator's act, in the Claude app.

What the endpoints return (Claude Code 2.1.286's bundle, the claude.ai frontend
in the desktop app's HTTP cache, and live reads, 2026-10-05):

* `GET /api/oauth/usage?cedar_ember=1&skip_spend=1` (Claude Code's own read).
  Its `cedar_ember` block is `{eligible, ineligible_reason, at_limit, exhausted,
  grants, next_grant_id, weekly_resets_at, cooldown_until, event_props}`; each
  grant is `{id, label, resets_total, resets_left, starts_at, ends_at, clears,
  paused, usable_now, use_requires_limit, percent_used, blocking}`. Asked with
  User-Agent `claude-code/2.1.286` the server answered `eligible: false,
  ineligible_reason: "surface"` with no grants, and with Claude Code's own,
  `claude-cli/<version> (external, cli)`, it listed the grant (one comparison on
  one account, 2026-10-05), so the sensor sends Claude Code's.
  The same payload carries dollar blocks. Claude Code does not read them; the
  claude.ai frontend reads `iguana_necktie` as the claimed cloud-session credit
  (`limit_dollars`, `remaining_dollars`), takes its expiry from the claim
  status's `expires_at` and only failing that from the block's `resets_at`, and
  polls usage after a claim until the block appears (so it is, by inference,
  absent until the credit is claimed).
* `GET /v1/code/promo/cloud_credit` (Claude Code's `/claim-credit` status, with
  `anthropic-version` and `x-organization-uuid`): `{eligible, claimed,
  claimed_at, expires_at, state}`, state one of `not_claimed`, `pending`,
  `active`, `expired`, `claimed_elsewhere` once normalised as the CLI does.
* `GET /api/oauth/profile`: `organization.{organization_type, rate_limit_tier,
  subscription_status, billing_type}`. A lapsed account reads `canceled` and
  `claude_free`. None of the reads here showed a cancellation or downgrade
  scheduled for the end of a billing period; claude.ai's `subscription_details`,
  which does, refuses OAuth tokens. The operator declares one in
  `<state root>/claude-plan-ends.json`.

All of them need a full Claude Code login (scope `user:profile`). A lane's setup
token is inference-only and gets 403 (C-9.9's `no-scope`), so the sensor reads
with the per-account logins under `<state root>/logins/` instead. A login is
bound to the lanes whose recorded identity its profile returns (C-10.6) and to
the lanes that recorded none whose label is its folder name (`associate`). The
label is a display name (C-1.4), so the name binding never records an identity;
it does decide whether a heal may be spent and whether a hold covers the login,
which lanes are shown beside it, and which lane ids a declared plan end may be
filed under.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import CLAUDE_CARDS_DEFAULTS

#: The usage read Claude Code makes when it asks about reset cards.
CARDS_USAGE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
#: Claude Code's `/claim-credit` status for the 2026-09 cloud-session credit.
CLOUD_CREDIT_STATUS_URL = "https://api.anthropic.com/v1/code/promo/cloud_credit"
OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"
REQUEST_TIMEOUT_S = 15.0
MAX_BODY_BYTES = 1 << 20
#: Used only when the installed CLI cannot say its own version.
FALLBACK_CLI_VERSION = "2.1.286"
SNAPSHOT_FILE = "claude-cards.json"
PLAN_ENDS_FILE = "claude-plan-ends.json"
SNAPSHOT_VERSION = 1

#: Dollar blocks in the usage payload that are credits, by their code names.
CREDIT_LABELS = {"iguana_necktie": "cloud-session credit"}
#: Credits whose expiry the claim status (`/v1/code/promo/cloud_credit`) gives.
CLAIM_EXPIRY_CREDITS = frozenset({"iguana_necktie"})
#: Usage blocks that are windows or overage, never promotional credits.
NOT_CREDITS = frozenset({"five_hour", "seven_day", "extra_usage", "spend", "limits",
                         "seven_day_breakdown", "cedar_ember", "juniper_tide"})
#: `subscription_status` values that put nothing at risk. Every other value warns,
#: because an unobserved one may be a lapse in progress.
HEALTHY_SUBSCRIPTION = frozenset({"active"})
FREE_ORGANIZATIONS = frozenset({"claude_free"})

#: `AccountCards.status`. Exhaustive, so a renderer can name every one.
OK = "ok"                      # read; cards and credits are current
NO_LOGIN = "no-login"          # no full login for this account under the logins folder
LOGIN_EXPIRED = "login-expired"  # access token past expiresAt and no heal ran
LOGIN_DEAD = "login-dead"      # the CLI said it could not renew the login: sign in again
LAPSED = "lapsed"              # the profile says the plan is gone; no cards can be read
NO_SCOPE = "no-scope"          # 403 on usage from an account that has not lapsed
RATE_LIMITED = "rate-limited"  # 429; retried after the server's Retry-After
UNAVAILABLE = "unavailable"    # network, 5xx, an unreadable body, or a heal that could not run
HELD = "held"                  # an operator hold covers the account; no turn is spent on it
STATUSES = (OK, NO_LOGIN, LOGIN_EXPIRED, LOGIN_DEAD, LAPSED, NO_SCOPE, RATE_LIMITED,
            UNAVAILABLE, HELD)

_CLI_VERSION = re.compile(r"(\d+\.\d+\.\d+)")


def settings(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {**CLAUDE_CARDS_DEFAULTS, **(policy.get("claude_cards") or {})}


def iso_utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime | None:
    """An ISO timestamp with a zone, or None. A naive one is not trusted."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.astimezone(timezone.utc) if when.tzinfo else None


def _iso(value: Any) -> str | None:
    when = parse_time(value)
    return iso_utc(when) if when else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _names(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


# --- parsing (pure) -----------------------------------------------------------


def parse_grant(raw: Any) -> dict[str, Any] | None:
    """One `cedar_ember.grants[]` row, or None when it cannot name a card."""
    if not isinstance(raw, dict):
        return None
    grant_id, left = _text(raw.get("id")), _int(raw.get("resets_left"))
    if grant_id is None or left is None:
        return None
    total = _int(raw.get("resets_total"))
    return {
        "id": grant_id,
        "label": _text(raw.get("label")) or "",
        "resets_total": total if total is not None else left,
        "resets_left": left,
        "starts_at": _iso(raw.get("starts_at")),
        "ends_at": _iso(raw.get("ends_at")),
        "clears": _names(raw.get("clears")),
        "paused": raw.get("paused") is True,
        "usable_now": raw.get("usable_now") is True,
        "use_requires_limit": raw.get("use_requires_limit") is not False,
        "blocking": _names(raw.get("blocking")),
    }


def parse_cards(block: Any) -> dict[str, Any] | None:
    """The `cedar_ember` block, or None when the payload had none to give."""
    if not isinstance(block, dict) or not isinstance(block.get("eligible"), bool):
        return None
    grants = [grant for grant in map(parse_grant, block.get("grants") or []) if grant]
    return {
        "eligible": block["eligible"],
        "ineligible_reason": _text(block.get("ineligible_reason")),
        "at_limit": block.get("at_limit") is True,
        "exhausted": _names(block.get("exhausted")),
        "next_grant_id": _text(block.get("next_grant_id")),
        "weekly_resets_at": _iso(block.get("weekly_resets_at")),
        "cooldown_until": _iso(block.get("cooldown_until")),
        "grants": grants,
    }


def parse_credits(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every dollar-denominated block that is a credit, not a usage window.

    A block counts when it carries a positive `limit_dollars` and a
    `remaining_dollars`; the account windows carry neither on a subscription.
    Unknown code names are kept under their own name rather than dropped, so a
    new promotion shows up before anyone has named it.
    """
    credits = []
    for key in sorted(payload):
        block = payload[key]
        if key in NOT_CREDITS or key.startswith("seven_day") or not isinstance(block, dict):
            continue
        limit, remaining = _money(block.get("limit_dollars")), _money(block.get("remaining_dollars"))
        if not limit or remaining is None:
            continue
        credits.append({
            "key": key,
            "label": CREDIT_LABELS.get(key, key),
            "limit_dollars": limit,
            "used_dollars": _money(block.get("used_dollars")),
            "remaining_dollars": remaining,
            "expires_at": _iso(block.get("resets_at")),
            "locked_reason": _text(block.get("locked_reason")),
        })
    return credits


#: Claude Code's claim states (2.1.286, `Bse`); anything else reads as no state.
CLAIM_STATES = ("not_claimed", "pending", "active", "expired", "claimed_elsewhere")
_STATE_PREFIX = re.compile(r"^[a-z_]*_state_")


def claim_state(value: Any) -> str | None:
    """Normalised as Claude Code does (`jse`): lower-case, strip `*_state_`, known only."""
    if not isinstance(value, str):
        return None
    state = _STATE_PREFIX.sub("", value.lower())
    return state if state in CLAIM_STATES else None


def parse_claim(payload: Any) -> dict[str, Any] | None:
    """`/v1/code/promo/cloud_credit`, read as Claude Code's `/claim-credit` reads it."""
    if not isinstance(payload, dict) or not ("eligible" in payload or "claimed" in payload):
        return None
    return {
        "eligible": payload.get("eligible") is True,
        "claimed": payload.get("claimed") is True,
        "state": claim_state(payload.get("state")),
        "claimed_at": _iso(payload.get("claimed_at")),
        "expires_at": _iso(payload.get("expires_at")),
    }


CLAIM_STALE_S = 2 * 86400


def claimable(claim: Mapping[str, Any] | None, now: datetime) -> bool:
    """Claude Code's test (eligible, not claimed), on a claim status that has not
    expired and was read within the last two days."""
    if not claim or not claim.get("eligible") or claim.get("claimed") or claim.get("state") == "expired":
        return False
    ends, checked = parse_time(claim.get("expires_at")), parse_time(claim.get("checked_at"))
    if ends is not None and ends <= now:
        return False
    return checked is None or recent(now, checked, CLAIM_STALE_S)


def parse_plan(payload: Any) -> dict[str, Any] | None:
    """The profile's identity and plan; None unless it names an account and an org."""
    account = payload.get("account") if isinstance(payload, dict) else None
    organization = payload.get("organization") if isinstance(payload, dict) else None
    if not isinstance(account, dict) or not isinstance(organization, dict):
        return None
    account_uuid, org_uuid = _text(account.get("uuid")), _text(organization.get("uuid"))
    if not account_uuid or not org_uuid:
        return None
    return {
        "identity": f"{account_uuid}:{org_uuid}",
        "org_uuid": org_uuid,
        "email": _text(account.get("email")),
        "organization_type": _text(organization.get("organization_type")),
        "rate_limit_tier": _text(organization.get("rate_limit_tier")),
        "subscription_status": _text(organization.get("subscription_status")),
        "billing_type": _text(organization.get("billing_type")),
    }


def lapsed(plan: Mapping[str, Any] | None) -> bool:
    """The plan is gone: a free organization or a canceled subscription, either
    one on its own (C-9.10: "`claude_free` or `canceled` is `lapsed`").

    `canceled` alone counts. Help article 17007452 says a reset is "no longer
    available" once the account cancels or downgrades. The one pending
    cancellation observed (org 7c4b7006, 2026-10-04) read `status: active`, with
    `plan_ending_at` set, in claude.ai's `subscription_details`, so a cancellation
    that is only scheduled is not taken to read `canceled`. Any other status but
    `active` is lapsing, not lapsed: its cards are still read, and warned on.
    """
    if not plan:
        return False
    return (plan.get("organization_type") in FREE_ORGANIZATIONS
            or plan.get("subscription_status") == "canceled")


# --- what is about to be lost (pure) ------------------------------------------


def unused_cards(account: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Cards with a reset left whose window has not closed."""
    cards = account.get("cards") or {}
    out = []
    for grant in cards.get("grants") or []:
        ends = parse_time(grant.get("ends_at"))
        if grant.get("resets_left", 0) > 0 and (ends is None or ends > now):
            out.append(grant)
    return out


def load_plan_ends(path: Path) -> dict[str, datetime]:
    """`{"<login label or lane id>": "<ISO date or timestamp>"}`, read leniently.

    A bare date means the end of that day in UTC: a plan cancelled "until Oct 9"
    still serves on Oct 9. Entries that do not parse are ignored, not fatal: this
    file is hand-edited and a typo must not stop the sensor.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, datetime] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        when = parse_time(value)
        if when is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            when = datetime.fromisoformat(value.strip()).replace(tzinfo=timezone.utc) + timedelta(days=1)
        if when is not None:
            out[key.strip()] = when
    return out


def plan_end_for(account: Mapping[str, Any], plan_ends: Mapping[str, datetime]) -> datetime | None:
    """The declared end for an account, by its login label or any of its lanes."""
    keys = [account.get("login"), *(account.get("lanes") or [])]
    found = [plan_ends[key] for key in keys if isinstance(key, str) and key in plan_ends]
    return min(found) if found else None


def warnings(accounts: Iterable[Mapping[str, Any]], now: datetime, *, warn_days: float,
             plan_ends: Mapping[str, datetime] | None = None) -> list[dict[str, Any]]:
    """Every card or credit about to be lost, one entry per thing at risk.

    Kinds, each keyed so a repeat is recognisable:
    * `card-expiring`: an unused card ends within `warn_days`. A card kept
      through a read that listed none carries `listed_at` and `unlisted`, as a
      `card-lapse-risk` does.
    * `card-lapse-risk`: an unused card on an account whose plan is lapsing: a
      subscription status other than `active`, or a declared plan end within
      `warn_days` and before the card's own end. (A lapsed plan's usage cannot be
      read, so its cards are recorded as lost instead; see `lost_since`.)
    * `credit-expiring`: a credit with money left expires within `warn_days`.
    * `credit-lapse-risk`: a credit with money left on a plan that is lapsing, as
      for a card. Help article 17152539 says a downgrade keeps the cloud credit,
      but when two of these accounts' plans ended (2026-09-30, 2026-10-04) their
      usage stopped showing it.
    * `credit-claimable`: a promotional credit the account may claim and has not
      (Claude Code's own test: eligible and not claimed), unless it has expired.
    * `card-lost`, `credit-lost`: what the last good read held and a later read
      found gone with the plan or past its end, for `warn_days` after it was seen.
    Only an `ok` read says anything about cards; a failed read neither raises nor
    clears a warning beyond what its last good snapshot said.
    """
    horizon = now + timedelta(days=warn_days)
    plan_ends = plan_ends or {}
    out: list[dict[str, Any]] = []
    for account in accounts:
        label = account.get("login") or account.get("identity") or "?"
        lanes = list(account.get("lanes") or [])
        base = {"login": label, "lanes": lanes}
        plan = account.get("plan") or {}
        declared = plan_end_for(account, plan_ends)
        status = plan.get("subscription_status")
        # Cards kept through a read that listed none say when they were last
        # listed and why the latest read listed none, so an alert can say so.
        unlisted = account.get("cards_unlisted") or {}
        listing = {"listed_at": unlisted.get("listed_at"),
                   "unlisted": "no cards block" if unlisted.get("missing")
                   else f"ineligible: {unlisted.get('ineligible_reason') or 'unknown'}"} if unlisted else {}

        def lapse_reasons(ends: datetime | None) -> list[str]:
            reasons = []
            if lapsed(plan):
                reasons.append("plan lapsed")
            elif status and status not in HEALTHY_SUBSCRIPTION:
                reasons.append(f"subscription {status}")
            if declared is not None and declared <= horizon and (ends is None or declared < ends):
                reasons.append(f"plan ends {iso_utc(declared)}")
            return reasons

        for grant in unused_cards(account, now):
            ends = parse_time(grant.get("ends_at"))
            if ends is not None and ends <= horizon:
                out.append({**base, "kind": "card-expiring", "key": f"{label}:{grant['id']}",
                            "grant": grant["id"], "at": iso_utc(ends), "resets_left": grant["resets_left"],
                            **listing})
            reasons = lapse_reasons(ends)
            if reasons:
                out.append({**base, "kind": "card-lapse-risk", "key": f"{label}:{grant['id']}:lapse",
                            "grant": grant["id"], "at": iso_utc(declared) if declared else None,
                            "resets_left": grant["resets_left"], "reasons": reasons, **listing})
        for credit in account.get("credits") or []:
            ends = parse_time(credit.get("expires_at"))
            remaining = credit.get("remaining_dollars") or 0
            if remaining <= 0 or (ends is not None and ends <= now):
                continue
            if ends is not None and ends <= horizon:
                out.append({**base, "kind": "credit-expiring", "key": f"{label}:{credit['key']}",
                            "credit": credit["key"], "label": credit["label"], "at": iso_utc(ends),
                            "remaining_dollars": remaining})
            reasons = lapse_reasons(ends)
            if reasons:
                out.append({**base, "kind": "credit-lapse-risk", "key": f"{label}:{credit['key']}:lapse",
                            "credit": credit["key"], "label": credit["label"],
                            "at": iso_utc(declared) if declared else None,
                            "remaining_dollars": remaining, "reasons": reasons})
        claim = account.get("cloud_credit_claim") or {}
        if claimable(claim, now):
            out.append({**base, "kind": "credit-claimable", "key": f"{label}:cloud_credit:claim",
                        "credit": "cloud_credit", "at": claim.get("expires_at")})
        for item in lost_items(account):
            seen = parse_time(item.get("at"))
            if seen is None or not timedelta(0) <= now - seen <= timedelta(days=warn_days):
                continue
            if item.get("grant"):
                out.append({**base, "kind": "card-lost", "key": f"{label}:lost:{item['grant']}",
                            "grants": [item["grant"]], "at": item["at"], "reason": item.get("reason")})
            else:
                out.append({**base, "kind": "credit-lost", "key": f"{label}:credit-lost:{item['credit']}",
                            "credits": [{"key": item["credit"], "label": item.get("label"),
                                         "remaining_dollars": item.get("remaining_dollars")}],
                            "at": item["at"], "reason": item.get("reason")})
    return out


def lost_items(account: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The loss record: one entry per card (`grant`) or credit (`credit`), each
    with when it was seen lost (`at`) and why (`reason`)."""
    lost = account.get("lost")
    return [item for item in lost if isinstance(item, dict)] if isinstance(lost, list) else []


def lost_since(previous: Mapping[str, Any] | None, current: Mapping[str, Any],
               now: datetime) -> list[dict[str, Any]]:
    """What the last good read held that a read now shows gone without being used.

    `lapse`: the profile read now says the plan lapsed. Every card the last good
    read held unused, and every credit with money left, is recorded: the lapsed
    plan's usage cannot be read, and when two of these accounts' plans ended
    (2026-09-30, 2026-10-04) their usage stopped showing the credit. `expired`: an
    `ok` read now, after a card's or credit's end, still shows the card unused or
    no longer lists it, or shows the credit with money left or no longer lists it.
    A read that is not `ok` says nothing about use, so it records no expiry. A
    thing already in the loss record is never recorded again.
    """
    if not previous:
        return []
    then = parse_time(previous.get("read_at"))
    if then is None:
        return []
    if previous.get("identity") and current.get("identity") and previous["identity"] != current["identity"]:
        return []           # another account now: nothing of the old one is known lost
    known = {item.get("grant") or item.get("credit") for item in lost_items(previous)}
    # Cards kept through `ok` reads that listed none were last listed at `listed_at`, and are judged
    # from that listing, as they are when the reads between failed: one that ended meanwhile is
    # still recorded by the next read that lists cards.
    listed = parse_time((previous.get("cards_unlisted") or {}).get("listed_at")) or then
    held = [grant for grant in unused_cards(previous, listed) if grant["id"] not in known]
    money = [credit for credit in previous.get("credits") or []
             if (credit.get("remaining_dollars") or 0) > 0 and credit.get("key") not in known
             and (parse_time(credit.get("expires_at")) is None or parse_time(credit.get("expires_at")) > then)]
    at = iso_utc(now)
    if lapsed(current.get("plan")) and not lapsed(previous.get("plan")):
        reason = "lapse"
    elif current.get("status") == OK:
        reason = "expired"
        cards_now = current.get("cards") or {}
        grants_now = {grant["id"]: grant for grant in cards_now.get("grants") or []}
        credits_now = {credit["key"]: credit for credit in current.get("credits") or []}
        # A cards block that is missing or ineligible lists nothing; its silence
        # says nothing about a card, so no card expiry is read from it. The cards
        # such a read keeps are the last listing read's, not this read's.
        listing = bool(cards_now) and cards_now.get("eligible") is True and not current.get("cards_unlisted")
        # Each thing is judged by its end as the read now gives it, when it is
        # still listed (an end can move), else by the end last seen.
        held = [grant for grant in held if listing
                and (parse_time(grants_now.get(grant["id"], grant).get("ends_at")) or now) < now
                and grants_now.get(grant["id"], grant).get("resets_left", 0) > 0]
        money = [credit for credit in money
                 if (ends := parse_time(credits_now.get(credit["key"], credit).get("expires_at"))) is not None
                 and ends <= now
                 and (credits_now.get(credit["key"], credit).get("remaining_dollars") or 0) > 0]
    else:
        return []
    return ([{"grant": grant["id"], "at": at, "reason": reason} for grant in held]
            + [{"credit": credit["key"], "label": credit.get("label"), "at": at, "reason": reason,
                "remaining_dollars": credit.get("remaining_dollars")} for credit in money])


# --- the sensor (I/O) ---------------------------------------------------------


def _urlopen(request: urllib.request.Request, timeout: float) -> tuple[int, bytes, str | None]:
    """Through the Claude adapter's one network seam, which tests replace whole.

    A 429 arrives as `HTTPError`, whose headers carry the Retry-After; a 200
    needs none.
    """
    from .adapters import claude as adapter
    status, body = adapter._urlopen(request, timeout)
    return status, body, None


#: What a heal must not inherit: any credential that would answer instead of the
#: login under `CLAUDE_CONFIG_DIR`, and the markers of a session it is not part of.
HEAL_ENV_REMOVE = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                   "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                   "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "SUBFLEET_ATTEMPT", "SUBFLEET_JOB",
                   "SUBFLEET_ROOT")
HEAL_TIMEOUT_S = 120


def _kill_group(pid: int, sig: int = signal.SIGKILL) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def heal_turn(home: Path, *, claude_bin: str = "claude", model: str, prompt: str,
              timeout: float = HEAL_TIMEOUT_S, cancel: threading.Event | None = None,
              popen: Callable[..., Any] = subprocess.Popen) -> tuple[int, str, str]:
    """C-23.47: one minimal turn under a login's folder, so the CLI renews its own login.

    The CLI runs in a process group of its own, in a scratch directory, with no
    MCP server (`--strict-mcp-config` and an empty config, so a login folder's
    own servers never start), one turn, no tools needed, and an environment
    holding no other credential. No heal starts once `cancel` is set. The group
    is stopped when the turn outlives `timeout` or `cancel` is set (a daemon stop
    must not wait for a heal): SIGTERM, then SIGKILL two seconds later. However
    the heal ends, a normal exit and an error while its output is read included,
    the group is killed, so nothing it started outlives it. Its output is read
    as UTF-8, a byte that is not UTF-8 replaced rather than raised. Waiting for its
    output after a kill is bounded. Returns (rc, stdout, stderr): 124 is a
    timeout, 127 a CLI that could not start, 130 a cancelled heal. Nothing here
    reads or writes the credential.
    """
    env = {key: value for key, value in os.environ.items() if key not in HEAL_ENV_REMOVE}
    env["CLAUDE_CONFIG_DIR"] = str(home)
    argv = [claude_bin, "-p", prompt, "--model", model, "--max-turns", "1",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    if cancel is not None and cancel.is_set():
        return 130, "", "heal cancelled: the daemon is stopping"
    with tempfile.TemporaryDirectory(prefix="subfleet-card-heal-") as workdir:
        try:
            child = popen(argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          start_new_session=True)
        except OSError as error:
            return 127, "", f"could not run {claude_bin}: {type(error).__name__}"
        try:
            deadline = time.monotonic() + timeout
            rc: int | None = None
            while True:
                try:
                    out, err = child.communicate(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        rc, why = 130, "heal cancelled: the daemon is stopping"
                    elif time.monotonic() >= deadline:
                        rc, why = 124, f"heal timed out after {timeout:g}s"
                    else:
                        continue
                    # SIGTERM first, so a CLI part way through renewing its login can
                    # finish writing it; what is still running two seconds later is killed.
                    _kill_group(child.pid, signal.SIGTERM)
                    try:
                        out, _err = child.communicate(timeout=2)
                        return rc, out or "", why
                    except subprocess.TimeoutExpired:
                        _kill_group(child.pid)
                    try:
                        out, _err = child.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        # A descendant that left the group still holds the pipes;
                        # its output is not worth waiting for.
                        child.kill()
                        out = ""
                    return rc, out or "", why
            return int(child.returncode or 0), out or "", err or ""
        finally:
            _kill_group(child.pid)      # whatever the turn left running, however it ended


class ReadOnlyViolation(RuntimeError):
    """Raised before any request that is not a GET leaves this module."""


def cli_version(claude_bin: str, runner: Callable[..., Any] = subprocess.run) -> str:
    """The installed Claude Code's version, for the User-Agent the server expects."""
    try:
        done = runner([claude_bin, "--version"], capture_output=True, text=True, timeout=15,
                      stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return FALLBACK_CLI_VERSION
    match = _CLI_VERSION.search(getattr(done, "stdout", "") or "")
    return match.group(1) if match else FALLBACK_CLI_VERSION


#: Heals that never reached the login: a timeout, a CLI that could not start, a stop.
TRANSIENT_HEAL_RC = frozenset({124, 127, 130})
HEAL_FAILURES = {124: "timed out", 127: "the claude CLI could not start or raised", 130: "cancelled by a stop"}
#: What Claude Code prints when it cannot renew a login (observed 2026-10-05:
#: "Failed to authenticate: OAuth session expired and could not be refreshed").
DEAD_LOGIN = re.compile(r"could not be refreshed|invalid_grant|refresh token (?:was |is )?(?:revoked|expired|invalid)", re.I)


def recent(now: datetime, then: datetime, window_s: float) -> bool:
    """Within `window_s` of `then`. A clock that has stepped back before `then`
    counts as recent: a guard that withholds a turn must not open because of it."""
    return (now - then).total_seconds() < window_s


class Sensor:
    """Reads one account's cards, credits and plan with its own full login.

    Injected: `opener(request, timeout) -> (status, body, retry_after)`, the
    login reader (`home -> claudeAiOauth block`), the heal (`home -> (rc,
    stdout, stderr)`, one minimal turn so the CLI renews its own login, C-23.47),
    and the clock. The sensor never refreshes a token itself and never writes
    the provider's credential store.
    """

    def __init__(self, *, login_reader: Callable[[Path], Mapping[str, Any] | None],
                 heal: Callable[[Path], tuple[int, str, str]] | None,
                 version: Callable[[], str],
                 opener: Callable[[urllib.request.Request, float], tuple[int, bytes, str | None]] | None = None,
                 now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._login_reader = login_reader
        self._heal = heal
        self._version = version
        self._opener = opener or _urlopen
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._agent: str | None = None

    def _user_agent(self) -> str:
        if self._agent is None:
            self._agent = f"claude-cli/{self._version()} (external, cli)"
        return self._agent

    def _get(self, url: str, token: str, extra: Mapping[str, str] | None = None
             ) -> tuple[int | None, Any, int | None]:
        """One GET. Returns (status, parsed JSON or None, Retry-After seconds)."""
        request = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent(),
            **(extra or {}),
        })
        if request.get_method() != "GET":
            raise ReadOnlyViolation(request.get_method())
        retry: str | None = None
        try:
            status, body, retry = self._opener(request, REQUEST_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            status, body = int(error.code), b""
            retry = error.headers.get("Retry-After") if error.headers else None
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            # Only the type would be safe to keep: an exception's text can quote
            # the request headers, and those hold the bearer (C-10.5).
            return None, None, None
        retry_after = int(retry) if isinstance(retry, str) and retry.strip().isdigit() else None
        try:
            payload = json.loads(body.decode("utf-8", "replace")) if body else None
        except ValueError:
            payload = None
        return status, payload, retry_after

    def login_state(self, home: Path) -> tuple[str | None, bool, float | None]:
        """(access token, expired?, expiresAt in ms) from the CLI's own store."""
        oauth = self._login_reader(home) or {}
        token = oauth.get("accessToken") if isinstance(oauth.get("accessToken"), str) else None
        expires = oauth.get("expiresAt")
        expires = float(expires) if isinstance(expires, (int, float)) and not isinstance(expires, bool) else None
        expired = expires is not None and expires <= self.now().timestamp() * 1000 + 60_000
        return token, expired, expires

    def read(self, home: Path, *, allow_heal: bool, previous: Mapping[str, Any] | None = None,
             heal_after_s: float = CLAUDE_CARDS_DEFAULTS["heal_interval_min"] * 60,
             pace: Callable[[], None] | None = None,
             stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        """One account's snapshot. A failed read keeps the last good cards and
        credits beside a status that says why they are not current. An `ok` read
        whose cards block is missing or ineligible keeps the cards the last
        listing read saw, beside `cards_unlisted` (when, why, and when they were
        last listed). A folder that answers as another account keeps nothing.

        An expired login is healed (one minimal turn under its folder, after
        which the CLI has renewed its own login) only when `allow_heal`, and at
        most once per `heal_after_s`. A login the CLI said it could not renew is
        `login-dead`, and no turn is spent on it until someone signs in again,
        which changes its `expiresAt`. A heal that could not run (a timeout, a
        missing CLI, a stop, or a failure that names no login problem) is
        `unavailable` and is tried again after `heal_after_s`.
        """
        now = self.now()
        previous = dict(previous or {})
        account: dict[str, Any] = {
            "login": home.name, "home": str(home), "identity": previous.get("identity"),
            "email": previous.get("email"), "observed_at": iso_utc(now), "detail": None,
            "plan": previous.get("plan"), "cards": previous.get("cards"),
            "cards_unlisted": previous.get("cards_unlisted"),
            "credits": previous.get("credits") or [], "cloud_credit_claim": previous.get("cloud_credit_claim"),
            "read_at": previous.get("read_at"), "heal": previous.get("heal"), "healed": False,
            "retry_after_until": None,
        }

        def done(status: str, detail: str | None = None) -> dict[str, Any]:
            account["status"], account["detail"] = status, detail
            return account

        retry_until = parse_time(previous.get("retry_after_until"))
        if retry_until is not None and retry_until > now:
            account["retry_after_until"] = iso_utc(retry_until)
            return done(RATE_LIMITED, "waiting out Retry-After")
        token, expired, expires = self.login_state(home)
        account["login_expires_ms"] = expires
        if token is None:
            return done(NO_LOGIN, "no Claude Code login in this folder")
        if expired:
            if not allow_heal or self._heal is None:
                return done(LOGIN_EXPIRED, "access token expired; no turn may be spent on this account")
            last = previous.get("heal") or {}
            last_at = parse_time(last.get("at"))
            if last.get("refreshed") is False and not last.get("transient") and last.get("expires_ms") == expires:
                # The CLI said this very login cannot be renewed; only a sign-in
                # (which changes its expiresAt) makes another turn worth spending.
                return done(LOGIN_DEAD, f"the CLI could not renew this login (heal at {last.get('at')})")
            if last_at is not None and recent(now, last_at, heal_after_s):
                next_at = iso_utc(last_at + timedelta(seconds=heal_after_s))
                if last.get("refreshed") is False:
                    return done(UNAVAILABLE, f"the last heal could not run ({last.get('why')}); next heal after {next_at}")
                return done(LOGIN_EXPIRED, f"access token expired again since the heal at {last.get('at')}; "
                                           f"next heal after {next_at}")
            try:
                rc, out, err = self._heal(home)
            except Exception as error:              # noqa: BLE001 - a heal that raised never reached the login
                rc, out, err = 127, "", type(error).__name__
            token, expired, renewed = self.login_state(home)
            refreshed = bool(token) and not expired
            account["login_expires_ms"] = renewed
            transient = not refreshed and (rc in TRANSIENT_HEAL_RC or not DEAD_LOGIN.search(f"{out}\n{err}"))
            why = None if refreshed else HEAL_FAILURES.get(rc) or ("the CLI could not renew it" if not transient
                                                                     else f"rc {rc}")
            # Recorded against the login as it now stands, so the next cycle
            # recognises the same dead login and does not spend another turn.
            account["heal"] = {"at": iso_utc(now), "rc": rc, "refreshed": refreshed,
                               "transient": transient, "why": why, "expires_ms": renewed}
            account["healed"] = True
            if not refreshed:
                if transient:
                    return done(UNAVAILABLE, f"the heal could not renew the login ({why}); it is tried again")
                return done(LOGIN_DEAD, "the CLI could not renew this login")
        try:
            return self._read_with(token, account, now, done, pace, stop)
        except Exception as error:                  # noqa: BLE001 - the heal record above must survive
            return done(UNAVAILABLE, f"read failed: {type(error).__name__}")

    def _read_with(self, token: str, account: dict[str, Any], now: datetime,
                   done: Callable[[str, str | None], dict[str, Any]],
                   pace: Callable[[], None] | None,
                   stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        if stop():
            return done(UNAVAILABLE, "stopped before the read")
        status, profile, retry = self._get(PROFILE_URL, token)
        if status == 429:
            return self._rate_limited(account, now, retry)
        plan = parse_plan(profile) if status == 200 else None
        if plan is None:
            return done(UNAVAILABLE if status != 403 else NO_SCOPE, f"profile HTTP {status}")
        if account.get("identity") and account["identity"] != plan["identity"]:
            # The folder now holds another account, which starts afresh: nothing
            # kept from the old one stands in for a read of this one that fails
            # below. That includes the old claim status, which would otherwise date
            # this account's cloud credit.
            account.update(cards=None, cards_unlisted=None, credits=[], cloud_credit_claim=None, read_at=None)
        account.update(identity=plan["identity"], email=plan["email"],
                       plan={key: plan[key] for key in ("organization_type", "rate_limit_tier",
                                                        "subscription_status", "billing_type")})
        if lapsed(plan):
            account.update(cards=None, cards_unlisted=None, credits=[], cloud_credit_claim=None,
                           read_at=iso_utc(now))
            return done(LAPSED, f"{plan['organization_type']}, subscription {plan['subscription_status']}")
        if pace is not None:
            pace()          # C-9.9: the usage endpoint penalises bursts
        if stop():
            return done(UNAVAILABLE, "stopped during the read")
        status, usage, retry = self._get(CARDS_USAGE_URL, token)
        if status == 429:
            return self._rate_limited(account, now, retry)
        if status == 403:
            return done(NO_SCOPE, "usage HTTP 403")
        if status != 200 or not isinstance(usage, dict):
            return done(UNAVAILABLE, f"usage HTTP {status}")
        cards, kept = parse_cards(usage.get("cedar_ember")), account.get("cards") or {}
        if (cards is None or not cards["eligible"]) and kept.get("eligible") is True:
            # A block that is missing or ineligible lists nothing, and its silence
            # says nothing about a card (`surface` was one User-Agent away from
            # listing one). The last block that listed cards stays, and this read
            # is recorded beside it.
            account["cards_unlisted"] = {
                "at": iso_utc(now), "ineligible_reason": cards["ineligible_reason"] if cards else None,
                "missing": cards is None,
                "listed_at": (account.get("cards_unlisted") or {}).get("listed_at") or account.get("read_at")}
        else:
            account["cards"], account["cards_unlisted"] = cards, None
        account["credits"] = parse_credits(usage)
        status, claim, _retry = (None, None, None) if stop() else self._get(CLOUD_CREDIT_STATUS_URL, token, {
            "anthropic-version": ANTHROPIC_VERSION, "x-organization-uuid": plan["org_uuid"]})
        parsed = parse_claim(claim) if status == 200 else None
        if parsed is not None:
            account["cloud_credit_claim"] = {**parsed, "checked_at": iso_utc(now)}
        claim_ends = (account.get("cloud_credit_claim") or {}).get("expires_at")
        for credit in account["credits"]:
            # The claude.ai frontend takes the cloud credit's expiry from the
            # claim status first and the usage block's `resets_at` only after.
            if credit["key"] in CLAIM_EXPIRY_CREDITS and claim_ends:
                credit["expires_at"], credit["expires_from"] = claim_ends, "claim"
        # A claim status that could not be read keeps the last one read, with its
        # `checked_at`; past two days it no longer raises `credit-claimable`.
        account["read_at"] = iso_utc(now)
        return done(OK)

    def _rate_limited(self, account: dict[str, Any], now: datetime, retry: int | None) -> dict[str, Any]:
        wait = max(retry or 0, 3600)
        account["retry_after_until"] = iso_utc(now + timedelta(seconds=wait))
        account["status"], account["detail"] = RATE_LIMITED, f"HTTP 429; Retry-After {retry}"
        return account


#: Read again at most once a day unless the login itself changes: only a sign-in
#: or a new subscription moves any of these.
SETTLED = frozenset({LAPSED, LOGIN_DEAD})
SETTLED_RETRY_S = 86400


def lane_label(lane: Mapping[str, Any]) -> str | None:
    """A lane's display name, the folder name a login for it would carry (C-1.4)."""
    label = lane.get("label") or str(lane.get("account_key") or "").removeprefix("claude:")
    return label or None


def associate(lanes: Iterable[Mapping[str, Any]], identity: str | None, login: str
              ) -> tuple[list[Mapping[str, Any]], str | None]:
    """The lanes a login backs, and how that is known.

    By identity (C-10.6), every lane that recorded the one this login's profile
    returned; and by name, every lane that recorded no identity: a
    setup-token lane cannot ask the profile endpoint, so on such a fleet the
    folder name, which is the lane's display label, is all that connects them.
    Before the login's own identity is known (its token expired before any
    read), the name is matched against every lane, so that a login backing an
    identity-bearing lane can be healed once and then bound by identity.
    """
    lanes = list(lanes)
    bound = [lane for lane in lanes if identity and lane.get("identity") == identity]
    named = [lane for lane in lanes if lane not in bound and (identity is None or not lane.get("identity"))
             and lane_label(lane) == login]
    how = "identity" if bound and not named else "identity+label" if bound else "label" if named else None
    return bound + named, how


def refresh(sensor: Sensor, *, logins: Iterable[Path],
            lanes: Iterable[Mapping[str, Any]] | Callable[[], Iterable[Mapping[str, Any]]],
            previous: Mapping[str, Any] | None, heal: bool, heal_after_s: float,
            pace: Callable[[], None] | None = None,
            stop: Callable[[], bool] = lambda: False,
            checkpoint: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """One pass over every login: the next snapshot.

    `lanes` are the Claude lanes, each with `lane_id`, `identity`, `label`,
    `account_key` and `held` (an operator hold in force), or a callable that
    returns them, asked again for every login so a hold placed during a pass
    covers the logins read after it; `associate` says which
    a login backs. A turn is spent healing a login only when it backs at least
    one lane (enabled or not: a disabled lane's account may still be paid for and
    hold a card) and no lane it backs is held (a hold means spend nothing there).
    A pass that is stopped, or a login whose read raises, keeps that login's
    last snapshot. `checkpoint` receives the snapshot as it stands after each
    login read, every other login at its last state, so a heal is on disk before
    the next login is read and a pass that dies cannot spend it again.
    """
    lanes_now = lanes if callable(lanes) else (lambda fixed=[dict(lane) for lane in lanes]: fixed)
    before = {row.get("login"): row for row in (previous or {}).get("accounts") or [] if isinstance(row, dict)}
    accounts = []
    homes = list(logins)
    for index, home in enumerate(homes):
        prior = before.get(home.name)
        if stop():
            if prior:
                accounts.append({**prior, "healed": False})
            continue
        try:
            current_lanes = [dict(lane) for lane in lanes_now()]
            account = _refresh_one(sensor, home, prior, current_lanes, heal=heal, heal_after_s=heal_after_s,
                                   pace=pace, stop=stop)
        except Exception as error:                  # noqa: BLE001 - one login never stops the pass
            account = {**(prior or {"login": home.name, "home": str(home), "lanes": []}),
                       "status": UNAVAILABLE, "detail": f"read failed: {type(error).__name__}",
                       "observed_at": iso_utc(sensor.now()), "healed": False}
        accounts.append(account)
        if checkpoint is not None and account.get("healed"):
            rest = [{**before[later.name], "healed": False} for later in homes[index + 1:] if later.name in before]
            checkpoint({"version": SNAPSHOT_VERSION, "read_at": (previous or {}).get("read_at"),
                        "accounts": accounts + rest})
    return {"version": SNAPSHOT_VERSION, "read_at": iso_utc(sensor.now()), "accounts": accounts}


def _refresh_one(sensor: Sensor, home: Path, prior: Mapping[str, Any] | None,
                 lanes: list[dict[str, Any]], *, heal: bool, heal_after_s: float,
                 pace: Callable[[], None] | None, stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    backed, _how = associate(lanes, (prior or {}).get("identity"), home.name)
    held = any(lane.get("held") for lane in backed)
    _token, _expired, expires = sensor.login_state(home)
    observed = parse_time((prior or {}).get("observed_at"))
    settled = (prior is not None and prior.get("status") in SETTLED and observed is not None
               and recent(sensor.now(), observed, SETTLED_RETRY_S)
               and prior.get("login_expires_ms") == expires)
    if settled:
        account = {**prior, "healed": False}
    else:
        account = sensor.read(home, allow_heal=heal and bool(backed) and not held, previous=prior,
                              heal_after_s=heal_after_s, pace=pace, stop=stop)
        if held and account["status"] == LOGIN_EXPIRED:
            account["status"], account["detail"] = HELD, "an operator hold covers this account; no turn is spent on it"
    backed, how = associate(lanes, account.get("identity"), home.name)
    account["lanes"], account["lanes_by"] = sorted(lane["lane_id"] for lane in backed), how
    # The folder now holds another account. `Sensor.read` dropped what the old
    # one held when the profile named the new one; nothing of it is lost here.
    changed = bool(prior and prior.get("identity") and account.get("identity")
                   and prior["identity"] != account["identity"])
    found = lost_since(prior, account, sensor.now())
    if any(item["reason"] == "lapse" for item in found):
        account.update(cards=None, cards_unlisted=None, credits=[])
    record = ([] if changed else lost_items(prior or {})) + found
    if record:
        account["lost"] = record
    return account


def view(snapshot: Mapping[str, Any] | None, now: datetime, *, warn_days: float,
         plan_ends: Mapping[str, datetime] | None = None) -> dict[str, Any]:
    """What `status`, `status.json` and the alerts read: accounts and warnings.

    Pure. Never says a card exists unless an `ok` read (now or before) saw it.
    """
    accounts = [dict(row) for row in (snapshot or {}).get("accounts") or [] if isinstance(row, dict)]
    plan_ends = plan_ends or {}
    for account in accounts:
        account["unused_cards"] = sum(grant["resets_left"] for grant in unused_cards(account, now))
        if isinstance(account.get("cards"), dict):
            account["cards"] = {**account["cards"], "grants": [
                {**grant, "ended": (ends := parse_time(grant.get("ends_at"))) is not None and ends <= now}
                for grant in account["cards"].get("grants") or []]}
        declared = plan_end_for(account, plan_ends)
        account["plan_ends_at"] = iso_utc(declared) if declared else None
        account["claimable"] = claimable(account.get("cloud_credit_claim"), now)
        account["recently_lost"] = [item for item in lost_items(account)
                                    if (seen := parse_time(item.get("at"))) is not None
                                    and timedelta(0) <= now - seen <= timedelta(days=warn_days)]
    return {"read_at": (snapshot or {}).get("read_at"), "warn_days": warn_days, "accounts": accounts,
            "warnings": warnings(accounts, now, warn_days=warn_days, plan_ends=plan_ends)}


def load_view(root: Path, policy: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The view from the state root's files, for the daemon and for `status` offline."""
    config = settings(policy)
    return view(read_snapshot(root / SNAPSHOT_FILE), now, warn_days=float(config["warn_days"]),
                plan_ends=load_plan_ends(root / PLAN_ENDS_FILE))


def logins_folder(root: Path, policy: Mapping[str, Any]) -> Path:
    folder = Path(str(settings(policy)["logins_dir"])).expanduser()
    return folder if folder.is_absolute() else root / folder


def discover_logins(folder: Path) -> list[Path]:
    """Every config folder under the logins folder, sorted; files are ignored."""
    try:
        return sorted(path for path in folder.iterdir() if path.is_dir() and not path.name.startswith("."))
    except OSError:
        return []


def read_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("version") == SNAPSHOT_VERSION else {}


def write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    """C-8.1: temp file, fsync, rename, directory fsync, mode 0600; a reader sees
    the old snapshot or the new one, never half."""
    from .guardian import atomic_publish
    atomic_publish(path, (json.dumps(snapshot, indent=1, sort_keys=True) + "\n").encode("utf-8"))
