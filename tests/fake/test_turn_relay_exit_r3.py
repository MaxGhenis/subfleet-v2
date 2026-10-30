"""Review #271 r3: changing stdout files and exit-observation ordering."""

import errno
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from subfleet import relay as relay_module
from subfleet.conversations import runner as runner_module
from subfleet.conversations.reconcile import Evidence, settle
from subfleet.conversations.store import ConversationError
from tests.fake.test_turn_relay_exit_recovery import recovery  # noqa: F401
from tests.fake.test_turn_relay_exit_review import rebuild


@pytest.mark.parametrize("shape", ["shrunk", "replaced", "zero", "missing"])
def test_changed_stdout_cannot_leave_the_drain_polling_forever(recovery, monkeypatch, shape):
    r = recovery
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()
    path = r.adir / "stdout"
    old_position = r.runner.offset + len(r.runner.partial)
    if shape in ("zero", "missing"):
        path.unlink()
        if shape == "zero":
            path.touch()
    else:
        with path.open("ab") as stream:
            stream.write(b" " * 4096)
        read = r.runner._read_stdout
        changed = False

        def change_then_read():
            nonlocal changed
            if not changed:
                changed = True
                if shape == "shrunk":
                    path.write_bytes(b"")
                else:
                    replacement = r.adir / "replacement"
                    replacement.write_bytes(b"{}\n")
                    replacement.replace(path)
            return read()

        monkeypatch.setattr(r.runner, "_read_stdout", change_then_read)
    r.runner._drain_stdout()
    assert r.runner.offset + len(r.runner.partial) == old_position
    assert not r.contained
    assert (r.runner.drain_caught_up_at is not None) == (shape in ("zero", "missing"))


def test_zero_entry_size_arms_the_bound_for_a_real_growing_file(recovery, monkeypatch):
    r = recovery
    r.runner._relay_lost("SECRET7 exit proof", child_exited=True)
    path = r.adir / "stdout"
    path.write_bytes(b"")
    read = r.runner._read_stdout

    def write_then_read():
        with path.open("ab") as stream:
            stream.write(b"{}\n")
        r.now[0] += 1
        return read()

    monkeypatch.setattr(r.runner, "_read_stdout", write_then_read)
    started = r.now[0]
    r.runner._drain_stdout()
    assert r.runner.drain_entry_size == 0
    assert r.runner.drain_caught_up_at == started
    assert r.now[0] - started == runner_module.EXIT_DRAIN_S
    assert r.contained == [r.runner.attempt_id]


@pytest.mark.parametrize("boundary", ["proof", "owner-ended"])
def test_a_transient_entry_stat_failure_still_settles_the_exited_turn(recovery, monkeypatch, boundary):
    r = recovery
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()
    runner = r.runner if boundary == "proof" else rebuild(r, ended=True)
    stat = Path.stat
    failed = []

    def transient_stat(path, *args, **kwargs):
        if path == r.adir / "stdout" and not failed:
            failed.append(True)
            raise OSError(errno.EIO, "SECRET7 transient stdout metadata failure")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", transient_stat)
    runner._run()
    assert failed == [True]
    assert runner.finished.is_set()
    assert r.reported == [runner], {"reported": len(r.reported), "contained": r.contained,
                                  "outcome": runner.driver.outcome}


def test_exit_visibility_after_the_refusal_can_still_settle_as_relay_failed(recovery, monkeypatch):
    """A pipe can close before the two poll observations see exit.

    This is a characterization of the remaining ordering, not a request to infer
    exit from a broken pipe: the live-child control must remain a relay failure.
    """
    r = recovery
    original = r.exchange

    def exchange(payload, **kwargs):
        frame = json.loads(payload)
        answer = original(payload, **kwargs)
        if frame.get("tag") == "user-message":
            # The fixture closed the real pipe reader. Exit is not observable
            # yet, as between descriptor teardown and a waitable process exit.
            r.child.returncode = None
        return answer

    monkeypatch.setattr(r.runner.relay, "_exchange", exchange)
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()
    assert r.refusals[-1]["error"] == "failed"
    assert "child_exited" not in r.refusals[-1]
    assert r.runner.relay_failed and r.runner.stop_reason == "relay-failed"
    r.child.returncode = 1
    (r.adir / "exit.json").write_text('{"rc":1}')
    r.runner._run()
    turn = json.loads((r.adir / "turn.json").read_text())
    outcome = settle(turn, provider="claude", turn_seq=1, gather=lambda: Evidence(
        acknowledged=True, native="found", frame="written", process_gone=True))
    assert r.reported == [r.runner]
    assert outcome.reason == "relay-failed"


def test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock(
        recovery, monkeypatch):
    """The guardian waits on Popen in one thread and polls it in the relay thread.

    POSIX Popen.wait holds _waitpid_lock across waitpid. While it is held, poll
    returns None without looking in the kernel, even for an already exited child.
    The relay's exit helper peeks at the kernel with WNOWAIT (fix r4), so this test
    proves that exit through it, on both CI Pythons (3.12 has no os.waitid), without
    reaping or updating Popen.
    """
    r = recovery
    exited_by, peek = relay_module.child_exited, relay_module._exit_peek
    fake = Path(__file__).resolve().parents[2] / "tests" / "bin" / "claude"
    fake_home = r.adir / "fake-home"
    fake_home.mkdir()
    r.server._close_pipe()
    reader, writer = os.pipe()
    r.server._pipe = writer
    observed, statuses, refusals = [], [], []
    pause = threading.Event()
    deadline = time.monotonic() + 60
    with (r.adir / "stdout").open("wb") as stdout, (r.adir / "stderr").open("wb") as stderr:
        try:
            child = subprocess.Popen(
                [sys.executable, str(fake), "--input-format", "stream-json", "--model", "opus[1m]",
                 "--session-id", r.runner.spec.new_session_id],
                stdin=reader, stdout=stdout, stderr=stderr, cwd=r.adir,
                env={"HOME": str(fake_home), "PYTHONDONTWRITEBYTECODE": "1",
                     "CLAUDE_FAKE_PROJECTS_DIR": str(r.adir / "projects")})
        finally:
            os.close(reader)
        r.server._child = child
        # This is the lock the guardian's existing blocking child.wait holds.
        child._waitpid_lock.acquire()
        try:
            def exchange(payload, *, before_send=None):
                if before_send:
                    before_send()
                frame = json.loads(payload)
                answer = r.server.apply(frame)
                if frame.get("op") == "status":
                    statuses.append(answer["status"]["child"])
                if frame.get("tag") == "user-message":
                    assert answer["ok"]
                    while True:
                        if exited_by(child):
                            observed.append(peek(child.pid))
                            break
                        assert time.monotonic() < deadline, "SECRET7 fake did not exit after its ack"
                        pause.wait(0.01)
                if answer.get("error") in ("failed", "closed"):
                    refusals.append(answer)
                return answer

            monkeypatch.setattr(r.runner.relay, "_exchange", exchange)
            r.runner._apply(r.runner.driver.start())
            while not observed:
                r.runner._read_stdout()
                assert time.monotonic() < deadline, "SECRET7 fake did not initialize"
                pause.wait(0.01)
            assert observed[0].si_pid == child.pid and observed[0].si_status == 1
        finally:
            child._waitpid_lock.release()
            if child.poll() is None:
                child.terminate()
            try:
                rc = child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                rc = child.wait(timeout=10)
    (r.adir / "exit.json").write_text(json.dumps({"rc": rc}))
    r.runner._run()
    turn = json.loads((r.adir / "turn.json").read_text())
    outcome = settle(turn, provider="claude", turn_seq=1, gather=lambda: Evidence(
        acknowledged=True, native="found", frame="written", process_gone=True))
    assert r.reported == [r.runner] and r.runner.driver.accepted
    assert outcome.reason == "ended-without-result", {
        "reason": outcome.reason, "poll_statuses": statuses, "exit_status": observed[0].si_status,
        "refusals": refusals, "stop_reason": r.runner.stop_reason}


# Fix r3 (hub decision 2): a failed entry stat never abandons the turn. A recoverable
# OSError is tried again up to STDOUT_STAT_RETRIES times, POLL_S apart; a size still
# unknown is never zero, and the drain catches up once a read reaches the end of the
# append-only file.
def _stdout_stat_fails(r, monkeypatch, failures):
    stat, calls = Path.stat, []

    def failing(path, *args, **kwargs):
        if path == r.adir / "stdout":
            calls.append(True)
            if len(calls) <= failures:
                raise OSError(errno.EIO, "SECRET7 stdout metadata failure")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing)
    return calls


@pytest.mark.parametrize("failures", [0, 3, 4, "missing"])
def test_an_entry_stat_is_tried_again_at_most_three_times(recovery, monkeypatch, failures):
    r = recovery
    if failures == "missing":
        (r.adir / "stdout").unlink()
    on_disk = 0 if failures == "missing" else (r.adir / "stdout").stat().st_size
    calls = _stdout_stat_fails(r, monkeypatch, 0 if failures == "missing" else failures)
    size = r.runner._stdout_size()
    if failures == "missing":
        assert (size, len(calls), r.polls) == (0, 1, [])          # absent: empty, as before
    elif failures == 4:
        assert (size, len(calls), len(r.polls)) == (None, 4, 3)   # unknown, never zero
    else:
        assert (size, len(calls), len(r.polls)) == (on_disk, failures + 1, failures)


def _backlog(r):
    rows = [{"type": "assistant", "message": {"id": "answer", "content": [
        {"type": "text", "text": f"[part-{index}]"}]}} for index in range(100)]
    rows.append({"type": "result", "subtype": "success", "is_error": False, "result": "[done]"})
    with (r.adir / "stdout").open("a") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    return (r.adir / "stdout").stat().st_size


@pytest.mark.parametrize("boundary", ["proof", "owner-ended"])
def test_every_entry_stat_failing_still_reads_the_whole_backlog_and_settles(recovery, monkeypatch, boundary):
    r = recovery
    r.runner._apply(r.runner.driver.start())
    assert r.runner._read_stdout()  # a real refused settings frame proves exit
    size = _backlog(r)               # on disk at the drain's entry, no writer left
    runner = r.runner if boundary == "proof" else rebuild(r, ended=True)
    calls = _stdout_stat_fails(r, monkeypatch, failures=1 << 30)
    monkeypatch.setattr(runner_module, "READ_CHUNK", 256)
    read = runner._read_stdout

    def slow_read():
        r.now[0] += 1  # one fake second per chunk: the backlog takes longer than the bound
        return read()

    monkeypatch.setattr(runner, "_read_stdout", slow_read)
    runner._run()
    assert len(calls) == runner_module.STDOUT_STAT_RETRIES + 1
    assert runner.drain_entry_size is None and runner.drain_caught_up_at is not None
    assert runner.offset == size, {"read": runner.offset, "size": size, "stop_reason": runner.stop_reason}
    assert r.reported == [runner] and not r.contained
    assert runner.driver.outcome.reason is None  # the provider's result decides


def test_an_unknown_entry_size_still_bounds_a_writer_that_never_pauses(recovery, monkeypatch):
    """No read of a descendant's continuous output comes back empty, so the
    first empty read alone would never start the bound. A read short of a whole
    chunk reached the end of the file, which holds all that was on disk at entry."""
    r = recovery
    r.runner._relay_lost("SECRET7 exit proof", child_exited=True)
    on_disk = (r.adir / "stdout").stat().st_size       # the initialize answer, unread
    _stdout_stat_fails(r, monkeypatch, failures=1 << 30)
    read, reads = r.runner._read_stdout, []

    def write_then_read():
        reads.append(True)
        assert len(reads) < 4 * runner_module.EXIT_DRAIN_S, "SECRET7 the drain never started its bound"
        with (r.adir / "stdout").open("ab") as stream:
            stream.write(b"{}\n")
        r.now[0] += 1
        return read()

    monkeypatch.setattr(r.runner, "_read_stdout", write_then_read)
    r.runner._drain_stdout()
    assert r.runner.drain_entry_size is None
    assert r.runner.offset >= on_disk
    assert r.runner.drain_caught_up_at == r.now[0] - runner_module.EXIT_DRAIN_S
    assert r.contained == [r.runner.attempt_id]


# Fix r3 (hub decision 3): a runner that ends on an unexpected exception still
# settles its turn, with the reason an unproved loss gets, and logs the exception.
def _failing_read(runner, monkeypatch, *, after, error=None):
    read, calls = runner._read_stdout, []

    def failing():
        calls.append(True)
        if len(calls) > after:
            raise error or OSError(errno.EIO, "SECRET7 stdout read failure")
        return read()

    monkeypatch.setattr(runner, "_read_stdout", failing)
    return calls


def _settled(r):
    turn = json.loads((r.adir / "turn.json").read_text())
    return turn, settle(turn, provider="claude", turn_seq=1, gather=lambda: Evidence(
        acknowledged=True, native="found", frame="written", process_gone=True))


@pytest.mark.parametrize("where", ["live", "proof", "receipt", "owner-ended"])
def test_a_runner_that_fails_still_settles_its_turn(recovery, monkeypatch, caplog, where):
    r = recovery
    runner = r.runner
    if where == "live":
        # The real relay, and a child that neither exits nor closes its stdin.
        monkeypatch.setattr(runner.relay, "_exchange", lambda payload, **kwargs: r.server.apply(json.loads(payload)))
        _failing_read(runner, monkeypatch, after=1)
    elif where == "proof":
        _failing_read(runner, monkeypatch, after=1)   # the drain's first read, after the refusal proves exit
    else:
        runner._apply(runner.driver.start())
        assert runner._read_stdout()
        if where == "receipt":
            (r.adir / "exit.json").write_text('{"rc":1}')
        runner = rebuild(r, ended=where == "owner-ended")
        monkeypatch.setattr(runner.relay, "_exchange", r.exchange)
        _failing_read(runner, monkeypatch, after=0)
    runner.log = logging.getLogger("tests.fix-r3")
    with caplog.at_level(logging.WARNING, logger="tests.fix-r3"):
        runner._run()
    assert runner.finished.is_set() and r.reported == [runner]
    assert r.contained == ([runner.attempt_id] if where in ("live", "proof") else [])
    turn, outcome = _settled(r)
    assert (turn["state"], turn["stop_reason"], turn["relay_failed"]) == ("failed", "relay-failed", True)
    assert outcome.reason == "relay-failed"
    assert "turn runner job/a1 failed: OSError" in caplog.text
    assert "turn job/a1 settles as an unproved loss" in caplog.text


@pytest.mark.parametrize("why", ["stopping", "store-closed"])
def test_a_runner_failing_while_its_service_closes_is_left_for_replay(recovery, monkeypatch, why):
    r = recovery
    error = (ConversationError("store-closed", "the conversation store is closed", code=1)
             if why == "store-closed" else None)
    _failing_read(r.runner, monkeypatch, after=1, error=error)
    if why == "stopping":
        monkeypatch.setattr(r.runner, "_drain_stdout", lambda: (r.runner.stop(), r.runner._read_stdout()))
    r.runner._run()
    assert r.runner.finished.is_set()
    assert r.reported == [] and r.contained == []      # a later daemon adopts or replays it (C-26.6)
    assert not r.runner.outcome_reported


def test_a_driver_that_fails_at_eof_still_settles_as_an_unproved_loss(recovery, monkeypatch):
    r = recovery
    _failing_read(r.runner, monkeypatch, after=2)     # the drain reads the acknowledgement, then fails

    def eof(offset):
        raise RuntimeError("SECRET7 driver defect at eof")

    monkeypatch.setattr(r.runner.driver, "eof", eof)
    r.runner._run()
    assert r.reported == [r.runner] and r.contained == [r.runner.attempt_id]
    turn, outcome = _settled(r)
    assert (turn["state"], turn["reason"], turn["ended_by"], turn["accepted"]) == (
        "failed", "ended-without-result", "eof", True)
    assert outcome.reason == "relay-failed"


def test_a_result_read_before_the_runner_fails_still_decides_the_turn(recovery, monkeypatch):
    r = recovery

    def exchange(payload, **kwargs):
        # The real relay; the child acknowledges the message and answers it, and
        # stays alive (its background work, C-26.5).
        frame = json.loads(payload)
        answer = r.server.apply(frame)
        if frame.get("tag") == "user-message":
            with (r.adir / "stdout").open("a") as stream:
                stream.write(json.dumps({"type": "command_lifecycle", "command_uuid": r.runner.message_id,
                                         "state": "started"}) + "\n")
                stream.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                         "result": "[done]"}) + "\n")
        return answer

    monkeypatch.setattr(r.runner.relay, "_exchange", exchange)
    _failing_read(r.runner, monkeypatch, after=2)     # the read after the result fails
    r.runner._run()
    assert r.reported == [r.runner]
    turn, outcome = _settled(r)
    assert (turn["state"], turn["stop_reason"], outcome.state) == ("complete", None, "complete")
    assert r.contained == [r.runner.attempt_id]       # no runner is left to bound its background work
