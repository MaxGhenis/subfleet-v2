"""A pass that an exception ends is a finished pass: C-23.28.

A pass expects cancellation and OSError: it records either as its end and
returns. Any other exception used to leave the sidecar's record at `running`
with no finish and no error. The daemon's timer caught the exception and ran
the pass again on its interval; each run wrote a new start before it failed
the same way, so `Mirror.health()` read `running` after every pass and never
reached the thirty-minute `stalled` reading. On a fixture store on 2026-10-10,
46 passes a minute apart all read `running`, `sessions mirror --status` exited
0 and `doctor` passed, while nothing was copied
(`docs/reports/2026-10-10-mirror-pass-failure.md`).

The invariants these tests hold the mirror to, for a full pass's `pass` record
and a hot pass's `hot` record:

* finished: once `run_once` or `run_hot` has returned or raised, the record of
  a pass that took the lock is `ok`, `error` or `cancelled` and has a finish,
  whenever the sidecar could be written;
* one account of the end: a pass that returns left its own record in the
  sidecar; a pass that raises left `error` (`cancelled` for an interrupt) with
  the exception's type, and raises that same exception;
* OSError and cancellation still return, and no other exception that ends a
  pass does;
* `last_ok_at` moves only when a full pass ends `ok`; a hot pass changes
  nothing of the full pass's record;
* the lock is free afterwards, and a pass that never held it wrote nothing;
* health follows: `healthy` after an `ok` full pass, `stalled` after any
  other, and never `running` for a pass that is over;
* the timer's `timer.error` event and the sidecar name the same type.

Every test names the clause it proves (C-20.5). The desktop store, the
transcripts, the app's log and the state root live under `tmp_path`, through
SUBFLEET_SESSION_STORE, SUBFLEET_CLAUDE_DIR, SUBFLEET_DESKTOP_LOG and
SUBFLEET_HOME; nothing here reads or writes the operator's own.
"""

from __future__ import annotations

import builtins
import errno
import fcntl
import io
import json
import re
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from subfleet import doctor
from subfleet.sessions import mirror
from tests import sessions_fixtures as fx

SESSIONS = tuple(f"{index:08d}-0000-4000-8000-00000000beef" for index in range(3))
ONE, TWO, THREE = SESSIONS
FOLDERS = (("acct-a", "org-a"), ("acct-b", "org-b"), ("acct-c", "org-c"))
#: 2026-10-10T12:26:40Z in the app's milliseconds; the passes' clock starts three days on.
NEW = 1_791_635_200_000
DAY = 86_400_000
CLOCK = datetime.fromtimestamp((NEW + 3 * DAY) / 1000, timezone.utc)
FINISHED = ("ok", "error", "cancelled")
#: ` (mirror.py:2467 in _pass)`: where the record says the exception was raised.
WHERE = re.compile(r" \(mirror\.py:\d+ in \w+\)$")


class Odd(Exception):
    """An exception no code in the mirror names."""


class Halt(BaseException):
    """An interrupt that is neither Ctrl-C nor an exit."""


# --- the fixture store ---------------------------------------------------------------

class World:
    """A state root, a `~/.claude` and a three-login desktop store under `base`."""

    def __init__(self, base: Path, patch, *, hot_interval: float = 0):
        self.base, self.patch = base, patch
        self.home, self.store, self.root = base / "claude", base / "claude-code-sessions", base / "state"
        patch.setenv("SUBFLEET_CLAUDE_DIR", str(self.home))
        patch.setenv("SUBFLEET_SESSION_STORE", str(self.store))
        patch.setenv("SUBFLEET_DESKTOP_LOG", str(base / "logs" / "main.log"))
        patch.setenv("SUBFLEET_HOME", str(self.root))
        (self.home / "projects").mkdir(parents=True)
        for account, org in FOLDERS:
            (self.store / account / org).mkdir(parents=True)
        self.root.mkdir()
        self.clock = CLOCK
        self.offset = 0.0
        monotonic = mirror.time.monotonic
        patch.setattr(mirror.time, "monotonic", lambda: monotonic() + self.offset)
        self.policy = fx.policy(mirror_hot_interval_s=hot_interval)
        self.cancel = threading.Event()
        self.running = mirror.Mirror(self.root, self.policy, now=lambda: self.clock,
                                     cancel=self.cancel)

    def wait(self, seconds: float) -> None:
        """Both of the mirror's clocks move on: the pass's and the sampling's."""
        self.clock += timedelta(seconds=seconds)
        self.offset += seconds

    def options(self, **overrides) -> mirror.Options:
        return mirror.options_from(self.policy, **overrides)

    def full(self, **overrides) -> mirror.Pass:
        return self.running.run_once(self.options(**overrides))

    def hot(self, **overrides) -> mirror.Pass:
        return self.running.run_hot(self.options(**overrides))

    def path(self, index: int, session: str = ONE) -> Path:
        account, org = FOLDERS[index]
        return self.store / account / org / f"local_{session}.json"

    def put(self, index: int, session: str = ONE, **fields) -> Path:
        fx.transcript(self.home, session, fx.completed())
        account, org = FOLDERS[index]
        return fx.index_entry(self.store, account, org, session, last_activity=NEW,
                              settings={"ultracode": True}, **fields)

    def save(self, index: int, session: str = ONE, **fields) -> None:
        """The app's own write: beside the file, then rename (never in place)."""
        target = self.path(index, session)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps({**json.loads(target.read_text()), **fields}))
        temporary.replace(target)

    def holds(self, session: str) -> list[int]:
        return [index for index in range(len(FOLDERS)) if self.path(index, session).exists()]

    def sidecar(self) -> dict:
        try:
            return json.loads(self.running.sidecar_path.read_text(encoding="utf-8"))
        except OSError:
            return {}

    def health(self) -> dict:
        """As `doctor` and `--status` judge: a new Mirror, the sidecar and nothing else."""
        return mirror.Mirror(self.root, self.policy, now=lambda: self.clock).health()

    def lock_is_free(self) -> bool:
        with open(self.root / "sessions" / mirror.LOCK_NAME, "a") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return True


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def unspread(world: World) -> None:
    """ONE under one login only: a full pass must copy it into the other two."""
    world.put(0, ONE)


def inventoried(world: World) -> None:
    """ONE under every login and a clean full pass: the next pass may be a hot one."""
    for index in range(3):
        world.put(index, ONE)
    assert world.full().state == "ok"


def raising(error: BaseException):
    def raises(*_args, **_kwargs):
        raise error
    return raises


class Fault:
    """The `nth` call of one function raises `error`; every other call goes through."""

    def __init__(self, patch, target, name: str, nth: int, error: BaseException):
        self.calls, self.fired = 0, False
        whole = getattr(target, name)

        def faulty(*args, **kwargs):
            self.calls += 1
            if self.calls == nth:
                self.fired = True
                raise error
            return whole(*args, **kwargs)

        patch.setattr(target, name, faulty)


def attempt(call):
    """`(what it returned, None)` or `(None, what it raised)`."""
    try:
        return call(), None
    except BaseException as exc:                    # noqa: BLE001 - the observation itself
        return None, exc


def run_cli(argv) -> tuple[int, str]:
    from subfleet import cli
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue()


# --- a full pass ---------------------------------------------------------------------

def test_a_full_pass_that_raises_is_recorded_as_failed_and_still_raises(world, monkeypatch):
    """C-23.28: the record ends as `error` with the exception's type, its
    message and where it was raised; the caller still gets the exception."""
    unspread(world)
    error = RuntimeError("no pass expects this")
    monkeypatch.setattr(mirror.Mirror, "_spread", raising(error))
    with pytest.raises(RuntimeError) as caught:
        world.full()
    assert caught.value is error
    data = world.sidecar()
    record = data["pass"]
    assert record["state"] == "error" and record["stage"] == "copying entries"
    assert record["started_at"] == record["finished_at"] == fx.iso(CLOCK)
    assert record["error"].startswith("RuntimeError: no pass expects this (mirror.py:")
    assert record["error"].endswith(" in _pass)") and WHERE.search(record["error"])
    assert data["last_ok_at"] is None, "a failed pass is no success"
    assert world.lock_is_free()
    assert world.holds(ONE) == [0]
    health = world.health()
    assert health["status"] == "stalled"
    assert health["detail"] == f"last pass failed: {record['error']}"


def test_a_mirror_that_fails_every_pass_reads_stalled_after_each(world, monkeypatch):
    """C-23.28: 46 passes a minute apart, each of which raises. Each one's new
    start had made health read `running` for good; now none is left in flight."""
    unspread(world)
    monkeypatch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("every pass")))
    seen, between = set(), set()
    for _minute in range(46):
        with pytest.raises(RuntimeError):
            world.full()
        seen.add((world.sidecar()["pass"]["state"], world.health()["status"]))
        world.wait(30)
        between.add(world.health()["status"])       # what a reading between two passes says
        world.wait(30)
    assert seen == {("error", "stalled")} and between == {"stalled"}
    assert world.sidecar()["last_ok_at"] is None and world.holds(ONE) == [0]


def test_a_pass_that_succeeds_after_failures_reads_healthy_again(world, monkeypatch):
    """C-23.28: the failure is the last pass's, not the mirror's for good."""
    unspread(world)
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("for a while")))
        for _minute in range(3):
            with pytest.raises(RuntimeError):
                world.full()
            world.wait(60)
    result = world.full()
    assert result.state == "ok" and result.added == 2 and world.holds(ONE) == [0, 1, 2]
    data = world.sidecar()
    assert data["pass"]["state"] == "ok" and data["pass"]["error"] is None
    assert data["last_ok_at"] == data["pass"]["finished_at"] == fx.iso(world.clock)
    assert world.health()["status"] == "healthy"


def test_status_and_doctor_report_the_failed_pass(world, monkeypatch):
    """C-23.28, C-17.3: `sessions mirror --status` exits 1 and names the cause,
    and `doctor` fails its mirror row, where both had read a pass in flight."""
    unspread(world)
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("no pass expects this")))
        with pytest.raises(RuntimeError):
            world.full()
    cause = world.sidecar()["pass"]["error"]
    code, out = run_cli(["sessions", "mirror", "--status"])
    assert code == 1 and out.splitlines()[0] == f"mirror stalled: last pass failed: {cause}"
    code, out = run_cli(["sessions", "mirror", "--status", "--json"])
    report = json.loads(out)
    assert code == 1 and report["status"] == "stalled" and cause in report["detail"]
    row = doctor.check_mirror(world.root)
    assert row["status"] == doctor.FAIL and row["detail"] == f"last pass failed: {cause}"


@pytest.mark.parametrize("error, state", [
    (KeyboardInterrupt(), "cancelled"), (SystemExit(3), "cancelled"), (Halt("stop"), "cancelled"),
    (Odd("odd"), "error"), (MemoryError(), "error"), (AssertionError("never"), "error"),
], ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else value)
def test_an_interrupt_is_recorded_as_cancelled_and_any_other_exception_as_error(
        world, monkeypatch, error, state):
    """C-23.28: Ctrl-C on `sessions mirror` ends the pass from outside, as the
    daemon's stop does; either way the record is finished and the exception
    goes on to the caller."""
    unspread(world)
    monkeypatch.setattr(mirror.Mirror, "_spread", raising(error))
    _result, raised = attempt(world.full)
    assert raised is error
    record = world.sidecar()["pass"]
    assert record["state"] == state and record["finished_at"] == fx.iso(CLOCK)
    assert record["error"].startswith(f"{type(error).__name__}:") and WHERE.search(record["error"])
    assert world.health()["status"] == "stalled" and world.lock_is_free()


def test_oserror_and_cancellation_are_recorded_and_returned_as_before(world, monkeypatch):
    """C-23.28: the two endings a pass expects do not reach the caller."""
    unspread(world)
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "folders", raising(OSError(errno.EMFILE, "too many open files")))
        result = world.full()
    assert result.state == "error" and result.error == "OSError: [Errno 24] too many open files"
    assert world.sidecar()["pass"] == json.loads(json.dumps(result.to_dict()))
    world.cancel.set()
    result = world.full()
    world.cancel.clear()
    assert result.state == "cancelled" and result.error.startswith("_Cancelled: mirror pass cancelled")
    assert world.sidecar()["pass"] == json.loads(json.dumps(result.to_dict()))
    assert world.health()["status"] == "stalled" and world.lock_is_free()


def test_a_report_that_raises_fails_the_pass_it_ends(world, monkeypatch):
    """C-23.28: the load-gap report is the pass's last step. A pass whose
    copies went through and whose report raised is a failed pass, recorded
    without a report, and it is no success."""
    unspread(world)
    error = TypeError("the report itself")
    monkeypatch.setattr(mirror.Mirror, "load_gap", raising(error))
    _result, raised = attempt(world.full)
    assert raised is error
    data = world.sidecar()
    assert data["pass"]["state"] == "error" and data["pass"]["stage"] == "complete"
    assert data["pass"]["error"].startswith("TypeError: the report itself (mirror.py:")
    assert data["pass"]["added"] == 2 and world.holds(ONE) == [0, 1, 2]
    assert data["last_ok_at"] is None and "load_gap" not in data
    assert world.health()["status"] == "stalled" and world.lock_is_free()


def test_a_report_that_raises_does_not_hide_a_failure_being_recorded(world, monkeypatch):
    """C-23.28: an OSError ends the pass and the report then raises inside the
    pass's own handler. The record still ends, with the exception that is raised."""
    unspread(world)
    error = TypeError("the report itself")
    monkeypatch.setattr(mirror.Mirror, "folders", raising(OSError("store not listed")))
    monkeypatch.setattr(mirror.Mirror, "load_gap", raising(error))
    _result, raised = attempt(world.full)
    assert raised is error and isinstance(raised.__context__, OSError)
    record = world.sidecar()["pass"]
    assert record["state"] == "error" and record["finished_at"] == fx.iso(CLOCK)
    assert record["error"].startswith("TypeError: the report itself")


def test_a_sidecar_that_cannot_be_written_leaves_the_pass_exception_to_raise(world, monkeypatch):
    """C-23.28: recording the end is no reason to lose the cause. The caller
    gets the pass's exception, with a note that the sidecar does not hold it."""
    unspread(world)
    error = RuntimeError("no pass expects this")
    record = mirror.Mirror._record
    calls = []

    def start_only(self, current, **kwargs):
        calls.append(current.state)
        if len(calls) > 1 and current.state != "running":
            raise OSError(errno.ENOSPC, "no space left on device")
        return record(self, current, **kwargs)

    monkeypatch.setattr(mirror.Mirror, "_record", start_only)
    monkeypatch.setattr(mirror.Mirror, "_spread", raising(error))
    _result, raised = attempt(world.full)
    assert raised is error and calls[-1] == "error"
    assert raised.__notes__ == ["the mirror's sidecar does not record this: OSError"]
    assert world.sidecar()["pass"]["state"] == "running", "nothing could say otherwise"
    assert world.lock_is_free()


def test_an_oserror_from_writing_the_end_still_raises_and_the_record_gets_another_try(
        world, monkeypatch):
    """C-23.28: the one change for OSError. One that the pass's own record of
    its end raises went to the caller with the record left at `running`; it
    still goes to the caller, and the record is tried once more."""
    unspread(world)
    world.full()                                    # a record to be left standing
    world.wait(60)
    record = mirror.Mirror._record
    calls = []

    def failing_twice(self, current, **kwargs):
        calls.append(current.state)
        if len(calls) <= 2:
            raise OSError(errno.EIO, f"write {len(calls)}")
        return record(self, current, **kwargs)

    monkeypatch.setattr(mirror.Mirror, "_record", failing_twice)
    _result, raised = attempt(world.full)
    assert isinstance(raised, OSError) and raised.strerror == "write 2"
    assert calls == ["running", "error", "error"]
    data = world.sidecar()["pass"]
    assert data["state"] == "error" and data["started_at"] == fx.iso(world.clock)
    assert data["error"].startswith("OSError: [Errno 5] write 2 (")
    assert world.health()["status"] == "stalled" and world.lock_is_free()


def test_a_pass_that_fails_before_its_inventory_records_no_earlier_split_report(
        world, monkeypatch):
    """C-23.28: a split report is written once, by the pass that took the
    inventory. A pass that raises before taking one writes its own end, and
    not a report that an earlier pass left unrecorded."""
    unspread(world)
    assert world.full().state == "ok"
    recorded = world.sidecar()["splits"]
    assert recorded["checked_at"] == fx.iso(CLOCK)
    world.running._splits = {"checked_at": "an earlier pass's", "count": 9}
    world.wait(60)
    monkeypatch.setattr(mirror._Journal, "refresh", raising(RuntimeError("before the inventory")))
    with pytest.raises(RuntimeError):
        world.full()
    data = world.sidecar()
    assert data["pass"]["state"] == "error" and data["pass"]["stage"] == "starting"
    assert data["splits"] == recorded


def test_a_pass_that_never_took_the_lock_writes_nothing(world, monkeypatch):
    """C-23.28: only the pass that holds the lock writes the sidecar. An
    exception before the lock is the caller's, and the record stays another's."""
    inventoried(world)
    before = world.running.sidecar_path.read_bytes()
    world.wait(60)
    error = RuntimeError("before the lock")
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_lock", raising(error))
        for act in (world.full, world.hot):
            _result, raised = attempt(act)
            assert raised is error
    lock = world.running._lock()
    try:
        for act in (world.full, world.hot):
            assert act().error == "another pass holds the lock"
    finally:
        lock.close()
    assert world.running.sidecar_path.read_bytes() == before


@pytest.mark.parametrize("kind", ["hot", "full"])
def test_a_dry_run_that_raises_writes_no_record(world, monkeypatch, kind):
    """C-17.4, C-23.28: a dry run records nothing, however it ends."""
    inventoried(world)
    world.put(0, TWO)
    before = world.running.sidecar_path.read_bytes()
    world.wait(60)
    monkeypatch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("in a dry run")))
    with pytest.raises(RuntimeError):
        world.hot(dry_run=True) if kind == "hot" else world.full(dry_run=True)
    assert world.running.sidecar_path.read_bytes() == before and world.lock_is_free()


def test_the_record_says_where_in_the_mirror_the_exception_came_from():
    """C-23.28: the timer keeps an exception's type and nothing else, so the
    record names the innermost frame of the mirror the exception came through."""
    assert mirror._raised_at(RuntimeError("never raised")) == ""
    try:
        json.loads("{")                             # raised outside the mirror, through none of it
    except ValueError as exc:
        assert re.fullmatch(r" \(decoder\.py:\d+ in \w+\)", mirror._raised_at(exc))
    try:
        mirror.activity_targets(None, 1)            # raised inside the mirror
    except TypeError as exc:
        assert re.fullmatch(r" \(mirror\.py:\d+ in activity_targets\)", mirror._raised_at(exc))
    else:
        pytest.fail("`activity_targets(None, 1)` no longer raises: pick another raise inside the mirror")


# --- a hot pass ----------------------------------------------------------------------

def test_a_hot_pass_that_raises_is_recorded_at_once_and_leaves_the_full_record_alone(
        world, monkeypatch):
    """C-23.28: `hot` ends as `error`, inside the sixty seconds in which an
    idle hot pass records nothing, and `pass`, `updated_at` and `last_ok_at`
    are the full pass's still."""
    inventoried(world)
    assert world.hot().state == "ok"                # the sampling window opens here
    full = {key: value for key, value in world.sidecar().items() if key not in ("hot", "load_gap")}
    world.put(0, TWO)                               # new: the hot pass has a copy to make
    world.wait(2)
    error = RuntimeError("no pass expects this")
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_spread", raising(error))
        _result, raised = attempt(world.hot)
    assert raised is error
    data = world.sidecar()
    hot = data["hot"]
    assert hot["kind"] == "hot" and hot["state"] == "error" and hot["stage"] == "copying entries"
    assert hot["started_at"] == hot["finished_at"] == fx.iso(world.clock)
    assert hot["error"].startswith("RuntimeError: no pass expects this (mirror.py:")
    assert hot["error"].endswith(" in _hot)")
    assert {key: value for key, value in data.items() if key not in ("hot", "load_gap")} == full
    health = world.health()
    assert health["status"] == "healthy", "a hot pass's failure is detail, as its OSError is"
    assert f"hot pass error: {hot['error']}" in health["detail"]
    assert world.lock_is_free()


@pytest.mark.parametrize("error, state", [
    (KeyboardInterrupt(), "cancelled"), (SystemExit(3), "cancelled"), (Halt("stop"), "cancelled"),
    (Odd("odd"), "error"),
], ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else value)
def test_a_hot_pass_records_an_interrupt_as_cancelled_and_raises_it(world, monkeypatch, error, state):
    """C-23.28: the hot pass's record ends for an interrupt as the full pass's does."""
    inventoried(world)
    world.put(0, TWO)
    world.wait(2)
    monkeypatch.setattr(mirror.Mirror, "_spread", raising(error))
    _result, raised = attempt(world.hot)
    assert raised is error
    hot = world.sidecar()["hot"]
    assert hot["state"] == state and hot["finished_at"] == fx.iso(world.clock)
    assert hot["error"].startswith(f"{type(error).__name__}:") and WHERE.search(hot["error"])
    assert world.sidecar()["pass"]["state"] == "ok" and world.lock_is_free()


def test_a_hot_pass_records_and_returns_oserror_and_cancellation_as_before(world, monkeypatch):
    """C-23.28: the two endings a hot pass expects do not reach the caller either."""
    inventoried(world)
    world.wait(2)
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "folders", raising(OSError(errno.EMFILE, "too many open files")))
        result = world.hot()
    assert result.kind == "hot" and result.state == "error"
    assert result.error == "OSError: [Errno 24] too many open files"
    assert world.sidecar()["hot"] == json.loads(json.dumps(result.to_dict()))
    world.wait(2)
    world.cancel.set()
    result = world.hot()
    world.cancel.clear()
    assert result.kind == "hot" and result.state == "cancelled"
    assert world.sidecar()["hot"] == json.loads(json.dumps(result.to_dict()))
    assert world.sidecar()["pass"]["state"] == "ok" and world.lock_is_free()


def test_a_hot_pass_that_fails_every_time_never_reads_in_flight(world, monkeypatch):
    """C-23.28: every recorded start is finished. 90 hot passes 2 s apart, each
    of which raises: the start sampled once a minute had stayed `running`."""
    inventoried(world)
    monkeypatch.setattr(mirror.Mirror, "_hot", raising(RuntimeError("every hot pass")))
    seen = set()
    for _tick in range(90):
        world.wait(2)
        with pytest.raises(RuntimeError):
            world.hot()
        hot = world.sidecar()["hot"]
        seen.add((hot["state"], hot["finished_at"] == hot["started_at"] == fx.iso(world.clock)))
        assert "hot pass in flight" not in world.health()["detail"]
    assert seen == {("error", True)}


def test_a_failed_hot_pass_leaves_its_new_session_to_the_next_full_pass(world, monkeypatch):
    """C-23.28: what the failed pass read is no longer new to the next hot
    pass, so the full pass is what spreads the session, as it always could."""
    inventoried(world)
    world.put(0, TWO)
    world.wait(2)
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("once")))
        with pytest.raises(RuntimeError):
            world.hot()
    world.wait(2)
    after = world.hot()
    assert after.state == "ok" and after.sessions == 0 and world.holds(TWO) == [0]
    assert world.sidecar()["hot"]["state"] == "ok"
    world.wait(56)
    assert world.full().added == 2 and world.holds(TWO) == [0, 1, 2]


def test_an_embedded_hot_service_that_raises_ends_both_records(tmp_path, monkeypatch):
    """C-23.28: a flag-only hot pass serviced at a full pass's checkpoint
    raises through that pass. Both records end as `error`, and the full pass
    does not go on from an inventory its helper left half used."""
    world = World(tmp_path, monkeypatch, hot_interval=1e-9)
    inventoried(world)
    world.wait(60)
    hot = mirror.Mirror._hot
    error = RuntimeError("inside the service")

    def flags_only_raise(self, current, options, *, spread=True):
        if not spread:
            raise error
        return hot(self, current, options, spread=spread)

    monkeypatch.setattr(mirror.Mirror, "_hot", flags_only_raise)
    _result, raised = attempt(world.full)
    assert raised is error
    data = world.sidecar()
    for record in (data["pass"], data["hot"]):
        assert record["state"] == "error" and record["finished_at"] == fx.iso(world.clock)
        assert record["error"].startswith("RuntimeError: inside the service (mirror.py:")
    assert data["pass"]["kind"] == "full" and data["hot"]["kind"] == "hot"
    assert world.health()["status"] == "stalled" and world.lock_is_free()


def test_the_hot_pass_after_a_service_that_raised_is_recorded_at_once(tmp_path, monkeypatch):
    """C-23.28: an error and what ends it are recorded at once, not sampled.
    After a serviced hot pass raised, the instance must know that an `error`
    stands in the sidecar, or the next idle hot pass leaves it there for up
    to a minute."""
    world = World(tmp_path, monkeypatch, hot_interval=1e-9)
    inventoried(world)
    world.wait(2)
    assert world.hot().state == "ok"                # recorded: the sampling window opens
    world.wait(2)
    hot = mirror.Mirror._hot

    def flags_only_raise(self, current, options, *, spread=True):
        if not spread:
            raise RuntimeError("inside the service")
        return hot(self, current, options, spread=spread)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_hot", flags_only_raise)
        with pytest.raises(RuntimeError):
            world.full()
    assert world.sidecar()["hot"]["state"] == "error"
    world.wait(2)
    after = world.hot()
    assert after.state == "ok" and not after.changed
    assert world.sidecar()["hot"] == json.loads(json.dumps(after.to_dict()))
    assert "hot pass error" not in world.health()["detail"]


# --- the daemon's timer --------------------------------------------------------------

@pytest.fixture
def timed(world):
    """The daemon's timers over the world's state root, and so their own Mirror."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    store = Store(world.base / "state.sqlite3")
    timers = Timers(store, world.root, world.policy, now=lambda: world.clock)

    def events(kind: str, timer: str) -> list[dict]:
        rows = [json.loads(row["data_json"]) for row in store.query(
            "SELECT data_json FROM events WHERE kind=? ORDER BY event_id", (kind,))]
        return [row for row in rows if row.get("timer") == timer]

    try:
        yield timers, events
    finally:
        timers.stop()
        store.close()


def test_the_timer_keeps_its_error_event_for_a_pass_that_raises(world, timed, monkeypatch):
    """C-23.28: the exception still reaches `Timers._run`. Its `timer.error`
    event and `last_error_type` are all the daemon's store learns of a failed
    pass; a pass that returned would read there as a clean run."""
    timers, events = timed
    for index in range(3):
        world.put(index, ONE)
    timers._run("mirror")                           # noqa: SLF001 - as `tick()` submits it
    assert events("timer.error", "mirror") == []
    assert timers.status()["mirror"]["last_error_type"] is None
    world.put(0, TWO)
    world.wait(2)
    monkeypatch.setattr(mirror.Mirror, "_spread", raising(Odd("no pass expects this")))
    timers._run("mirror_hot")                       # noqa: SLF001
    world.wait(58)
    timers._run("mirror")                           # noqa: SLF001
    data = world.sidecar()
    for timer, record in (("mirror_hot", data["hot"]), ("mirror", data["pass"])):
        assert events("timer.error", timer) == [{"timer": timer, "error_type": "Odd"}]
        assert timers.status()[timer]["last_error_type"] == "Odd"
        assert events("timer.run", timer)[-1]["last_error_type"] == "Odd"
        assert record["state"] == "error" and record["error"].startswith("Odd: no pass expects this")
    assert world.health()["status"] == "stalled"


def test_the_timer_records_no_error_for_the_endings_a_pass_expects(world, timed, monkeypatch):
    """C-23.28: an OSError is the pass's own record and no `timer.error`, as before."""
    timers, events = timed
    unspread(world)
    monkeypatch.setattr(mirror.Mirror, "folders", raising(OSError("store not listed")))
    timers._run("mirror")                           # noqa: SLF001
    assert events("timer.error", "mirror") == []
    assert timers.status()["mirror"]["last_error_type"] is None
    assert world.sidecar()["pass"]["error"] == "OSError: store not listed"


# --- for every pass, every place and every exception ----------------------------------

#: Where a fault is put: a function the pass calls, and which exceptions the
#: mirror's own code between there and the pass can meet without the sidecar
#: becoming unwritable. An OSError put into a sidecar write is that case.
EVERY = ("strict", "soft", "os", "base")
UNEXPECTED = ("strict", "soft", "base")
SITES = (
    (mirror._Journal, "refresh", EVERY),
    (mirror._Journal, "save", EVERY),
    (mirror.Mirror, "folders", EVERY),
    (mirror.Mirror, "_listing_gaps", EVERY),
    (mirror.Mirror, "transcript_stems", EVERY),
    (mirror.Mirror, "_scan", EVERY),
    (mirror.Mirror, "_file", EVERY),
    (mirror.Mirror, "_openable", EVERY),
    (mirror, "_rank", EVERY),
    (mirror.Mirror, "_spread", EVERY),
    (mirror.Mirror, "_place", EVERY),
    (mirror, "_copy_entry", EVERY),
    (mirror.Mirror, "sync_flags", EVERY),
    (mirror.Mirror, "_update_flag_retry", EVERY),
    (mirror.Mirror, "load_gap", EVERY),
    (mirror.Mirror, "_checkpoint", EVERY),
    (mirror.Mirror, "_record", EVERY),
    (mirror.Mirror, "_record_hot", UNEXPECTED),
    (mirror, "_write_json", UNEXPECTED),
)
#: No handler inside a pass names these, so one that is raised ends the pass.
STRICT = (RuntimeError, KeyError, ZeroDivisionError, AssertionError, AttributeError,
          IndexError, MemoryError, RecursionError, Odd)
#: Some handlers inside a pass name these (a write that fails, a record that
#: is no record), so one may be met and the pass go on.
SOFT = (ValueError, TypeError)
BASE = (KeyboardInterrupt, SystemExit, Halt)
MESSAGES = st.text(max_size=24)


def errors(family: str) -> st.SearchStrategy[BaseException]:
    if family == "os":
        return st.one_of(
            st.builds(OSError, MESSAGES),
            st.sampled_from([errno.EMFILE, errno.EIO, errno.ENOSPC, errno.EACCES, errno.ENOENT])
            .map(lambda code: OSError(code, errno.errorcode[code])))
    kinds = {"strict": STRICT, "soft": SOFT, "base": BASE}[family]
    return st.builds(lambda kind, text: kind(text), st.sampled_from(kinds), MESSAGES)


@st.composite
def faults(draw):
    """None, the daemon's stop at some checkpoint, or one exception at one call."""
    choice = draw(st.sampled_from(["none", "cancel", "raise", "raise", "raise"]))
    if choice == "none":
        return None
    nth = draw(st.sampled_from([1, 1, 1, 2, 2, 3, 4, 6, 9, 12]))
    if choice == "cancel":
        return ("cancel", nth)
    target, name, families = draw(st.sampled_from(SITES))
    return ("raise", target, name, nth, draw(errors(draw(st.sampled_from(families)))))


FOLDER = st.integers(min_value=0, max_value=2)
EDITS = st.one_of(
    st.tuples(st.just("record"), st.sampled_from(SESSIONS), FOLDER),
    st.tuples(st.just("save"), st.sampled_from(SESSIONS), FOLDER,
              st.sampled_from([{"isArchived": True}, {"isArchived": False}, {"isStarred": True},
                               {"title": "renamed", "titleSource": "manual"}])),
)
#: The app's saves, which pass runs, how long since the last one, and what ends it.
STEPS = st.tuples(st.lists(EDITS, max_size=2), st.sampled_from(["full", "hot", "hot"]),
                  st.sampled_from([2, 2, 61]), faults())


def edit(world: World, step: tuple) -> None:
    """One of the app's own changes to the store."""
    match step:
        case ("record", session, index):
            if world.path(index, session).exists():
                world.save(index, session, lastFocusedAt=NEW + 1)
            else:
                world.put(index, session)
        case ("save", session, index, fields):
            if world.path(index, session).exists():
                world.save(index, session, **fields)


def arm(world: World, patch, fault) -> Fault | None:
    match fault:
        case ("cancel", nth):
            checkpoint = mirror.Mirror._checkpoint
            calls = [0]

            def stopping(self, *args, **kwargs):
                calls[0] += 1
                if calls[0] == nth:
                    world.cancel.set()              # the daemon's stop
                return checkpoint(self, *args, **kwargs)

            patch.setattr(mirror.Mirror, "_checkpoint", stopping)
        case ("raise", target, name, nth, error):
            return Fault(patch, target, name, nth, error)
    return None


def ended_as(record: dict, result, raised, when: str) -> None:
    """One pass's record against what the call did: finished, and one account of the end."""
    assert record["state"] in FINISHED and record["finished_at"] is not None, record
    assert record["started_at"] == when, "the record is this pass's"
    if raised is None:
        assert record == json.loads(json.dumps(result.to_dict()))
        return
    assert record["state"] == ("error" if isinstance(raised, Exception) else "cancelled")
    assert record["error"].startswith(f"{type(raised).__name__}:") and WHERE.search(record["error"])


@settings(max_examples=250, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(interval=st.sampled_from([0, 1e-9]), steps=st.lists(STEPS, min_size=1, max_size=6))
def test_every_pass_that_starts_is_recorded_as_finished(interval, steps, tmp_path_factory,
                                                        monkeypatch):
    """C-23.28: for any passes, full and hot, with the app saving between
    them, each ended by nothing, by the daemon's stop at any checkpoint, or by
    any exception at any call of any step (with a flag-only hot pass serviced
    at every checkpoint, or none): the record is finished, says what the call
    did, and health follows it. A clean full pass then reads healthy."""
    with monkeypatch.context() as patch:
        world = World(tmp_path_factory.mktemp("passes"), patch, hot_interval=interval)
        for index in range(3):
            world.put(index, ONE)
        world.put(0, TWO)
        for edits, kind, pause, fault in steps:
            for step in edits:
                edit(world, step)
            world.wait(pause)
            before = world.sidecar()
            full = kind == "full" or not world.running._inventoried    # a first hot pass is a full one
            with pytest.MonkeyPatch.context() as faulty:
                armed = arm(world, faulty, fault)
                result, raised = attempt(world.full if kind == "full" else world.hot)
            stopped = world.cancel.is_set()
            world.cancel.clear()
            error = fault[4] if armed is not None and armed.fired else None
            if raised is not None:
                assert raised is error, f"{raised!r} was raised, with {fault!r}"
                assert not isinstance(error, OSError), "an OSError returns"
            else:
                assert not isinstance(error, STRICT + BASE), f"{error!r} at {fault[2]} returned"
                if stopped:
                    assert result.state == "cancelled", result
                elif error is None:
                    assert result.state == "ok", result
            event(f"{'full' if full else 'hot'} pass: "
                  + (f"raised {'an interrupt' if isinstance(raised, BASE) else 'an exception'}"
                     if raised is not None else f"returned {result.state}"
                     + (", having met the fault" if error is not None else "")))
            data = world.sidecar()
            when = fx.iso(world.clock)
            hot = data.get("hot")
            # Every recorded start is finished, whichever pass recorded it.
            assert hot is None or (hot["state"] in FINISHED and hot["finished_at"] is not None), hot
            if full:
                record = data["pass"]
                ended_as(record, result, raised, when)
                assert data["last_ok_at"] == (record["finished_at"] if record["state"] == "ok"
                                              else before.get("last_ok_at"))
            else:
                failed = raised is not None or result.state != "ok"
                stood = (before.get("hot") or {}).get("state") in ("error", "cancelled")
                if failed or stood:                 # an error, and its end, are recorded at once
                    ended_as(hot, result, raised, when)
                # A hot pass never changes the full pass's record.
                assert ({key: value for key, value in data.items() if key not in ("hot", "load_gap")}
                        == {key: value for key, value in before.items()
                            if key not in ("hot", "load_gap")})
            status = world.health()["status"]
            assert status == ("healthy" if data["pass"]["state"] == "ok" else "stalled"), data["pass"]
            assert world.lock_is_free()
        world.wait(60)
        last = world.full()
        assert last.state == "ok" and world.health()["status"] == "healthy"
        assert world.sidecar()["last_ok_at"] == fx.iso(world.clock)


@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(timer=st.sampled_from(["mirror", "mirror_hot"]), fault=faults())
def test_the_timer_and_the_sidecar_name_the_same_ending(timer, fault, tmp_path_factory,
                                                        monkeypatch):
    """C-23.28: two records of one pass, the daemon's `timer.error` event and
    the mirror's sidecar. A pass has an error event exactly when its record is
    an `error` that is no OSError's, and both name the same type."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    if fault is not None and fault[0] == "raise" and isinstance(fault[4], BASE):
        fault = None                                # `Timers._run` catches Exception, as a worker does
    with monkeypatch.context() as patch:
        world = World(tmp_path_factory.mktemp("timer"), patch)
        store = Store(world.base / "state.sqlite3")
        timers = Timers(store, world.root, world.policy, now=lambda: world.clock)
        timers.cancel = world.cancel
        try:
            for index in range(3):
                world.put(index, ONE)
            timers._run("mirror")                   # noqa: SLF001 - the first inventory
            assert world.sidecar()["pass"]["state"] == "ok"
            world.put(0, TWO)
            world.wait(61)
            with pytest.MonkeyPatch.context() as faulty:
                arm(world, faulty, fault)
                timers._run(timer)                  # noqa: SLF001
            world.cancel.clear()
            logged = [row for row in (json.loads(item["data_json"]) for item in store.query(
                "SELECT data_json FROM events WHERE kind='timer.error' ORDER BY event_id"))
                if row]                             # the store's own row beside each event is empty
            data = world.sidecar()
            record = data["pass" if timer == "mirror" else "hot"]
            assert record["state"] in FINISHED and record["finished_at"] is not None
            named = (record["error"] or "").split(":")[0]
            returned = isinstance(getattr(builtins, named, None), type) and issubclass(
                getattr(builtins, named), OSError)
            if logged:
                assert logged == [{"timer": timer, "error_type": named}]
                assert record["state"] == "error" and not returned
                assert timers.status()[timer]["last_error_type"] == named
            else:
                assert timers.status()[timer]["last_error_type"] is None
                assert record["state"] != "error" or returned, record
        finally:
            timers.stop()
            store.close()
