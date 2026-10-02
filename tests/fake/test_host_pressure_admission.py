"""C-6.15: admission under the opt-in host-pressure hold, against a real daemon and store."""

import logging

from subfleet import daemon as daemon_module, render
from subfleet.daemon import after, utcnow
from subfleet.host_pressure import GIB, Sampler
from tests.fake.test_admission_visibility import age, fleet, submit  # noqa: F401  (fixture)
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
    service._admit()
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


def test_the_queue_is_never_starved_once_nothing_is_in_flight(fleet):  # noqa: F811
    """C-6.15: the hold ends with the last attempt in flight, however high the reading stays."""
    service, harness = fleet
    Host(service, 200)
    jobs = [submit(service, harness, pinned_model="terra") for _ in range(3)]
    service._admit()
    assert [states(service, job) for job in jobs] == [["reserved"], [], []]
    for running, waiting in zip(jobs, jobs[1:]):
        service.store.update_job(waiting, next_check_at=after(3600))       # backed off, however slow this machine is
        end_attempt(service, running)
        service._admit()                                                  # C-6.10: the freed lease brings it forward
        assert states(service, waiting) == ["reserved"]
    assert service.store.get_job(jobs[-1])["state"] != "waiting"


def test_the_hold_ends_when_the_reading_falls(fleet):  # noqa: F811
    """C-6.15: a later reading at or below the threshold places the job beside the running one."""
    service, harness = fleet
    host = Host(service, 66.1)
    submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    service._admit()
    assert service._holds[second]["reason"] == "host-pressure"
    host.gib, host.now = 12.0, host.now + 15
    service.store.update_job(second, next_check_at=utcnow())
    service._admit()
    assert states(service, second) == ["reserved"] and host.reads == 2


def test_the_host_is_read_once_an_interval_however_many_passes_run(fleet):  # noqa: F811
    """C-6.15, C-5.11: a pass every 50 ms starts one `vm_stat` per `sample_s`."""
    service, harness = fleet
    host = Host(service, 10)
    submit(service, harness, pinned_model="terra")
    for _ in range(40):
        host.now += .05
        service._admit()
    assert host.reads == 1
    host.now += 15
    service._admit()
    assert host.reads == 2


def test_off_the_host_is_never_read_and_holds_nothing(fleet):  # noqa: F811
    """C-6.15: the shipped policy never starts `vm_stat`, whatever the host holds."""
    service, harness = fleet
    assert service.policy["host_pressure"]["enabled"] is False
    off = Host(service, 200, enabled=False)
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    service._admit()
    assert states(service, first) == states(service, second) == ["reserved"] and off.reads == 0


def test_a_host_that_cannot_be_read_holds_nothing(fleet):  # noqa: F811
    """C-6.15: no reading is no evidence of pressure; the second job starts beside the first."""
    service, harness = fleet
    unreadable = Host(service, None)
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    service._admit()
    assert states(service, first) == states(service, second) == ["reserved"] and unreadable.reads == 1


def test_a_reading_too_old_to_be_evidence_holds_nothing(fleet):  # noqa: F811
    """C-6.15: a reading four sample intervals old is not what the host holds now."""
    service, harness = fleet
    host = Host(service, 66.1)
    submit(service, harness, pinned_model="terra")
    service._admit()
    host.read = None                                   # nothing refreshes it from here
    service._host_pressure._read = lambda: (_ for _ in ()).throw(OSError("vm_stat is gone"))
    host.now += 61
    second = submit(service, harness, pinned_model="terra")
    service._admit()
    assert states(service, second) == ["reserved"]


def test_a_pressure_hold_is_ordinary_waiting_in_the_log_and_renders(fleet):  # noqa: F811
    """C-6.11: held by policy with lanes open is information, not a warning, and the hold is a sentence."""
    service, harness = fleet
    Host(service, 66.1)
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    submit(service, harness, pinned_model="terra")
    submit(service, harness, pinned_model="terra")
    service._admit()
    service._admit()                                   # a pass that places nothing starts the idle stretch
    age(service, daemon_module.ADMISSION_IDLE_LOG_S + 1)
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO] and "host-pressure x1" in seen[0].getMessage()
    lines = render.why_queue({"job_id": "j", "state": "waiting",
                              "hold": {"reason": "host-pressure", "compressor_gib": 66.1, "compressor_max_gib": 40}})
    assert any("66.1 GiB" in line and "C-6.15" in line for line in lines)
    assert "? GiB" in "\n".join(render.why_queue({"job_id": "j", "state": "waiting", "hold": {"reason": "host-pressure"}}))
