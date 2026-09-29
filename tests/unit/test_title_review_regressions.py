"""The review of 66d692a0 (review-titles/astra.md), one schedule per finding.

Each test drives the runner only through what both 66d692a0 and its fix have: the
provider's stdout, the relay (a real RelayServer, and a real pipe where the finding
is about one), the conversation store and the service's own Stop. Each fails on
66d692a0, where the first turn's title request went out during the turn, and passes
now that it goes after the first reply, at a quiescent point (titles.py).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from subfleet import relay as relay_module
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.titles import TITLE_BUDGET_S, TITLE_CANCEL_FRAME, TITLE_FRAME, TITLE_REQUEST_ID
from subfleet.conversations.turn import TurnSpec
from subfleet.relay import Ack, RelayServer, read_log
from tests.unit.test_conversation_service import FakeDaemon
from tests.unit.test_turn_runner import INIT_OK, MID, SID, STEER_CAPS, STEER_MID, claim_steer

SETTINGS = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
ACCEPTED = json.dumps({"type": "command_lifecycle", "command_uuid": MID, "state": "started"})
REPLY = json.dumps({"type": "assistant", "message": {
    "id": "reply", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "Done"}]}})
RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False})
ANSWER = json.dumps({"type": "control_response", "response": {
    "request_id": TITLE_REQUEST_ID, "subtype": "success", "response": {"title": "Importer repairs"}}})
TITLE_TAGS = (TITLE_FRAME, TITLE_CANCEL_FRAME)


def logged(adir: Path) -> list[str]:
    return [row["tag"] for row in read_log(adir / "stdin.jsonl")]


def say(runner: TurnRunner, adir: Path, *lines: str) -> None:
    with open(adir / "stdout", "ab") as out:
        out.write(b"".join(line.encode() + b"\n" for line in lines))
    runner._read_stdout()


def first_turn(store: ConversationStore, root: Path, text: str) -> tuple[str, Path, TurnSpec]:
    cid = store.create_conversation(provider="claude", workspace=str(root), workspace_kind="in-place",
                                    settings=SETTINGS, origin="new")[0]["conversation_id"]
    store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None, text=text,
                         attachments=[], settings=SETTINGS)
    adir = root / "a1"
    adir.mkdir()
    (adir / "stdout").write_bytes(b"")
    spec = TurnSpec(provider="claude", message_id=MID, text=text, model_id="opus[1m]", permission="ask",
                    native_session_id=None, new_session_id=SID, cwd=str(root))
    return cid, adir, spec


# --- P1: a title write waited on a provider that had stopped reading stdin ---------------

def test_p1_a_stop_is_written_while_the_provider_does_not_read_its_stdin():
    """Review P1 (runner.py:823, relay.py:377): Claude takes the message and stops reading
    stdin; a large steer fills most of the pipe. On 66d692a0 the title request then went
    out and its write waited, under the relay's lock and on the runner's thread, until
    the pipe drained, so the person's Stop had its interrupt written only then. The 8 KiB
    cap could not prevent it. Now no title goes during the turn: the interrupt is
    written at once, behind nothing."""
    root = Path(tempfile.mkdtemp(prefix="sfp1-", dir="/tmp"))
    store = ConversationStore(root / "state")
    text = "Fix the importer: " + "the whole description of the problem. " * 240   # a title line of 8 KiB
    cid, adir, spec = first_turn(store, root, text)
    server = RelayServer(root / "r.sock", adir / "stdin.jsonl")
    server.bind()
    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    server.serve(write_end)                     # nothing reads the provider's stdin
    runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1"}, spec=spec, conversation_id=cid,
                        attempt_dir=adir, control_socket=str(root / "r.sock"), on_outcome=lambda r: None,
                        on_contain=lambda a: None)
    runner.relay.timeout_s = 2
    ready, done, failures = threading.Event(), threading.Event(), []

    def runner_thread():
        try:
            runner._apply(runner.driver.start())
            say(runner, adir, INIT_OK, STEER_CAPS)
            runner.replay_caught_up = True
            claim_steer(runner, text="s" * 50_000)       # 50 kB of the pipe's 64 KiB, never read
            runner.steer(STEER_MID)
            runner._drain_commands()
            ready.set()
            say(runner, adir, ACCEPTED)
            while not done.is_set():
                runner._drain_commands()
                runner._send_outbox()
                time.sleep(0.02)
        except Exception as exc:                      # reported below, never lost in the thread
            failures.append(exc)
            ready.set()

    worker = threading.Thread(target=runner_thread, daemon=True)
    worker.start()
    try:
        assert ready.wait(30) and not failures, failures
        assert logged(adir)[-1] == f"steer:{STEER_MID}"
        time.sleep(0.5)                                # the provider's acceptance is handled
        runner.interrupt("stopped")
        deadline = time.monotonic() + 3
        while "interrupt" not in logged(adir) and time.monotonic() < deadline:
            time.sleep(0.02)
        tags = logged(adir)
        assert "interrupt" in tags, f"the Stop's interrupt waited behind the title write: {tags}"
        assert TITLE_FRAME not in tags and not failures, failures
    finally:
        done.set()
        end = time.monotonic() + 30
        while worker.is_alive() and time.monotonic() < end:
            try:
                os.read(read_end, 1 << 16)             # drain, so a write that waits can finish
            except BlockingIOError:
                time.sleep(0.02)
        worker.join(5)
        server.stop()
        os.close(read_end)
        store.close()
        shutil.rmtree(root, ignore_errors=True)
    assert not worker.is_alive()


# --- P1: a Stop committed between the runner's last look and the title write -------------

class LoggingRelay:
    """The relay interface, applied through a real RelayServer; `on_send` sees each frame
    as the runner hands it over."""

    def __init__(self, server, on_send):
        self.server, self.on_send = server, on_send

    def status(self):
        return self.server.status()["status"]

    def send(self, seq, op, *, line=None, tag=None, sig=None):
        self.on_send(tag)
        body = self.server.apply({"seq": seq, "op": op, "line": line, "tag": tag, "sig": sig,
                                  "sha256": relay_module.frame_sha256(op, line, sig)})
        return Ack(seq=seq, ok=body["ok"], dup=body.get("dup", False), error=body.get("error"))

    def close(self):
        pass


class Held:
    """The message's handover lock (a re-entrant lock, `ConversationService._handover`),
    counting how deep the runner is inside it."""

    def __init__(self, lock):
        self.lock, self.depth = lock, 0

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self.lock.release()

    def locked(self):
        return self.depth > 0


def stop_after_store_access(root: Path, target: int, delay: int) -> tuple[bool, list[str]]:
    """Run a first turn through a title budget before its result and one after it, with a
    person's Stop committed through the service right after the runner's `target`th store
    access from the provider's acceptance on. The Stop's `interrupt` reaches the runner
    `delay` store accesses later (the service calls it after its commit, on its own
    thread). Returns whether the Stop fired, and the violations: title frames the runner
    began to hand over after the Stop was recorded."""
    root.mkdir()
    (root / "state").mkdir()
    daemon = FakeDaemon(root / "state")
    svc = ConversationService(daemon)
    try:
        store = svc.store
        cid, adir, spec = first_turn(store, root, "Fix the importer")
        store.set_state(MID, "starting", expect=("queued",))
        server = RelayServer(root / "unused.sock", adir / "stdin.jsonl")
        server._pipe = os.open(os.devnull, os.O_WRONLY)
        clock = [1000.0]
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1"}, spec=spec, conversation_id=cid,
                            attempt_dir=adir, control_socket=str(root / "unused.sock"), on_outcome=lambda r: None,
                            on_contain=lambda a: None, handover=svc._handover(MID), handover_for=svc._handover)
        runner.title.clock = lambda: clock[0]
        runner.handover = Held(runner.handover)
        svc.runners["job/a1"] = runner
        order = itertools.count()
        stopped: list[int] = []
        late: list[str] = []

        def on_send(tag):
            if tag in TITLE_TAGS and stopped and next(order) > stopped[0]:
                late.append(tag)

        runner.relay = LoggingRelay(server, on_send)
        state = {"armed": False, "seen": 0, "firing": False}
        interrupt, deferred = runner.interrupt, []

        def late_interrupt(reason="stopped"):
            if delay:
                deferred.append([delay, reason])
            else:
                interrupt(reason)
        runner.interrupt = late_interrupt

        def after_access():
            if state["firing"]:
                return
            for entry in list(deferred):
                entry[0] -= 1
                if entry[0] <= 0:
                    deferred.remove(entry)
                    interrupt(entry[1])
            if not state["armed"] or stopped or runner.handover.depth:
                # Inside the runner's handover a person's Stop, on its own thread, waits.
                return
            state["seen"] += 1
            if state["seen"] == target:
                state["firing"] = True
                try:
                    svc.op_turn_interrupt({"message_id": MID}, None)   # the person's Stop, as the app sends it
                finally:
                    state["firing"] = False
                stopped.append(next(order))

        for name in ("one", "query"):
            original = getattr(store, name)

            def wrapped(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                after_access()
                return result
            setattr(store, name, wrapped)
        transaction = store.transaction

        def wrapped_transaction(*args, **kwargs):
            manager = transaction(*args, **kwargs)

            class After:
                def __enter__(self):
                    return manager.__enter__()

                def __exit__(self, *exc):
                    result = manager.__exit__(*exc)
                    after_access()
                    return result
            return After()
        store.transaction = wrapped_transaction

        runner._apply(runner.driver.start())
        say(runner, adir, INIT_OK)
        state["armed"] = True
        say(runner, adir, ACCEPTED)
        say(runner, adir, REPLY)

        def ticks():
            for _ in range(3):
                runner._drain_commands()
                runner._send_outbox()
                runner._timers()

        ticks()
        clock[0] += TITLE_BUDGET_S                    # a title asked during the turn runs out of budget
        ticks()
        say(runner, adir, RESULT)
        ticks()
        clock[0] += TITLE_BUDGET_S                    # one asked after the reply does
        ticks()
        return bool(stopped), late
    finally:
        svc.close()
        daemon.store.close()


@pytest.mark.parametrize("delay", [0, 3], ids=["interrupt-at-once", "interrupt-waiting"])
def test_p1_no_title_write_begins_after_a_stop_that_raced_the_runners_last_look(delay):
    """Review P1 (runner.py:785): the runner's last look found no stop; the person's Stop
    committed and queued its interrupt; the runner then wrote the title (or, after the
    budget, its cancellation). Swept over every store access the runner makes from the
    provider's acceptance on, with the Stop's interrupt reaching the runner at once or
    three accesses later: whichever access the Stop lands after, the Stop (sent through
    the service, as the app sends it) is followed by no title frame at all."""
    base = Path(tempfile.mkdtemp(prefix="sfp1b-", dir="/tmp"))
    violations = {}
    try:
        for target in range(1, 300):
            fired, late = stop_after_store_access(base / str(target), target, delay)
            if late:
                violations[target] = late
            if not fired:
                break
        else:
            raise AssertionError("the sweep never reached the end of the runner's store accesses")
        assert target > 3, "the sweep covered the runner's accesses"
    finally:
        shutil.rmtree(base, ignore_errors=True)
    assert violations == {}, f"a title frame began after a recorded Stop, by store access: {violations}"


# --- P2: the title claim held the store's lock across a commit of its own ---------------

def test_p2_a_stop_is_recorded_while_the_title_claim_commits():
    """Review P2 (store.py:542, 432): the claim was a store transaction of its own. With its
    COMMIT stalled (a slow disk), the store's lock stayed held and a person's Stop could not
    be recorded, nor a steer or a message. Now the claim rides the transaction that
    records the first turn's result: no transaction holds the lock for the title alone,
    and the Stop is recorded at once. The conversation's title is still claimed."""
    root = Path(tempfile.mkdtemp(prefix="sfp2-", dir="/tmp"))
    store = ConversationStore(root / "state")
    cid, adir, spec = first_turn(store, root, "Fix the importer")
    server = RelayServer(root / "unused.sock", adir / "stdin.jsonl")
    server._pipe = os.open(os.devnull, os.O_WRONLY)
    runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1"}, spec=spec, conversation_id=cid,
                        attempt_dir=adir, control_socket=str(root / "unused.sock"), on_outcome=lambda r: None,
                        on_contain=lambda a: None)
    runner.relay = LoggingRelay(server, lambda tag: None)
    stalled, release = threading.Event(), threading.Event()
    statements: list[str] = []

    def trace(sql: str) -> None:                 # the thread running the statement runs this first
        if sql.startswith("BEGIN"):
            statements.clear()
        statements.append(sql)
        if sql == "COMMIT" and any("SET title_requested_at" in s for s in statements) and not any(
                "INTO events" in s for s in statements):
            stalled.set()                        # a transaction for the title's claim alone
            release.wait(10)

    store._db.set_trace_callback(trace)
    failures = []

    def turn():
        try:
            runner._apply(runner.driver.start())
            say(runner, adir, INIT_OK, ACCEPTED, REPLY, RESULT, ANSWER)
            runner._send_outbox()
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=turn, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 60
        while worker.is_alive() and not stalled.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        stop = threading.Thread(target=store.update_message, args=(MID,),
                                kwargs={"stop_requested_at": "2026-09-29T12:00:00.000Z"}, daemon=True)
        stop.start()
        stop.join(2)
        assert not stop.is_alive(), "a person's Stop waited on the title claim's commit"
    finally:
        release.set()
        worker.join(30)
        store._db.set_trace_callback(None)
    try:
        assert not worker.is_alive() and not failures, failures
        requested = store.one("SELECT title_requested_at FROM conversations WHERE conversation_id=?", (cid,))
        assert requested["title_requested_at"] is not None, "the claim itself still happens"
    finally:
        if server._pipe is not None:
            os.close(server._pipe)
        store.close()
        shutil.rmtree(root, ignore_errors=True)
