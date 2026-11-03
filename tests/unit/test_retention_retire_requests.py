"""C-8.6: retirement on request. A finished job whose retirement was asked for is
retired whatever its pool's budget, after orphan leases' jobs and before the rest,
through the steps every retirement takes (design d635: quarantine, archive, verify,
commit, delete). A request lifts the budget only: a pin, a deferral and the batch
still bind.

Origin, 2026-11-02: Fleet ops asked to retire 39 finished Track B jobs ahead of the
Microcosm US release (20.36 GB of trees, measured read-only in
~/reviews/subfleet-hub/trackb-retention-2026-11-02/). Retention in 2.1.12 retires
only a pool's oldest jobs while the pool is over its budget, and 10,132 finished
detached jobs were older than the oldest of the 39 (read-only, 2026-11-02 21:00Z).

These run real passes (`retention.maintenance`) over a real store and real job
folders. Jobs here have no worktree, so no git runs: what selection reads is the
store, and the archive of a job folder is the one every retirement makes.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet.retention_survey import survey
from subfleet.store import Store, utc_now
from tests.unit.retention_world import Clock

TERMINAL = ("succeeded", "failed", "cancelled", "lost")
HUGE = 10 ** 15


class Root:
    """A state root whose jobs have a job folder and no worktree, oldest first in the
    order they are made."""

    def __init__(self, base: Path):
        self.root = base / "state"
        (self.root / "jobs").mkdir(parents=True)
        (self.root / "worktrees").mkdir()
        self.store = Store(self.root / "state.sqlite3")
        self.made = 0

    def job(self, job_id: str, *, state: str = "succeeded", kind: str = "dispatch") -> str:
        self.made += 1
        created = f"2026-10-{1 + self.made // 24:02d}T{self.made % 24:02d}:00:00Z"
        self.store.add_job(job_id=job_id, request_id="req-" + job_id, payload_digest="d", kind=kind,
                           workdir="/nonexistent-workdir", prompt_path="/prompt", sandbox="read-only",
                           state=state, created_at=created)
        folder = self.root / "jobs" / job_id
        folder.mkdir()
        (folder / "stdout").write_bytes(b"output of " + job_id.encode())
        return created

    def event(self, kind: str, job_id: str, data_json: str) -> None:
        """One event row, as `Daemon._retire` writes it: no audit event beside it."""
        self.store.conn.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                                (utc_now(), kind, job_id, data_json))

    def request(self, job_id: str, *, created_at: str | None = None) -> None:
        created = created_at if created_at is not None else self.store.get_job(job_id)["created_at"]
        self.event(retention.RETIRE_REQUESTED, job_id, json.dumps({"created_at": created}))

    def withdraw(self, job_id: str) -> None:
        self.event(retention.RETIRE_WITHDRAWN, job_id,
                   json.dumps({"created_at": self.store.get_job(job_id)["created_at"]}))

    def ids(self) -> set[str]:
        return {row["job_id"] for row in self.store.query("SELECT job_id FROM jobs")}

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def root(tmp_path):
    made = Root(tmp_path)
    yield made
    made.close()


def run(root: Root, **kwargs):
    kwargs.setdefault("holders", lambda watches, **_: {})
    return retention.maintenance(root.store, root.root, **kwargs)


# --- the request path ---------------------------------------------------------------

def test_a_request_retires_a_job_its_pools_budget_keeps(root):
    for job_id in ("a", "b", "c"):
        root.job(job_id)
    assert run(root, max_jobs=10)["pruned"] == []          # within budget: nothing goes
    root.request("b")
    result = run(root, max_jobs=10)
    assert result["pruned"] == ["b"] and result["requested"] == ["b"], result
    assert root.ids() == {"a", "c"}
    assert not (root.root / "jobs" / "b").exists()
    # The archive every retirement makes, and its audit event.
    assert [a["archive"] for a in rarch.list_archives(root.root)] == ["b"]
    assert root.store.query("SELECT job_id FROM events WHERE kind='retention.pruned'") == [{"job_id": "b"}]
    # The request left with its job: the next pass, still within budget, keeps the rest.
    assert run(root, max_jobs=10)["pruned"] == []


def test_a_request_never_lifts_a_pin(root):
    """Invariant 1: the pass's own exclusions bind a requested job unchanged."""
    root.job("pinned")
    root.job("running", state="running")
    root.request("pinned")
    root.request("running")             # the daemon refuses this one; a pass must not need it to
    result = run(root, max_jobs=10, pins=lambda: {"pinned"})
    assert result["pruned"] == []
    assert result["pin_reasons"]["pinned"] == "conversation" and result["pin_reasons"]["running"] == "active"
    assert run(root, max_jobs=10)["pruned"] == ["pinned"]         # the pin let go
    assert root.ids() == {"running"}


def test_a_deferred_request_waits_out_its_deferral(root):
    root.job("busy")
    root.request("busy")
    state, clock = retention.RetentionState(), Clock()
    state.defer("busy", 600, "holder: pid 123", clock())
    result = run(root, max_jobs=10, state=state, clock=clock)
    assert result["pruned"] == [] and result["deferred"] == {"busy": "holder: pid 123"}
    clock.advance(601)
    assert run(root, max_jobs=10, state=state, clock=clock)["pruned"] == ["busy"]


def test_requested_jobs_go_after_leftovers_and_before_the_rest(root):
    """Invariant 2, with a batch of one: an orphan lease's job, then the requested job,
    then the oldest of the rest, though the requested job is the newest."""
    for job_id in ("old", "mid", "new"):
        root.job(job_id)
    root.request("new")
    state = retention.RetentionState()
    state.leftovers.add("mid")
    order = [run(root, max_jobs=0, batch=1, state=state)["pruned"] for _ in range(3)]
    assert order == [["mid"], ["new"], ["old"]]


def test_without_a_request_the_oldest_goes_first(root):
    """The control for the test above: the same pool and batch, no request."""
    for job_id in ("old", "mid", "new"):
        root.job(job_id)
    assert [run(root, max_jobs=0, batch=1)["pruned"] for _ in range(3)] == [["old"], ["mid"], ["new"]]


def test_a_request_names_its_job_by_created_at(root):
    """A retirement deletes the rows and leaves the events, so a request a retired job
    left behind must not take a later job that has its id."""
    created = root.job("reused")
    root.request("reused", created_at="2026-09-01T00:00:00Z")       # the earlier job's
    assert run(root, max_jobs=10)["pruned"] == []
    root.request("reused", created_at=created)
    assert run(root, max_jobs=10)["pruned"] == ["reused"]


def test_a_withdrawn_request_retires_nothing(root):
    root.job("kept")
    root.request("kept")
    root.withdraw("kept")
    assert run(root, max_jobs=10)["pruned"] == []
    root.request("kept")
    assert run(root, max_jobs=10)["pruned"] == ["kept"]


def test_requests_fill_the_batch_and_the_pass_says_more_is_waiting(root):
    for job_id in ("a", "b", "c"):
        root.job(job_id)
        root.request(job_id)
    first = run(root, max_jobs=10, batch=2)
    assert first["pruned"] == ["a", "b"] and first["more"] is True
    assert run(root, max_jobs=10, batch=2)["pruned"] == ["c"]


def test_the_deadline_binds_requested_jobs(root, monkeypatch):
    """Invariant 1: once the deadline passes, a pass starts no more jobs, requested or not."""
    for job_id in ("a", "b"):
        root.job(job_id)
        root.request(job_id)
    clock, real = Clock(), retention._Pass._start

    def start(self, job, protected):
        started = real(self, job, protected)
        clock.advance(60)               # the first start takes the pass past its deadline
        return started
    monkeypatch.setattr(retention._Pass, "_start", start)
    result = run(root, max_jobs=10, clock=clock, deadline=clock() + 30)
    assert result["pruned"] == ["a"] and result["more"] is True, result
    assert root.ids() == {"b"}


def test_a_request_in_one_pool_takes_nothing_from_the_other(root):
    root.job("turn-1", kind="turn")
    root.job("detached-1")
    root.request("turn-1")
    result = run(root, max_jobs=10, turn_max_jobs=10, turn_keep_s=0)
    assert result["pruned"] == ["turn-1"] and root.ids() == {"detached-1"}


# --- reading requests ---------------------------------------------------------------

@pytest.mark.parametrize("data_json", ["not json", "{}", "[]", '{"created_at": null}', "null"])
def test_a_request_that_names_no_created_at_is_none(root, data_json):
    """An audit event a transaction writes under the request's kind carries `{}`: read as
    the latest request it would unbind the one before it, so it is no request at all, and
    leaves standing what stood."""
    created = root.job("odd")
    root.event(retention.RETIRE_REQUESTED, "odd", data_json)
    assert retention.retire_requests(root.store.query) == {}
    root.request("odd", created_at=created)
    root.event(retention.RETIRE_REQUESTED, "odd", data_json)
    standing = retention.retire_requests(root.store.query)
    assert standing["odd"]["created_at"] == created
    assert retention.requested(root.store.list_jobs(), standing) == {"odd"}
    assert run(root, max_jobs=10)["pruned"] == ["odd"]


def test_the_request_reads_stay_indexed(root):
    """A pass reads every request by kind (events_kind); the daemon's answer reads one
    job's by that job's own events (events_job), never every request ever made."""
    seen: list[tuple[str, tuple]] = []

    def query(sql, params=()):
        seen.append((sql, params))
        return root.store.query(sql, params)
    retention.retire_requests(query)
    retention.retire_requests(query, "a-job")
    every, one = (" | ".join(row["detail"] for row in root.store.query("EXPLAIN QUERY PLAN " + sql, params))
                  for sql, params in seen)
    assert "events_kind" in every and "SCAN events" not in every, every
    assert "events_job" in one, one


OPS = st.lists(st.tuples(st.sampled_from(["j1", "j2", "j3"]), st.sampled_from(["request", "withdraw"])),
               max_size=12)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=OPS)
def test_each_jobs_latest_request_or_withdrawal_decides(tmp_path_factory, ops):
    """Invariant 3: a request stands exactly when a job's last request-or-withdrawal
    event is a request, whatever other jobs' events come between."""
    made = Root(tmp_path_factory.mktemp("requests"))
    try:
        for job_id in ("j1", "j2", "j3"):
            made.job(job_id)
        last = {}
        for job_id, op in ops:
            (made.request if op == "request" else made.withdraw)(job_id)
            last[job_id] = op
        standing = retention.retire_requests(made.store.query)
        assert set(standing) == {job_id for job_id, op in last.items() if op == "request"}
        assert retention.requested(made.store.list_jobs(), standing) == set(standing)
        for job_id in ("j1", "j2", "j3"):
            assert retention.retire_requests(made.store.query, job_id) == (
                {job_id: standing[job_id]} if job_id in standing else {})
    finally:
        made.close()


# --- a pass takes what the rule says ------------------------------------------------

JOB = st.fixed_dictionaries({
    "state": st.sampled_from([*TERMINAL, "running", "queued"]),
    "pool": st.sampled_from(["detached", "turn"]),
    "requested": st.booleans(),
    "pinned": st.booleans(),
    "deferred": st.booleans(),
})


def expected(jobs: list[dict], budgets: dict[str, int], batch: int, *, requests: bool = True) -> list[str]:
    """The rule C-8.4 and C-8.6 state, written out apart from the code: oldest first,
    requested jobs ahead of the rest; a pin (here: a conversation pin, or a job not yet
    finished) keeps a job, so does a deferral; a job goes while its pool is over its
    budget or it is requested, until the batch is full. `requests=False` is the rule
    before C-8.6."""
    counts = Counter(job["pool"] for job in jobs)
    chosen: list[str] = []
    for job in sorted(jobs, key=lambda job: not (requests and job["requested"])):
        if job["pinned"] or job["state"] not in TERMINAL:
            continue
        if not (counts[job["pool"]] > budgets[job["pool"]] or (requests and job["requested"])):
            continue
        if job["deferred"]:
            continue
        if len(chosen) >= batch:
            break
        chosen.append(job["id"])
        counts[job["pool"]] -= 1
    return chosen


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(jobs=st.lists(JOB, min_size=1, max_size=7), detached=st.integers(0, 7), turn=st.integers(0, 7),
       batch=st.integers(1, 8))
def test_a_pass_retires_what_the_rule_says(tmp_path_factory, jobs, detached, turn, batch):
    """Invariants 1, 2 and 4 against an oracle: one pass retires exactly the jobs the
    rule picks; never a pinned, deferred or unfinished job; each one it retires was
    requested or its pool was over its budget; and with no request it retires what it
    did before C-8.6 (the differential)."""
    made = Root(tmp_path_factory.mktemp("pass"))
    try:
        state, clock = retention.RetentionState(), Clock()
        for n, job in enumerate(jobs):
            job["id"] = f"job-{n}"
            made.job(job["id"], state=job["state"], kind="turn" if job["pool"] == "turn" else "dispatch")
            if job["requested"]:
                made.request(job["id"])
            if job["deferred"]:
                state.defer(job["id"], 600, "busy", clock())
        pinned = {job["id"] for job in jobs if job["pinned"]}
        budgets = {"detached": detached, "turn": turn}
        result = run(made, max_jobs=detached, max_bytes=HUGE, turn_max_jobs=turn, turn_max_bytes=HUGE,
                     turn_keep_s=0, pins=lambda: pinned, state=state, clock=clock, batch=batch)
        want = expected(jobs, budgets, batch)
        assert result["pruned"] == want, (result["pruned"], want)
        by_id = {job["id"]: job for job in jobs}
        assert not [job_id for job_id in result["pruned"] if by_id[job_id]["pinned"] or by_id[job_id]["deferred"]
                    or by_id[job_id]["state"] not in TERMINAL]
        assert result["requested"] == sorted(job_id for job_id in result["pruned"] if by_id[job_id]["requested"])
        assert made.ids() == {job["id"] for job in jobs} - set(want)
        if not any(job["requested"] for job in jobs):
            assert want == expected(jobs, budgets, batch, requests=False)
    finally:
        made.close()


# --- the survey ---------------------------------------------------------------------

def test_the_survey_counts_a_request_as_a_pass_does(root):
    for job_id in ("a", "b", "c"):
        root.job(job_id)
    root.request("c")
    report = survey(root.root, holders=False, sample_throughput=False)
    assert report["would_retire"]["jobs"] == 1 and report["would_retire"]["requested"] == 1
    assert [row["job_id"] for row in report["candidates"]] == ["c"]
    assert run(root)["pruned"] == ["c"]         # the pass agrees (default budget: 500 jobs)


def test_the_survey_takes_a_request_before_the_oldest(root):
    """A pool one over its budget, its newest job requested: that job's retirement brings
    the pool within budget, so the survey, like a pass, retires it alone."""
    for job_id in ("a", "b", "c"):
        root.job(job_id)
    root.request("c")
    report = survey(root.root, holders=False, sample_throughput=False,
                    budgets={"detached": (2, HUGE), "turn": (500, HUGE)})
    assert [row["job_id"] for row in report["candidates"]] == ["c"], report["candidates"]
    assert run(root, max_jobs=2, max_bytes=HUGE)["pruned"] == ["c"]
