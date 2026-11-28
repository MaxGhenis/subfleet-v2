"""C-6.25: rolling start budgets over the reservation ledger."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import scheduler
from .policy import admission_settings

GLOBAL_WINDOW_S = 60
REASON = "start-budget"


def _instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _stamp(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def _shift(instant: datetime, seconds: int) -> datetime:
    try:
        return instant + timedelta(seconds=seconds)
    except OverflowError:
        extreme = datetime.min if seconds < 0 else datetime.max
        return extreme.replace(tzinfo=timezone.utc)


class StartBudget:
    """One detached pass's ledger snapshot, plus its committed reservations."""

    def __init__(self, policy):
        settings = admission_settings(policy)
        self.lane_limit = settings["lane_starts_per_window"]
        self.lane_window = settings["lane_start_window_s"]
        self.global_limit = settings["global_starts_per_min"]
        self.lane_starts: dict[str, list[datetime]] = defaultdict(list)
        self.global_starts: list[datetime] = []
        self.lane_deadlines: dict[str, datetime] = {}
        self.global_deadline: datetime | None = None

    @property
    def enabled(self) -> bool:
        return self.lane_limit is not None or self.global_limit is not None

    @classmethod
    def read(cls, policy, query, now: str, *, priority_jobs) -> StartBudget:
        budget = cls(policy)
        if not budget.enabled:
            return budget
        instant = _instant(now)
        lane_cutoff = _shift(instant, -budget.lane_window)
        global_cutoff = _shift(instant, -GLOBAL_WINDOW_S)
        since = lane_cutoff if budget.lane_limit is not None else global_cutoff
        if budget.global_limit is not None:
            since = min(since, global_cutoff)
        # Read whole boundary seconds through attempts_reserved, then compare
        # parsed times so that a fractional reservation can expire mid-second.
        rows = query(
            "SELECT a.lane_id,a.reserved_at,j.job_id,j.kind,j.caller_session,j.parent_job_id "
            "FROM attempts a JOIN jobs j USING(job_id) WHERE a.reserved_at BETWEEN ? AND ?",
            (_stamp(since)[:19], _stamp(instant)[:19] + "~"),
        )
        jobs = {row["job_id"]: dict(row) for row in rows}
        family = priority_jobs(list(jobs.values()))
        counted = {job_id for job_id, job in jobs.items() if not scheduler.machine_guard_exempt(
            scheduler.priority_class(job, policy=policy, jobs=family))}
        for row in rows:
            if row["job_id"] not in counted:
                continue
            try:
                when = _instant(row["reserved_at"])
            except ValueError:
                continue
            if when <= instant:
                budget._record(row["lane_id"], when, lane=when > lane_cutoff, global_=when > global_cutoff)
        return budget

    def record(self, lane_id: str, reserved_at: str) -> None:
        self._record(lane_id, _instant(reserved_at), lane=True, global_=True)

    def _record(self, lane_id: str, when: datetime, *, lane: bool, global_: bool) -> None:
        if lane and self.lane_limit is not None:
            starts = self.lane_starts[lane_id]
            starts.append(when)
            if len(starts) == self.lane_limit:
                self.lane_deadlines[lane_id] = _shift(min(starts), self.lane_window)
            elif len(starts) > self.lane_limit:
                self.lane_deadlines[lane_id] = min(self.lane_deadlines[lane_id], _shift(when, self.lane_window))
        if global_ and self.global_limit is not None:
            self.global_starts.append(when)
            if len(self.global_starts) == self.global_limit:
                self.global_deadline = _shift(min(self.global_starts), GLOBAL_WINDOW_S)
            elif self.global_deadline is not None:
                self.global_deadline = min(self.global_deadline, _shift(when, GLOBAL_WINDOW_S))

    def lane_wait(self, decision) -> tuple[datetime | None, bool]:
        """First budget expiry on usable lanes, and whether a slot also blocked a lane."""
        deadlines = []
        slot_wait = False
        for evaluation in decision.evaluations:
            for row in evaluation["rejections"]:
                if row["reasons"] == ["no-slot"]:
                    if row.get("start_budget"):
                        deadlines.append(self.lane_deadlines[row["lane_id"]])
                    else:
                        slot_wait = True
        return min(deadlines, default=None), slot_wait

    def status(self, lane_ids) -> dict:
        result = {}
        if self.lane_limit is not None:
            result["lane"] = {"limit": self.lane_limit, "window_s": self.lane_window,
                              "used": {lane: len(self.lane_starts.get(lane, ()))
                                       for lane in sorted(set(lane_ids) | self.lane_starts.keys())}}
        if self.global_limit is not None:
            result["global"] = {"limit": self.global_limit, "window_s": GLOBAL_WINDOW_S,
                                "used": len(self.global_starts)}
        return result
