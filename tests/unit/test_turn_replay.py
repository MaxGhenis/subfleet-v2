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


# --- a message whose attempt ended with no runner to settle it -----------------------------

LIFECYCLE = '{"type": "command_lifecycle", "command_uuid": "<mid>", "state": "started"}'
SUCCESS = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"})
WRITTEN = [{"kind": "intent", "seq": 1, "tag": "init", "op": "write"}, {"kind": "written", "seq": 1},
           {"kind": "intent", "seq": 2, "tag": "user-message", "op": "write"}, {"kind": "written", "seq": 2}]


class Ended:
    """A delivered turn whose runner no relay answers (the provider is simulated by its
    stdout), in a state root a first and a next service share, as a daemon and the one
    after its restart do."""

    def __init__(self, tmp: Path, monkeypatch, stdout: list[str], logged: list[dict] = WRITTEN):
        monkeypatch.setattr(runner_module, "RESEND_MAX", 10**6)   # no relay listens: not "relay-failed"
        self.root, self.ws = tmp / "state", tmp / "work"
        self.root.mkdir()
        self.ws.mkdir()
        self.svc = self.service()
        self.cid = conversation(self.svc)
        self.mid = submit(self.svc, self.cid)
        self.svc.store.set_state(self.mid, "waiting")
        self.aid = turn_attempt(self.svc, self.mid, state="running", n=0)
        self.job_id = self.aid.split("/")[0]
        job = self.root / "jobs" / self.job_id
        self.adir = job / "a1"
        self.adir.mkdir(parents=True)
        (self.adir / "start.json").write_text(json.dumps({"control_socket": str(tmp / "none.sock")}))
        (tmp / "projects").mkdir()                          # where settlement looks for the session's transcript
        (self.adir / "launch.json").write_text(json.dumps({"notes": {"projects_dir": str(tmp / "projects")}}))
        lines = [line.replace("<mid>", self.mid) for line in stdout]
        (self.adir / "stdout").write_text("".join(line + "\n" for line in lines))
        (self.adir / "stdin.jsonl").write_text("".join(json.dumps(r) + "\n" for r in logged))
        turn = {"provider": "claude", "conversation_id": self.cid, "message_id": self.mid, "text": "hello",
                "settings": SETTINGS, "cwd": str(self.ws), "new_session_id": str(uuid.uuid4())}
        (job / "manifest.json").write_text(json.dumps({TURN_MANIFEST_KEY: turn}))

    def service(self) -> ConversationService:
        daemon = FakeDaemon(self.root)
        daemon.policy["conversations"]["catalog_interval_s"] = 0
        svc = ConversationService(daemon)
        svc.clock = Clock()
        svc.test_workspace = str(self.ws)
        return svc

    def restart(self):
        self.svc.close()
        self.svc.daemon.store.close()
        self.svc = self.service()

    def end_attempt(self, state: str = "succeeded"):
        """What the next daemon's `_finalize` leaves, before its conversation tick."""
        with self.svc.daemon.store.transaction("attempt.ended", job_id=self.job_id, attempt_id=self.aid) as tx:
            tx.execute("UPDATE attempts SET state=? WHERE attempt_id=?", (state, self.aid))
            tx.execute("UPDATE jobs SET state=? WHERE job_id=?", ("lost" if state == "lost" else state, self.job_id))

    def ticks(self, n: int = 3):
        for _ in range(n):
            self.svc.tick()
            for runner in list(self.svc.runners.values()):
                assert runner.join(60)
            self.svc.clock.now += 60

    def message(self) -> tuple:
        message = self.svc.store.message(self.mid)
        return message["state"], message["state_reason"]

    def close(self):
        self.svc.close()
        self.svc.daemon.store.close()


@pytest.fixture
def ended(tmp_path, monkeypatch):
    made = []

    def make(stdout, logged=WRITTEN):
        made.append(Ended(tmp_path, monkeypatch, stdout, logged))
        return made[-1]
    yield make
    for world in made:
        world.close()


def test_a_turn_close_stopped_before_its_report_is_settled_after_the_next_daemon_ends_its_attempt(ended, caplog):
    """C-25.3, C-26.6 (review of 585ea41..4d3d3ea): close() stopped a runner whose turn
    had reached its outcome (turn.json written, stdin's close queued) before the exit
    receipt came, so it never reported. The next daemon finalized the attempt from its
    exit receipt before its first conversation tick, and only attempts not yet ended
    were adopted: the message stayed running for good, its follow-up never dispatched,
    and neither a stop nor a cancel could move it. The ended attempt is now replayed,
    and the message settles; close() says it left one unreported."""
    import logging
    world = ended([INIT_OK, LIFECYCLE, SUCCESS])
    world.svc._adopt_runners()
    first = world.svc.runners[world.aid]
    until(lambda: (world.adir / "turn.json").exists(), "the first runner's outcome")
    with caplog.at_level(logging.WARNING, logger="test-conversations"):
        world.restart()
    assert first.finished.is_set() and not first.outcome_reported
    assert any("outcome unreported" in r.getMessage() and world.mid in r.getMessage() for r in caplog.records)
    (world.adir / "exit.json").write_text(json.dumps({"rc": 0}))
    world.end_attempt()
    follow = submit(world.svc, world.cid, text="next", after=world.mid)
    world.ticks()
    assert world.message() == ("complete", None)
    assert world.svc.store.message(follow)["state"] != "queued"         # dispatched once the turn settled


def test_an_attempt_that_ended_with_no_exit_receipt_is_settled_from_its_stdout(ended):
    """C-26.6: an attempt the daemon ended as lost (its guardian gone) leaves no exit
    receipt; the replay takes its provider as gone and settles what stdout shows."""
    world = ended([INIT_OK, LIFECYCLE])
    world.end_attempt("lost")
    world.ticks()
    assert world.message() == ("failed", "ended-without-result")
    assert world.svc.store.conversation(world.cid)["blocked_by"] == "unfinished-turn"


def test_a_replay_reads_all_of_stdout_before_it_ends_the_turn(ended):
    """C-26.6: a runner that found its provider gone read one more chunk (1 MiB) of
    stdout and ended the turn there. A runner adopted after a restart is as far behind
    as the turn was long: a turn with more than 2 MiB before its result ended as one
    with no result. It reads all of stdout first."""
    padding = json.dumps({"type": "system", "subtype": "padding", "text": "x" * 4000})
    world = ended([INIT_OK, LIFECYCLE, *([padding] * 800), SUCCESS])
    assert (world.adir / "stdout").stat().st_size > 3 * runner_module.READ_CHUNK
    (world.adir / "exit.json").write_text(json.dumps({"rc": 0}))
    with world.svc.daemon.store.transaction("attempt.finalizing", job_id=world.job_id, attempt_id=world.aid) as tx:
        tx.execute("UPDATE attempts SET state='finalizing' WHERE attempt_id=?", (world.aid,))
    world.ticks(1)
    assert world.message() == ("complete", None)


@pytest.mark.parametrize("cut_at", ["the block", "the message's state"])
def test_a_settlement_cut_short_leaves_the_message_live_for_the_replay(ended, monkeypatch, cut_at):
    """C-24.8, C-25.3: a turn that ended without a result blocks its conversation
    (`unfinished-turn`). The message was settled first and the block written after, so
    close() refusing the second write (the runner past close()'s wait) left the message
    failed with its conversation open, and the next daemon dispatched the follow-up
    into a session left mid-turn. The block is written first: a cut leaves the message
    live, and the next daemon's replay settles it whole, before any follow-up."""
    from subfleet.conversations.store import ConversationError
    world = ended([INIT_OK, LIFECYCLE])
    (world.adir / "exit.json").write_text(json.dumps({"rc": 1}))
    closed = ConversationError("store-closed", "the conversation store is closed", code=1)
    if cut_at == "the block":
        real_update = world.svc.store.update_conversation

        def cut(conversation_id, **fields):
            if "blocked_by" in fields:
                raise closed
            return real_update(conversation_id, **fields)
        monkeypatch.setattr(world.svc.store, "update_conversation", cut)
    else:
        real_set = world.svc.store.set_state

        def cut(message_id, state, **kwargs):
            if message_id == world.mid and state == "failed":
                raise closed
            return real_set(message_id, state, **kwargs)
        monkeypatch.setattr(world.svc.store, "set_state", cut)
    world.svc._adopt_runners()
    assert world.svc.runners[world.aid].join(60)
    assert world.message()[0] in ("starting", "running")                # live, not failed with no block
    world.restart()
    world.end_attempt("failed")
    follow = submit(world.svc, world.cid, text="next", after=world.mid)
    world.ticks()
    assert world.message() == ("failed", "ended-without-result")
    assert world.svc.store.conversation(world.cid)["blocked_by"] == "unfinished-turn"
    assert world.svc.store.message(follow)["state"] == "queued"
    assert world.svc.daemon.submits == [] or all(a.request_id.split(":")[1] != follow
                                                 for a in world.svc.daemon.submits)


def test_an_ended_attempt_is_replayed_once(ended, monkeypatch):
    """A replay that cannot settle its message (here its runner fails at once) is not
    started again on every tick; a later daemon tries again."""
    world = ended([INIT_OK, LIFECYCLE])
    world.end_attempt("lost")
    built = []
    real = runner_module.TurnRunner._read_stdout

    def failing(self):
        built.append(self.attempt_id)
        raise RuntimeError("a runner defect")          # logged; the runner ends without its report

    monkeypatch.setattr(runner_module.TurnRunner, "_read_stdout", failing)
    world.ticks(3)
    assert built == [world.aid]
    monkeypatch.setattr(runner_module.TurnRunner, "_read_stdout", real)
    world.restart()
    world.ticks(1)
    assert world.message() == ("failed", "ended-without-result")


@pytest.mark.parametrize("path", ["same-daemon", "restart-live", "restart-ended"])
def test_a_withheld_turn_is_readmitted_however_its_attempt_is_settled(ended, path):
    """C-26.6, C-30.4 (review of the branch): a turn the legacy hold withheld ends
    stopped before its message was sent, and its message is readmitted. A replay of
    its ended attempt had skipped the withhold and derived the outcome again from
    stdout (an initialize answer, then EOF: no result), overwriting the recorded one,
    so the message failed and was never carried again. A replay reads the hold, and
    keeps the outcome an earlier runner recorded."""
    world = ended([INIT_OK], logged=WRITTEN[:2])
    world.svc.store.set_legacy_hold(world.cid, "held by a test: the cockpit may be using this session")
    world.svc._adopt_runners()
    first = world.svc.runners[world.aid]
    until(lambda: (world.adir / "turn.json").exists(), "the first runner's outcome")
    recorded = json.loads((world.adir / "turn.json").read_text())
    assert (recorded["state"], recorded["reason"]) == ("interrupted", "stopped-before-send")
    if path == "same-daemon":
        (world.adir / "exit.json").write_text(json.dumps({"rc": 0}))
        assert first.join(60)
    else:
        world.restart()
        (world.adir / "exit.json").write_text(json.dumps({"rc": 0}))
        if path == "restart-ended":
            world.end_attempt()
        world.ticks()
    assert world.message() == ("waiting", "readmit:legacy-owner")
    final = json.loads((world.adir / "turn.json").read_text())
    assert (final["state"], final["reason"], final["stop_reason"]) == ("interrupted", "stopped-before-send",
                                                                        "legacy-owner")


@pytest.mark.parametrize("ended_attempt", [False, True], ids=["live", "replay"])
def test_a_codex_thread_that_went_idle_before_its_provider_exited_completed_its_turn(tmp_path, ended_attempt):
    """C-26.5: a Codex turn that used a tool gets no `turn/completed`; the runner ends
    it from the thread going idle, after a grace. A runner that met the provider
    already gone (adopted after a restart, or a replay of the ended attempt) drained
    stdout and ended the turn at EOF with no result, failing a turn the provider had
    completed. A thread idle when its provider exited has ended its turn."""
    from subfleet.conversations.runner import TurnRunner
    from subfleet.conversations.store import ConversationStore
    from subfleet.conversations.turn import TurnSpec
    from tests.unit.test_turn_runner import codex_replies
    thread, mid = "7f1c9a0e-2222-4222-8333-444455556666", str(uuid.uuid4())
    store = ConversationStore(tmp_path / "state")
    settings = {"model": "gpt-6", "effort": None, "fast": False, "permission": "read-only", "auto_continue": True}
    cid = store.create_conversation(provider="codex", workspace=str(tmp_path), workspace_kind="in-place",
                                    settings=settings, origin="new")[0]["conversation_id"]
    store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None, text="hi", attachments=[],
                         settings=settings)
    store.set_state(mid, "starting")
    adir = tmp_path / "a1"
    adir.mkdir()
    lines = codex_replies(str(tmp_path)) + [
        json.dumps({"id": 5, "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}}),
        json.dumps({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": "turn-1"}}}),
        json.dumps({"method": "item/completed", "params": {"threadId": thread, "turnId": "turn-1",
                    "item": {"type": "agentMessage", "id": "m1", "text": "Done."}}}),
        json.dumps({"method": "thread/status/changed", "params": {"threadId": thread, "status": {"type": "idle"}}})]
    (adir / "stdout").write_text("".join(line + "\n" for line in lines))
    tags = ["init", "initialized", "hooks", "models", "thread", "user-message"]
    (adir / "stdin.jsonl").write_text("".join(json.dumps(r) + "\n" for seq, tag in enumerate(tags, 1) for r in (
        {"kind": "intent", "seq": seq, "tag": tag, "op": "write"}, {"kind": "written", "seq": seq})))
    (adir / "exit.json").write_text(json.dumps({"rc": 0}))           # the provider is gone already
    spec = TurnSpec(provider="codex", message_id=mid, text="hi", model_id="gpt-6", permission="read-only",
                    native_session_id=None, new_session_id=None, cwd=str(tmp_path))
    reported = []
    runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "codex-1"}, spec=spec,
                        conversation_id=cid, attempt_dir=adir, control_socket=str(tmp_path / "none.sock"),
                        on_outcome=reported.append, on_contain=lambda a: None, ended=ended_attempt)
    runner.start()
    try:
        assert runner.join(60)
        turn = json.loads((adir / "turn.json").read_text())
        assert (turn["state"], turn["reason"]) == ("complete", None), turn
        assert reported == [runner]
    finally:
        store.close()


def test_a_replay_whose_thread_cannot_start_is_tried_again_on_the_next_pass(ended, monkeypatch):
    """C-25.3: a runner whose thread cannot start is not kept, and the next pass
    adopts its turn again. A replay was marked done before its runner started, so one
    that could not start was never tried again in that daemon."""
    import threading
    world = ended([INIT_OK, LIFECYCLE, SUCCESS])
    (world.adir / "exit.json").write_text(json.dumps({"rc": 0}))
    world.end_attempt()
    real_start, failed = threading.Thread.start, []

    def once(self):
        if self.name.startswith("turn:") and not failed:
            failed.append(self.name)
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", once)
    world.ticks(2)
    assert failed and world.message() == ("complete", None)
