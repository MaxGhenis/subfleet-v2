"""C-15.9 (D-WAIT-PRUNED1): a job retention retires under a `wait --mine` poll is skipped.

The daemon resolves `--mine` (and `--last`) through `list` when each long poll
starts, then reads every listed row while the poll lasts (C-15.4, C-15.5).
Retention (C-8.4) deletes a retired job's rows, and at hundreds of jobs an hour
it often retires one of a long-lived session's old jobs inside a 60 s poll. The
waiter's read then raised `unknown job` for a job its own resolver had listed:
2026-11-02 11:53Z, the Subfleet hub session's `subfleet wait --mine` exited 2
with `unknown job 20261006-004417-pr127-review-r6`. `test_c15_9_real_retention_*`
replays that against the archive driver's own retirement and fails on the base.

Now a poll that resolved its own ids skips a job whose row is gone and names it
in the answer's `retired`, on a timeout as on an answer. An id the caller named
is still unknown (exit 2). `runs --mine` reads its rows in one statement, so it
lists only what the store holds and has nothing to skip.

Invariants, each executed below for every mix of job fates Hypothesis draws:

* a resolved wait never raises for a retired job;
* its answer names exactly the session's live detached jobs, never a retired
  job, a turn, or another session's job;
* `retired` is exactly the resolved jobs retired during the poll;
* a wait that names a retired job raises `unknown-job`, exit 2, and one that
  names only live jobs never does.
"""

from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import protocol, retention
from subfleet.offline import Offline
from tests.fake.test_state_contract import state_daemon  # noqa: F401  (fixture)
from tests.fake.test_turn_listing import add

TERMINAL = ("succeeded", "failed", "cancelled", "lost")
OLD = "20261006-004417-pr127-review-r6"
LIVE = "20261102-115300-live-review"
HUB = "sess-hub"


def retire_rows(daemon, job_id: str) -> None:
    """Delete a job's rows as retention's commit does (`retention_archive`, C-8.4)."""
    attempts = "attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)"
    with daemon.store.transaction("retention.pruned", job_id=job_id) as conn:
        conn.execute(f"DELETE FROM artifacts WHERE {attempts}", (job_id,))
        conn.execute(f"DELETE FROM readings WHERE {attempts}", (job_id,))
        for table in ("notices", "decisions", "attempts", "jobs"):
            conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
    daemon._notify()


@contextmanager
def retiring(daemon, job_ids):
    """Retire `job_ids` after a wait has resolved and registered its jobs and
    before its first read: the window a retention commit lands in under a poll."""
    original = daemon.wait_hub.watching

    @contextmanager
    def watching(ids):
        with original(ids) as ready:
            for job_id in job_ids:
                retire_rows(daemon, job_id)
            yield ready

    daemon.wait_hub.watching = watching
    try:
        yield
    finally:
        del daemon.wait_hub.watching


def retire_everything_finished(daemon) -> dict:
    """One real retention pass with the budgets at zero: every finished job is over."""
    return retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {})


def in_thread(call):
    """Run `call` on a thread; the dict gets its `answer` or `error`."""
    out: dict = {}

    def run():
        try:
            out["answer"] = call()
        except Exception as exc:  # noqa: BLE001  (the test inspects it)
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def until_watched(daemon, count: int = 1) -> None:
    give_up = time.monotonic() + 10
    while daemon.wait_hub.watched < count:
        assert time.monotonic() < give_up, "the wait never registered with the hub"
        time.sleep(0.01)


@pytest.fixture
def hub_session(state_daemon):  # noqa: F811
    """The hub's shape: an old finished job and a live one, both dispatched by one session."""
    daemon, _ = state_daemon
    add(daemon, OLD, created_at="2026-10-06T00:44:17Z", session=HUB)
    add(daemon, LIVE, state="running", created_at="2026-11-02T11:53:00Z", session=HUB)
    return daemon


# --- the incident, against retention's own retirement --------------------------

def test_c15_9_real_retention_under_a_mine_poll_times_out_naming_the_retired_job(hub_session):
    """C-15.9, C-15.4 the incident: a running job keeps the poll open, retention retires the
    session's old job, and the poll's last read raised `unknown job` (exit 2). It now times
    out as any poll whose jobs are still running does, and says which job it skipped."""
    daemon = hub_session
    thread, out = in_thread(lambda: daemon.dispatch("wait", {"mine": HUB, "deadline_s": 2}))
    until_watched(daemon)
    assert retire_everything_finished(daemon)["pruned"] == [OLD]
    assert daemon.store.get_job(OLD) is None
    thread.join(15)
    assert not thread.is_alive()
    assert "error" not in out, out
    assert out["answer"] == {"timeout": True, "retired": [OLD]}


def test_c15_9_real_retention_then_the_live_job_ends_answers_it_alone(hub_session):
    """C-15.9, C-15.5 the hub wakes the waiter when its last live job ends; the answer is
    that job, and the job retention retired meanwhile is named in `retired`, not read."""
    daemon = hub_session
    thread, out = in_thread(lambda: daemon.dispatch("wait", {"mine": HUB, "deadline_s": 30}))
    until_watched(daemon)
    assert retire_everything_finished(daemon)["pruned"] == [OLD]
    daemon.store.update_job(LIVE, state="succeeded", rc=0, finished_at="2026-11-02T11:59:00Z")
    daemon._notify()
    thread.join(15)
    assert not thread.is_alive()
    assert "error" not in out, out
    answer = out["answer"]
    assert [job["job_id"] for job in answer["jobs"]] == [LIVE]
    assert answer["jobs"][0]["state"] == "succeeded"
    assert answer["timeout"] is False and answer["retired"] == [OLD]


def test_c15_9_real_retention_under_a_named_wait_is_still_an_unknown_job(hub_session):
    """C-15.9, C-17.3 an id the caller named is the caller's word: once retention retires
    it, the wait says so, exit 2, as it always did."""
    daemon = hub_session
    thread, out = in_thread(lambda: daemon.dispatch("wait", {"job_ids": [OLD, LIVE], "deadline_s": 2}))
    until_watched(daemon)
    assert retire_everything_finished(daemon)["pruned"] == [OLD]
    thread.join(15)
    assert not thread.is_alive()
    error = out.get("error")
    assert isinstance(error, protocol.ProtocolError), out
    assert error.reason == "unknown-job" and int(error.code) == 2
    assert str(error) == f"unknown job {OLD}"


def test_c15_9_runs_mine_lists_only_what_the_store_holds_after_retention(hub_session):
    """C-15.9, C-17.1, C-17.5 `runs --mine` reads its rows in one statement, online and
    offline, so a retired job is simply not listed: nothing to skip, nothing to fail."""
    daemon = hub_session
    assert [row["job_id"] for row in daemon.dispatch("list", {"mine": HUB})["jobs"]] == [LIVE, OLD]
    assert retire_everything_finished(daemon)["pruned"] == [OLD]
    assert [row["job_id"] for row in daemon.dispatch("list", {"mine": HUB})["jobs"]] == [LIVE]
    offline = Offline(daemon.root)
    assert [row["job_id"] for row in offline.list_jobs(session=HUB, last=0)] == [LIVE]


# --- the resolvers, deterministically ------------------------------------------

def test_c15_9_wait_last_skips_its_job_when_retention_retires_it(state_daemon):  # noqa: F811
    """C-15.9 `--last` resolves through `list` too: its one job, retired under the poll, is
    skipped and named, and there is nothing left to wait for."""
    daemon, _ = state_daemon
    add(daemon, OLD, created_at="2026-10-06T00:44:17Z", session=HUB)
    with retiring(daemon, [OLD]):
        answer = daemon.dispatch("wait", {"last": True, "deadline_s": 0})
    assert answer == {"jobs": [], "timeout": False, "retired": [OLD]}


def test_c15_9_a_job_retired_before_the_poll_is_neither_listed_nor_named(state_daemon):  # noqa: F811
    """C-15.9 a poll names only what it resolved: a job retired before it started is not in
    `list`, so it is neither waited on nor reported, and nothing is left to wait for."""
    daemon, _ = state_daemon
    add(daemon, OLD, created_at="2026-10-06T00:44:17Z", session=HUB)
    retire_rows(daemon, OLD)
    assert daemon.dispatch("wait", {"mine": HUB, "deadline_s": 0}) == {"jobs": [], "timeout": False}


@pytest.mark.parametrize("beside", [{}, {"mine": HUB}, {"last": True}], ids=["alone", "beside-mine", "beside-last"])
@pytest.mark.parametrize("when", ["before", "during"])
def test_c15_9_a_named_retired_job_is_still_unknown(state_daemon, beside, when):  # noqa: F811
    """C-15.9, C-17.3 a named id is never skipped, whatever resolver rides beside it, and
    whether its row went before the poll or during it."""
    daemon, _ = state_daemon
    add(daemon, OLD, created_at="2026-10-06T00:44:17Z", session=HUB)
    request = {"job_ids": [OLD], "deadline_s": 0, **beside}
    if when == "before":
        retire_rows(daemon, OLD)
        with pytest.raises(protocol.ProtocolError) as error:
            daemon.dispatch("wait", request)
    else:
        with retiring(daemon, [OLD]), pytest.raises(protocol.ProtocolError) as error:
            daemon.dispatch("wait", request)
    assert error.value.reason == "unknown-job" and int(error.value.code) == 2
    assert str(error.value) == f"unknown job {OLD}"


# --- the property ----------------------------------------------------------------

#: What happens to each of the session's detached jobs: it ended, it still runs,
#: retention retired it before the poll, or retention retired it during the poll.
FATES = ("ended", "running", "retired-before", "retired-during")
JOBS = st.lists(st.tuples(st.sampled_from(FATES), st.sampled_from(TERMINAL)), max_size=8)
#: Rows a resolver must never answer with: the session's turn jobs, and another
#: session's jobs, one of them retired during the poll.
NOISE = st.lists(st.sampled_from(("turn-ended", "turn-queued", "other-ended", "other-retired-during")),
                 max_size=4)
EXAMPLE = itertools.count()


def lay_out(daemon, fates, noise):
    """Add one example's rows, each id unique to it, and return them by fate."""
    n = next(EXAMPLE)
    session = f"sess-prop-{n}"
    by_fate: dict[str, list[str]] = {fate: [] for fate in FATES}
    by_fate["noise-retired-during"] = []
    states: dict[str, str] = {}
    for k, (fate, ended) in enumerate(fates):
        job_id = f"20261102-120000-x{n}-j{k}"
        state = "running" if fate == "running" else ended
        add(daemon, job_id, state=state, created_at=f"2026-11-02T12:{k:02d}:00Z", session=session)
        by_fate[fate].append(job_id)
        states[job_id] = state
    for k, shape in enumerate(noise):
        job_id = f"20261102-130000-x{n}-n{k}"
        created_at = f"2026-11-02T13:{k:02d}:00Z"
        if shape.startswith("turn"):
            add(daemon, job_id, kind="turn", state="queued" if shape == "turn-queued" else "succeeded",
                created_at=created_at, session=session)
        else:
            add(daemon, job_id, created_at=created_at, session=f"sess-other-{n}")
            if shape == "other-retired-during":
                by_fate["noise-retired-during"].append(job_id)
    for job_id in by_fate["retired-before"]:
        retire_rows(daemon, job_id)
    return session, by_fate, states


@given(fates=JOBS, noise=NOISE)
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_c15_9_property_wait_mine_waits_exactly_on_the_live_jobs(state_daemon, fates, noise):  # noqa: F811
    """C-15.9 for any mix of live and retired jobs: `wait --mine` never raises, answers with
    exactly the live ones once they have all ended, and names exactly the jobs retired under
    its poll; a turn or another session's job is never in either."""
    daemon, _ = state_daemon
    session, by_fate, states = lay_out(daemon, fates, noise)
    during = by_fate["retired-during"] + by_fate["noise-retired-during"]
    with retiring(daemon, during):
        answer = daemon.dispatch("wait", {"mine": session, "deadline_s": 0})
    named_retired = {"retired": sorted(by_fate["retired-during"])} if by_fate["retired-during"] else {}
    if by_fate["running"]:
        assert answer == {"timeout": True, **named_retired}
        return
    assert answer["timeout"] is False
    assert {key: value for key, value in answer.items() if key == "retired"} == named_retired
    answered = {job["job_id"]: job["state"] for job in answer["jobs"]}
    assert answered == {job_id: states[job_id] for job_id in by_fate["ended"]}


@given(fates=JOBS.filter(bool), picks=st.lists(st.booleans(), min_size=8, max_size=8))
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_c15_9_property_a_named_wait_fails_exactly_when_it_names_a_retired_job(state_daemon, fates, picks):  # noqa: F811
    """C-15.9, C-17.3 for any named set: unknown-job (exit 2) when it names a job retired
    before or during the poll, and an answer or a timeout when every named job is live."""
    daemon, _ = state_daemon
    session, by_fate, states = lay_out(daemon, fates, [])
    every = [job_id for fate in FATES for job_id in by_fate[fate]]
    named = [job_id for job_id, pick in zip(sorted(every), picks) if pick] or sorted(every)[:1]
    gone = set(by_fate["retired-before"]) | set(by_fate["retired-during"])
    with retiring(daemon, by_fate["retired-during"]):
        if gone & set(named):
            with pytest.raises(protocol.ProtocolError) as error:
                daemon.dispatch("wait", {"job_ids": named, "mine": session, "deadline_s": 0})
            assert error.value.reason == "unknown-job" and int(error.value.code) == 2
            assert str(error.value).removeprefix("unknown job ") in gone & set(named)
            return
        answer = daemon.dispatch("wait", {"job_ids": named, "mine": session, "deadline_s": 0})
    assert "retired" not in answer
    if any(states[job_id] == "running" for job_id in named):
        assert answer == {"timeout": True}
    else:
        assert {job["job_id"] for job in answer["jobs"]} == set(named)
