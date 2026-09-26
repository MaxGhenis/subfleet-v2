"""C-6.3, C-26.9: admission under commits, and turns first.

Incident, 2026-09-26 (machine at load 110-170, 48 jobs queued): a conversation
turn waited about 725 s queued and then ran in about 20 s. `why` said no
admission pass had reached it 12 minutes in. The lock watch named the admission
thread holding the store lock 7.7 to 12.6 s at a time, every time inside the
reserving transaction's second route evaluation (`_route` -> `_pick` ->
`_capacity_rows`), which ran whenever any commit had landed since the first:
nearly always, with dozens of writers.

These tests run the daemon's own admission in-process, with commits made
between each early evaluation and its reservation.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import capacity, protocol, scheduler
from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading,
                                ReadingLabel)
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.routing_strategies import comparable, event, walked_no_further

ACTIVE = ("reserved", "starting", "running", "finalizing")


def measure(service, lane_id, utilization=.2, observed=None):
    service.store.add_reading(Reading(lane_id, "account", "seven_day", utilization, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", observed or utcnow()))


def add_codex_lanes(service, *lane_ids):
    for lane_id in lane_ids:
        ref = str(service.root / f"home-{lane_id}")
        service.store.put_lane(Lane(lane_id, "codex", f"codex:{lane_id}", Credential("codex", ref, "home"), ref,
                                    LaneOwner.V2, False))


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def submit_turn(service, harness, n, **changes):
    """A conversation turn's job, as the dispatcher submits it (`ConversationService._submit_turn`)."""
    prompt = harness.root / f"turn-{n}.md"
    prompt.write_text("turn")
    args = protocol.SubmitArgs(request_id=f"turn:message-{n}:0", kind="turn", workdir=str(harness.workdir),
                               prompt_path=str(prompt), sandbox="read-only", pinned_model="astra",
                               name=f"turn-conversation-{n}", in_place=True, independent=True, no_preamble=True,
                               max_attempts=1, allow_tmp=True, **changes)
    turn = {"conversation_id": f"conversation-{n}", "message_id": f"message-{n}", "provider": "codex",
            "digest": f"digest-{n}"}
    return service.submit(args, turn=turn)["job_id"]


def reserved(service, job_id):
    return [row["lane_id"] for row in service.store.list_attempts(job_id) if row["state"] == "reserved"]


def elsewhere(fn):
    """Commit from another thread, as a concurrent writer does (C-3.7: never inside a snapshot here)."""
    failures = []

    def run():
        try:
            fn()
        except BaseException as exc:                                   # noqa: BLE001
            failures.append(exc)
    thread = threading.Thread(target=run, name="test-commit")
    thread.start()
    thread.join(300)                                                   # a starved machine is slow, not stuck
    assert not thread.is_alive() and not failures, failures


# --- no view is built with the store lock held --------------------------------------------------

def test_c6_3_no_capacity_view_is_built_with_the_store_lock_held_across_100_reservations(routing_state, monkeypatch):  # noqa: F811
    """100 jobs reserved while other threads commit readings, events and leases between
    every early evaluation and its reservation: no capacity view is read or built, and no
    route evaluated, with the store lock held; every reservation was decided from the
    evaluation made before it and the lanes whose rows changed, none evaluated again."""
    service, harness = routing_state
    add_codex_lanes(service, "codex-2", "codex-3", "codex-4")
    for lane_id in ("codex-1", "codex-2", "codex-3", "codex-4"):
        measure(service, lane_id)
    # Readings fresh for an hour, so a starved run cannot age a lane into `unmeasured`.
    service.policy["caps"].update(max_active_attempts=200, max_in_flight_per_lane=50, reading_ttl_s=3600)
    jobs = [submit(service, harness, pinned_model="astra") for _ in range(100)]
    # No git here: one subprocess per job is not what this measures.
    monkeypatch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
    held: list[str] = []
    rows, build, evaluate = service._capacity_rows, capacity.build_view, scheduler.evaluate

    def watched(name, fn):
        def call(*args, **kwargs):
            if service.store._holds_writer():
                held.append(name)
            return fn(*args, **kwargs)
        return call
    monkeypatch.setattr(service, "_capacity_rows", watched("_capacity_rows", rows))
    monkeypatch.setattr(capacity, "build_view", watched("build_view", build))
    monkeypatch.setattr(scheduler, "evaluate", watched("evaluate", evaluate))
    pick, count = service._pick, [0]

    def pick_then_commit(job, **options):
        decision = pick(job, **options)
        count[0] += 1
        lane = f"codex-{1 + count[0] % 4}"
        elsewhere(lambda: (service.store.add_event("hook.fixture", data={"n": count[0]}),
                           measure(service, lane, .2 + (count[0] % 5) / 100)))
        return decision
    monkeypatch.setattr(service, "_pick", pick_then_commit)
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            with service.store.transaction("test.writer"):
                service.store.add_event("notice.fixture", data={"n": n})
            service.store.acquire_lease(f"test:{n}", "test")
            service.store.release_leases("test")
            time.sleep(.001)
    background = threading.Thread(target=writer, name="test-writer")
    background.start()
    try:
        service._admit()
    finally:
        stop.set()
        background.join(30)
    assert sum(1 for job_id in jobs if reserved(service, job_id)) == 100
    assert held == []
    counts = service._route_evaluations
    assert counts["reused"] + counts["rechosen"] == 100 and counts["again"] == 0   # decided without evaluating again


# --- no cap is exceeded, nothing is double-booked, whatever lands in between ----------------------

LANES = ("codex-1", "codex-2", "codex-3")


def latest(readings):
    by_key = {}
    for row in readings:
        key = (row["scope"], row["window"])
        order = (capacity._time(row["observed_at"]), row["reading_id"])
        if key not in by_key or order > by_key[key][0]:
            by_key[key] = (order, row)
    return [row for _, row in by_key.values()]


def measured_now(service, lane_id, now):
    """C-6.4, stated again: a lane is measured when its newest reading of some scope and
    window is a finite provider utilization, at most `reading_ttl_s` old, not past its reset."""
    ttl = service.policy["caps"]["reading_ttl_s"]
    for row in latest(service.store.query("SELECT * FROM readings WHERE lane_id=?", (lane_id,))):
        observed = capacity._time(row["observed_at"])
        if (row["label"] == "provider" and row["utilization"] is not None
                and 0 <= (now - observed).total_seconds() <= ttl
                and (not row["resets_at"] or capacity._time(row["resets_at"]) > now)):
            return True
    return False


def check_reservation(service, attempt_id):
    """An oracle written apart from `scheduler`: the attempt just reserved had room, on a
    lane that could take it, and nothing is booked twice."""
    store, caps = service.store, service.policy["caps"]
    turns = service.policy.get("conversations") or {}
    attempt = store.get_attempt(attempt_id)
    job = store.get_job(attempt["job_id"])
    lane_id, turn = attempt["lane_id"], job["kind"] == "turn"
    now = datetime.now(timezone.utc)
    live = store.query("SELECT a.*, j.kind FROM attempts a JOIN jobs j USING(job_id) "
                       "WHERE a.state IN ('reserved','starting','running','finalizing') AND a.attempt_id<>?",
                       (attempt_id,))
    pool = [row for row in live if (row["kind"] == "turn") == turn]
    probes = store.query("SELECT * FROM leases WHERE holder LIKE 'probe:%'")
    on_lane = sum(1 for row in pool if row["lane_id"] == lane_id)
    if turn:
        assert on_lane < turns.get("turn_slots_per_lane", 1)
        assert len(pool) < turns.get("max_active_turns", 3)
    else:
        slots = caps["max_in_flight_per_lane"] if measured_now(service, lane_id, now) else min(
            caps["max_in_flight_per_lane"], caps["max_in_flight_unmeasured"], 1)
        assert on_lane < slots, (lane_id, on_lane, slots)
        assert len(pool) + len(probes) < caps["max_active_attempts"]
    lane = store.one("SELECT * FROM lanes WHERE lane_id=?", (lane_id,))
    assert lane["enabled"] and lane["owner"] == "v2"
    model = attempt["model_requested"]
    assert not [row for row in store.query("SELECT * FROM closures WHERE lane_id=? AND released_at IS NULL",
                                           (lane_id,))
                if row["scope"] in ("account", model) and capacity._time(row["until_at"]) > now]
    assert not [row for row in probes if row["lease_key"].split(":")[1] == lane_id]
    slots_held = store.query("SELECT lease_key, holder FROM leases WHERE lease_key LIKE 'lane:%:slot:%' "
                             "AND holder NOT LIKE 'probe:%'")
    holders = [row["holder"] for row in slots_held]
    assert len(holders) == len(set(holders))                    # one slot per attempt
    assert attempt_id in holders
    assert all(row["lease_key"].split(":")[1] == store.get_attempt(row["holder"])["lane_id"] for row in slots_held
               if store.get_attempt(row["holder"]))
    jobs = [row["job_id"] for row in live] + [job["job_id"]]
    assert len(jobs) == len(set(jobs))                          # one attempt at a time per job


@contextlib.contextmanager
def fleet_daemon(root):
    """`routing_state`, for one Hypothesis example: a daemon with codex-1 to codex-3."""
    root.mkdir(parents=True)
    harness = Harness(root)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        service = Daemon(root)
        try:
            add_codex_lanes(service, "codex-2", "codex-3")
            yield service, harness, patch
        finally:
            service.close()


COMMITS = st.sampled_from(["reading", "stale-reading", "full-reading", "close", "close-astra", "release",
                           "end", "reserve-elsewhere", "probe", "unprobe", "disable", "enable", "event"])


def commit(service, what, lane_id, n):
    store = service.store
    if what == "reading":
        measure(service, lane_id, .1 + n % 7 / 10)
    elif what == "stale-reading":
        measure(service, lane_id, .2, (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    elif what == "full-reading":
        measure(service, lane_id, .97)
    elif what in ("close", "close-astra"):
        store.put_closure(Closure(lane_id, "account" if what == "close" else "gpt-6-astra", after(3600),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "test"))
    elif what == "release":
        with store.transaction("test.release") as tx:
            tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND released_at IS NULL", (utcnow(), lane_id))
    elif what == "end":
        row = store.one("SELECT * FROM attempts WHERE lane_id=? AND state IN ('reserved','running') LIMIT 1", (lane_id,))
        if row:
            with store.transaction("test.end") as tx:
                tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (row["attempt_id"],))
                tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (row["job_id"],))
                tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (row["attempt_id"], row["job_id"]))
    elif what == "reserve-elsewhere":
        # Another admission's reservation (the turn pass beside the detached one): into a free slot.
        with store.transaction("test.elsewhere") as tx:
            job_id = f"elsewhere-{n}"
            tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                       "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (job_id, job_id, "x", "dispatch", "running", "/tmp", "/tmp/p", "read-only", utcnow()))
            slot = 0
            while tx.execute("SELECT 1 FROM leases WHERE lease_key=?", (f"lane:{lane_id}:slot:{slot}",)).fetchone():
                slot += 1
            tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                       (f"lane:{lane_id}:slot:{slot}", job_id + "/a1", utcnow()))
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                       "VALUES(?,?,1,?,'gpt-6-astra','running',?)", (job_id + "/a1", job_id, lane_id, utcnow()))
    elif what == "probe":
        if not store.one("SELECT 1 FROM leases WHERE lease_key LIKE ?", (f"lane:{lane_id}:%",)):
            store.acquire_lease(f"lane:{lane_id}:slot:0", "probe:timer:test")
    elif what == "unprobe":
        store.release_leases("probe:timer:test")
    elif what in ("disable", "enable"):
        store.update_lane(lane_id, enabled=int(what == "enable"))
    else:
        store.add_event("hook.fixture", data={"n": n})


def admission_under_commits(data, service, harness, patch, *, checked=None):
    """A random fleet, jobs and turns; random commits after every early evaluation; two
    passes. `checked` wraps the reservation's check, when given. Returns the reservations
    the oracle passed."""
    caps = service.policy["caps"]
    # Readings fresh for an hour: the checks here compare two clocks (the reservation's
    # and the oracle's, or a full evaluation's made after it), and on a starved machine
    # an example can outlast the policy's 120 s, which would move a lane between them.
    caps.update(max_active_attempts=data.draw(st.sampled_from([1, 2, 3, 5]), label="fleet cap"),
                max_in_flight_per_lane=data.draw(st.sampled_from([1, 2]), label="lane cap"), reading_ttl_s=3600)
    service.policy.setdefault("conversations", {}).update(
        max_active_turns=data.draw(st.sampled_from([1, 2]), label="turn cap"), turn_slots_per_lane=1)
    for lane_id in LANES:
        if data.draw(st.booleans(), label=f"{lane_id} measured"):
            measure(service, lane_id, data.draw(st.sampled_from([.1, .5, .9]), label=f"{lane_id} use"))
    for n in range(data.draw(st.integers(0, 2), label="running")):
        commit(service, "reserve-elsewhere", data.draw(st.sampled_from(LANES)), 100 + n)
    jobs = []
    for n in range(data.draw(st.integers(1, 6), label="jobs")):
        if data.draw(st.integers(0, 3), label=f"job {n} is a turn") == 0:
            jobs.append(submit_turn(service, harness, n))
        else:
            jobs.append(submit(service, harness, pinned_model=data.draw(st.sampled_from(["astra", "terra"]))))
    patch.setattr(service.conversations, "launch", lambda *a, **k: (_ for _ in ()).throw(
        AdapterError("test: turns are not launched", code=7)))
    pick, n = Daemon._pick.__get__(service), [0]

    def pick_then_commit(job, **options):
        assert not service.store._holds_writer()                 # C-6.3: never with the store lock held
        decision = pick(job, **options)
        for _ in range(data.draw(st.integers(0, 2), label="commits")):
            n[0] += 1
            what, lane_id = data.draw(COMMITS), data.draw(st.sampled_from(LANES))
            elsewhere(lambda: commit(service, what, lane_id, n[0]))
        return decision
    patch.setattr(service, "_pick", pick_then_commit)
    if checked is not None:
        patch.setattr(service, "_route_stands", checked(service._route_stands, pick))
    boundary, placed = service._boundary, []

    def on_boundary(name, job_id, attempt_id=None):
        if name == "reserved":
            check_reservation(service, attempt_id)
            placed.append(attempt_id)
        return boundary(name, job_id, attempt_id)
    patch.setattr(service, "_boundary", on_boundary)
    for _ in range(2):
        service._admit()
        for job_id in jobs:                                      # every wait due again: a second pass looks
            if service.store.get_job(job_id)["state"] == "waiting":
                service.store.update_job(job_id, next_check_at=utcnow())
    assert all(len([row for row in service.store.list_attempts(job_id) if row["state"] in ACTIVE]) <= 1
               for job_id in jobs)
    event(f"reservations: {min(len(placed), 4)}{'+' if len(placed) >= 4 else ''}")
    return placed


PROPERTY = settings(max_examples=80, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


@PROPERTY
@given(st.data())
def test_c6_3_admission_never_exceeds_a_cap_or_double_books_across_commits(tmp_path_factory, data):
    """Random fleets, jobs and turns, with random commits (readings, closures, attempts
    ending and reserved elsewhere, probes, lanes disabled) between every early evaluation
    and its reservation. No route is evaluated with the store lock held, and at each
    reservation an oracle written apart from `scheduler` finds that it had room, on a
    lane that could take it, and that nothing is booked twice."""
    with fleet_daemon(tmp_path_factory.mktemp("admission") / "state") as (service, harness, patch):
        admission_under_commits(data, service, harness, patch)


@PROPERTY
@given(st.data())
def test_c6_3_a_check_lets_a_decision_stand_exactly_when_a_full_evaluation_there_agrees(tmp_path_factory, data):
    """The same runs, with a full evaluation made at every check, inside the reservation
    (in this test only): the check gives a decision exactly when the lanes whose rows
    changed decide it (the capacity blocks are the early decision's, and the walk goes no
    further), and that decision is the full evaluation's: lane, model, verdict, details
    and evidence. A refusal for the clock (`old`) is the caller's own."""

    def checked(stands, pick):
        def check(basis, decision):
            why, judged, standing = stands(basis, decision)
            if why in (None, "moved", "full"):
                full = pick(basis["job"], desktop=basis["desktop"])
                assert (why is None) == walked_no_further(decision, full), (why, decision.chosen_lane, full.chosen_lane)
                if why is None:
                    assert comparable(standing) == comparable(full)
                    kept = (standing.chosen_lane, standing.chosen_model) == (decision.chosen_lane, decision.chosen_model)
                    why_label = "kept" if kept else "chose again"
                else:
                    why_label = why
            else:
                why_label = why
            event(f"check: {why_label}{' (no lane before)' if not decision.chosen_lane else ''}"
                  f"{', lanes judged again' if judged else ''}")
            return why, judged, standing
        return check
    with fleet_daemon(tmp_path_factory.mktemp("admission") / "state") as (service, harness, patch):
        admission_under_commits(data, service, harness, patch, checked=checked)


# --- a job whose decision keeps moving keeps its place -----------------------------------------

def test_c6_3_a_job_whose_decision_keeps_moving_keeps_its_place(routing_state, monkeypatch):  # noqa: F811
    """Before each of ROUTE_TRIES reservations, another reservation fills the fleet or an
    attempt ends and frees it: a cap that begins or ends takes every lane's rows, so each
    check refuses and the route is evaluated again off the lock. After the last, the job
    is left for the next pass (`route-moved`, `deferred`), holding back the later job it
    competes with (C-6.9); the next pass places it."""
    service, harness = routing_state
    service.policy["caps"].update(max_active_attempts=2, reading_ttl_s=3600)
    add_codex_lanes(service, "codex-2")
    measure(service, "codex-1")
    measure(service, "codex-2")
    first = submit(service, harness, pinned_model="astra")
    second = submit(service, harness, pinned_model="astra")
    pick, moves = service._pick, [0]

    def pick_then_move(job, **options):
        decision = pick(job, **options)
        if job["job_id"] == first and moves[0] < daemon_module.ROUTE_TRIES:
            moves[0] += 1
            elsewhere(lambda: commit(service, "end" if moves[0] % 2 == 0 else "reserve-elsewhere",
                                     "codex-2", moves[0]))
            if moves[0] % 2:
                elsewhere(lambda: commit(service, "reserve-elsewhere", "codex-2", 100 + moves[0]))
        return decision
    monkeypatch.setattr(service, "_pick", pick_then_move)
    service._admit()
    assert service._holds[first] == {"reason": "route-moved", "tries": daemon_module.ROUTE_TRIES}
    assert service._holds[second]["reason"] == "behind-older-job" and service._holds[second]["behind"] == first
    assert not service.store.list_attempts(first) and not service.store.list_attempts(second)
    assert service._route_evaluations["deferred"] == 1 and service._route_evaluations["moved"] == daemon_module.ROUTE_TRIES
    assert service._route_evaluations["again"] == daemon_module.ROUTE_TRIES - 1
    for row in service.store.query("SELECT * FROM attempts WHERE state IN ('reserved','running')"):
        commit(service, "end", row["lane_id"], 0)
    service._admit()
    assert reserved(service, first) and reserved(service, second)


# --- C-26.9: a turn does not wait for detached jobs ------------------------------------------------

def test_c26_9_a_queued_turn_is_placed_before_a_detached_backlog_is_evaluated(routing_state, monkeypatch):  # noqa: F811
    """A turn submitted after 30 queued detached jobs of an earlier tier is placed by the
    pass's turn half, before any of them is evaluated. The single pass sorted turns ahead
    only of their own tier's detached jobs, so it evaluated every `trivial` job first."""
    service, harness = routing_state
    add_codex_lanes(service, "codex-2", "codex-3")
    for lane_id in ("codex-1", "codex-2", "codex-3"):
        measure(service, lane_id)
    service.policy["caps"].update(max_active_attempts=40, max_in_flight_per_lane=20, reading_ttl_s=3600)
    backlog = [submit(service, harness, pinned_model="astra", tier="trivial") for _ in range(30)]
    turn = submit_turn(service, harness, 1)
    monkeypatch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))   # no git per job
    pick, order = service._pick, []

    def recording(job, **options):
        order.append(job["job_id"])
        return pick(job, **options)
    monkeypatch.setattr(service, "_pick", recording)
    service._admit()
    assert order[0] == turn                                         # evaluated first ...
    assert reserved(service, turn)                                  # ... and placed within the one pass
    assert all(reserved(service, job_id) for job_id in backlog)


def test_c26_9_a_turn_is_placed_while_a_detached_pass_is_held_up(routing_state, monkeypatch):  # noqa: F811
    """The control loop runs a turn pass of its own beside the detached pass. A detached
    job's preparation is held (a probe, a slow `git worktree add`, a starved evaluation);
    a turn submitted meanwhile is reserved by the turn pass while the detached pass is
    still held, not after it."""
    service, harness = routing_state
    measure(service, "codex-1")
    detached = submit(service, harness, pinned_model="astra")
    entered, release = threading.Event(), threading.Event()
    prepare = service._prepare_route

    def held(job, decision_job, exclusions):
        if job["job_id"] == detached:
            entered.set()
            release.wait(30)
        return prepare(job, decision_job, exclusions)
    monkeypatch.setattr(service, "_prepare_route", held)
    monkeypatch.setattr(service, "_launch", lambda attempt: None)            # nothing is started here
    monkeypatch.setattr(service.timers, "tick", lambda: None)
    service._recovery_complete.set()
    loop = threading.Thread(target=service._control, name="test-control")
    loop.start()
    try:
        assert entered.wait(20), "the detached pass reached the held job"
        turn = submit_turn(service, harness, 1)
        deadline = time.monotonic() + 20
        while not reserved(service, turn) and time.monotonic() < deadline:
            time.sleep(.02)
        assert reserved(service, turn), "the turn waited for the detached pass"
        assert not release.is_set() and not service.store.list_attempts(detached)
    finally:
        release.set()
        service.stopping.set()
        loop.join(30)
    assert not loop.is_alive()


# --- found by the review of this change ---------------------------------------------------------

def test_c6_3_a_lane_enrolled_under_a_reset_credit_override_is_never_reserved_as_measured(routing_state, monkeypatch):  # noqa: F811
    """Whether a confirmed reset-credit override covers a lane turns on the lane's row (its
    account key, its home), not only on the override history. codex-3 is enrolled after a
    `hard` job's early decision, under an account with a confirmed override, and codex-1 is
    disabled: an evaluation now holds codex-3's fresh reading out, so the lane is unmeasured
    and the job needs its probe first (C-11.4). The check used the early decision's override
    set and reserved codex-3 as measured, with no probe. It now refuses; the route is
    evaluated again off the lock, and the job waits for its probe."""
    service, harness = routing_state
    service.policy["caps"].update(reading_ttl_s=3600)
    measure(service, "codex-1")
    service.store.add_action(action_id="credit-1", kind="reset-credit", op_key="codex:codex-3:credit-1",
                             subject="codex-3", state="confirmed",
                             request_json=json.dumps({"account_key": "codex:codex-3"}))
    job_id = submit(service, harness, pinned_model="astra", tier="hard")
    monkeypatch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
    pick, early = service._pick, []

    def pick_then_commit(job, **options):
        decision = pick(job, **options)
        if not early:
            early.append(decision.chosen_lane)

            def enrol():
                add_codex_lanes(service, "codex-3")
                measure(service, "codex-3", .1)
                service.store.update_lane("codex-1", enabled=0)
            elsewhere(enrol)
        return decision
    monkeypatch.setattr(service, "_pick", pick_then_commit)
    service._admit()
    assert early == ["codex-1"]
    assert not service.store.list_attempts(job_id)             # never placed as measured, without its probe
    assert service._holds[job_id]["reason"] == "probe-pending"
    assert service._route_evaluations["moved"] == 1 and service._route_evaluations["again"] == 1


def test_c6_12_a_row_the_check_cannot_read_settles_its_job_not_the_pass(routing_state, monkeypatch):  # noqa: F811
    """A closure whose `until_at` does not parse is committed after the early evaluation.
    The check inside the reservation cannot read it: the route is evaluated again off the
    lock, where it raises for this job alone (C-6.12, `route`), and the pass goes on."""
    service, harness = routing_state
    service.policy["caps"].update(reading_ttl_s=3600)
    measure(service, "codex-1")
    first = submit(service, harness, pinned_model="astra")
    pick, done = service._pick, []

    def pick_then_commit(job, **options):
        decision = pick(job, **options)
        if not done:
            done.append(True)
            elsewhere(lambda: service.store.put_closure(Closure("codex-1", "gpt-5.6-terra", "not-a-time",
                                                                ClosureReason.PROVIDER_LIMIT, ClockSource.GUESSED,
                                                                "test")))
        return decision
    monkeypatch.setattr(service, "_pick", pick_then_commit)
    service._admit()                                           # does not raise
    assert service._holds[first]["reason"] == "route" and not service.store.list_attempts(first)
    assert service._route_evaluations["again"] == 1


def test_c26_9_a_turn_half_that_raises_still_lets_the_detached_half_run(routing_state, monkeypatch):  # noqa: F811
    """A store error in the turn half is the pass's (C-6.12) and is raised, for C-5.10 to
    retry, but not before the detached half has run: a turn's trouble never stops
    detached jobs, as before the split, when the pass placed an earlier-tier job before
    it met the turn. The turn worker's own pass raises it too."""
    service, harness = routing_state
    measure(service, "codex-1")
    detached = submit(service, harness, pinned_model="astra", tier="trivial")
    submit_turn(service, harness, 1)

    def locked(job):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(service.conversations, "admission_hold", locked)
    with pytest.raises(sqlite3.OperationalError):
        service._admit()
    assert reserved(service, detached)
    with pytest.raises(sqlite3.OperationalError):
        service._admit_turns()


def test_c6_11_a_detached_pass_that_is_placing_never_reads_as_idle(routing_state, monkeypatch):  # noqa: F811
    """The turn pass records admission's state (C-6.11) every tick. A placement made by a
    detached pass still running is counted when it is made, so the turn pass's record says
    placing, not 'none placed for N s'."""
    service, harness = routing_state
    measure(service, "codex-1")
    service.policy.setdefault("conversations", {})["max_active_turns"] = 0     # a turn that stays held,
    submit_turn(service, harness, 1)                                            # so the turn pass runs
    job_id = submit(service, harness, pinned_model="astra")
    boundary, seen = service._boundary, []

    def on_boundary(name, job, attempt_id=None):
        boundary(name, job, attempt_id)
        if name == "reserved" and not seen:
            service._admit_turns()                             # the turn worker's pass, mid-way through this one
            seen.append(service._admission["placed_at"])
    monkeypatch.setattr(service, "_boundary", on_boundary)
    service._admit()
    assert reserved(service, job_id) and seen and seen[0] is not None


def test_c6_11_a_job_left_for_the_next_pass_reports_that_look(routing_state, monkeypatch):  # noqa: F811
    """`recheck` describes the last look: a waiting job whose look ends `route-moved`
    reports that, not the verdict its wait had before."""
    service, harness = routing_state
    service.policy["caps"].update(max_active_attempts=2, reading_ttl_s=3600)
    add_codex_lanes(service, "codex-2")
    measure(service, "codex-1")
    measure(service, "codex-2")
    job_id = submit(service, harness, pinned_model="astra")
    service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=utcnow())
    service._capacity_wait(job_id, "an earlier verdict", {"reason": "below-floor"})
    pick, moves = service._pick, [0]

    def pick_then_move(job, **options):
        decision = pick(job, **options)
        moves[0] += 1
        if moves[0] <= daemon_module.ROUTE_TRIES:
            elsewhere(lambda: commit(service, "end" if moves[0] % 2 == 0 else "reserve-elsewhere", "codex-2", moves[0]))
            if moves[0] % 2:
                elsewhere(lambda: commit(service, "reserve-elsewhere", "codex-2", 100 + moves[0]))
        return decision
    monkeypatch.setattr(service, "_pick", pick_then_move)
    service._admit()
    assert service._holds[job_id]["reason"] == "route-moved"
    assert service._capacity_waits[job_id]["label"] == "route-moved"


def test_c6_3_a_clock_that_steps_back_is_evaluated_again(routing_state, monkeypatch):  # noqa: F811
    """The wall clock steps back 30 s between the early evaluation and its reservation:
    the check reads a clock earlier than the instant the view was built at, where the
    view's judgements need not hold (a reading can have a negative age then). It
    refuses (`old`); the route is evaluated again, off the lock, on the clock as it now
    is, and that decision is reserved."""
    service, harness = routing_state
    measure(service, "codex-1")
    job_id = submit(service, harness, pinned_model="astra")
    step = [timedelta(0)]

    class Stepped(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + step[0]
    monkeypatch.setattr(daemon_module, "datetime", Stepped)
    pick = service._pick

    def pick_then_step_back(job, **options):
        decision = pick(job, **options)
        step[0] = timedelta(seconds=-30)                      # after this evaluation, before its reservation
        return decision
    monkeypatch.setattr(service, "_pick", pick_then_step_back)
    service._admit()
    assert service._route_evaluations["old"] == 1 and service._route_evaluations["again"] == 1
    assert reserved(service, job_id)


def test_c26_9_an_idle_turn_pass_is_one_statement(routing_state, monkeypatch):  # noqa: F811
    """The control loop offers the turn pass every 50 ms tick. With no turn queued and
    none held, it asks one indexed question and does nothing else: no desktop identity,
    no pass, no admission record."""
    service, harness = routing_state
    submit(service, harness, pinned_model="astra")                 # a detached job queued: not the turn pass's
    calls, statements = [], []
    monkeypatch.setattr(service, "_admit_pass", lambda *a, **k: calls.append("pass"))
    monkeypatch.setattr(service, "_desktop_identity", lambda: calls.append("desktop"))
    monkeypatch.setattr(service, "_note_admission", lambda *a, **k: calls.append("note"))
    one, query = service.store.one, service.store.query
    monkeypatch.setattr(service.store, "one", lambda *a, **k: statements.append(a[0]) or one(*a, **k))
    monkeypatch.setattr(service.store, "query", lambda *a, **k: statements.append(a[0]) or query(*a, **k))
    service._admit_turns()
    assert calls == [] and len(statements) == 1
    submit_turn(service, harness, 1)
    service._admit_turns()
    assert calls[:1] == ["pass"]


def test_c6_12_a_check_that_raises_on_every_try_settles_its_job_with_the_error_named(routing_state, monkeypatch):  # noqa: F811
    """A defect in the check itself (it raises whatever the rows) must not become a silent
    `route-moved` stall, looked at and deferred on every pass with nothing said. After
    ROUTE_TRIES it settles the job as an evaluation error does (C-6.12): a `route` wait
    naming the error, backed off, and counted (review of this change)."""
    service, harness = routing_state
    measure(service, "codex-1")
    job_id = submit(service, harness, pinned_model="astra")

    def broken(*args, **kwargs):
        raise KeyError("fixture: a check defect")
    monkeypatch.setattr(daemon_module.route_check, "still_stands", broken)
    service._admit()
    hold = service._holds[job_id]
    assert hold["reason"] == "route" and hold["error_type"] == "KeyError"
    assert service.store.get_job(job_id)["next_check_at"] > utcnow()   # backed off (C-6.12)
    assert service._route_evaluations["error"] == daemon_module.ROUTE_TRIES
    assert not service.store.list_attempts(job_id)
