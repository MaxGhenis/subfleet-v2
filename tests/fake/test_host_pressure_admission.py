"""C-6.15: admission under the opt-in host-pressure hold, against a real daemon and store."""

import logging

from subfleet import daemon as daemon_module, render
from subfleet.daemon import after, utcnow
from subfleet.host_pressure import GIB, Sampler
from tests.fake.test_admission_visibility import Inline, age, fleet, submit  # noqa: F401  (fixture)
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)


class Host:
    """A host whose compressor occupancy a test sets, read only outside transactions."""

    def __init__(self, service, gib, *, enabled=True):
        self.service, self.gib, self.now, self.reads = service, gib, 1000.0, 0
        service.policy["host_pressure"] = {"enabled": enabled, "compressor_max_gib": 40, "sample_s": 15}
        service._host_pressure = Sampler(self.read, lambda: self.now)

    def read(self):
        assert self.service.store._depth == 0, "vm_stat is never started inside a store transaction (C-3.3)"
        self.reads += 1
        return None if self.gib is None else int(self.gib * GIB)

    def admit(self):
        """What a tick does: the control loop's read on a worker of its own, then an admission pass."""
        self.service._host_pressure.refresh(self.service.policy["host_pressure"]["sample_s"])
        self.service._admit()


def end_attempt(service, job_id):
    attempt = service.store.list_attempts(job_id)[0]["attempt_id"]
    with service.store.transaction("fixture.attempt_ended") as tx:
        tx.execute("UPDATE attempts SET state='failed' WHERE attempt_id=?", (attempt,))
        tx.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (job_id,))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt, job_id))


def states(service, job_id):
    return [row["state"] for row in service.store.list_attempts(job_id)]


def test_a_job_is_held_while_the_host_is_under_pressure_and_says_why(fleet):  # noqa: F811
    """C-6.15, C-6.11: with an attempt in flight and the compressor above the threshold,
    the next job waits on `capacity`, its hold reads `host-pressure` with the reading,
    and `why` says what ends the wait."""
    service, harness = fleet
    host = Host(service, 66.1)
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    host.admit()
    assert states(service, first) == ["reserved"], "nothing was in flight: the first job starts whatever the host holds"
    assert states(service, second) == []
    job = service.store.get_job(second)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity")
    assert service._holds[second] == {"reason": "host-pressure", "compressor_gib": 66.1, "compressor_max_gib": 40,
                                      "next_check_at": job["next_check_at"]}
    answer = service.dispatch("why", {"job_id": second})
    assert "memory compressor holds 66.1 GiB" in answer["text"] and "(40)" in answer["text"]
    assert service._admission["reasons"] == {"host-pressure": 1}
    assert host.reads == 1


def test_the_queue_is_not_held_once_nothing_is_in_flight(fleet):  # noqa: F811
    """C-6.15: the hold ends with the last attempt in flight, however high the reading stays."""
    service, harness = fleet
    host = Host(service, 200)
    jobs = [submit(service, harness, pinned_model="terra") for _ in range(3)]
    host.admit()
    assert [states(service, job) for job in jobs] == [["reserved"], [], []]
    for running, waiting in zip(jobs, jobs[1:]):
        service.store.update_job(waiting, next_check_at=after(3600))       # backed off, however slow this machine is
        end_attempt(service, running)
        host.admit()                                                      # C-6.10: the freed lease brings it forward
        assert states(service, waiting) == ["reserved"]
    assert service.store.get_job(jobs[-1])["state"] != "waiting"


def test_a_child_starts_beside_the_parent_that_waits_for_it(fleet):  # noqa: F811
    """C-6.15 (review of 5ef95154): a running parent's own attempt does not hold the
    child it submitted, and a stranger submitted beside them is held."""
    service, harness = fleet
    host = Host(service, 200)
    parent = submit(service, harness, pinned_model="terra")
    host.admit()
    assert states(service, parent) == ["reserved"]
    child = submit(service, harness, pinned_model="astra", parent_job_id=parent)
    stranger = submit(service, harness, pinned_model="astra")
    host.admit()
    assert states(service, child) == ["reserved"]
    assert states(service, stranger) == [] and service._holds[stranger]["reason"] == "host-pressure"


def test_two_parents_waiting_on_held_children_do_not_deadlock(fleet):  # noqa: F811
    """C-6.15 (review of 15cc9f7e): P1 and P2 run, each submits a child and waits.
    Neither parent's attempt holds a child while that parent waits on a job that has
    not started, so the first child starts. Then P1 waits on nothing pending, and the
    second child is held behind P1 and its child; once those end, it starts."""
    service, harness = fleet
    service.policy["caps"].update(max_active_attempts=8, max_in_flight_per_lane=8)   # room: only pressure can hold
    host = Host(service, 30)                              # below the threshold: both parents start
    parents = [submit(service, harness, pinned_model="terra"), submit(service, harness, pinned_model="terra")]
    host.admit()
    assert [states(service, parent) for parent in parents] == [["reserved"], ["reserved"]]
    host.gib, host.now = 200, host.now + 15               # then the host comes under pressure
    children = [submit(service, harness, pinned_model="astra", parent_job_id=parent) for parent in parents]
    host.admit()
    assert states(service, children[0]) == ["reserved"]
    assert states(service, children[1]) == [] and service._holds[children[1]]["reason"] == "host-pressure"
    end_attempt(service, children[0])                     # the first child ends; its parent still runs
    service.store.update_job(children[1], next_check_at=after(3600))
    host.admit()                                          # C-6.10: the freed lease brings it forward
    assert states(service, children[1]) == [] and service._holds[children[1]]["reason"] == "host-pressure", \
        "held behind the first parent, which now waits on nothing that has not started"
    end_attempt(service, parents[0])                      # then that parent ends
    service.store.update_job(children[1], next_check_at=after(3600))
    host.admit()
    assert states(service, children[1]) == ["reserved"]


def test_the_hold_ends_when_the_reading_falls(fleet):  # noqa: F811
    """C-6.15: a later reading at or below the threshold places the job beside the running one."""
    service, harness = fleet
    host = Host(service, 66.1)
    submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    host.admit()
    assert service._holds[second]["reason"] == "host-pressure"
    host.gib, host.now = 12.0, host.now + 15
    service.store.update_job(second, next_check_at=utcnow())
    host.admit()
    assert states(service, second) == ["reserved"] and host.reads == 2


def test_a_reading_too_old_to_be_evidence_holds_nothing(fleet):  # noqa: F811
    """C-6.15: with nothing reading the host again, a reading more than four sample
    intervals old stops holding: `why` and the pass both see none."""
    service, harness = fleet
    host = Host(service, 66.1)
    submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    host.admit()
    assert service._holds[second]["reason"] == "host-pressure"
    job = service.store.get_job(second)
    host.now += 60                                     # four intervals: still evidence
    assert service._pick(job).chosen_lane is None
    host.now += 1                                      # and now it is not; no read has happened since the first
    assert service._pick(job).chosen_lane == "codex-1"
    service.store.update_job(second, next_check_at=utcnow())
    service._admit()
    assert states(service, second) == ["reserved"] and host.reads == 1


def drive(service, monkeypatch, ticks, each=None):
    """Run the control loop for `ticks` iterations with the pool run in the caller; the keys it was given."""
    offered = []
    service.workers = Inline(getattr(service.workers, "real", service.workers))
    service._recovery_complete.set()
    monkeypatch.setattr(service.timers, "tick", lambda: None)
    monkeypatch.setattr(service, "_last_maintenance", daemon_module.time.monotonic())
    real = service._schedule

    def schedule(key, fn, *args, paced=False):
        offered.append(key)
        if key in ("admission", "host-pressure"):      # a reserved attempt stays reserved: nothing is launched
            real(key, fn, *args, paced=paced)
    monkeypatch.setattr(service, "_schedule", schedule)
    count = [0]

    def wait(_):
        count[0] += 1
        if each is not None:
            each(count[0])
        if count[0] >= ticks:
            service.stopping.set()
    monkeypatch.setattr(service.stopping, "wait", wait)
    service.stopping.clear()
    service._control()
    return offered


def test_the_host_is_read_once_an_interval_on_a_worker_of_its_own(fleet, monkeypatch):  # noqa: F811
    """C-6.15, C-5.11: forty ticks start one `vm_stat`; the read is its own key, not admission's,
    so a slow one delays no pass; and it is read with nothing in flight too."""
    service, harness = fleet
    host = Host(service, 10)

    def each(tick):
        host.now += .05
    offered = drive(service, monkeypatch, 40, each)
    assert host.reads == 1 and offered.count("host-pressure") == 1 and offered.count("admission") == 40
    host.now += 15
    assert drive(service, monkeypatch, 3, each).count("host-pressure") == 1 and host.reads == 2


def test_admission_never_reads_the_host(fleet, monkeypatch):  # noqa: F811
    """C-6.15 (reviews of 5ef95154 and 15cc9f7e): an admission pass starts no `vm_stat`
    and waits for none, so a read that never returns delays no pass. Only the
    control loop's own key reads; with that read stuck, it is not offered again."""
    service, harness = fleet
    host = Host(service, 200)
    calls = []
    real_refresh = service._host_pressure.refresh
    monkeypatch.setattr(service._host_pressure, "refresh", lambda sample_s: calls.append(sample_s))
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    service._admit()
    service._admit()
    assert calls == [] and host.reads == 0
    assert states(service, first) == states(service, second) == ["reserved"]
    monkeypatch.setattr(service._host_pressure, "refresh", real_refresh)
    service._host_pressure._reading_now = True          # a `vm_stat` that has not come back
    assert "host-pressure" not in drive(service, monkeypatch, 2)


def test_off_the_host_is_never_read_and_holds_nothing(fleet, monkeypatch):  # noqa: F811
    """C-6.15: the shipped policy never starts `vm_stat`, whatever the host holds."""
    service, harness = fleet
    assert service.policy["host_pressure"]["enabled"] is False
    off = Host(service, 200, enabled=False)
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    offered = drive(service, monkeypatch, 3)
    assert states(service, first) == states(service, second) == ["reserved"]
    assert off.reads == 0 and "host-pressure" not in offered


def test_a_host_that_cannot_be_read_holds_nothing(fleet):  # noqa: F811
    """C-6.15: no reading is no evidence of pressure; the second job starts beside the first."""
    service, harness = fleet
    unreadable = Host(service, None)
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    unreadable.admit()
    assert states(service, first) == states(service, second) == ["reserved"] and unreadable.reads == 1


def test_the_pick_op_is_not_held(fleet):  # noqa: F811
    """C-6.15 (review of 5ef95154): `pick` advises a person's own session; the hold is admission's."""
    service, harness = fleet
    host = Host(service, 200)
    submit(service, harness, pinned_model="terra")
    held = submit(service, harness, pinned_model="terra")
    host.admit()
    assert service._holds[held]["reason"] == "host-pressure"
    def pick():
        answer = service.dispatch("pick", {"provider": "codex"})
        return {key: value for key, value in answer.items() if key != "generated_at"}
    assert "host_pressure" not in service._capacity_view(None), "only `_pick`'s view carries the reading"
    under_the_hold = pick()
    service.policy["host_pressure"]["enabled"] = False
    assert under_the_hold == pick()


def test_a_pressure_hold_is_ordinary_waiting_in_the_log_and_renders(fleet):  # noqa: F811
    """C-6.11: held by policy with lanes open is information, not a warning, and the hold is a sentence."""
    service, harness = fleet
    host = Host(service, 66.1)
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    submit(service, harness, pinned_model="terra")
    submit(service, harness, pinned_model="terra")
    host.admit()
    host.admit()                                       # a pass that places nothing starts the idle stretch
    age(service, daemon_module.ADMISSION_IDLE_LOG_S + 1)
    host.admit()
    assert [record.levelno for record in seen] == [logging.INFO] and "host-pressure x1" in seen[0].getMessage()
    lines = render.why_queue({"job_id": "j", "state": "waiting",
                              "hold": {"reason": "host-pressure", "compressor_gib": 66.1, "compressor_max_gib": 40}})
    assert any("66.1 GiB" in line and "C-6.15" in line for line in lines)
    assert "? GiB" in "\n".join(render.why_queue({"job_id": "j", "state": "waiting", "hold": {"reason": "host-pressure"}}))
