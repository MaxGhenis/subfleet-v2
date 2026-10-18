#!/usr/bin/env python3
"""Replay weekly ranking from a read-only *backup*, never from a live store.

Example (pinning the source snapshot prevents .backup restarting on every write):
    sqlite3 'file:/Users/you/.subfleet/state.sqlite3?mode=ro' <<'SQL'
    BEGIN;
    SELECT count(*) FROM schema_version;
    .backup /tmp/ranking-snapshot.sqlite3
    ROLLBACK;
    SQL
    python tools/replay_weekly_ranking.py /tmp/ranking-snapshot.sqlite3 \
        --output docs/reports/2026-10-03-rank-earliest-reset-replay.json

The fixed workload is the recorded attempt arrivals (including recorded retries),
their models, durations, and historical eligible candidate sets. No extra retry
or model promotion is invented. Recorded job arrivals are reconstructed and
counted; first attempts are their admitted arrivals. Candidate sets preserve the
actual standing exclusions, desktop handling, model reserve/stranding, affinity,
and closure decisions. They cannot represent counterfactual closure changes.

Positive consecutive usage deltas in one reset cycle are divided among attempts
in proportion to their overlapping active seconds. Account and model weekly
windows are charged separately. Increments when no attempt overlaps are external
usage. This attributes overlapping desktop/native activity to fleet attempts too;
it is an estimate of required demand, not token accounting. A limited attempt's
unobserved demand is its successful sibling's cost, or a provider/window median.

The physical cost model is recorded utilization plus cumulative simulated spend
minus factual fleet increments as each recorded reading arrives, within the
current reset cycle. Full job cost is charged at admission, with partial
consumption on a modeled limit. Counterfactual readings move only at the factual
sensor timestamps: admissions do not instantly change ranked usage. Their ages
remain factual; stale/past-reset observations never become fresh ranking
evidence. Resets clear corrections. Known account weekly resets are
reported from the last observation before reset, including corrections. These
assumptions isolate the comparator; this is not a prediction of live failure rate.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import heapq
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from typing import Any


WINDOWS = ("seven_day", "five_hour")
LIVE = Path.home() / ".subfleet" / "state.sqlite3"


def epoch(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    point = fraction * (len(ordered) - 1)
    low = int(point)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (point - low)


@dataclass
class Usage:
    lane: str
    scope: str
    window: str
    used: float
    reset: float | None
    observed: float
    reading_id: int
    label: str = "provider"

    @property
    def key(self) -> tuple[str, str, str]:
        return self.lane, self.scope, self.window


class Replay:
    def __init__(self, database: Path, *, until: str | None = None, days: int = 7,
                 progress: bool = False):
        self.progress = progress
        self.started = time.monotonic()
        if database.resolve() == LIVE.resolve() or ".subfleet" in database.resolve().parts:
            raise ValueError("Pass a temporary SQLite backup; reading a live .subfleet store is refused")
        self.db = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.lanes = {r["lane_id"]: dict(r) for r in self.db.execute("SELECT * FROM lanes")}
        newest = self.db.execute("SELECT max(observed_at) FROM readings").fetchone()[0]
        self.until = epoch(until or newest)
        assert self.until is not None
        self.since = self.until - days * 86400
        self.jobs = {r["job_id"]: dict(r) for r in self.db.execute(
            "SELECT * FROM jobs WHERE created_at<=?", (iso(self.until),))}
        # A day of lead-in plus the last preceding reading of every key lets the
        # replay initialize cycles without discarding low-frequency model windows.
        # Keep one preceding observation of each window, rather than loading
        # the live store's entire historical readings into memory.
        lead_in = iso(self.since - 86400)
        previous = {r[0] for r in self.db.execute("""SELECT max(reading_id) FROM readings
            WHERE label IN ('provider','stale-provider') AND utilization IS NOT NULL
            AND window IN ('seven_day','five_hour') AND observed_at<?
            GROUP BY lane_id,scope,window""", (lead_in,))}
        rows = self.db.execute("""SELECT reading_id,lane_id,scope,window,utilization,resets_at,observed_at,label
            FROM readings WHERE label IN ('provider','stale-provider') AND utilization IS NOT NULL
            AND window IN ('seven_day','five_hour') AND observed_at<=?
            AND (observed_at>=? OR reading_id IN (""" + ",".join("?" for _ in previous) + ")) "
            "ORDER BY observed_at,reading_id", (iso(self.until), lead_in, *previous))
        self.by_key: dict[tuple[str, str, str], list[Usage]] = defaultdict(list)
        for row in rows:
            reading = Usage(row["lane_id"], row["scope"], row["window"], row["utilization"],
                            epoch(row["resets_at"]), epoch(row["observed_at"]), row["reading_id"], row["label"])
            assert reading.observed is not None
            self.by_key[reading.key].append(reading)
        self.readings: list[Usage] = []
        for key, readings in self.by_key.items():
            first = max(0, bisect_right([r.observed for r in readings], self.since - 86400) - 1)
            self.by_key[key] = readings[first:]
            self.readings.extend(readings[first:])
        self.readings.sort(key=lambda r: (r.observed, r.reading_id))
        self.attempts = [dict(r) for r in self.db.execute("""SELECT a.*,j.kind,j.created_at AS job_created_at,
            j.pinned_lane,j.allow_desktop FROM attempts a JOIN jobs j USING(job_id)
            WHERE a.reserved_at<=? AND COALESCE(a.finished_at,?)>=? ORDER BY a.reserved_at,a.attempt_id""",
            (iso(self.until), iso(self.until), iso(self.since - 86400)))]
        for attempt in self.attempts:
            attempt["arrival"] = epoch(attempt["reserved_at"])
            attempt["end"] = epoch(attempt["finished_at"]) or self.until
            attempt["end"] = max(attempt["arrival"] + 1, attempt["end"])
            attempt["provider"] = self.lanes[attempt["lane_id"]]["provider"]
        self.arrivals = [a for a in self.attempts if self.since <= a["arrival"] <= self.until]
        self._progress(f"loaded {len(self.readings)} readings / {len(self.arrivals)} arrivals")
        self.snapshots = self._snapshots()
        self._progress(f"reconstructed {len(self.snapshots)} admission snapshots")
        self.costs, self.cost_details, self.factual_increments = self._costs()
        self._progress("attributed observed usage costs")
        self.db.close()

    def _progress(self, message: str) -> None:
        if self.progress:
            print(f"replay +{time.monotonic() - self.started:.1f}s: {message}", file=sys.stderr, flush=True)

    def _costs(self) -> tuple[dict[str, dict[tuple[str, str], float]], dict[str, Any], dict[int, float]]:
        costs: dict[str, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
        observed: dict[str, set[tuple[str, str]]] = defaultdict(set)
        lane_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in self.attempts:
            lane_attempts[attempt["lane_id"]].append(attempt)
        arrival_ids = {a["attempt_id"] for a in self.arrivals if
                       self.snapshots.get(a["attempt_id"], {}).get("candidates")}
        factual_increments: dict[int, float] = defaultdict(float)
        positive_intervals = external_intervals = reset_intervals = 0
        for key, rows in self.by_key.items():
            lane, scope, window = key
            scoped_attempts = [a for a in lane_attempts[lane]
                               if scope == "account" or a["model_requested"] == scope]
            active: list[dict[str, Any]] = []
            cursor = 0
            for left, right in zip(rows, rows[1:]):
                if right.observed < self.since or right.observed <= left.observed:
                    continue
                if left.reset != right.reset or left.reset is not None and left.reset <= right.observed:
                    reset_intervals += 1
                    continue
                delta = right.used - left.used
                if delta < -1e-8:
                    continue
                while cursor < len(scoped_attempts) and scoped_attempts[cursor]["arrival"] < right.observed:
                    active.append(scoped_attempts[cursor])
                    cursor += 1
                active = [a for a in active if a["end"] > left.observed]
                overlapping = []
                for attempt in active:
                    overlap = min(right.observed, attempt["end"]) - max(left.observed, attempt["arrival"])
                    if overlap > 0:
                        overlapping.append((attempt, overlap))
                        observed[attempt["attempt_id"]].add((scope, window))
                if not overlapping:
                    external_intervals += delta > 0
                    continue
                positive_intervals += delta > 0
                total = sum(seconds for _, seconds in overlapping)
                for attempt, seconds in overlapping:
                    attributed = max(0.0, delta) * seconds / total
                    costs[attempt["attempt_id"]][scope, window] += attributed
                    if attempt["attempt_id"] in arrival_ids:
                        factual_increments[right.reading_id] += attributed
        pools: dict[tuple[str, str], list[float]] = defaultdict(list)
        for attempt in self.arrivals:
            for window in WINDOWS:
                if ("account", window) in observed[attempt["attempt_id"]] and attempt["outcome_class"] != "limited":
                    pools[attempt["provider"], window].append(costs[attempt["attempt_id"]]["account", window])
        medians = {key: statistics.median(values) for key, values in pools.items()}
        siblings: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        for attempt in self.attempts:
            if attempt["outcome_class"] == "ok":
                for window in WINDOWS:
                    siblings[attempt["job_id"], attempt["provider"], window].append(
                        costs[attempt["attempt_id"]]["account", window])
        inferred: dict[str, dict[tuple[str, str], float]] = {}
        imputed = Counter()
        provider_windows = {(self.lanes[lane]["provider"], window)
                            for lane, _, window in self.by_key}
        for attempt in self.attempts:
            identity = attempt["attempt_id"]
            inferred[identity] = dict(costs[identity])
            for window in WINDOWS:
                if (attempt["provider"], window) not in provider_windows:
                    continue
                value = costs[identity]["account", window]
                if attempt["outcome_class"] == "limited" and value <= 0:
                    values = siblings.get((attempt["job_id"], attempt["provider"], window))
                    value = max(values) if values else medians.get((attempt["provider"], window), 0.0)
                    imputed[f"limited:{attempt['provider']}:{window}"] += identity in arrival_ids
                elif ("account", window) not in observed[identity]:
                    value = medians.get((attempt["provider"], window), 0.0)
                    imputed[f"unobserved:{attempt['provider']}:{window}"] += identity in arrival_ids
                inferred[identity]["account", window] = value
        summaries = {}
        for (provider, window), values in sorted(pools.items()):
            positive = [v for v in values if v > 1e-12]
            summaries[f"{provider}:{window}"] = {
                "observed_attempts": len(values), "positive_attempts": len(positive),
                "median": quantile(values, .5), "p90": quantile(values, .9),
                "p95": quantile(values, .95), "p99": quantile(values, .99),
                "positive_median": quantile(positive, .5), "positive_p95": quantile(positive, .95),
                "max": max(values, default=0.0),
            }
        job_costs: dict[tuple[str, str, str], float] = defaultdict(float)
        for attempt in self.arrivals:
            for window in WINDOWS:
                if ("account", window) in observed[attempt["attempt_id"]]:
                    job_costs[attempt["job_id"], attempt["provider"], window] += costs[attempt["attempt_id"]]["account", window]
        job_pools: dict[tuple[str, str], list[float]] = defaultdict(list)
        for (_, provider, window), value in job_costs.items():
            job_pools[provider, window].append(value)
        job_summaries = {}
        for (provider, window), values in sorted(job_pools.items()):
            positive = [v for v in values if v > 1e-12]
            job_summaries[f"{provider}:{window}"] = {
                "observed_jobs": len(values), "positive_jobs": len(positive),
                "median": quantile(values, .5), "p90": quantile(values, .9),
                "p95": quantile(values, .95), "p99": quantile(values, .99),
                "positive_median": quantile(positive, .5), "positive_p95": quantile(positive, .95),
                "max": max(values, default=0.0),
            }
        return inferred, {"attempt_quantiles": summaries, "job_quantiles": job_summaries, "imputed": dict(imputed),
                          "positive_workload_intervals": positive_intervals,
                          "positive_external_intervals": external_intervals,
                          "renewal_intervals_excluded": reset_intervals}, dict(factual_increments)

    def _snapshots(self) -> dict[str, dict[str, Any]]:
        snapshots = {}
        for attempt in self.arrivals:
            # Decisions are recorded in the same reserve transaction. Some old
            # versions left attempt_id NULL, so the job/time fallback is needed.
            # Select a small id first: sorting hundreds of large JSON blobs for
            # a long-running job would otherwise spill unnecessarily to disk.
            identity = self.db.execute("""SELECT decision_id FROM decisions WHERE job_id=?
                AND attempt_id=? ORDER BY evaluated_at DESC,decision_id DESC LIMIT 1""",
                (attempt["job_id"], attempt["attempt_id"])).fetchone()
            if identity is None:
                identity = self.db.execute("""SELECT decision_id FROM decisions WHERE job_id=?
                    AND evaluated_at<=? ORDER BY evaluated_at DESC,decision_id DESC LIMIT 1""",
                    (attempt["job_id"], iso(attempt["arrival"] + 2))).fetchone()
            row = self.db.execute("SELECT decision_json FROM decisions WHERE decision_id=?", identity).fetchone() if identity else None
            if not row:
                continue
            try:
                record = json.loads(row[0])
            except ValueError:
                continue
            evaluation = next((e for e in record.get("evaluations", ())
                               if e.get("model_id") == attempt["model_requested"] and e.get("candidates")), None)
            if evaluation is None:
                continue
            snapshots[attempt["attempt_id"]] = evaluation
        return snapshots

    def run(self, name: str, *, spread: int | None = 2, urgency: bool = False, old: bool = False) -> dict[str, Any]:
        latest: dict[tuple[str, str, str], Usage] = {}
        ranked_used: dict[tuple[str, str, str], float] = {}
        corrections: dict[tuple[str, str, str, float | None], float] = defaultdict(float)
        active: list[tuple[float, str, str]] = []
        in_flight: Counter[tuple[str, str]] = Counter()
        reset_rows = []
        reset_indexes: dict[tuple[str, float], int] = {}
        limits = near_five = placed = missed = measured = stale = changed = 0
        placement_counts: Counter[str] = Counter()
        by_provider: dict[str, Counter] = defaultdict(Counter)
        # Attempts already running at the seven-day boundary keep their factual
        # lanes and remaining occupancy. Their usage is left in the baseline.
        for attempt in self.attempts:
            if attempt["arrival"] < self.since < attempt["end"]:
                heapq.heappush(active, (attempt["end"], attempt["lane_id"], attempt["kind"]))
                in_flight[attempt["lane_id"], attempt["kind"]] += 1

        def physical(row: Usage, now: float, *, before_reset: bool = False) -> float:
            if row.reset is not None and row.reset <= now and not before_reset:
                # Physical renewal is used by the cost model only. Ranking below
                # still refuses this expired observation as measured evidence.
                return min(1.0, max(0.0, corrections[*row.key, row.reset]))
            return min(1.0, max(0.0, row.used + corrections[*row.key, row.reset]))

        def lane_rows(lane: str, model: str) -> list[Usage]:
            return [row for key, row in latest.items() if key[0] == lane and key[1] in ("account", model)]

        def detail(lane: str, attempt: dict[str, Any]) -> dict[str, Any]:
            now = attempt["arrival"]
            rows = lane_rows(lane, attempt["model_requested"])
            fresh = [row for row in rows if row.label == "provider" and 0 <= now - row.observed <= 120]
            if not old and any(row.reset is not None and row.reset <= now for row in rows):
                fresh = []
            weekly = [row for row in fresh if row.window == "seven_day"]
            binding = min(weekly, key=lambda row: (1 - ranked_used[row.key], row.reset or float("inf"), row.scope), default=None)
            five = [ranked_used[row.key] for row in fresh if row.window == "five_hour"]
            recorded = self.snapshots[attempt["attempt_id"]].get("candidate_details", {}).get(lane, {})
            return {"measured": bool(fresh),
                    "weekly": 1 - ranked_used[binding.key] if binding else None,
                    "weekly_used": ranked_used[binding.key] if binding else None,
                    "five": 1 - max(five) if five else None,
                    "five_used": max(five) if five else None,
                    "reset": binding.reset if binding else None,
                    "old_reset": min((row.reset for row in weekly if row.reset is not None), default=None),
                    "headroom": min((1 - ranked_used[row.key] for row in fresh), default=0.0),
                    "stranded": bool(recorded.get("stranded_scopes")),
                    "reserve_slack": (recorded.get("reserve") or {}).get("slack")}

        def key(lane: str, attempt: dict[str, Any]) -> tuple:
            d = detail(lane, attempt)
            count = in_flight[lane, attempt["kind"]]
            band = count // spread if spread else 0
            desktop = bool(self.lanes[lane]["desktop"])
            # A turn's admission record already places affinity first. The
            # separate conversation store is intentionally not read; preserve
            # that historically chosen lane as affinity for turn replays.
            affinity = attempt["kind"] == "turn" and lane != attempt["lane_id"]
            prefix = (desktop, affinity, band)
            if attempt["provider"] == "claude":
                prefix += (not d["stranded"],)
            prefix += (not d["measured"],)
            if old:
                if attempt["provider"] == "codex":
                    return (*prefix, d["old_reset"] or float("inf"), lane)
                return (*prefix, -(d["reserve_slack"] if d["reserve_slack"] is not None else d["headroom"]), count, lane)
            weekly_reserve = d["weekly_used"] is not None and d["weekly_used"] > 1 - .02
            five_reserve = d["five_used"] is not None and d["five_used"] > 1 - .10
            if urgency:
                hours = max((d["reset"] - attempt["arrival"]) / 3600, 1 / 3600) if d["reset"] else None
                rate = d["weekly"] / hours if hours is not None and d["weekly"] is not None else 0.0
                return (*prefix, weekly_reserve, five_reserve, -rate,
                        d["reset"] or float("inf"), -(d["weekly"] or 0), count, lane)
            return (*prefix, weekly_reserve, five_reserve, d["reset"] or float("inf"),
                    -(d["weekly"] or 0), count, lane)

        events: list[tuple[float, int, Any]] = [(r.observed, 0, r) for r in self.readings]
        resets = sorted({(r.reset, r.lane, r.scope, r.window) for r in self.readings
                         if r.reset is not None and self.since <= r.reset <= self.until})
        events += [(when, 1, (lane, scope, window)) for when, lane, scope, window in resets]
        events += [(a["arrival"], 2, a) for a in self.arrivals]
        events.sort(key=lambda item: (item[0], item[1], getattr(item[2], "reading_id", 0)))
        for now, kind, item in events:
            while active and active[0][0] <= now:
                _, lane, pool = heapq.heappop(active)
                in_flight[lane, pool] -= 1
            if kind == 0:
                latest[item.key] = item
                corrections[*item.key, item.reset] -= self.factual_increments.get(item.reading_id, 0.0)
                ranked_used[item.key] = physical(item, now)
                continue
            if kind == 1:
                lane, scope, window = item
                row = latest.get(item)
                if row and row.reset == now and scope == "account" and window == "seven_day":
                    account = self.lanes[lane].get("account_key", lane)
                    record = {"lane": lane, "reset": iso(now),
                              "account_lanes": sorted(identity for identity, info in self.lanes.items()
                                  if info.get("account_key", identity) == account),
                              "unused_fraction": round(1 - physical(row, now, before_reset=True), 6),
                              "reading_age_s": round(now - row.observed, 1)}
                    duplicate = reset_indexes.get((account, now))
                    if duplicate is None:
                        reset_indexes[account, now] = len(reset_rows)
                        reset_rows.append(record)
                    elif record["reading_age_s"] < reset_rows[duplicate]["reading_age_s"]:
                        reset_rows[duplicate] = record
                if row and row.reset == now:
                    corrections[*row.key, row.reset] = 0.0
                continue
            attempt = item
            snapshot = self.snapshots.get(attempt["attempt_id"])
            if not snapshot:
                missed += 1
                continue
            candidates = [lane for lane in snapshot["candidates"] if lane in self.lanes]
            if not candidates:
                missed += 1
                continue
            target = min(candidates, key=lambda lane: key(lane, attempt))
            placed += 1
            changed += target != attempt["lane_id"]
            placement_counts[target] += 1
            d = detail(target, attempt)
            measured += d["measured"]
            stale += not d["measured"]
            provider_counts = by_provider[attempt["provider"]]
            provider_counts["attempts"] += 1
            provider_counts["measured_placements"] += d["measured"]
            target_rows = lane_rows(target, attempt["model_requested"])
            if any(row.window == "five_hour" and physical(row, now) >= .95 for row in target_rows):
                near_five += 1
                provider_counts["within_five_percent_five_hour"] += 1
            cost = self.costs[attempt["attempt_id"]]
            fractions = [(1 - physical(row, now)) / cost.get((row.scope, row.window), 0.0)
                         for row in target_rows if cost.get((row.scope, row.window), 0.0) > 0]
            fraction = max(0.0, min([1.0, *fractions]))
            limits += fraction < 1 - 1e-9
            provider_counts["limited_attempts"] += fraction < 1 - 1e-9
            for row in target_rows:
                corrections[*row.key, row.reset] += cost.get((row.scope, row.window), 0.0) * fraction
            heapq.heappush(active, (attempt["end"], target, attempt["kind"]))
            in_flight[target, attempt["kind"]] += 1
        return {"variant": name, "lane_spread": spread, "attempts": placed,
                "missing_decisions": missed, "changed_placements": changed,
                "measured_placements": measured, "unmeasured_placements": stale,
                "within_five_percent_five_hour": near_five,
                "limited_attempts": limits, "limited_share": round(limits / placed if placed else 0, 6),
                "weekly_unused_sum": round(sum(r["unused_fraction"] for r in reset_rows), 6),
                "weekly_unused_mean": round(statistics.mean(r["unused_fraction"] for r in reset_rows), 6) if reset_rows else None,
                "weekly_resets": reset_rows, "placements_by_lane": dict(sorted(placement_counts.items())),
                "by_provider": {provider: {**counts,
                    "limited_share": round(counts["limited_attempts"] / counts["attempts"], 6)}
                    for provider, counts in sorted(by_provider.items())}}

    def report(self, database: Path) -> dict[str, Any]:
        variants = [self.run("old", old=True), self.run("new, spread=2"),
                    self.run("urgency=headroom/hours", urgency=True), self.run("new, spread=4", spread=4),
                    self.run("new, spread=null", spread=None)]
        cost = self.cost_details
        recorded = Counter(a["outcome_class"] or "unfinished" for a in self.arrivals)
        resets = variants[0]["weekly_resets"]
        return {"period": {"since": iso(self.since), "until": iso(self.until)},
                "snapshot_bytes": database.stat().st_size,
                "snapshot_method": "sqlite3 file:~/.subfleet/state.sqlite3?mode=ro; BEGIN; SELECT count(*) FROM schema_version; .backup TEMP_COPY; ROLLBACK; replay only the copy with mode=ro and query_only=ON",
                "recorded_attempt_arrivals": len(self.arrivals),
                "recorded_job_arrivals": sum(self.since <= epoch(j["created_at"]) <= self.until for j in self.jobs.values()),
                "admitted_first_attempts": sum(a["seq"] == 1 for a in self.arrivals),
                "turn_attempts": sum(a["kind"] == "turn" for a in self.arrivals),
                "recorded_outcomes": dict(recorded),
                "recorded_limited_share": round(recorded["limited"] / len(self.arrivals), 6),
                "usage_readings_loaded": len(self.readings), "cost_model": cost,
                "weekly_reset_coverage": {
                    "account_resets": len(resets),
                    "by_provider": dict(Counter(self.lanes[row["lane"]]["provider"] for row in resets)),
                    "youngest_pre_reset_reading_s": min((row["reading_age_s"] for row in resets), default=None),
                    "oldest_pre_reset_reading_s": max((row["reading_age_s"] for row in resets), default=None)},
                "reserves": {"weekly": .02, "five_hour": .10},
                "variants": variants,
                "limitations": [
                    "Recorded attempt/retry arrivals, durations, models and eligible candidate sets are fixed; no counterfactual retries, promotions or closure changes.",
                    "Job creation arrivals are counted, but placement occurs at recorded attempt reservation timestamps. Requests with no recorded admission have no observable attempt cost and are not placed.",
                    "Turn affinity is the factual selected lane, because the separate conversation store was not copied.",
                    "Observed positive utilization deltas are allocated by active overlap; unrelated usage while attempts run cannot be separated.",
                    "Usage deltas are normalized fractions of original account limits; lane-specific subscription capacity differences are not modeled.",
                    "Full demand is charged at admission; successful siblings or provider/window medians impute limited and unobserved demand. Ranking moves only when a factual sensor timestamp arrives.",
                    "Renewal intervals are excluded from cost attribution; post-reset spend without a renewed observation is discarded when the next cycle's factual reading arrives.",
                    "Historical readings and their ages are retained, so this replay does not estimate the benefit of additional busy-lane sensor reads.",
                    "Unused weekly capacity uses the last pre-reset observation plus workload corrections; external use after that observation is unobserved.",
                    "Only observed scheduled account resets can be reported; this snapshot contains Claude resets and no Codex weekly resets. Pre-reset readings are generally stale by hours or days.",
                    "A cost-model limited fraction is an estimate, not a prediction of provider errors or the actual live limited rate."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--until")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    replay = Replay(args.database, until=args.until, days=args.days, progress=args.progress)
    report = replay.report(args.database)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(json.dumps({key: value for key, value in report.items() if key not in ("variants", "limitations")}, indent=2))
    for run in report["variants"]:
        print(json.dumps({key: value for key, value in run.items() if key not in ("weekly_resets", "placements_by_lane")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
