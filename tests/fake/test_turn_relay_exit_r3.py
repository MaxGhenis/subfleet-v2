"""Review #271 r3: changing stdout files and exit-observation ordering."""

import errno
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from subfleet.conversations import runner as runner_module
from subfleet.conversations.reconcile import Evidence, settle
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
    WNOWAIT lets this test prove that exit without reaping or updating Popen.
    """
    r = recovery
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
                        exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                        if exited is not None:
                            observed.append(exited)
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
