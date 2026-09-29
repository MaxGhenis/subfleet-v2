"""First-message titles are optional metadata: never a prerequisite for a turn, never a wait for anything."""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import queue
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import relay as relay_module
from subfleet.conversations import runner as runner_module, titles as titles_module
from subfleet.conversations.runner import Clocks, TurnRunner
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError, ConversationStore, TitleUpdate
from subfleet.conversations.titles import (
    CLAIMED, ENDED, OPEN, PIPE_FLOOR, SENT, SessionTitle, TITLE_BUDGET_S, TITLE_CANCEL_FRAME, TITLE_DESCRIPTION_MAX,
    TITLE_FRAME, TITLE_LINE_MAX, TITLE_REQUEST_ID, fallback_title, request_line,
)
from subfleet.conversations.turn import Frame, TurnSpec
from subfleet.relay import Ack, FrameTooLarge, RelayError, RelayServer, read_log
from tests.unit.test_conversation_service import SETTINGS, FakeDaemon, svc  # noqa: F401
from tests.unit.test_turn_runner import INIT_OK, MID, SID, STEER_CAPS, STEER_MID, claim_steer
from tests.unit.test_title_review_regressions import Held


@pytest.fixture
def store(tmp_path):
    result = ConversationStore(tmp_path / "state")
    yield result
    result.close()


def conversation(store, **fields):
    return store.create_conversation(provider=fields.pop("provider", "claude"), workspace="/workspace",
                                     workspace_kind="in-place", settings=SETTINGS, origin="new", **fields)[0]


def submit(store, cid, text="Please fix Widget_API.py; then update the docs", **fields):
    return store.submit_message(conversation_id=cid, message_id=str(uuid.uuid4()),
                                after_message_id=fields.pop("after_message_id", None), text=text,
                                attachments=[], settings=SETTINGS, **fields)[0]


def response(title="Widget_API.py repairs", subtype="success"):
    return json.dumps({"type": "control_response", "response": {
        "request_id": TITLE_REQUEST_ID, "subtype": subtype, "response": {"title": title}}})


def batch(store, cid, mid, title: TitleUpdate, events=()):
    """The runner's events batch (`TurnRunner._flush`), carrying the title's work."""
    store.append_events(conversation_id=cid, message_id=mid, attempt_id="job/a1", events=list(events),
                        stdout_offset=0, stdin_seq=0, title=title)
    return title


def claim(store, cid, mid, at=100.0):
    return batch(store, cid, mid, TitleUpdate(claim_at=at)).claimed


# --- the fallback and the store ---------------------------------------------------------

@pytest.mark.parametrize("prompt,title", [
    ("Please fix Widget_API.py; then update docs", "Widget_API.py"),
    ("Could you help me build a fast little search bar with filters? More", "a fast little search bar with"),
    ("Review parser.ts. Extra context", "parser.ts"),
    ("Build.cs errors", "Build.cs errors"),
    ("fix-importer.js crashes", "fix-importer.js crashes"),
    ("Résumé de l’architecture du système", "Résumé de l’architecture du système"),
    ("   ", "New conversation"),
])
def test_deterministic_fallback_uses_first_clause_and_preserves_identifiers(prompt, title):
    assert fallback_title(prompt) == title


def test_first_acceptance_has_fallback_without_waiting_for_a_provider(store):
    cid = conversation(store)["conversation_id"]
    first = submit(store, cid)
    named = store.conversation(cid)
    assert (named["title"], named["title_source"]) == ("Widget_API.py", "fallback")
    submit(store, cid, "Fix something different", after_message_id=first["message_id"])
    assert store.conversation(cid)["title"] == "Widget_API.py"
    assert store.changes_after(0)["changes"][-1]["title_source"] == "fallback"


def test_only_the_first_person_message_claims_one_request_and_watch_gets_its_title(store):
    cid = conversation(store)["conversation_id"]
    brief = submit(store, cid, "internal brief", origin="handoff")["message_id"]
    assert store.conversation(cid)["title"] is None
    store.set_state(brief, "complete")
    first = submit(store, cid)
    store.set_state(first["message_id"], "running")
    second = submit(store, cid, "a later message", after_message_id=first["message_id"])
    assert not claim(store, cid, second["message_id"])          # not the conversation's first message
    store.set_state(second["message_id"], "complete")          # nothing else of the conversation waits
    assert claim(store, cid, first["message_id"])
    assert not claim(store, cid, first["message_id"], at=101)  # one request per conversation, ever
    assert batch(store, cid, first["message_id"], TitleUpdate(answer=("Widget_API.py repairs", 101))).recorded
    assert store.conversation(cid)["title_source"] == "generated"
    assert not batch(store, cid, first["message_id"], TitleUpdate(answer=("Regenerated incorrectly", 102))).recorded
    assert store.conversation(cid)["title"] == "Widget_API.py repairs"
    assert store.changes_after(0)["changes"][-1]["title"] == "Widget_API.py repairs"


@pytest.mark.parametrize("waiting", ["stop", "queued", "waiting", "steering", None])
def test_the_claim_is_refused_while_anything_else_of_the_conversation_waits(store, waiting):
    """The store's half of the quiescent point (titles.py): a stop recorded for the message,
    or another message of the conversation queued, waiting to start or steering."""
    cid = conversation(store)["conversation_id"]
    first = submit(store, cid)["message_id"]
    store.set_state(first, "running")
    if waiting == "stop":
        store.update_message(first, stop_requested_at="2026-09-29T12:00:00.000Z")
    elif waiting is not None:
        other = submit(store, cid, "and then this", after_message_id=first)["message_id"]
        if waiting != "queued":
            store.set_state(other, waiting, reason=f"steer:{first}" if waiting == "steering" else None)
    assert claim(store, cid, first) == (waiting is None)


def test_the_claim_and_the_answer_ride_the_events_batch_and_their_failure_costs_only_the_title(store, monkeypatch):
    """No transaction of their own: the title's statements run inside the batch's, in a
    savepoint. A failure there leaves the batch's events and watermark recorded."""
    cid = conversation(store)["conversation_id"]
    first = submit(store, cid)["message_id"]
    store.set_state(first, "running")

    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_claim_title", broken)
    event = ("stdout", "10:1", 0, "turn.completed", {"state": "complete"})
    title = batch(store, cid, first, TitleUpdate(claim_at=100.0), events=[event])
    assert not title.claimed and "disk I/O error" in title.error
    assert [row["kind"] for row in store.query("SELECT kind FROM events WHERE message_id=?", (first,))] == [
        "turn.completed"]
    assert store.conversation(cid)["title_requested_at"] is None
    monkeypatch.undo()
    assert claim(store, cid, first)


def test_rename_atomically_wins_an_inflight_generated_result(store):
    cid = conversation(store)["conversation_id"]
    message = submit(store, cid)["message_id"]
    assert claim(store, cid, message)
    store.rename_conversation(cid, "My chosen name")
    assert not batch(store, cid, message, TitleUpdate(answer=("Widget_API.py repairs", 101))).recorded
    result = store.conversation(cid)
    assert (result["title"], result["title_source"]) == ("My chosen name", "person")


def test_person_title_and_codex_never_claim_generation(store):
    for fields in ({"title": "Chosen name"}, {"provider": "codex"}):
        cid = conversation(store, **fields)["conversation_id"]
        message = submit(store, cid)["message_id"]
        assert not claim(store, cid, message)
        assert store.conversation(cid)["title_source"] == ("person" if "title" in fields else "fallback")


@pytest.mark.parametrize("after,recorded", [(TITLE_BUDGET_S - 0.01, True), (TITLE_BUDGET_S, False)])
def test_an_answer_counts_only_within_the_budget_of_its_claim(store, after, recorded):
    cid = conversation(store)["conversation_id"]
    message = submit(store, cid)["message_id"]
    assert claim(store, cid, message, at=100.0)
    assert batch(store, cid, message, TitleUpdate(answer=("Widget repairs", 100.0 + after))).recorded == recorded
    assert store.conversation(cid)["title_source"] == ("generated" if recorded else "fallback")


@pytest.mark.parametrize("reply", [response(None), response(""), response("title", "error"),
                                   response("x" * 121), '{"subfleet-session-title":', '["subfleet-session-title"]'])
def test_provider_errors_null_and_malformed_answers_keep_the_fallback(reply):
    title = SessionTitle("cv", "m", clock=lambda: 100)
    title.receive(reply)
    assert title.take() is None
    # A response to the request, even unusable, is its answer: the hold it kept ends.
    assert title.answered == reply.startswith('{"type"')


def test_an_answer_is_normalized_and_handed_over_once():
    title = SessionTitle("cv", "m", clock=lambda: 100)
    title.receive(response("  Widget   API\trepairs "))
    assert title.pending and title.take() == ("Widget API repairs", 100) and title.take() is None


def test_existing_title_is_preserved_when_title_columns_are_added(tmp_path):
    root = tmp_path / "old-state"
    store = ConversationStore(root)
    cid = conversation(store, title="Existing chosen title")["conversation_id"]
    store.close()
    with sqlite3.connect(root / "conversations.sqlite3") as db:
        db.execute("ALTER TABLE conversations DROP COLUMN title_source")
        db.execute("ALTER TABLE conversations DROP COLUMN title_message_id")
        db.execute("ALTER TABLE conversations DROP COLUMN title_requested_at")
    reopened = ConversationStore(root)
    try:
        assert reopened.conversation(cid)["title_source"] == "person"
        message = submit(reopened, cid)["message_id"]
        assert not claim(reopened, cid, message)
    finally:
        reopened.close()


def test_rename_op_returns_person_title_and_rejects_empty(svc):
    cid = conversation(svc.store)["conversation_id"]
    result = svc.handle("conversation.rename", {"conversation_id": cid, "title": "My session"}, None)
    assert result["conversation"]["title_source"] == "person"
    with pytest.raises(ConversationError, match="title must"):
        svc.handle("conversation.rename", {"conversation_id": cid, "title": "  "}, None)


def test_no_folder_uses_an_idempotent_private_scratch_directory_not_daemon_cwd(svc):
    args = {"request_id": "../untrusted-request-id", "provider": "claude", "workspace": "", "settings": SETTINGS}
    first = svc.handle("conversation.create", args, None)["conversation"]
    again = svc.handle("conversation.create", args, None)["conversation"]
    path = Path(first["workspace"])
    assert path == Path(again["workspace"])
    assert path.parent == svc.root / "conversations" / "workspaces"
    assert path.is_dir() and path != Path.cwd()
    assert path.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ConversationError, match="No folder"):
        svc.handle("conversation.create", {**args, "workspace_kind": "worktree"}, None)


# --- the title's gate (SessionTitle) ----------------------------------------------------

@settings(max_examples=300, deadline=None)
@given(ops=st.lists(st.sampled_from(["claim", "write", "write-refused", "end"]), max_size=12))
def test_property_the_gate_never_writes_after_it_ends_and_writes_at_most_once(ops):
    """open → claimed → sent, and any state may end; nothing leaves `ended`. A write
    begins only from claimed and only while its in-memory check holds."""
    title, writes, ended = SessionTitle("cv", "m", clock=lambda: 0), 0, False
    for op in ops:
        before = title.state
        if op == "claim":
            assert title.claim(1.0) == (before == OPEN)
        elif op.startswith("write"):
            began = title.begin_write(lambda: op == "write")
            assert began == (before == CLAIMED and op == "write")
            writes += began
            assert not (began and ended)
        else:
            assert title.end("stopped") == before
            ended = True
            assert title.state == ENDED and not title.holding
    assert writes <= 1 and (title.state == ENDED) == ended


def test_ending_the_gate_never_waits_on_a_write_under_way():
    """A stop ends the title while the runner writes its line: the end returns at once
    (the gate's lock is never held across I/O) and the write goes on."""
    title = SessionTitle("cv", "m")
    assert title.claim(1.0) and title.begin_write(lambda: True) and title.state == SENT
    ender = threading.Thread(target=title.end, args=("stopped",))
    ender.start()
    ender.join(1)
    assert not ender.is_alive() and title.state == ENDED and title.why == "stopped"


# --- the runner's title discipline -----------------------------------------------------
# The title request is asked of the first turn's own process after its reply, at a
# quiescent point (titles.py): the provider's successful result is read and recorded, it
# is idle and reading stdin, and nothing else of the conversation waits. Its one line
# goes to an idle reader, never after a stop, a close or an outcome other than that
# success, never on a replay, and through nothing but the relay interface (status,
# send, close). The turn's stdin close waits for its answer, its budget, or anything
# else for the conversation, whichever comes first.

ACCEPTED = json.dumps({"type": "command_lifecycle", "command_uuid": MID, "state": "started"})
RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False})
FAILED = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": ""})
STOPPED_AT = "2026-09-28T12:00:00.000Z"
UNSUPPORTED = json.dumps({"type": "control_request", "request_id": "hook-1", "request": {"subtype": "hook_callback"}})


def reply(text="Done"):
    return json.dumps({"type": "assistant", "message": {
        "id": "reply", "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]}})


def lifecycle(mid, state):
    return json.dumps({"type": "command_lifecycle", "command_uuid": mid, "state": state})


class InterfaceRelay:
    """Exactly the relay interface: `status`, `send` and `close`, with no timeout, socket
    or cap attribute to reach for (`__slots__`), applied through a real RelayServer's
    `apply` and log. `fail_next_title` makes the next title write lose its answer after
    (`reached`) or before (`unreached`) the relay took it, be `refused`, or exceed the cap
    (`large`). `sends` records, for each frame, the tags waiting in the outbox, whether
    the runner held its handover lock or the store's lock, and how many commands waited.
    `during_title` runs while the title's line is being handed over."""

    __slots__ = ("server", "runner", "store", "fail_next_title", "failed", "sends", "during_title")

    def __init__(self, server, store):
        self.server, self.store, self.runner, self.fail_next_title, self.failed = server, store, None, None, []
        self.sends, self.during_title = [], None

    def status(self):
        return self.server.status()["status"]

    def send(self, seq, op, *, line=None, tag=None, sig=None):
        runner = self.runner
        held = self.store._lock.held
        self.sends.append((tag, [frame.tag for frame in runner.outbox], runner.handover.locked(),
                           held is not None and held[0] == threading.get_ident(), runner.commands.qsize()))
        if tag == TITLE_FRAME and self.during_title is not None:
            self.during_title()
        frame = {"seq": seq, "op": op, "line": line, "tag": tag, "sig": sig,
                 "sha256": relay_module.frame_sha256(op, line, sig)}
        failure = self.fail_next_title if tag == TITLE_FRAME else None
        if failure:
            self.fail_next_title = None
            self.failed.append((tag, failure))
            if failure == "large":
                raise FrameTooLarge(len(line or ""), 1)
            if failure == "refused":
                return Ack(seq=seq, ok=False, error="conflict")
            if failure == "reached":
                self.server.apply(frame)
            raise RelayError("relay closed the connection before acknowledging")
        body = self.server.apply(frame)
        return Ack(seq=seq, ok=body["ok"], dup=body.get("dup", False), error=body.get("error"))

    def close(self):
        pass


class TitleTurn:
    """The first Claude turn of a fresh conversation, run by the real service's store and
    Stop: its relay, its stdout, its title's clock. `runner()` makes the attempt's runner,
    again for a later daemon's replay."""

    def __init__(self, root: Path, *, title=None, text="Fix the importer", clocks=Clocks()):
        (root / "state").mkdir()
        self.daemon = FakeDaemon(root / "state")
        self.svc = ConversationService(self.daemon)
        self.store = self.svc.store
        self.adir = root / "a1"
        self.adir.mkdir()
        (self.adir / "stdout").write_bytes(b"")
        claude = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
        self.cid = self.store.create_conversation(provider="claude", workspace=str(root), workspace_kind="in-place",
                                                  settings=claude, origin="new", title=title)[0]["conversation_id"]
        self.store.submit_message(conversation_id=self.cid, message_id=MID, after_message_id=None, text=text,
                                  attachments=[], settings=claude)
        self.store.set_state(MID, "starting", expect=("queued",))
        self.claude = claude
        self.spec = TurnSpec(provider="claude", message_id=MID, text=text, model_id="opus[1m]",
                             permission="ask", native_session_id=None, new_session_id=SID, cwd=str(root))
        self.server = RelayServer(root / "unused.sock", self.adir / "stdin.jsonl")
        self.server._pipe = os.open(os.devnull, os.O_WRONLY)
        self.relay = InterfaceRelay(self.server, self.store)
        self.now = [1000.0]
        self.clocks = clocks
        self.root = root
        self.runners = 0

    def runner(self) -> TurnRunner:
        self.runners += 1
        runner = TurnRunner(store=self.store, attempt={"attempt_id": "job/a1"}, spec=self.spec,
                            conversation_id=self.cid, attempt_dir=self.adir,
                            control_socket=str(self.root / "unused.sock"), on_outcome=lambda r: None,
                            on_contain=lambda a: None, clocks=self.clocks, clock=lambda: self.now[0],
                            handover=Held(self.svc._handover(MID)), handover_for=self.svc._handover)
        runner.relay, self.relay.runner = self.relay, runner
        runner.title.clock = lambda: self.now[0]
        self.svc.runners[f"job/a1#{self.runners}"] = runner
        runner._apply(runner.driver.start())
        runner._read_stdout()                  # a replay reads what the provider already said
        return runner

    def say(self, runner, *lines):
        with open(self.adir / "stdout", "ab") as out:
            out.write(b"".join(line.encode() + b"\n" for line in lines))
        runner._read_stdout()

    def tick(self, runner, n=1):
        """What the runner's loop does each turn, bar reading stdout."""
        for _ in range(n):
            runner._drain_commands()
            runner._send_outbox()
            runner._timers()
            if runner._flush_due():
                runner._flush()

    def to_result(self, runner, *, result=RESULT):
        self.say(runner, INIT_OK, ACCEPTED, reply(), result)

    def stop(self):
        """A person's Stop, as the app sends it (`turn.interrupt`)."""
        self.svc.op_turn_interrupt({"message_id": MID}, None)

    def logged(self):
        return [row["tag"] for row in read_log(self.adir / "stdin.jsonl")]

    def requested(self):
        return self.store.conversation(self.cid)["title_requested_at"]

    def title(self):
        conversation = self.store.conversation(self.cid)
        return conversation["title"], conversation["title_source"]

    def title_sends(self):
        return [send for send in self.relay.sends if send[0] in (TITLE_FRAME, TITLE_CANCEL_FRAME)]

    def titles_yielded(self):
        """Every title write went with nothing of the turn waiting but its held close, no
        command waiting, and neither the handover lock nor the store's lock held."""
        return all(waiting == ["close"] and not handover and not store_lock and queued == 0
                   for _, waiting, handover, store_lock, queued in self.title_sends())

    def close(self):
        self.svc.close()
        self.daemon.store.close()
        if self.server._pipe is not None:
            os.close(self.server._pipe)


@contextmanager
def title_turn(**kwargs):
    with tempfile.TemporaryDirectory(prefix="title-turn-", dir="/tmp") as directory:
        turn = TitleTurn(Path(directory), **kwargs)
        try:
            yield turn
        finally:
            turn.close()


def test_the_title_is_asked_after_the_first_reply_and_its_answer_frees_the_close():
    with title_turn() as turn:
        runner = turn.runner()
        turn.say(runner, INIT_OK, ACCEPTED, reply())
        turn.tick(runner, 3)
        assert turn.logged() == ["init", "user-message", "settings"] and turn.requested() is None
        turn.say(runner, RESULT)
        # Asked once the result is read and recorded; the turn's stdin close waits for it.
        assert turn.logged() == ["init", "user-message", "settings", TITLE_FRAME]
        assert json.loads((turn.adir / "turn.json").read_text())["state"] == "complete"
        assert turn.requested() == 1000.0 and runner.title.state == SENT and [f.op for f in runner.outbox] == ["close"]
        assert turn.titles_yielded()
        turn.tick(runner, 3)
        assert turn.logged()[-1] == TITLE_FRAME                        # held while the provider titles
        turn.now[0] += 1.2
        turn.say(runner, response("Importer repairs"))
        assert turn.logged() == ["init", "user-message", "settings", TITLE_FRAME, "close"]
        turn.tick(runner)
        assert turn.title() == ("Importer repairs", "generated") and runner.title.why == "answered"
        request = json.loads(read_log(turn.adir / "stdin.jsonl")[3]["line"])
        assert request["request"] == {"subtype": "generate_session_title", "description": "Fix the importer",
                                      "persist": False}


def test_the_close_waits_for_the_title_only_until_its_budget():
    with title_turn() as turn:
        runner = turn.runner()
        turn.to_result(runner)
        turn.now[0] += TITLE_BUDGET_S - 0.01
        turn.tick(runner)
        assert turn.logged()[-1] == TITLE_FRAME
        turn.now[0] += 0.01
        turn.tick(runner)
        assert turn.logged()[-2:] == [TITLE_FRAME, "close"] and runner.title.why == "budget"
        turn.say(runner, response("Too late"))                        # handled whenever it comes
        turn.tick(runner)
        assert turn.title() == ("the importer", "fallback")
        assert TITLE_CANCEL_FRAME not in turn.logged()                   # the close ends it; nothing cancels


@pytest.mark.parametrize("barrier", [
    "recorded-stop", "persons-stop", "gate-ended", "daemon-stop", "queued-answer", "frame-waiting",
    "next-message", "steering-message", "failed-result", "stopped-result", "eof", "model-mismatch",
    "relay-failed", "frame-refused", "person-title", "not-first-message", "unread-steer", "unread-bytes"])
def test_a_first_turn_that_ends_without_a_quiescent_point_is_never_titled(barrier):
    """Every way the first turn's end is not a quiescent point: the title is not asked,
    nothing waits for it (its stdin close goes at once), and the conversation keeps its
    fallback. The store refuses what only it knows: a stop recorded, another message."""
    text = "Fix the importer " + "with every detail of it " * 400 if barrier == "unread-bytes" else "Fix the importer"
    with title_turn(title="Chosen" if barrier == "person-title" else None, text=text) as turn:
        if barrier == "not-first-message":
            with turn.store.transaction() as tx:          # an earlier message of the conversation was its first
                tx.execute("UPDATE conversations SET title_message_id='an-earlier-message' WHERE conversation_id=?",
                           (turn.cid,))
        runner = turn.runner()
        turn.say(runner, INIT_OK, STEER_CAPS, ACCEPTED, reply())
        result = RESULT
        if barrier == "recorded-stop":
            turn.store.update_message(MID, stop_requested_at=STOPPED_AT)   # the runner has not heard yet
        elif barrier == "persons-stop":
            turn.stop()
            turn.tick(runner)
        elif barrier == "gate-ended":
            runner.end_title("stopped")        # a Stop's first step; its record and interrupt not yet in
        elif barrier == "frame-waiting":
            runner.outbox.append(Frame("probe", "write", "{}"))
            runner.resends, runner.resend_at = 1, turn.now[0] + 3600    # a turn frame waits its resend
        elif barrier == "daemon-stop":
            runner.interrupt("wall-limit")
            turn.tick(runner)
        elif barrier == "queued-answer":
            runner.respond("an-answered-request", "allow")
        elif barrier in ("next-message", "steering-message"):
            other = str(uuid.uuid4())
            turn.store.submit_message(conversation_id=turn.cid, message_id=other, after_message_id=MID,
                                      text="and then this", attachments=[], settings=turn.claude)
            if barrier == "steering-message":
                turn.store.set_state(other, "steering", reason=f"steer:{MID}", expect=("queued",))
        elif barrier == "failed-result":
            result = FAILED
        elif barrier == "stopped-result":
            runner.driver.interrupt_requested = True          # a person's cancel-turn answer, say
        elif barrier == "relay-failed":
            runner._relay_lost("simulated")
        elif barrier == "frame-refused":
            runner.frame_refused = "approval:x"
        elif barrier == "unread-steer":
            runner.replay_caught_up = True
            claim_steer(runner)
            runner.steer(STEER_MID)
            turn.tick(runner)
            assert f"steer:{STEER_MID}" in turn.logged()
            runner.driver._steer_pending.clear()               # the provider never said it read it
            turn.store.set_state(STEER_MID, "steered", reason=f"steer:{MID}", expect=("steering",))
        elif barrier == "unread-bytes":
            turn.say(runner, *[UNSUPPORTED.replace("hook-1", f"hook-{n}") for n in range(60)])   # 60 refusals
        if barrier == "eof":
            (turn.adir / "exit.json").write_text("{}")
            runner._apply(runner.driver.eof(runner.offset))
        elif barrier == "model-mismatch":
            turn.say(runner, json.dumps({"type": "assistant", "message": {
                "id": "m2", "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "hi"}]}}))
        else:
            turn.say(runner, result)
        turn.tick(runner, 3)
        tags = turn.logged()
        assert TITLE_FRAME not in tags and TITLE_CANCEL_FRAME not in tags, tags
        if barrier not in ("eof", "relay-failed", "frame-refused", "frame-waiting"):
            assert tags[-1] == "close", tags                   # the close went at once: nothing held it
        assert turn.title()[1] == ("person" if barrier == "person-title" else "fallback")
        # Refused by the store (a stop recorded, another message, a person's name, not the
        # first message), or never asked of it: either way no request is claimed.
        assert turn.requested() is None and runner.title.state in (OPEN, ENDED)


@pytest.mark.parametrize("release", ["persons-stop", "daemon-stop", "command", "next-message", "rename",
                                     "late-signal", "bad-answer", "provider-exit"])
def test_anything_else_for_the_conversation_frees_the_held_close_at_once(release):
    """While the title is asked, a stop, a command (an answer, a steer), the conversation's
    next message, a rename, a signal of the turn or the provider's own answer frees the
    close within one turn of the runner's loop. Nothing else is written: no second
    request and no cancellation."""
    clocks = Clocks(after_result_s=1.0) if release == "late-signal" else Clocks()
    with title_turn(clocks=clocks) as turn:
        runner = turn.runner()
        turn.to_result(runner)
        turn.tick(runner)
        assert turn.logged()[-1] == TITLE_FRAME and runner.title.holding
        if release == "persons-stop":
            turn.stop()
        elif release == "daemon-stop":
            runner.interrupt("wall-limit")
        elif release == "command":
            runner.respond("late-request", "allow")
        elif release == "next-message":
            turn.store.submit_message(conversation_id=turn.cid, message_id=str(uuid.uuid4()), after_message_id=MID,
                                      text="and then this", attachments=[], settings=turn.claude)
        elif release == "rename":
            turn.store.rename_conversation(turn.cid, "Mine")
        elif release == "late-signal":
            turn.now[0] += 1.0
        elif release == "bad-answer":
            turn.say(runner, response(None))
        else:
            (turn.adir / "exit.json").write_text("{}")
            runner.end_title("provider-ended")                 # `_run`'s first step once the provider is gone
        turn.tick(runner)
        tags = turn.logged()
        assert tags.count(TITLE_FRAME) == 1 and TITLE_CANCEL_FRAME not in tags
        if release != "provider-exit":
            assert "close" in tags and tags.index("close") > tags.index(TITLE_FRAME), (release, tags)
        assert not runner.title.holding and not runner.relay_failed


def test_no_stop_steer_or_message_waits_on_the_title_write():
    """While the title's line is handed to the relay, the runner holds neither its
    handover lock nor the store's lock, so a person's Stop, a steer's claim and the next
    message's submission, each on its own thread, finish at once (they need exactly
    those). The Stop ends the title, frees the held close and settles nothing twice."""
    with title_turn() as turn:
        runner = turn.runner()
        finished = {}

        def elsewhere(name, work):
            thread = threading.Thread(target=lambda: finished.setdefault(name, work() or True), daemon=True)
            thread.start()
            thread.join(2)
            assert not thread.is_alive(), f"{name} waited on the title write"

        def during_title():
            elsewhere("stop", turn.stop)
            elsewhere("message", lambda: turn.store.submit_message(
                conversation_id=turn.cid, message_id=STEER_MID, after_message_id=MID, text="next", attachments=[],
                settings=turn.claude))
            elsewhere("steer", lambda: turn.store.set_state(STEER_MID, "steering", reason=f"steer:{MID}",
                                                            expect=("queued",)))

        turn.relay.during_title = during_title
        turn.to_result(runner)
        assert set(finished) == {"stop", "message", "steer"}
        assert turn.titles_yielded()
        turn.tick(runner)
        assert turn.logged()[-2:] == [TITLE_FRAME, "close"] and runner.title.why == "stopped"


def test_the_claim_rides_the_batch_that_records_the_first_turns_result():
    """The claim is never a transaction of its own (review of 66d692a0, P2): a titled
    first turn makes exactly the store transactions of an untitled one up to its result."""
    counts = {}
    for title in (None, "Chosen"):
        with title_turn(title=title) as turn:
            transactions, claims = [0], []
            transaction, append = turn.store.transaction, turn.store.append_events

            def counted(*args, **kwargs):
                transactions[0] += 1
                return transaction(*args, **kwargs)

            def appended(**kwargs):
                if kwargs.get("title") is not None and kwargs["title"].claim_at is not None:
                    claims.append([event[3] for event in kwargs["events"]])
                return append(**kwargs)

            turn.store.transaction, turn.store.append_events = counted, appended
            runner = turn.runner()
            turn.to_result(runner)
            counts[title] = transactions[0]
            assert claims and all("turn.completed" in kinds for kinds in claims)
            assert (turn.requested() is not None) == (title is None)
    assert counts[None] == counts["Chosen"]


# The reviewer's schedules for the two guards 66d692a0's review showed are not redundant
# (review-titles/astra.md, "Tests and mutations"), as they fall in this design.

def test_a_stop_the_runner_has_before_its_command_is_queued_asks_for_no_title():
    """The stop-fields schedule, before the claim (review of 66d692a0: `interrupt()` paused
    after setting `stop_reason`, before its queue insertion): the provider's result
    arrives meanwhile. The runner's stop field alone keeps the turn from being a
    quiescent point: nothing is claimed, nothing written, the close goes."""
    with title_turn() as turn:
        runner = turn.runner()
        paused, resume = threading.Event(), threading.Event()

        class PausedCommands(queue.Queue):
            def put(self, item, block=True, timeout=None):
                paused.set()
                resume.wait(10)
                super().put(item, block, timeout)

        runner.commands = PausedCommands()
        stopper = threading.Thread(target=runner.interrupt, args=("stopped",))
        turn.say(runner, INIT_OK, ACCEPTED, reply())
        stopper.start()
        try:
            assert paused.wait(5) and runner.stop_reason == "stopped" and runner.commands.empty()
            turn.say(runner, RESULT)
            assert TITLE_FRAME not in turn.logged() and turn.requested() is None
            assert turn.logged()[-1] == "close"
        finally:
            resume.set()
            stopper.join(5)


def test_a_stop_between_the_claim_and_the_write_stops_the_write():
    """The stop-fields schedule, between the claim and the write: the stop lands after
    the result's transaction claimed the title, and after the held close was checked,
    but before the runner writes the title, with its gate not ended yet (another
    thread's `interrupt()` at its first statement). The write's own check under the
    gate sees it: the conversation keeps its claimed-but-unasked fallback, and the
    close goes."""
    with title_turn() as turn:
        runner = turn.runner()
        send_frames = runner._send_frames

        def frames_then_stop():
            send_frames()
            if runner.title.state == CLAIMED:
                runner.stop_reason = "stopped"  # `interrupt()`'s first statement, and no more yet

        runner._send_frames = frames_then_stop
        turn.to_result(runner)
        assert turn.requested() is not None and TITLE_FRAME not in turn.logged()
        assert turn.logged()[-1] == "close" and runner.title.why == "not-quiescent"


@pytest.mark.parametrize("command", ["answer", "steer"])
def test_a_command_queued_between_the_claim_and_the_write_skips_the_title(command):
    """A person's answer or steer is sent to the runner (another thread) after the result's
    transaction claimed the title and before the write: the title is not written, so the
    command never waits even the one line, and the close goes."""
    with title_turn() as turn:
        runner = turn.runner()
        send_frames = runner._send_frames

        def frames_then_command():
            send_frames()
            if runner.title.state == CLAIMED:
                if command == "answer":
                    runner.respond("a-request", "allow")
                else:
                    claim_steer(runner)
                    runner.steer(STEER_MID)

        runner._send_frames = frames_then_command
        turn.to_result(runner)
        assert turn.requested() is not None and TITLE_FRAME not in turn.logged()
        assert turn.logged()[-1] == "close" and runner.title.why == "not-quiescent"


def test_a_runner_never_asks_a_provider_other_than_claude():
    """The request is a Claude control. A runner for another provider never claims it or
    writes it, even from a store that would grant the claim (defense in depth: the store
    grants only Claude conversations, `ConversationStore._claim_title`)."""
    with title_turn() as turn:
        runner = turn.runner()
        runner.spec = dataclasses.replace(runner.spec, provider="codex")
        turn.to_result(runner)
        assert TITLE_FRAME not in turn.logged() and turn.requested() is None and turn.logged()[-1] == "close"


@pytest.mark.parametrize("ending", ["failed-result", "eof", "model-mismatch", "stopped-before-send"])
def test_only_the_providers_successful_result_is_a_quiescent_point(ending):
    """The outcome schedule: a turn that ended any other way (an error result, stdout's
    end, the driver's own stop, a stop before the message went) never asks, even with
    the provider accepting its message and nothing else waiting."""
    with title_turn() as turn:
        runner = turn.runner()
        if ending == "stopped-before-send":
            runner.withhold("legacy-owner")
            turn.say(runner, INIT_OK)
        else:
            turn.say(runner, INIT_OK, ACCEPTED, reply())
        if ending == "failed-result":
            turn.say(runner, FAILED)
        elif ending == "eof":
            runner._apply(runner.driver.eof(runner.offset))
        elif ending == "model-mismatch":
            turn.say(runner, json.dumps({"type": "assistant", "message": {
                "id": "m2", "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "hi"}]}}))
        turn.tick(runner, 2)
        assert runner.driver.outcome is not None and runner.driver.outcome.state != "complete"
        assert TITLE_FRAME not in turn.logged() and turn.requested() is None


@pytest.mark.parametrize("how", ["stop-before-handover", "legacy-withhold"])
def test_a_withheld_message_never_asks_for_a_title(how):
    with title_turn() as turn:
        if how == "stop-before-handover":
            turn.store.update_message(MID, stop_requested_at=STOPPED_AT)
        runner = turn.runner()
        if how == "legacy-withhold":
            runner.withhold("legacy-owner")
        turn.say(runner, INIT_OK, ACCEPTED, RESULT)
        turn.tick(runner)
        assert turn.logged() == ["init", "close"] and runner.withheld
        assert runner.driver.outcome.reason == "stopped-before-send" and turn.requested() is None


@pytest.mark.parametrize("crash", ["after-the-claim", "after-the-write", "during-the-hold"])
def test_a_replay_never_asks_for_a_title_again(crash):
    """A later daemon's runner replays what the provider said. It writes no title
    request, whether the first runner died after claiming (the conversation keeps its
    fallback), after writing, or while the close waited; it closes stdin. An answer the
    provider gave meanwhile still names the conversation, within the budget."""
    with title_turn() as turn:
        first = turn.runner()
        if crash == "after-the-claim":
            send_title = first._send_title

            def crash_once_claimed():
                if first.title.state == CLAIMED:
                    raise RuntimeError("simulated daemon crash between the claim and the write")
                return send_title()

            first._send_title = crash_once_claimed
            with pytest.raises(RuntimeError, match="simulated"):
                turn.to_result(first)
        else:
            turn.to_result(first)
        first.stop()
        replay = turn.runner()
        assert replay.replayed_message and replay.recorded is not None
        if crash == "during-the-hold":
            turn.say(replay, response("Importer repairs"))
        turn.tick(replay, 2)
        tags = turn.logged()
        assert tags.count(TITLE_FRAME) == (0 if crash == "after-the-claim" else 1)
        assert tags[-1] == "close" and tags.count("close") == 1 and tags.count("user-message") == 1
        assert turn.requested() is not None
        assert turn.title() == (("Importer repairs", "generated") if crash == "during-the-hold"
                                else ("the importer", "fallback"))
        assert not replay.relay_failed


def test_a_steer_the_provider_consumed_leaves_a_quiescent_point():
    """A steer during the turn, read and consumed by the provider (its lifecycle): the
    result is still a quiescent point, and the title follows it, behind the steer."""
    with title_turn() as turn:
        runner = turn.runner()
        turn.say(runner, INIT_OK, STEER_CAPS, ACCEPTED)
        runner.replay_caught_up = True
        claim_steer(runner)
        runner.steer(STEER_MID)
        turn.tick(runner)
        turn.say(runner, lifecycle(STEER_MID, "started"), lifecycle(STEER_MID, "completed"), reply())
        turn.store.set_state(STEER_MID, "steered", reason=f"steer:{MID}", expect=("steering",))
        turn.say(runner, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                     "user_message_uuids": [MID, STEER_MID]}))
        tags = turn.logged()
        assert tags.index(f"steer:{STEER_MID}") < tags.index(TITLE_FRAME) and turn.titles_yielded()


@pytest.mark.parametrize("failure", ["reached", "unreached", "refused", "large"])
def test_a_lost_refused_or_oversized_title_write_is_never_retried_and_never_fails_the_turn(failure):
    """The held close then goes once, after resynchronizing with the relay's log (a lost
    answer may have taken a frame number), and the relay stays up."""
    with title_turn() as turn:
        runner = turn.runner()
        turn.relay.fail_next_title = failure
        turn.to_result(runner)
        turn.tick(runner, 2)
        tags = turn.logged()
        assert tags.count(TITLE_FRAME) == (failure == "reached") and tags[-1] == "close" and tags.count("close") == 1
        assert [send[0] for send in turn.title_sends()] == [TITLE_FRAME]          # asked once, never again
        assert not runner.relay_failed and runner.stop_reason is None and runner.frame_refused is None
        assert not runner.optional_ack_lost and runner.handshaken


def test_the_runner_reaches_its_relay_only_through_status_send_and_close():
    """Rule 6 on every code path, not only those a test drives: each use of the runner's
    relay is one of the interface's three calls, or the assignment of the relay (the
    merge once read and set `timeout_s` and the private `_sock` for every frame)."""
    tree = ast.parse(Path(runner_module.__file__).read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    uses = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "relay" and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            parent = parents[node]
            if isinstance(node.ctx, ast.Store):
                uses.append("=")
            elif isinstance(parent, ast.Attribute) and isinstance(parents[parent], ast.Call):
                uses.append(parent.attr)
            else:
                uses.append(ast.unparse(parent))
    assert set(uses) == {"=", "status", "send", "close"}, uses


def test_a_relay_with_only_status_send_and_close_carries_a_titled_turn():
    with title_turn() as turn:
        runner = turn.runner()
        turn.to_result(runner)
        turn.say(runner, response())
        assert turn.logged() == ["init", "user-message", "settings", TITLE_FRAME, "close"]
        assert not runner.relay_failed and runner.frame_refused is None and runner.outbox == []


# --- the title's line never waits on the provider ---------------------------------------

@pytest.mark.parametrize("text", ["\U0001F600" * TITLE_DESCRIPTION_MAX, "漢" * TITLE_DESCRIPTION_MAX,
                                  '"\\\n\x01' * 5000, "x" * TITLE_DESCRIPTION_MAX],
                         ids=["emoji", "cjk", "escapes", "ascii"])
def test_the_title_line_and_all_the_provider_may_not_have_read_fit_a_pipe_with_no_reader(text):
    """At the quiescent point the runner allows at most PIPE_FLOOR bytes the provider may
    not have read, the title's line included (`_title_fits`). A pipe here takes that much
    with no reader at all, so the relay's write of the line never waits on the provider."""
    line = (request_line(text) + "\n").encode()
    read_end, write_end = os.pipe()
    try:
        os.set_blocking(write_end, False)
        ahead = b"x" * (PIPE_FLOOR - len(line))
        assert os.write(write_end, ahead) == len(ahead)
        assert os.write(write_end, line) == len(line)     # a short write or EAGAIN would be a wait
    finally:
        os.close(read_end)
        os.close(write_end)


@settings(max_examples=300, deadline=None)
@given(text=st.one_of(st.text(), st.builds(lambda head, character, count, tail: head + character * count + tail,
                                             st.text(max_size=40), st.characters(), st.integers(0, 20_000),
                                             st.text(max_size=40))))
def test_property_the_title_line_is_bounded_and_describes_the_longest_prefix_that_fits(text):
    line = request_line(text)
    assert line.isascii() and len(line.encode()) <= TITLE_LINE_MAX < PIPE_FLOOR
    request = json.loads(line)
    assert (request["type"], request["request_id"]) == ("control_request", TITLE_REQUEST_ID)
    description = request["request"].pop("description")
    assert request["request"] == {"subtype": "generate_session_title", "persist": False}
    offered = text[:TITLE_DESCRIPTION_MAX]
    assert offered.startswith(description)
    if description != offered:
        assert len(titles_module._request_line(offered[:len(description) + 1])) > TITLE_LINE_MAX


# --- every schedule --------------------------------------------------------------------
# One generated schedule interleaves the provider's rows with everything else that can
# touch the conversation. Provider: `init`, `accept`, `text`, `result` (successful),
# `failed` (an error result), `answer` and `bad-answer` (the title's), `exit` (the
# provider ends). Others: `stop` (a person's Stop, through the service), `daemon-stop`
# (a wall limit or a kill), `steer` (a person's steer: claimed, then the command),
# `respond` (a person's answer, queued), `message` (the conversation's next message),
# `rename`, `lose-*`/`refused`/`large` (the next title write fails), `expire` (the
# title's budget runs out), `tick` (a turn of the runner's loop), `restart` (a later
# daemon's runner replays the attempt).
PROVIDER = ("init", "accept", "text", "result", "failed", "answer", "bad-answer", "exit")
OTHERS = ("stop", "daemon-stop", "steer", "respond", "message", "rename", "lose-reached", "lose-unreached",
          "refused", "large", "expire", "tick", "tick", "restart")


# Each schedule builds a service and two stores (about 2 s here under load, most of it
# fsync); SUBFLEET_TITLE_SCHEDULES runs more of them (400 ran for the review record).
SCHEDULES = int(os.environ.get("SUBFLEET_TITLE_SCHEDULES", "60"))


@settings(max_examples=SCHEDULES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(actions=st.lists(st.one_of(st.sampled_from(PROVIDER), st.sampled_from(OTHERS)), min_size=1, max_size=18))
@example(actions=["init", "accept", "text", "result", "tick", "answer", "tick"])
@example(actions=["init", "accept", "result", "stop", "tick"])
@example(actions=["init", "accept", "stop", "result", "tick"])
@example(actions=["init", "accept", "message", "result", "tick"])
@example(actions=["init", "accept", "result", "message", "tick"])
@example(actions=["init", "accept", "steer", "result", "tick"])
@example(actions=["init", "accept", "result", "steer", "tick"])
@example(actions=["init", "accept", "respond", "result", "tick"])
@example(actions=["init", "accept", "result", "expire", "answer", "tick"])
@example(actions=["init", "accept", "result", "restart", "answer", "tick"])
@example(actions=["init", "accept", "restart", "result", "tick"])
@example(actions=["init", "accept", "lose-reached", "result", "tick", "stop", "tick"])
@example(actions=["init", "accept", "result", "daemon-stop", "answer", "tick"])
@example(actions=["init", "accept", "failed", "tick"])
@example(actions=["init", "accept", "result", "exit", "restart", "tick"])
def test_property_a_title_waits_for_the_first_reply_and_nothing_waits_for_it(actions):
    """Invariants, for every schedule:
    1. at most one title request is ever written, replays included, and no cancellation;
    2. a title request is written only after this runner's own first turn ended with the
       provider's successful result, recorded (its event and turn.json), and with the
       claim recorded in the same batch as that result;
    3. none is written after a recorded stop (a person's or the daemon's), after the
       close, after an outcome recorded by an earlier runner, or while the conversation's
       next message waits;
    4. no stop, steer or message waits on a title write or claim: none is written while
       a lock they need is held or a command waits, and the claim has no transaction of
       its own;
    5. the close is held only while the title is outstanding: after the answer, the
       budget, a stop, a command or the next message, the next turn of the loop writes it;
    6. title failures never fail the relay, refuse a frame or stop the turn;
    7. liveness: a first turn ending in the provider's success with nothing else before
       it, and no title write failure, asks exactly once.
    """
    with title_turn() as turn:
        runner, epoch, gone = turn.runner(), 0, False
        said, barrier = set(), None             # the log length when the first barrier fell
        claims = []
        append = turn.store.append_events

        def appended(**kwargs):
            title = kwargs.get("title")
            result = append(**kwargs)
            if title is not None and title.claim_at is not None:
                claims.append(([event[3] for event in kwargs["events"]], title.claimed))
            return result

        turn.store.append_events = appended

        def at_title_write():
            outcome = json.loads((turn.adir / "turn.json").read_text())
            assert outcome["state"] == "complete" and outcome["ended_by"] == "provider"            # 2
            kinds, granted = claims[-1]
            assert turn.requested() is not None and granted and "turn.completed" in kinds              # 4
            assert not turn.store.query("SELECT 1 FROM messages WHERE conversation_id=? AND message_id<>? "
                                        "AND state IN ('queued','waiting','steering')", (turn.cid, MID))  # 3
            assert not runner.replayed_message and runner.recorded is None                              # 2

        turn.relay.during_title = lambda: at_title_write()
        failed_write = clean = False
        released = None                          # the log length when the hold should have ended
        for action in actions:
            if gone and action != "restart":
                continue
            before = runner.title.holding
            if action == "init" and "init" not in said:
                said.add("init")
                turn.say(runner, INIT_OK, STEER_CAPS)
                runner.replay_caught_up = True
            elif action == "accept" and "init" in said and "accept" not in said:
                said.add("accept")
                turn.say(runner, ACCEPTED)
            elif action == "text" and "accept" in said:
                turn.say(runner, reply())
            elif action in ("result", "failed") and "accept" in said and not said & {"result", "failed"}:
                said.add(action)
                clean = action == "result" and barrier is None and epoch == 0 and not failed_write
                turn.say(runner, RESULT if action == "result" else FAILED)
            elif action in ("answer", "bad-answer"):
                turn.say(runner, response() if action == "answer" else response(None, "error"))
            elif action == "exit":
                (turn.adir / "exit.json").write_text("{}")
                runner.end_title("provider-ended")
                runner._read_stdout()
                if runner.driver.outcome is None:
                    runner._apply(runner.driver.eof(runner.offset))
                runner._flush()
                gone = True
            elif action == "stop":
                try:
                    turn.stop()
                except ConversationError:
                    pass                          # not running (no message state for it to stop)
                barrier = len(turn.logged()) if barrier is None else barrier
            elif action == "daemon-stop":
                runner.interrupt("wall-limit")
                barrier = len(turn.logged()) if barrier is None else barrier
            elif action == "steer" and "steer" not in said and "init" in said:
                said.add("steer")
                try:
                    claim_steer(runner)
                except ConversationError:
                    continue                      # refused by the store (its predecessor not accepted yet)
                runner.steer(STEER_MID)
                barrier = len(turn.logged()) if barrier is None else barrier
            elif action == "respond":
                # Waits only until the runner drains it: no title goes meanwhile (4), and one
                # asked already frees the close (5). Drained before the result, it is gone.
                runner.respond("an-answered-request", "allow")
            elif action == "message" and "message" not in said:
                said.add("message")
                try:
                    turn.store.submit_message(conversation_id=turn.cid, message_id=str(uuid.uuid4()),
                                              after_message_id=MID, text="next", attachments=[], settings=turn.claude)
                except ConversationError:
                    continue                      # refused by the store (its predecessor not accepted yet)
                barrier = len(turn.logged()) if barrier is None else barrier
            elif action == "rename":
                turn.store.rename_conversation(turn.cid, "Mine")
                barrier = len(turn.logged()) if barrier is None else barrier
            elif action in ("lose-reached", "lose-unreached", "refused", "large"):
                turn.relay.fail_next_title = action.removeprefix("lose-")
            elif action == "expire":
                turn.now[0] += TITLE_BUDGET_S
            elif action == "restart":
                runner.stop()
                runner.finished.set()             # its thread ended: the service no longer finds it
                runner, epoch, gone = turn.runner(), epoch + 1, False
                if runner.driver.outcome is None and (turn.adir / "exit.json").exists():
                    gone = True
            turn.tick(runner)
            if turn.relay.failed and turn.relay.failed[-1][0] == TITLE_FRAME:
                failed_write = True
            tags = turn.logged()
            assert tags.count(TITLE_FRAME) <= 1 and TITLE_CANCEL_FRAME not in tags                    # 1
            if TITLE_FRAME in tags:
                if barrier is not None:
                    assert tags.index(TITLE_FRAME) < barrier                                            # 3
                if "close" in tags:
                    assert tags.index(TITLE_FRAME) < tags.index("close")                                # 3
            assert turn.titles_yielded()                                                                # 4
            if before and not runner.title.holding and not gone:
                released = released if released is not None else len(tags)
            if released is not None and not gone and not runner.relay_failed:
                assert "close" in tags, (action, tags)                                                  # 5
            if barrier is None and epoch == 0:
                assert not runner.relay_failed and runner.frame_refused is None                        # 6
                assert runner.stop_reason is None
        if clean and not gone and not failed_write:
            assert turn.logged().count(TITLE_FRAME) == 1, actions                                      # 7
        assert all(kinds.count("turn.completed") == 1 for kinds, _ in claims)                           # 2, 4
        assert sum(granted for _, granted in claims) <= 1                                               # 1
