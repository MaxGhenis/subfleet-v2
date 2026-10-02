"""C-5.11: the control loop looks at every live attempt each tick and gives the
worker pool only those a pass could do something for.

The invariant is one-sided and is what makes the filter safe: whenever
`_has_work` says no, `_process_attempt` on that attempt returns None and changes
nothing. The filter may say yes when there is nothing to do; it may never say no
when there is.
"""

from __future__ import annotations

import itertools
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import daemon as daemon_module
from subfleet import procs
from subfleet.contracts import Credential, Lane, LaneOwner, attempt_dir
from subfleet.daemon import LIVE_ATTEMPTS, LIVE_TICK, Daemon, utcnow

JOB = "20261002-090000-prefilter"
ATTEMPT = JOB + "/a1"
FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
RECEIPT = json.dumps({"rc": 0, "signal": None, "wall_s": 3, "child_pid": 4243, "finished_at": "2026-10-02T09:00:03Z"})
#: What can be at `exit.json`: nothing, a receipt, a value a pass does not take
#: as one (`{}`, `null`), and bytes that are not JSON.
RECEIPTS = [None, RECEIPT, "{}", "null", ""]
LONG_AGO = "2026-09-01T00:00:00Z"


@pytest.fixture
def core(tmp_path):
    daemon = Daemon(tmp_path / "state")
    procs.forget_boot_id()
    home = tmp_path / "home"
    daemon.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                               str(home), LaneOwner.V2, False))
    daemon.store.add_job(job_id=JOB, request_id="p-1", payload_digest="d", kind="run", state="running",
                         workdir=str(tmp_path), prompt_path=str(tmp_path / "p.md"), sandbox="read-only",
                         started_at=utcnow())
    daemon.store.add_attempt(attempt_id=ATTEMPT, job_id=JOB, seq=1, lane_id="codex-1",
                             model_requested="astra", state="running", guardian_pid=4242,
                             child_pid=4243, pgid=4242, boot_id="boot", proc_start="start",
                             started_at=utcnow(), evidence_json=json.dumps({"owned": "x" * 9000}))
    attempt_dir(daemon.root, JOB, 1).mkdir(parents=True, exist_ok=True)
    yield daemon
    daemon.close()


def arrange(core, *, state="running", receipt=None, cancelled=False, started=None, max_wall_s=21600,
            inspect_in=3600.0, retry=False, failed=False):
    """Put the one attempt in a given situation; `inspect_in` is seconds until its next inspection (None: never set)."""
    with core.store.transaction("fixture.arrange") as tx:
        tx.execute("UPDATE attempts SET state=? WHERE attempt_id=?", (state, ATTEMPT))
        tx.execute("UPDATE jobs SET cancel_requested_at=?,started_at=?,max_wall_s=? WHERE job_id=?",
                   (utcnow() if cancelled else None, started or utcnow(), max_wall_s, JOB))
    path = attempt_dir(core.root, JOB, 1) / "exit.json"
    path.unlink(missing_ok=True)
    if receipt is not None:
        path.write_text(receipt)
    core._inspect_next.clear()
    core._inspect_retry.clear()
    core._worker_failures.clear()
    core._worker_retry_at.clear()
    core._children.clear()
    if inspect_in is not None:
        core._inspect_next[ATTEMPT] = time.monotonic() + inspect_in
    if retry:
        core._inspect_retry.add(ATTEMPT)
    if failed:
        core._worker_failures[ATTEMPT] = 1


def tick_row(core):
    rows = [row for row in core.store.query(LIVE_TICK) if row["attempt_id"] == ATTEMPT]
    return rows[0] if rows else None


def forbid_action(core, monkeypatch):
    """Every way a pass acts on an attempt; a pass that reaches one had work to do."""
    reached = []
    for name in ("_inspect_running", "_begin_finalizing", "_kill_attempt", "_launch", "_unlaunched",
                 "_finalize", "_contain", "_quarantine"):
        monkeypatch.setattr(core, name, lambda *args, _name=name, **kwargs: reached.append(_name))
    return reached


# --- the invariant: a no is always safe -------------------------------------------

def test_a_pass_the_filter_withholds_would_have_done_nothing(core, monkeypatch):
    """C-5.11: in every situation of a live attempt (all 1,920 combinations of state,
    receipt, cancel request, wall limit, inspection clock and pending retry), when
    `_has_work` says no the pass returns None, reaches no action, commits nothing and
    leaves the pacing as it was."""
    reached = forbid_action(core, monkeypatch)
    withheld = 0
    walls = [(None, 21600), (LONG_AGO, 1), (LONG_AGO, 10 ** 9)]
    for state, receipt, cancelled, (started, max_wall_s), inspect_in, retry, failed in itertools.product(
            ["reserved", "starting", "running", "finalizing"], RECEIPTS, [False, True], walls,
            [None, -5.0, 0.0, 3600.0], [False, True], [False, True]):
        arrange(core, state=state, receipt=receipt, cancelled=cancelled, started=started, max_wall_s=max_wall_s,
                inspect_in=inspect_in, retry=retry, failed=failed)
        if core._has_work(tick_row(core)):
            continue                              # offered, as before this clause: nothing to prove
        withheld += 1
        situation = (state, receipt, cancelled, started, max_wall_s, inspect_in, retry, failed)
        before = (core.store.generation, dict(core._inspect_next), set(core._inspect_retry),
                  core.store.get_attempt(ATTEMPT), core.store.get_job(JOB))
        assert core._process_attempt(ATTEMPT) is None, situation
        assert reached == [], situation
        assert before == (core.store.generation, dict(core._inspect_next), set(core._inspect_retry),
                          core.store.get_attempt(ATTEMPT), core.store.get_job(JOB)), situation
    # Withheld is rare by construction, and not empty: a running attempt inside
    # its wall limit, not due, nothing pending, with nothing at exit.json.
    assert withheld == 2


@settings(max_examples=300, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(receipt=st.sampled_from(RECEIPTS), cancelled=st.booleans(),
       age_s=st.integers(min_value=0, max_value=10 ** 6), max_wall_s=st.integers(min_value=1, max_value=10 ** 6),
       inspect_in=st.one_of(st.none(), st.floats(min_value=-10, max_value=10, allow_nan=False)))
def test_a_running_attempt_is_withheld_only_when_a_pass_would_do_nothing(core, monkeypatch, receipt, cancelled,
                                                                        age_s, max_wall_s, inspect_in):
    """C-5.11: the same invariant where it is close: a running attempt, for every age
    against every wall limit and every inspection clock around now."""
    started = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    arrange(core, receipt=receipt, cancelled=cancelled, started=started, max_wall_s=max_wall_s, inspect_in=inspect_in)
    offered = core._has_work(tick_row(core))
    # A second of margin on each side of the two clocks the test and the filter both read.
    surely_quiet = (receipt is None and not cancelled and age_s < max_wall_s - 2
                    and inspect_in is not None and inspect_in > 1)
    if surely_quiet:
        assert offered is False
    if offered:
        return
    reached = forbid_action(core, monkeypatch)
    generation = core.store.generation
    assert core._process_attempt(ATTEMPT) is None and reached == [] and core.store.generation == generation


# --- each thing a pass acts on brings one ------------------------------------------

def test_a_quiet_running_attempt_is_not_offered(core):
    """C-5.11: no receipt, no cancel request, inside its wall limit, not due for inspection."""
    arrange(core)
    assert core._has_work(tick_row(core)) is False


@pytest.mark.parametrize("change", [
    {"receipt": RECEIPT}, {"receipt": "{}"}, {"receipt": ""},          # a file at exit.json, whatever it holds
    {"cancelled": True},
    {"started": LONG_AGO, "max_wall_s": 1},                            # the wall limit
    {"inspect_in": -1.0}, {"inspect_in": 0.0}, {"inspect_in": None},   # an inspection due, or never yet scheduled
    {"retry": True}, {"failed": True},                                 # C-5.10: a pass that raised
    {"state": "reserved"}, {"state": "starting"}, {"state": "finalizing"},
])
def test_each_thing_a_pass_acts_on_is_offered(core, change):
    """C-5.11, C-5.10, C-5.12: any one of these alone makes the attempt the pool's this tick."""
    arrange(core, **change)
    assert core._has_work(tick_row(core)) is True


def test_a_guardian_that_ended_is_offered_so_the_pass_lets_go_of_it(core):
    """C-5.11: the pass is what drops an ended guardian from `_children`."""
    arrange(core)

    class Ended:
        def poll(self):
            return 0
    core._children[ATTEMPT] = Ended()
    assert core._has_work(tick_row(core)) is True
    core._children[ATTEMPT] = type("Running", (), {"poll": lambda self: None})()
    assert core._has_work(tick_row(core)) is False


def test_an_exit_receipt_that_cannot_be_looked_at_is_the_passes_to_report(core, monkeypatch):
    """C-5.11: every doubt is a yes: a `stat` that fails for any reason but absence offers the attempt."""
    arrange(core)

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(daemon_module.os, "stat", refuse)
    assert core._has_work(tick_row(core)) is True


# --- the tick's statement -------------------------------------------------------------

def test_the_ticks_statement_reads_no_evidence_and_finds_the_same_attempts(core):
    """C-5.11: one statement for every live attempt, naming no column kept on overflow
    pages, and it finds exactly the attempts the full-row statement finds."""
    assert "evidence_json" not in LIVE_TICK and "*" not in LIVE_TICK
    for state in ("reserved", "starting", "running", "finalizing", "succeeded", "quarantined", "lost"):
        arrange(core, state=state)
        narrow = {row["attempt_id"] for row in core.store.query(LIVE_TICK)}
        assert narrow == {row["attempt_id"] for row in core.store.query(LIVE_ATTEMPTS)}
    arrange(core, cancelled=True, started=LONG_AGO, max_wall_s=77)
    row = tick_row(core)
    job = core.store.get_job(JOB)
    assert set(row) == {"attempt_id", "job_id", "seq", "state", "cancel_requested_at", "job_started_at", "max_wall_s"}
    assert (row["job_id"], row["seq"], row["state"]) == (JOB, 1, "running")
    assert (row["cancel_requested_at"], row["job_started_at"], row["max_wall_s"]) == (
        job["cancel_requested_at"], job["started_at"], job["max_wall_s"])


# --- the control loop -------------------------------------------------------------------

def drive(core, monkeypatch, ticks, each=None):
    """Run the control loop for `ticks` iterations; the attempts it gave the pool, per tick."""
    offered = [[]]
    monkeypatch.setattr(core, "_recover_then_start_timers", lambda: None)
    monkeypatch.setattr(core, "_retention", lambda: None)
    real = core._schedule

    def schedule(key, fn, *args, paced=False):
        if fn == core._process_attempt:
            offered[-1].append(key)
        elif key not in ("timer-recovery", "retention"):
            real(key, fn, *args, paced=paced)
    monkeypatch.setattr(core, "_schedule", schedule)
    count = [0]

    def wait(_):
        count[0] += 1
        if each is not None:
            each(count[0])
        if count[0] >= ticks:
            core.stopping.set()
        else:
            offered.append([])
    monkeypatch.setattr(core.stopping, "wait", wait)
    core.stopping.clear()
    core._control()
    return offered


def test_a_quiet_attempt_costs_the_pool_nothing_and_a_receipt_brings_a_pass_at_once(core, monkeypatch):
    """C-5.11: the loop still looks every tick; the pool is given the attempt on the tick its receipt appears."""
    arrange(core)

    def each(tick):
        if tick == 3:
            (attempt_dir(core.root, JOB, 1) / "exit.json").write_text(RECEIPT)
    offered = drive(core, monkeypatch, 5, each)
    assert offered == [[], [], [], [ATTEMPT], [ATTEMPT]]


def test_an_attempt_due_for_inspection_is_offered_until_it_has_been(core, monkeypatch):
    """C-5.12: once the interval is up the attempt is offered each tick, as it was, until a pass moves it on."""
    arrange(core, inspect_in=-1.0)

    def each(tick):
        if tick == 2:
            core._inspect_next[ATTEMPT] = time.monotonic() + 3600      # what an inspection leaves
    assert drive(core, monkeypatch, 4, each) == [[ATTEMPT], [ATTEMPT], [], []]


def test_a_run_v1_still_owns_is_never_offered_and_one_it_settled_is(core, monkeypatch):
    """Migration principle 3, C-5.11: `imported_external` is read from the row while it says yes."""
    arrange(core, inspect_in=None)
    with core.store.transaction("fixture.imported") as tx:
        tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?",
                   (json.dumps({"imported": True, "imported_external": True}), ATTEMPT))

    def each(tick):
        if tick == 2:
            with core.store.transaction("fixture.settled") as tx:
                tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?",
                           (json.dumps({"imported": True, "imported_external": False}), ATTEMPT))
    assert drive(core, monkeypatch, 4, each) == [[], [], [ATTEMPT], [ATTEMPT]]


def test_an_attempt_of_this_daemons_own_has_its_evidence_read_once(core, monkeypatch):
    """C-5.11: "not imported" cannot change, so the 9 KB of evidence is read on the first tick only,
    and forgotten with the attempt."""
    arrange(core)
    reads = []
    real = core.store.one

    def one(sql, params=()):
        if "evidence_json" in sql:
            reads.append(params)
        return real(sql, params)
    monkeypatch.setattr(core.store, "one", one)
    drive(core, monkeypatch, 6)
    assert reads == [(ATTEMPT,)] and core._native == {ATTEMPT}
    arrange(core, state="succeeded")
    drive(core, monkeypatch, 1)
    assert core._native == set()
