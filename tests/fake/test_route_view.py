"""C-6.4, C-11: a route evaluation reads only the attempts and jobs a route can depend on.

Differential: over any store of jobs (with parent chains, including cycles a hand
edit could leave) and attempts in every state, `scheduler.evaluate` reaches the
same decision over the route view as over the full view, for any job, any fleet
and parent cap, and any lane pin.
"""

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import capacity, scheduler
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import ROUTE_ATTEMPTS, ROUTE_JOBS, after, utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)

LANES = ("codex-1", "codex-2", "claude-1", "claude-2")
STATES = ("reserved", "starting", "running", "finalizing", "succeeded", "failed", "cancelled", "lost",
          "quarantined", "interrupted")
FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]


class _Instant(datetime):
    """`datetime` in `subfleet.capacity` only, stopped, so two views built in turn
    stamp the same reading ages (`age_s`) and agree to the last field."""

    at = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.at if tz is None else cls.at.astimezone(tz)


@pytest.fixture
def store_daemon(routing_state, tmp_path, monkeypatch):  # noqa: F811
    service, _ = routing_state
    monkeypatch.setattr(capacity, "datetime", _Instant)
    for identity in LANES[1:]:
        provider = identity.split("-")[0]
        home = tmp_path / identity
        home.mkdir()
        service.store.put_lane(Lane(identity, provider, f"{provider}:{identity}@example.com",
                                    Credential(provider, str(home), "home"), str(home), LaneOwner.V2, False))
    for identity, used in zip(LANES, (.3, .95, .6, .1)):          # codex-2 is below the headroom floor
        service.store.add_reading(Reading(identity, "account", "seven_day", used, after(86400),
                                          ReadingLabel.PROVIDER, "fixture", utcnow()))
    # Stopped after the readings, so their ages are small and not negative and
    # every lane is measured (review of 66d50c17: stopped at import, every reading
    # was in the future and no lane was).
    monkeypatch.setattr(_Instant, "at", datetime.now(timezone.utc) + timedelta(seconds=1))
    return service


#: Parent values a hand edit, an import or an older writer could leave: empty, blank,
#: and a job the store does not hold. `ROUTE_JOBS` keeps a parent `> ''`; the
#: scheduler follows a parent that is truthy.
ODD_PARENTS = ("", " ", "job-99")


def _parent(parent):
    if parent is None or isinstance(parent, str):
        return parent
    return f"job-{parent}"


def lay(service, jobs, attempts):
    """Replace every job and attempt with these. Foreign keys are off while the
    store is laid, so a parent may name no job, as an odd value can."""
    service.store.connection.execute("PRAGMA foreign_keys=OFF")
    try:
        _lay(service, jobs, attempts)
    finally:
        service.store.connection.execute("PRAGMA foreign_keys=ON")


def _lay(service, jobs, attempts):
    with service.store.transaction("fixture.store") as tx:
        for table in ("decisions", "notices", "artifacts", "attempts", "jobs"):
            tx.execute(f"DELETE FROM {table}")
        for index, (parent, state) in enumerate(jobs):
            tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                       "parent_job_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"job-{index}", f"r-{index}", "d", "dispatch", state, "/w", "/w/p.md", "read-only",
                        _parent(parent), f"2026-10-02T00:00:{index:02d}Z"))
        for index, (job, lane, state, evidence) in enumerate(attempts):
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at,"
                       "evidence_json) VALUES(?,?,?,?,?,?,?,?)",
                       (f"job-{job}/a{index}", f"job-{job}", index + 1, lane, "m", state,
                        f"2026-10-02T00:01:{index:02d}Z", json.dumps({"x": "y" * evidence})))


@st.composite
def stores(draw):
    count = draw(st.integers(min_value=1, max_value=12))
    # A parent is any job, earlier or later, so chains and cycles both appear.
    jobs = [(draw(st.one_of(st.none(), st.integers(min_value=0, max_value=count - 1), st.sampled_from(ODD_PARENTS))),
             draw(st.sampled_from(["queued", "waiting", "running", "succeeded", "failed"]))) for _ in range(count)]
    attempts = draw(st.lists(st.tuples(st.integers(min_value=0, max_value=count - 1), st.sampled_from(LANES),
                                       st.sampled_from(STATES), st.sampled_from([0, 9000])), max_size=14))
    return jobs, attempts


candidates = st.fixed_dictionaries(
    {"task": st.sampled_from(["research", "build", "sweep"]), "tier": st.sampled_from(["easy", "standard", "hard"]),
     "sandbox": st.just("read-only")},
    optional={"pinned_lane": st.sampled_from(LANES),
              "parent": st.one_of(st.integers(min_value=0, max_value=11), st.sampled_from(ODD_PARENTS)),
              "existing": st.integers(min_value=0, max_value=11)})


@settings(max_examples=150, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(store=stores(), job=candidates, fleet=st.integers(min_value=1, max_value=6),
       per_parent=st.one_of(st.none(), st.integers(min_value=1, max_value=3)),
       pressure=st.one_of(st.none(), st.sampled_from([10.0, 40.0, 66.1, 300.0])))
def test_a_route_reads_less_and_decides_the_same(store_daemon, store, job, fleet, per_parent, pressure):
    """C-6.4, C-11, C-6.15: the route view's decision equals the full view's, for every
    store and job, with the host-pressure hold off or on at any reading (review of
    17bbb2d6: the hold leaves out ancestors of pending jobs, which the route view
    must still show it)."""
    service = store_daemon
    jobs, attempts = store
    lay(service, jobs, attempts)
    caps = {**service.policy["caps"], "max_active_attempts": fleet}
    if per_parent is not None:
        caps["max_active_attempts_per_parent"] = per_parent
    else:
        caps.pop("max_active_attempts_per_parent", None)
    policy = {**service.policy, "caps": caps}
    if pressure is not None:
        policy["host_pressure"] = {**policy["host_pressure"], "enabled": True, "compressor_max_gib": 40}
    job = dict(job)
    if "existing" in job and job["existing"] < len(jobs):
        job["job_id"] = f"job-{job['existing']}"
        parent = jobs[job["existing"]][0]
        job["parent_job_id"] = _parent(parent)
    elif "parent" in job and (isinstance(job["parent"], str) or job["parent"] < len(jobs)):
        job["parent_job_id"] = _parent(job["parent"])
    job.pop("existing", None), job.pop("parent", None)
    full = service._capacity_view(None)
    route = service._capacity_view(None, route=True)
    assert route["now"] == full["now"]
    if pressure is not None:
        full["host_pressure"] = route["host_pressure"] = {"compressor_bytes": int(pressure * 1024 ** 3), "age_s": 1.0}
    assert sum(1 for lane in full["lanes"] if lane["measured"]) == len(LANES), "the measured branches run"
    try:
        expected = scheduler.evaluate(policy, full, job)
    except (ValueError, scheduler.RouteError) as refusal:
        with pytest.raises(type(refusal)):
            scheduler.evaluate(policy, route, job)
        return
    assert dataclasses.asdict(scheduler.evaluate(policy, route, job)) == dataclasses.asdict(expected)
    # What the route view leaves out it leaves out because it cannot matter.
    assert all(row["state"] in scheduler.ACTIVE_ATTEMPTS for row in route["attempts"])
    assert all(row["parent_job_id"] > "" for row in route["jobs"])
    assert route["in_flight"] == full["in_flight"]


@pytest.mark.parametrize("odd", ODD_PARENTS)
def test_a_parent_cap_counts_the_same_under_any_parent_value(store_daemon, odd):
    """C-6.4, C-11 (review of 66d50c17): a sibling whose parent is empty, blank or a
    missing job counts against the cap exactly as the scheduler follows that value,
    in both views. The property test reaches this only by chance."""
    service = store_daemon
    lay(service, [(odd, "running"), (odd, "running")], [(0, "codex-1", "running", 0)])
    policy = {**service.policy, "caps": {**service.policy["caps"], "max_active_attempts_per_parent": 1}}
    job = {"task": "research", "tier": "hard", "sandbox": "read-only", "job_id": "job-1", "parent_job_id": odd}
    full, route = service._capacity_view(None), service._capacity_view(None, route=True)
    held = scheduler.evaluate(policy, full, job)
    assert dataclasses.asdict(scheduler.evaluate(policy, route, job)) == dataclasses.asdict(held)
    blocked = any(str(block).startswith("parent:") for row in held.evaluations for block in row["capacity_blocks"])
    assert blocked == bool(odd), "a truthy parent is followed and its cap applies; an empty one is no parent"


def test_the_hold_sees_pending_children_through_the_route_view(store_daemon):
    """C-6.15 with C-11.2 (review of 17bbb2d6): a running parent waiting on a pending
    child is left out of the child's count in the route view as in the full view,
    which needs each job's `state` in `ROUTE_JOBS`. Without it the child would be
    held behind its own parent under admission and placed under `daemon.status`."""
    service = store_daemon
    # job-0 runs and waits on job-1, which has not started.
    lay(service, [(None, "running"), (0, "queued")], [(0, "codex-1", "running", 0)])
    policy = {**service.policy, "host_pressure": {**service.policy["host_pressure"], "enabled": True,
                                                    "compressor_max_gib": 40}}
    job = {"task": "research", "tier": "hard", "sandbox": "read-only", "job_id": "job-1", "parent_job_id": "job-0"}
    stranger = {"task": "research", "tier": "hard", "sandbox": "read-only", "job_id": "job-9"}
    views = [service._capacity_view(None), service._capacity_view(None, route=True)]
    for view in views:
        view["host_pressure"] = {"compressor_bytes": 300 * 1024 ** 3, "age_s": 1.0}
    for candidate in (job, stranger):
        full, route = (scheduler.evaluate(policy, view, candidate) for view in views)
        assert dataclasses.asdict(route) == dataclasses.asdict(full)
        assert not any(row.get("host_pressure") for row in route.evaluations), \
            "job-0 waits on a job that has not started, so it holds nothing"
        assert route.chosen_lane is not None


def test_the_route_statements_read_no_evidence_and_use_the_live_index(store_daemon):
    """C-6.4: neither statement names `evidence_json`, and active attempts are found by `attempts_live`."""
    assert "evidence_json" not in ROUTE_ATTEMPTS and "*" not in ROUTE_ATTEMPTS and "*" not in ROUTE_JOBS
    plan = " ".join(row["detail"] for row in store_daemon.store.query("EXPLAIN QUERY PLAN " + ROUTE_ATTEMPTS))
    assert "attempts_live" in plan


def test_pick_reads_the_route_rows_and_status_every_row(store_daemon, monkeypatch):
    """C-6.11, C-6.3: `_pick` (which admission reaches through `_route`, and `why` and
    `run --dry-run` call) reads the route rows; `daemon.status` still carries every job
    and attempt (C-6.11's `subfleet status` counts live ones from it)."""
    service = store_daemon
    lay(service, [(None, "succeeded"), (0, "running")], [(0, "codex-1", "succeeded", 9000), (1, "codex-1", "running", 9000)])
    seen = []
    real = service._capacity_view

    def spy(desktop=None, **options):
        seen.append(options.get("route", False))
        return real(desktop, **options)
    monkeypatch.setattr(service, "_capacity_view", spy)
    service._pick({"task": "research", "tier": "standard", "sandbox": "read-only"})
    status = service.dispatch("daemon.status", {})
    assert seen == [True, False]
    assert {row["job_id"] for row in status["jobs"]} == {"job-0", "job-1"} and len(status["attempts"]) == 2
