"""C-6.4, C-11, C-26.9: a route evaluation reads only the attempts and jobs a route can depend on.

Differential: over any store of jobs (detached and turns, with parent chains,
cycles and odd parent values a hand edit could leave) and attempts in every
state, `scheduler.evaluate` reaches the same decision over the route view as over
the full view, for any job (detached or a turn), any fleet, parent and turn cap,
and any lane pin.
"""

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import scheduler
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import ROUTE_ATTEMPTS, ROUTE_JOBS, after, utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)

LANES = ("codex-1", "codex-2", "claude-1", "claude-2")
STATES = ("reserved", "starting", "running", "finalizing", "succeeded", "failed", "cancelled", "lost",
          "quarantined", "interrupted")
FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
#: Parent values a hand edit, an import or an older writer could leave: empty, blank,
#: and a job the store does not hold. `ROUTE_JOBS` keeps a parent `> ''`; the
#: scheduler follows a parent that is truthy.
ODD_PARENTS = ("", " ", "job-99")


@pytest.fixture
def store_daemon(routing_state, tmp_path):  # noqa: F811
    service, _ = routing_state
    for identity in LANES[1:]:
        provider = identity.split("-")[0]
        home = tmp_path / identity
        home.mkdir()
        service.store.put_lane(Lane(identity, provider, f"{provider}:{identity}@example.com",
                                    Credential(provider, str(home), "home"), str(home), LaneOwner.V2, False))
    for identity, used in zip(LANES, (.3, .95, .6, .1)):          # codex-2 is below the headroom floor
        service.store.add_reading(Reading(identity, "account", "seven_day", used, after(86400),
                                          ReadingLabel.PROVIDER, "fixture", utcnow()))
    return service


def _parent(parent):
    if parent is None or isinstance(parent, str):
        return parent
    return f"job-{parent}"


def lay(service, jobs, attempts):
    """Replace every job and attempt with these. Foreign keys are off while the
    store is laid, so a parent may name no job, as an odd value can."""
    service.store.connection.execute("PRAGMA foreign_keys=OFF")
    try:
        with service.store.transaction("fixture.store") as tx:
            for table in ("decisions", "notices", "artifacts", "attempts", "jobs"):
                tx.execute(f"DELETE FROM {table}")
            for index, (parent, state, kind) in enumerate(jobs):
                tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                           "parent_job_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (f"job-{index}", f"r-{index}", "d", kind, state, "/w", "/w/p.md", "read-only",
                            _parent(parent), f"2026-10-02T00:00:{index:02d}Z"))
            for index, (job, lane, state, evidence) in enumerate(attempts):
                tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at,"
                           "evidence_json) VALUES(?,?,?,?,?,?,?,?)",
                           (f"job-{job}/a{index}", f"job-{job}", index + 1, lane, "m", state,
                            f"2026-10-02T00:01:{index:02d}Z", json.dumps({"x": "y" * evidence})))
    finally:
        service.store.connection.execute("PRAGMA foreign_keys=ON")


@st.composite
def stores(draw):
    count = draw(st.integers(min_value=1, max_value=12))
    # A parent is any job, earlier or later, so chains and cycles both appear.
    jobs = [(draw(st.one_of(st.none(), st.integers(min_value=0, max_value=count - 1), st.sampled_from(ODD_PARENTS))),
             draw(st.sampled_from(["queued", "waiting", "running", "succeeded", "failed"])),
             draw(st.sampled_from(["dispatch", "dispatch", "turn"]))) for _ in range(count)]
    attempts = draw(st.lists(st.tuples(st.integers(min_value=0, max_value=count - 1), st.sampled_from(LANES),
                                       st.sampled_from(STATES), st.sampled_from([0, 9000])), max_size=14))
    return jobs, attempts


candidates = st.fixed_dictionaries(
    {"task": st.sampled_from(["research", "build", "sweep"]), "tier": st.sampled_from(["easy", "standard", "hard"]),
     "sandbox": st.just("read-only"), "kind": st.sampled_from(["dispatch", "turn"])},
    optional={"pinned_lane": st.sampled_from(LANES),
              "parent": st.one_of(st.integers(min_value=0, max_value=11), st.sampled_from(ODD_PARENTS)),
              "existing": st.integers(min_value=0, max_value=11)})


def views(service):
    """The full view and the route view, built at one instant after the readings."""
    instant = datetime.now(timezone.utc) + timedelta(seconds=1)
    full = service._capacity_view(None, full_ledger_rows(service), now=instant)
    route = service._capacity_view(None, service._capacity_rows(route=True), now=instant)
    return full, route


def full_ledger_rows(service):
    """The independent full-ledger oracle, even when operator reads are bounded."""
    with service.store.snapshot():
        rows = service._capacity_rows()
        rows["view"]["attempts"] = service.store.list_attempts()
        rows["view"]["jobs"] = service.store.query("SELECT * FROM jobs ORDER BY created_at,rowid")
        return rows


@settings(max_examples=150, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(store=stores(), job=candidates, fleet=st.integers(min_value=1, max_value=6),
       per_parent=st.one_of(st.none(), st.integers(min_value=1, max_value=3)),
       turns=st.one_of(st.none(), st.integers(min_value=1, max_value=4)),
       turn_slots=st.one_of(st.none(), st.integers(min_value=1, max_value=2)))
def test_a_route_reads_less_and_decides_the_same(store_daemon, store, job, fleet, per_parent, turns, turn_slots):
    """C-6.4, C-11, C-26.9: the route view's decision equals the full view's, for
    every store and job, detached or a turn."""
    service = store_daemon
    jobs, attempts = store
    lay(service, jobs, attempts)
    caps = {**service.policy["caps"], "max_active_attempts": fleet}
    if per_parent is not None:
        caps["max_active_attempts_per_parent"] = per_parent
    else:
        caps.pop("max_active_attempts_per_parent", None)
    policy = {**service.policy, "caps": caps}
    if turns is not None or turn_slots is not None:
        policy["conversations"] = {**(policy.get("conversations") or {}),
                                   **({"max_active_turns": turns} if turns is not None else {}),
                                   **({"turn_slots_per_lane": turn_slots} if turn_slots is not None else {})}
    job = dict(job)
    if "existing" in job and job["existing"] < len(jobs):
        job["job_id"] = f"job-{job['existing']}"
        job["parent_job_id"] = _parent(jobs[job["existing"]][0])
        job["kind"] = jobs[job["existing"]][2]
    elif "parent" in job and (isinstance(job["parent"], str) or job["parent"] < len(jobs)):
        job["parent_job_id"] = _parent(job["parent"])
    job.pop("existing", None), job.pop("parent", None)
    full, route = views(service)
    assert route["now"] == full["now"]
    assert sum(1 for lane in full["lanes"] if lane.get("measured")) == len(LANES), "the measured branches run"
    try:
        expected = scheduler.evaluate(policy, full, job)
    except (ValueError, scheduler.RouteError) as refusal:
        with pytest.raises(type(refusal)):
            scheduler.evaluate(policy, route, job)
        return
    assert dataclasses.asdict(scheduler.evaluate(policy, route, job)) == dataclasses.asdict(expected)
    # What the route view leaves out it leaves out because it cannot matter.
    assert all(row["state"] in scheduler.ACTIVE_ATTEMPTS for row in route["attempts"])
    active_jobs = {row["job_id"] for row in route["attempts"]}
    assert all((row["parent_job_id"] or "") > "" or row["job_id"] in active_jobs for row in route["jobs"])
    assert route["in_flight"] == full["in_flight"] and route["in_flight_turns"] == full["in_flight_turns"]


@settings(max_examples=80, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(store=stores(), existing=st.integers(min_value=0, max_value=11),
       per_parent=st.one_of(st.none(), st.integers(min_value=1, max_value=3)))
def test_the_reservation_recheck_reads_the_same_from_the_route_rows(store_daemon, store, existing, per_parent):
    """C-6.3 with C-11.2 (port review of a4b10b83): the reserving transaction's recheck
    (`_route_rows`, `_route_stands`) takes its known jobs from the rows `_pick` read.
    From the route rows it finds the same jobs, reading the rest by id, and reaches
    the same answer as from every row."""
    service = store_daemon
    jobs, attempts = store
    lay(service, jobs, attempts)
    index = existing % len(jobs)
    if per_parent is not None:
        service.policy = {**service.policy, "caps": {**service.policy["caps"],
                                                     "max_active_attempts_per_parent": per_parent}}
    job = {"task": "research", "tier": "hard", "sandbox": "read-only", "job_id": f"job-{index}",
           "parent_job_id": _parent(jobs[index][0]), "kind": jobs[index][2]}
    basis = {}
    try:
        decision = service._pick(job, basis=basis)
    except (ValueError, scheduler.RouteError):
        return
    full = {**basis, "rows": full_ledger_rows(service)}
    now = datetime.now(timezone.utc)
    narrow_rows, full_rows = service._route_rows(basis, now), service._route_rows(full, now)
    assert (narrow_rows is None) == (full_rows is None)
    if narrow_rows is None:
        return
    key = lambda rows: {row["job_id"]: dict(row) for row in rows["jobs"]}          # noqa: E731
    assert key(narrow_rows) == key(full_rows)
    assert service._route_stands(basis, decision)[0] == service._route_stands(full, decision)[0]


@pytest.mark.parametrize("odd", ODD_PARENTS)
def test_a_parent_cap_counts_the_same_under_any_parent_value(store_daemon, odd):
    """C-6.4, C-11: a sibling whose parent is empty, blank or a missing job counts
    against the cap exactly as the scheduler follows that value, in both views."""
    service = store_daemon
    lay(service, [(odd, "running", "dispatch"), (odd, "running", "dispatch")], [(0, "codex-1", "running", 0)])
    policy = {**service.policy, "caps": {**service.policy["caps"], "max_active_attempts_per_parent": 1}}
    job = {"task": "research", "tier": "hard", "sandbox": "read-only", "job_id": "job-1", "parent_job_id": odd}
    full, route = views(service)
    held = scheduler.evaluate(policy, full, job)
    assert dataclasses.asdict(scheduler.evaluate(policy, route, job)) == dataclasses.asdict(held)
    blocked = any(str(block).startswith("parent:") for row in held.evaluations for block in row["capacity_blocks"])
    assert blocked == bool(odd)


def test_a_turns_attempt_counts_as_a_turn_in_the_route_view(store_daemon):
    """C-26.9: an active attempt of a turn job with no parent is counted as a turn in
    both views, which needs the route view to keep the jobs of active attempts."""
    service = store_daemon
    lay(service, [(None, "running", "turn"), (None, "running", "dispatch")],
        [(0, "codex-1", "running", 0), (1, "codex-1", "running", 0)])
    full, route = views(service)
    assert route["in_flight"] == full["in_flight"] == {**{lane: 0 for lane in LANES}, "codex-1": 1}
    assert route["in_flight_turns"] == full["in_flight_turns"] == {**{lane: 0 for lane in LANES}, "codex-1": 1}


def test_the_route_statements_read_no_evidence_and_use_the_live_index(store_daemon):
    """C-6.4: neither statement names `evidence_json`, and active attempts are found by `attempts_live`."""
    assert "evidence_json" not in ROUTE_ATTEMPTS and "*" not in ROUTE_ATTEMPTS and "*" not in ROUTE_JOBS
    plan = " ".join(row["detail"] for row in store_daemon.store.query("EXPLAIN QUERY PLAN " + ROUTE_ATTEMPTS))
    assert "attempts_live" in plan
    # Both halves of `ROUTE_JOBS` by index (a multi-index OR), never a scan of every job.
    plan = " ".join(row["detail"] for row in store_daemon.store.query("EXPLAIN QUERY PLAN " + ROUTE_JOBS))
    assert "jobs_parent" in plan and "attempts_live" in plan and "SCAN jobs" not in plan, plan


def test_pick_reads_the_route_rows_and_status_live_rows(store_daemon, monkeypatch):
    """C-6.11, C-6.3: `_pick` (which admission reaches through `_route`, and `why` and
    `run --dry-run` call) reads the route rows; `daemon.status` carries only live
    jobs and attempts, without evidence."""
    service = store_daemon
    lay(service, [(None, "succeeded", "dispatch"), (0, "running", "dispatch")],
        [(0, "codex-1", "succeeded", 9000), (1, "codex-1", "running", 9000)])
    seen = []
    real = service._capacity_rows

    def spy(**options):
        seen.append(options.get("route", False))
        return real(**options)
    monkeypatch.setattr(service, "_capacity_rows", spy)
    service._pick({"task": "research", "tier": "standard", "sandbox": "read-only"})
    status = service.dispatch("daemon.status", {})
    assert seen[0] is True and seen[1:] and not any(seen[1:]), seen
    assert {row["job_id"] for row in status["jobs"]} == {"job-1"}
    assert {row["attempt_id"] for row in status["attempts"]} == {"job-1/a1"}
    assert all("evidence_json" not in row for row in status["attempts"])
