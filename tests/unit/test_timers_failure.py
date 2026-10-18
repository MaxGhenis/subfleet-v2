"""C-18.5: a timer run that an exception ends is seen by an operator, and its events are bounded.

Until 2026-10-10 `Timers._run` kept only the exception's type, in `last_error_type`
(replaced by the next run) and in a `timer.error` event per failed run that no command
read; `status` and `doctor` said nothing, and `mirror_hot` could write two events every
2 s for as long as it failed. Every test runs on a store under a temporary directory;
none reads or writes `~/.subfleet`. The daemon's monotonic clock, which paces events,
follows each test's clock (`Clock.mono`).
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
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, doctor, render
from subfleet import timers as timers_module
from subfleet.offline import Offline
from subfleet.store import Store
from subfleet.timers import (FAILURE_CHANGE_S, FAILURE_HEARTBEAT_S, FAILURE_KINDS_MAX, FAILURE_MESSAGE_CHARS,
                             FAILURE_VISIBLE_S, RECORDED_QUERY, RECORDS_FILE, Timers, iso, load_records, recorded)

#: Today at 12:00 UTC: readers judge the one-day window against the real clock, so a
#: fixed date would age out of it.
START = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
POLICY = json.loads(Path("subfleet/default_policy.json").read_text())
#: MOCK credential-shaped text, built here so no literal key sits in the file.
MOCK_KEY = "api_" + "key=" + "Mock1234Value5678Abcd"
MOCK_VALUE = MOCK_KEY.split("=", 1)[1]
#: Distinct exception types: each is a kind of failure (type and place) of its own.
FAULTS = [type(f"Fault{index}", (Exception,), {}) for index in range(FAILURE_KINDS_MAX + 2)]


class Clock:
    """The wall clock (`now`) and the monotonic clock (`mono`). `advance` moves both;
    `set_wall` moves only the wall clock, as a clock correction does, and the
    monotonic clock a second on."""

    def __init__(self, at=START, mono=1000.0):
        self.at, self.mono = at, mono

    def __call__(self):
        return self.at

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)
        self.mono += seconds

    def set_wall(self, at):
        self.at, self.mono = at, self.mono + 1


@contextlib.contextmanager
def rig(root=None, clock=None):
    """A `Timers` over a store under a temporary directory, on a clock the test moves.
    Given `root`, it is a restart of the daemon whose state is there."""
    with contextlib.ExitStack() as stack:
        root = Path(root or stack.enter_context(tempfile.TemporaryDirectory(prefix="sf-timer-")))
        clock = clock or Clock()
        stack.enter_context(mock.patch.object(timers_module.time, "monotonic", clock.monotonic))
        store = stack.enter_context(Store(root / "state.sqlite3"))
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


def stored(store, root=None, now=None):
    """What `doctor` reads: the store's events, and with `root` the exact records."""
    return recorded(store.query(RECORDED_QUERY), now, records=load_records(root) if root else None)


def scheduled(timer, name, outcome):
    """`run`, after claiming the timer as `tick` does."""
    with timer._lock:
        timer._running.add(name)
    run(timer, name, outcome)


#: A JWT whose header, `{ "alg":"RS256","typ":"JWT"}`, does not encode to `eyJ`, which the
#: handoff list (C-23.14) keys on. MOCK: its payload and signature are made up.
MOCK_JWT = ("eyAiYWxnIjoiUlMyNTYiLCJ0eXAiOiJKV1QifQ" + "." + "eyJzdWIiOiJNT0NLLTEyMzQ1Njc4OTAifQ"
            + "." + "MoCkSiGnAtUrE0123456789abcdefGHIJ")


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


def test_a_token_the_handoff_list_misses_never_reaches_a_reader(monkeypatch):
    """C-18.5, review of #171 (P1): a JWT whose header is not `eyJ` passed the handoff
    scrub, and the message carried it to status, events, `timers.json` and doctor."""
    with rig() as (timer, store, _, root):
        run(timer, "keepalive", RuntimeError("provider rejected " + MOCK_JWT))
        assert timer.status()["keepalive"]["failure"]["message"] == "provider rejected [REDACTED]"
        monkeypatch.setenv("SUBFLEET_HOME", str(root))
        seen = (json.dumps(store.query("SELECT data_json FROM events")) + json.dumps(timer.status())
                + (root / RECORDS_FILE).read_text() + json.dumps(doctor.check_timers(root))
                + json.dumps(Offline(root).status()["timers"]) + status_text({"timers": timer.status()}))
        for part in MOCK_JWT.split("."):
            assert part not in seen


def test_redaction_keeps_what_a_defect_is_found_by():
    """C-18.5: paths, job ids, module names and plain words stay; tokens and hashes go."""
    text = ("open /Users/someone/subfleet-v2-wt/timers/x.json for 20261010-173947-review-171-r1 in "
            "subfleet.sessions.handoff: sha 9749b6ff4f20c4f6002f1a977f88cbbaa708eeae, key Ab12Cd34Ef56Gh78Ij90")
    assert timers_module.redact_opaque(text) == (
        "open /Users/someone/subfleet-v2-wt/timers/x.json for 20261010-173947-review-171-r1 in "
        "subfleet.sessions.handoff: sha [REDACTED], key [REDACTED]")


B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _mixed(text):
    return any(ch.isupper() for ch in text) and any(ch.islower() for ch in text) and any(ch.isdigit() for ch in text)


def mixed_run(min_size, max_size):
    """Token characters holding an upper-case letter, a lower-case letter and a digit,
    built so: a filter rejected so many draws that Hypothesis's health check failed
    under load (review of #171, round 2)."""
    return st.builds(lambda head, rest, order: "".join(sorted(head + rest, key=lambda _: next(order))),
                     st.tuples(st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), st.sampled_from("abcdefghijklmnopqrstuvwxyz"),
                               st.sampled_from("0123456789")).map("".join),
                     st.text(B64URL, min_size=min_size - 3, max_size=max_size - 3),
                     st.randoms(use_true_random=False).map(lambda rng: iter(lambda: rng.random(), None)))


TOKEN = st.one_of(
    mixed_run(20, 90),
    st.text("0123456789abcdef", min_size=32, max_size=64),
    st.tuples(*[mixed_run(10, 40)] * 3).map(".".join))


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(token=TOKEN, before=st.sampled_from(["", "x ", "token: ", "auth ", "'", "(", "/v1/keys/", "=", "\n"]),
       after=st.sampled_from(["", " y", "'", ")", ".", ",", "\n"]))
def test_no_token_shaped_value_survives_the_message(token, before, after):
    """C-18.5: whatever surrounds it, no token-shaped run of the message is published."""
    message = timers_module.failure_message(RuntimeError(f"failed {before}{token}{after} here"))
    for part in token.split("."):
        assert part not in (message or "")


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


@pytest.mark.parametrize("script,expected", [
    # review of #171 (P2): three failed passes, a restart, a fourth.
    (["fail", "fail", "fail", "restart", "fail"], {"runs": 4, "failed_runs": 4, "since": 0}),
    # a failed pass, a clean one, a restart, a failed one: a new streak.
    (["fail", "clean", "restart", "fail"], {"runs": 1, "failed_runs": 2, "since": 6}),
    # a failed pass, a clean one, a restart: still cleared, not failing again.
    (["fail", "clean", "restart", "clean"], {"runs": 1, "failed_runs": 1, "recovered_at": 2}),
])
def test_a_hot_timer_restarts_from_its_exact_record(script, expected):
    """C-18.5: `mirror_hot`'s events are bounded and lag, so a restart reads its record
    from `timers.json`, which is rewritten whenever a record changes."""
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        stack = contextlib.ExitStack()
        timer, _, clock, _ = stack.enter_context(rig(root))
        try:
            for step in script:
                if step == "restart":
                    stack.close()
                    stack = contextlib.ExitStack()
                    timer, _, clock, _ = stack.enter_context(rig(root, Clock(clock.at, clock.mono)))
                    clock.advance(2)                  # the reviewer's timeline: a restart takes 2 s
                    continue
                run(timer, "mirror_hot", from_instant if step == "fail" else None)
                clock.advance(2)
            failure = timer.status()["mirror_hot"]["failure"]
            for key, value in expected.items():
                assert failure[key] == (iso(START + timedelta(seconds=value)) if key in ("since", "recovered_at")
                                        else value), (key, failure)
        finally:
            stack.close()


def test_the_records_file_is_written_only_when_a_record_changes(monkeypatch):
    """C-18.5: clean runs of a clear timer rewrite nothing; each failed run does."""
    from subfleet import guardian
    writes = []
    real = guardian.atomic_publish
    monkeypatch.setattr(guardian, "atomic_publish", lambda path, data: (writes.append(path), real(path, data)))
    with rig() as (timer, _, clock, root):
        for _ in range(50):
            run(timer, "mirror_hot", None)
            clock.advance(2)
        assert len(writes) == 1                       # the first: this process had written none
        for _ in range(3):
            run(timer, "mirror_hot", from_instant)
            clock.advance(2)
        entry = load_records(root)["mirror_hot"]
        assert len(writes) == 4 and entry["failure"]["runs"] == 3 and entry["rev"] == 3


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
    a change between failing and not is written to the store at most once a minute."""
    with rig() as (timer, store, clock, root):
        run(timer, "mirror_hot", from_instant)
        assert stored(store, now=clock())["mirror_hot"]["failure"]["recovered_at"] is None
        for _ in range(29):                      # 2 s to 58 s: clean, not yet written
            clock.advance(2)
            run(timer, "mirror_hot", None)
            assert stored(store, now=clock())["mirror_hot"]["failure"]["recovered_at"] is None
        clock.advance(2)                         # 60 s: written
        run(timer, "mirror_hot", None)
        assert stored(store, now=clock())["mirror_hot"]["failure"]["recovered_at"] == iso(START + timedelta(seconds=2))
        assert len(events(store, "timer.run", "mirror_hot")) == 2
        # What doctor reads is exact from the first clean pass: the records file.
        assert stored(store, root, clock())["mirror_hot"]["failure"]["recovered_at"] == iso(START + timedelta(seconds=2))


@pytest.mark.parametrize("refused", ["timer.error", "timer.run"])
def test_a_record_write_that_raises_never_leaves_the_timer_running(monkeypatch, refused):
    """C-18.5: before, a `timer.error` write that raised skipped the rest of `_run`,
    and the timer stayed "already running" until a restart: no tick started it."""
    with rig() as (timer, store, clock, _):
        real = store.add_event

        def refusing(kind, **fields):
            if kind == refused:
                raise OSError("disk full")
            return real(kind, **fields)
        monkeypatch.setattr(store, "add_event", refusing)
        with pytest.raises(OSError):
            scheduled(timer, "keepalive", from_instant)
        assert "keepalive" not in timer._running
        timer.start()
        timer._due["keepalive"] = 0
        submitted = []
        monkeypatch.setattr(timer._cycles, "submit", lambda fn, *args: submitted.append(args))
        timer.tick()
        assert submitted == [("keepalive",)]           # a tick starts it again


def test_a_refused_first_event_is_written_by_a_later_run(monkeypatch):
    """C-18.5, review of #171 (P2): the first failure's event, refused by the store,
    stays to be written, with the latest after it; it is not replaced by the next."""
    with rig() as (timer, store, clock, _):
        real = store.add_event
        refuse = {"on": True}

        def refusing(kind, **fields):
            if refuse["on"] and kind == "timer.error":
                raise OSError("disk full")
            return real(kind, **fields)
        monkeypatch.setattr(store, "add_event", refusing)
        with pytest.raises(OSError):
            run(timer, "mirror_hot", ValueError("first failure"))
        clock.advance(2)
        with pytest.raises(OSError):
            run(timer, "mirror_hot", KeyError("second failure"))
        clock.advance(2)
        with pytest.raises(OSError):
            run(timer, "mirror_hot", KeyError("third failure"))
        refuse["on"] = False
        clock.advance(2)
        run(timer, "mirror_hot", None)                 # a clean run writes what was left
        told = events(store, "timer.error", "mirror_hot")
        assert [(event["error_type"], event["failed_runs"], event["message"]) for event in told] == [
            ("ValueError", 1, "first failure"), ("KeyError", 3, "'third failure'")]


def test_a_refused_snapshot_leaves_doctor_exact(monkeypatch):
    """C-18.5, review of #171 (P2): a `timer.run` the store refused left doctor with
    nothing for a hot timer's recovery; `timers.json` holds it."""
    with rig() as (timer, store, clock, root):
        real = store.add_event
        refuse = {"on": True}
        monkeypatch.setattr(store, "add_event", lambda kind, **fields: (
            (_ for _ in ()).throw(OSError("disk full")) if refuse["on"] and kind == "timer.run"
            else real(kind, **fields)))
        with pytest.raises(OSError):
            run(timer, "mirror_hot", from_instant)
        refuse["on"] = False
        for _ in range(35):
            clock.advance(2)
            run(timer, "mirror_hot", None)
        item = doctor.check_timers(root)
        assert item["status"] == doctor.WARN and f"mirror_hot ran clean at {iso(START + timedelta(seconds=2))}" in item["detail"]


def test_a_clock_set_back_adds_no_event():
    """C-18.5, review of #171 (P2): the hourly event is paced by the monotonic clock.
    Before, a wall clock swinging an hour each run wrote an event on each of 101 runs."""
    with rig() as (timer, store, clock, _):
        for index in range(101):
            clock.set_wall(START + timedelta(hours=index % 2))
            run(timer, "mirror_hot", from_instant)
        told = events(store, "timer.error", "mirror_hot")
        assert len(told) <= FAILURE_KINDS_MAX + int(math.log2(101)) + 1
        assert [event["failed_runs"] for event in told] == [1, 2, 4, 8, 16, 32, 64]


@pytest.mark.parametrize("companion", ["reset_credits", "alerts"])
def test_a_companion_that_raises_inside_the_probe_cycle_has_its_own_record(monkeypatch, companion):
    """C-18.5, review of #171 (P2): `reset_credits` and `alerts` run inside the probe
    cycle; when one raised, only probe's record said so."""
    with rig() as (timer, _, clock, root):
        def broken(*args, **kwargs):
            timers_module.instant("not a date")
        if companion == "reset_credits":
            monkeypatch.setattr(timer, "evaluate_resets", broken)
        else:
            monkeypatch.setattr(timer.alerts, "evaluate", broken)
        for _ in range(2):
            timer._run("probe")
            clock.advance(60)
        records = timer.status()
        assert records[companion]["failure"]["runs"] == 2
        assert records[companion]["failure"]["raised_at"].endswith(" in instant")
        assert records["probe"]["failure"]["runs"] == 2       # the cycle ended with it, as before
        assert f"{companion} failing since" in doctor.check_timers(root)["detail"]


@pytest.mark.parametrize("stamp", ["2026-10-10T12:01:00", "2026-10-10 12:01", "10/10/2026", "2026-10-10T12:01:00+01:00"])
def test_a_record_with_a_bad_time_never_crashes_a_reader(monkeypatch, stamp):
    """C-18.5, review of #171 (P2): a record whose `recovered_at` has no time zone was
    accepted, and `Offline.status` and `doctor` raised TypeError on it."""
    record = {"first_at": iso(START), "since": iso(START), "last_at": iso(START), "runs": 1, "failed_runs": 1,
              "error_type": "OSError", "message": "m", "raised_at": "timers.py:1 in x", "recovered_at": stamp}
    aware = stamp.endswith("+01:00")
    assert (timers_module.failure_record(record) is not None) == aware
    with rig() as (timer, store, _, root):
        store.add_event("timer.run", data={"timer": "mirror", "last_run": iso(START), "next_due": None,
                                           "last_error_type": None, "failure": record})
        (root / RECORDS_FILE).write_text(json.dumps({"version": 1, "timers": {"mirror_hot": {"failure": record}}}))
        monkeypatch.setenv("SUBFLEET_HOME", str(root))
        assert doctor.check_timers(root)["status"] in (doctor.PASS, doctor.WARN)
        Offline(root).status()
        Timers(store, root, POLICY).stop()


# --- round 2 of the review of #171 -----------------------------------------------------------

def test_a_message_that_is_not_utf8_reaches_every_reader(monkeypatch):
    """C-18.5, review round 2 (P1): a lone surrogate (retention decodes git paths with
    `surrogateescape`) made every write for the timer raise, blocked its events for
    good, and broke `daemon.status`, offline `status` and the whole `doctor` table."""
    with rig() as (timer, store, clock, root):
        for _ in range(3):
            run(timer, "retention", OSError("cannot remove /jobs/b\udcffad/x"))
            clock.advance(3600)
        failure = timer.status()["retention"]["failure"]
        assert failure["runs"] == 3 and "\\udcff" in failure["message"]
        json.dumps(timer.status(), ensure_ascii=False).encode("utf-8")      # a reply can be sent
        told = events(store, "timer.error", "retention")             # 1, 2, and 3 an hour on: none blocked
        assert [event["failed_runs"] for event in told] == [1, 2, 3]
        monkeypatch.setenv("SUBFLEET_HOME", str(root))
        monkeypatch.setattr(doctor, "_run", lambda argv, timeout=20.0, **kwargs: (0, "stub"))
        monkeypatch.setattr(doctor, "_run_full", lambda argv, timeout=20.0, **kwargs: (0, "stub"))
        rows = {item["check"]: item for item in doctor.checks(root)}
        assert rows["daemon timers"]["status"] == doctor.FAIL
        assert "  retention  failing since" in status_text(Offline(root).status())


def test_an_event_the_store_can_never_take_holds_back_no_later_one(monkeypatch):
    """C-18.5, review round 2 (P1): an event that cannot be encoded is dropped, not
    retried ahead of every later one; one the store refuses (OSError) waits."""
    with rig() as (timer, store, clock, _):
        real = store.add_event
        refusals = {"left": 1}

        def refusing(kind, **fields):
            if kind == "timer.error" and refusals["left"]:
                refusals["left"] -= 1
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
            return real(kind, **fields)
        monkeypatch.setattr(store, "add_event", refusing)
        run(timer, "mirror", KeyError("first"))       # its event can never be written: dropped
        clock.advance(60)
        run(timer, "mirror", KeyError("second"))      # written: the first this process wrote
        assert [event["failed_runs"] for event in events(store, "timer.error", "mirror")] == [2]
        assert not timer._unsent["mirror"]


def _failing_publish(monkeypatch, error=OSError(28, "No space left on device")):
    from subfleet import guardian
    def publish(path, data):
        raise error
    monkeypatch.setattr(guardian, "atomic_publish", publish)


def test_a_failed_records_file_write_is_shown_and_the_events_win(monkeypatch):
    """C-18.5, review round 2 (P2): `timers.json` won over the store even when its
    writes failed, silently. Now the newer revision wins, the failure is in
    `daemon.status` (`timers_file`), `status` and `doctor`, and a restart keeps the
    record from the events."""
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        with rig(root) as (timer, store, clock, _):
            run(timer, "keepalive", None)              # the file is written once
            _failing_publish(monkeypatch)
            for _ in range(3):
                clock.advance(60)
                run(timer, "keepalive", from_instant)
            file_status = timer.records_status()
            assert file_status["tries"] == 3 and file_status["error_type"] == "OSError"
            assert "No space left on device" in file_status["message"]
            text = status_text({"timers": timer.status(), "timers_file": file_status})
            assert "  timers.json not written since" in text and "3 tries" in text
            item = doctor.check_timers(root)
            assert item["status"] == doctor.FAIL and "keepalive failing since" in item["detail"]
            assert "timers.json is behind the store's events for keepalive" in item["detail"]
            at = clock()
        monkeypatch.undo()
        with rig(root, Clock(at, 5000.0)) as (timer, _, _, _):
            assert timer.status()["keepalive"]["failure"]["runs"] == 3      # from the events


def test_a_records_file_that_cannot_be_written_never_keeps_a_recovered_timer_failing(monkeypatch):
    """C-18.5, review round 2 (P2): with the state directory read-only, doctor kept
    reporting FAIL for a timer that had recovered."""
    with rig() as (timer, _, clock, root):
        _failing_publish(monkeypatch, PermissionError(13, "Permission denied"))
        run(timer, "mirror", from_instant)
        clock.advance(60)
        run(timer, "mirror", None)
        item = doctor.check_timers(root)
        assert item["status"] == doctor.WARN and "mirror ran clean at" in item["detail"]


def test_the_file_is_written_before_the_events(monkeypatch):
    """C-18.5, review round 2 (P2): a daemon killed between a run's events and its
    file write came back with the older record. The file now goes first; killed
    between them, the file is the newer and wins."""
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        with rig(root) as (timer, store, clock, _):
            run(timer, "keepalive", from_instant)
            clock.advance(60)

            def killed(kind, **fields):
                raise SystemExit("killed after the file write")
            monkeypatch.setattr(store, "add_event", killed)
            with pytest.raises(SystemExit):
                run(timer, "keepalive", None)
            at = clock()
        monkeypatch.undo()
        with rig(root, Clock(at, 5000.0)) as (timer, _, _, _):
            assert timer.status()["keepalive"]["failure"]["recovered_at"] == iso(START + timedelta(seconds=60))
        assert doctor.check_timers(Path(root))["status"] == doctor.WARN


def test_doctor_reads_a_hot_timer_from_one_revision():
    """C-18.5, review round 2 (P3): offline doctor said `mirror_hot` ran clean and,
    from its lagging event, that its last run reported an error."""
    with rig() as (timer, _, clock, root):
        run(timer, "mirror_hot", from_instant)
        clock.advance(2)
        run(timer, "mirror_hot", None)
        detail = doctor.check_timers(root)["detail"]
        assert "mirror_hot ran clean at" in detail and "reported" not in detail


def test_doctor_live_shows_a_records_file_behind(daemon, root):
    """C-18.5: `doctor --live` reads `timers_file` from `daemon.status`."""
    clean = {"probe": {"last_run": iso(START), "next_due": None, "last_error_type": None, "failure": None}}
    file_status = {"since": iso(START), "last_at": iso(START), "tries": 2, "error_type": "OSError", "message": "full"}
    daemon({"daemon.status": lambda request: {"timers": clean, "timers_file": file_status}})
    item = doctor.check_timers_live(root)
    assert item["status"] == doctor.WARN and "timers.json not written since" in item["detail"]


def test_a_hot_timer_paces_its_changes_by_the_monotonic_clock():
    """C-18.5, review round 2 (P3): a mutant pacing `mirror_hot`'s change writes by the
    wall clock survived; with the wall clock swinging an hour each pass, it wrote a
    `timer.run` on every change."""
    with rig() as (timer, store, clock, _):
        for index in range(60):
            clock.set_wall(START + timedelta(hours=index % 2))
            clock.mono += 1                            # 2 s a pass by the monotonic clock
            run(timer, "mirror_hot", from_instant if index % 2 else None)
        told = len(events(store, "timer.error", "mirror_hot"))
        assert len(events(store, "timer.run", "mirror_hot")) <= told + 120 // FAILURE_CHANGE_S + 1


def test_stop_writes_what_a_refused_write_left(monkeypatch):
    """C-18.5, review round 2 (P3): a mutant without `stop()`'s last send survived."""
    with rig() as (timer, store, clock, _):
        real = store.add_event
        refuse = {"on": True}

        def refusing(kind, **fields):
            if refuse["on"] and kind == "timer.error":
                raise OSError("disk full")
            return real(kind, **fields)
        monkeypatch.setattr(store, "add_event", refusing)
        with pytest.raises(OSError):
            run(timer, "keepalive", from_instant)
        refuse["on"] = False
        timer.stop()
        assert [event["failed_runs"] for event in events(store, "timer.error", "keepalive")] == [1]


def test_one_timers_events_are_written_by_one_thread_at_a_time(monkeypatch):
    """C-18.5, review round 2 (P3): `stop()` racing a late mark could write one event twice."""
    import threading
    with rig() as (timer, store, clock, _):
        real = store.add_event
        entered, release = threading.Event(), threading.Event()
        refuse = {"on": True}

        def slow(kind, **fields):
            if kind == "timer.error" and refuse["on"]:
                raise OSError("disk full")
            if kind == "timer.error" and not entered.is_set():
                entered.set()
                release.wait(5)
            return real(kind, **fields)
        monkeypatch.setattr(store, "add_event", slow)
        with pytest.raises(OSError):
            run(timer, "retention", from_instant)
        refuse["on"] = False
        first = threading.Thread(target=timer._send_errors, args=("retention",))
        first.start()
        assert entered.wait(5)
        second = threading.Thread(target=timer._send_errors, args=("retention",))
        second.start()
        release.set()
        first.join(5), second.join(5)
        assert len(events(store, "timer.error", "retention")) == 1


def test_a_companions_own_error_ends_the_cycle_when_its_record_cannot_be_written(monkeypatch):
    """C-18.5, review round 2 (P3): when the store refused the companion's record,
    probe's record showed the store's OSError, not the companion's error."""
    with rig() as (timer, store, _, _):
        real = store.add_event
        monkeypatch.setattr(timer, "evaluate_resets", lambda *args, **kwargs: timers_module.instant("not a date"))
        monkeypatch.setattr(store, "add_event", lambda kind, **fields: (
            (_ for _ in ()).throw(OSError("disk full")) if kind == "timer.error" else real(kind, **fields)))
        with pytest.raises(OSError):
            timer._run("probe")
        assert timer.status()["probe"]["failure"]["error_type"] == "ValueError"
        assert timer.status()["reset_credits"]["failure"]["error_type"] == "ValueError"


# --- round 3 of the review of #171 -----------------------------------------------------------

def test_a_hot_recovery_the_file_could_not_take_is_written_with_its_run(monkeypatch):
    """C-18.5, review round 3 (P2): `mirror_hot` recovered within the minute its change
    pacing holds back, and the file write failed; the recovery was in neither copy, so
    a restart showed the next failure as the second in a row. A revision the file could
    not take now goes into the run's own `timer.run`."""
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        with rig(root) as (timer, store, clock, _):
            run(timer, "mirror_hot", from_instant)     # rev 1: file and event
            clock.advance(2)
            _failing_publish(monkeypatch, PermissionError(13, "Permission denied"))
            run(timer, "mirror_hot", None)              # rev 2: the file refuses it
            assert stored(store, Path(root), clock())["mirror_hot"]["failure"]["recovered_at"] == iso(
                START + timedelta(seconds=2))
            assert doctor.check_timers(Path(root))["status"] == doctor.WARN
            clock.advance(2)
            at, mono = clock(), clock.mono
        monkeypatch.undo()
        with rig(root, Clock(at, mono)) as (timer, _, clock, _):
            run(timer, "mirror_hot", from_instant)
            failure = timer.status()["mirror_hot"]["failure"]
            assert (failure["runs"], failure["since"], failure["failed_runs"]) == (1, iso(at), 2)


def test_while_the_file_fails_each_changed_revision_is_in_an_event(monkeypatch):
    """C-18.5: with the file refused, every run that changes a record writes its
    `timer.run`, and a clean run that changes nothing writes none."""
    with rig() as (timer, store, clock, _):
        _failing_publish(monkeypatch)
        for _ in range(5):
            run(timer, "mirror_hot", from_instant)
            clock.advance(2)
        for _ in range(5):
            run(timer, "mirror_hot", None)
            clock.advance(2)
        revs = [event["rev"] for event in events(store, "timer.run", "mirror_hot")]
        assert revs == [1, 2, 3, 4, 5, 6]


def test_stop_writes_a_revision_still_in_neither_copy(monkeypatch):
    """C-18.5: a revision the file refused and whose event the store refused is written
    by `stop()` when the store takes it."""
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        with rig(root) as (timer, store, clock, _):
            run(timer, "mirror_hot", from_instant)
            clock.advance(2)
            _failing_publish(monkeypatch)
            real = store.add_event
            refuse = {"on": True}
            monkeypatch.setattr(store, "add_event", lambda kind, **fields: (
                (_ for _ in ()).throw(OSError("busy")) if refuse["on"] else real(kind, **fields)))
            with pytest.raises(OSError):
                run(timer, "mirror_hot", None)
            refuse["on"] = False
            timer.stop()
            assert events(store, "timer.run", "mirror_hot")[-1]["failure"]["recovered_at"] == iso(
                START + timedelta(seconds=2))


def test_a_file_never_written_is_shown_behind_offline(monkeypatch):
    """C-18.5, review round 3 (P3): with the first write of `timers.json` refused there
    was no file, and offline `doctor` and `status` said nothing of it."""
    with rig() as (timer, _, clock, root):
        _failing_publish(monkeypatch)
        run(timer, "keepalive", from_instant)
        assert not (root / RECORDS_FILE).exists()
        item = doctor.check_timers(root)
        assert item["status"] == doctor.FAIL and "timers.json is behind the store's events for keepalive" in item["detail"]
        monkeypatch.setenv("SUBFLEET_HOME", str(root))
        assert "timers.json is behind" in status_text(Offline(root).status())


@pytest.mark.parametrize("damage", ["delete", "truncate"])
def test_a_damaged_file_is_written_again_by_the_next_run(damage):
    """C-18.5, review round 3 (P3): a file deleted or truncated while no record changed
    stayed so, and offline readers fell back to an older revision."""
    with rig() as (timer, store, clock, root):
        run(timer, "mirror_hot", from_instant)
        clock.advance(2)
        run(timer, "mirror_hot", None)
        path = root / RECORDS_FILE
        path.unlink() if damage == "delete" else path.write_text("{")
        clock.advance(2)
        run(timer, "mirror_hot", None)
        assert load_records(root)["mirror_hot"]["rev"] == 2
        assert stored(store, root, clock())["mirror_hot"]["failure"]["recovered_at"] == iso(START + timedelta(seconds=2))


def test_a_record_read_back_is_made_utf8_too(monkeypatch):
    """C-18.5, review round 3 (P3): a record stored before messages were made UTF-8 was
    read back as it was, and broke `daemon.status` and the timer's next write."""
    record = {"first_at": iso(START), "since": iso(START), "last_at": iso(START), "runs": 1, "failed_runs": 1,
              "error_type": "OSError", "message": "bad \udcff", "raised_at": "retention.py:1 in f\udcff",
              "recovered_at": None}
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        Path(root, RECORDS_FILE).write_text(json.dumps({"version": 1, "timers": {"retention": {"failure": record}}}))
        with rig(root) as (timer, store, clock, _):
            message = timer.status()["retention"]["failure"]["message"]
            assert message == "bad \\udcff"
            json.dumps(timer.status(), ensure_ascii=False).encode("utf-8")
            clock.advance(3600)
            timer.mark("retention")
            assert events(store, "timer.run", "retention")


def test_a_source_path_that_is_not_utf8_is_escaped():
    """C-18.5, review round 3 (P3): the frame sanitizer had no test of its own. A path
    decoded with `surrogateescape` can name a frame's file; Python refuses a surrogate
    in a type name, so the type's escape is only a defence."""
    code = compile("def broken():\n    raise KeyError('x')\n", "/somewhere/b\udcffad.py", "exec")
    space = {}
    exec(code, space)
    exc = _raised(space["broken"])
    place, trace = timers_module.raised_from(exc)
    assert place == "b\\udcffad.py:2 in broken" and trace == []
    with rig() as (timer, store, _, _):
        run(timer, "mirror", space["broken"])
        failure = timer.status()["mirror"]["failure"]
        json.dumps(failure, ensure_ascii=False).encode("utf-8")
        assert failure["raised_at"].startswith("timers.py:")              # the innermost package frame
        json.dumps(events(store, "timer.error", "mirror"), ensure_ascii=False).encode("utf-8")


def _raised(fn):
    try:
        fn()
    except Exception as exc:                            # noqa: BLE001
        return exc
    raise AssertionError("it did not raise")


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
    assert lines[1].startswith(f"  mirror_hot  failing since {iso(START)}: 2 runs in a row, the latest at "
                               f"{iso(START + timedelta(seconds=2))}: ValueError: Invalid isoformat string: 'not a date' (timers.py:")
    assert lines[2].startswith(f"  mirror      ran clean at {iso(START + timedelta(seconds=62))} after a failed run at "
                               f"{iso(START + timedelta(seconds=2))}: KeyError: 'sessionId' (timers.py:")
    assert lines[3] == f"  probe       its last run, at {iso(START + timedelta(seconds=62))}, reported URLError"


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
                assert f"keepalive  failed at {iso(START)}, its last run: ValueError" in out.getvalue()


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
        assert item["detail"].startswith(f"mirror failed at {iso(START)}, its last run: ValueError")
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
    monkeypatch.setattr(doctor, "_run", lambda argv, timeout=20.0, **kwargs: (0, "stub"))
    monkeypatch.setattr(doctor, "_run_full", lambda argv, timeout=20.0, **kwargs: (0, "stub"))
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

#: A step: the run's outcome (None: clean; i: raises FAULTS[i]); the seconds to the
#: next run (below zero: the wall clock is set back that far while the monotonic
#: clock moves a second); how many times it repeats; and whether the daemon restarts
#: after it.
STEP = st.tuples(st.one_of(st.none(), st.integers(0, len(FAULTS) - 1)),
                 st.one_of(st.integers(1, 120), st.integers(3000, 4000), st.integers(80000, 100000),
                           st.integers(-7200, -1)),
                 st.integers(1, 40), st.booleans())


def expected_record(history, now):
    """The failure record C-18.5 defines, from the whole history of `(time, fault, ...)`
    runs, written apart from `timers.advance`. None when there is none to show at `now`.

    A record ends at the first run, of either kind, that comes a day or more after its
    first clean run (by that run's own time, whatever the wall clock did since): a
    failure after that starts a new record, and setting the clock back revives none.
    """
    failed = [index for index, (_, fault, *_) in enumerate(history) if fault is not None]
    if not failed:
        return None

    def first_clean_after(index, upto):
        return next((k for k in range(index + 1, upto) if history[k][1] is None), None)

    def ended(recovered, upto):
        return any((history[k][0] - history[recovered][0]).total_seconds() >= FAILURE_VISIBLE_S
                   for k in range(recovered + 1, upto + 1))

    start = failed[0]
    for previous, index in zip(failed, failed[1:]):
        recovered = first_clean_after(previous, index)
        if recovered is not None and ended(recovered, index):
            start = index                         # the old record was gone: a new one
    last = failed[-1]
    recovered = first_clean_after(last, len(history))
    if recovered is not None and (ended(recovered, len(history) - 1)
                                  or (now - history[recovered][0]).total_seconds() >= FAILURE_VISIBLE_S):
        return None
    streak = last
    while streak > start and history[streak - 1][1] is not None:
        streak -= 1
    return {"first_at": iso(history[start][0]), "since": iso(history[streak][0]), "last_at": iso(history[last][0]),
            "runs": last - streak + 1, "failed_runs": sum(1 for index in failed if index >= start),
            "error_type": FAULTS[history[last][1]].__name__,
            "recovered_at": iso(history[recovered][0]) if recovered is not None else None}


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
    """C-18.5 invariants, for every sequence of clean and raising runs, with the wall
    clock set back at times and the daemon restarted:

    1. the record equals the reference model after every run: `runs` is the number of
       failed runs since the last clean run, `failed_runs` those since `first_at`;
    2. what `doctor` reads (the store and `timers.json`) equals the daemon's record
       after every run, restarts included;
    3. `timer.error` events per record are at most (FAILURE_KINDS_MAX + 1) x (1 +
       restarts) + floor(log2 failed_runs) + floor(monotonic hours from its first to
       its last failed run);
    4. the first failed run of every record is told, with its type;
    5. every failed run is less than FAILURE_HEARTBEAT_S of monotonic time after one
       of its record's events;
    6. `mirror_hot` writes at most one `timer.run` per `timer.error`, plus one a
       monotonic minute and one per restart.
    """
    history, restarts = [], []
    with tempfile.TemporaryDirectory(prefix="sf-timer-") as root:
        stack = contextlib.ExitStack()
        timer, store, clock, _ = stack.enter_context(rig(root))
        try:
            for fault, gap, repeat, restart in steps:
                for _ in range(repeat):
                    run(timer, name, None if fault is None else FAULTS[fault]("x"))
                    history.append((clock(), fault, clock.mono))
                    record = timer.status()[name]["failure"]
                    assert_matches(record, expected_record(history, clock()))
                    read = stored(store, root, clock())
                    assert (read[name]["failure"] if name in read else None) == record
                    if gap < 0:
                        clock.set_wall(clock() + timedelta(seconds=gap))
                    else:
                        clock.advance(gap)
                if restart:
                    stack.close()
                    stack = contextlib.ExitStack()
                    timer, store, clock, _ = stack.enter_context(rig(root, Clock(clock.at, clock.mono)))
                    restarts.append(clock.mono)
            check_events(store, name, history, restarts)
        finally:
            stack.close()


def check_events(store, name, history, restarts):
    told = events(store, "timer.error", name)
    records = {}
    for index, (at, fault, mono) in enumerate(history):
        if fault is not None:
            first_at = expected_record(history[:index + 1], at)["first_at"]
            records.setdefault(first_at, []).append((fault, mono))
    assert set(records) == {event["first_at"] for event in told}
    for first_at, runs in records.items():
        mine = [event for event in told if event["first_at"] == first_at]
        span = [mono for mono in restarts if runs[0][1] < mono <= runs[-1][1]]
        hours = (runs[-1][1] - runs[0][1]) // FAILURE_HEARTBEAT_S
        bound = (FAILURE_KINDS_MAX + 1) * (1 + len(span)) + int(math.log2(len(runs))) + hours
        assert len(mine) <= bound, (len(mine), bound, len(runs))
        assert mine[0]["failed_runs"] == 1 and mine[0]["error_type"] == FAULTS[runs[0][0]].__name__
        # An event's `failed_runs` names the failed run of its record that wrote it.
        written = [runs[event["failed_runs"] - 1][1] for event in mine]
        for _, mono in runs:
            assert any(0 <= mono - at < FAILURE_HEARTBEAT_S for at in written)
    if name == "mirror_hot":
        span = history[-1][2] - history[0][2]
        assert len(events(store, "timer.run", name)) <= len(told) + span // FAILURE_CHANGE_S + 1 + len(restarts)


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
FIELD = st.one_of(st.none(), st.booleans(), st.integers(-2, 3), st.text(max_size=4), st.just(iso(START)),
                  st.sampled_from(["2026-10-10T12:01:00", "2026-10-10T12:01:00+05:30", "2026-10-10 12:01", "\udcff"]))


@settings(deadline=None)
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
    if isinstance(value, dict):
        timers_module.failure_shown(value, START)       # never raises, whatever it holds
    render.timer_notes({"t": {"failure": value, "last_error_type": value}})
