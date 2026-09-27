"""Hypothesis strategies for routing: stores, route jobs, policies, and the commits
that land between admission's early evaluation and its reservation (C-6.3).

Rows are shaped as the store holds them, so the same rows build a capacity view
(`capacity.build_view`, as `Daemon._capacity_view` does) and feed the check made
inside the reservation (`route_check.still_stands`).
"""

from __future__ import annotations

import copy
import dataclasses
import re
from datetime import datetime, timedelta, timezone

from hypothesis import event as hypothesis_event
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument

from subfleet import capacity
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy

#: The early evaluation's clock: not a whole second, as the wall clock seldom is.
NOW = datetime(2026, 9, 26, 12, 0, 0, 400000, tzinfo=timezone.utc)
LANE_IDS = ("claude-1", "claude-2", "claude-3", "codex-1", "codex-2")
SPARE_LANES = ("claude-4", "codex-3")
ACCOUNTS = ("a@example.invalid", "b@example.invalid", "c@example.invalid")
SCOPES = ("account", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5", "claude-haiku-4-5-20251001",
          "gpt-6-astra", "gpt-5.6-terra")
ACTIVE = ("reserved", "starting", "running", "finalizing")
ENDED = ("succeeded", "failed", "cancelled", "interrupted", "lost", "quarantined")
CANONICAL = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
BASE_POLICY = load_policy(DEFAULT_POLICY_PATH)


def stamp(at: datetime, shape: str = "canonical") -> str:
    """A timestamp as a writer stores it: `utc_now`'s shape, or another one a hand edit or an old row has."""
    if shape == "offset":
        return at.replace(microsecond=0).isoformat()                 # 2026-09-26T12:00:00+00:00
    if shape == "fraction":
        return at.isoformat().replace("+00:00", "Z")                 # 2026-09-26T12:00:00.400000Z
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def candidates_of(readings: list[dict]) -> list[dict]:
    """What `Store.latest_reading_candidates` returns over these rows: per lane, scope and
    window, the rows at the greatest canonical timestamp, plus every other shape."""
    top: dict[tuple, str] = {}
    for row in readings:
        if CANONICAL.match(row["observed_at"]):
            key = (row["lane_id"], row["scope"], row["window"])
            top[key] = max(top.get(key, ""), row["observed_at"])
    return [dict(row) for row in readings
            if not CANONICAL.match(row["observed_at"])
            or row["observed_at"] == top[(row["lane_id"], row["scope"], row["window"])]]


@st.composite
def lane_rows(draw, lane_id: str) -> dict:
    provider = lane_id.split("-")[0]
    account = draw(st.sampled_from(ACCOUNTS))
    return {"lane_id": lane_id, "provider": provider, "account_key": f"{provider}:{account}",
            "credential_ref": f"/credentials/{lane_id}",
            "credential_kind": draw(st.sampled_from(["keychain-token", "keychain-token", "home"])),
            "credential_epoch": 1, "home": draw(st.sampled_from([None, f"/homes/{lane_id}"])),
            "owner": draw(st.sampled_from(["v2"] * 12 + ["v1"])),
            "desktop": draw(st.sampled_from([0] * 12 + [1])), "enabled": draw(st.sampled_from([1] * 12 + [0])),
            "plan": None, "identity": None, "label": draw(st.sampled_from([None, account])),
            "identity_status": draw(st.sampled_from([None] * 8 + ["verified", "verified", "mismatch"])),
            "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"}


@st.composite
def reading_rows(draw, lane_id: str, reading_id: int, around: datetime) -> dict:
    observed = around + timedelta(seconds=draw(st.sampled_from([-600, -130, -121, -119, -118, -60, -3, 0, 2, 40])))
    resets = draw(st.sampled_from([None, -10, 1, 3, 3600, 3 * 86400]))
    return {"reading_id": reading_id, "lane_id": lane_id, "scope": draw(st.sampled_from(SCOPES)),
            "window": draw(st.sampled_from(["seven_day", "seven_day", "five_hour", "admission"])),
            "utilization": draw(st.sampled_from([None, 0.0, 0.1, 0.4, 0.7, 0.84, 0.86, 0.95, 1.0])),
            "resets_at": None if resets is None else stamp(around + timedelta(seconds=resets)),
            "label": draw(st.sampled_from(["provider"] * 5 + ["stale-provider", "admission-observed", "unknown"])),
            "source": draw(st.sampled_from(["oauth-usage", "oauth-usage", "rate_limit_event", "probe"])),
            "observed_at": stamp(observed, draw(st.sampled_from(["canonical"] * 8 + ["offset", "fraction"]))),
            "attempt_id": draw(st.sampled_from([None, None, "shared/a1"]))}


@st.composite
def closure_rows(draw, lane_id: str, closure_id: int, around: datetime) -> dict:
    until = around + timedelta(seconds=draw(st.sampled_from([-30, 1, 4, 90, 3600, 86400])))
    return {"closure_id": closure_id, "lane_id": lane_id, "scope": draw(st.sampled_from(SCOPES[:5] + ("gpt-6-astra",))),
            "until_at": stamp(until, draw(st.sampled_from(["canonical"] * 6 + ["fraction"]))),
            "reason": draw(st.sampled_from(["provider-limit", "provider-limit", "operator-hold", "cooldown"])),
            "clock_source": draw(st.sampled_from(["reported", "guessed"])), "source_event": "fixture",
            "created_at": stamp(around), "released_at": draw(st.sampled_from([None] * 6 + [stamp(around)]))}


@st.composite
def stores(draw) -> dict:
    """One store: lanes, readings, closures, jobs with a parent chain, attempts, probes, overrides."""
    lane_ids = draw(st.lists(st.sampled_from(LANE_IDS), min_size=1, max_size=len(LANE_IDS), unique=True))
    for provider in ("claude", "codex"):             # both providers, so most jobs have lanes to walk
        if not any(lane_id.startswith(provider) for lane_id in lane_ids):
            lane_ids.append(f"{provider}-1")
    lanes = [draw(lane_rows(lane_id)) for lane_id in sorted(lane_ids)]
    readings = []
    for n in range(draw(st.integers(0, 14))):
        readings.append(draw(reading_rows(draw(st.sampled_from(lane_ids)), n + 1, NOW)))
    closures = [draw(closure_rows(draw(st.sampled_from(lane_ids)), n + 1, NOW))
                for n in range(draw(st.integers(0, 4)))]
    jobs = []
    for n in range(draw(st.integers(1, 5))):
        parent = draw(st.sampled_from([None, None] + [row["job_id"] for row in jobs]))
        jobs.append({"job_id": f"job-{n}", "kind": draw(st.sampled_from(["dispatch", "dispatch", "turn"])),
                     "parent_job_id": parent})
    attempts = [{"attempt_id": f"attempt-{n}", "job_id": draw(st.sampled_from(jobs))["job_id"],
                 "lane_id": draw(st.sampled_from(lane_ids)), "state": draw(st.sampled_from(ACTIVE + ENDED))}
                for n in range(draw(st.integers(0, 4)))]
    probes = {lane_id: draw(st.sampled_from(["probe:timer:1", "credential-latched"]))
              for lane_id in draw(st.lists(st.sampled_from(lane_ids), max_size=2, unique=True))}
    overridden = set(draw(st.lists(st.sampled_from(lane_ids), max_size=1, unique=True)))
    return {"lanes": lanes, "readings": readings, "closures": closures, "jobs": jobs, "attempts": attempts,
            "unavailable": probes, "overridden": overridden}


@st.composite
def route_jobs(draw, store: dict) -> dict:
    """A job admission routes, as `Daemon._pick` hands it to `evaluate`."""
    kind = draw(st.sampled_from(["dispatch", "dispatch", "turn", "resume"]))
    job = {"job_id": "route-job", "kind": kind, "sandbox": draw(st.sampled_from(["read-only", "workspace-write"])),
           "task": None, "tier": None, "pinned_model": None, "pinned_lane": None, "allow_desktop": 0,
           "parent_job_id": draw(st.sampled_from([None] + [row["job_id"] for row in store["jobs"]])),
           "policy_hash": "fixture", "unmeasured_reserve_reason": None}
    how = draw(st.sampled_from(["task", "task", "model", "lane", "lane-and-model"]))
    if how == "task":
        job["task"] = draw(st.sampled_from(["research", "build", "sweep", "strategy"]))
        job["tier"] = draw(st.sampled_from([None, "trivial", "easy", "standard", "hard"]))
    if how in ("model", "lane-and-model"):
        job["pinned_model"] = draw(st.sampled_from(["opus", "fable", "haiku", "astra", "terra"]))
    if how in ("lane", "lane-and-model"):
        lanes = store["lanes"]
        provider = BASE_POLICY["models"][job["pinned_model"]]["provider"] if job["pinned_model"] else None
        matching = [row for row in lanes if row["provider"] == provider] or lanes
        pick = draw(st.sampled_from(matching if draw(st.integers(0, 5)) else lanes))
        job["pinned_lane"] = draw(st.sampled_from([pick["lane_id"], pick["account_key"].partition(":")[2],
                                                   "nobody@example.invalid"]))
        if how == "lane" and draw(st.booleans()):
            job["task"], job["tier"] = "research", "standard"
    job["exclusions"] = tuple(draw(st.lists(st.sampled_from(list(ACCOUNTS[:1]) + ["claude-2", "codex-1"]),
                                            max_size=1, unique=True)))
    job["allow_desktop"] = int(draw(st.booleans()))
    if kind == "turn" and draw(st.booleans()):
        job["affinity_lane"] = draw(st.sampled_from([row["lane_id"] for row in store["lanes"]]))
    if how == "lane-and-model" and draw(st.integers(0, 9)) == 0:
        job["unmeasured_reserve_reason"] = "fixture authorization"
    return job


@st.composite
def policies(draw) -> dict:
    policy = copy.deepcopy(BASE_POLICY)
    if draw(st.booleans()):
        policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    policy["caps"].update(max_active_attempts=draw(st.sampled_from([1, 3, 4, 8, 8])),
                          max_in_flight_per_lane=draw(st.sampled_from([1, 2, 3])),
                          max_in_flight_unmeasured=1,
                          max_active_attempts_per_parent=draw(st.sampled_from([1, 2, 9])))
    policy.setdefault("conversations", {}).update(max_active_turns=draw(st.sampled_from([1, 3])),
                                                  turn_slots_per_lane=draw(st.sampled_from([1, 2])))
    return policy


def view_of(store: dict, now: datetime, ttl: int = 120) -> dict:
    """The view `Daemon._pick` evaluates: built from the reading candidates, the probe
    leases and latched credentials as `unavailable_lanes`, overridden lanes' readings
    held out."""
    view = capacity.build_view(store["lanes"], candidates_of(store["readings"]), store["closures"],
                               store["attempts"], store["jobs"], now=now, reading_ttl_s=ttl)
    view["unavailable_lanes"] = dict(store["unavailable"])
    view["reserved_probes"] = sum(1 for holder in store["unavailable"].values() if holder.startswith("probe:"))
    view["readings"] = [row for row in view["readings"] if row["lane_id"] not in store["overridden"]]
    return view


@st.composite
def commits(draw, store: dict, focus: tuple[str, ...] = ()) -> tuple[dict, float]:
    """What other transactions commit between the snapshot and the reservation, and
    how long it all takes: a new store and the seconds that passed. `focus` names
    lanes the commits land on more often (the lanes a decision walked)."""
    after = copy.deepcopy(store)
    seconds = draw(st.sampled_from([0, 0.3, 0.7, 1.5, 5, 30, 200]))
    later = NOW + timedelta(seconds=seconds)
    lane_ids = [row["lane_id"] for row in after["lanes"]]
    focus = [lane_id for lane_id in focus if lane_id in lane_ids]

    def a_lane():
        return draw(st.sampled_from(focus if focus and draw(st.integers(0, 3)) else lane_ids))
    next_reading = max((row["reading_id"] for row in after["readings"]), default=0) + 1
    next_closure = max((row["closure_id"] for row in after["closures"]), default=0) + 1
    for step in range(draw(st.integers(0, 5))):
        what = draw(st.sampled_from(["reading", "reading", "reading", "closure", "release", "extend",
                                     "attempt", "end", "lane", "new-lane", "probe", "unprobe",
                                     "override", "unoverride"]))
        if what == "reading" and lane_ids:
            after["readings"].append(draw(reading_rows(a_lane(), next_reading, later)))
            next_reading += 1
        elif what == "closure" and lane_ids:
            after["closures"].append(draw(closure_rows(a_lane(), next_closure, later)))
            next_closure += 1
        elif what == "release" and after["closures"]:
            draw(st.sampled_from(after["closures"]))["released_at"] = stamp(later)
        elif what == "extend" and after["closures"]:
            row = draw(st.sampled_from(after["closures"]))
            row["until_at"] = stamp(capacity._time(row["until_at"]) + timedelta(hours=1))
        elif what == "attempt" and lane_ids:
            job = draw(st.sampled_from(after["jobs"] + [None]))
            if job is None:
                job = {"job_id": f"job-new-{step}", "kind": draw(st.sampled_from(["dispatch", "turn"])),
                       "parent_job_id": draw(st.sampled_from([None] + [row["job_id"] for row in after["jobs"]]))}
                after["jobs"].append(job)
            after["attempts"].append({"attempt_id": f"attempt-new-{step}", "job_id": job["job_id"],
                                      "lane_id": a_lane(), "state": "reserved"})
        elif what == "end":
            live = [row for row in after["attempts"] if row["state"] in ACTIVE]
            if live:
                draw(st.sampled_from(live))["state"] = draw(st.sampled_from(["succeeded", "failed", "quarantined"]))
        elif what == "lane" and after["lanes"]:
            row = draw(st.sampled_from(after["lanes"]))
            key = draw(st.sampled_from(["enabled", "owner", "identity_status", "desktop", "label", "credential_kind"]))
            row[key] = draw({"enabled": st.sampled_from([0, 1]), "owner": st.sampled_from(["v1", "v2"]),
                             "identity_status": st.sampled_from([None, "verified", "mismatch"]),
                             "desktop": st.sampled_from([0, 1]), "label": st.sampled_from([None, *ACCOUNTS]),
                             "credential_kind": st.sampled_from(["keychain-token", "home"])}[key])
        elif what == "new-lane":
            spare = [lane_id for lane_id in SPARE_LANES if lane_id not in lane_ids]
            if spare:
                after["lanes"].append(draw(lane_rows(spare[0])))
                lane_ids.append(spare[0])
        elif what == "probe" and lane_ids:
            after["unavailable"][a_lane()] = "probe:timer:2"
        elif what == "unprobe" and after["unavailable"]:
            after["unavailable"].pop(draw(st.sampled_from(sorted(after["unavailable"]))))
        elif what == "override" and lane_ids:
            after["overridden"] = set(after["overridden"]) | {a_lane()}       # a reset credit confirmed
        elif what == "unoverride" and after["overridden"]:
            after["overridden"] = set(after["overridden"]) - {draw(st.sampled_from(sorted(after["overridden"])))}
    return after, seconds


# --- comparing decisions ---------------------------------------------------------------------

def event(text: str) -> None:
    """Count a kind of case in Hypothesis's statistics; outside a Hypothesis test, nothing."""
    try:
        hypothesis_event(text)
    except InvalidArgument:
        pass


def exact(decision) -> dict:
    """A decision as data, all of it: lane, model, verdict, details and evidence, every
    reading's age and label and every closure row, and `evaluated_at`. What C-6.3's check
    returns is exactly what `scheduler.evaluate` returns at the check's clock (review of
    d04b8b3: the comparison used to keep only the evidence's ids, and hid a reading
    labelled `provider` where an evaluation said `stale-provider`)."""
    return dataclasses.asdict(decision)


def walked_no_further(decision, full) -> bool:
    """Whether an evaluation now (`full`) can be had from the lanes whose rows changed
    since `decision`'s: it has the capacity blocks the early one had and walked no model
    of the chain the early one did not judge. (A pin that names another lane now is the
    other case; the caller checks it where pins are generated.)"""
    return (full is not None
            and full.evaluations[0]["capacity_blocks"] == decision.evaluations[0]["capacity_blocks"]
            and len(full.chain) <= len(decision.chain))
