"""Validated routing policy and compatibility helpers (C-11.1, C-6.4)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    DEFAULT_CAPS, HEADROOM_FLOOR, PROVIDERS, READING_TTL_S, RETENTION_MAX_BYTES, RETENTION_MAX_JOBS, SCRUB_MAX_CHARS,
    TURN_RETENTION_KEEP_DAYS, TURN_RETENTION_MAX_BYTES, TURN_RETENTION_MAX_JOBS,
    Closure, Decision, Exit, Lane, Reading,
)

DEFAULT_POLICY_PATH = Path(__file__).with_name("default_policy.json")

#: `sessions.handoff_caps` (C-23.36): a character cap per brief section, carried
#: forward from v1 `handoff.py`'s module constants so a ported brief is the same
#: size it always was. `recent_records` is a count of main-chain entries, not
#: characters; it bounds the scan, and the character caps bound the result.
HANDOFF_CAPS: dict[str, int] = {
    "recent_records": 40,
    "recent": 48_000,
    "tool_result": 5_000,
    "tool_results_total": 16_000,
    "tool_input": 4_000,
    "tool_inputs_total": 12_000,
    "original_task": 24_000,
    "progress": 32_000,
    "repository": 16_000,
}
#: The caps of the sections a brief carries (the tool caps bound what `recent`
#: selects), and room for its header: fixed text, three paths and a session id.
#: The assembled brief is scrubbed whole once more (C-23.14), so together they
#: must fit the scrubber's bound, or its final pass would exceed it (C-23.36).
HANDOFF_BRIEF_SECTIONS = ("original_task", "recent", "progress", "repository")
HANDOFF_HEADER_CHARS = 16 * 1024
HANDOFF_BRIEF_MAX_CHARS = SCRUB_MAX_CHARS - HANDOFF_HEADER_CHARS

#: `sessions.*` (C-6.4): the sessions kit's caps, all of them policy data rather
#: than constants, because a restart storm or a slow host is a tuning problem.
#: `auto_revive_desktop_owned` is plan decision 7 and defaults to OFF: a lease in
#: subfleet's store binds only launches subfleet makes, so it cannot make a
#: headless revive exclusive against a desktop restart that lands between the
#: census and the launch (the 2026-09-04 twin). Handoff is the default recovery.
SESSION_DEFAULTS: dict[str, Any] = {
    "nudge_max_age_h": 8,            # C-23.33's age cap on the interruption
    "nudge_cooldown_min": 1.5,       # C-23.33's per-session cooldown (v1: 90 s)
    "nudge_delay_s": 8,              # the inbox binds a moment after SessionStart
    "nudge_sample_s": 3,             # C-23.34's liveness sample before delivery
    "sweep_quiet_s": 120,            # C-23.34: a hand-started sweep waits longer
    "muster_max_age_h": 2,           # the roll-call window
    "muster_quiet_s": 120,
    "revive_min_age_s": 120,         # below this the app may still restart it
    "revive_max_batch": 8,
    "auto_revive_desktop_owned": False,
    "mirror_interval_s": 60,         # C-23.28, plan decision 8
    "mirror_hot_interval_s": 2,      # C-23.28: spread before the app's next load
    "mirror_stall_min": 10,
    "mirror_hang_min": 30,           # C-23.28's in-flight tolerance
    "mirror_ultracode_default": True,
}

#: `conversations.*` (C-24 to C-30): the desktop workspace's timings.
#: The turn clocks, in seconds, are read by `TurnRunner` (`subfleet/conversations/runner.py`).
#: A stop escalates as C-24.7, review IR-3 and design D-13 (revision 3) order
#: it: the provider's own interrupt at once, then SIGINT through the guardian's
#: relay, then closing stdin, then C-5.6 containment, each that many seconds
#: after the stop was requested. `after_result_s` is how long a process may outlive its terminal
#: event before the same escalation stops it (D-15: the 120 s background ceiling
#: every Claude turn launches with, plus 15 s). `approval_wait_s` is D-7's bound
#: on an unanswered approval. The two turn caps (C-26.9) are `null` by default,
#: meaning no cap: a person's turn waits only for a lane that can take it, never
#: for a count Subfleet imposes (Max, 2026-09-27, after a turn waited 12 minutes
#: behind two other conversations' turns while one turn per lane was the rule).
CONVERSATION_DEFAULTS: dict[str, float | None] = {
    "approval_wait_s": 3600,         # C-26.9: an unanswered approval stops its turn
    "catalog_interval_s": 60,        # C-30.1, design D-23: a catalog run this often; 0: on request only
    "compact_after_s": 300,          # C-25.4: a settled turn keeps its deltas this long
    "compact_per_tick": 20,          # C-25.4: attempts compacted per conversation tick
    "max_active_turns": None,        # C-26.9: turns running at once, apart from detached jobs; null: no cap
    "turn_slots_per_lane": None,     # C-26.9: turns on one lane at once, apart from detached jobs; null: no cap
    "stop_sigint_after_s": 10,       # C-24.7: a stop not honoured by then gets SIGINT
    "stop_close_after_s": 20,        # C-24.7: then stdin is closed
    "stop_contain_after_s": 30,      # C-24.7: then the attempt is contained
    "after_result_s": 135,           # C-26.5: background output allowed after `result`
}

#: C-26.9: the `conversations` keys that cap turns, each a positive whole number or null (no cap).
TURN_CAPS = frozenset({"max_active_turns", "turn_slots_per_lane"})


def turn_cap(conversations: Mapping[str, Any] | None, key: str) -> int | None:
    """C-26.9: one turn cap from policy `conversations`, or None when there is none.

    A section without the key has the default, which is no cap, so a missing
    key and a null mean the same thing to every reader.
    """
    if key not in TURN_CAPS:
        raise KeyError(key)
    value = (conversations or {}).get(key, CONVERSATION_DEFAULTS[key])
    return None if value is None else int(value)


#: `conversations.default_effort` (C-26.8): the effort a turn runs at when its
#: message names none, per provider. It applies only where the catalog a turn last
#: reported for the model offers it; a provider set to null keeps its own default,
#: and `default_effort: null` turns the default off for every provider.
CONVERSATION_DEFAULT_EFFORT: dict[str, str | None] = {"claude": "ultracode", "codex": None}

#: `retention.*` (C-8.4, C-26.12): detached jobs and conversation turn jobs are
#: pruned against separate budgets, so a busy conversation never evicts the
#: evidence of detached work, and the reverse.
RETENTION_DEFAULTS: dict[str, float] = {
    "jobs": RETENTION_MAX_JOBS,
    "bytes": RETENTION_MAX_BYTES,
    "turn_jobs": TURN_RETENTION_MAX_JOBS,
    "turn_bytes": TURN_RETENTION_MAX_BYTES,
    "turn_keep_days": TURN_RETENTION_KEEP_DAYS,
}




def policy_hash(path: str | Path) -> str:
    """C-11.1: hash the policy file's exact bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PolicyError(ValueError):
    """Invalid routing data or model input, reported as exit 2 (C-11.1)."""

    code = Exit.INVALID_INPUT

    def __init__(self, path: str | Path, key: str, message: str):
        self.path, self.key = str(path), key
        super().__init__(f"{self.path}: {key}: {message}")


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fraction(value: Any, maximum: float = 1) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= maximum)


def load_policy(path: str | Path) -> dict[str, Any]:
    """C-11.1: validate routing data and retain the hash of the bytes evaluated.

    Extra policy keys are preserved for independent policy consumers. Each
    known field is validated before routing can use it; every error includes
    the file and dotted/indexed key that needs correcting.
    """
    path = Path(path)

    def fail(key: str, message: str) -> None:
        raise PolicyError(path, key, message)

    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PolicyError(path, "$", f"cannot read policy JSON: {error}") from error
    if not isinstance(value, dict):
        fail("$", "must be an object")
    required = ("tiers", "chains", "fallback", "permissions", "models", "retired",
                "desktop_login", "caps", "reset_credits")
    for key in required:
        if key not in value:
            fail(key, "required key is missing")

    tiers = value["tiers"]
    if not isinstance(tiers, list) or not tiers:
        fail("tiers", "must be a nonempty list of distinct tier names")
    for index, tier in enumerate(tiers):
        if not _name(tier):
            fail(f"tiers[{index}]", "must be a nonempty tier name")
        if tier in tiers[:index]:
            fail(f"tiers[{index}]", f"duplicate tier {tier!r}")

    models = value["models"]
    if not isinstance(models, dict) or not models:
        fail("models", "must be a nonempty map of short names to model definitions")
    for name, model in models.items():
        key = f"models.{name}"
        if not _name(name):
            fail(key, "model short name must be nonempty")
        if not isinstance(model, dict):
            fail(key, "must be a model definition object")
        if model.get("provider") not in PROVIDERS:
            fail(f"{key}.provider", f"must be one of {', '.join(PROVIDERS)}")
        if not _name(model.get("id")):
            fail(f"{key}.id", "must be a nonempty model id")
        for optional in ("effort", "scope"):
            if optional in model and not _name(model[optional]):
                fail(f"{key}.{optional}", "must be a nonempty string when provided")
        if "priority" in model and (type(model["priority"]) is not int or model["priority"] < 0):
            fail(f"{key}.priority", "must be a nonnegative integer when provided")

    chains = value["chains"]
    if not isinstance(chains, dict) or not chains:
        fail("chains", "must map task names to one model per tier")
    for task, chain in chains.items():
        key = f"chains.{task}"
        if not _name(task):
            fail(key, "task name must be nonempty")
        if not isinstance(chain, list) or len(chain) != len(tiers):
            fail(key, "must be a list with one short model name per tier")
        for index, model in enumerate(chain):
            if not _name(model) or model not in models:
                fail(f"{key}[{index}]", f"unknown model {model!r}; expected a models key")
    if value["fallback"] != "upward-only":
        fail("fallback", 'must be "upward-only"')
    if value["desktop_login"] != "never":
        fail("desktop_login", 'must be "never"')

    permissions = value["permissions"]
    if not isinstance(permissions, dict):
        fail("permissions", "must map task names (or *) to sandboxes")
    for task, sandbox in permissions.items():
        if task != "*" and task not in chains:
            fail(f"permissions.{task}", "unknown task; expected a chains key or *")
        if sandbox not in ("read-only", "workspace-write"):
            fail(f"permissions.{task}", "must be read-only or workspace-write")
    for task in chains:
        if task not in permissions and "*" not in permissions:
            fail(f"permissions.{task}", "missing sandbox; provide this task or *")

    retired = value["retired"]
    if not isinstance(retired, dict):
        fail("retired", "must map retired aliases to model short names")
    for alias, target in retired.items():
        if not _name(alias):
            fail(f"retired.{alias}", "alias must be a nonempty string")
        if alias in models:
            fail(f"retired.{alias}", "alias must not shadow a current model short name")
        if not _name(target) or target not in models:
            fail(f"retired.{alias}", f"unknown model {target!r}; expected a models key")

    if not isinstance(value["caps"], dict):
        fail("caps", "must be a map of positive admission bounds")
    caps = {**DEFAULT_CAPS, "reading_ttl_s": READING_TTL_S,
            "max_tokens_observed": None, **value["caps"]}
    for name, cap in caps.items():
        if name == "max_tokens_observed" and cap is None:
            continue
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            fail(f"caps.{name}", "must be a positive integer")
    value["caps"] = caps

    floor = value.get("headroom_floor", HEADROOM_FLOOR)
    if not _fraction(floor):
        fail("headroom_floor", "must be a finite fraction between 0 and 1")
    value["headroom_floor"] = floor
    reset = value["reset_credits"]
    if not isinstance(reset, dict):
        fail("reset_credits", "must be an object")
    for key in ("enabled", "headroom_floor_pct", "min_interval_min"):
        if key not in reset:
            fail(f"reset_credits.{key}", "required key is missing")
    if not isinstance(reset["enabled"], bool):
        fail("reset_credits.enabled", "must be a boolean")
    if not _fraction(reset["headroom_floor_pct"], 100):
        fail("reset_credits.headroom_floor_pct", "must be a finite percentage between 0 and 100")
    interval = reset["min_interval_min"]
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or not math.isfinite(interval) or interval <= 0):
        fail("reset_credits.min_interval_min", "must be a positive number of minutes")

    for section, defaults in (("timers", {"probe_interval_s": 60, "keepalive_interval_s": 18300}),
                              ("alerts", {"realert_hours": 6, "expiring_capacity_daily": True})):
        supplied = value.get(section, {})
        if not isinstance(supplied, dict):
            fail(section, "must be an object")
        settings = {**defaults, **supplied}
        for key, default in defaults.items():
            item = settings[key]
            if isinstance(default, bool):
                if not isinstance(item, bool):
                    fail(f"{section}.{key}", "must be a boolean")
            elif (not isinstance(item, (int, float)) or isinstance(item, bool)
                  or not math.isfinite(item) or item <= 0):
                fail(f"{section}.{key}", "must be a positive finite number")
        value[section] = settings

    # `sessions` is validated on its own because zero is meaningful in it: every
    # cap, window and interval there switches OFF at zero — a mirror interval of
    # 0 stops the timer without removing the verb, a cooldown of 0 removes the
    # restart-storm guard, a quiet window of 0 removes the wait. The two mirror
    # health windows are the exception: a zero there would call the mirror
    # stalled the instant a pass ended, which is not "off", it is broken.
    supplied = value.get("sessions", {})
    if not isinstance(supplied, dict):
        fail("sessions", "must be an object")
    settings = {**SESSION_DEFAULTS, **{k: v for k, v in supplied.items()
                                       if k != "handoff_caps"}}
    for key, default in SESSION_DEFAULTS.items():
        item = settings[key]
        if isinstance(default, bool):
            if not isinstance(item, bool):
                fail(f"sessions.{key}", "must be a boolean")
        elif (not isinstance(item, (int, float)) or isinstance(item, bool)
              or not math.isfinite(item) or item < 0):
            fail(f"sessions.{key}", "must be a nonnegative finite number")
        elif key in ("mirror_stall_min", "mirror_hang_min") and item <= 0:
            fail(f"sessions.{key}", "must be a positive finite number of minutes")
    value["sessions"] = settings

    # `sessions.handoff_caps` is the one nested section: every handoff section is
    # bounded by an explicit character cap (C-23.36), and a cap of zero or a
    # non-number would silently produce an unbounded or empty brief.
    supplied_caps = supplied.get("handoff_caps", {})
    if not isinstance(supplied_caps, dict):
        fail("sessions.handoff_caps", "must be an object of per-section character caps")
    caps = {**HANDOFF_CAPS, **supplied_caps}
    for key, item in caps.items():
        if (not isinstance(item, int) or isinstance(item, bool) or item <= 0):
            fail(f"sessions.handoff_caps.{key}", "must be a positive whole number of characters")
    brief = sum(caps[key] for key in HANDOFF_BRIEF_SECTIONS)
    if brief > HANDOFF_BRIEF_MAX_CHARS:
        fail("sessions.handoff_caps",
             f"{' + '.join(HANDOFF_BRIEF_SECTIONS)} is {brief:,} characters; the assembled brief is "
             f"scrubbed whole, so they may total at most {HANDOFF_BRIEF_MAX_CHARS:,}")
    value["sessions"]["handoff_caps"] = caps

    # `network` (d260): whether a writable Codex job's shell reaches the network.
    network = value.get("network", {})
    if not isinstance(network, dict):
        fail("network", "must be an object")
    for key, item in network.items():
        if key != "codex_workspace_write":
            fail(f"network.{key}", "is not a network setting (codex_workspace_write)")
        if not isinstance(item, bool):
            fail(f"network.{key}", "must be true or false")

    # `conversations` and `retention`: whole counts where the value counts
    # things, and zero only where it means "at once", "never on a timer" or "keep
    # nothing extra" (C-25.4's compaction delay, C-30.1's catalog timer, C-26.12's
    # days kept after a turn ends). A turn cap may also be null: no cap (C-26.9).
    # C-26.8: `conversations.default_effort` names an effort, or null, per provider.
    default_effort = (value.get("conversations") or {}).get("default_effort") if isinstance(
        value.get("conversations"), dict) else None
    if default_effort is not None:
        if not isinstance(default_effort, dict):
            fail("conversations.default_effort", "must be an object of provider to effort or null")
        for key, item in default_effort.items():
            if key not in CONVERSATION_DEFAULT_EFFORT:
                fail(f"conversations.default_effort.{key}", "is not a provider (claude, codex)")
            if item is not None and (not isinstance(item, str) or not item or len(item) > 20):
                fail(f"conversations.default_effort.{key}", "must be an effort name or null")
    for section, defaults, may_be_zero, whole in (
            ("conversations", CONVERSATION_DEFAULTS, {"compact_after_s", "catalog_interval_s"},
             {"compact_per_tick", *TURN_CAPS}),
            ("retention", RETENTION_DEFAULTS, {"turn_keep_days"}, {"jobs", "bytes", "turn_jobs", "turn_bytes"})):
        supplied = value.get(section, {})
        if not isinstance(supplied, dict):
            fail(section, "must be an object")
        settings = {**defaults, **supplied}
        for key in defaults:
            item = settings[key]
            if item is None and section == "conversations" and key in TURN_CAPS:
                continue
            if (not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item)
                    or item < 0 or (item == 0 and key not in may_be_zero)):
                fail(f"{section}.{key}", "must be a nonnegative finite number" if key in may_be_zero
                     else "must be a positive finite number")
            if key in whole and item != int(item):
                fail(f"{section}.{key}", "must be a whole number")
        value[section] = settings
    # C-8.4, C-13.4: the only thing that may remove an allocated worktree is the
    # archiver named here, run once a pass with every worktree retention would
    # retire; absent or null, retention keeps those worktrees and their jobs.
    archiver = value["retention"].get("worktree_archiver")
    if archiver is not None:
        if not isinstance(archiver, dict):
            fail("retention.worktree_archiver", "must be an object with argv, or null")
        unknown = set(archiver) - {"argv", "per_worktree", "timeout_s"}
        if unknown:
            fail(f"retention.worktree_archiver.{sorted(unknown)[0]}", "is not a setting (argv, per_worktree, timeout_s)")
        argv = archiver.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            fail("retention.worktree_archiver.argv", "must be a non-empty list of non-empty strings")
        if not os.path.isabs(argv[0]):
            fail("retention.worktree_archiver.argv", "must start with an absolute path")
        per_worktree = archiver.get("per_worktree", ["--only", "{path}"])
        if (not isinstance(per_worktree, list) or not all(isinstance(part, str) for part in per_worktree)
                or not any("{path}" in part for part in per_worktree)):
            fail("retention.worktree_archiver.per_worktree", "must be a list of strings naming {path}")
        timeout = archiver.get("timeout_s", 3600)
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout)
                or timeout <= 0):
            fail("retention.worktree_archiver.timeout_s", "must be a positive finite number")
        value["retention"]["worktree_archiver"] = {"argv": list(argv), "per_worktree": list(per_worktree),
                                                   "timeout_s": float(timeout)}
    # C-24.7, C-26.5, C-26.9: the stop escalation keeps its order (SIGINT, then
    # closing stdin, then containment), because each step is only worth taking
    # while the previous one had its chance to end the turn.
    clocks = value["conversations"]
    if not clocks["stop_sigint_after_s"] < clocks["stop_close_after_s"] < clocks["stop_contain_after_s"]:
        fail("conversations.stop_close_after_s",
             "the stop escalation must keep its order: "
             "stop_sigint_after_s < stop_close_after_s < stop_contain_after_s")

    # Metadata is replaced even when a caller serializes a previously loaded map.
    value["_policy_hash"] = hashlib.sha256(raw).hexdigest()
    value["_policy_path"] = str(path)
    return value


def resolve_model(policy: Mapping[str, Any], name: str, *, key: str = "pinned_model",
                  note: bool = True) -> str:
    """C-11.1: resolve a current name, exact id, or retired alias to a short name."""
    models = policy["models"]
    if _name(name):
        if name in models:
            return name
        retired = policy.get("retired", {})
        if name in retired:
            target = retired[name]
            if note:
                print(f"subfleet: retired model {name!r} resolves to {target!r}", file=sys.stderr)
            return target
        for short, model in models.items():
            if model["id"] == name:
                return short
    raise PolicyError(policy.get("_policy_path", "policy.json"), key,
                      f"unknown model {name!r}; expected a models or retired key")


def _row(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else dict(value)


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def lane_capacity(policy: Mapping[str, Any], lane_id: str,
                  readings: Iterable[Reading | Mapping[str, Any]], *, now: datetime | str | None = None) -> int:
    """C-6.4: an unmeasured or stale lane is limited to one in-flight attempt."""
    from .capacity import fresh_provider

    instant = _time(now) if now is not None else datetime.now(timezone.utc)
    caps = {**DEFAULT_CAPS, "reading_ttl_s": READING_TTL_S, **policy.get("caps", {})}
    measured = False
    for item in readings:
        row = _row(item)
        if row.get("lane_id") != lane_id or row.get("label") != "provider":
            continue
        try:
            if fresh_provider(row, now=instant, reading_ttl_s=caps["reading_ttl_s"]):
                measured = True
                break
        except (ValueError, TypeError, KeyError):
            continue
    return caps["max_in_flight_per_lane"] if measured else min(caps["max_in_flight_per_lane"], caps["max_in_flight_unmeasured"], 1)


def pick(policy: Mapping[str, Any], lanes: Iterable[Lane | Mapping[str, Any]], *,
         pinned_model: str | None = None, pinned_lane: str | None = None,
         task: str | None = None, tier: str | None = None, exclusions: Iterable[str] = (),
         allow_desktop: bool = False, closures: Iterable[Closure | Mapping[str, Any]] = (),
         readings: Iterable[Reading | Mapping[str, Any]] = (), in_flight: Mapping[str, int] | None = None,
         policy_digest: str = "", now: datetime | str | None = None) -> Decision:
    """C-11.2: compatibility entry point for the daemon's single evaluator.

    Callers of the original helper get the same reserve, capacity, ordering,
    and fallback decisions as submissions and `why`.
    """
    from .capacity import build_view
    from .scheduler import evaluate

    snapshot = build_view(lanes, readings, closures, now=now,
                          reading_ttl_s=policy.get("caps", {}).get("reading_ttl_s", READING_TTL_S))
    snapshot["in_flight"] = dict(in_flight or {})
    return evaluate(policy, snapshot, {
        "pinned_model": pinned_model, "pinned_lane": pinned_lane,
        "task": task, "tier": tier, "exclusions": tuple(exclusions),
        "allow_desktop": allow_desktop, "policy_hash": policy_digest or policy.get("_policy_hash", ""),
    })
