"""C-31: an API credit lane's own rules, pure and free of the store.

A credit lane spends one Max plan's monthly Claude API credit through the
`claude-api` provider (docs/decisions/2026-11-03-api-credit-lanes.md). This
module holds what the credential resolver, the adapter and the daemon share:

- the lane's reference (`claude-api-<email>`) and account key (C-31.1);
- the Console key rules and the key fingerprint (C-31.2);
- the read-only `claude-credits` registry (C-31.2, C-31.4);
- renewals, the credit closure's end and the cycle (C-31.4);
- the cycle ledger and the per-attempt grant (C-31.4).

The key rules and the renewal arithmetic mirror the `claude-credits` tool.
`tests/fixtures/claude_credits_ref.py` holds a verbatim copy of its functions,
and `tests/unit/test_credit_lane.py` checks that these agree with it. Nothing
here imports that tool or runs it.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
import re
from typing import Any

from .home_paths import home_path

#: C-31.1: the agent-keychain namespace `claude-credits` stores plan keys under.
REFERENCE_PREFIX = "claude-api-"
#: C-31.1: a credit lane's account key is `claude-api:<email>`.
ACCOUNT_PREFIX = "claude-api:"
#: C-31.1: the plan every credit lane records.
PLAN = "api-credit"
#: C-31.4: the enrolment default, Max 20x's monthly credit in US dollars.
DEFAULT_CREDIT_USD = 200.0
#: C-31.4: the share of `credit_usd` the ledger and the grants may reach. The
#: harness stops within one response of its grant; the rest covers that overshoot.
SPEND_SHARE = 0.95
#: C-31.4: a grant under this does not launch; the lane closes for credit instead.
MIN_GRANT_USD = 1.0
#: C-31.4: the ledger's window when the renewal day is unknown. No cycle is
#: longer, so the window can only shrink a grant.
TRAILING_CYCLE_DAYS = 31
#: C-31.2: the registry's path override. Under pytest no fallback to the
#: person's own file is ever taken (`home_path(..., test_only=True)`).
REGISTRY_VARIABLE = "SUBFLEET_API_PLAN_REGISTRY"
REGISTRY_MAX_BYTES = 1 << 20
#: C-31.2: every refusal of a credit credential uses this reason.
REFUSED = "credential-refused"

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
#: C-31.2: `claude-api-<email>`, with a plain ASCII address.
REFERENCE = re.compile(rf"claude-api-(?P<email>[A-Za-z0-9._%+-]{{1,64}}@{_LABEL}(?:\.{_LABEL})+)", re.ASCII)

#: C-31.2: `claude_credits.KEY_RE`, character for character.
KEY_RE = re.compile(r"sk-ant-(?P<kind>usr|svc|api)(?P<ver>\d{2})?-[A-Za-z0-9_-]{40,}")
#: C-31.2: the prefixes `claude_credits.classify` names, in its order, and what
#: each is in words that never carry a value.
_NOT_A_CONSOLE_KEY = (("oat", "a subscription OAuth token"), ("ort", "an OAuth refresh token"),
                      ("admin", "an Admin API key"))


def refused(message: str, fix: str | None = None, *, code: int = 7):
    """C-31.2: a credit lane's credential refusal, reason `credential-refused`.

    A client reads the reason's closed template (`refusals.HANDLER_MESSAGES`);
    `message` and `fix` stay with the exception for its direct callers and
    daemon.log, scrubbed whole. Neither ever carries a key: callers name the
    check that failed, never the value it read."""
    from .adapters.base import AdapterError
    return AdapterError(message, code=code, fix=fix, reason="credential-refused")


def reference_email(reference: Any) -> str | None:
    """C-31.2: the email of a `claude-api-<email>` reference, or None for anything else."""
    found = REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
    return found["email"] if found else None


def account_key(email: str) -> str:
    """C-31.1: the account key of the credit lane for `email`."""
    return ACCOUNT_PREFIX + email


def key_kind(value: str) -> str | None:
    """C-31.2: the kind (usr, svc, api) `claude_credits.classify` gives a Console
    key, or None for any value it refuses."""
    found = KEY_RE.fullmatch(value)
    return found["kind"] if found else None


def key_refusal(value: str) -> str | None:
    """C-31.2: None for a Console key; otherwise what the value is instead, in
    words that never carry it (`claude_credits.classify`'s rules)."""
    if key_kind(value) is not None:
        return None
    for prefix, what in _NOT_A_CONSOLE_KEY:
        if value.startswith(f"sk-ant-{prefix}"):
            return f"the keychain item holds {what}, not a Claude Console API key"
    return "the keychain item does not hold a Claude Console API key"


def fingerprint(value: str) -> str:
    """C-31.2: `claude_credits.fingerprint`, the registry's short sha256 of a key."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def registry_path() -> Path:
    """C-31.2: where `claude-credits` keeps its registry. Subfleet reads it and never writes it."""
    return Path(home_path(REGISTRY_VARIABLE,
                          lambda: Path.home() / ".config" / "claude-credits" / "registry.json",
                          test_only=True))


def read_registry(path: str | Path | None = None) -> dict[str, Any] | None:
    """C-31.2: the registry, or None when it cannot be read or parsed.

    The registry holds no secrets. A read or parse failure means the renewal is
    unknown, and credential resolution refuses: it has no fingerprint or
    organization to check against."""
    from .sessions.transcripts import read_regular
    try:
        data = json.loads(read_regular(registry_path() if path is None else path, limit=REGISTRY_MAX_BYTES))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("accounts"), dict) else None


def registry_entry(email: str, registry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """C-31.2: the registry's row for `email`, when it is a mapping."""
    entry = ((registry or {}).get("accounts") or {}).get(email) if isinstance(registry, Mapping) else None
    return entry if isinstance(entry, dict) else None


def entry_fingerprint(entry: Mapping[str, Any] | None) -> str | None:
    value = (entry or {}).get("fingerprint")
    return value if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{16}", value) else None


def entry_org(entry: Mapping[str, Any] | None) -> str | None:
    value = (entry or {}).get("org_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def renews_day(entry: Mapping[str, Any] | None) -> int | None:
    """C-31.4: the registry's renewal day (1-31), or None when unknown or invalid."""
    day = (entry or {}).get("renews_day") if isinstance(entry, Mapping) else None
    return day if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 31 else None


def renewal_on_or_after(day: int, ref: dt.date) -> dt.date:
    """`claude_credits.renewal_on_or_after`: the first renewal on or after `ref` for a
    plan that renews on `day` (1-31); short months renew on their last day."""
    if not 1 <= day <= 31:
        raise ValueError("renewal day must be 1-31")
    year, month = ref.year, ref.month
    for _ in range(2):
        found = dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))
        if found >= ref:
            return found
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise AssertionError("unreachable: some renewal falls within two months")


def next_renewal(day: int | None, ref: dt.date) -> dt.date | None:
    """C-31.4: the renewal strictly after `ref`, where `claude_credits.days_left`
    counts to (the credit refreshes on the renewal day itself), or None."""
    if day is None:
        return None
    found = renewal_on_or_after(day, ref)
    return found if found > ref else renewal_on_or_after(day, ref + dt.timedelta(days=1))


def last_renewal(day: int, ref: dt.date) -> dt.date:
    """C-31.4: the renewal on or before `ref`, which began the cycle `ref` is in."""
    year, month = ref.year, ref.month
    for _ in range(2):
        found = dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))
        if found <= ref:
            return found
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    raise AssertionError("unreachable: some renewal falls within two months")


def exhausted_until(day: int | None, seen: dt.date, *, workspace: bool = False) -> dt.date:
    """C-31.4, invariant 5: the day an exhaustion seen on `seen` stops holding the lane.

    The next renewal strictly after `seen`; one day later when the renewal is
    unknown, falls on `seen` (the new credit may not have landed yet), or the
    limit is a workspace's (its holder can raise it at any time). This is
    `claude_credits.exhausted_until` for the entry `{"renews_day": day}`, with
    `"exhausted_by": "workspace"` for a workspace limit."""
    if day is not None and not workspace:
        found = renewal_on_or_after(day, seen)
        if found > seen:
            return found
    return seen + dt.timedelta(days=1)


def local_date(instant: dt.datetime) -> dt.date:
    """The host's local date at `instant`: `claude-credits` reads renewals by it."""
    return instant.astimezone().date()


def local_midnight(day: dt.date) -> dt.datetime:
    """The aware instant the host's local `day` begins."""
    return dt.datetime.combine(day, dt.time()).astimezone()


def cycle_start(day: int | None, now: dt.datetime) -> dt.datetime:
    """C-31.4: when the cycle `now` falls in began: the last renewal on or before
    today, at local midnight; with no renewal day known, `TRAILING_CYCLE_DAYS`
    before `now`."""
    if day is None:
        return now - dt.timedelta(days=TRAILING_CYCLE_DAYS)
    return local_midnight(last_renewal(day, local_date(now)))


def _usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def attempt_charge(evidence: Mapping[str, Any], *, started: bool = True) -> float:
    """C-31.4: what an attempt that ended counts for in the ledger.

    The `usd` its `end` event reported (`classification.usd`). With no `end`
    event, the larger of the dollars its responses reported and its grant: the
    harness may have spent up to the grant without saying so. An attempt whose
    harness never started (no guardian, or a spawn error with no response)
    spent nothing."""
    classification = evidence.get("classification") if isinstance(evidence.get("classification"), Mapping) else {}
    spent = _usd(classification.get("usd"))
    if spent is not None:
        return spent
    responses = _usd(classification.get("responses_usd")) or 0.0
    if not started or (classification.get("spawn_error") and not classification.get("responses")):
        return responses
    return max(responses, _usd(evidence.get("credit_grant_usd")) or 0.0)


def attempt_grant(evidence: Mapping[str, Any]) -> float:
    """C-31.4: the grant of an attempt still in flight (zero before its launch)."""
    return _usd(evidence.get("credit_grant_usd")) or 0.0


def spend(attempts: Iterable[Mapping[str, Any]], since: dt.datetime) -> tuple[float, float]:
    """C-31.4: `(ledger, in flight)` in US dollars for the lane's attempts.

    Each attempt is `{"finished_at": <ISO instant or None>, "live": bool,
    "started": bool, "evidence": {...}}`. The ledger sums the charge of every
    attempt that ended at or after `since`; an attempt that is live or has no
    end time counts its grant as in flight."""
    ledger = in_flight = 0.0
    for attempt in attempts:
        evidence = attempt.get("evidence") or {}
        finished = attempt.get("finished_at")
        if attempt.get("live") or not finished:
            in_flight += attempt_grant(evidence)
            continue
        try:
            ended = dt.datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
        except ValueError:
            # An unreadable end time counts: it can only shrink grants.
            ledger += attempt_charge(evidence, started=attempt.get("started", True))
            continue
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=dt.timezone.utc)
        if ended >= since:
            ledger += attempt_charge(evidence, started=attempt.get("started", True))
    return ledger, in_flight


def grant(credit_usd: float, cap_usd: float, ledger_usd: float, in_flight_usd: float) -> float:
    """C-31.4, invariant 8: the next attempt's `--max-usd`, floored to the cent.

    The smaller of the policy's per-attempt cap and the lane's remaining budget,
    `SPEND_SHARE` of `credit_usd` minus the ledger and the grants in flight;
    never negative. A result under `MIN_GRANT_USD` does not launch."""
    remaining = SPEND_SHARE * float(credit_usd) - float(ledger_usd) - float(in_flight_usd)
    value = min(float(cap_usd), remaining)
    return max(0.0, math.floor(value * 100) / 100) if math.isfinite(value) else 0.0


def denied_org_ids(policy: Mapping[str, Any] | None) -> frozenset[str]:
    """C-31.2: the live policy's `claude_api.denied_org_ids`."""
    section = (policy or {}).get("claude_api") if isinstance(policy, Mapping) else None
    values = (section or {}).get("denied_org_ids") if isinstance(section, Mapping) else None
    return frozenset(value for value in values or () if isinstance(value, str))
