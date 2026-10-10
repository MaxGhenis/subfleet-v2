"""A failed pass stays visible after the next pass starts: C-23.28.

The sidecar's `pass` holds one full pass, and each pass writes its start
before it does any work, so the start of the pass after a failed one replaced
the failed pass's record. Health read the new pass as in flight and said
nothing of the failure. On a fixture store on the daemon's 60 s interval, a
mirror whose every pass ran 40 s and then failed read `running`, with no word
of a failure, in 400 of 600 readings a second apart; `sessions mirror
--status` exited 0 and `doctor` passed its mirror row at each of them
(`docs/reports/2026-10-10-mirror-failure-carried.md`).

The full pass's record now also keeps, beside `last_ok_at`, `last_end` (the
last full pass that is over), `not_ok_passes` (how many in a row, that one
the last, did not end `ok`) and `not_ok_since` (when the first of them
started). The invariants these tests hold the mirror to:

* the run: after any passes, `not_ok_passes` counts the full passes since the
  last one that ended `ok` (all of them, if none did), a pass whose end the
  next pass found unrecorded among them; `not_ok_since` is the first one's
  start, and is null exactly when the count is 0; `last_end` is the last pass
  that is over, `unfinished` for one that recorded no end;
* a start carries, an end replaces: while a pass is in flight the three are
  what they were when it started;
* an end written twice counts once;
* a hot pass, a dry run and a pass that never took the lock change none of
  the three;
* once `pass` is over, `last_end` is that pass, and the count is 0 exactly
  when it ended `ok`;
* the three change one status: a pass in flight, inside the hang limit,
  after a pass that did not end `ok` reads `stalled`; every other reading has
  the status it has with the three removed, and every detail begins with the
  detail it gives without them (a differential against that reading);
* while a pass is in flight, the reading names the last pass before it that
  did not end `ok`, and how many in a row, whenever there was one;
* no value in the three, from an older mirror or a hand edit, makes a
  reading or a pass raise;
* a sidecar that cannot be read for a cause that may pass is never taken for
  an empty one: no record is written over it, so the run survives the read
  that failed; one no one can read is written over, as before.

Every test names the clause it proves (C-20.5). The desktop store, the
transcripts, the app's log and the state root live under `tmp_path`, through
SUBFLEET_SESSION_STORE, SUBFLEET_CLAUDE_DIR, SUBFLEET_DESKTOP_LOG and
SUBFLEET_HOME (`World`, shared with the pass-failure tests); nothing here
reads or writes the operator's own.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from subfleet import doctor
from subfleet.sessions import mirror
from tests import sessions_fixtures as fx
from tests.unit.test_sessions_mirror_pass_failure import (
    ONE, THREE, TWO, Odd, World, attempt, inventoried, raising, run_cli, unspread)

CARRIED = ("last_end", "not_ok_passes", "not_ok_since")


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def carried(data: dict) -> dict:
    return {key: data.get(key) for key in CARRIED}


def stripped(data: dict) -> dict:
    """The sidecar as a mirror without the three wrote it."""
    return {key: value for key, value in data.items() if key not in CARRIED}


def end_of(record: dict) -> dict:
    return {key: record.get(key) for key in mirror.END_FIELDS}


def reader(world: World) -> mirror.Mirror:
    return mirror.Mirror(world.root, world.policy, now=lambda: world.clock)


def run_of(data: dict) -> int:
    """`not_ok_passes` where it is one: beside an object `last_end`, a whole
    number from zero to `RUN_LIMIT` that is no boolean, and, if it is not
    zero, with a start that is a time. Read here, not by the mirror's own
    `_carried`, so the rule below is checked against an account of its own."""
    last, count, since = (data.get("last_end"), data.get("not_ok_passes"),
                          data.get("not_ok_since"))
    if not (isinstance(last, dict) and type(count) is int and 0 <= count <= mirror.RUN_LIMIT):
        return 0
    if count and not (isinstance(since, str) and mirror._instant(since) is not None):
        return 0
    return count


def by_the_rule(world: World, data: dict) -> dict:
    """The rule, against the same sidecar without the three: a pass in flight
    after a pass that did not end ok reads `stalled`; every other reading has
    the status it had; every detail only adds to the one it had."""
    engine = reader(world)
    with_them, without = engine._full_health(data), engine._full_health(stripped(data))
    record = data.get("pass") or {}
    in_flight = record.get("state") == "running" and mirror._instant(record.get("finished_at")) is None
    expected = "stalled" if in_flight and run_of(data) else without["status"]
    assert with_them["status"] == expected, (with_them, without)
    assert with_them["detail"].startswith(without["detail"]), (with_them, without)
    return with_them


def on_the_world_clock(world: World, patch) -> None:
    """`sessions mirror --status` and `doctor` build a Mirror with no clock:
    give it the world's, so a reading's minutes are the fixture's."""
    build = mirror.Mirror.__init__

    def built(engine, root, policy=None, *, now=None, cancel=None):
        build(engine, root, policy, now=now or (lambda: world.clock), cancel=cancel)

    patch.setattr(mirror.Mirror, "__init__", built)


def during(world: World, patch, nth: int, act, *, kind: str = "full",
           seconds: float = 0.0) -> list:
    """At the `nth` checkpoint of a pass of `kind`: move both clocks on by
    `seconds`, then `act(current)`. Returns the list `act`'s results go to."""
    checkpoint = mirror.Mirror._checkpoint
    calls, seen = [0], []

    def hooked(engine, current, stage=None):
        if current.kind == kind:
            calls[0] += 1
            if calls[0] == nth:
                world.wait(seconds)
                seen.append(act(current))
        return checkpoint(engine, current, stage)

    patch.setattr(mirror.Mirror, "_checkpoint", hooked)
    return seen


def unreadable(world: World, patch, failing: set[int], *, code: int = errno.EMFILE,
               known: list[bool] | None = None) -> list[int]:
    """The writers' reads of the sidecar (`_load(..., strict=True)`) whose
    numbers are in `failing` raise `code` (EMFILE: a full descriptor table).
    Readers (`health`, `doctor`, `--status`) read as before. Returns the
    numbers of the reads that failed; `known` gets, for each, whether the
    file was then as this process last read or wrote it."""
    load = mirror._load
    reads, failed = [0], []

    def loading(path, *, strict=False):
        if strict and Path(path) == world.running.sidecar_path:
            reads[0] += 1
            if reads[0] in failing:
                failed.append(reads[0])
                if known is not None:
                    known.append(world.running._sidecar_seen.signature
                                 == mirror._stat_signature(os.lstat(path)))
                raise OSError(code, os.strerror(code))
        return load(path, strict=strict)

    patch.setattr(mirror, "_load", loading)
    return failed


def fail_once(world: World, patch, error: BaseException) -> dict:
    """One full pass that `_spread` ends with `error`; its finished record."""
    with patch.context() as faulty:
        faulty.setattr(mirror.Mirror, "_spread", raising(error))
        attempt(world.full)
    return world.sidecar()["pass"]


# --- the pass after a failed one ------------------------------------------------------

@pytest.mark.parametrize("error", [RuntimeError("no pass expects this"),
                                   OSError(errno.EMFILE, "too many open files")],
                         ids=["RuntimeError", "OSError"])
def test_the_pass_after_a_failed_one_names_it_while_in_flight(world, monkeypatch, error):
    """C-23.28, C-17.3: the next start replaced the failed record, and every
    reader said only "a pass has been in flight" and passed. Now each names
    the failure, in its detail and in the JSON reply, and reads `stalled`."""
    on_the_world_clock(world, monkeypatch)
    unspread(world)
    failed = fail_once(world, monkeypatch, error)
    assert failed["state"] == "error"
    world.wait(60)
    started = fx.iso(world.clock)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 3, lambda _current: (
            world.sidecar(), world.health(), run_cli(["sessions", "mirror", "--status"]),
            run_cli(["sessions", "mirror", "--status", "--json"]),
            doctor.check_mirror(world.root)), seconds=40)
        assert world.full().state == "ok"
    data, health, (code, out), (json_code, json_out), row = seen[0]
    assert data["pass"]["state"] == "running" and data["pass"]["started_at"] == started
    assert carried(data) == {"last_end": end_of(failed), "not_ok_passes": 1,
                             "not_ok_since": failed["started_at"]}
    detail = f"a pass has been in flight for 0.7 min; the pass before it failed: {failed['error']}"
    assert health["status"] == "stalled" and health["detail"] == detail
    assert by_the_rule(world, data)["detail"] == detail
    assert code == 1 and out.splitlines()[0] == f"mirror stalled: {detail}"
    reply = json.loads(json_out)
    assert json_code == 1 and reply["status"] == "stalled" and reply["detail"] == detail
    assert reply["last_end"] == end_of(failed) and reply["not_ok_passes"] == 1
    assert reply["not_ok_since"] == failed["started_at"] and reply["last_ok_at"] is None
    assert row["status"] == doctor.FAIL and row["detail"] == detail


def test_a_run_of_failures_is_named_with_its_length_and_the_last_ok_pass(world, monkeypatch):
    """C-23.28: three passes that fail in different ways after a clean one.
    Between passes and while the fourth is in flight, the reading names the
    last failure, how many in a row, and when a pass last ended ok; a pass
    that ends ok clears the run."""
    unspread(world)
    assert world.full().state == "ok"
    good = world.sidecar()["last_ok_at"]
    assert carried(world.sidecar()) == {"last_end": end_of(world.sidecar()["pass"]),
                                        "not_ok_passes": 0, "not_ok_since": None}
    world.put(0, TWO)                               # a session the next passes must spread
    starts = []
    for error in (RuntimeError("first"), OSError(errno.EIO, "second"), Odd("third")):
        world.wait(60)
        starts.append(fx.iso(world.clock))
        last = fail_once(world, monkeypatch, error)
    data = world.sidecar()
    assert carried(data) == {"last_end": end_of(last), "not_ok_passes": 3,
                             "not_ok_since": starts[0]}
    world.wait(30)
    between = world.health()
    assert between["status"] == "stalled"
    assert between["detail"] == (f"last pass failed: {last['error']}; 3 passes in a row did not "
                                 f"end ok; no pass has ended ok since {good}")
    world.wait(30)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: (world.sidecar(), world.health()))
        assert world.full().state == "ok"
    data, health = seen[0]
    assert carried(data) == {"last_end": end_of(last), "not_ok_passes": 3,
                             "not_ok_since": starts[0]}
    assert health["status"] == "stalled"
    assert health["detail"] == ("a pass has been in flight for 0.0 min; the 3 passes before it "
                                f"did not end ok, and the last failed: {last['error']}; "
                                f"no pass has ended ok since {good}")
    data = world.sidecar()
    assert carried(data) == {"last_end": end_of(data["pass"]), "not_ok_passes": 0,
                             "not_ok_since": None}
    assert data["last_end"]["state"] == "ok" and data["last_ok_at"] == fx.iso(world.clock)
    health = world.health()
    assert health["status"] == "healthy" and "did not end ok" not in health["detail"]
    assert health["not_ok_passes"] == 0 and health["last_end"] == end_of(data["pass"])


def test_a_cancelled_pass_is_named_as_cancelled(world, monkeypatch):
    """C-23.28: the daemon's stop ends a pass `cancelled`; the next pass in
    flight says so, not "failed"."""
    unspread(world)
    world.cancel.set()
    assert world.full().state == "cancelled"
    world.cancel.clear()
    stopped = world.sidecar()["pass"]
    world.wait(60)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: world.health())
        world.full()
    assert seen[0]["detail"] == ("a pass has been in flight for 0.0 min; the pass before it "
                                 f"was cancelled: {stopped['error']}")
    assert seen[0]["status"] == "stalled", "the rule's stated cost: a stop counts as a failure"


# --- a pass that recorded no end ------------------------------------------------------

def test_a_pass_whose_process_died_is_one_that_recorded_no_end(world, monkeypatch):
    """C-23.28: only the lock's holder writes `pass`, so a record the next
    pass finds still `running` is a pass that is over and recorded no end.
    It joins the run as `unfinished`, with the stage it had reached."""
    unspread(world)
    started = fx.iso(world.clock)
    with monkeypatch.context() as patch:
        snapshot = during(world, patch, 3, lambda current: (
            world.running.sidecar_path.read_bytes(), current.stage))
        world.full()
    left, stage = snapshot[0]
    world.running.sidecar_path.write_bytes(left)     # the process died at that checkpoint
    world.running = mirror.Mirror(world.root, world.policy, now=lambda: world.clock,
                                  cancel=world.cancel)
    assert world.health()["status"] == "running", "a dead pass reads in flight, as before"
    world.wait(60)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: (world.sidecar(), world.health()))
        assert world.full().state == "ok"
    data, health = seen[0]
    assert data["last_end"]["state"] == mirror.UNFINISHED
    assert data["last_end"]["started_at"] == started and data["last_end"]["stage"] == stage
    assert data["last_end"]["finished_at"] is None
    assert data["not_ok_passes"] == 1 and data["not_ok_since"] == started
    assert health["status"] == "stalled"
    assert health["detail"] == ("a pass has been in flight for 0.0 min; the pass before it "
                                f"recorded no end (last stage: {stage})")


def test_a_pass_that_died_after_failures_lengthens_their_run(world, monkeypatch):
    """C-23.28: a pass that recorded no end joins the run before it, which
    keeps its first start; it does not start a run of its own."""
    unspread(world)
    first = fail_once(world, monkeypatch, RuntimeError("first"))
    world.wait(60)
    fail_once(world, monkeypatch, OSError(errno.EIO, "second"))
    world.wait(60)
    with monkeypatch.context() as patch:
        snapshot = during(world, patch, 3, lambda _current: world.running.sidecar_path.read_bytes())
        world.full()
    world.running.sidecar_path.write_bytes(snapshot[0])
    world.running = mirror.Mirror(world.root, world.policy, now=lambda: world.clock,
                                  cancel=world.cancel)
    world.wait(60)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: world.sidecar())
        world.full()
    assert seen[0]["last_end"]["state"] == mirror.UNFINISHED
    assert seen[0]["not_ok_passes"] == 3 and seen[0]["not_ok_since"] == first["started_at"]


def test_an_end_that_could_not_be_written_is_counted_by_the_next_pass(world, monkeypatch):
    """C-23.28: a pass whose end the sidecar could not take leaves `running`
    in it, in this process too; the next pass counts it as unfinished."""
    unspread(world)
    started = fx.iso(world.clock)
    record = mirror.Mirror._record

    def start_only(self, current, **kwargs):
        if current.state != "running":
            raise OSError(errno.ENOSPC, "no space left on device")
        return record(self, current, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_record", start_only)
        _result, raised = attempt(world.full)
    assert isinstance(raised, OSError) and world.sidecar()["pass"]["state"] == "running"
    world.wait(60)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: world.sidecar())
        assert world.full().state == "ok"
    assert seen[0]["last_end"]["state"] == mirror.UNFINISHED
    assert seen[0]["last_end"]["started_at"] == started
    assert seen[0]["not_ok_passes"] == 1 and seen[0]["not_ok_since"] == started


def test_an_end_written_twice_counts_once(world, monkeypatch):
    """C-23.28: a pass whose `error` end was written and which an interrupt
    then ended writes its end again as `cancelled` (`_record_raised`). The
    run counts the pass once, and the second end is the one kept."""
    unspread(world)
    first = fail_once(world, monkeypatch, RuntimeError("first"))
    world.wait(60)
    write = mirror._write_json
    written = []

    def interrupted_after_the_error(path, value, **kwargs):
        inode = write(path, value, **kwargs)
        if Path(path).name == mirror.SIDECAR_NAME:
            written.append(value["pass"]["state"])
            if written.count("error") == 1 and value["pass"]["state"] == "error":
                written.append("interrupt")
                raise KeyboardInterrupt
        return inode

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_write_json", interrupted_after_the_error)
        patch.setattr(mirror.Mirror, "_spread", raising(OSError(errno.EIO, "second")))
        _result, raised = attempt(world.full)
    assert isinstance(raised, KeyboardInterrupt)
    assert written[-3:] == ["error", "interrupt", "cancelled"], written
    data = world.sidecar()
    assert data["pass"]["state"] == "cancelled" and data["last_end"] == end_of(data["pass"])
    assert data["not_ok_passes"] == 2 and data["not_ok_since"] == first["started_at"]


# --- a sidecar that cannot be read -----------------------------------------------------

def written_elsewhere(world: World) -> None:
    """Another process's write of the sidecar, as `sessions mirror --once`
    makes while the daemon waits for the lock: the same contents under a new
    inode, so this process no longer knows what the file holds."""
    path = world.running.sidecar_path
    other = path.with_name(path.name + ".elsewhere")
    other.write_bytes(path.read_bytes())
    os.replace(other, path)


#: Whether the file is as this process last read or wrote it when a read fails.
KNOWN = pytest.mark.parametrize("known", [True, False], ids=["known", "written-elsewhere"])
CAUSES = pytest.mark.parametrize("code", [errno.EMFILE, errno.EIO], ids=["EMFILE", "EIO"])


@KNOWN
@CAUSES
def test_a_read_that_fails_at_a_start_keeps_the_run(world, monkeypatch, known, code):
    """C-23.28 (review of #172, P2): two failed passes, then the third pass's
    read of the sidecar at its start fails. That read had become an empty
    sidecar, and the third start a run of none. Now, when the file is as
    this process last wrote it, that copy is used and the start is written;
    when another process wrote it since, the start waits for the pass's next
    read. Either way the run is three, and nothing else in the sidecar is lost."""
    unspread(world)
    first = fail_once(world, monkeypatch, RuntimeError("first"))
    world.wait(61)
    fail_once(world, monkeypatch, RuntimeError("second"))
    kept_ok = world.sidecar()["last_ok_at"]
    if not known:
        written_elsewhere(world)
    world.wait(61)
    started = fx.iso(world.clock)
    with monkeypatch.context() as patch:
        failed = unreadable(world, patch, {1}, code=code)
        seen = during(world, patch, 1, lambda _current: world.sidecar())
        patch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("third")))
        attempt(world.full)
    assert failed == [1]
    at_the_first_checkpoint = seen[0]
    if known:
        assert at_the_first_checkpoint["pass"]["started_at"] == started
        assert at_the_first_checkpoint["not_ok_passes"] == 2
    else:
        assert at_the_first_checkpoint["pass"]["error"].startswith("RuntimeError: second")
    data = world.sidecar()
    assert data["pass"]["error"].startswith("RuntimeError: third")
    assert data["not_ok_passes"] == 3 and data["not_ok_since"] == first["started_at"]
    assert data["last_ok_at"] == kept_ok
    assert world.health()["detail"].endswith("; 3 passes in a row did not end ok; no pass on "
                                             "record has ended ok")


def test_a_failed_read_after_another_process_passed_does_not_bring_back_what_this_one_knew(
        world, monkeypatch):
    """C-23.28: the copy this process keeps is what the file holds only while
    its signature is unchanged. Here `sessions mirror --once` (another
    process) fails a pass between two of the daemon's; the daemon's next read
    fails. Its copy is from before that pass, so it is not used: the run
    counts the other process's failure."""
    unspread(world)
    first = fail_once(world, monkeypatch, RuntimeError("the daemon's"))
    world.wait(61)
    other = mirror.Mirror(world.root, world.policy, now=lambda: world.clock, cancel=world.cancel)
    with monkeypatch.context() as patch:
        patch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("the other process's")))
        attempt(lambda: other.run_once(world.options()))
    assert world.sidecar()["not_ok_passes"] == 2
    world.wait(61)
    with monkeypatch.context() as patch:
        unreadable(world, patch, {1})
        patch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("the daemon's again")))
        attempt(world.full)
    data = world.sidecar()
    assert data["pass"]["error"].startswith("RuntimeError: the daemon's again")
    assert data["not_ok_passes"] == 3 and data["not_ok_since"] == first["started_at"]


def test_the_embedded_hot_worker_keeps_the_copy_current(tmp_path, monkeypatch):
    """C-23.28: while a full pass holds the lock, its embedded hot worker is
    the one other writer. It writes the hot record into the copy the full
    pass keeps, so a full pass whose end read then fails still knows the file
    and records its end as it ended."""
    world = World(tmp_path, monkeypatch, hot_interval=1e-9)
    for index in range(3):
        world.put(index, ONE)
    assert world.full().state == "ok"
    world.put(0, TWO)
    world.save(1, ONE, isStarred=True)                # a flag the embedded service publishes
    world.wait(61)
    with monkeypatch.context() as patch:
        write = mirror._write_json
        hot_writes = []

        def counting(path, value, **kwargs):
            if Path(path) == world.running.sidecar_path and "hot" in value and \
                    value["hot"].get("started_at") == fx.iso(world.clock):
                hot_writes.append(value["hot"]["state"])
            return write(path, value, **kwargs)

        patch.setattr(mirror, "_write_json", counting)
        failed = the_end_unreadable(world, patch)
        result = world.full()
    assert hot_writes, "the embedded worker wrote no hot record in this pass"
    assert len(failed) == 1 and result.state == "ok"
    data = world.sidecar()
    assert data["pass"]["state"] == "ok" and data["hot"]["started_at"] == fx.iso(world.clock)


def the_end_unreadable(world: World, patch, code: int = errno.EMFILE) -> list:
    """The first writer read after the full pass's `complete` checkpoint fails:
    the read of its end. Returns the reads that failed."""
    checkpoint = mirror.Mirror._checkpoint
    complete = []

    def marking(engine, current, stage=None):
        result = checkpoint(engine, current, stage)
        if stage == "complete":
            complete.append(current.state)          # its progress record is written by now
        return result

    patch.setattr(mirror.Mirror, "_checkpoint", marking)
    load = mirror._load
    failed = []

    def end_unreadable(path, *, strict=False):
        if strict and complete and not failed and Path(path) == world.running.sidecar_path:
            failed.append(path)
            raise OSError(code, os.strerror(code))
        return load(path, strict=strict)

    patch.setattr(mirror, "_load", end_unreadable)
    return failed


@CAUSES
def test_a_read_that_fails_at_an_end_records_the_pass_as_it_ended(world, monkeypatch, code):
    """C-23.28 (second review of #172): one failed read of an end had made an
    `ok` pass `error`. The pass's own progress record is what the file holds,
    so the end is written over that copy, as it ended."""
    unspread(world)
    fail_once(world, monkeypatch, RuntimeError("first"))
    world.wait(61)
    with monkeypatch.context() as patch:
        failed = the_end_unreadable(world, patch, code)
        result = world.full()
    assert len(failed) == 1 and result.state == "ok" and result.error is None
    data = world.sidecar()
    assert data["pass"]["state"] == "ok" and data["not_ok_passes"] == 0
    assert data["last_ok_at"] == data["pass"]["finished_at"]


def test_an_end_whose_read_fails_on_a_file_it_does_not_know_fails_the_pass(world, monkeypatch):
    """C-23.28: when the file is not what this process last wrote (another
    writer, which the lock rules out for a pass in flight, or a signature that
    cannot be read), the end raises the OSError, as a failed write of the end
    does (#170): the pass then ends `error` over the sidecar it reads next,
    and the run before it is kept."""
    unspread(world)
    first = fail_once(world, monkeypatch, RuntimeError("first"))
    world.wait(61)
    with monkeypatch.context() as patch:
        failed = the_end_unreadable(world, patch)
        lstat, unknown = mirror.os.lstat, []

        def unknown_once(path, *args, **kwargs):
            # The look that follows the failed read cannot say what the file is.
            if failed and not unknown and Path(path) == world.running.sidecar_path:
                unknown.append(path)
                raise OSError(errno.EIO, "fixture")
            return lstat(path, *args, **kwargs)

        patch.setattr(mirror.os, "lstat", unknown_once)
        result = world.full()
    assert unknown
    assert len(failed) == 1 and result.state == "error"
    assert result.error == "OSError: [Errno 24] Too many open files"
    data = world.sidecar()
    assert data["pass"]["state"] == "error" and data["not_ok_passes"] == 2
    assert data["not_ok_since"] == first["started_at"] and data["last_ok_at"] is None


def test_an_end_whose_reads_all_fail_on_a_file_it_does_not_know_writes_nothing(
        world, monkeypatch):
    """C-23.28: when no read of the end works and the file is not known,
    nothing replaces the start record, and the next pass counts this one as
    unfinished."""
    unspread(world)
    fail_once(world, monkeypatch, RuntimeError("first"))
    world.wait(61)
    started = fx.iso(world.clock)
    with monkeypatch.context() as patch:
        load, lstat = mirror._load, mirror.os.lstat
        ours = []

        def ends_unreadable(path, *, strict=False):
            if strict and Path(path) == world.running.sidecar_path:
                record = load(path, strict=True)
                if (record.get("pass") or {}).get("started_at") == started:
                    ours.append(path)
                    raise OSError(errno.EMFILE, "Too many open files")
                return record
            return load(path, strict=strict)

        def unknown(path, *args, **kwargs):
            if ours and Path(path) == world.running.sidecar_path:
                raise OSError(errno.EIO, "fixture")
            return lstat(path, *args, **kwargs)

        patch.setattr(mirror, "_load", ends_unreadable)
        patch.setattr(mirror.os, "lstat", unknown)
        _result, raised = attempt(world.full)
    assert isinstance(raised, OSError) and raised.errno == errno.EMFILE
    data = world.sidecar()
    assert data["pass"]["state"] == "running" and data["pass"]["started_at"] == started
    assert data["not_ok_passes"] == 1
    world.wait(61)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: world.sidecar())
        assert world.full().state == "ok"
    assert seen[0]["not_ok_passes"] == 2 and seen[0]["last_end"]["state"] == mirror.UNFINISHED


@KNOWN
def test_a_hot_pass_whose_read_fails_keeps_the_full_record_and_the_run(world, monkeypatch,
                                                                        known):
    """C-23.28 (review of #172): a hot pass after a failed read had written its
    record over an empty sidecar and erased the full pass's record and the
    run (older than #172). Now it writes over the copy it knows, or, when
    the file is not known, writes nothing and records later."""
    inventoried(world)
    fail_once(world, monkeypatch, RuntimeError("a full pass that failed"))
    if not known:
        written_elsewhere(world)
    kept = world.sidecar()
    world.put(0, TWO)
    world.wait(2)
    with monkeypatch.context() as patch:
        failed = unreadable(world, patch, set(range(1, 10)))
        assert world.hot().state == "ok"
    data = world.sidecar()
    assert failed and data["pass"] == kept["pass"] and carried(data) == carried(kept)
    assert data["last_ok_at"] == kept["last_ok_at"]
    if known:
        assert data["hot"]["state"] == "ok" and data["hot"] != kept.get("hot")
    else:
        assert data == kept
    world.put(0, THREE)
    world.wait(61)
    world.hot()
    data = world.sidecar()
    assert data["hot"]["state"] == "ok" and carried(data) == carried(kept)
    assert data["pass"] == kept["pass"]


def test_a_failed_progress_read_keeps_everything_beside_the_run(world, monkeypatch):
    """C-23.28 (second review of #172, its mutant R07): a progress record over
    a file it cannot read keeps `last_ok_at`, the hot record and the split
    report, not the run alone."""
    inventoried(world)
    world.put(0, TWO)
    world.wait(2)
    assert world.hot().state == "ok"
    kept = world.sidecar()
    assert kept["hot"] and kept["last_ok_at"] and "splits" in kept
    written_elsewhere(world)
    world.wait(61)
    with monkeypatch.context() as patch:
        failed = unreadable(world, patch, {1, 2, 3})
        seen = during(world, patch, 4, lambda _current: world.sidecar())
        assert world.full().state == "ok"
    assert failed == [1, 2, 3]
    for key in ("last_ok_at", "hot", "splits"):
        assert seen[0][key] == kept[key], key


def test_a_full_pass_keeps_the_hot_record(world, monkeypatch):
    """C-23.28 (second review of #172, its mutant R06; older than #172): the
    end of a full pass writes the hot pass's record back as it found it."""
    inventoried(world)
    world.put(0, TWO)
    world.wait(2)
    assert world.hot().state == "ok"
    hot = world.sidecar()["hot"]
    world.wait(61)
    assert world.full().state == "ok"
    assert world.sidecar()["hot"] == hot


def test_a_fifo_where_the_sidecar_belongs_is_written_over(world, monkeypatch):
    """C-23.28: a sidecar that is no regular file holds nothing to keep, as
    before this change: the pass's record replaces it, and the pass is `ok`.
    (A writer that took it for an unreadable sidecar would leave it there
    for good, and record nothing.)"""
    unspread(world)
    world.running.dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(world.running.sidecar_path)
    assert world.full().state == "ok"
    data = world.sidecar()
    assert world.running.sidecar_path.is_file() and data["pass"]["state"] == "ok"
    assert data["not_ok_passes"] == 0 and data["last_end"] == end_of(data["pass"])


def test_a_sidecar_no_one_can_read_is_written_over(world, monkeypatch):
    """C-23.28: a sidecar whose read is denied (EACCES, as for a file of
    mode 000) holds nothing to keep, as one that is no regular file holds
    nothing: the pass writes over it and heals it, as before this change. A
    cause that may pass is the one that leaves the sidecar alone."""
    unspread(world)
    fail_once(world, monkeypatch, RuntimeError("before the sidecar was locked"))
    written_elsewhere(world)                        # so this process's copy cannot stand in
    world.wait(61)
    with monkeypatch.context() as patch:
        load = mirror._load

        def denied(path, *, strict=False):
            if strict and Path(path) == world.running.sidecar_path:
                raise PermissionError(errno.EACCES, "Permission denied")
            return load(path, strict=strict)

        patch.setattr(mirror, "_load", denied)
        assert world.full().state == "ok"
    data = world.sidecar()
    assert data["pass"]["state"] == "ok" and data["not_ok_passes"] == 0
    assert data["last_end"] == end_of(data["pass"])


def test_a_socket_where_the_sidecar_belongs_is_written_over(world, monkeypatch):
    """C-23.28 (second review of #172): opening a socket fails before any
    check of what the file is (EOPNOTSUPP on macOS, not in `_permanent`), so
    the writer looks with `os.lstat`: no regular file, nothing to keep."""
    unspread(world)
    assert world.full().state == "ok"
    path = world.running.sidecar_path
    path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with monkeypatch.context() as here:
        here.chdir(path.parent)                      # AF_UNIX paths are limited to 104 bytes
        server.bind(path.name)
    try:
        world.wait(61)
        result, raised = attempt(world.full)
        assert raised is None and result.state == "ok", (result, raised)
        assert stat.S_ISREG(os.lstat(path).st_mode)
        assert world.health()["status"] == "healthy"
    finally:
        server.close()


def test_a_sidecar_too_deep_to_parse_is_written_over_and_breaks_no_reader(world, monkeypatch):
    """C-23.28 (second review of #172; older than it): a document nested too
    deep for `json.loads` raised RecursionError from every pass and from
    `doctor`. It is no JSON object: readers see none, and a pass writes over it."""
    unspread(world)
    world.running.dir.mkdir(parents=True, exist_ok=True)
    world.running.sidecar_path.write_text("[" * 100_000 + "]" * 100_000)
    assert world.health()["status"] == "absent"
    assert doctor.check_mirror(world.root)["status"] == doctor.UNKNOWN
    assert world.full().state == "ok"
    assert world.health()["status"] == "healthy"


@pytest.mark.parametrize("code", [errno.EIO, errno.EMFILE], ids=["EIO", "EMFILE"])
def test_a_read_that_never_works_stops_no_recording_for_long(world, monkeypatch, code):
    """C-23.28 (second review of #172): a sidecar every read of which fails,
    by readers and writers alike (a bad block; injected). This process knows
    what it last wrote, so its passes go on recording; a process that never
    wrote it (a new daemon) leaves it for `UNREADABLE_KEEP_S`, then writes over
    it, as before the read rule, so a cause that does not pass costs the run
    and never the heartbeat."""
    unspread(world)
    assert world.full().state == "ok"
    opened = mirror.transcripts.open_regular

    def bad_block(path, mode="rb", *args, **kwargs):
        if Path(path) == world.running.sidecar_path and mode == "r":
            raise OSError(code, os.strerror(code), str(path))
        return opened(path, mode, *args, **kwargs)

    monkeypatch.setattr(mirror.transcripts, "open_regular", bad_block)
    for _minute in range(3):
        before = world.running.sidecar_path.read_bytes()
        world.wait(61)
        assert world.full().state == "ok"
        assert world.running.sidecar_path.read_bytes() != before
    world.running = mirror.Mirror(world.root, world.policy, now=lambda: world.clock,
                                  cancel=world.cancel)
    stood = world.running.sidecar_path.read_bytes()
    left = 0.0                                    # since the first read that failed
    while True:
        world.wait(61)
        attempt(world.full)
        if world.running.sidecar_path.read_bytes() != stood:
            break
        left += 61
        assert left < mirror.UNREADABLE_KEEP_S + 61, "it was never written over"
    assert left >= mirror.UNREADABLE_KEEP_S - 61, "it was written over too soon"
    data = json.loads(world.running.sidecar_path.read_text())
    assert data["pass"]["started_at"] == fx.iso(world.clock)


def test_a_hot_record_that_failed_its_read_is_not_counted_as_recorded(world, monkeypatch):
    """C-23.28 (second review of #172, its mutant R02): a hot `error` stands
    only until the next hot pass that can write: the sampling state moves
    only after a write."""
    for index in range(3):
        world.put(index, ONE)
    assert world.full().state == "ok"
    world.put(0, TWO)
    world.wait(2)
    with monkeypatch.context() as faulty:
        faulty.setattr(mirror.Mirror, "_hot", raising(OSError(errno.EIO, "hot fixture")))
        assert world.hot().state == "error"
    assert world.sidecar()["hot"]["state"] == "error"
    written_elsewhere(world)
    world.wait(2)
    with monkeypatch.context() as faulty:
        failed = unreadable(world, faulty, {1, 2})
        assert world.hot().state == "ok"
    assert failed and world.sidecar()["hot"]["state"] == "error"
    world.wait(2)
    assert world.hot().state == "ok"
    assert world.sidecar()["hot"]["state"] == "ok"


# --- passes in the same second, a start that half failed, the hang limit ----------------

def test_two_passes_that_start_in_the_same_second_are_two(world, monkeypatch):
    """C-23.28 (review of #172): a pass is known by its record object, not by
    its start, which two passes can share."""
    unspread(world)
    first = fail_once(world, monkeypatch, RuntimeError("first"))
    second = fail_once(world, monkeypatch, RuntimeError("second"))
    assert first["started_at"] == second["started_at"]
    assert world.sidecar()["not_ok_passes"] == 2


def test_a_start_that_was_written_and_then_raised_keeps_the_run(world, monkeypatch):
    """C-23.28 (review of #172): the run is worked out before the first write,
    so a start that was written and then raised still counts the passes
    before it once, and the end that follows adds this one."""
    unspread(world)
    fail_once(world, monkeypatch, RuntimeError("first"))
    world.wait(61)
    write = mirror._write_json
    fired = []

    def written_then_raised(path, value, **kwargs):
        inode = write(path, value, **kwargs)
        if Path(path) == world.running.sidecar_path and not fired:
            fired.append(value["pass"]["state"])
            raise OSError(errno.EIO, "the start was written, then the call failed")
        return inode

    monkeypatch.setattr(mirror, "_write_json", written_then_raised)
    assert world.full().state == "error"
    assert fired == ["running"] and world.sidecar()["not_ok_passes"] == 2


def test_a_pass_thirty_minutes_in_is_still_in_flight(world):
    """C-23.28: in flight until thirty minutes after its start, the limit itself
    included; the run changes no part of that."""
    start = mirror._iso(world.clock - timedelta(minutes=30))
    data = {"pass": {"state": "running", "started_at": start, "finished_at": None}}
    assert world.running._full_health(data)["status"] == "running"
    data["pass"]["started_at"] = mirror._iso(world.clock - timedelta(minutes=30, seconds=1))
    assert world.running._full_health(data)["status"] == "stalled"


# --- hand edits -------------------------------------------------------------------------

def disagreeing_end(data: dict) -> None:
    data["not_ok_passes"] = 1000
    data["last_end"] = {**data["last_end"], "stage": "edited"}


def partial_end(data: dict) -> None:
    data["not_ok_passes"] = 1000
    data["last_end"] = {"state": "error", "started_at": data["pass"]["started_at"]}


def past_the_limit(data: dict) -> None:
    data["not_ok_passes"] = mirror.RUN_LIMIT + 1


def four_thousand_three_hundred_digits(data: dict) -> None:
    data["not_ok_passes"] = int("9" * 4300)


def a_start_that_is_no_time(data: dict) -> None:
    data["not_ok_since"] = "not-a-time"


def a_run_with_no_start(data: dict) -> None:
    data["not_ok_passes"], data["not_ok_since"] = 999, None


@pytest.mark.parametrize("edit", [disagreeing_end, partial_end, past_the_limit,
                                  four_thousand_three_hundred_digits, a_start_that_is_no_time,
                                  a_run_with_no_start],
                         ids=lambda edit: edit.__name__)
def test_a_hand_edit_of_the_three_is_not_carried_and_breaks_no_pass(world, monkeypatch, edit):
    """C-23.28 (review of #172, P3): a run whose `last_end` is not the record
    field for field, a count past `RUN_LIMIT` (one that its next increment
    would put past 4,300 digits could not be written back), and a start that
    is no time are not carried: the run starts from the record, and the
    passes that follow record as they would."""
    unspread(world)
    failed = fail_once(world, monkeypatch, RuntimeError("before the edit"))
    path = world.running.sidecar_path
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))
    reading = world.health()
    assert reading["status"] == "stalled" and reading["not_ok_passes"] == 1
    assert reading["not_ok_since"] == failed["started_at"]
    world.wait(61)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: world.sidecar())
        patch.setattr(mirror.Mirror, "_spread", raising(RuntimeError("after the edit")))
        attempt(world.full)
    assert carried(seen[0]) == {"last_end": end_of(failed), "not_ok_passes": 1,
                                "not_ok_since": failed["started_at"]}
    after = world.sidecar()
    assert after["pass"]["state"] == "error" and after["not_ok_passes"] == 2
    assert after["not_ok_since"] == failed["started_at"]


def test_a_run_at_the_limit_stays_at_the_limit(world, monkeypatch):
    """C-23.28 (second review of #172): a run of `RUN_LIMIT` passes that fails
    once more stays at `RUN_LIMIT`, which the next read still carries, where
    one past it would be read as no run at all."""
    unspread(world)
    fail_once(world, monkeypatch, RuntimeError("before the edit"))
    path = world.running.sidecar_path
    data = json.loads(path.read_text())
    data["not_ok_passes"] = mirror.RUN_LIMIT
    path.write_text(json.dumps(data))
    written_elsewhere(world)
    world.wait(61)
    fail_once(world, monkeypatch, RuntimeError("one more"))
    after = world.sidecar()
    assert after["not_ok_passes"] == mirror.RUN_LIMIT
    assert world.health()["not_ok_passes"] == mirror.RUN_LIMIT


# --- what changes none of the three ---------------------------------------------------

def test_hot_passes_dry_runs_and_passes_without_the_lock_change_none_of_the_three(
        world, monkeypatch):
    """C-23.28, C-17.4: a hot pass's failures are detail on the full pass's
    status, never a full pass that did not end ok; a dry run and a pass that
    never took the lock record nothing."""
    inventoried(world)
    fail_once(world, monkeypatch, RuntimeError("a full pass that failed"))
    kept = carried(world.sidecar())
    assert kept["not_ok_passes"] == 1
    world.put(0, TWO)                               # new: the hot pass must spread it
    acts = (
        ("hot pass that raises", lambda: world.hot(), RuntimeError("hot")),
        ("hot pass that fails by OSError", lambda: world.hot(), OSError(errno.EIO, "hot")),
        ("dry full pass that raises", lambda: world.full(dry_run=True), RuntimeError("dry")),
        ("dry hot pass that raises", lambda: world.hot(dry_run=True), RuntimeError("dry")),
        ("dry full pass", lambda: world.full(dry_run=True), None),
    )
    for name, act, error in acts:
        world.wait(2)
        with monkeypatch.context() as patch:
            if error is not None:
                patch.setattr(mirror.Mirror, "_spread", raising(error))
            attempt(act)
        assert carried(world.sidecar()) == kept, name
    lock = world.running._lock()
    try:
        world.wait(60)
        for act in (world.full, world.hot):
            assert act().error == "another pass holds the lock"
    finally:
        lock.close()
    assert carried(world.sidecar()) == kept
    world.wait(2)
    assert world.hot().state == "ok"
    assert carried(world.sidecar()) == kept
    health = world.health()
    assert health["status"] == "stalled" and health["not_ok_passes"] == 1


# --- a sidecar the change did not write -----------------------------------------------

def test_a_sidecar_from_before_the_change_starts_a_run_from_its_record(world, monkeypatch):
    """C-23.28: a mirror that kept none of the three wrote only `pass`. The
    first pass after the upgrade starts the run from that record: a failed
    one is a run of one, an `ok` one a run of none."""
    unspread(world)
    failed = fail_once(world, monkeypatch, RuntimeError("written by the old mirror"))
    path = world.running.sidecar_path
    path.write_text(json.dumps(stripped(json.loads(path.read_text()))))
    reading = world.health()
    assert reading["status"] == "stalled" and reading["not_ok_passes"] == 1
    assert reading["detail"] == f"last pass failed: {failed['error']}"
    world.wait(60)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: world.sidecar())
        assert world.full().state == "ok"
    assert carried(seen[0]) == {"last_end": end_of(failed), "not_ok_passes": 1,
                                "not_ok_since": failed["started_at"]}
    path.write_text(json.dumps(stripped(json.loads(path.read_text()))))
    world.wait(60)
    with monkeypatch.context() as patch:
        seen = during(world, patch, 2, lambda _current: world.sidecar())
        world.full()
    assert seen[0]["not_ok_passes"] == 0 and seen[0]["not_ok_since"] is None
    assert seen[0]["last_end"]["state"] == "ok"


# --- on the daemon's interval ---------------------------------------------------------

def test_on_the_daemons_interval_every_reading_after_a_failure_names_one(world, monkeypatch):
    """C-23.28: passes started by the daemon's scheduler (`Timers.tick`) on its
    60 s interval, each running 40 s and then raising. Read every 5 s for four
    passes, the readings in flight had said only "a pass has been in flight",
    and passed `doctor`; now each one taken after the first failure names a
    failure and reads `stalled`."""
    from subfleet.store import Store
    from subfleet.timers import Timers
    origin = time.monotonic()
    monkeypatch.setattr(mirror.time, "monotonic", lambda: origin + world.offset)
    unspread(world)
    store = Store(world.base / "state.sqlite3")
    timers = Timers(store, world.root, world.policy, now=lambda: world.clock)
    timers.intervals = {"mirror": world.policy["sessions"]["mirror_interval_s"]}
    readings: list[tuple[bool, dict]] = []

    def read(in_flight: bool) -> None:
        data = world.sidecar()
        if data.get("pass"):
            readings.append((in_flight, by_the_rule(world, data)))

    def slow(engine, exclude):
        for _step in range(8):
            world.wait(5)
            read(True)
        raise RuntimeError("fixture: no pass expects this")

    monkeypatch.setattr(mirror.Mirror, "folders", slow)
    submitted = []
    submit = timers._mirror.submit                   # noqa: SLF001

    def remember(*args, **kwargs):
        future = submit(*args, **kwargs)
        submitted.append(future)
        return future

    timers._mirror.submit = remember                 # noqa: SLF001
    try:
        timers.start()
        while len([1 for flight, _ in readings if not flight]) < 4 * 4:
            world.wait(5)
            timers.tick()
            while submitted:
                submitted.pop().result()
            read(False)
    finally:
        timers.stop()
        store.close()
    first_end = next(index for index, (flight, reading) in enumerate(readings)
                     if not flight and reading["status"] == "stalled")
    after = readings[first_end:]
    in_flight = [reading for flight, reading in after if flight]
    assert len(in_flight) >= 16 and len(after) - len(in_flight) >= 4
    for flight, reading in after:
        assert reading["status"] == "stalled" and reading["not_ok_passes"] >= 1
        assert ("before it failed: RuntimeError: fixture" in reading["detail"]
                or "and the last failed: RuntimeError: fixture" in reading["detail"]
                if flight else reading["detail"].startswith("last pass failed: RuntimeError")), reading


# --- every sequence of passes, read at any instant ------------------------------------

#: How a full pass ends: `died` leaves the sidecar as it was at a checkpoint and
#: a new process takes over; `unwritten` is an end the sidecar could not take;
#: `read` and `read2` fail one, or two in a row, of the writers' reads of the
#: sidecar (counted from the pass's start), as EMFILE does.
ENDINGS = ("ok", "ok", "os", "runtime", "stop", "interrupt", "died", "unwritten", "read", "read2")
STEPS = st.tuples(
    st.sampled_from(["full", "full", "full", "hot", "dry", "locked", "elsewhere"]),
    st.sampled_from(ENDINGS),
    st.integers(min_value=1, max_value=8),          # the checkpoint the ending comes at
    st.integers(min_value=1, max_value=8),          # the checkpoint a reading is taken at
    st.sampled_from([0, 5, 40, 1900]),               # how long the pass has run by then
    st.sampled_from([2, 61, 61, 700]),               # how long before the pass starts
    st.sampled_from([0, 30, 700]),                   # how long after it ends a reading comes
    # The writers' read that fails: a full pass makes 9 to 11, its end the last;
    # the first ones are drawn more often, since only a read before this
    # process writes again meets a file it does not know.
    st.one_of(st.integers(min_value=1, max_value=2), st.integers(min_value=1, max_value=12)),
)


class Run:
    """The model: what the three must say, from what each call did."""

    def __init__(self) -> None:
        self.count, self.since, self.last = 0, None, None
        self.pending: str | None = None              # a start whose end was never recorded

    def begun(self) -> "Run":
        """The run as a pass that has just recorded its start finds it."""
        copy = Run()
        copy.count, copy.since, copy.last, copy.pending = (self.count, self.since, self.last,
                                                           self.pending)
        copy.begin()
        return copy

    def begin(self) -> None:
        if self.pending is not None:
            self.count += 1
            self.since = self.since or self.pending
            self.last = (mirror.UNFINISHED, self.pending)
            self.pending = None

    def end(self, state: str, start: str) -> None:
        if state == "ok":
            self.count, self.since = 0, None
        else:
            self.count, self.since = self.count + 1, self.since or start
        self.last = (state, start)

    def holds(self, data: dict) -> None:
        last = data.get("last_end")
        assert data.get("not_ok_passes") == self.count, (data, vars(self))
        assert data.get("not_ok_since") == self.since, (data, vars(self))
        assert ((last["state"], last["started_at"]) if last else None) == self.last, (data, vars(self))


def named(detail: str, count: int) -> bool:
    """Whether an in-flight detail names the run before it."""
    if count == 1:
        return "; the pass before it " in detail
    return f"; the {count} passes before it did not end ok, and the last " in detail


@settings(max_examples=120, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(interval=st.sampled_from([0, 1e-9]), steps=st.lists(STEPS, min_size=1, max_size=7))
def test_the_run_is_carried_across_every_sequence_of_passes(interval, steps, tmp_path_factory,
                                                          monkeypatch):
    """C-23.28: for any passes, full, hot, dry and without the lock, each
    ending ok, by OSError, by an exception no pass expects, by the daemon's
    stop, by an interrupt, by its process dying, or with an end the sidecar
    could not take, or with one or two of the writers' reads of the sidecar
    failing, also after another process wrote the sidecar (so this one does
    not know what it holds), with a flag-only hot pass serviced at every
    checkpoint or none,
    and read at any checkpoint of a pass and at any time after it: the three
    are the model's run, a start carries them and an end replaces them, a
    pass that left no record changes nothing, a reading in flight names the
    run, and the run changes a reading's status only as the rule says: a pass
    in flight after a pass that did not end ok is `stalled`. A clean full pass then
    clears the run."""
    with monkeypatch.context() as patch:
        world = World(tmp_path_factory.mktemp("carried"), patch, hot_interval=interval)
        for index in range(3):
            world.put(index, ONE)
        world.put(0, TWO)
        run = Run()
        for kind, ending, nth, look, ran, pause, after, which in steps:
            world.wait(pause)
            before = world.sidecar()
            start = fx.iso(world.clock)
            full = kind in ("full", "dry") or (kind == "hot" and not world.running._inventoried)
            real = full and kind != "dry"
            if kind == "elsewhere":                  # another process's write, same contents
                if world.running.sidecar_path.exists():
                    written_elsewhere(world)
                    assert world.sidecar() == before
                event("a write by another process")
                continue
            if kind == "locked":
                lock = world.running._lock()
                try:
                    assert world.full().error == "another pass holds the lock"
                finally:
                    lock.close()
                assert world.sidecar() == before
                event("a pass without the lock")
                continue
            begun = run.begun()
            seen, snapshot = [], []
            with pytest.MonkeyPatch.context() as faulty:
                checkpoint = mirror.Mirror._checkpoint
                passes = "full" if full else "hot"
                calls = [0]

                def hooked(engine, current, stage=None):
                    if current.kind == passes:
                        calls[0] += 1
                        if calls[0] == look:
                            world.wait(ran)
                            data = world.sidecar()
                            seen.append((data, by_the_rule(world, data)))
                        if calls[0] == nth:
                            if ending == "died" and real:
                                snapshot.append(world.running.sidecar_path.read_bytes())
                            elif ending == "os":
                                raise OSError(errno.EIO, "fixture")
                            elif ending == "runtime":
                                raise RuntimeError("fixture")
                            elif ending == "interrupt":
                                raise KeyboardInterrupt
                            elif ending == "stop":
                                world.cancel.set()
                    return checkpoint(engine, current, stage)

                faulty.setattr(mirror.Mirror, "_checkpoint", hooked)
                failed_reads, known_files = [], []
                if ending in ("read", "read2"):
                    failed_reads = unreadable(world, faulty,
                                              {which} if ending == "read" else {which, which + 1},
                                              known=known_files)
                if ending == "unwritten" and real:
                    record = mirror.Mirror._record

                    def start_only(self, current, **kwargs):
                        if current.state != "running":
                            raise OSError(errno.ENOSPC, "fixture")
                        return record(self, current, **kwargs)

                    faulty.setattr(mirror.Mirror, "_record", start_only)
                act = (lambda: world.full(dry_run=True)) if kind == "dry" else (
                    world.full if kind == "full" else world.hot)
                result, raised = attempt(act)
            world.cancel.clear()
            for data, reading in seen:
                ours = (data.get("pass") or {}).get("started_at") == start
                if real and ours:                    # this pass is the one in flight
                    assert data["pass"]["state"] == "running"
                    begun.holds(data)
                    assert named(reading["detail"], begun.count) == bool(begun.count), reading
                    event(f"a reading in flight ({reading['status']}) after "
                          + ("no pass that did not end ok" if not begun.count
                             else "one that did not" if begun.count == 1
                             else "several that did not")
                          + (", the last unfinished"
                             if begun.last and begun.last[0] == mirror.UNFINISHED else ""))
                else:                                # before this pass's start was written
                    assert carried(data) == carried(before)
                    if real:
                        event("a reading in flight before the pass could record its start")
            data = world.sidecar()
            if not real:
                assert carried(data) == carried(before), kind
                if kind == "dry":
                    assert data == before
                event(f"a {kind} pass")
            elif (data.get("pass") or {}).get("started_at") != start:
                assert data == before, "a pass that left no record changes nothing"
                event("a full pass that left no record")
            elif snapshot:
                world.running.sidecar_path.write_bytes(snapshot[0])
                world.running = mirror.Mirror(world.root, world.policy, now=lambda: world.clock,
                                              cancel=world.cancel)
                run.begin()
                run.pending = start
                data = world.sidecar()
                assert data["pass"]["state"] == "running"
                event("a full pass whose process died")
            elif ending == "unwritten":
                assert isinstance(raised, OSError) and data["pass"]["state"] == "running"
                run.begin()
                run.pending = start
                event("a full pass whose end was not written")
            else:
                state = (result.state if raised is None
                         else "error" if isinstance(raised, Exception) else "cancelled")
                assert data["pass"]["state"] == state and data["pass"]["started_at"] == start
                assert data["last_end"] == end_of(data["pass"])
                run.begin()
                run.end(state, start)
                event(f"a full pass that ended {state}"
                      + (" after a read of the sidecar failed" if failed_reads else ""))
                if known_files:
                    event("a failed read met a file this process "
                          + ("knew" if all(known_files) else "did not know"))
            if real:                                 # ended, or as it stood in flight
                run.holds(data)
            world.wait(after)
            reading = by_the_rule(world, world.sidecar())
            if (real and run.pending is None and run.count > 1
                    and data["pass"]["state"] in ("error", "cancelled")):
                assert f"; {run.count} passes in a row did not end ok; " in reading["detail"]
        world.wait(61)
        assert world.full().state == "ok"
        run.begin()
        run.end("ok", fx.iso(world.clock))
        run.holds(world.sidecar())
        assert world.health()["status"] == "healthy"


# --- any value in the three -----------------------------------------------------------

JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner,
                                                                max_size=3),
    max_leaves=8)
INSTANTS = st.sampled_from([None, "2026-10-13T12:26:40Z", "2026-10-13T11:00:00Z",
                            "2026-10-13T12:20:00Z", "not a time", 7])
RECORDS = st.fixed_dictionaries({
    "state": st.sampled_from(["running", "running", "ok", "error", "cancelled",
                              mirror.UNFINISHED, None, 3]),
    "started_at": INSTANTS, "finished_at": INSTANTS,
    "error": st.none() | st.text(max_size=12), "stage": st.none() | st.text(max_size=8)})
SIDECARS = st.fixed_dictionaries(
    {"pass": RECORDS, "updated_at": INSTANTS},
    optional={"last_end": st.one_of(JSON, RECORDS),
              "not_ok_passes": st.one_of(st.booleans(), st.integers(min_value=-3, max_value=5),
                                         JSON),
              "not_ok_since": st.one_of(JSON, INSTANTS), "last_ok_at": st.one_of(JSON, INSTANTS)})


def left_by_hand(state: str, count, started: Any = "2026-10-13T12:20:00Z") -> dict:
    """A sidecar whose `not_ok_passes` is `count`, beside an object `last_end`."""
    record = {"state": state, "started_at": started, "error": None,
              "finished_at": None if state == "running" else "2026-10-13T12:21:00Z",
              "stage": "reading entries"}
    return {"pass": record, "updated_at": "2026-10-13T12:20:00Z", "not_ok_passes": count,
            "last_end": {"state": "error", "started_at": "2026-10-13T12:19:00Z"},
            "not_ok_since": "2026-10-13T12:19:00Z"}


@settings(max_examples=400, deadline=None)
@given(data=SIDECARS)
# Values Hypothesis reaches only now and then, beside a record in flight and
# one that is over: a count that is a boolean, below zero, or not whole.
@example(data=left_by_hand("running", True))
@example(data=left_by_hand("error", True))
@example(data=left_by_hand("running", -1))
@example(data=left_by_hand("error", -1))
@example(data=left_by_hand("running", 2.0))
# A run beside a record that ended ok, and a start that is no string.
@example(data=left_by_hand("ok", 2))
@example(data=left_by_hand("running", 0, started=7))
def test_no_value_in_the_three_changes_a_status_or_raises(data):
    """C-23.28: a sidecar an older mirror or a hand edit left. Health reads
    by the rule (`stalled` for a pass in flight after a run that is one,
    otherwise the status it reads without the three), gives a reply whose
    three are sane, and the run a new pass would start from is sane and held
    to the record: `last_end` is the record once it is over, and the run
    counts it exactly when it did not end ok; a record in flight is
    unfinished."""
    engine = mirror.Mirror("/nonexistent-state-root", fx.policy(),
                           now=lambda: mirror._instant("2026-10-13T12:26:40Z"))
    with_them = engine._full_health(data)
    without = engine._full_health(stripped(data))
    flight = mirror._in_flight(data["pass"])
    assert with_them["status"] == ("stalled" if flight and run_of(data) else without["status"])
    assert with_them["detail"].startswith(without["detail"])
    count = with_them["not_ok_passes"]
    assert isinstance(count, int) and not isinstance(count, bool) and count >= 0
    assert with_them["last_end"] is None or isinstance(with_them["last_end"], dict)
    assert with_them["not_ok_since"] is None or isinstance(with_them["not_ok_since"], str)
    json.dumps(with_them)
    last, count, since = mirror._over(data)
    assert isinstance(count, int) and not isinstance(count, bool) and count >= 0
    assert since is None or isinstance(since, str)
    assert count or since is None
    record = data["pass"]
    if mirror._in_flight(record):
        assert last["state"] == mirror.UNFINISHED and count >= 1
        event("in flight")
    else:
        assert last == end_of(record)
        assert (count >= 1) == (record["state"] in ("error", "cancelled"))
        event("over")


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(left=st.fixed_dictionaries({}, optional={
    "last_end": JSON, "not_ok_passes": JSON, "not_ok_since": JSON}))
def test_a_pass_over_any_value_in_the_three_records_a_sane_run(left, tmp_path_factory,
                                                               monkeypatch):
    """C-23.28: whatever a hand edit left in the three, a full pass records
    its start and its end, and the run it writes is sane."""
    with monkeypatch.context() as patch:
        world = World(tmp_path_factory.mktemp("left"), patch)
        world.put(0, ONE)
        failed = fail_once(world, patch, RuntimeError("before the edit"))
        path = world.running.sidecar_path
        path.write_text(json.dumps({**stripped(json.loads(path.read_text())), **left}))
        world.wait(60)
        with patch.context() as hooked:
            seen = during(world, hooked, 2, lambda _current: world.sidecar())
            assert world.full().state == "ok"
        assert seen[0]["not_ok_passes"] >= 1 and seen[0]["last_end"] == end_of(failed)
        data = world.sidecar()
        assert carried(data) == {"last_end": end_of(data["pass"]), "not_ok_passes": 0,
                                 "not_ok_since": None}
