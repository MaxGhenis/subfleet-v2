"""C-24.4, C-29.11 (2026-09-29): why a conversation message waits, in its own words.

Admission records why it left a turn job unplaced (C-6.11's hold). The message
the job carries says so in its `state_reason`, as `<kind>: <detail>`, so the app
never guesses. On 2026-09-28 four of the owner's messages waited 29 minutes to 13
hours for another conversation's turn in the same folder, while the app said
"Waiting for capacity": their `state_reason` was empty and the app fell back to
that text. The kinds:

- `capacity`: a lane would take it once room frees (its slots are busy, the
  turn pool is at a policy cap, the account's usage is at the floor or kept for
  the reserved model). The only kind the app words as waiting for capacity.
- `closed`: every lane that could take it is closed by its provider until a
  reset; the detail names the earliest.
- `usage-unknown`: no fresh usage reading says whether a lane may take it.
- `no-lane`: no lane can take it for a reason no wait ends by itself
  (disabled, signed in to another account, excluded, needs a probe), or the
  lane a Codex conversation keeps refuses it so (C-11.8's `pin-unadmittable`).
- `lease`: another job holds something it needs; the detail names that job,
  and the conversation's title when it is a turn.
- `blocked`, `workspace`, `route`, `admission`, `placed`: the conversation is
  blocked, its folder could not be prepared, its route could not be evaluated,
  admission has not looked at it yet (or keeps it behind an older job), it was
  placed and its provider is starting.

Functions here are pure: the service passes `describe` to name a lease's holder
and `who` to name a job, so they are tested without a store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .. import render

#: A job bound to its message, before admission's first look at it.
SUBMITTED = "admission: sent to the daemon, which has not placed it yet"
#: Placed: an attempt is reserved and its provider is being started.
PLACED = "placed: starting the provider"

#: C-11.3 to C-11.7's lane rejections, as a person reads them, and each one's kind.
_LANE = {
    "no-slot": ("busy", "capacity"),
    "below-floor": ("at its usage floor", "capacity"),
    "disabled": ("disabled", "no-lane"),
    "identity-mismatch": ("signed in to another account", "no-lane"),
    "config-dir": ("a home lane, which cannot continue this session", "no-lane"),
    "owner-v1": ("still run by v1", "no-lane"),
    "excluded": ("excluded for this message", "no-lane"),
    "desktop": ("the desktop login", "no-lane"),
}
_ORDER = ("capacity", "closed", "usage-unknown", "no-lane")
_PROVIDERS = {"claude": "Claude", "codex": "Codex"}


def lane_label(reasons: list[str]) -> str:
    """The reason that keeps a lane from a job, as `scheduler.dominant_rejection` reads
    it: the first standing reason, `no-slot` only when there is no other."""
    standing = [reason for reason in reasons if reason != "no-slot"]
    return standing[0] if standing else (reasons[0] if reasons else "unknown")


def lane_kind(label: str) -> tuple[str, str]:
    """`(words, kind)` for one lane's reason."""
    if label.startswith("closed:"):
        return "closed", "closed"
    if label.startswith("reserve:"):
        _, model, state = (label.split(":", 2) + ["", ""])[:3]
        if state == "reserved":
            return f"kept for {model}", "capacity"
        if state == "unmeasured":
            return "without a fresh usage reading", "usage-unknown"
        if state == "probe-required":
            return "in need of a probe, which a conversation turn never waits for", "no-lane"
        return label, "no-lane"
    return _LANE.get(label, (label, "no-lane"))


def lane_summary(decision: Any) -> dict:
    """What kept a decision from every lane, small enough to keep in a hold: the
    provider, lane ids by reason, the earliest reset among closed lanes, and the
    capacity blocks (a turn cap: `fleet`). `decision` is a `scheduler.Decision` or
    its dict."""
    evaluations = (decision.get("evaluations") if isinstance(decision, Mapping)
                   else getattr(decision, "evaluations", None)) or ()
    lanes: dict[str, list[str]] = {}
    resets: list[str] = []
    provider = None
    blocks: set[str] = set()
    for evaluation in evaluations:
        provider = provider or evaluation.get("provider")
        blocks.update(str(block) for block in evaluation.get("capacity_blocks") or ())
        for row in evaluation.get("rejections") or ():
            reasons = [str(reason) for reason in (row.get("reasons") or [row.get("reason") or "unknown"])]
            label = lane_label(reasons)
            key = ":".join(label.split(":")[:2]) if label.startswith("closed:") else label
            lane = str(row.get("lane_id"))
            if lane not in lanes.setdefault(key, []):
                lanes[key].append(lane)
            if label.startswith("closed:") and label.count(":") >= 2:
                resets.append(label.split(":", 2)[2])
    return {"provider": provider, "lanes": lanes, "reset": min(resets) if resets else None,
            "blocks": sorted(blocks)}


def when(value: str | None) -> str:
    """An ISO instant as `2026-09-29 14:05 UTC`; the text as given when it does not parse."""
    if not value:
        return "an unknown time"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def lanes_reason(summary: Mapping[str, Any] | None, label: str) -> str:
    """No lane took the turn: the kind, then the lanes' reasons counted, the earliest
    reset named. `label` is the hold's own (C-6.11), used when there is no summary."""
    summary = summary or {}
    pool_cap = "fleet" in (summary.get("blocks") or ())
    lanes: Mapping[str, list[str]] = summary.get("lanes") or {}
    if not lanes:
        if pool_cap:
            return "capacity: the conversations' turn pool is at its policy cap (conversations.max_active_turns)"
        if label in ("no-lanes", "not-evaluated"):
            return "no-lane: no enrolled lane can take this conversation's model"
        words, kind = lane_kind(label)
        if kind == "closed":
            until = label.split(":", 2)[2] if label.count(":") >= 2 else None
            return "closed: every account that can take it is closed" + (f" until {when(until)}" if until else "")
        return f"{kind}: every lane that can take it is {words}"
    counts: dict[str, dict[str, int]] = {}
    for key, ids in lanes.items():
        words, kind = lane_kind(key)
        group = counts.setdefault(kind, {})
        group[words] = group.get(words, 0) + len(ids)
    kind = next((each for each in _ORDER if each in counts), "no-lane")
    reset = when(summary.get("reset"))
    parts = []
    for each in _ORDER:
        for words, n in sorted(counts.get(each, {}).items(), key=lambda item: (-item[1], item[0])):
            if words != "closed" or kind == "closed":
                parts.append(f"{n} {words}")
            else:
                parts.append(f"{n} closed until {reset}" if n == 1 else f"{n} closed, the first until {reset}")
    provider = _PROVIDERS.get(summary.get("provider") or "", "")
    noun = f"{provider} lane" if provider else "lane"
    if pool_cap and "no-slot" in lanes:
        # A lane that waits only for a slot, with a turn cap set: the cap is what holds it.
        head = "the conversations' turn pool is at its policy cap (conversations.max_active_turns)"
    else:
        head = {"capacity": f"no {noun} has room for it yet",
                "closed": f"every {noun} that could take it is closed until {reset} at the earliest",
                "usage-unknown": f"no {noun} can take it until its usage is read again",
                "no-lane": f"no {noun} can take it"}[kind]
    return f"{kind}: {head} ({'; '.join(parts)})"


def hold_reason(hold: Mapping[str, Any], *, describe: Callable[[str], str],
                who: Callable[[str], str]) -> str | None:
    """The message's `state_reason` for one admission hold (C-6.11), or None when the
    hold means the message is no longer waiting (`message-settled`)."""
    reason = str(hold.get("reason") or "waiting")
    if reason == "message-settled":
        return None
    if reason == "lease-held":
        held = [describe(key) for key in hold.get("leases") or ()]
        behind = [who(job_id) for job_id in hold.get("queued_behind") or ()]
        if behind:
            held.append("an older turn waiting for the same thing goes first: " + ", ".join(behind))
        return "lease: " + ("; ".join(held) or "a lease it needs is held by another job")
    if reason == "conversation-blocked":
        if hold.get("error_type"):
            return f"blocked: its conversation could not be checked ({hold.get('error_type')}: {hold.get('error')})"
        why = hold.get("blocked_by") or (f"held by the legacy import: {hold['legacy_hold']}"
                                         if hold.get("legacy_hold") else "blocked")
        return f"blocked: the conversation is blocked ({why})"
    if reason == "workspace":
        detail = f" ({hold['error_type']}: {hold['error']})" if hold.get("error_type") else ""
        return f"workspace: its folder could not be prepared{detail}; it is tried again with backoff"
    if reason == "route":
        detail = f" ({hold['error_type']}: {hold['error']})" if hold.get("error_type") else ""
        return f"route: its route could not be evaluated{detail}; it is looked at again with backoff"
    if reason == "attempt-live":
        return "admission: an earlier attempt of this turn is still live or quarantined"
    if reason == "route-moved":
        return "admission: its lane changed while it was being placed; it is looked at again at once"
    if reason == "behind-older-job":
        return f"capacity: behind an older turn waiting for the same lanes ({who(str(hold.get('behind')))})"
    if reason == "fleet-full":
        return (f"capacity: the conversations' turn pool is full ({hold.get('max_active_attempts')} at once, "
                "conversations.max_active_turns)")
    if reason == "slot-kept":
        return f"capacity: the last turn slot is kept for an older turn ({who(str(hold.get('kept_for')))})"
    if reason == "probe-pending":
        return "admission: its lane is being probed before it may start there"
    if reason in ("approval", "uncertain"):
        return ("admission: an operator's approval is needed" if reason == "approval" else
                "admission: a probe was quarantined; an operator must resolve it")
    if reason in ("waiting", "not-evaluated", "capacity"):
        # `capacity` here is the job row's raw `wait_reason`, which every admission
        # wait records (a lease wait too), met with no remembered hold: it says nothing
        # of lanes, so it is not worded as capacity.
        return "admission: the daemon looks at it again shortly"
    if reason == "parent-cap":
        return "capacity: its parent job already has as many attempts running as its cap allows"
    if reason == "pin-unadmittable":
        # C-11.8: a Codex conversation keeps its lane (C-26.2), and that lane refuses the
        # turn for a reason no wait ends. A turn is never failed for it (C-26.12).
        lane = hold.get("lane_id") or "its lane"
        return (f"no-lane: this conversation's lane {lane} cannot take it ({render.pin_refusals(hold)}); "
                "it waits until that changes")
    return lanes_reason(hold.get("lanes"), reason)
