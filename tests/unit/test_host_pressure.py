"""C-6.15: the opt-in host-pressure hold, its reading, and what it can never do."""

import copy
import threading

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import host_pressure
from subfleet.host_pressure import GIB, Sampler, parse_vm_stat
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from subfleet.scheduler import (ACTIVE_ATTEMPTS, RouteError, dominant_rejection, evaluate,
                                host_pressure_evidence, host_pressure_hold, in_flight_beside,
                                verdict_signature)

NOW = "2026-09-05T10:33:00Z"
TOMORROW = "2026-09-06T10:33:00Z"

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                   255515.
Pages active:                                1614882.
Pages stored in compressor:                  9521784.
Pages occupied by compressor:                4334768.
Decompressions:                           5805850092.
"""


def _policy(**pressure):
    loaded = load_policy(DEFAULT_POLICY_PATH)
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}
    loaded["host_pressure"] = {**loaded["host_pressure"], **pressure}
    return loaded


POLICY_OFF = _policy()
POLICY_ON = _policy(enabled=True)
LANES = ("claude-1", "claude-2", "claude-3", "codex-1", "codex-2")


def _lane(identity):
    provider = identity.split("-")[0]
    return {"lane_id": identity, "provider": provider, "account_key": f"{provider}:{identity}@example.com",
            "owner": "v2", "enabled": True, "desktop": False, "home": f"/lanes/{identity}"}


def _reading(identity, utilization):
    return {"lane_id": identity, "scope": "account", "window": "seven_day", "utilization": utilization,
            "resets_at": TOMORROW, "observed_at": NOW, "label": "provider", "source": "probe"}


def _view(attempts=(), utilization=None, compressor_gib=None, *, jobs=(), probes=(), counted=True, **extra):
    """A view as the daemon lays it: the attempt rows, the per-lane counts `build_view`
    takes from them (`counted`), the jobs, and the lanes a probe holds."""
    attempts = list(attempts)
    view = {"now": NOW, "lanes": [_lane(identity) for identity in LANES],
            "readings": [_reading(identity, (utilization or {}).get(identity, .3)) for identity in LANES],
            "closures": [], "attempts": attempts, "jobs": list(jobs),
            "unavailable_lanes": {identity: f"probe:{identity}" for identity in probes},
            "reserved_probes": len(probes), **extra}
    if counted:
        view["in_flight"] = {identity: sum(1 for row in attempts if row["lane_id"] == identity
                                           and row["state"] in ACTIVE_ATTEMPTS) for identity in LANES}
    if compressor_gib is not None:
        view["host_pressure"] = {"compressor_bytes": int(compressor_gib * GIB), "age_s": 1.0}
    return view


def _attempt(index, identity, state, job_id=None):
    job_id = job_id or f"job-{index}"
    return {"attempt_id": f"{job_id}/a1", "job_id": job_id, "lane_id": identity, "state": state}


JOB = {"task": "research", "tier": "standard", "sandbox": "read-only"}

attempts = st.lists(st.tuples(st.sampled_from(LANES),
                              st.sampled_from(sorted(ACTIVE_ATTEMPTS) + ["succeeded", "failed", "quarantined"])),
                    max_size=6).map(lambda rows: [_attempt(index, lane, state) for index, (lane, state) in enumerate(rows)])
occupancy = st.one_of(st.none(), st.floats(min_value=0, max_value=512, allow_nan=False))
utilizations = st.dictionaries(st.sampled_from(LANES), st.floats(min_value=0, max_value=1, allow_nan=False))
jobs = st.fixed_dictionaries({"task": st.sampled_from(["research", "build", "sweep", "authored-prose"]),
                              "tier": st.sampled_from(["trivial", "easy", "standard", "hard"]),
                              "sandbox": st.just("read-only")},
                             optional={"pinned_lane": st.sampled_from(LANES)})
probes = st.lists(st.sampled_from(LANES), unique=True, max_size=2)
#: A chain of jobs each the parent of the next; the job under evaluation is the last one's child.
lineage = st.lists(st.sampled_from(["grandparent", "parent"]), unique=True, max_size=2).map(sorted)


def _active(rows):
    return sum(1 for row in rows if row["state"] in ACTIVE_ATTEMPTS)


def _decide(policy, view, job):
    """The decision, or the refusal a job gets whatever the host is doing (a pin no model can serve)."""
    try:
        return evaluate(policy, view, job)
    except (RouteError, ValueError) as refusal:
        return type(refusal), str(refusal)


# --- the reading ---------------------------------------------------------------

def test_vm_stat_gives_the_bytes_the_compressor_occupies():
    """C-6.15: page size times `Pages occupied by compressor`, as `vm_stat` prints them."""
    assert parse_vm_stat(VM_STAT) == 4334768 * 16384
    assert round(parse_vm_stat(VM_STAT) / GIB, 1) == 66.1


@pytest.mark.parametrize("text", ["", "Pages occupied by compressor: 12.\n",
                                  "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 3.\n",
                                  "(page size of 16384 bytes)\nPages stored in compressor: 9.\n"])
def test_a_vm_stat_that_does_not_say_is_no_reading(text):
    """C-6.15: without both the page size and the occupied line there is no reading."""
    assert parse_vm_stat(text) is None


def test_an_unreadable_vm_stat_is_no_reading(monkeypatch):
    """C-6.15: a `vm_stat` that cannot run is a host that could not be read, not an error."""
    def refuse(argv, **options):
        raise host_pressure.procs.InspectionError("vm_stat inspection unavailable")
    monkeypatch.setattr(host_pressure.procs, "_read", refuse)
    assert host_pressure.read_compressor_bytes() is None


@settings(max_examples=200, deadline=None)
@given(steps=st.lists(st.tuples(st.floats(min_value=0, max_value=40, allow_nan=False), st.booleans()),
                      min_size=1, max_size=80),
       sample_s=st.sampled_from([1.0, 5.0, 15.0]),
       values=st.lists(st.one_of(st.none(), st.integers(min_value=0, max_value=600 * GIB)), min_size=1, max_size=80))
def test_the_host_is_read_at_most_once_an_interval_and_a_stale_reading_is_none(steps, sample_s, values):
    """C-6.15: `vm_stat` begins at most once per `sample_s` however often it is asked
    for and whatever it answered, and a reading is given only while it is no older
    than `STALE_AFTER` intervals. Time passes with and without a read being asked for."""
    clock, reads, script = [100.0], [], iter(values)

    def read():
        reads.append(clock[0])
        return next(script, None)
    sampler = Sampler(read, lambda: clock[0])
    last = None
    for gap, ask in steps:
        clock[0] += gap
        if ask:
            due, before = sampler.due(sample_s), len(reads)
            sampler.refresh(sample_s)
            assert (len(reads) > before) == due
            if len(reads) > before:
                value = values[before] if before < len(values) else None
                last = (clock[0], value) if value is not None else None
        answer = sampler.reading(sample_s)
        if last is None or clock[0] - last[0] > host_pressure.STALE_AFTER * sample_s:
            assert answer is None
        else:
            assert answer["compressor_bytes"] == last[1] and answer["age_s"] == round(clock[0] - last[0], 1)
    assert all(later - earlier >= sample_s for earlier, later in zip(reads, reads[1:]))


def test_a_reading_goes_stale_with_nobody_reading():
    """C-6.15: four intervals after it was read a reading is still evidence; just past that it is none."""
    clock = [50.0]
    sampler = Sampler(lambda: 70 * GIB, lambda: clock[0])
    sampler.refresh(15)
    clock[0] = 50.0 + 4 * 15
    assert sampler.reading(15) == {"compressor_bytes": 70 * GIB, "age_s": 60.0}
    clock[0] += .001
    assert sampler.reading(15) is None


def test_a_read_that_is_running_is_not_started_again_and_its_value_is_kept():
    """C-6.15: a caller that finds a read running returns at once, and no read can begin
    between this read ending and its value being kept."""
    started, release, values = threading.Event(), threading.Event(), iter([70 * GIB, 10 * GIB])
    reads = []

    def read():
        reads.append(1)
        started.set()
        assert release.wait(10)
        return next(values)
    sampler = Sampler(read, lambda: 0.0)              # a clock that never moves: only the flag rations
    worker = threading.Thread(target=sampler.refresh, args=(0,))
    worker.start()
    assert started.wait(10)
    assert sampler.due(0) is False
    sampler.refresh(0)                                # returns without reading, and without waiting
    assert reads == [1] and sampler.reading(15) is None
    release.set()
    worker.join(10)
    assert sampler.reading(15)["compressor_bytes"] == 70 * GIB and sampler.due(0) is True


# --- what the hold can never do --------------------------------------------------

@settings(max_examples=300, deadline=None)
@given(job=jobs, rows=attempts, gib=occupancy, usage=utilizations, held_by=probes, counted=st.booleans(),
       above=lineage, limit=st.floats(min_value=.5, max_value=256, allow_nan=False))
def test_with_nothing_in_flight_beside_the_job_pressure_never_changes_a_decision(job, rows, gib, usage, held_by,
                                                                               counted, above, limit):
    """C-6.15: the hold never blocks a job while nothing it could wait for is in
    flight. With no attempt reserved, starting, running or finalizing other than
    those of the job's own ancestors, the decision is the one a policy without the
    hold reaches, whatever the compressor holds, whatever the threshold, whatever
    lanes a probe holds, and whether or not the view carries per-lane counts."""
    idle = [row for row in rows if row["state"] not in ACTIVE_ATTEMPTS]
    # Each ancestor's own attempt is running: a parent waiting for this job.
    family = [{"job_id": name, "parent_job_id": above[index - 1] if index else None}
              for index, name in enumerate(above)]
    running = [_attempt(90 + index, LANES[index], "running", job_id=name) for index, name in enumerate(above)]
    job = {**job, "job_id": "child", **({"parent_job_id": above[-1]} if above else {})}
    policy = {**POLICY_ON, "host_pressure": {**POLICY_ON["host_pressure"], "compressor_max_gib": limit}}
    view = lambda: _view(idle + running, usage, gib, jobs=family, probes=held_by, counted=counted)   # noqa: E731
    held = _decide(policy, view(), job)
    free = _decide(POLICY_OFF, view(), job)
    if isinstance(free, tuple):
        assert held == free
        return
    assert (held.chosen_lane, held.chosen_model, held.chain) == (free.chosen_lane, free.chosen_model, free.chain)
    assert verdict_signature({**held.__dict__, "policy_hash": ""}) == verdict_signature({**free.__dict__, "policy_hash": ""})
    assert all("host-pressure" not in row["capacity_blocks"] and "host_pressure" not in row
               for row in held.evaluations)


@settings(max_examples=150, deadline=None)
@given(job=jobs, rows=attempts, gib=occupancy, usage=utilizations)
def test_a_policy_that_leaves_it_off_decides_as_if_there_were_no_reading(job, rows, gib, usage):
    """C-6.15: off, the default, a reading in the view changes nothing at all."""
    assert _decide(POLICY_OFF, _view(rows, usage, gib), job) == _decide(POLICY_OFF, _view(rows, usage), job)
    # And a policy that never heard of the section decides as the shipped one does.
    bare = {key: value for key, value in POLICY_OFF.items() if key != "host_pressure"}
    assert _decide(bare, _view(rows, usage, gib), job) == _decide(POLICY_OFF, _view(rows, usage, gib), job)


@settings(max_examples=300, deadline=None)
@given(job=jobs, rows=attempts, gib=occupancy, usage=utilizations, held_by=probes, counted=st.booleans())
def test_the_hold_only_ever_withholds_a_lane(job, rows, gib, usage, held_by, counted):
    """C-6.15: under the hold a job is placed where it would have been or nowhere,
    and it is withheld exactly when the reading is above the threshold while an
    attempt is in flight. A probe's reservation is not an attempt and holds nothing."""
    held = _decide(POLICY_ON, _view(rows, usage, gib, probes=held_by, counted=counted), job)
    free = _decide(POLICY_OFF, _view(rows, usage, gib, probes=held_by, counted=counted), job)
    if isinstance(free, tuple):
        assert held == free
        return
    above = gib is not None and int(gib * GIB) > 40 * GIB and _active(rows) > 0
    blocked = any("host-pressure" in row["capacity_blocks"] for row in held.evaluations)
    assert blocked == above
    if above:
        assert held.chosen_lane is None
        assert host_pressure_evidence(held) == {"compressor_gib": round(int(gib * GIB) / GIB, 1),
                                                "compressor_max_gib": 40}
    else:
        assert (held.chosen_lane, held.chosen_model) == (free.chosen_lane, free.chosen_model)
        assert host_pressure_evidence(held) == {}


@settings(max_examples=200, deadline=None)
@given(active=st.integers(min_value=-3, max_value=50), low=st.floats(min_value=0, max_value=512, allow_nan=False),
       extra=st.floats(min_value=0, max_value=512, allow_nan=False),
       limit=st.floats(min_value=.5, max_value=256, allow_nan=False))
def test_the_hold_is_monotone_in_occupancy_and_never_holds_an_idle_fleet(active, low, extra, limit):
    """C-6.15: more occupied never releases what less held; nothing in flight, nothing held."""
    policy = {"host_pressure": {"enabled": True, "compressor_max_gib": limit}}

    def held(gib):
        return host_pressure_hold(policy, {"host_pressure": {"compressor_bytes": int(gib * GIB)}}, active) is not None
    if held(low):
        assert held(low + extra)
    if active <= 0:
        assert not held(low) and not held(low + extra)
    assert not host_pressure_hold(policy, {}, active)                          # no reading
    assert not host_pressure_hold(policy, {"host_pressure": None}, active)     # an unreadable host
    assert not host_pressure_hold({"host_pressure": {"enabled": False, "compressor_max_gib": limit}},
                                  {"host_pressure": {"compressor_bytes": 10 ** 15}}, active)


@settings(max_examples=200, deadline=None)
@given(job=jobs, rows=attempts, usage=utilizations, low=st.floats(min_value=0, max_value=300, allow_nan=False),
       extra=st.floats(min_value=0, max_value=300, allow_nan=False))
def test_through_evaluate_more_occupied_never_places_what_less_withheld(job, rows, usage, low, extra):
    """C-6.15: monotone in the whole evaluation, not only in the helper."""
    lower = _decide(POLICY_ON, _view(rows, usage, low), job)
    higher = _decide(POLICY_ON, _view(rows, usage, low + extra), job)
    if isinstance(lower, tuple):
        assert higher == lower
        return
    if lower.chosen_lane is None:
        assert higher.chosen_lane is None
    assert higher.chosen_lane in (None, lower.chosen_lane)


# --- a parent and its child ---------------------------------------------------------

def test_a_child_is_not_held_behind_the_parent_that_waits_for_it():
    """C-6.15 (review of 5ef95154): `subfleet run --parent`, then `subfleet wait`: the
    parent's attempt ends only when the child has, so it does not hold the child."""
    parent = [_attempt(0, "claude-1", "running", job_id="parent")]
    family = [{"job_id": "parent", "parent_job_id": None}]
    child = {**JOB, "job_id": "child", "parent_job_id": "parent"}
    policy = {**POLICY_ON, "caps": {**POLICY_ON["caps"], "max_active_attempts_per_parent": 8}}
    decision = evaluate(policy, _view(parent, compressor_gib=200, jobs=family), child)
    assert decision.chosen_lane is not None
    # A stranger's attempt beside it does hold the child, and an unrelated job is held by the parent's.
    stranger = parent + [_attempt(1, "claude-2", "running")]
    assert evaluate(policy, _view(stranger, compressor_gib=200, jobs=family), child).chosen_lane is None
    assert evaluate(policy, _view(parent, compressor_gib=200, jobs=family), JOB).chosen_lane is None


def test_the_count_leaves_out_every_ancestor_and_nothing_else():
    """C-6.15: a grandparent's attempt is the job's to finish for too; a sibling's is not."""
    family = [{"job_id": "grandparent", "parent_job_id": None}, {"job_id": "parent", "parent_job_id": "grandparent"},
              {"job_id": "sibling", "parent_job_id": "parent"}]
    rows = [_attempt(0, "claude-1", "running", job_id="grandparent"), _attempt(1, "claude-2", "starting", job_id="parent"),
            _attempt(2, "claude-3", "running", job_id="sibling"), _attempt(3, "codex-1", "succeeded"),
            _attempt(4, "codex-2", "quarantined")]
    view = _view(rows, jobs=family)
    counts = view["in_flight"]
    assert in_flight_beside(view, {"job_id": "child", "parent_job_id": "parent"}, counts) == 1
    assert in_flight_beside(view, {"job_id": "other"}, counts) == 3
    # A cycle in the parent links ends the walk; a view with counts only counts them all.
    loop = [{"job_id": "a", "parent_job_id": "b"}, {"job_id": "b", "parent_job_id": "a"}]
    assert in_flight_beside(_view(rows, jobs=loop), {"job_id": "c", "parent_job_id": "a"}, counts) == 3
    assert in_flight_beside({"in_flight": counts}, {"job_id": "child", "parent_job_id": "parent"}, counts) == 3


def test_two_waiting_parents_hold_neither_child():
    """C-6.15 (review of 15cc9f7e): with P1 and P2 running and each one's child
    pending, each child's count leaves out both parents; so does an unrelated job's,
    since both parents wait on jobs that have not started."""
    rows = [_attempt(0, "claude-1", "running", job_id="p1"), _attempt(1, "claude-2", "running", job_id="p2")]
    family = [{"job_id": "p1", "parent_job_id": None, "state": "running"},
              {"job_id": "p2", "parent_job_id": None, "state": "running"},
              {"job_id": "c1", "parent_job_id": "p1", "state": "queued"},
              {"job_id": "c2", "parent_job_id": "p2", "state": "waiting"}]
    view = _view(rows, compressor_gib=200, jobs=family)
    for job in ({**JOB, "job_id": "c1", "parent_job_id": "p1"}, {**JOB, "job_id": "c2", "parent_job_id": "p2"}, JOB):
        assert in_flight_beside(view, job, view["in_flight"]) == 0
        assert evaluate(POLICY_ON, view, job).chosen_lane is not None
    # Once c2 has started, p2 waits on nothing pending, and a stranger is held behind it and c2.
    started = rows + [_attempt(2, "claude-3", "running", job_id="c2")]
    family[3] = {**family[3], "state": "running"}
    view = _view(started, compressor_gib=200, jobs=family)
    assert in_flight_beside(view, JOB, view["in_flight"]) == 2
    assert evaluate(POLICY_ON, view, JOB).chosen_lane is None


@st.composite
def forests(draw):
    """Running parents, each waiting on one or more children that have not started
    (some also with started children that wait on pending grandchildren), and a
    number of unrelated running jobs."""
    jobs, attempts, serial = [], [], iter(range(10 ** 6))

    def running(name, parent=None):
        jobs.append({"job_id": name, "parent_job_id": parent, "state": "running"})
        attempts.append(_attempt(next(serial), LANES[len(attempts) % len(LANES)], "running", job_id=name))

    for index in range(draw(st.integers(min_value=1, max_value=4))):
        name = f"p{index}"
        running(name)
        for child in range(draw(st.integers(min_value=1, max_value=2))):
            jobs.append({"job_id": f"{name}-c{child}", "parent_job_id": name,
                         "state": draw(st.sampled_from(["queued", "waiting"]))})
        if draw(st.booleans()):                       # a started child that itself waits on a grandchild
            running(f"{name}-s", name)
            jobs.append({"job_id": f"{name}-s-g", "parent_job_id": f"{name}-s", "state": "queued"})
    strangers = draw(st.integers(min_value=0, max_value=3))
    for index in range(strangers):
        running(f"x{index}")
    # Parents whose children have all started or ended wait on nothing that has
    # not started, so they count like strangers (review of 79f75e25: a rule that
    # left out any job with a child would otherwise pass).
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        running(f"d{index}")
        strangers += 1
        for child in range(draw(st.integers(min_value=1, max_value=2))):
            state = draw(st.sampled_from(["running", "succeeded", "failed"]))
            if state == "running":
                running(f"d{index}-c{child}", f"d{index}")
                strangers += 1
            else:
                jobs.append({"job_id": f"d{index}-c{child}", "parent_job_id": f"d{index}", "state": state})
    return jobs, attempts, strangers


@settings(max_examples=300, deadline=None)
@given(forest=forests(), gib=st.floats(min_value=0, max_value=512, allow_nan=False),
       limit=st.floats(min_value=.5, max_value=256, allow_nan=False))
def test_attempts_waiting_on_pending_jobs_hold_none_and_strangers_hold_all(forest, gib, limit):
    """C-6.15 (review of 15cc9f7e): an attempt of an ancestor of a job that has not
    started never holds a pending job, so parents that wait on their children cannot
    deadlock on the hold; an unrelated attempt in flight holds every pending job
    while the reading is above the threshold, and none at or below it."""
    jobs, attempts, strangers = forest
    policy = {**POLICY_ON, "host_pressure": {**POLICY_ON["host_pressure"], "compressor_max_gib": limit}}
    view = _view(attempts, compressor_gib=gib, jobs=jobs)
    above = int(gib * GIB) > limit * GIB
    for row in (row for row in jobs if row["state"] in ("queued", "waiting")):
        job = {**JOB, "job_id": row["job_id"], "parent_job_id": row["parent_job_id"]}
        count = in_flight_beside(view, job, view["in_flight"])
        assert count == strangers
        assert (host_pressure_hold(policy, view, count) is not None) == (above and strangers > 0)


def test_a_parents_cap_is_named_before_pressure():
    """C-6.11: a child its parent's cap holds reads `parent-cap`, with or without pressure."""
    family = [{"job_id": "parent", "parent_job_id": None}, {"job_id": "sibling", "parent_job_id": "parent"}]
    rows = [_attempt(0, "claude-1", "running", job_id="parent"), _attempt(1, "claude-2", "running", job_id="sibling")]
    child = {**JOB, "job_id": "child", "parent_job_id": "parent"}
    policy = {**POLICY_ON, "caps": {**POLICY_ON["caps"], "max_active_attempts_per_parent": 1}}
    assert dominant_rejection(evaluate(policy, _view(rows, compressor_gib=200, jobs=family), child)) == "parent-cap"


# --- the reason a held job is given ------------------------------------------------

def test_a_job_held_only_by_pressure_says_so_and_keeps_its_verdict():
    """C-6.15, C-6.10, C-6.11: the hold reads `host-pressure`, each lane is rejected for
    room alone, and the verdict is the one a full lane gives, so pressure that comes
    and goes adds no decision rows."""
    running = [_attempt(0, "claude-1", "running")]
    held = evaluate(POLICY_ON, _view(running, compressor_gib=66.1), JOB)
    assert held.chosen_lane is None and dominant_rejection(held) == "host-pressure"
    assert all(row["reasons"] == ["no-slot"] for evaluation in held.evaluations for row in evaluation["rejections"])
    assert host_pressure_evidence(held) == {"compressor_gib": 66.1, "compressor_max_gib": 40}
    below = evaluate(POLICY_ON, _view(running, compressor_gib=39.9), JOB)
    assert below.chosen_lane is not None and dominant_rejection(below) != "host-pressure"
    at = evaluate(POLICY_ON, _view(running, compressor_gib=40), JOB)
    assert at.chosen_lane is not None, "at the threshold is not above it"


def test_a_full_fleet_or_a_standing_reason_is_named_before_pressure():
    """C-6.11: a job the fleet cap holds reads `fleet-full`, and one no lane would take
    anyway reads that lane reason, with or without pressure."""
    full = [_attempt(index, LANES[index % len(LANES)], "running") for index in range(4)]
    assert dominant_rejection(evaluate(POLICY_ON, _view(full, compressor_gib=90), JOB)) == "fleet-full"
    spent = {identity: 1.0 for identity in LANES}
    running = [_attempt(0, "claude-1", "running")]
    label = dominant_rejection(evaluate(POLICY_ON, _view(running, spent, compressor_gib=90), JOB))
    assert label == dominant_rejection(evaluate(POLICY_OFF, _view(running, spent), JOB)) == "below-floor"


# --- the policy ----------------------------------------------------------------------

def test_the_shipped_policy_leaves_the_hold_off():
    """C-6.15: off in `default_policy.json`, with the threshold and interval it would use."""
    assert load_policy(DEFAULT_POLICY_PATH)["host_pressure"] == {"enabled": False, "compressor_max_gib": 40,
                                                                 "sample_s": 15}


def test_a_policy_without_the_section_gets_the_defaults(tmp_path):
    """C-6.15: an installed policy written before the section existed loads with it off."""
    import json
    document = json.loads(DEFAULT_POLICY_PATH.read_text())
    del document["host_pressure"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    assert load_policy(path)["host_pressure"]["enabled"] is False


@pytest.mark.parametrize("section,key", [({"enabled": "yes"}, "host_pressure.enabled"),
                                         ({"compressor_max_gib": 0}, "host_pressure.compressor_max_gib"),
                                         ({"compressor_max_gib": True}, "host_pressure.compressor_max_gib"),
                                         ({"sample_s": -1}, "host_pressure.sample_s"),
                                         ({"sample_s": .05}, "host_pressure.sample_s"),
                                         ({"sample_s": float("inf")}, "host_pressure.sample_s"),
                                         ([], "host_pressure")])
def test_a_bad_host_pressure_section_is_refused_by_key(tmp_path, section, key):
    """C-11.1, C-6.15: every field is checked whether or not the hold is on."""
    import json
    document = json.loads(DEFAULT_POLICY_PATH.read_text())
    document["host_pressure"] = section if isinstance(section, list) else {**document["host_pressure"], **section}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document).replace("Infinity", "1e999"))
    with pytest.raises(PolicyError) as raised:
        load_policy(path)
    assert raised.value.key == key


def test_the_default_policy_object_is_not_shared():
    """A caller that switches the hold on in one loaded policy does not switch it on in another."""
    first = load_policy(DEFAULT_POLICY_PATH)
    first["host_pressure"]["enabled"] = True
    assert load_policy(DEFAULT_POLICY_PATH)["host_pressure"]["enabled"] is False
    assert copy.deepcopy(POLICY_OFF)["host_pressure"]["enabled"] is False
