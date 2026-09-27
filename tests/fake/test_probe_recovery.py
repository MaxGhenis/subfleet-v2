"""Probe supervision over mocked process identity and durable rows (C-5, C-11.4)."""

import collections
import dataclasses
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as daemon_module, procs
from subfleet.contracts import Credential, Lane, LaneOwner, Launch, Outcome, OutcomeClass
from subfleet.daemon import after, utcnow
from tests.fake.test_routing_end_to_end import routing_state
from tests.fake_adapter import FakeAdapter

# C-5.5: the 2026-09-27 census: the environment scan timed out, and no source that could be read saw a process.
MARKER_TIMED_OUT = procs.Containment(unverifiable=True, errors=(
    "marker enumeration unavailable: ps timed out: still running after 10 s",))


def events(service, kind):
    """The records of `kind` (`add_event` also writes the kind's empty audit row, C-3.2)."""
    rows = service.store.query("SELECT data_json FROM events WHERE kind=? ORDER BY event_id", (kind,))
    return [data for data in (json.loads(row["data_json"]) for row in rows) if data]


def reserved_probe(service, job_id, *, state="starting"):
    directory = service.root / "lanes" / "codex-1" / "probes" / "fixture"
    directory.mkdir(parents=True)
    record = {"holder": "probe:fixture", "job_id": job_id, "lane_id": "codex-1",
              "model_id": "gpt-6-astra", "directory": str(directory), "state": state,
              "created_at": utcnow(), "deadline_at": after(60), "guardian_pid": 900001,
              "pgid": 900001, "boot_id": "boot", "proc_start": "start", "owned_identities": {}}
    service.store.acquire_lease("lane:codex-1:slot:0", record["holder"])
    service._save_probe(record)
    return record


def submitted(service, harness):
    return service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]


def exit_receipts(record):
    directory = Path(record["directory"])
    launch = Launch(("fake",), {}, (), str(directory), None, str(directory / "stdout"),
                    str(directory / "stderr"), None, None)
    value = dataclasses.asdict(launch)
    value.pop("env_add")
    (directory / "launch.json").write_text(json.dumps(value))
    (directory / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "wall_s": .1, "child_pid": 900002}))


@pytest.mark.parametrize("change", [{"owner": "v1"}, {"enabled": False}, {"desktop": True}])
def test_c11_probe_rechecks_lane_before_reserving_after_selection(routing_state, monkeypatch, change):
    """C-10.3, C-11.2: rollback or an operator change wins before the probe lease."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    original_pick = service._pick
    selected = []
    calls = []

    def pick_then_change(*args, **kwargs):
        decision = original_pick(*args, **kwargs)
        assert decision.chosen_lane == "codex-1"
        assert not service.store.conn.in_transaction
        service.store.update_lane(decision.chosen_lane, **change)
        selected.append(decision.chosen_lane)
        return decision

    def probe(*args):
        calls.append(args)
        return Outcome(OutcomeClass.UNKNOWN, "unexpected provider call")

    monkeypatch.setattr(service, "_pick", pick_then_change)
    monkeypatch.setattr(service, "_execute_probe", probe)
    service._admit()
    assert selected == ["codex-1"]
    assert calls == []
    assert service.store.list_leases() == []
    assert service.store.list_attempts(job_id) == []
    assert service.store.query("SELECT 1 FROM events WHERE kind='probe.reserved'") == []


def test_c5_probe_restart_accepts_receipt_only_after_verified_empty(routing_state, monkeypatch):
    """C-5.3–7, C-8.4, C-11.4: recovered probes record evidence and release after containment."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id)
    exit_receipts(record)
    monkeypatch.setattr(service, "_probe_census", lambda record: procs.Containment())
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    service._recover_probes()
    assert not service.store.list_leases(record["holder"])
    assert service._probe_record(record["holder"])["state"] == "completed"
    assert service.store.list_readings()[0]["label"] == "admission-observed"
    assert not service.store.list_attempts(job_id)
    assert not Path(record["directory"]).exists()


def test_c5_probe_quarantine_keeps_lease_and_never_signals_unowned_escape(routing_state, monkeypatch):
    """C-5.4–7, C-9.5: unknown escaped probe processes retain leases without a quota closure."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="quarantined")
    exit_receipts(record)
    census = procs.Containment(marker_pids=frozenset({900003}),
                              identities={900003: procs.ProcessIdentity(900003, "boot", "escaped")})
    monkeypatch.setattr(service, "_probe_census", lambda record: census)
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    def forbidden(*args, **kwargs):
        raise AssertionError("an unowned process must not be signalled")
    monkeypatch.setattr(procs, "signal_group", forbidden)
    monkeypatch.setattr(procs, "signal_process", forbidden)
    service._recover_probes()
    assert service.store.list_leases(record["holder"])
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    assert not service.store.list_closures()
    assert Path(record["directory"]).exists()
    snapshot = service._capacity_view()
    assert snapshot["in_flight"]["codex-1"] == 0
    assert snapshot["unavailable_lanes"]["codex-1"] == record["holder"]
    assert "probe=quarantined" in service.dispatch("daemon.status", {})["status"]


def test_c5_probe_reservation_before_launch_recovers_without_dispatch(routing_state, monkeypatch):
    """C-5.1, C-8.4: a pre-gate crash cannot launch a provider or become a work attempt."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    record.pop("guardian_pid")
    record.pop("pgid")
    service._save_probe(record)
    monkeypatch.setattr(service, "_probe_census", lambda record: procs.Containment())
    service._recover_probes()
    assert not service.store.list_leases(record["holder"])
    assert service._probe_record(record["holder"])["outcome"]["cls"] == "unknown"
    assert not service.store.list_attempts(job_id)
    assert not service.store.list_readings()


def test_c5_probe_exception_after_spawn_keeps_unverifiable_lease(routing_state, monkeypatch):
    """C-5.5–7: an exception after guardian start never releases unverified probe ownership. A census that could
    not be read and saw nothing is deferred, not quarantined (C-5.5): the lease stays and the job waits `uncertain`
    for recovery, with the census's cause on record (on release/217 this quarantined the probe)."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id)
    service.term_grace_s = 0
    def failed(*args):
        raise OSError("receipt unavailable")
    monkeypatch.setattr(service, "_execute_probe", failed)
    monkeypatch.setattr(service, "_probe_census", lambda record: MARKER_TIMED_OUT)
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    decision = SimpleNamespace(chosen_lane="codex-1", chosen_model="astra")
    outcome = service._probe_candidate(service.store.get_job(job_id), decision, record["holder"])
    assert outcome.evidence["probe_deferred"] and "probe_quarantined" not in outcome.evidence
    stored = service._probe_record(record["holder"])
    assert stored["state"] == "containing" and stored["containment"]["errors"] == list(MARKER_TIMED_OUT.errors)
    assert service.store.list_leases(record["holder"])
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    assert events(service, "probe.quarantined") == []
    assert [event["deferrals"] for event in events(service, "probe.census_deferred")] == [1]


def test_c5_probe_gate_opens_after_durable_identity_and_readonly_launch(routing_state, monkeypatch):
    """C-5.1, C-10.5, C-11.4: probe guardian ownership commits before gated readonly dispatch."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    job = service.store.get_job(job_id)
    lane = service.store.get_lane("codex-1")
    model = service.policy["models"]["astra"]
    writes = []
    original_close, original_write = daemon_module.os.close, daemon_module.os.write
    original_build = FakeAdapter.build_launch
    def build(adapter, spec, *args, **kwargs):
        assert spec.sandbox == "read-only"
        assert spec.kind == "probe"
        return original_build(adapter, spec, *args, **kwargs)
    monkeypatch.setattr(FakeAdapter, "build_launch", build)
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: (800, 801))
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else original_close(fd))
    def release(fd, value):
        if fd != 801:
            return original_write(fd, value)
        stored = service._probe_record(record["holder"])
        assert stored["state"] == "starting"
        assert stored["guardian_pid"] == 900001
        assert stored["proc_start"] == "fixture-start"
        assert not service.store.conn.in_transaction
        writes.append((fd, value))
    monkeypatch.setattr(daemon_module.os, "write", release)
    def spawn(command, **kwargs):
        assert command[1:3] == ["-m", "subfleet.guardian"]
        assert "--launch-fd" in command
        assert kwargs["env"]["SUBFLEET_ATTEMPT"] == record["holder"]
        assert all(key not in kwargs["env"] for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
        return SimpleNamespace(pid=900001)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", spawn)
    def awaited(value, child):
        value["state"] = "contained"
        service._save_probe(value)
        return True, {"rc": 0, "signal": None, "wall_s": .1, "child_pid": 900002}
    monkeypatch.setattr(service, "_await_probe", awaited)
    outcome = service._execute_probe(job, lane, model, record["holder"])
    assert outcome.cls == OutcomeClass.OK
    assert writes == [(801, b"1")]
    launch = json.loads((Path(record["directory"]) / "launch.json").read_text())
    assert "env_add" not in launch
    assert "probe" in launch["cwd"]


def test_c6_capacity_waiter_cannot_be_bypassed_by_newer_same_tier(routing_state):
    """C-4.1, C-6.4; amendment 11: FIFO retains an older capacity waiter's place."""
    service, harness = routing_state
    first = service.dispatch("submit", harness.submit_args())["job_id"]
    second = service.dispatch("submit", harness.submit_args())["job_id"]
    service.store.update_job(first, state="waiting", wait_reason="capacity", next_check_at=after(60))
    service._admit()
    assert not service.store.list_attempts(second)
    service.store.update_job(first, next_check_at=utcnow())
    service._admit()
    assert service.store.list_attempts(first)
    assert not service.store.list_attempts(second)


# --- C-5.5: a probe whose census cannot be read is deferred, not quarantined (incident 2026-09-27) ----------------

def test_c5_5_a_deferred_probe_is_contained_by_recovery_at_backoff_and_finished(routing_state, monkeypatch):
    """C-5.5, C-5.10 recovery takes a deferred probe's census again only when its backoff is due, never on every
    admission pass; the first census that verifies it contained finishes the probe from its receipt, releases the
    lease and hands the job back to admission. On release/217 the probe was quarantined on the first census."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id)
    exit_receipts(record)
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    service.term_grace_s = 0                                  # the first containment's grace re-reads every 50 ms
    censuses = []
    answer = [MARKER_TIMED_OUT]
    monkeypatch.setattr(service, "_probe_census", lambda value: censuses.append(1) or answer[0])
    service._recover_probes()                                  # the whole protocol: a census, and one after SIGKILL
    assert len(censuses) == 2 and service._probe_record(record["holder"])["state"] == "containing"
    assert service.store.list_leases(record["holder"])
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    service._recover_probes()                                  # not due yet: no census
    service._recover_probes()
    assert len(censuses) == 2
    count, due = service._probe_census_due[record["holder"]]
    assert count == 1 and 0 < due - time.monotonic() <= .5
    service._probe_census_due[record["holder"]] = (count, 0.0)  # due
    service._recover_probes()                                  # still unreadable: one census, the backoff doubles
    assert len(censuses) == 3 and service._probe_census_due[record["holder"]][0] == 2
    assert [event["deferrals"] for event in events(service, "probe.census_deferred")] == [1, 2]
    answer[0] = procs.Containment()                            # `ps` answers again
    service._probe_census_due[record["holder"]] = (2, 0.0)
    service._recover_probes()
    assert service._probe_record(record["holder"])["state"] == "completed"
    assert not service.store.list_leases(record["holder"])
    assert record["holder"] not in service._probe_census_due
    job = service.store.get_job(job_id)
    assert job["wait_reason"] == "capacity" and job["next_check_at"] <= utcnow()
    assert events(service, "probe.quarantined") == []


def test_c5_5_a_probe_census_that_shows_an_escape_still_quarantines_once_and_is_paced(routing_state, monkeypatch):
    """C-5.4–7 unchanged where the census is evidence: an unowned live process quarantines. The quarantine is one
    event, not one per admission pass, and its census is taken again at the backoff (release/217: every pass)."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id)
    exit_receipts(record)
    escaped = procs.Containment(marker_pids=frozenset({900003}),
                                identities={900003: procs.ProcessIdentity(900003, "boot", "escaped")})
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: pytest.fail("no recorded leader is live"))
    monkeypatch.setattr(procs, "signal_process", lambda *args, **kwargs: pytest.fail("the escape is not owned"))
    service.term_grace_s = 0
    censuses = []
    monkeypatch.setattr(service, "_probe_census", lambda value: censuses.append(1) or escaped)
    service._recover_probes()
    assert service._probe_record(record["holder"])["state"] == "quarantined"
    taken = len(censuses)
    for _ in range(3):
        service._recover_probes()
    assert len(censuses) == taken
    service._probe_census_due[record["holder"]] = (1, 0.0)
    service._recover_probes()
    assert len(censuses) == taken + 1
    assert len(events(service, "probe.quarantined")) == 1
    assert service.store.list_leases(record["holder"])
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"


def test_c5_5_a_quarantined_probe_s_job_that_was_let_go_is_held_again(routing_state, monkeypatch):
    """C-5.7 a job whose probe is still quarantined waits `uncertain`; if something let it go meanwhile (a restart
    that found the record quarantined, an operator's edit) the next census holds it again, once."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="quarantined")
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    monkeypatch.setattr(service, "_probe_census", lambda value: MARKER_TIMED_OUT)
    service._recover_probes()
    stored = service._probe_record(record["holder"])
    assert stored["state"] == "quarantined"                    # a quarantine ends only on a verified census
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    kinds = [row["kind"] for row in service.store.query("SELECT kind FROM events WHERE kind='probe.job_held'")]
    assert kinds == ["probe.job_held"]


def test_c5_5_a_timer_turn_whose_containment_was_deferred_is_left_for_recovery(routing_state, monkeypatch):
    """C-5.5 a timer turn (keepalive, heal) whose census could not be read is not marked completed and keeps its
    directory, so recovery can finish it from its receipt once a census verifies it (release/217 completed only
    a turn that was not quarantined, and had no deferral)."""
    service, _ = routing_state
    lane = service.store.get_lane("codex-1")
    holder = "probe:timer:" + os.urandom(6).hex()
    monkeypatch.setattr(service, "_execute_probe", lambda job, lane_, model, holder_: Outcome(
        OutcomeClass.UNKNOWN, "probe containment deferred: its census could not be read",
        evidence={"probe_deferred": True}))
    outcome = service._timer_turn(lane, "keepalive", holder, cancel=threading.Event(), deadline=time.monotonic() + 60)
    assert outcome.evidence["probe_deferred"]
    assert service._probe_record(holder)["state"] != "completed"
    assert Path(service._probe_record(holder)["directory"]).exists()


def test_c5_5_a_deferred_timer_turn_keeps_its_lease_for_recovery():
    """C-5.5 a timer turn whose containment was deferred keeps its probe lease, as a quarantined one does."""
    from subfleet.timers import held
    assert held(Outcome(OutcomeClass.UNKNOWN, "timer containment deferred", evidence={"probe_deferred": True}))
    assert held(Outcome(OutcomeClass.UNKNOWN, "timer quarantined", evidence={"probe_quarantined": True}))
    assert not held(Outcome(OutcomeClass.OK, "ok"))


# --- C-11.4: the probe deadline under load -----------------------------------------------------------------------

def test_c11_4_the_probe_deadline_is_the_cap_until_probes_need_more(routing_state, monkeypatch):
    """C-11.4 `caps.probe_timeout_s` with no evidence; then twice the slowest recent wall, or twice a deadline a probe
    was killed at; never past four times the cap; evidence older than PROBE_LOAD_MEMORY_S is forgotten."""
    service, _ = routing_state
    clock = [1000.0]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep))
    assert service._probe_deadline_s() == 60
    service._note_probe_need({}, {"rc": 0, "signal": None, "wall_s": 20.0})
    assert service._probe_deadline_s() == 60                   # 40 s is inside the cap
    service._note_probe_need({}, {"rc": 0, "signal": None, "wall_s": 45.6})    # the incident's, at load 160
    assert service._probe_deadline_s() == pytest.approx(91.2)
    service._note_probe_need({"deadline_hit": True, "deadline_s": 91.2}, {"rc": 143, "signal": 15, "wall_s": 91})
    assert service._probe_deadline_s() == pytest.approx(182.4)
    service._note_probe_need({"deadline_hit": True, "deadline_s": 182.4}, None)
    assert service._probe_deadline_s() == 240                  # the ceiling
    service._note_probe_need({}, {"rc": 143, "signal": 15, "wall_s": 500})     # a cancelled probe says nothing
    clock[0] += daemon_module.PROBE_LOAD_MEMORY_S + 1
    assert service._probe_deadline_s() == 60


@given(st.lists(st.one_of(st.tuples(st.just("wall"), st.floats(0, 10_000)),
                          st.tuples(st.just("killed"), st.floats(1, 10_000)),
                          st.tuples(st.just("cancelled"), st.floats(0, 10_000))), max_size=40),
       st.floats(1, 600))
@settings(max_examples=200, deadline=None)
def test_c11_4_the_probe_deadline_is_bounded_and_follows_the_evidence(events_seen, cap):
    """C-11.4, for every history of probes: the deadline is at least the cap and at most four times it, it is exactly
    the largest recent need clamped to those bounds, it never shrinks when a need is added, and a cancelled probe
    changes nothing."""
    core = object.__new__(daemon_module.Daemon)
    core.policy = {"caps": {"probe_timeout_s": cap}}
    core._probe_needs = collections.deque(maxlen=daemon_module.PROBE_LOAD_SAMPLES)
    needs = []
    for kind, seconds in events_seen:
        before = core._probe_deadline_s()
        if kind == "wall":
            core._note_probe_need({}, {"rc": 0, "signal": None, "wall_s": seconds})
            needs.append(daemon_module.PROBE_WALL_MARGIN * seconds)
        elif kind == "killed":
            core._note_probe_need({"deadline_hit": True, "deadline_s": seconds}, None)
            needs.append(2 * seconds)
        else:
            core._note_probe_need({}, {"rc": 143, "signal": 15, "wall_s": seconds})
        after_ = core._probe_deadline_s()
        assert cap <= after_ <= daemon_module.PROBE_DEADLINE_CEILING_FACTOR * cap
        recent = needs[-daemon_module.PROBE_LOAD_SAMPLES:]
        assert after_ == min(daemon_module.PROBE_DEADLINE_CEILING_FACTOR * cap, max([cap, *recent]))
        if kind == "cancelled":
            assert after_ == before


def test_c11_4_a_probe_killed_at_its_deadline_is_retried_once_at_once_never_approved(routing_state, monkeypatch):
    """C-11.4 a probe killed at its deadline leaves the job due again in about a second, once, with the longer deadline
    its kill earned; a second kill in a row waits the usual 60 s; the pair is never approved without an `ok` probe;
    any other end of a probe ends the streak."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    deadlines, answers = [], []

    def probe(job, decision, holder):
        record = service._probe_record(holder)
        deadlines.append(record["deadline_s"])
        record.update(state="contained")
        service._save_probe(record)
        outcome = answers.pop(0)
        if outcome.evidence.get("deadline_hit"):
            service._note_probe_need({"deadline_hit": True, "deadline_s": record["deadline_s"]}, None)
        service._finish_probe(record, outcome)
        return outcome
    monkeypatch.setattr(service, "_probe_candidate", probe)
    killed = Outcome(OutcomeClass.UNKNOWN, "rc 143", evidence={"deadline_hit": True})

    def prepare():
        job = service.store.get_job(job_id)
        return service._prepare_route(job, job, ())[0]

    def due_in() -> float:
        due = datetime.fromisoformat(service.store.get_job(job_id)["next_check_at"].replace("Z", "+00:00"))
        return (due - datetime.now(timezone.utc)).total_seconds()

    answers.append(killed)
    assert prepare() is None                                   # never approved on a killed probe
    assert due_in() <= 2 and service._probe_retried[job_id]
    answers.append(killed)
    assert prepare() is None
    assert 50 <= due_in() <= 61                                # the retry was killed too: the usual wait
    assert deadlines == [60, 120]                              # the retry had the deadline the first kill earned
    answers.append(Outcome(OutcomeClass.UNKNOWN, "no receipt"))
    assert prepare() is None
    assert job_id not in service._probe_retried                # the streak ended
    assert deadlines[-1] == 240                                # and the second kill's evidence stands (ceiling)
    waits = [json.loads(row["data_json"]) for row in service.store.list_events(job_id)
             if row["kind"] == "job.probe_waiting"]
    assert [(w["deadline_hit"], w["prompt_retry"]) for w in waits] == [(True, True), (True, False), (False, False)]


def test_c11_4_an_admission_probe_s_deadline_runs_from_its_gate(routing_state, monkeypatch):
    """C-11.4 what the launch costs under load (the credential, the spawn, the store) is not taken from the probe:
    its deadline is set again as its gate opens. On release/217 it ran from the reservation."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    record.update(deadline_s=90, deadline_at="2026-01-01T00:00:00Z")    # a launch that took far too long
    service._save_probe(record)
    job, lane = service.store.get_job(job_id), service.store.get_lane("codex-1")
    model = service.policy["models"]["astra"]
    original_close, original_write = daemon_module.os.close, daemon_module.os.write
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: (800, 801))
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else original_close(fd))
    at_gate = []

    def gate(fd, value):
        if fd != 801:
            return original_write(fd, value)
        at_gate.append(service._probe_record(record["holder"])["deadline_at"])
    monkeypatch.setattr(daemon_module.os, "write", gate)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace(pid=900001))
    monkeypatch.setattr(service, "_await_probe", lambda value, child: (True, {"rc": 0, "signal": None,
                                                                               "wall_s": .1, "child_pid": 900002}))
    before = datetime.now(timezone.utc)
    service._execute_probe(job, lane, model, record["holder"])
    opened = datetime.fromisoformat(at_gate[0].replace("Z", "+00:00"))
    assert 89 <= (opened - before).total_seconds() <= 92


def test_c5_5_a_probe_deferred_at_its_end_leaves_admission_without_a_second_census(routing_state, monkeypatch):
    """C-5.5, C-6.9 a probe whose census could not be read as it ended: `_prepare_route` holds the job `uncertain`
    (which holds no later job back), keeps the lease for recovery, finishes nothing, and takes no second census."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    service.term_grace_s = 0
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    censuses = []
    monkeypatch.setattr(service, "_probe_census", lambda value: censuses.append(1) or MARKER_TIMED_OUT)

    def execute(job, lane, model, holder):
        record = service._probe_record(holder)
        record.update(state="running", guardian_pid=900001, pgid=900001, boot_id="boot", proc_start="start")
        service._save_probe(record)
        exit_receipts(record)
        service._contain_probe(record)                        # what `_await_probe` ends with
        return Outcome(OutcomeClass.UNKNOWN, "probe containment deferred: its census could not be read",
                       evidence={"probe_deferred": True, "deadline_s": 60, "deadline_hit": False})
    monkeypatch.setattr(service, "_execute_probe", execute)
    job = service.store.get_job(job_id)
    assert service._prepare_route(job, job, ())[0] is None
    assert len(censuses) == 2                                  # the protocol's two; `_probe_candidate` adds none
    holder = next(iter(service._probe_census_due))
    assert service._probe_record(holder)["state"] == "containing" and service.store.list_leases(holder)
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    assert events(service, "probe.completed") == [] and events(service, "probe.quarantined") == []


PROBE_CENSUSES = {
    "empty": procs.Containment(),
    "marker-unread": MARKER_TIMED_OUT,
    "escape": procs.Containment(marker_pids=frozenset({900003}),
                                identities={900003: procs.ProcessIdentity(900003, "boot", "escaped")}),
    "escape-and-marker-unread": procs.Containment(group_pids=frozenset({900004}), unverifiable=True,
                                                  identities={900004: procs.ProcessIdentity(900004, "boot", "g")},
                                                  errors=("marker enumeration unavailable: ps exited 1",)),
}


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(seq=st.lists(st.sampled_from(sorted(PROBE_CENSUSES)), min_size=1, max_size=8))
def test_c5_5_a_probe_lease_goes_only_with_a_verified_census_whatever_came_before(routing_state, monkeypatch, seq):
    """C-5.4–7, C-5.5, for every sequence of censuses recovery takes of a finished probe: its lease is released only
    by a verified-empty census; it is quarantined only once a census shows a process, and stays so until one is
    verified empty; an inconclusive census leaves it `containing`; its job waits `uncertain` while the lease is held;
    at most one `probe.quarantined` event is written; and no unowned process is ever signalled."""
    service, harness = routing_state
    service.term_grace_s = 0
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: pytest.fail("no recorded leader is live"))
    monkeypatch.setattr(procs, "signal_process", lambda *args, **kwargs: pytest.fail("nothing here is owned"))
    for lease in service.store.list_leases():
        service.store.release_leases(lease["holder"])
    job_id = submitted(service, harness)
    token = os.urandom(6).hex()
    directory = service.root / "lanes" / "codex-1" / "probes" / token
    directory.mkdir(parents=True)
    record = {"holder": f"probe:{token}", "job_id": job_id, "lane_id": "codex-1", "model_id": "gpt-6-astra",
              "directory": str(directory), "state": "starting", "created_at": utcnow(), "deadline_at": after(60),
              "guardian_pid": 900001, "pgid": 900001, "boot_id": "boot", "proc_start": "start",
              "owned_identities": {}}
    assert service.store.acquire_lease("lane:codex-1:slot:0", record["holder"])
    service._save_probe(record)
    exit_receipts(record)
    quarantined_seen = released = False
    for name in seq:
        census = PROBE_CENSUSES[name]
        monkeypatch.setattr(service, "_probe_census", lambda value, census=census: census)
        if record["holder"] in service._probe_census_due:
            count, _ = service._probe_census_due[record["holder"]]
            service._probe_census_due[record["holder"]] = (count, 0.0)     # due
        service._recover_probes()
        state = service._probe_record(record["holder"])["state"]
        if census.verified_empty:
            assert state == "completed" and not service.store.list_leases(record["holder"])
            released = True
            break
        assert service.store.list_leases(record["holder"])
        quarantined_seen = quarantined_seen or bool(census.live_pids)
        assert state == ("quarantined" if quarantined_seen else "containing")
        assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    holder_rows = service.store.query("SELECT data_json FROM events WHERE kind='probe.quarantined' AND job_id=?", (job_id,))
    assert len([row for row in holder_rows if row["data_json"] != "{}"]) <= 1
    if not released:
        assert service.store.list_leases(record["holder"])


def test_c11_4_the_gate_s_deadline_is_set_after_the_ownership_commit(routing_state, monkeypatch):
    """C-11.4 the store's commits are not taken from the probe: a slow commit of the gate's ownership record leaves
    the wait its whole deadline from when the gate opens (review of 2133efd, Astra finding 3: the deadline was set
    before that commit, so a 120 s commit opened an expired gate on a 90 s probe)."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    record.update(deadline_s=90)
    service._save_probe(record)
    job, lane = service.store.get_job(job_id), service.store.get_lane("codex-1")
    model = service.policy["models"]["astra"]
    original_close, original_write, original_save = daemon_module.os.close, daemon_module.os.write, service._save_probe
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: (800, 801))
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else original_close(fd))
    opened = []
    monkeypatch.setattr(daemon_module.os, "write",
                        lambda fd, value: opened.append(datetime.now(timezone.utc)) if fd == 801 else original_write(fd, value))

    def slow_save(value):
        if value.get("state") == "starting":
            time.sleep(2.2)                                   # the ownership commit, under load
        original_save(value)
    monkeypatch.setattr(service, "_save_probe", slow_save)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace(pid=900001))
    waited = []

    def awaited(value, child):
        waited.append(value["deadline_at"])
        return True, {"rc": 0, "signal": None, "wall_s": .1, "child_pid": 900002}
    monkeypatch.setattr(service, "_await_probe", awaited)
    service._execute_probe(job, lane, model, record["holder"])
    deadline = datetime.fromisoformat(waited[0].replace("Z", "+00:00"))
    assert (deadline - opened[0]).total_seconds() >= 89


@pytest.mark.parametrize("flag", ["probe_deferred", "probe_quarantined"])
def test_c5_5_a_keepalive_whose_probe_is_held_keeps_its_lease_for_recovery(tmp_path, flag):
    """C-5.5, C-5.7 at the keepalive's own call site: a turn whose containment was deferred, like one quarantined,
    leaves its probe lease for the daemon's recovery (review of 2133efd: only `held()` was tested directly)."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    policy = {"models": {"haiku": {"id": "claude-haiku-4-5-20251001"}},
              "timers": {"probe_interval_s": 300, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}
    home = tmp_path / "claude-1"
    home.mkdir()
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("claude-1", "claude", "claude:claude-1", Credential("claude", str(home), "home"),
                            str(home), LaneOwner.V2, False, True))
        turns = []

        def turn(lane, purpose, holder, *, cancel, deadline):
            turns.append(holder)
            return Outcome(OutcomeClass.UNKNOWN, "held", evidence={flag: True})
        timer = Timers(store, tmp_path, policy, turn=turn)
        try:
            timer.keepalive_cycle()
        finally:
            timer.stop()
        assert turns and store.list_leases(turns[0])
