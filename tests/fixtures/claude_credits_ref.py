"""A verbatim copy of five functions from the claude-credits tool, for tests only.

Source: /Users/maxghenis/reviews/fleet-ops/claude-credits/claude_credits.py
sha256: 678b2be9ef590bc251610b875b620e77c5e8469f0618bb51f9cffe227ee69361

The functions `classify`, `fingerprint`, `renewal_on_or_after`, `days_left` and
`exhausted_until`, with the constants and the exception they need (`KEY_RE`,
`NOT_A_CONSOLE_KEY`, `Refused`), are copied without edits, in source order.
Tests compare `subfleet.credit_lane` with them (C-31.2, C-31.4). Production
code never imports this module or the tool (docs/decisions/2026-11-03-api-credit-lanes.md).
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import re


KEY_RE = re.compile(r"sk-ant-(?P<kind>usr|svc|api)(?P<ver>\d{2})?-[A-Za-z0-9_-]{40,}")
NOT_A_CONSOLE_KEY = {
    "oat": "a subscription OAuth token (claude setup-token). It belongs in claude-quota-<email>; "
           "enroll it with `subfleet lanes enroll`.",
    "ort": "an OAuth refresh token, not an API key.",
    "admin": "an Admin API key. It can read usage and manage the org but can't send messages.",
}


class Refused(Exception):
    """A check failed: say why, change nothing."""


def classify(value: str) -> str:
    """Kind of Console key (usr, svc, api), or raise Refused naming what it is instead."""
    m = KEY_RE.fullmatch(value)
    if m:
        return m["kind"]
    for kind, why in NOT_A_CONSOLE_KEY.items():
        if value.startswith(f"sk-ant-{kind}"):
            raise Refused(f"that is {why}")
    raise Refused("that doesn't look like a Claude Console API key (expected sk-ant-usr-…)")


def fingerprint(value: str) -> str:
    """Short sha256 of a high-entropy key: enough to tell keys apart, useless for guessing one."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def renewal_on_or_after(day: int, ref: dt.date) -> dt.date:
    """The first renewal date on or after `ref` for a plan that renews on `day` (1-31);
    short months renew on their last day."""
    if not 1 <= day <= 31:
        raise ValueError("renewal day must be 1-31")
    y, m = ref.year, ref.month
    for _ in range(2):
        d = dt.date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if d >= ref:
            return d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise AssertionError("unreachable: some renewal falls within two months")


def days_left(entry: dict, ref: dt.date) -> int | None:
    """Days until this account's credit expires (its next renewal), or None if unknown."""
    day = entry.get("renews_day")
    if not day:
        return None
    nxt = renewal_on_or_after(day, ref)
    # Credit refreshes on the renewal day itself, so on that day the current credit is new.
    if nxt == ref:
        nxt = renewal_on_or_after(day, ref + dt.timedelta(days=1))
    return (nxt - ref).days


def exhausted_until(entry: dict, seen: dt.date) -> str:
    """How long an exhaustion seen on `seen` holds: until the plan's next renewal, when new
    credit arrives. On the renewal day itself the new credit may not have landed yet (the
    time of day isn't documented), and with no renewal day known there's no date to wait
    for, so both re-check the next day. So does a workspace spend limit, which the plan
    holder can raise at any time."""
    day = entry.get("renews_day")
    if day and entry.get("exhausted_by") != "workspace":
        nxt = renewal_on_or_after(day, seen)
        if nxt > seen:
            return nxt.isoformat()
    return (seen + dt.timedelta(days=1)).isoformat()
