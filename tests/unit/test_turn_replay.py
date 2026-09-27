"""What a runner adopted after a restart rebuilds of a turn (C-24.7, C-25.3, C-26.6, D-13).

A runner a later daemon adopts for an attempt replays the turn from its stdout and
the relay's log. A person's stop of a delivered turn was not replayed: the adopted
runner sent no interrupt and read the turn's end as its own failure
(`ended-without-result`), where the first daemon settles the same turn as the
person's stop. Found in the review of the close() stack (585ea41..4d3d3ea): close()
stops the runners, and a `turn.interrupt` still running on the requests pool records
its stop after that.

A real relay on a socket, the real service and runner; the provider is simulated by
appending to the attempt's stdout and publishing exit.json. Each case compares what a
turn settles as when the next daemon adopts it with what the first daemon settles.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from subfleet.conversations import runner as runner_module
from subfleet.conversations.launch import TURN_MANIFEST_KEY
from subfleet.conversations.service import ConversationService
from subfleet.relay import RelayServer, read_log
from tests.unit.test_conversation_service import (INIT_OK, SETTINGS, Clock, FakeDaemon, conversation, submit,
                                                  turn_attempt)

RECEIPT = json.dumps({"type": "control_response", "response": {"subtype": "success",
                                                               "request_id": "subfleet-interrupt", "response": {}}})
ABORTED = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                      "errors": []})


def until(pred, what, timeout=60):
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, f"timed out waiting for {what}"
        time.sleep(0.02)


class World:
    def __init__(self, tmp: Path, short: Path):
        self.root, self.ws = tmp / "state", tmp / "work"
        self.root.mkdir()
        self.ws.mkdir()
        self.svc = self.service()
        cid = conversation(self.svc)
        self.mid = submit(self.svc, cid)
        self.svc.store.set_state(self.mid, "waiting")
        self.aid = turn_attempt(self.svc, self.mid, state="running", n=0)
        job = self.root / "jobs" / self.aid.split("/")[0]
        self.adir = job / "a1"
        self.adir.mkdir(parents=True)
        sock = short / "r.sock"
        self.relay = RelayServer(sock, self.adir / "stdin.jsonl")
        self.relay.bind()
        self.pipe_r, pipe_w = os.pipe()
        os.set_blocking(self.pipe_r, False)
        self.relay.serve(pipe_w)
        (self.adir / "start.json").write_text(json.dumps({"control_socket": str(sock)}))
        (self.adir / "stdout").write_bytes(b"")
        turn = {"provider": "claude", "conversation_id": cid, "message_id": self.mid, "text": "hello",
                "settings": SETTINGS, "cwd": str(self.ws), "new_session_id": str(uuid.uuid4())}
        (job / "manifest.json").write_text(json.dumps({TURN_MANIFEST_KEY: turn}))

    def service(self):
        daemon = FakeDaemon(self.root)
        daemon.policy["conversations"]["catalog_interval_s"] = 0
        svc = ConversationService(daemon)
        svc.clock = Clock()
        svc.test_workspace = str(self.ws)
        return svc

    def say(self, *lines):
        with open(self.adir / "stdout", "ab") as out:
            out.write(b"".join(line.encode() + b"\n" for line in lines))

    def logged(self):
        return [r["tag"] for r in read_log(self.adir / "stdin.jsonl")]

    def runner(self):
        self.svc._adopt_runners()
        return self.svc.runners[self.aid]

    def deliver(self, runner):
        until(lambda: self.logged() == ["init"], "the init frame")
        self.say(INIT_OK)
        until(lambda: self.logged() == ["init", "user-message"], "the message frame")
        self.say(json.dumps({"type": "command_lifecycle", "command_uuid": self.mid, "state": "started"}))
        until(lambda: runner.driver.accepted and self.svc.store.message(self.mid)["state"] == "running", "acceptance")

    def restart(self):
        self.svc.close()
        self.svc.daemon.store.close()
        self.svc = self.service()

    def settle(self, runner, *, result: bool):
        if result:
            until(lambda: "close" in self.logged(), "the driver's close after the result")
        (self.adir / "exit.json").write_text(json.dumps({"rc": 0}))
        assert runner.join(60)
        message = self.svc.store.message(self.mid)
        return message["state"], message["state_reason"]

    def close(self):
        self.svc.close()
        self.svc.daemon.store.close()
        self.relay.stop()
        os.close(self.pipe_r)


@pytest.fixture
def world(tmp_path):
    short = Path(tempfile.mkdtemp(prefix="sfsr-", dir="/tmp"))
    made = World(tmp_path, short)
    yield made
    made.close()
    shutil.rmtree(short, ignore_errors=True)


@pytest.mark.parametrize("result", [False, True], ids=["eof", "aborted-result"])
@pytest.mark.parametrize("restart", [False, True], ids=["same-daemon", "restart"])
def test_a_stop_whose_interrupt_was_written_settles_stopped_across_a_restart(world, restart, result):
    """The first daemon sent the provider's interrupt; the turn then ends (at EOF, or
    with the aborted `result`) before or after a restart. Either way it is the
    person's stop, and the interrupt is written once: a runner adopted after the
    restart reads the written interrupt from the relay's log."""
    runner = world.runner()
    world.deliver(runner)
    world.svc.op_turn_interrupt({"message_id": world.mid}, None)
    until(lambda: "interrupt" in world.logged(), "the interrupt frame")
    if restart:
        world.restart()
    if result:
        world.say(RECEIPT, ABORTED)
    if restart:
        runner = world.runner()
        until(lambda: runner.handshaken and runner.driver.accepted, "the replay")
    assert world.settle(runner, result=result) == ("interrupted", "stopped")
    assert world.logged().count("interrupt") == 1


@pytest.mark.parametrize("chunk", [None, 16], ids=["whole", "in-16-byte-reads"])
def test_a_stop_recorded_after_close_stopped_the_runner_is_sent_by_the_next_daemon(world, monkeypatch, chunk):
    """`turn.interrupt` ran after close() had stopped the runner (the request pool is
    drained after the service): the next daemon sends the provider's interrupt at once
    (well before the 10 s SIGINT) and the turn settles as the person's stop. The
    interrupt waits until the replay has read the stdout there is: read a few bytes
    at a time, a driver still initializing would have ended the delivered turn as
    stopped before sending, and sent nothing."""
    if chunk:
        monkeypatch.setattr(runner_module, "READ_CHUNK", chunk)
    runner = world.runner()
    world.deliver(runner)
    runner.stop()
    assert runner.join(10)
    assert world.svc.op_turn_interrupt({"message_id": world.mid}, None)["stop_requested"]
    assert "interrupt" not in world.logged()
    world.restart()
    runner = world.runner()
    until(lambda: "interrupt" in world.logged(), "the next daemon's interrupt", timeout=5)
    assert world.settle(runner, result=False) == ("interrupted", "stopped")
    assert world.logged().count("interrupt") == 1
