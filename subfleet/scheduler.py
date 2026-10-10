"""Pure policy evaluation and queue admission rules (C-6.4, C-11.2–C-11.6).

The daemon supplies a capacity snapshot and reserves the chosen slot in its
admission transaction. Evaluation never probes, writes the store, or runs ps.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .capacity import credential_gone, desktop_excluded, fresh_provider, identity_blocked, pilot_block
from .contracts import (CAPACITY_RECHECK_BASE_S, CAPACITY_RECHECK_CEILING_S, DEFAULT_CAPS,
                        HEADROOM_FLOOR, Decision, Exit)
from .policy import (MEMORY_PRESSURE_LEVELS, PolicyError, admission_settings, cap, flatten_chain, lane_slot_cap,
                     resolve_model, turn_cap)

ACTIVE_ATTEMPTS = frozenset({"reserved", "starting", "running", "finalizing"})


def _row(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else dict(value)


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class RouteError(ValueError):
    """C-6.12: the job's own pin cannot be routed, whatever capacity does.

    Raised for a pin that names several lanes, a pin and a model of different
    providers, and an unmeasured-reserve authorization that is not whole: what
    submit refuses, and what no wait can fix, because only the caller can say
    what was meant. Admission refuses such a job with this message instead of
    abandoning its pass. `policy_dependent` marks the cases a policy edit can
    cause (a tier's chain moved to another provider): admission refuses those
    only under the policy the job was accepted with, and otherwise waits, as
    for any error that is not the job's. It is a ValueError, so a caller that
    caught the old ValueError still does.
    """

    def __init__(self, message: str, *, policy_dependent: bool = False):
        super().__init__(message)
        self.policy_dependent = policy_dependent


def _identities(lane: Mapping[str, Any]) -> set[str]:
    """Every name `-a` and `-x` may use for one lane (C-11.2, C-17.2).

    `label` is here because C-1.4 makes a verified Claude lane's account key a
    pair of uuids: without it an operator's `-a max@example.org` would stop
    resolving the moment a lane is re-enrolled with its identity bound.
    `email` is only in the capacity view: the Codex usage probe reports it and
    `Timers.enrich_view` adds it, so a pin resolved against store rows alone
    would miss it (C-11.2, the 2026-09-22 stall).
    """
    account = str(lane.get("account_key") or "")
    return {str(value) for value in (lane.get("lane_id"), account,
            account.partition(":")[2], lane.get("label"), lane.get("email"),
            lane.get("home")) if value}


def _binding(lane: Mapping[str, Any]) -> tuple[Any, Any]:
    """The credential a lane is bound to; a re-enrolment keeps it (`Timers.enrich_view`)."""
    return lane.get("provider"), lane.get("home") or lane.get("credential_ref")


def resolve_lane(lanes: Iterable[Any], pin: str, provider: str | None = None, *,
                 follow: bool = True) -> dict[str, Any] | None:
    """C-11.2: resolve one identity pin without silently choosing another lane.

    A lane id is exact, except that a disabled lane whose credential an enabled
    lane now holds (a re-enrolment, C-10.2) resolves to that lane, the same
    account on the same credential, unless `follow` is false (an authorization
    is bound to the lane id it was granted for, C-11.7). Any other name can
    match more than one lane: a Claude login and a Codex subscription often
    share an email, and a re-enrolment leaves the old binding in the roster
    under the same label. Two narrowings drop lanes that could never take the
    job, never one that could: `provider` (the job's, from `pin_provider`)
    drops lanes of the other provider, a disabled binding is dropped when an
    enabled lane that matches is bound to the same credential, and a lane whose
    credential proved to hold another account (C-10.6) is dropped when another
    lane of the same provider also matches. A name that still matches several lanes raises
    `RouteError` naming them. None means the pin names no lane.
    """
    roster = [_row(lane) for lane in lanes]
    exact = next((lane for lane in roster if lane.get("lane_id") == pin), None)
    if exact:
        if follow and not exact.get("enabled", True):
            return next((lane for lane in roster if lane.get("enabled", True)
                         and _binding(lane) == _binding(exact)), exact)
        return exact
    matches = [lane for lane in roster if pin in _identities(lane)]
    if len(matches) > 1 and provider:
        matches = [lane for lane in matches if lane.get("provider") == provider] or matches
    if len(matches) > 1:
        live = {_binding(lane) for lane in matches if lane.get("enabled", True)}
        matches = [lane for lane in matches if lane.get("enabled", True) or _binding(lane) not in live]
    if len(matches) > 1 and len({lane.get("provider") for lane in matches}) == 1:
        # C-10.6: a credential that proved to hold another account never takes a
        # job again. Only a tie within one provider: across providers the name is
        # still ambiguous, and dropping one side would pick the provider.
        matches = [lane for lane in matches if not identity_blocked(lane)] or matches
    if len(matches) > 1:
        names = ", ".join(sorted(str(lane["lane_id"]) for lane in matches))
        raise RouteError(f"pinned_lane: {pin!r} names {len(matches)} lanes ({names}); "
                         f"pin one of them by its lane id")
    return matches[0] if matches else None


def current_lane_id(lanes: Iterable[Any], lane_id: str, *, follow: bool = True) -> str:
    """C-4.5, C-11.2: the lane an attempt's lane id names now, as a pin to it resolves.

    A re-enrolment gives the same account on the same credential a new lane
    id; attempts on the old id and on its successor are attempts on one lane
    when retries and exclusions are counted.
    """
    try:
        found = resolve_lane(lanes, lane_id, follow=follow)
    except ValueError:
        found = None
    return str(found["lane_id"]) if found else lane_id


#: C-12.9: the provider whose launch gives a job the MCP servers it names.
MCP_PROVIDER = "claude"


def job_mcp_servers(job: Any) -> tuple[str, ...]:
    """C-12.9: the MCP servers a job named, from its row (JSON text) or a mapping."""
    value = _row(job).get("mcp_servers")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise RouteError("mcp_servers: the job's record is not a list of names") from None
    return tuple(value or ())


def mcp_chain(policy: Mapping[str, Any], chain: Iterable[str], job: Any) -> list[str]:
    """C-12.9: the models of `chain` a job may run on. A job that names MCP
    servers runs only where a launch can start them, on a Claude model; any
    other job keeps its whole chain."""
    chain = list(chain)
    if not job_mcp_servers(job):
        return chain
    return [name for name in chain if policy["models"][name]["provider"] == MCP_PROVIDER]


def pin_provider(policy: Mapping[str, Any], job: Any) -> str | None:
    """C-11.2: the provider a lane-pinned job must run on, as `evaluate` decides it.

    A pinned job evaluates one model: its pinned model, else the first model of
    its task's chain from its tier that it may run on (C-12.9). None when the
    job names neither, so only the lane can say, or when the policy cannot tell
    (`evaluate` reports that).
    """
    job = _row(job)
    try:
        if job.get("pinned_model"):
            return policy["models"][resolve_model(policy, job["pinned_model"], note=False)]["provider"]
        task = job.get("task")
        if task in policy["chains"]:
            tiers = policy["tiers"]
            tier = job.get("tier") or ("standard" if "standard" in tiers else tiers[0])
            chain = mcp_chain(policy, flatten_chain(policy["chains"][task], tiers.index(tier)), job)
            return policy["models"][chain[0]]["provider"] if chain else None
    except (PolicyError, ValueError, KeyError, IndexError):
        pass
    return None


#: C-6.9: who gets the next lane, first to last. `attended` is a conversation
#: turn from the Subfleet app; `priority` detached work chosen by the operator
#: (C-6.16); `session` a detached job someone is waiting on
#: now; `background` one nobody is (Max, 2026-09-27: "uncap everything and
#: instead use prioritization").
PRIORITY_CLASSES = ("attended", "priority", "session", "background")


@dataclass(frozen=True)
class Liveness:
    """C-6.9: who is waiting, read once per pass (`Daemon._liveness`).

    `sessions` are the Claude Code session ids (lower case) that a registry row
    names whose pid is still the process that wrote it (`registry.validated`);
    `jobs` the ids of jobs not yet finished, for a job's parent."""

    sessions: frozenset[str] = frozenset()
    jobs: frozenset[str] = frozenset()


def priority_class(job: Any, live: Liveness | None = None, *,
                   policy: Mapping[str, Any] | None = None,
                   jobs: Mapping[str, Any] | None = None) -> str:
    """C-6.9: a job's class, from what it records and who is live now.

    A turn is `attended`. Detached work whose caller or any ancestor's caller
    is in `admission.priority_callers` is `priority` (C-6.16), regardless of
    liveness. `jobs` supplies ancestor rows, including finished jobs; missing
    parents end the walk, and a visited set terminates cycles.
    Otherwise a gate round is `session`: a gate is always waited on
    by the `subfleet gate` that asked for it. Any other job is `session` while its
    caller's Claude Code session is live (a validated registry row names
    `caller_session`) or its parent job is unfinished; otherwise `background`. A
    caller's pid alone is not evidence: a live pid proves a process, not the
    caller. With no liveness to read (`live` None) every other detached job is
    `session`, which orders as before when priority callers are unset.
    """
    job = _row(job)
    kind = job.get("kind")
    if kind == "turn":
        return "attended"
    callers = {item.strip().lower() for item in admission_settings(policy or {})["priority_callers"] or ()}
    if callers:
        current = job
        seen: set[str] = set()
        while current:
            session = str(current.get("caller_session") or "").strip().lower()
            if session and session in callers:
                return "priority"
            parent = current.get("parent_job_id")
            if not parent or parent in seen:
                break
            seen.add(parent)
            current = _row((jobs or {}).get(parent, {}))
    if kind == "gate-review" or live is None:
        return "session"
    session = str(job.get("caller_session") or "").strip().lower()
    if session and session in live.sessions:
        return "session"
    if job.get("parent_job_id") and job["parent_job_id"] in live.jobs:
        return "session"
    return "background"


def ordered_jobs(policy: Mapping[str, Any], jobs: Iterable[Any], live: Liveness | None = None, *,
                 ancestors: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """C-4.1, C-6.9, C-26.9; plan amendment 11: class, then tier, then FIFO.

    Classes go `attended`, `priority`, `session`, `background` (`priority_class`);
    priority work is FIFO regardless of tier (C-6.16). Other classes keep the
    policy's tier order, then oldest first. Stable
    sorting preserves the store's submission order when second-precision
    timestamps are tied.
    """
    tiers = policy["tiers"]
    default = "standard" if "standard" in tiers else tiers[0]
    rank = {tier: index for index, tier in enumerate(tiers)}
    classes = {name: index for index, name in enumerate(PRIORITY_CLASSES)}
    rows = [_row(job) for job in jobs]
    family = {**(ancestors or {}), **{job["job_id"]: job for job in rows if job.get("job_id")}}

    def key(job: dict[str, Any]) -> tuple[int, int, str]:
        klass = priority_class(job, live, policy=policy, jobs=family)
        tier = 0 if klass == "priority" else rank.get(job.get("tier") or default, len(tiers))
        return classes[klass], tier, job.get("created_at") or ""

    return sorted(rows, key=key)


def pool_capped(policy: Mapping[str, Any], job: Any) -> bool:
    """C-6.9: whether the job's pool has a count one job could take from another.

    Only then does a job that cannot be placed hold back the later jobs that
    compete with it: in an uncapped pool a later job never takes a slot an
    older one needs, because there are no slots to take. A turn's pool is capped
    by the two turn caps (C-26.9), a detached job's by the fleet and per-lane
    caps, and a job with a parent also by the parent cap.
    """
    job = _row(job)
    caps = policy.get("caps") or {}
    if job.get("kind") == "turn":
        conversations = policy.get("conversations") or {}
        capped = (turn_cap(conversations, "max_active_turns") is not None
                  or turn_cap(conversations, "turn_slots_per_lane") is not None)
    else:
        capped = any(cap(caps, key) is not None for key in
                     ("max_active_attempts", "max_in_flight_per_lane", "max_in_flight_unmeasured"))
    return capped or bool(job.get("parent_job_id")) and cap(caps, "max_active_attempts_per_parent") is not None


def hold_scope(policy: Mapping[str, Any], job: Any) -> str | None:
    """C-6.9: which older jobs a job may wait behind. `pool`: any that compete with
    it (its pool has a fleet or per-lane count); `family`: only those that share
    an ancestor with it (the parent cap is its pool's only count, and that count is
    shared only within a family, review of PR #72); None: none."""
    job = _row(job)
    if not pool_capped(policy, job):
        return None
    caps = policy.get("caps") or {}
    if job.get("kind") == "turn":
        conversations = policy.get("conversations") or {}
        pooled = (turn_cap(conversations, "max_active_turns") is not None
                  or turn_cap(conversations, "turn_slots_per_lane") is not None)
    else:
        pooled = any(cap(caps, key) is not None for key in
                     ("max_active_attempts", "max_in_flight_per_lane", "max_in_flight_unmeasured"))
    return "pool" if pooled else "family"


def machine_hold(policy: Mapping[str, Any], machine: Mapping[str, Any] | None, klass: str) -> dict[str, Any] | None:
    """C-6.13: the `machine-busy` hold for a job of `klass`, or None to go on.

    A class's threshold is met while the larger of the 1- and 5-minute load
    averages per logical CPU is at or above its `load_per_cpu`, or the kernel's
    memory pressure is at or above its `memory_pressure`. The larger average
    holds quickly and lets go slowly, so a dip does not release a burst. An
    `attended` turn or `priority` job is never held, nor is any class the guard
    does not name, and nothing is held on a reading that is missing.
    """
    guard = admission_settings(policy).get("machine_guard")
    limits = (guard or {}).get(klass) if klass not in ("attended", "priority") else None
    if not limits or not machine:
        return None
    hold: dict[str, Any] = {}
    loads = [value for value in (machine.get("load1"), machine.get("load5")) if isinstance(value, (int, float))]
    cpus = machine.get("cpus")
    threshold = limits.get("load_per_cpu")
    if threshold is not None and loads and isinstance(cpus, int) and cpus > 0:
        per_cpu = round(max(loads) / cpus, 2)
        if per_cpu >= threshold:
            hold.update(load_per_cpu=per_cpu, load_threshold=threshold)
    level = machine.get("memory_pressure")
    named = limits.get("memory_pressure")
    if named is not None and isinstance(level, int) and level >= MEMORY_PRESSURE_LEVELS[named]:
        hold.update(memory_pressure=level, memory_threshold=named)
    return {"reason": "machine-busy", "class": klass, **hold} if hold else None


def waiter_class(job: Any, tier: str) -> str:
    """C-26.9: turns and detached jobs keep separate C-6.9 queues, so a waiting
    turn never holds a detached job back and a detached job never holds a turn."""
    return f"{tier}#turn" if _row(job).get("kind") == "turn" else tier


def demand_models(policy: Mapping[str, Any], job: Any) -> frozenset[str] | None:
    """The models a job could run on, exactly as `evaluate` builds its chain (C-11.2).

    A pin is that one model; a task is its chain from the job's tier upward. None
    means "cannot tell" (a lane pin with no model), which admission treats as
    competing with everything.
    """
    job = _row(job)
    try:
        if job.get("pinned_model"):
            return frozenset({resolve_model(policy, job["pinned_model"], note=False)})
        task = job.get("task")
        if task in policy["chains"]:
            tiers = policy["tiers"]
            default = "standard" if "standard" in tiers else tiers[0]
            return frozenset(flatten_chain(policy["chains"][task], tiers.index(job.get("tier") or default)))
    except (PolicyError, ValueError, KeyError):
        pass
    return None


def demand_lanes(lanes: Iterable[Any], job: Any,
                 policy: Mapping[str, Any] | None = None) -> frozenset[str] | None:
    """The lanes a job could run on: its pin, resolved to one lane id (C-11.2).

    Resolved as `evaluate` resolves it when `policy` is given (the job's
    provider narrows the name). None means any lane, or a pin that cannot be
    resolved here, which admission treats as competing with everything.
    """
    pin = _row(job).get("pinned_lane")
    if not pin:
        return None
    try:
        found = resolve_lane(lanes, pin, pin_provider(policy, job) if policy else None,
                             follow=not _row(job).get("unmeasured_reserve_reason"))
    except ValueError:
        return None
    return frozenset({found["lane_id"]}) if found else None


def competes(models: frozenset[str] | None, other: frozenset[str] | None,
             lanes: frozenset[str] | None = None, other_lanes: frozenset[str] | None = None) -> bool:
    """C-6.9: two jobs compete when some model could serve both AND some lane could
    serve both; either side unknown counts as overlap. Two jobs pinned to
    different lanes never compete: neither can take a slot the other waits for."""
    if models is not None and other is not None and not models & other:
        return False
    return lanes is None or other_lanes is None or bool(lanes & other_lanes)


def probe_turn(line: Iterable[tuple[str, str, frozenset[str] | None]], lane_id: str, model: str) -> str | None:
    """C-6.9: the job whose turn it is to carry the probe of `model` on `lane_id`,
    or None when it is the asker's.

    `line` is the jobs of this pass that wait on an admission probe (C-11.4), in
    the pass's order and so all ahead of the asker: each with the model its probe
    is of and the lanes it could run on (`demand_lanes`; None is any lane). A
    probe's prompt is fixed and it runs on the lane's credential in a private
    directory, so its answer says nothing about the job that carried it, and the
    first job in line that could use this one carries it. A job waiting on a
    probe of another model, or pinned to another lane, has no use for this probe
    and holds nobody."""
    return next((job_id for job_id, wanted, lanes in line
                 if wanted == model and (lanes is None or lane_id in lanes)), None)


def probe_reach(policy: Mapping[str, Any], roster: Iterable[Mapping[str, Any]], model: str,
                lanes: frozenset[str] | None, exclusions: Iterable[str]) -> frozenset[str]:
    """C-6.9, C-11.4: the lanes whose probe of `model` a job could use: the lanes of
    the model's provider in `roster` (as `Daemon._pin_roster` names them), only its
    pin's when `lanes` (`demand_lanes`) names one, and never one its exclusions name,
    by any of the names `evaluate` excludes by (`_identities`). A job waiting on a
    probe holds the turn at exactly these lanes' probes (`probe_turn`)."""
    provider = ((policy.get("models") or {}).get(model) or {}).get("provider")
    excluded = {str(name) for name in exclusions}
    return frozenset(str(lane["lane_id"]) for lane in roster
                     if lane.get("provider") == provider and (lanes is None or lane["lane_id"] in lanes)
                     and not _identities(lane) & excluded)


def left_out_only(decision: Decision | Mapping[str, Any], lanes: Iterable[str]) -> bool:
    """C-11.4: whether some lane of `lanes`, left out of an evaluation by the job's
    probe choice, was refused for that alone (`excluded` its only reason), so an
    evaluation without it could have chosen that lane."""
    left = set(lanes)
    return any(str(row.get("lane_id")) in left and set(row.get("reasons") or [row.get("reason")]) == {"excluded"}
               for evaluation in _row(decision).get("evaluations", ())
               for row in evaluation.get("rejections", ()))


def promoted_past(decision: Decision | Mapping[str, Any], model: str) -> bool:
    """C-11.4: whether `decision` walked past `model` to a later model of its chain
    (it judged `model` and chose another). A model earlier in the chain is no
    promotion: the walk stopped before it reached `model`."""
    value = _row(decision)
    return value.get("chosen_model") != model and any(
        row.get("model") == model for row in value.get("evaluations", ()))


def probe_lanes_taken(line: Iterable[tuple[str, str, frozenset[str] | None]],
                      model: str) -> frozenset[str] | None:
    """C-6.9: the lanes whose probe of `model` is the turn of a job in `line`
    (`probe_turn` names one for exactly these lanes), or None when it is every
    lane's: a job ahead waits on a probe of `model` and could run on any lane."""
    taken: set[str] = set()
    for _, wanted, lanes in line:
        if wanted != model:
            continue
        if lanes is None:
            return None
        taken |= lanes
    return frozenset(taken)


def _parent_blocks(policy: Mapping[str, Any], view: Mapping[str, Any], job: dict[str, Any]) -> list[str]:
    """All descendants of every ancestor share that ancestor's concurrency cap,
    `max_active_attempts_per_parent`, which is none unless the policy sets one
    (C-6.4; until 2026-09-27 it was a hidden 1)."""
    limit = cap(policy.get("caps"), "max_active_attempts_per_parent")
    if limit is None:
        return []
    jobs = {_row(item)["job_id"]: _row(item) for item in view.get("jobs", ())}
    if job.get("job_id"):
        jobs[job["job_id"]] = job

    def ancestors(item: Mapping[str, Any]) -> set[str]:
        seen: set[str] = set()
        parent = item.get("parent_job_id")
        while parent and parent not in seen:
            seen.add(parent)
            parent = jobs.get(parent, {}).get("parent_job_id")
        return seen

    parents = ancestors(job)
    if not parents:
        return []
    counts = dict.fromkeys(parents, 0)
    for item in view.get("attempts", ()):
        attempt = _row(item)
        if attempt.get("state") in ACTIVE_ATTEMPTS:
            for parent in ancestors(jobs.get(attempt.get("job_id"), {})) & parents:
                counts[parent] += 1
    return [f"parent:{parent}" for parent in sorted(parents) if counts[parent] >= limit]


def _future_closure(closure: Mapping[str, Any], now: datetime) -> bool:
    return not closure.get("released_at") and _time(closure["until_at"]) > now


def _higher_model_scopes(policy: Mapping[str, Any], short: str) -> set[str]:
    """C-23.37: stronger models come from policy, never provider name guesses.

    Explicit priorities compare models across separate task chains (Fable's
    writing chain and the general-work chain). Older policies still express
    ordering within their upward-only chains.
    """
    model = policy["models"][short]
    higher = set()
    for entries in policy["chains"].values():
        for index in range(len(entries)):
            within = flatten_chain(entries[index:index + 1])
            if short in within:
                # Slice after the first occurrence before deduping. A model
                # named both before and after `short` still strands its lane
                # under older string policies (C-23.37).
                higher.update(flatten_chain([within[within.index(short) + 1:], *entries[index + 1:]]))
                break
    priority = model.get("priority")
    if priority is not None:
        higher.update(name for name, other in policy["models"].items()
                      if other.get("priority", -1) > priority)
    return {policy["models"][name]["id"] for name in higher if name != short
            and policy["models"][name]["provider"] == model["provider"]}


def _earliest_reset(evaluations: Iterable[Mapping[str, Any]], now: datetime) -> str | None:
    clocks: list[datetime] = []
    for evaluation in evaluations:
        for key, clock in (("closures", "until_at"), ("readings", "resets_at")):
            for evidence in evaluation.get(key, ()):
                if not evidence.get(clock):
                    continue  # Admission observations have no quota/reset clock.
                try:
                    instant = _time(evidence[clock])
                    if instant > now:
                        clocks.append(instant)
                except (KeyError, TypeError, ValueError):
                    continue
    return _iso(min(clocks)) if clocks else None


def _unmeasured_reserve_reason(job: Mapping[str, Any]) -> str | None:
    """Validate the explicit exception independently of socket input validation."""
    reason = job.get("unmeasured_reserve_reason")
    if reason is None:
        return None
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise RouteError("unmeasured_reserve_reason: provide a nonblank reason of at most 2000 characters")
    if any(not isinstance(job.get(key), str) or not job[key].strip()
           for key in ("pinned_lane", "pinned_model")):
        raise RouteError("unmeasured_reserve_reason: explicit pinned_lane and pinned_model are required")
    return reason.strip()


#: C-6.3: every field of a lane row that `evaluate` reads (`prepare` and
#: `judge_lane`): its identities for pins and exclusions, its binding, and what
#: can reject it. A lane whose values of these, readings, closures, attempts in
#: flight and slot block are what they were is judged as it was.
LANE_FACTS = ("lane_id", "provider", "account_key", "credential_ref", "credential_kind", "home", "owner",
              "enabled", "desktop", "desktop_in_use", "identity_status", "label", "email")


def prepare(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> dict[str, Any]:
    """C-11.2, C-26.9: what `evaluate` settles before it looks at any lane.

    The job's chain and the lane its pin names, the clock, the caps, the job's
    exclusions, the attempts in flight in the job's pool, and the capacity
    blocks (the fleet's or a parent's cap) that refuse every lane alike. It
    reads the view's `now`, `lanes`, `in_flight` or `in_flight_turns`,
    `reserved_probes`, `attempts` and `jobs`, and no reading or closure, so
    C-6.3's check inside the reservation builds it from a few rows."""
    job = _row(job)
    authorization_reason = _unmeasured_reserve_reason(job)
    now = _time(view["now"]) if view.get("now") else datetime.now(timezone.utc)
    lanes = sorted((_row(lane) for lane in view.get("lanes", ())), key=lambda lane: lane["lane_id"])
    caps = {**DEFAULT_CAPS, "reading_ttl_s": 120, **policy.get("caps", {})}
    floor = policy.get("headroom_floor", HEADROOM_FLOOR)
    excluded = job.get("exclusions") or ()
    excluded = set(json.loads(excluded) if isinstance(excluded, str) else excluded)
    pin = job.get("pinned_lane")
    task, tier = job.get("task"), job.get("tier")
    # An unknown task, tier, or model is the policy's to fix, not the job's: submit
    # checked each against the policy it ran under (C-6.12).
    if task is not None and task not in policy["chains"]:
        raise ValueError(f"task: unknown task {task!r}")
    if tier is not None and tier not in policy["tiers"]:
        raise ValueError(f"tier: unknown tier {tier!r}")
    if job.get("pinned_model"):
        chain = [resolve_model(policy, job["pinned_model"])]
    elif task:
        default = "standard" if "standard" in policy["tiers"] else policy["tiers"][0]
        chain = flatten_chain(policy["chains"][task], policy["tiers"].index(tier or default))
    else:
        chain = []
    if chain and job_mcp_servers(job):
        # C-12.9: only a Claude launch starts the MCP servers the job named. A
        # policy edit since submit can leave none; that waits, as C-6.12 says.
        chain = mcp_chain(policy, chain, job)
        if not chain:
            raise RouteError("mcp_servers: this job names MCP servers, which only a Claude launch "
                             "starts, and its chain has no Claude model", policy_dependent=True)
    # C-11.2: a pinned job evaluates one model, the first of its chain, so a lane
    # of any other provider could never take it (`pin_provider` says the same).
    selected = (resolve_lane(lanes, pin, policy["models"][chain[0]]["provider"] if chain else None,
                             follow=not authorization_reason) if pin else None)
    if authorization_reason and selected and pin != selected["lane_id"]:
        raise RouteError("unmeasured_reserve_reason: pinned_lane must be the canonical lane id")
    if not chain and selected:
        if job_mcp_servers(job) and selected["provider"] != MCP_PROVIDER:
            raise RouteError(f"mcp_servers: this job names MCP servers, which only a Claude launch starts, "
                             f"and lane {pin!r} is a {selected['provider']} lane")
        model_name = next((name for name, model in policy["models"].items()
                           if model["provider"] == selected["provider"]), None)
        if model_name is None:
            raise PolicyError(policy.get("_policy_path", "policy.json"), "pinned_lane",
                              f"lane {pin!r} has provider {selected['provider']!r} with no models in policy")
        chain = [model_name]
    elif not chain:
        chain = (mcp_chain(policy, policy["models"], job) or [next(iter(policy["models"]))])[:1]
    if pin:
        chain = chain[:1]
        if selected and policy["models"][chain[0]]["provider"] != selected["provider"]:
            raise RouteError("pinned_lane and pinned_model/task: different providers", policy_dependent=True)
    # C-26.9: a conversation turn has its own capacity, counted apart from
    # detached jobs: `conversations.max_active_turns` across the fleet and
    # `conversations.turn_slots_per_lane` per lane, each no cap unless the policy
    # sets one. Neither kind waits for the other's slots; an attended turn never
    # waits behind background work.
    is_turn = _row(job).get("kind") == "turn"
    conversation_caps = policy.get("conversations") or {}
    key = "in_flight_turns" if is_turn else "in_flight"
    in_flight = dict(view.get(key, {}))
    if key not in view:
        turn_jobs = {_row(item).get("job_id") for item in view.get("jobs", ()) if _row(item).get("kind") == "turn"}
        for item in view.get("attempts", ()):
            attempt = _row(item)
            if attempt.get("state") in ACTIVE_ATTEMPTS and (attempt.get("job_id") in turn_jobs) == is_turn:
                identity = attempt["lane_id"]
                in_flight[identity] = in_flight.get(identity, 0) + 1
    capacity_blocks = _parent_blocks(policy, view, job)
    # C-6.4: each fleet cap is none unless the policy sets one.
    fleet_cap = turn_cap(conversation_caps, "max_active_turns") if is_turn else cap(caps, "max_active_attempts")
    if fleet_cap is not None and sum(in_flight.values()) + (0 if is_turn else view.get("reserved_probes", 0)) >= fleet_cap:
        capacity_blocks.append("fleet")
    return {"policy": policy, "job": job, "authorization_reason": authorization_reason, "now": now,
            "lanes": lanes, "caps": caps, "floor": floor, "excluded": excluded, "pin": pin,
            "selected": selected, "chain": chain, "is_turn": is_turn, "conversation_caps": conversation_caps,
            "in_flight": in_flight, "capacity_blocks": capacity_blocks, "higher": {},
            "lane_spread": admission_settings(policy)["lane_spread"]}


def model_lanes(setup: Mapping[str, Any], short: str) -> list[dict[str, Any]]:
    """The lanes `evaluate` looks at for one model: its provider's, or the pinned lane."""
    model, pin, selected = setup["policy"]["models"][short], setup["pin"], setup["selected"]
    return [lane for lane in setup["lanes"] if lane["provider"] == model["provider"]
            and (not pin or selected and lane["lane_id"] == selected["lane_id"])]


def ranking_usage(readings: Iterable[Mapping[str, Any]], *, now: datetime,
                  reading_ttl_s: int, admission: Mapping[str, Any]) -> dict[str, Any]:
    """C-11.3: observed usage for ranking, never a synthetic renewed reading.

    The caller supplies only account and requested-model scopes. The binding
    weekly window has the least headroom, and supplies BOTH headroom and reset.
    Equal headrooms bind the earliest known reset, then scope, deterministically.
    A missing reset sorts after known resets in the same reserve class. If any
    latest applicable provider window read within the TTL has already reset,
    rank the lane as unmeasured until a reading arrives or that evidence ages
    out. A stopped window cannot demote the lane forever. Do not assume zero
    utilization. This uncertainty changes ranking only, never probes or pick.
    Admission's existing floors, slots and model reserve are judged separately.
    """
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in readings:
        if row.get("label") not in ("provider", "stale-provider") or row.get("utilization") is None:
            continue
        key = (row["scope"], row["window"])
        previous = latest.get(key)
        if previous is None or _time(row["observed_at"]) > _time(previous["observed_at"]):
            latest[key] = row
    # A window already expired at its own observation is unusable as soon as
    # that observation enters the TTL guard. Future observations do not change
    # present ranking; their observation time is a horizon (C-6.3).
    recent = [row for row in latest.values()
              if 0 <= (now - _time(row["observed_at"])).total_seconds() <= reading_ttl_s]
    renewed = any(row.get("resets_at") and _time(row["resets_at"]) <= max(now, _time(row["observed_at"]))
                  for row in recent)
    fresh = [] if renewed else [row for row in latest.values()
        if fresh_provider(row, now=now, reading_ttl_s=reading_ttl_s)]
    weekly = min((row for row in fresh if row["window"] == "seven_day"),
                 key=lambda row: (1 - row["utilization"],
                                  _iso(_time(row["resets_at"])) if row.get("resets_at") else "9999",
                                  row["scope"]), default=None)
    weekly_headroom = 1 - weekly["utilization"] if weekly else None
    five_hour_headroom = min((1 - row["utilization"] for row in fresh
                             if row["window"] == "five_hour"), default=None)
    # Compare reported utilization with the complement of the reserve. Computing
    # (1 - .9) < .10 would put exactly 10% remaining in the low class because
    # binary floats represent that subtraction as .09999999999999998.
    weekly_low = weekly is not None and weekly["utilization"] > 1 - admission["weekly_reserve"]
    five_hour_low = any(row["utilization"] > 1 - admission["five_hour_reserve"]
                        for row in fresh if row["window"] == "five_hour")
    reserve_class = ("weekly+five-hour" if weekly_low and five_hour_low else
                     "weekly" if weekly_low else "five-hour" if five_hour_low else
                     "clear" if fresh else "unmeasured")
    observed = min((_time(row["observed_at"]) for row in (fresh or recent or latest.values())), default=None)
    return {"measured": bool(fresh), "weekly_headroom": weekly_headroom,
            "five_hour_headroom": five_hour_headroom,
            "seven_day_reset": _iso(_time(weekly["resets_at"])) if weekly and weekly.get("resets_at") else None,
            "weekly_scope": weekly["scope"] if weekly else None,
            "weekly_reserve": weekly_low, "five_hour_reserve": five_hour_low,
            "reserve_class": reserve_class, "reading_observed_at": _iso(observed) if observed else None,
            "reading_renewed": renewed}


def ranking_reading_age(detail: Mapping[str, Any], now: str | datetime) -> float | None:
    """C-11.5: derive explanatory age without putting a ticking value in judgement.

    Older decision records carried the age directly; keep their explanations.
    """
    observed = detail.get("reading_observed_at")
    return (_time(now) - _time(observed)).total_seconds() if observed else detail.get("reading_age_s")


def judge_lane(setup: Mapping[str, Any], short: str, lane: Mapping[str, Any],
               readings: Iterable[Mapping[str, Any]], closures: Iterable[Mapping[str, Any]], *,
               in_flight: int, unavailable: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """C-11.3–C-11.7, C-26.2, C-26.9: one lane for one model of the chain.

    The reasons the lane is refused (none: it is a candidate) and the detail
    its ranking reads. `readings` and `closures` are the view's rows of this
    lane (every scope and window), in the view's order; `in_flight` is the
    lane's attempts in the job's pool; `unavailable` is the view's
    `unavailable_lanes`. Nothing else about other lanes is read: the fleet and
    parent caps come in `setup` as capacity blocks."""
    policy, job, now, caps = setup["policy"], setup["job"], setup["now"], setup["caps"]
    model = policy["models"][short]
    higher_scopes = setup["higher"].get(short)
    if higher_scopes is None:
        higher_scopes = setup["higher"][short] = _higher_model_scopes(policy, short)
    readings, closures = list(readings), list(closures)
    identity = lane["lane_id"]
    reasons = []
    lane_readings = [row for row in readings if row["scope"] in ("account", model["id"])]
    measured_readings = [row for row in lane_readings
                         if fresh_provider(row, now=now, reading_ttl_s=caps["reading_ttl_s"])]
    headroom = min((1 - row["utilization"] for row in measured_readings), default=None)
    ranking = ranking_usage(lane_readings, now=now, reading_ttl_s=caps["reading_ttl_s"],
                            admission=admission_settings(policy))
    # C-11.3's uncertainty orders candidates only. C-11.4 probes and C-11.5
    # pick keep the admission freshness predicate they used before this rule.
    detail = {**ranking, "ranking_measured": ranking["measured"], "measured": bool(measured_readings),
              "headroom": headroom, "in_flight": in_flight}
    detail["status"] = "eligible" if detail["measured"] else "eligible but unmeasured"
    if model["provider"] == "claude":
        detail["stranded_scopes"] = sorted({row["scope"] for row in closures
            if row["scope"] in higher_scopes and _future_closure(row, now)})
    if lane.get("desktop"):
        # C-10.3, C-11.3: a desktop lane that is a candidate sorts after every other.
        detail["desktop"] = True
    if _identities(lane) & setup["excluded"]:
        reasons.append("excluded")
    if desktop_excluded(lane) and not job.get("allow_desktop"):
        # C-10.3: only while Claude Code is using the desktop login (or that is unknown).
        reasons.append("desktop")
    if (job.get("kind") == "turn" and model["provider"] == "claude"
            and lane.get("credential_kind") == "home"):
        # C-26.2: a home lane has its own config directory; the
        # conversation's transcript is not there.
        reasons.append("config-dir")
    if lane.get("owner") != "v2":
        reasons.append("owner-v1")
    if not lane.get("enabled", True):
        reasons.append("disabled")
    if identity_blocked(lane):
        # C-10.6: the profile endpoint said this credential holds another
        # account. Its usage is not this lane's, so neither is its capacity.
        reasons.append("identity-mismatch")
    reasons.extend(f"closed:{row['scope']}:{row['until_at']}" for row in closures
                   if row["scope"] in ("account", model["id"]) and _future_closure(row, now))
    lane_measured = any(fresh_provider(row, now=now, reading_ttl_s=caps["reading_ttl_s"]) for row in readings)
    # C-6.4, C-26.9: None when the policy sets no per-lane cap for the job's pool.
    slot_cap = (turn_cap(setup["conversation_caps"], "turn_slots_per_lane") if setup["is_turn"] else
                lane_slot_cap(caps, lane_measured))
    if identity in unavailable and not (setup["is_turn"] and pilot_block(unavailable[identity])):
        # C-6.14: a pilot's block holds detached attempts only; a turn is never held for one.
        detail["slot_block"] = unavailable[identity]
    if setup["capacity_blocks"] or (slot_cap is not None and in_flight >= slot_cap) or detail.get("slot_block"):
        reasons.append("no-slot")
    if any(row["utilization"] >= 1 - setup["floor"] for row in measured_readings):
        reasons.append("below-floor")
    # C-11.7: a model that is not reserved may only spend a lane's slack above
    # what the reserved model could still use of the shared weekly window.
    for reserved in (policy.get("reserve") or {}).get("models", ()):
        r_model = policy["models"].get(reserved)
        if not r_model or r_model["provider"] != model["provider"] or r_model["id"] == model["id"]:
            continue
        verdict = reserve_verdict(identity, r_model["id"], readings, now=now,
                                  reading_ttl_s=caps["reading_ttl_s"],
                                  reserve=policy.get("reserve") or {}, closures=closures)
        detail["reserve"] = {"model": reserved, **verdict}
        if verdict["state"] == "unmeasured" and setup["authorization_reason"]:
            # This authorizes uncertainty on the explicit pinned pair;
            # it supplies no usage evidence and releases no other guard.
            detail["reserve"]["authorization"] = {
                "reason": setup["authorization_reason"], "lane_id": identity, "model_id": model["id"]}
        elif verdict["state"] != "slack":
            reasons.append(f"reserve:{reserved}:{verdict['state']}")
        elif job.get("kind") == "turn" and verdict.get("requires_probe"):
            # C-26.9: a turn never waits on a probe; a lane that needs one
            # is not a candidate for it.
            reasons.append(f"reserve:{reserved}:probe-required")
    return reasons, detail


#: C-11.8: what `judge_lane` refuses a lane for that no wait for capacity ends.
#: The job's own exclusions, a turn's config directory and a v1 owner never
#: change on their own; the desktop login while Claude Code uses it, a disabled
#: lane and a credential that proved to hold another account (C-10.6) change
#: only when a person acts, and so does a latched credential `capacity.credential_gone`
#: holds of: revoked or missing, or a Codex token whose one heal for its login ran
#: and left it expired (C-23.47). Any other expired token may still heal by
#: itself, so it is not standing. A slot, a reading, the floor and the reserve
#: are capacity, which comes back by itself. A closure is a hold only when it ends
#: more than `admission.pin_hold_far_s` out; before that it is a wait.
STANDING_REFUSALS = ("excluded", "desktop", "config-dir", "owner-v1", "disabled", "identity-mismatch")

#: C-6.12: what evaluating a route may raise; the job's, never the caller's.
_ROUTE_RAISES = (ValueError, KeyError, TypeError, AttributeError, IndexError)


def standing_refusals(reasons: Iterable[str], detail: Mapping[str, Any], lane: Mapping[str, Any] | None,
                      closures: Iterable[Mapping[str, Any]], now: datetime, far_s: float) -> list[str]:
    """C-11.8: which of one lane's refusals no wait for capacity ends.

    `reasons` and `detail` are what `judge_lane` found for the lane (a rejection
    row carries both), `lane` its row, and `closures` the rows it read: a
    `closed:<scope>:<until>` reason counts when the closure it names ends more
    than `far_s` seconds after `now`, and a latched credential when only a person
    brings it back (`capacity.credential_gone` of the row; with no row, never)."""
    rows = {f"closed:{row['scope']}:{row['until_at']}": row for row in closures}
    found = [reason for reason in reasons if reason in STANDING_REFUSALS or reason in rows
             and (_time(rows[reason]["until_at"]) - now).total_seconds() > far_s]
    if detail.get("slot_block") == "credential-latched" and lane is not None and credential_gone(lane):
        found.append("credential-latched")
    return found


def pin_unadmittable(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> dict[str, Any] | None:
    """C-11.8: why the lane a job is pinned to can never admit it, or None.

    The pin is resolved and its lane judged exactly as `evaluate` resolves and
    judges it (`prepare`, `judge_lane`), and only the standing refusals are kept
    (`standing_refusals`); a pin that names no lane is `unknown`. So whenever
    this names a reason, `evaluate` over any view with the same lane rows,
    closures and latched credentials chooses no lane for the job, whatever the
    readings, the attempts in flight, the probes and the caps.

    None for a job with no lane pin, for a lane refused for capacity only, and
    for a job whose route cannot be evaluated at all (C-6.12 settles that one).
    Reads the view's `now`, `lanes` (marked and merged as a view marks them),
    `closures` and `unavailable_lanes`, and nothing else."""
    job = _row(job)
    if not job.get("pinned_lane"):
        return None
    try:
        setup = prepare(policy, view, job)
        lane = setup["selected"]
        if lane is None:
            return {"lane_id": str(setup["pin"]), "reasons": ["unknown"], "closures": []}
        closures = [row for row in (_row(item) for item in view.get("closures", ()))
                    if row.get("lane_id") == lane["lane_id"]]
        reasons, detail = judge_lane(setup, setup["chain"][0], lane, (), closures, in_flight=0,
                                     unavailable=view.get("unavailable_lanes") or {})
        found = standing_refusals(reasons, detail, lane, closures, setup["now"],
                                  admission_settings(policy)["pin_hold_far_s"])
    except _ROUTE_RAISES:
        return None
    if not found:
        return None
    held = [{"scope": row["scope"], "until_at": row["until_at"], "reason": row.get("reason")}
            for row in closures if f"closed:{row['scope']}:{row['until_at']}" in found]
    return {"lane_id": lane["lane_id"], "reasons": found, "closures": held,
            **({"probe_status": lane.get("probe_status")} if "credential-latched" in found else {})}


def unadmittable(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any,
                 memo: dict | None = None) -> list[str] | None:
    """C-11.8, C-6.9: the standing refusals that keep every lane from a job now,
    whatever capacity does, or None when some lane could take it once capacity
    comes back.

    A lane-pinned job is its pinned lane's (`pin_unadmittable`). Any other job
    must be refused for a standing reason by every lane of every model its chain
    could walk (`prepare`, `model_lanes`, `judge_lane`), as `refused_for_good`
    reads the same thing off a decision; a model with no lane is `no-lanes`.
    `view` is what `pin_unadmittable` reads. `memo`, kept for one pass, answers
    again for a model and the few job fields a standing refusal turns on
    (exclusions, `allow_desktop`, a turn): a queue of like jobs costs one look
    per model. None too for a job whose route cannot be evaluated (C-6.12)."""
    job = _row(job)
    if job.get("pinned_lane"):
        found = pin_unadmittable(policy, view, job)
        return found["reasons"] if found else None
    try:
        setup = prepare(policy, view, job)
        far = admission_settings(policy)["pin_hold_far_s"]
        closures: dict[str, list[dict[str, Any]]] = {}
        for item in view.get("closures", ()):
            row = _row(item)
            closures.setdefault(row.get("lane_id"), []).append(row)
        found: list[str] = []
        for short in setup["chain"]:
            key = (short, frozenset(setup["excluded"]), bool(job.get("allow_desktop")), setup["is_turn"])
            if memo is not None and key in memo:
                refusals = memo[key]
            else:
                refusals = []
                lanes = model_lanes(setup, short)
                if not lanes:
                    refusals.append("no-lanes")
                for lane in lanes:
                    reasons, detail = judge_lane(setup, short, lane, (), closures.get(lane["lane_id"], ()),
                                                 in_flight=0, unavailable=view.get("unavailable_lanes") or {})
                    standing = standing_refusals(reasons, detail, lane, closures.get(lane["lane_id"], ()),
                                                 setup["now"], far)
                    if not standing:
                        refusals = None
                        break
                    refusals.extend(standing)
                if memo is not None:
                    memo[key] = refusals
            if refusals is None:
                return None
            found.extend(refusals)
    except _ROUTE_RAISES:
        return None
    return list(dict.fromkeys(found))


def refused_for_good(policy: Mapping[str, Any], decision: Decision | Mapping[str, Any] | None,
                     job: Any, lanes: Iterable[Any] = ()) -> list[str] | None:
    """C-11.8, C-6.9: the standing refusals that keep every lane a decision walked
    from its job, or None when some lane could take the job once capacity comes
    back (or one was chosen).

    Every lane of every model walked must be refused for a standing reason
    (`standing_refusals`). A model with no lane to walk is `unknown` for a pinned
    job (its pin names no lane) and `no-lanes` otherwise (no lane of that
    provider is enrolled). A job so refused holds no other job back (C-6.9).
    `lanes` are the rows a latched credential is judged on (`capacity.credential_gone`):
    a decision carries no lane's credential, and with no row a latch is never standing."""
    value = _row(decision) if decision is not None else {}
    evaluations = value.get("evaluations") or ()
    if not evaluations or value.get("chosen_lane"):
        return None
    far = admission_settings(policy)["pin_hold_far_s"]
    pinned = bool(_row(job).get("pinned_lane"))
    rows_by_id = {row["lane_id"]: row for row in (_row(item) for item in lanes)}
    found: list[str] = []
    try:
        for evaluation in evaluations:
            if evaluation.get("candidates"):
                return None
            rows = evaluation.get("rejections") or ()
            if not rows:
                found.append("unknown" if pinned else "no-lanes")
                continue
            now = _time(evaluation["evaluated_at"])
            for row in rows:
                standing = standing_refusals(row.get("reasons") or [row.get("reason")], row,
                                             rows_by_id.get(row.get("lane_id")), evaluation.get("closures") or (),
                                             now, far)
                if not standing:
                    return None
                found.extend(standing)
    except _ROUTE_RAISES:
        return None
    return list(dict.fromkeys(found))


def rank_key(setup: Mapping[str, Any], short: str, identity: str, detail: Mapping[str, Any]) -> tuple:
    """C-11.3, C-11.7, C-26.2, C-10.3: where a candidate lane stands; the least is chosen.

    The desktop login's lane sorts after every other (C-10.3), then a turn's
    affinity lane first (C-26.2), then the load band: attempts in flight in the
    job's pool divided by `admission.lane_spread`, so with no per-lane cap lanes
    fill evenly, that many at a time, instead of one lane taking every job
    (C-11.3, 2026-09-27). Claude's stranded term retains its position before
    measured status. Then both providers prefer measured lanes, lanes above
    the weekly reserve, lanes above the five-hour reserve, the binding weekly
    reset ascending (unknown last), weekly headroom descending, in-flight
    ascending and lane id. The reserves are preferences, never exclusions.
    C-11.7 still guards reserved model capacity but its slack is no longer a
    comparator. Every key ends with lane id, so no two candidates tie."""
    job, provider = setup["job"], setup["policy"]["models"][short]["provider"]
    spread = setup.get("lane_spread")
    band = detail["in_flight"] // spread if spread else 0
    prefix = (band, not bool(detail.get("stranded_scopes"))) if provider == "claude" else (band,)
    base = (*prefix, not detail.get("ranking_measured", detail["measured"]), detail["weekly_reserve"], detail["five_hour_reserve"],
            detail["seven_day_reset"] or "9999", -(detail["weekly_headroom"] or 0),
            detail["in_flight"], identity)
    desktop = bool(detail.get("desktop"))
    affinity = job.get("affinity_lane") if job.get("kind") == "turn" else None
    if affinity is not None:
        # C-26.2: a conversation keeps the account that served its last
        # turn while that account stays a candidate (prompt cache).
        return (desktop, identity != affinity, *base)
    return (desktop, *base)


#: C-6.19: the cache lifetimes a Claude stream reports (`usage.cache_creation`),
#: in seconds. A run that wrote both keeps the shorter, which a wait cannot outlast.
CACHE_TTL_S = {"1h": 3600, "5m": 300, "mixed": 300}


def warm_reopen(reasons: Iterable[str], lane_readings: Iterable[Mapping[str, Any]],
                closures: Iterable[Mapping[str, Any]], *, now: datetime, floor: float,
                reading_ttl_s: int) -> str | None:
    """C-6.19: when a lane refused only by usage limits takes work again, or None.

    `reasons` are what `judge_lane` refused the lane for. Each must end at an
    instant the provider reported: a `closed:<scope>:<until>` whose closure row is
    a `provider-limit` with clock `reported`, or `below-floor`, which ends when
    every fresh window at or over the floor resets (its `resets_at`). Any other
    reason (an exclusion, an operator hold, auth, a guessed clock, a slot, the
    reserve, a window with no reset) has no known end, and the answer is None.
    The answer is the latest of those instants: the lane is closed until all end."""
    rows = {f"closed:{row['scope']}:{row['until_at']}": row for row in closures}
    ends: list[datetime] = []
    reasons = list(reasons)
    if not reasons:
        return None
    for reason in reasons:
        if reason in rows:
            row = rows[reason]
            if row.get("reason") != "provider-limit" or row.get("clock_source") != "reported":
                return None
            ends.append(_time(row["until_at"]))
        elif reason == "below-floor":
            over = [row for row in lane_readings
                    if fresh_provider(row, now=now, reading_ttl_s=reading_ttl_s)
                    and row["utilization"] >= 1 - floor]
            if not over or any(not row.get("resets_at") for row in over):
                return None
            ends.extend(_time(row["resets_at"]) for row in over)
        else:
            return None
    return _iso(max(ends))


def warm_verdict(*, reopens_at: str | datetime | None, now: str | datetime,
                 wait_since: str | datetime | None, warm_wait_s: float | None,
                 cache_until: str | datetime | None) -> dict[str, Any]:
    """C-6.19: wait for the lane whose prompt cache holds the session, or move.

    Pure and deterministic. `reopens_at` is `warm_reopen`'s answer for the warm
    lane, `wait_since` when this job began waiting for it (None: it has not),
    `warm_wait_s` the policy bound (None: the feature is off and the caller keeps
    its old rule), and `cache_until` the warm lane's last use of the session plus
    the cache lifetime its stream reported (None: not measured; only the bound
    applies). The deadline is the earlier of `wait_since + warm_wait_s` and
    `cache_until`: past the cache's life the warm lane loads cold too.

    Actions: `off`; `wait` (until `next_check_at`, never later than the deadline,
    so the job is looked at again before the bound passes); `move` with `why`
    (`no-known-reopen`, `cache-expires-first`, `past-bound`). Invariants (tests):
    a `wait` has `now < deadline <= wait_since + warm_wait_s` (no job waits past
    the bound); `next_check_at <= deadline`; the same inputs give the same verdict."""
    if warm_wait_s is None:
        return {"action": "off"}
    instant = _time(now)
    if reopens_at is None:
        return {"action": "move", "why": "no-known-reopen", "reopens_at": None}
    reopen = _time(reopens_at)
    since = _time(wait_since) if wait_since is not None else instant
    bound = since + timedelta(seconds=warm_wait_s)
    cache = _time(cache_until) if cache_until is not None else None
    deadline = min(bound, cache) if cache is not None else bound
    record = {"reopens_at": _iso(reopen), "deadline": _iso(deadline), "wait_since": _iso(since),
              "cache_until": _iso(cache) if cache is not None else None}
    if reopen <= deadline and instant < deadline:
        return {"action": "wait", "next_check_at": _iso(max(instant, min(reopen, deadline))), **record}
    why = "cache-expires-first" if cache is not None and reopen > cache and cache <= bound else "past-bound"
    return {"action": "move", "why": why, **record}


def cache_until(last_use: str | datetime | None, cache_ttl: str | None) -> str | None:
    """C-6.19: until when the warm lane's cache holds a session: its last use plus
    the lifetime the stream reported (`CACHE_TTL_S`); None when either is unknown."""
    if last_use is None or cache_ttl not in CACHE_TTL_S:
        return None
    return _iso(_time(last_use) + timedelta(seconds=CACHE_TTL_S[cache_ttl]))


def turn_route(decision: Decision | Mapping[str, Any], affinity_lane: str | None, *, floor: float,
               reading_ttl_s: int, last_use: str | None = None, cache_ttl: str | None = None,
               context_tokens: int | None = None) -> dict[str, Any] | None:
    """C-11.10, C-26.2: what the person is told when a turn leaves its warm lane.

    None when the turn has no affinity lane, no lane was chosen, or it runs on
    its affinity lane. Otherwise the move: `from` (the warm lane), `to`, the warm
    lane's `reasons` as the decision recorded them, `reopens_at` (`warm_reopen`;
    None when not refused only by limits, or not walked), `cache_until` (the
    previous turn's last use plus its measured cache lifetime; None unmeasured),
    `context_tokens` (the previous turn's last request, as measured; None
    unmeasured), and `cold`: True when the cache was still alive at the decision
    (staying would have read it), False when it had expired (the move cost
    nothing more), None when the lifetime is unknown. A turn never waits for
    this (C-26.2): the record only says what happened."""
    value = _row(decision)
    chosen = value.get("chosen_lane")
    if not affinity_lane or not chosen or chosen == affinity_lane:
        return None
    evaluation = next((row for row in value.get("evaluations", ()) if row.get("model") == value.get("chosen_model")),
                      None) or {}
    rejection = next((row for row in evaluation.get("rejections", ()) if row.get("lane_id") == affinity_lane), None)
    now = _time(evaluation.get("evaluated_at")) if evaluation.get("evaluated_at") else None
    reasons = list((rejection or {}).get("reasons") or ([rejection["reason"]] if rejection else []))
    reopens = None
    if rejection and now is not None:
        reopens = warm_reopen(reasons, [row for row in evaluation.get("capacity_readings", ())
                                        if row.get("lane_id") == affinity_lane
                                        and row.get("scope") in ("account", evaluation.get("model_id"))],
                              [row for row in evaluation.get("closures", ()) if row.get("lane_id") == affinity_lane],
                              now=now, floor=floor, reading_ttl_s=reading_ttl_s)
    until = cache_until(last_use, cache_ttl)
    cold = None if until is None or now is None else _time(until) > now
    return {"from": affinity_lane, "to": chosen, "reasons": reasons if rejection else ["not-walked"],
            "reopens_at": reopens, "cache_until": until, "context_tokens": context_tokens, "cold": cold,
            "decided_at": _iso(now) if now is not None else None}


def model_reason(setup: Mapping[str, Any], index: int, short: str, candidates: list[str],
                 details: Mapping[str, Mapping[str, Any]]) -> str:
    """C-11.5: what one model of the chain came to, in words (`candidates` ranked)."""
    if candidates:
        return f"{short}: chose {candidates[0]}; {details[candidates[0]]['status']}"
    suffix = "; promoted" if index + 1 < len(setup["chain"]) else ""
    reason = f"{short}: no candidate lanes after exclusions{suffix}"
    if setup["pin"] and setup["selected"] is None:
        reason += f"; pinned lane {setup['pin']!r} is unknown"
    return reason


def decision_reason(evaluations: Iterable[Mapping[str, Any]], chosen_lane: str | None, now: datetime) -> str:
    """C-11.5: a decision's reason: each model's, then, with no lane, the earliest reset."""
    evaluations = list(evaluations)
    messages = [row["reason"] for row in evaluations]
    if chosen_lane is None:
        messages.append("earliest reset: " + (_earliest_reset(evaluations, now) or "unknown"))
    return "; ".join(messages)


def evaluate(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> Decision:
    """C-11.2–C-11.6: walk upward, applying every rejection before comparison.

    `prepare` settles the chain, the pin and the capacity blocks; each lane of
    each model is judged alone (`judge_lane`) and the candidates ranked by
    `rank_key`, so a lane can be judged again without the rest (C-6.3)."""
    setup = prepare(policy, view, job)
    job, now, pin, selected = setup["job"], setup["now"], setup["pin"], setup["selected"]
    readings = [_row(item) for item in view.get("readings", ())]
    closures = [_row(item) for item in view.get("closures", ())]
    unavailable = view.get("unavailable_lanes", {})
    by_lane_readings: dict[str, list[dict[str, Any]]] = {}
    for row in readings:
        by_lane_readings.setdefault(row["lane_id"], []).append(row)
    by_lane_closures: dict[str, list[dict[str, Any]]] = {}
    for row in closures:
        by_lane_closures.setdefault(row["lane_id"], []).append(row)
    chain, capacity_blocks = setup["chain"], setup["capacity_blocks"]
    evaluations: list[dict[str, Any]] = []
    chosen_lane = chosen_model = None
    for index, short in enumerate(chain):
        model = policy["models"][short]
        higher_scopes = setup["higher"].get(short)
        if higher_scopes is None:
            higher_scopes = setup["higher"][short] = _higher_model_scopes(policy, short)
        lanes_here = model_lanes(setup, short)
        lane_ids = {lane["lane_id"] for lane in lanes_here}
        scoped_readings = [row for row in readings if row["lane_id"] in lane_ids
                           and row["scope"] in ("account", model["id"])]
        scoped_closures = [row for row in closures if row["lane_id"] in lane_ids
                          and row["scope"] in ("account", model["id"]) and _future_closure(row, now)]
        candidates, rejections, details = [], [], {}
        for lane in lanes_here:
            identity = lane["lane_id"]
            reasons, detail = judge_lane(setup, short, lane, by_lane_readings.get(identity, ()),
                                         by_lane_closures.get(identity, ()),
                                         in_flight=setup["in_flight"].get(identity, 0), unavailable=unavailable)
            if reasons:
                rejections.append({"lane_id": identity, "reason": reasons[0], "reasons": reasons, **detail})
            else:
                candidates.append(identity)
                details[identity] = detail
        candidates.sort(key=lambda identity: rank_key(setup, short, identity, details[identity]))
        if candidates:
            chosen_lane, chosen_model = candidates[0], short
        reason = model_reason(setup, index, short, candidates, details)
        evaluations.append({"model": short, "model_id": model["id"], "provider": model["provider"],
                            "candidates": candidates, "candidate_details": details,
                            "rejections": rejections, "rejected": rejections,
                            "readings": scoped_readings,
                            "capacity_readings": [row for row in readings if row["lane_id"] in lane_ids],
                            "closures": scoped_closures, "capacity_blocks": list(capacity_blocks),
                            "stranding_closures": [row for row in closures if row["lane_id"] in lane_ids
                                and row["scope"] in higher_scopes and _future_closure(row, now)],
                            "reason": reason, "evaluated_at": _iso(now)})
        if candidates:
            break
    digest = job.get("policy_hash") or policy.get("_policy_hash", "")
    return Decision(tuple(row["model"] for row in evaluations), tuple(evaluations),
                    chosen_lane, chosen_model, decision_reason(evaluations, chosen_lane, now), digest)


def reserve_verdict(identity: str, reserved_id: str, readings: Iterable[Mapping[str, Any]], *,
                    now: datetime, reading_ttl_s: int, reserve: Mapping[str, Any],
                    closures: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """C-11.7: how much of a lane's shared weekly window a non-reserved model may spend.

    Reads fresh `provider` readings of window `seven_day`: a complete OAuth
    usage snapshot, or shared and scoped windows from the same stream event.
    A reported, active model-only closure can instead establish that no reserve
    is usable until reset, but requires an admission probe for the requested model.
    slack = (1 - shared utilization)
    - cap_ratio * (1 - reserved utilization). States: `unmeasured` (no fresh
    usage read of the shared window), `reserved` (slack below `min_slack`),
    `slack` (may spend `slack`; a usage read that shows no reserved window makes
    the whole remainder slack).
    """
    ratio = float(reserve.get("cap_ratio", 1.0))
    min_slack = float(reserve.get("min_slack", 0.05))
    readings = list(readings)
    fresh = [row for row in readings if row["lane_id"] == identity and row.get("window") == "seven_day"
             and fresh_provider(row, now=now, reading_ttl_s=reading_ttl_s)]
    # A stream can measure reserve slack only when it actually carries both
    # weekly buckets in the same event. Its missing scoped bucket is unknown,
    # unlike absence in a validated complete OAuth usage snapshot.
    accounts = [row for row in fresh if row["scope"] == "account" and (
        row.get("source") == "oauth-usage" or
        row.get("source") == "rate_limit_event" and any(
            scoped["scope"] == reserved_id and scoped.get("source") == "rate_limit_event"
            and scoped["observed_at"] == row["observed_at"]
            and scoped.get("attempt_id") == row.get("attempt_id") for scoped in fresh))]
    account = max(accounts, key=lambda row: _time(row["observed_at"]), default=None)
    exhausted = [row for row in closures if row["lane_id"] == identity
                 and row["scope"] == reserved_id and row.get("reason") == "provider-limit"
                 and row.get("clock_source") == "reported" and _future_closure(row, now)]
    if exhausted and account is None:
        # A provider-reported model-only exhaustion protects no usable reserve
        # before its reset. This says nothing about the other model's capacity:
        # require its own supervised admission probe before dispatch.
        return {"state": "slack", "reserved_remaining": 0.0,
                "all_remaining": None,
                "requires_probe": True, "cap_ratio": ratio, "min_slack": min_slack,
                "note": "reserved model is closed by the provider until its reset",
                "closure_until": max(row["until_at"] for row in exhausted)}
    if account is None:
        return {"state": "unmeasured", "cap_ratio": ratio, "min_slack": min_slack}
    all_remaining = round(1 - float(account["utilization"]), 4)
    scoped_evidence = [row for row in readings if row["lane_id"] == identity
                       and row.get("window") == "seven_day" and row["scope"] == reserved_id
                       and _time(row["observed_at"]) >= _time(account["observed_at"])]
    scoped = next((row for row in scoped_evidence if row in fresh
                   and row.get("source") == account.get("source")
                   and (account.get("source") != "rate_limit_event" or (
                       row["observed_at"] == account["observed_at"]
                       and row.get("attempt_id") == account.get("attempt_id")))), None)
    if scoped is None and scoped_evidence:
        # Same-snapshot or newer uncertain evidence is not absence. An older
        # scoped row, however, is superseded by a newer complete usage payload.
        # The sensor rejects incomplete snapshots before publishing readings.
        return {"state": "unmeasured", "cap_ratio": ratio, "min_slack": min_slack,
                "note": "reserved window has no fresh usage reading"}
    if scoped is None:
        return {"state": "slack", "all_remaining": all_remaining, "reserved_remaining": None,
                "slack": all_remaining, "cap_ratio": ratio, "min_slack": min_slack,
                "note": "no reserved window on this account"}
    reserved_remaining = round(1 - float(scoped["utilization"]), 4)
    slack = round(all_remaining - ratio * reserved_remaining, 4)
    return {"state": "slack" if slack >= min_slack else "reserved", "all_remaining": all_remaining,
            "reserved_remaining": reserved_remaining, "slack": slack, "cap_ratio": ratio,
            "min_slack": min_slack}


def probe_required(decision: Decision, job: Any) -> bool:
    """C-11.4: probe the requested model before expensive unmeasured work.

    C-23.20 makes a revive stricter than expensive: it admits a lane only on a
    `provider` reading taken in the same pass, so a stored reading — even a
    fresh one inside `reading_ttl_s` — never qualifies the lane on its own. The
    2026-08-25 observation is the reason: three lanes the ledger called healthy
    were out of Fable, and a revive that lands on one burns the window with no
    reader. `_prepare_route` converges after one probe because the approved pair
    short-circuits the loop.
    """
    job = _row(job)
    authorization_reason = _unmeasured_reserve_reason(job)
    if not decision.chosen_lane:
        return False
    if authorization_reason:
        evaluation = next((row for row in decision.evaluations
                           if row["model"] == decision.chosen_model), None)
        if (decision.chosen_lane != job["pinned_lane"] or evaluation is None
                or job["pinned_model"] not in (decision.chosen_model, evaluation["model_id"])):
            raise RouteError("unmeasured_reserve_reason: the probe must use the authorized lane and model",
                             policy_dependent=True)
        # An admission observation or a newly measured window cannot remove
        # the promised same-model probe. The daemon's approved pair ends it.
        return True
    if job.get("kind") == "revive":
        return True
    if job.get("kind") == "turn":
        # C-26.9: `evaluate` already refused every lane a turn would need a probe for.
        return False
    evaluation = next(row for row in decision.evaluations if row["model"] == decision.chosen_model)
    if (evaluation["candidate_details"][decision.chosen_lane].get("reserve") or {}).get("requires_probe"):
        return True
    if not (job.get("sandbox") == "workspace-write" or job.get("tier") == "hard"):
        return False
    return not evaluation["candidate_details"][decision.chosen_lane]["measured"]


def exit_code(decision: Decision) -> Exit:
    """C-17.3: an evaluation without a candidate is NO_LANE, including pins."""
    return Exit.OK if decision.chosen_lane else Exit.NO_LANE


def capacity_recheck_delay(rechecks: int, ceiling_s: float = CAPACITY_RECHECK_CEILING_S) -> float:
    """C-6.10: seconds until the next look at a job whose verdict has held `rechecks` times."""
    # The exponent is bounded so a job that waits for days cannot overflow a float.
    return min(ceiling_s, CAPACITY_RECHECK_BASE_S * 2 ** min(max(rechecks, 0), 16))


def waiting_metadata(decision: Decision, now: str | datetime | None = None, *,
                     rechecks: int = 0, ceiling_s: float = CAPACITY_RECHECK_CEILING_S) -> dict[str, str]:
    """C-4.1; plan amendment 11: capacity waits always have a recheck clock.

    C-6.10: the clock backs off while the verdict repeats, and a known reset
    that falls sooner than the backed-off clock is still checked on time.
    """
    instant = _time(now) if now is not None else datetime.now(timezone.utc)
    next_check = instant + timedelta(seconds=capacity_recheck_delay(rechecks, ceiling_s))
    earliest = _earliest_reset(decision.evaluations, instant)
    if earliest:
        next_check = min(next_check, _time(earliest))
    return {"wait_reason": "capacity", "next_check_at": _iso(next_check)}


def verdict_signature(decision: Decision | Mapping[str, Any]) -> str:
    """C-6.10: what a decision concluded, without the evidence it consulted.

    Two evaluations with the same signature walked the same chain, chose the
    same lane and model, and rejected the same lanes for the same reasons.
    Readings, their utilization, and `evaluated_at` change every probe cycle
    without changing the verdict, so they are left out. So is `no-slot` on a
    lane that is rejected for another reason as well: a lane that is closed,
    reserved, or excluded is no nearer to taking the job when its slot empties.
    Slots fill and empty all day, and a probe's reservation counts toward the
    fleet cap, so each probe cycle (C-18.1) marks every lane `no-slot` for the
    second it runs. A lane whose only reason is `no-slot` keeps it: that job is
    waiting for a slot, and that is its verdict.
    """
    value = _row(decision)
    walked = []
    for evaluation in value.get("evaluations", ()):
        rejections = []
        for row in evaluation.get("rejections", ()):
            reasons = list(row.get("reasons") or [row.get("reason")])
            standing = [reason for reason in reasons if reason != "no-slot"]
            rejections.append((str(row.get("lane_id")), tuple(standing or reasons)))
        rejections.sort()
        candidates = sorted(row if isinstance(row, str) else str(row.get("lane_id"))
                            for row in evaluation.get("candidates", ()))
        walked.append([evaluation.get("model"), candidates, rejections])
    material = [list(value.get("chain", ())), value.get("chosen_lane"), value.get("chosen_model"),
                value.get("policy_hash"), walked]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def dominant_rejection(decision: Decision | Mapping[str, Any] | None) -> str:
    """C-6.11: what keeps a job out of every lane, as one short label.

    A lane rejected only for `no-slot` would take the job if it had room, so
    room is the cause: `fleet-full` when the fleet cap made it so, `parent-cap`
    for a parent's, else `no-slot`. When every lane has a standing reason the
    label is the commonest of those, and the cap is beside the point: a probe's
    reservation counts toward the fleet cap, so a job that no lane admits anyway
    would otherwise read `fleet-full` for the second each probe runs.
    """
    if decision is None:
        return "not-evaluated"
    value = _row(decision)
    blocks = [block for evaluation in value.get("evaluations", ())
              for block in evaluation.get("capacity_blocks", ())]
    counts: dict[str, int] = {}
    room_only = False
    for evaluation in value.get("evaluations", ()):
        for row in evaluation.get("rejections", ()):
            reasons = [str(reason) for reason in (row.get("reasons") or [row.get("reason") or "unknown"])]
            standing = [reason for reason in reasons if reason != "no-slot"]
            if not standing:
                room_only = True
                continue
            # A closure's reason carries its own expiry; the label groups them.
            label = ":".join(standing[0].split(":")[:2]) if standing[0].startswith("closed:") else standing[0]
            counts[label] = counts.get(label, 0) + 1
    if room_only:
        if "fleet" in blocks:
            return "fleet-full"
        if any(str(block).startswith("parent:") for block in blocks):
            return "parent-cap"
        slot_blocks = [row.get("slot_block") for evaluation in value.get("evaluations", ())
                       for row in evaluation.get("rejections", ())
                       if set(row.get("reasons") or [row.get("reason")]) == {"no-slot"}]
        # C-6.14: every lane with room only for want of a slot is waiting on its pilot.
        return "lane-proving" if slot_blocks and all(pilot_block(block) for block in slot_blocks) else "no-slot"
    if not counts:
        return "no-lanes"
    return max(sorted(counts), key=lambda label: counts[label])
