"""C-18.5: a timer run that an exception ends is seen by an operator, and its events are bounded.

Until 2026-10-10 `Timers._run` kept only the exception's type, in `last_error_type`
(replaced by the next run) and in a `timer.error` event per failed run that no command
read; `status` and `doctor` said nothing, and `mirror_hot` could write two events every
2 s for as long as it failed. Every test runs on a store under a temporary directory;
none reads or writes `~/.subfleet`.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, doctor, render
from subfleet import timers as timers_module
from subfleet.offline import Offline
from subfleet.store import Store
from subfleet.timers import (FAILURE_CHANGE_S, FAILURE_HEARTBEAT_S, FAILURE_KINDS_MAX, FAILURE_MESSAGE_CHARS,
                             FAILURE_VISIBLE_S, RECORDED_QUERY, Timers, iso, recorded)

START = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
POLICY = json.loads(Path("subfleet/default_policy.json").read_text())
#: MOCK credential-shaped text, built here so no literal key sits in the file.
MOCK_KEY = "api_" + "key=" + "Mock1234Value5678Abcd"
MOCK_VALUE = MOCK_KEY.split("=", 1)[1]
#: Distinct exception types: each is a kind of failure (type and place) of its own.
FAULTS = [type(f"Fault{index}", (Exception,), {}) for index in range(FAILURE_KINDS_MAX + 2)]


class Clock:
    def __init__(self):
        self.at = START

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


@contextlib.contextmanager
def rig(root=None):
    """A `Timers` over a store under a temporary directory, on a clock the test moves."""
    with contextlib.ExitStack() as stack:
        root = Path(root or stack.enter_context(tempfile.TemporaryDirectory(prefix="sf-timer-")))
        store = stack.enter_context(Store(root / "state.sqlite3"))
        clock = Clock()
        timer = Timers(store, root, POLICY, now=clock)
        stack.callback(timer.stop)
        yield timer, store, clock, root


def run(timer, name, outcome):
    """One run of `name`: clean when `outcome` is None, else it raises `outcome` (an
    exception, or a callable that raises from inside the package). A clean hot pass
    changes nothing."""
    def cycle():
        if outcome is None:
            return False
        if callable(outcome) and not isinstance(outcome, BaseException):
            outcome()
        raise outcome
    setattr(timer, name + "_cycle", cycle)
    timer._run(name)


def from_instant():
    """Raises ValueError inside `subfleet/timers.py` (`instant`)."""
    timers_module.instant("not a date")


def events(store, kind, name):
    found = []
    for row in store.query("SELECT data_json FROM events WHERE kind=? ORDER BY event_id", (kind,)):
        data = json.loads(row["data_json"])
        if data.get("timer") == name:
            found.append(data)
    return found


def stored(store):
    return recorded(store.query(RECORDED_QUERY))


# --- what is kept -------------------------------------------------------------

def test_a_failure_is_kept_with_its_type_message_place_and_streak():
    """C-18.5: since when, how many runs in a row, and with what."""
    with rig() as (timer, store, clock, _):
        run(timer, "keepalive", from_instant)
        clock.advance(60)
        run(timer, "keepalive", from_instant)
        status = timer.status()["keepalive"]
        failure = status["failure"]
        assert status["last_error_type"] == "ValueError"
        assert failure["first_at"] == failure["since"] == iso(START)
        assert failure["last_at"] == iso(START + timedelta(seconds=60))
        assert failure["runs"] == failure["failed_runs"] == 2 and failure["recovered_at"] is None
        assert failure["error_type"] == "ValueError" and "isoformat" in failure["message"]
        assert failure["raised_at"].startswith("timers.py:") and failure["raised_at"].endswith(" in instant")
        first = events(store, "timer.error", "keepalive")[0]
        assert first["failed_runs"] == 1 and first["raised_at"] == failure["raised_at"]
        assert first["trace"][-1] == failure["raised_at"] and first["trace"][0].endswith(" in _run")
        assert all(frame.split(":")[0].endswith(".py") for frame in first["trace"])


def test_the_place_is_the_innermost_frame_under_the_package():
    """C-18.5: an exception raised outside the package is placed at the last package
    frame it came through, and the trace names no frame outside it."""
    with rig() as (timer, store, _, _):
        run(timer, "mirror", KeyError("sessionId"))
        failure = timer.status()["mirror"]["failure"]
        assert failure["raised_at"].startswith("timers.py:") and failure["raised_at"].endswith(" in _run")
        assert failure["message"] == "'sessionId'"
        assert all(frame.startswith("timers.py:") for frame in events(store, "timer.error", "mirror")[0]["trace"])


def test_a_message_is_scrubbed_then_bounded_to_one_line():
    """C-18.5, C-23.14: a credential in the message never reaches status, events or doctor."""
    with rig() as (timer, store, _, root):
        run(timer, "keepalive", RuntimeError(f"read failed:\n  {MOCK_KEY}\n" + "and then some more words " * 200))
        message = timer.status()["keepalive"]["failure"]["message"]
        assert len(message) <= FAILURE_MESSAGE_CHARS and "\n" not in message
        assert message.startswith("read failed: api_key=[REDACTED]") and message.endswith("…")
        everything = json.dumps(store.query("SELECT data_json FROM events")) + json.dumps(timer.status())
        assert MOCK_VALUE not in everything
        assert MOCK_VALUE not in json.dumps(doctor.check_timers(root))


def test_a_message_too_long_to_scrub_is_left_out():
    with rig() as (timer, _, _, _):
        run(timer, "keepalive", RuntimeError("y" * (timers_module.FAILURE_SCRUB_MAX_CHARS + 1)))
        assert timer.status()["keepalive"]["failure"]["message"].startswith("(a message of 1,000,001 characters")


# --- how long it is seen --------------------------------------------------------

def test_a_failure_stays_visible_for_a_day_after_a_clean_run():
    """C-18.5: a later clean run does not hide a failure for a day; then it goes, and the
    next failure starts a record of its own."""
    with rig() as (timer, _, clock, _):
        run(timer, "mirror", from_instant)
        clock.advance(60)
        run(timer, "mirror", None)
        failure = timer.status()["mirror"]["failure"]
        assert failure["recovered_at"] == iso(START + timedelta(seconds=60)) and failure["runs"] == 1
        assert timer.status()["mirror"]["last_error_type"] is None
        clock.advance(FAILURE_VISIBLE_S - 1)
        assert timer.status()["mirror"]["failure"] == failure
        clock.advance(1)
        assert timer.status()["mirror"]["failure"] is None
        run(timer, "mirror", KeyError("x"))
        failure = timer.status()["mirror"]["failure"]
        assert failure["first_at"] == iso(clock()) and failure["failed_runs"] == 1


def test_a_failure_within_the_day_continues_the_record():
    with rig() as (timer, _, clock, _):
        run(timer, "mirror", from_instant)
        clock.advance(60)
        run(timer, "mirror", None)
        clock.advance(60)
        run(timer, "mirror", KeyError("x"))
        failure = timer.status()["mirror"]["failure"]
        assert failure["first_at"] == iso(START) and failure["since"] == iso(clock())
        assert failure["runs"] == 1 and failure["failed_runs"] == 2 and failure["error_type"] == "KeyError"


def test_a_reported_error_type_opens_no_record():
    """C-18.5: a run that ends without an exception is clean whatever type it reports
    (a probe cycle whose lane read failed, a retention pass its deadline stopped)."""
    with rig() as (timer, store, _, _):
        timer.mark("retention", error="TimeoutError")
        status = timer.status()["retention"]
        assert status["last_error_type"] == "TimeoutError" and status["failure"] is None
        assert not events(store, "timer.error", "retention")


def test_a_restart_carries_the_record_on():
    """C-18.5: the record is in every `timer.run` and read back at start."""
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        with rig(root) as (timer, _, clock, _):
            for _ in range(3):
                run(timer, "keepalive", from_instant)
                clock.advance(60)
            before = timer.status()["keepalive"]["failure"]
        with rig(root) as (timer, store, clock, _):
            clock.at = START + timedelta(seconds=180)
            assert timer.status()["keepalive"]["failure"] == before
            run(timer, "keepalive", from_instant)
            assert timer.status()["keepalive"]["failure"]["runs"] == 4
            # The first failed run this process sees is told, whatever the count.
            assert events(store, "timer.error", "keepalive")[-1]["failed_runs"] == 4


# --- how many events --------------------------------------------------------------

@pytest.mark.parametrize("name,interval", [("mirror_hot", 2), ("mirror", 60), ("keepalive", 18300)])
def test_a_timer_failing_on_every_run_writes_a_logarithmic_number_of_errors(name, interval):
    """C-18.5: of 1000 failed runs, the first and each power of two are told, and any
    run an hour or more after the last event; the record still counts all 1000. For
    the 2 s hot pass that is ten `timer.error` events (before: 1000); for the 60 s
    mirror ten plus one an hour; for the five-hourly keepalive, every run."""
    with rig() as (timer, store, clock, _):
        for _ in range(1000):
            run(timer, name, from_instant)
            clock.advance(interval)
        told = [event["failed_runs"] for event in events(store, "timer.error", name)]
        heartbeat = FAILURE_HEARTBEAT_S // interval
        expected = [n for n in range(1, 1001) if n & (n - 1) == 0 or n > 1 and interval >= FAILURE_HEARTBEAT_S]
        if interval < FAILURE_HEARTBEAT_S:
            expected, last = [], None
            for n in range(1, 1001):
                if last is None or n & (n - 1) == 0 or n - last >= heartbeat:
                    expected.append(n)
                    last = n
        assert told == expected
        assert len(told) == {2: 10, 60: 10 + 15, 18300: 1000}[interval]
        assert timer.status()[name]["failure"]["runs"] == 1000
        runs = events(store, "timer.run", name)
        assert len(runs) == (len(told) if name == "mirror_hot" else 1000)


def test_a_new_kind_is_told_until_the_cap():
    """C-18.5: each kind (type and place) is told once, up to FAILURE_KINDS_MAX of them."""
    with rig() as (timer, store, clock, _):
        for fault in FAULTS:
            run(timer, "mirror_hot", fault("x"))
            clock.advance(2)
        types = [event["error_type"] for event in events(store, "timer.error", "mirror_hot")]
        assert types == [fault.__name__ for fault in FAULTS[:FAILURE_KINDS_MAX]]


def test_a_hot_timer_records_a_change_within_a_minute():
    """C-18.5: `mirror_hot` records its clean passes only when they change something, so
    a change between failing and not is written at most once a minute."""
    with rig() as (timer, store, clock, _):
        run(timer, "mirror_hot", from_instant)
        assert stored(store)["mirror_hot"]["failure"]["recovered_at"] is None
        for _ in range(29):                      # 2 s to 58 s: clean, not yet written
            clock.advance(2)
            run(timer, "mirror_hot", None)
            assert stored(store)["mirror_hot"]["failure"]["recovered_at"] is None
        clock.advance(2)                         # 60 s: written
        run(timer, "mirror_hot", None)
        assert stored(store)["mirror_hot"]["failure"]["recovered_at"] == iso(START + timedelta(seconds=2))
        assert len(events(store, "timer.run", "mirror_hot")) == 2


def test_a_record_write_that_raises_never_leaves_the_timer_running(monkeypatch):
    """C-18.5: before, a `timer.error` write that raised skipped the rest of `_run`, and
    the timer stayed "already running" until a restart. The failed run that follows
    writes what was not written."""
    with rig() as (timer, store, clock, _):
        real = store.add_event

        def broken(kind, **fields):
            raise OSError("disk full")
        monkeypatch.setattr(store, "add_event", broken)
        with pytest.raises(OSError):
            run(timer, "keepalive", from_instant)
        assert "keepalive" not in timer._running
        monkeypatch.setattr(store, "add_event", real)
        clock.advance(60)
        run(timer, "keepalive", from_instant)
        told = events(store, "timer.error", "keepalive")
        assert [event["failed_runs"] for event in told] == [2]
        assert timer.status()["keepalive"]["failure"]["first_at"] == iso(START)


# --- the readers ------------------------------------------------------------------

def status_text(data):
    return cli.format_status({"lanes": [], "readings": [], **data})


def test_status_prints_failing_cleared_and_reported_timers():
    """C-18.5: `subfleet status` names each timer to look at, most urgent first."""
    with rig() as (timer, _, clock, _):
        run(timer, "mirror_hot", from_instant)
        clock.advance(2)
        run(timer, "mirror_hot", from_instant)
        run(timer, "mirror", KeyError("sessionId"))
        clock.advance(60)
        run(timer, "mirror", None)
        timer.mark("probe", error="URLError")
        lines = status_text({"timers": timer.status()}).splitlines()
    assert lines[0] == "timers: 1 failing, 1 failed in the last day, 1 reported an error"
    assert lines[1].startswith("  mirror_hot  failing since 2026-10-10T12:00:00Z: 2 runs in a row, the latest at "
                               "2026-10-10T12:00:02Z: ValueError: Invalid isoformat string: 'not a date' (timers.py:")
    assert lines[2].startswith("  mirror      ran clean at 2026-10-10T12:01:02Z after a failed run at "
                               "2026-10-10T12:00:02Z: KeyError: 'sessionId' (timers.py:")
    assert lines[3] == "  probe       its last run, at 2026-10-10T12:01:02Z, reported URLError"


def test_status_says_nothing_of_timers_that_run_clean_and_reads_an_older_daemon():
    clean = {"probe": {"last_run": "2026-10-10T12:00:00Z", "next_due": None, "last_error_type": None, "failure": None}}
    assert "timers" not in status_text({"timers": clean})
    older = {"mirror": {"last_run": "2026-10-10T12:00:00Z", "next_due": None, "last_error_type": "OSError"}}
    assert "mirror  its last run, at 2026-10-10T12:00:00Z, reported OSError" in status_text({"timers": older})


def test_offline_status_reads_the_record_from_the_store(monkeypatch):
    """C-17.5, C-18.5: with no daemon, `status` and `status --json` read the store."""
    with rig() as (timer, _, clock, root):
        run(timer, "keepalive", from_instant)
        monkeypatch.setenv("SUBFLEET_HOME", str(root))
        assert Offline(root).status()["timers"]["keepalive"] == timer.status()["keepalive"]
        for as_json in (False, True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                assert cli.cmd_status(argparse.Namespace(json=as_json)) == 0
            if as_json:
                assert json.loads(out.getvalue())["timers"]["keepalive"]["failure"]["runs"] == 1
            else:
                assert "keepalive  failed at 2026-10-10T12:00:00Z, its last run: ValueError" in out.getvalue()


def test_doctor_fails_while_a_timer_fails_and_warns_for_a_day_after():
    """C-18.5, C-17.3: `fail` (exit 1) while the last recorded run failed; `warn` after a
    clean run and for a reported error; `pass` otherwise."""
    with rig() as (timer, _, clock, root):
        assert doctor.check_timers(root)["status"] == doctor.UNKNOWN
        run(timer, "mirror", None)
        assert doctor.check_timers(root)["status"] == doctor.PASS
        run(timer, "mirror", from_instant)
        item = doctor.check_timers(root)
        assert item["status"] == doctor.FAIL and doctor.exit_code([item]) == 1
        assert item["detail"].startswith("mirror failed at 2026-10-10T12:00:00Z, its last run: ValueError")
        assert "timer.error" in item["fix"]
        clock.advance(60)
        run(timer, "mirror", None)
        item = doctor.check_timers(root)
        assert item["status"] == doctor.WARN and doctor.exit_code([item]) == 0
        clock.advance(FAILURE_VISIBLE_S)
        run(timer, "mirror", None)
        timer.mark("probe", error="URLError")
        item = doctor.check_timers(root)
        assert item["status"] == doctor.WARN and item["detail"].startswith("probe its last run")
        timer.mark("probe")
        assert doctor.check_timers(root)["status"] == doctor.PASS


def test_doctor_reads_the_store_when_no_store_exists(tmp_path):
    assert doctor.check_timers(tmp_path)["status"] == doctor.UNKNOWN


def test_doctor_has_the_row_offline_and_live(daemon, root, monkeypatch):
    """C-18.5: `daemon timers` is in the offline table; `--live` adds the daemon's own."""
    monkeypatch.setattr(doctor, "_run", lambda argv, timeout=20.0: (0, "stub"))
    monkeypatch.setattr(doctor, "_run_full", lambda argv, timeout=20.0: (0, "stub"))
    failing = {"mirror_hot": {"last_run": iso(START), "next_due": None, "last_error_type": "ValueError",
                              "failure": {"first_at": iso(START), "since": iso(START), "last_at": iso(START),
                                          "runs": 3, "failed_runs": 3, "error_type": "ValueError",
                                          "message": "bad", "raised_at": "timers.py:1 in instant",
                                          "recovered_at": None}}}
    replies = {"daemon.status": lambda request: {"timers": failing},
               "ping": lambda request: {"pong": True, "version": "t"}}
    daemon(replies)
    names = {item["check"] for item in doctor.checks(root)}
    assert "daemon timers" in names and "daemon timers (live)" not in names
    live = {item["check"]: item for item in doctor.checks(root, live=True)}
    assert live["daemon timers (live)"]["status"] == doctor.FAIL
    assert "mirror_hot failing since" in live["daemon timers (live)"]["detail"]
    failing["mirror_hot"].pop("failure")
    assert doctor.check_timers_live(root)["status"] == doctor.UNKNOWN


def test_render_notes_are_ordered_by_urgency():
    notes = render.timer_notes({
        "b": {"last_error_type": "X"},
        "a": {"last_error_type": None, "failure": {"error_type": "E", "runs": 1, "failed_runs": 1,
                                                   "last_at": "t", "recovered_at": None}}})
    assert [(state, name) for state, name, _ in notes] == [("failing", "a"), ("reported", "b")]


# --- properties over sequences of runs ----------------------------------------------

#: A step: the run's outcome (None: clean; i: raises FAULTS[i]), the seconds before
#: the next run, how many times it repeats, and (for `keepalive`) whether the daemon
#: restarts after it.
STEP = st.tuples(st.one_of(st.none(), st.integers(0, len(FAULTS) - 1)),
                 st.one_of(st.integers(1, 120), st.integers(3000, 4000), st.integers(80000, 100000)),
                 st.integers(1, 40), st.booleans())


def expected_record(history, now):
    """The failure record C-18.5 defines, from the whole history of `(time, fault)` runs,
    written apart from `timers.advance`. None when there is none to show at `now`."""
    failed = [index for index, (_, fault) in enumerate(history) if fault is not None]
    if not failed:
        return None
    start = failed[0]
    for previous, index in zip(failed, failed[1:]):
        clean = [at for at, fault in history[previous + 1:index] if fault is None]
        if clean and (history[index][0] - clean[0]).total_seconds() >= FAILURE_VISIBLE_S:
            start = index                         # the old record was gone: a new one
    last = failed[-1]
    after = [at for at, fault in history[last + 1:] if fault is None]
    if after and (now - after[0]).total_seconds() >= FAILURE_VISIBLE_S:
        return None
    streak = last
    while streak > 0 and history[streak - 1][1] is not None:
        streak -= 1
    streak = max(streak, start)
    return {"first_at": iso(history[start][0]), "since": iso(history[streak][0]), "last_at": iso(history[last][0]),
            "runs": last - streak + 1, "failed_runs": sum(1 for index in failed if index >= start),
            "error_type": FAULTS[history[last][1]].__name__, "recovered_at": iso(after[0]) if after else None}


def failing(record):
    return record is not None and record["recovered_at"] is None


def assert_matches(record, model):
    """`record` has every field of `model` with its value, or both are None."""
    if model is None:
        assert record is None, record
    else:
        assert record is not None and {key: record[key] for key in model} == model, (record, model)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(name=st.sampled_from(["keepalive", "mirror_hot"]), steps=st.lists(STEP, min_size=1, max_size=25))
def test_the_record_and_its_events_over_any_sequence_of_runs(name, steps):
    """C-18.5 invariants, for every sequence of clean and raising runs:

    1. the record equals the reference model after every run: `runs` is the number of
       failed runs since the last clean run, `failed_runs` those since `first_at`;
    2. `timer.error` events per record are at most FAILURE_KINDS_MAX x (1 + restarts)
       + floor(log2 failed_runs) + 1 + floor(hours from first to last failed run) + restarts;
    3. the first failed run of every record is told, with its type;
    4. no failed run is more than FAILURE_HEARTBEAT_S after the record's last event;
    5. `keepalive`: the store's record equals the daemon's after every run, restarts
       included; `mirror_hot`: it disagrees on failing or not only within
       FAILURE_CHANGE_S of the last change it wrote, and its `timer.run` events are at
       most its `timer.error` events plus one a minute.
    """
    history, restarts_at = [], []
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        stack = contextlib.ExitStack()
        timer, store, clock, _ = stack.enter_context(rig(root))
        try:
            for fault, gap, repeat, restart in steps:
                for _ in range(repeat):
                    run(timer, name, None if fault is None else FAULTS[fault]("x"))
                    history.append((clock(), fault))
                    now = clock()
                    model = expected_record(history, now)
                    record = timer.status()[name]["failure"]
                    assert_matches(record, model)
                    durable = stored(store)[name]["failure"] if name in stored(store) else None
                    if name == "keepalive":
                        assert durable == record
                    elif failing(durable) != failing(record):
                        changes = change_writes(store, name)
                        assert changes and (now - changes[-1]).total_seconds() < FAILURE_CHANGE_S
                    clock.advance(gap)
                if restart and name == "keepalive":
                    stack.close()
                    stack = contextlib.ExitStack()
                    timer, store, clock2, _ = stack.enter_context(rig(root))
                    clock2.at, clock = clock(), clock2
                    restarts_at.append(clock())
            check_events(store, name, history, restarts_at)
        finally:
            stack.close()


def change_writes(store, name):
    """When `name`'s `timer.run` events changed between failing and not."""
    times, before = [], False
    for data in events(store, "timer.run", name):
        now = failing(timers_module.failure_record(data.get("failure")))
        if now != before:
            times.append(timers_module.instant(data["last_run"]))
        before = now
    return times


def check_events(store, name, history, restarts_at):
    told = events(store, "timer.error", name)
    records = {}
    for at, fault in history:
        if fault is not None:
            model = expected_record(history[:history.index((at, fault)) + 1], at)
            records.setdefault(model["first_at"], []).append((at, fault))
    assert set(records) == {event["first_at"] for event in told}
    for first_at, runs in records.items():
        mine = [event for event in told if event["first_at"] == first_at]
        restarts = sum(1 for at in restarts_at if runs[0][0] < at <= runs[-1][0])
        hours = (runs[-1][0] - runs[0][0]).total_seconds() // FAILURE_HEARTBEAT_S
        bound = FAILURE_KINDS_MAX * (1 + restarts) + int(math.log2(len(runs))) + 1 + hours + restarts
        assert len(mine) <= bound, (len(mine), bound, len(runs))
        assert mine[0]["failed_runs"] == 1 and mine[0]["error_type"] == FAULTS[runs[0][1]].__name__
        times = [timers_module.instant(event["last_at"]) for event in mine]
        for at, _ in runs:
            assert any(timedelta(0) <= at - told_at < timedelta(seconds=FAILURE_HEARTBEAT_S) for told_at in times)
    if name == "mirror_hot":
        span = (history[-1][0] - history[0][0]).total_seconds()
        assert len(events(store, "timer.run", name)) <= len(told) + span // FAILURE_CHANGE_S + 1


@settings(max_examples=200, deadline=None)
@given(outcomes=st.lists(st.one_of(st.none(), st.integers(0, 2)), min_size=1, max_size=60),
       gaps=st.lists(st.integers(1, 2 * FAILURE_VISIBLE_S), min_size=60, max_size=60))
def test_advance_is_the_reference_model(outcomes, gaps):
    """C-18.5: `timers.advance`, the pure step, agrees with the reference model, and a
    record is never shown with a count below one or a `since` before `first_at`."""
    history, record, at = [], None, START
    for outcome, gap in zip(outcomes, gaps):
        raised = None if outcome is None else {"error_type": FAULTS[outcome].__name__, "message": None, "raised_at": None}
        record = timers_module.advance(record, iso(at), raised)
        history.append((at, outcome))
        shown = record if timers_module.failure_shown(record, at) else None
        assert_matches(shown, expected_record(history, at))
        if shown:
            assert 1 <= shown["runs"] <= shown["failed_runs"] and shown["first_at"] <= shown["since"] <= shown["last_at"]
        at += timedelta(seconds=gap)


#: A value a record's field might hold in a store something else wrote.
FIELD = st.one_of(st.none(), st.booleans(), st.integers(-2, 3), st.text(max_size=4), st.just(iso(START)))


@given(st.one_of(st.none(), st.integers(), st.text(max_size=4),
                 st.fixed_dictionaries({key: FIELD for key in ("first_at", "since", "last_at", "runs", "failed_runs",
                                                               "error_type", "message", "raised_at", "recovered_at")})))
def test_a_malformed_record_is_never_read_as_one(value):
    """C-18.5: a record read back from the store has the shape its readers rely on, or
    is None; neither `status` nor `doctor` can be made to raise by one."""
    record = timers_module.failure_record(value)
    if record is not None:
        assert all(isinstance(record[key], int) and not isinstance(record[key], bool) and record[key] >= 1
                   for key in ("runs", "failed_runs"))
        assert isinstance(record["error_type"], str)
        timers_module.failure_shown(record, START)
    status = {"t": {"last_run": iso(START), "last_error_type": None, "failure": record}}
    render.timer_notes(status)
    render.timer_notes({"t": {"failure": value, "last_error_type": value}})
