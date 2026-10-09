"""C-6.10, C-11.4: a probe that says nothing moves its job's next probe, on a clock from its reservation.

Defect D-1 (incident 2026-10-03, on the desktop release line; report
`docs/reports/2026-10-08-probe-vehicle-starvation.md` there): every admission
probe a `hard` review carried ran to its 60 s deadline and read `unknown` or
`transient`. The job was put on a clock 60 s from the probe's end, so with probes
run one after another inside the admission pass its next look came a pass later,
and its next probe went to whichever lane ranked first, the same lane twice
running. Here every later job of its tier that competes with it waits behind it
(C-6.9), so the whole tier waited on that lane.

Invariants checked below on scripted outcomes and, in the property at the end,
on generated ones:

I3 (the clock). After an inconclusive probe its job is due `PROBE_RETRY_S` after
   the probe was reserved, or at once if that has passed.
I6 (rotation). After a probe of a model on a lane says nothing, the job's next
   probe of that model goes to a lane of that model it has not had such an
   answer on this round, never to a model the chain promotes to. So while one
   lane's probes always run out of time, every job that could run on another lane
   starts, except one that C-6.9 holds behind an older job pinned to the slow lane
   (here a job with no lane pin competes with every lane, so it waits behind any
   older waiter of its model; the desktop release line narrows that); and once
   every probe answers, every job starts.
"""

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.contracts import PROBE_RETRY_S, Credential, Lane, LaneOwner, Outcome, OutcomeClass
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake.test_probe_recovery import reserved_probe
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake_adapter import FakeAdapter

OK = Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None})
#: What the incident's probes were read as once their deadline had stopped them.
CUT = Outcome(OutcomeClass.UNKNOWN, "Codex exited without a verified deliverable",
              {"rc": 0, "signal": None, "admission": "no successful deliverable", "stopped": "deadline"})
SLOW = Outcome(OutcomeClass.TRANSIENT, "Temporary Codex transport or capacity failure",
               {"rc": 0, "signal": None, "stopped": "deadline",
                "admission": "failed to refresh available models: request timed out"})
MODELS = {"gpt-6-astra": "astra", "gpt-5.6-terra": "terra"}


def codex_lane(root: Path, identity: str) -> Lane:
    home = root / ("home-" + identity)
    home.mkdir(exist_ok=True)
    return Lane(identity, "codex", "codex:" + identity, Credential("codex", str(home), "home"), str(home),
                LaneOwner.V2, False)


def uncap(service):
    """The shipped caps hold one unmeasured attempt per lane; these cases are about probes."""
    service.policy["caps"].update(max_active_attempts=50, max_in_flight_per_lane=50, max_in_flight_unmeasured=50)


def submit(service, harness, **changes):
    """A `hard` job: on an unmeasured lane it needs the lane's probe first (C-11.4)."""
    return service.dispatch("submit", harness.submit_args(**{"tier": "hard", **changes}))["job_id"]


def moment(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class Probes:
    """`Daemon._execute_probe`, scripted: each probe takes the next outcome, then `ok`; a probe on a
    lane in `bad` always runs to its deadline. An outcome may come as `(outcome, seconds run)`, 60 by
    default; nothing waits, the reservation is moved that far into the past."""

    def __init__(self, service, monkeypatch, outcomes=(), bad=()):
        self.service, self.outcomes, self.bad = service, list(outcomes), set(bad)
        self.vehicles: list[tuple[str, str, str]] = []
        self.classes: list[OutcomeClass] = []
        self.reserved: dict[str, str] = {}
        monkeypatch.setattr(service, "_execute_probe", self)

    def __call__(self, job, lane, model, holder):
        outcome = CUT if lane.lane_id in self.bad else self.outcomes.pop(0) if self.outcomes else OK
        outcome, ran_s = outcome if isinstance(outcome, tuple) else (outcome, 60)
        self.vehicles.append((job["job_id"], lane.lane_id, MODELS[model["id"]]))
        self.classes.append(outcome.cls)
        record = self.service._probe_record(holder)
        record["created_at"] = self.reserved[job["job_id"]] = after(-ran_s)
        self.service._save_probe(record)
        return outcome

    def lanes(self):
        return [lane for _, lane, _ in self.vehicles]


def started(service) -> list[str]:
    return [row["job_id"] for row in service.store.query("SELECT job_id FROM attempts ORDER BY rowid")]


def end_attempts(service):
    """Every attempt in flight ends: here an attempt takes the lowest free slot from 0, the one a probe
    needs, so a lane in use is probed again only once its attempts are done."""
    with service.store.transaction("fixture.attempts_ended") as tx:
        for row in tx.execute("SELECT attempt_id,job_id FROM attempts WHERE state='reserved'").fetchall():
            tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (row[0],))
            tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (row[1],))
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (row[0], row[1]))


def make_due(service):
    for job in service.store.list_jobs():
        if job["state"] == "waiting":
            service.store.update_job(job["job_id"], next_check_at=utcnow())


@pytest.fixture
def fleet(routing_state, monkeypatch):  # noqa: F811
    service, harness = routing_state
    uncap(service)
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    return service, harness


# --- I3: the clock ------------------------------------------------------------------------------

@pytest.mark.parametrize("ran_s, left_s", [(60, 0), (75, 0), (7, 53), (0, 60)])
def test_the_retry_clock_runs_from_the_probes_reservation(routing_state, monkeypatch, ran_s, left_s):
    """I3: a probe that used its whole minute leaves its job due at once; one that failed in seven
    seconds waits out the other fifty-three, so a broken CLI is never probed in a loop."""
    service, harness = routing_state
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [(CUT, ran_s)])
    before = utcnow()
    service._admit()
    due = moment(service.store.get_job(job_id)["next_check_at"])
    assert 0 <= (due - moment(before)).total_seconds() - left_s <= 2          # whole-second stamps
    assert (due - moment(probes.reserved[job_id])).total_seconds() >= min(ran_s, PROBE_RETRY_S)
    event = service.store.query("SELECT lane_id,data_json FROM events WHERE kind='job.probe_waiting' "
                                "AND data_json<>'{}'")[-1]
    assert event["lane_id"] == "codex-1"
    assert json.loads(event["data_json"]) == {"model": "astra", "class": "unknown",
                                              "next_check_at": service.store.get_job(job_id)["next_check_at"]}


def test_the_tier_behind_a_probe_its_deadline_stopped_waits_one_pass_not_a_minute(fleet, monkeypatch):
    """I3, C-6.9: the jobs behind it wait for it; it is due on the very next pass."""
    service, harness = fleet
    oldest, later = submit(service, harness), submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT])
    service._admit()
    assert [job_id for job_id, _, _ in probes.vehicles] == [oldest]
    assert service._holds[later]["reason"] == "behind-older-job"
    service._admit()                                          # nothing made due by hand
    assert started(service) == [oldest, later]


# --- I6: rotation -------------------------------------------------------------------------------

def test_after_a_probe_said_nothing_the_next_goes_to_another_lane(fleet, monkeypatch):
    """I6: the incident's job probed codex-2 twice running, and a second job did the same."""
    service, harness = fleet
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT, CUT, CUT])
    for _ in range(3):
        service._admit()
    # codex-1 ranks first (both unmeasured, nothing in flight, lane id); then the
    # other lane; then, both tried, a new round from the top.
    assert probes.lanes() == ["codex-1", "codex-2", "codex-1"]
    service._admit()
    assert probes.lanes() == ["codex-1", "codex-2", "codex-1", "codex-2"]
    # The reservation evaluates the route again and leaves the same lane out:
    # it starts where the probe answered, not on the lane that said nothing.
    assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == ["codex-2"]


def test_a_slow_lane_at_the_top_holds_the_tier_one_probe_not_for_ever(fleet, monkeypatch):
    """I6: every probe on codex-1 runs to its deadline, and it ranks first whenever it is as idle as
    the rest. Without rotation the oldest job probed codex-1 every pass and every later job of its
    tier waited behind it (C-6.9) for as long as codex-1 stayed slow."""
    service, harness = fleet
    service.store.put_lane(codex_lane(service.root, "codex-3"))
    oldest, later = submit(service, harness), submit(service, harness)
    probes = Probes(service, monkeypatch, bad={"codex-1"})
    service._admit()
    assert probes.vehicles == [(oldest, "codex-1", "astra")]
    assert service._holds[later]["reason"] == "behind-older-job"
    service._admit()
    assert probes.vehicles[1:] == [(oldest, "codex-2", "astra"), (later, "codex-1", "astra")]
    service._admit()
    assert probes.vehicles[3:] == [(later, "codex-3", "astra")]
    assert started(service) == [oldest, later]


def test_rotation_never_takes_a_model_the_chain_promotes_to(fleet, monkeypatch):
    """I6: with every lane of its model tried, a job starts a new round on that model; it does not
    spend a costlier one because probes said nothing."""
    from tests.fake.test_routing_end_to_end import claude_lane
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-2"))
    service.policy["tiers"].append("highest")
    service.policy["chains"]["research"] = ["haiku", "sonnet", "opus", "astra", "opus"]
    job_id = submit(service, harness, pinned_model=None, task="research")
    probes = Probes(service, monkeypatch, [CUT, CUT])
    for _ in range(3):
        service._admit()
    assert [model for _, _, model in probes.vehicles] == ["astra", "astra", "astra"]
    assert [row["model_requested"] for row in service.store.list_attempts(job_id)] == ["gpt-6-astra"]


def test_a_pinned_job_probes_its_lane_again(routing_state, monkeypatch):
    """I6: a lane pin has no other lane to go to."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    job_id = submit(service, harness, pinned_lane="codex-1")
    probes = Probes(service, monkeypatch, [CUT])
    service._admit()
    service._admit()
    assert probes.lanes() == ["codex-1", "codex-1"]
    assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == ["codex-1"]


def test_a_quarantined_probe_is_no_miss(routing_state, monkeypatch):
    """`uncertain` is a person's to end (C-4.1): nothing is rotated and the clock is the 60 s it was."""
    service, harness = routing_state
    job_id = submit(service, harness)
    Probes(service, monkeypatch, [Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined",
                                          {"probe_quarantined": True})])
    service._admit()
    job = service.store.get_job(job_id)
    assert job["wait_reason"] == "uncertain" and moment(job["next_check_at"]) > moment(after(55))
    assert service._probe_misses.get(job_id, {}) == {}


def test_a_job_that_left_the_queue_is_forgotten(fleet, monkeypatch):
    service, harness = fleet
    job_id = submit(service, harness)
    Probes(service, monkeypatch, [(CUT, 1)])
    service._admit()
    assert service._probe_misses[job_id] == {"astra": frozenset({"codex-1"})}
    service.dispatch("kill", {"job_id": job_id})
    service._admit()
    assert job_id not in service._probe_misses


# --- C-11.4: a probe its deadline stopped says so ---------------------------------------------

def test_a_probe_stopped_at_its_deadline_says_so(routing_state, monkeypatch):
    service, harness = routing_state
    record = reserved_probe(service, submit(service, harness))
    record["deadline_at"] = after(-1)
    monkeypatch.setattr(service, "_contain_probe", lambda value: True)
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *a, **k: True)
    assert service._await_probe(record) == (True, None)
    assert record["stopped"] == "deadline"


def test_a_probe_that_ended_by_itself_is_not_called_stopped(routing_state, monkeypatch):
    service, harness = routing_state
    record = reserved_probe(service, submit(service, harness))
    record["deadline_at"] = after(-1)                               # the receipt is read first
    (Path(record["directory"]) / "exit.json").write_text(json.dumps({"rc": 0, "child_pid": 900002}))
    monkeypatch.setattr(service, "_contain_probe", lambda value: True)
    assert service._await_probe(record)[0] is True
    assert "stopped" not in record


@pytest.mark.parametrize("receipt", [{"rc": 0, "signal": None, "wall_s": 60.0, "child_pid": 900002}, None])
def test_the_deadline_is_in_the_probes_evidence(routing_state, monkeypatch, receipt):
    service, harness = routing_state
    job_id = submit(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    monkeypatch.setattr(daemon_module.os, "pipe", lambda: (800, 801))
    close, write = daemon_module.os.close, daemon_module.os.write
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else close(fd))
    monkeypatch.setattr(daemon_module.os, "write", lambda fd, value: None if fd == 801 else write(fd, value))
    monkeypatch.setattr(daemon_module.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace(pid=900001))

    def awaited(value, child):
        value.update(state="contained", stopped="deadline")
        service._save_probe(value)
        return True, receipt
    monkeypatch.setattr(service, "_await_probe", awaited)
    outcome = service._execute_probe(service.store.get_job(job_id), service.store.get_lane("codex-1"),
                                     service.policy["models"]["astra"], record["holder"])
    assert outcome.evidence["stopped"] == "deadline"
    assert outcome.cls == (OutcomeClass.OK if receipt else OutcomeClass.UNKNOWN)


# --- the property ------------------------------------------------------------------------------

@contextmanager
def generated_fleet():
    """A daemon over two unmeasured Codex lanes, as `routing_state` builds one, for one example."""
    with tempfile.TemporaryDirectory(prefix="probe-rotation-") as tmp, pytest.MonkeyPatch.context() as patch:
        root = Path(tmp).resolve() / "state"
        root.mkdir()
        harness = Harness(root)
        patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        service = Daemon(root)
        try:
            uncap(service)
            service.store.put_lane(codex_lane(root, "codex-2"))
            yield service, harness, patch
        finally:
            service.close()


SAYS_NOTHING = st.tuples(st.sampled_from([CUT, SLOW, Outcome(OutcomeClass.UNKNOWN, "probe unavailable: OSError"),
                                          Outcome(OutcomeClass.CONTENT_FILTER, "filtered", {"rc": 1})]),
                         st.sampled_from([0, 1, 7, 30, 59, 60, 61, 90]))
JOBS = st.lists(st.tuples(st.sampled_from(["astra", "terra"]), st.sampled_from([None, "codex-1", "codex-2"])),
                min_size=1, max_size=6)


@settings(max_examples=50, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(jobs=JOBS, outcomes=st.lists(st.one_of(st.just(OK), SAYS_NOTHING), max_size=12),
       due=st.lists(st.booleans(), min_size=1, max_size=8),
       bad=st.sets(st.sampled_from(["codex-1", "codex-2"]), max_size=1))
def test_a_slow_lane_never_keeps_a_job_from_a_lane_that_answers(jobs, outcomes, due, bad):
    """I3, I6 over generated jobs (two models, pinned to either lane or to none), outcomes that say
    nothing (of any length), passes with and without the clocks run down, and a slow lane."""
    with generated_fleet() as (service, harness, patch):
        ids = [submit(service, harness, pinned_model=model, pinned_lane=lane) for model, lane in jobs]
        wants = dict(zip(ids, jobs))
        probes = Probes(service, patch, outcomes, bad=bad)

        def check(first: int):
            last = {}                                   # each job's last probe of this pass
            for (job_id, _, model), cls in zip(probes.vehicles[first:], probes.classes[first:]):
                assert model == wants[job_id][0]                          # never a promoted model
                last[job_id] = cls
            for job_id, cls in last.items():
                job = service.store.get_job(job_id)
                if cls != OutcomeClass.OK:
                    # I3: due PROBE_RETRY_S after the probe's reservation, or now if that has passed.
                    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity")
                    reserved = moment(probes.reserved[job_id])
                    waited = (moment(job["next_check_at"]) - reserved).total_seconds()
                    ran = (moment(utcnow()) - reserved).total_seconds()
                    assert abs(waited - max(PROBE_RETRY_S, ran)) <= 2, (waited, ran)
            assert not service.store.query("SELECT 1 FROM leases WHERE holder LIKE 'probe:%'")
            assert len(started(service)) == len(set(started(service)))

        for make in due:
            end_attempts(service)
            if make:
                make_due(service)
            first = len(probes.vehicles)
            service._admit()
            check(first)
        # I6: once the other probes answer, every job that could run on a lane that
        # is not slow starts while the slow lane stays slow.
        probes.outcomes.clear()
        for _ in range(3 * len(ids) + 3):
            end_attempts(service)
            make_due(service)
            first = len(probes.vehicles)
            service._admit()
            check(first)
        def held_by_c6_9(index: int) -> bool:
            """An older job of the same model pinned to the slow lane holds this one (C-6.9)."""
            model, pin = jobs[index]
            return pin is None and any(other == (model, lane) for other in jobs[:index] for lane in bad)
        assert set(started(service)) == {job_id for index, job_id in enumerate(ids)
                                         if wants[job_id][1] not in bad and not held_by_c6_9(index)}
        # Once every probe answers, the rest start too.
        probes.bad.clear()
        for _ in range(2 * len(ids) + 2):
            end_attempts(service)
            make_due(service)
            service._admit()
        assert set(started(service)) == set(ids)
