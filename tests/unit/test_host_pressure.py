"""C-6.15: the opt-in host-pressure hold, its reading, and what it can never do."""

import copy

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import host_pressure
from subfleet.host_pressure import GIB, Sampler, parse_vm_stat
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from subfleet.scheduler import (ACTIVE_ATTEMPTS, RouteError, dominant_rejection, evaluate,
                                host_pressure_evidence, host_pressure_hold, verdict_signature)

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


def _view(attempts=(), utilization=None, compressor_gib=None, **extra):
    view = {"now": NOW, "lanes": [_lane(identity) for identity in LANES],
            "readings": [_reading(identity, (utilization or {}).get(identity, .3)) for identity in LANES],
            "closures": [], "attempts": list(attempts), "jobs": [], **extra}
    if compressor_gib is not None:
        view["host_pressure"] = {"compressor_bytes": int(compressor_gib * GIB), "age_s": 1.0}
    return view


def _attempt(index, identity, state):
    return {"attempt_id": f"job-{index}/a1", "job_id": f"job-{index}", "lane_id": identity, "state": state}


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
@given(gaps=st.lists(st.floats(min_value=0, max_value=40, allow_nan=False), min_size=1, max_size=80),
       sample_s=st.sampled_from([1.0, 5.0, 15.0]),
       values=st.lists(st.one_of(st.none(), st.integers(min_value=0, max_value=600 * GIB)), min_size=1, max_size=80))
def test_the_host_is_read_at_most_once_an_interval_and_a_stale_reading_is_none(gaps, sample_s, values):
    """C-6.15: `vm_stat` begins at most once per `sample_s` however often it is asked
    for and whatever it answered, and a reading is given only while it is newer than
    `STALE_AFTER` intervals."""
    clock, reads, script = [100.0], [], iter(values)

    def read():
        reads.append(clock[0])
        return next(script, None)
    sampler = Sampler(read, lambda: clock[0])
    last = None
    for gap in gaps:
        clock[0] += gap
        before = len(reads)
        sampler.refresh(sample_s)
        if len(reads) > before:
            value = values[before] if before < len(values) else None
            last = (clock[0], value) if value is not None else None
        answer = sampler.reading(sample_s)
        if last is None or clock[0] - last[0] > host_pressure.STALE_AFTER * sample_s:
            assert answer is None
        else:
            assert answer["compressor_bytes"] == last[1]
    assert all(later - earlier >= sample_s for earlier, later in zip(reads, reads[1:]))
    assert len(reads) <= 1 + (clock[0] - reads[0]) / sample_s if reads else True


# --- what the hold can never do --------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(job=jobs, rows=attempts, gib=occupancy, usage=utilizations,
       limit=st.floats(min_value=.5, max_value=256, allow_nan=False))
def test_with_no_attempt_in_flight_pressure_never_changes_a_decision(job, rows, gib, usage, limit):
    """C-6.15: the hold never starves the queue. With no attempt reserved, starting,
    running or finalizing, the decision is the one a policy without the hold reaches,
    whatever the compressor holds and whatever the threshold."""
    idle = [row for row in rows if row["state"] not in ACTIVE_ATTEMPTS]
    policy = {**POLICY_ON, "host_pressure": {**POLICY_ON["host_pressure"], "compressor_max_gib": limit}}
    held = _decide(policy, _view(idle, usage, gib), job)
    free = _decide(POLICY_OFF, _view(idle, usage, gib), job)
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


@settings(max_examples=150, deadline=None)
@given(job=jobs, rows=attempts, gib=occupancy, usage=utilizations)
def test_the_hold_only_ever_withholds_a_lane(job, rows, gib, usage):
    """C-6.15: under the hold a job is placed where it would have been or nowhere,
    and it is withheld exactly when the reading is above the threshold while an
    attempt is in flight."""
    held = _decide(POLICY_ON, _view(rows, usage, gib), job)
    free = _decide(POLICY_OFF, _view(rows, usage, gib), job)
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
