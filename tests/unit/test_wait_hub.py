"""C-15.5: one reader answers every `wait`.

Invariants pinned here:

- a wait returns within `WAIT_POLL_S` (0.1 s) plus the hub's and its own reads
  after the commit that ends its last job, even when nothing pokes the hub;
  with a poke, at once;
- the store reads that waiting costs do not grow with the number of waiters:
  one hub read per commit for all of them, and each waiter reads only when it
  starts and when its jobs are done;
- a waiter is woken only when all of its jobs may be returned: ended, and a
  succeeded job's export done; a job the store does not have is an error, as
  it always was;
- no wake-up is lost, however commits and registrations interleave;
- a waiter is always woken or times out: a hub pass that raises wakes every
  waiter, a hub thread that dies wakes them all and is started again, and a
  hub that cannot stay up leaves its waiters polling every `recheck_s`
  (property over generated schedules with injected failures and deaths);
- a stopping daemon answers every waiter at once.
"""

from __future__ import annotations

import random
import threading
import time

import pytest

from subfleet import protocol
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.waits import WAIT_POLL_S, WaitHub


@pytest.fixture
def daemon(tmp_path):
    core = Daemon(tmp_path / "state")
    home = tmp_path / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    core.test_root = tmp_path
    yield core
    core.close()


def add_job(core, job_id: str, state: str = "running") -> str:
    core.store.add_job(job_id=job_id, request_id="r-" + job_id, payload_digest="d", kind="run",
                       state=state, workdir=str(core.test_root), prompt_path=str(core.test_root / "p.md"),
                       sandbox="read-only")
    return job_id


def end(core, job_id: str, state: str = "succeeded", *, notify: bool = True) -> float:
    with core.store.transaction("test.ended", job_id=job_id) as tx:
        tx.execute("UPDATE jobs SET state=?,rc=0 WHERE job_id=?", (state, job_id))
    committed = time.monotonic()
    if notify:
        core._notify()
    return committed


class Waiter(threading.Thread):
    def __init__(self, core, job_ids, deadline_s=20):
        super().__init__()
        self.core, self.job_ids, self.deadline_s = core, job_ids, deadline_s
        self.result, self.error, self.returned = None, None, None

    def run(self):
        try:
            self.result = self.core.wait(protocol.WaitArgs(job_ids=self.job_ids, deadline_s=self.deadline_s))
        except Exception as exc:                            # noqa: BLE001
            self.error = exc
        self.returned = time.monotonic()


def started(core, *waiters, count=None):
    for waiter in waiters:
        waiter.start()
    deadline = time.monotonic() + 5
    while core.wait_hub.watched < (count or len(waiters)) and time.monotonic() < deadline:
        time.sleep(.002)
    assert core.wait_hub.watched >= (count or len(waiters))


def test_a_poked_wait_returns_at_once(daemon):
    job = add_job(daemon, "20260925-000001-poked")
    waiter = Waiter(daemon, [job])
    started(daemon, waiter)
    committed = end(daemon, job)
    waiter.join(5)
    assert waiter.result["timeout"] is False and waiter.result["jobs"][0]["state"] == "succeeded"
    assert waiter.returned - committed < .5


def test_an_unpoked_wait_returns_within_the_poll(daemon):
    """No `_notify` at all: the hub finds the commit on its own clock."""
    daemon.wait_hub.recheck_s = 3600                             # the generation alone must do it
    job = add_job(daemon, "20260925-000002-unpoked")
    waiter = Waiter(daemon, [job])
    started(daemon, waiter)
    time.sleep(3 * WAIT_POLL_S)                                  # the hub is idle, not mid-read
    committed = end(daemon, job, notify=False)
    waiter.join(5)
    assert waiter.result["timeout"] is False
    # The contract is WAIT_POLL_S plus the hub's and the waiter's reads; the margin is for a loaded machine.
    assert waiter.returned - committed < WAIT_POLL_S + .5


def test_waiting_costs_one_hub_read_per_commit_whatever_the_number_of_waiters(daemon, monkeypatch):
    daemon.wait_hub.recheck_s = 3600
    jobs = [add_job(daemon, f"20260925-{n:06d}-many") for n in range(50)]
    reads = []
    get_job = daemon.store.get_job
    monkeypatch.setattr(daemon.store, "get_job", lambda job_id: reads.append(job_id) or get_job(job_id))
    waiters = [Waiter(daemon, [job]) for job in jobs]
    started(daemon, *waiters)
    time.sleep(3 * WAIT_POLL_S)
    assert len(reads) == 50                                      # each waiter's first read
    before = daemon.wait_hub.reads
    for n in range(20):                                          # commits that end nothing watched
        with daemon.store.transaction("test.unrelated") as tx:
            tx.execute("INSERT INTO leases VALUES (?,?,?,NULL)", (f"lane:x{n}", "other", "t"))
        daemon._notify()
        time.sleep(2 * WAIT_POLL_S)
    assert len(reads) == 50                                      # no waiter read again
    assert 1 <= daemon.wait_hub.reads - before <= 20             # at most one hub read per commit
    for job in jobs:
        end(daemon, job, notify=False)
    daemon._notify()
    for waiter in waiters:
        waiter.join(5)
        assert waiter.result["timeout"] is False
    assert len(reads) == 100                                     # and one more each, for the answer


def test_a_wait_on_several_jobs_waits_for_the_last(daemon):
    first, second = add_job(daemon, "20260925-000010-first"), add_job(daemon, "20260925-000011-second")
    waiter = Waiter(daemon, [first, second])
    started(daemon, waiter)
    end(daemon, first)
    time.sleep(3 * WAIT_POLL_S)
    assert waiter.is_alive()
    end(daemon, second, "failed")
    waiter.join(5)
    assert [job["state"] for job in waiter.result["jobs"]] == ["succeeded", "failed"]


def test_a_succeeded_job_is_returned_only_after_its_export(daemon):
    job = add_job(daemon, "20260925-000020-exporting")
    with daemon.store.transaction("test.export") as tx:
        tx.execute("INSERT INTO leases VALUES ('out:/tmp/x.md',?,'t',NULL)", (job,))
    waiter = Waiter(daemon, [job])
    started(daemon, waiter)
    end(daemon, job)
    time.sleep(3 * WAIT_POLL_S)
    assert waiter.is_alive()
    with daemon.store.transaction("test.exported") as tx:
        tx.execute("DELETE FROM leases WHERE holder=?", (job,))
    daemon._notify()
    waiter.join(5)
    assert waiter.result["timeout"] is False


def test_a_failed_job_with_an_output_lease_is_returned(daemon):
    """Only a success waits for its export, as before C-15.5."""
    job = add_job(daemon, "20260925-000021-failed-lease")
    with daemon.store.transaction("test.export") as tx:
        tx.execute("INSERT INTO leases VALUES ('out:/tmp/y.md',?,'t',NULL)", (job,))
    waiter = Waiter(daemon, [job])
    started(daemon, waiter)
    end(daemon, job, "failed")
    waiter.join(5)
    assert waiter.result["jobs"][0]["state"] == "failed"


def test_an_unknown_job_is_an_error_at_once(daemon):
    started_at = time.monotonic()
    with pytest.raises(protocol.ProtocolError, match="unknown job"):
        daemon.wait(protocol.WaitArgs(job_ids=["20260925-000030-nothing"], deadline_s=20))
    assert time.monotonic() - started_at < 2
    assert daemon.wait_hub.watched == 0


def test_a_job_that_disappears_while_waited_on_is_an_error(daemon):
    job = add_job(daemon, "20260925-000031-pruned")
    waiter = Waiter(daemon, [job])
    started(daemon, waiter)
    with daemon.store.transaction("test.pruned") as tx:
        tx.execute("DELETE FROM jobs WHERE job_id=?", (job,))
    daemon._notify()
    waiter.join(5)
    assert isinstance(waiter.error, protocol.ProtocolError)


def test_nothing_to_wait_for_returns_at_once(daemon):
    assert daemon.wait(protocol.WaitArgs(job_ids=[], mine="nobody", deadline_s=20)) == {
        "jobs": [], "timeout": False}


def test_the_deadline_still_ends_a_wait(daemon):
    job = add_job(daemon, "20260925-000040-slow")
    began = time.monotonic()
    assert daemon.wait(protocol.WaitArgs(job_ids=[job], deadline_s=.3)) == {"timeout": True}
    assert .25 <= time.monotonic() - began < 3
    assert daemon.wait_hub.watched == 0


def test_a_stopping_daemon_answers_every_waiter(tmp_path):
    core = Daemon(tmp_path / "state")
    home = tmp_path / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    core.test_root = tmp_path
    waiters = [Waiter(core, [add_job(core, f"20260925-{n:06d}-stop")], deadline_s=60) for n in range(5)]
    started(core, *waiters)
    began = time.monotonic()
    core.stopping.set()
    core.wait_hub.stop()
    for waiter in waiters:
        waiter.join(5)
        assert waiter.result == {"timeout": True}
    assert time.monotonic() - began < 3
    core.close()


@pytest.mark.parametrize("seed", range(6))
def test_no_wake_up_is_lost(daemon, seed):
    """Registrations, commits and pokes in random orders: every waiter returns
    its ended job soon after the commit, whether or not anything poked the hub."""
    rng = random.Random(seed)
    jobs = [add_job(daemon, f"20260925-{seed:02d}{n:04d}-race") for n in range(24)]
    waiters = [Waiter(daemon, [job]) for job in jobs]
    ended: dict[str, float] = {}

    def ender():
        for job in rng.sample(jobs, len(jobs)):
            time.sleep(rng.choice((0, 0, .001, .01, .05)))
            ended[job] = end(daemon, job, notify=rng.random() < .5)
    enders = threading.Thread(target=ender)
    for waiter in waiters:                              # some register before, some after, their commit
        waiter.start()
        if rng.random() < .3:
            time.sleep(.002)
        if waiter is waiters[len(waiters) // 3]:
            enders.start()
    enders.join(20)
    for waiter in waiters:
        waiter.join(10)
        assert waiter.error is None and waiter.result["timeout"] is False, waiter.result
        lag = waiter.returned - ended[waiter.job_ids[0]]
        assert lag < WAIT_POLL_S + 1.0, (waiter.job_ids, lag)


def test_a_hub_whose_read_fails_wakes_its_waiters(tmp_path):
    """A failed hub read leaves no waiter asleep: each reads for itself."""
    class Broken:
        generation = 0
        def query(self, *_):
            raise RuntimeError("store closing")
    errors = []
    hub = WaitHub(Broken(), poll_s=.01, on_error=errors.append)
    with hub.watching(["x"]) as event:
        assert event.wait(2)
    hub.stop()
    assert errors and isinstance(errors[0], RuntimeError)


# --- the hub never leaves a waiter asleep (review of 5841d8b, low finding) --------------------

class Die(BaseException):
    """What ends a thread that catches `Exception`: the hub's thread dies of it."""


class FakeStore:
    """The two statements the hub makes, over jobs in memory, with failures to order."""

    def __init__(self, fail=None):
        self.states: dict[str, str] = {}
        self._generation = 0
        self.fail = fail or (lambda: None)
        self.lock = threading.Lock()

    @property
    def generation(self):
        return self._generation

    def add(self, job):
        with self.lock:
            self.states[job] = "running"
            self._generation += 1

    def end(self, job):
        with self.lock:
            self.states[job] = "succeeded"
            self._generation += 1

    def ended(self, jobs):
        with self.lock:
            return all(self.states.get(job) == "succeeded" for job in jobs)

    def query(self, sql, params):
        self.fail()
        with self.lock:
            if sql.startswith("SELECT job_id,state"):
                return [{"job_id": job, "state": self.states[job]} for job in params if job in self.states]
            return []


def wait_like_the_daemon(hub, store, jobs, deadline_s):
    """`Daemon.wait`'s loop over a fake store: read, and wait for the hub, until done or the deadline."""
    deadline = time.monotonic() + deadline_s
    with hub.watching(jobs) as ready:
        while True:
            ready.clear()
            if store.ended(jobs):
                return "answer", time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", time.monotonic()
            ready.wait(remaining)


def test_a_hub_pass_that_raises_outside_its_read_wakes_its_waiters_and_goes_on(tmp_path):
    """Before: only the read was guarded; anything else that raised in a pass (or an
    `on_error` that raised) ended the hub's thread, and every later wait ran to its
    deadline."""
    class Flaky(FakeStore):
        failing = True

        @property
        def generation(self):
            if self.failing:
                self.failing = False
                raise RuntimeError("generation")
            return self._generation
    store, errors = Flaky(), []
    hub = WaitHub(store, poll_s=.01, on_error=lambda exc: errors.append(exc) or 1 / 0)
    store.add("j")
    try:
        with hub.watching(["j"]) as event:
            assert event.wait(2)                        # the failed pass woke it
            event.clear()
            assert hub._thread.is_alive() and hub.deaths == 0
            store.end("j")
            hub.poke()
            assert event.wait(2)                        # and the hub still answers
        assert [type(exc) for exc in errors] == [RuntimeError]   # the raising on_error did not stop it
    finally:
        hub.stop()


def test_a_hub_whose_thread_dies_wakes_everyone_and_is_started_again():
    deaths = [1]

    def fail():
        if deaths[0]:
            deaths[0] -= 1
            raise Die()
    store, errors = FakeStore(fail), []
    hub = WaitHub(store, poll_s=.01, recheck_s=.05, on_error=errors.append)
    store.add("j")
    try:
        with hub.watching(["j"]) as event:
            assert event.wait(2)                        # woken as the thread died
            deadline = time.monotonic() + 5
            while hub._thread is not None and time.monotonic() < deadline:
                time.sleep(.01)
            assert hub.deaths == 1 and isinstance(errors[0], Die)
            event.clear()
            time.sleep(.06)                             # past recheck_s since it started
            store.end("j")
            assert event.wait(2)                        # this wait started a new hub, which woke it
            assert hub.restarts == 1 and hub._thread is not None and hub._thread.is_alive()
    finally:
        hub.stop()


def test_a_hub_that_cannot_stay_up_turns_its_waiters_into_slow_pollers():
    """Its thread dies at every start: each wait is cut to `recheck_s`, so a waiter
    still returns soon after its job ends, and still times out at its deadline."""
    def fail():
        raise Die()
    store = FakeStore(fail)
    hub = WaitHub(store, poll_s=.01, recheck_s=.2, on_error=lambda exc: None)
    store.add("done"), store.add("never")
    try:
        threading.Timer(.5, store.end, args=("done",)).start()
        began = time.monotonic()
        outcome, at = wait_like_the_daemon(hub, store, ["done"], 10)
        assert outcome == "answer" and at - began < .5 + .2 + 1.0, at - began
        began = time.monotonic()
        outcome, at = wait_like_the_daemon(hub, store, ["never"], .6)
        assert outcome == "timeout" and .55 <= at - began < .6 + 1.0, at - began
        assert hub.deaths >= 2
    finally:
        hub.stop()


PROPERTY_CASES = 20
#: Scheduling on a loaded machine.
SLACK_S = 1.0


@pytest.mark.parametrize("case", range(PROPERTY_CASES))
def test_every_waiter_is_woken_or_times_out(case):
    """Property: over generated schedules of waiters, job ends, pokes, failed hub
    passes and hub deaths, every waiter returns its answer within `poll_s` +
    `recheck_s` (+ slack) of its last job ending, when that is before its deadline,
    and otherwise returns (answer or timeout) by its deadline (+ slack); none hangs."""
    rng = random.Random(5000 + case)
    guard = threading.Lock()
    p_fail, p_die = rng.choice((0, .05, .2)), rng.choice((0, 0, .02, .1))

    def fail():
        with guard:
            roll = rng.random()
        if roll < p_die:
            raise Die()
        if roll < p_die + p_fail:
            raise RuntimeError("injected")
    store = FakeStore(fail)
    poll, recheck = .02, rng.choice((.1, .2))
    hub = WaitHub(store, poll_s=poll, recheck_s=recheck, on_error=lambda exc: None)
    plans = []
    for n in range(rng.randrange(1, 9)):
        jobs = [f"c{case}-w{n}-j{k}" for k in range(rng.randrange(1, 3))]
        for job in jobs:
            store.add(job)
        plans.append({"jobs": jobs, "deadline": rng.choice((.5, 1.5, 3.0)),
                      "ends": {job: rng.choice((0, .05, .3, .8, 2.0, None)) for job in jobs}})
    results = [None] * len(plans)
    ended_at: dict[str, float] = {}

    def waiter(index, plan):
        results[index] = (time.monotonic(), *wait_like_the_daemon(hub, store, plan["jobs"], plan["deadline"]))

    def ender():
        began = time.monotonic()
        schedule = sorted((after, job) for plan in plans for job, after in plan["ends"].items() if after is not None)
        for after, job in schedule:
            time.sleep(max(0.0, began + after - time.monotonic()))
            store.end(job)
            ended_at[job] = time.monotonic()
            with guard:
                poke = rng.random() < .5
            if poke:
                hub.poke()
    threads = [threading.Thread(target=waiter, args=(index, plan)) for index, plan in enumerate(plans)]
    try:
        for thread in threads:
            thread.start()
        closing = threading.Thread(target=ender)
        closing.start()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive(), (case, "a waiter hung")
        closing.join(10)
    finally:
        hub.stop()
    for plan, (started_at, outcome, returned) in zip(plans, results):
        deadline = started_at + plan["deadline"]
        assert returned <= deadline + SLACK_S, (case, plan, returned - deadline)
        last = max((ended_at.get(job, float("inf")) for job in plan["jobs"]), default=float("inf"))
        if last + poll + recheck + SLACK_S < deadline:
            assert outcome == "answer", (case, plan, outcome)
            assert returned - last <= poll + recheck + SLACK_S, (case, plan, returned - last)
        if outcome == "answer":
            assert last <= returned, (case, plan)
