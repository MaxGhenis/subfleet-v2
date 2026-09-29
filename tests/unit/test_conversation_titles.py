"""First-message titles are optional metadata, never a prerequisite for a turn."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from subfleet.conversations.store import ConversationError, ConversationStore
from subfleet.conversations.titles import (
    SessionTitle, TITLE_BUDGET_S, TITLE_FRAME, TITLE_REQUEST_ID, fallback_title,
)
from tests.unit.test_conversation_service import SETTINGS, svc  # noqa: F401
from tests.unit.test_turn_runner import INIT_OK, MID, logged, logged_intent, relayed  # noqa: F401
from subfleet.relay import RelayServer, read_log


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


def test_only_first_person_message_claims_one_request_and_watch_gets_title(store):
    cid = conversation(store)["conversation_id"]
    submit(store, cid, "internal brief", origin="handoff")
    assert store.conversation(cid)["title"] is None
    first = submit(store, cid)
    second = submit(store, cid, "a later message", after_message_id=first["message_id"])
    title = SessionTitle(store, cid, first["message_id"], clock=lambda: 100)
    assert SessionTitle(store, cid, second["message_id"]).request("later") is None
    frame = title.request("first context")
    assert frame.tag == TITLE_FRAME
    assert json.loads(frame.line)["request"] == {
        "subtype": "generate_session_title", "description": "first context", "persist": False}
    assert title.request("first context") is None
    title.receive(response())
    assert store.conversation(cid)["title_source"] == "generated"
    title.receive(response("Regenerated incorrectly"))
    assert store.conversation(cid)["title"] == "Widget_API.py repairs"
    assert store.changes_after(0)["changes"][-1]["title"] == "Widget_API.py repairs"


def test_rename_atomically_wins_an_inflight_generated_result(store):
    cid = conversation(store)["conversation_id"]
    message = submit(store, cid)
    title = SessionTitle(store, cid, message["message_id"], clock=lambda: 100)
    assert title.request("first")
    store.rename_conversation(cid, "My chosen name")
    title.receive(response())
    result = store.conversation(cid)
    assert (result["title"], result["title_source"]) == ("My chosen name", "person")


def test_person_title_and_codex_never_request_generation(store):
    for fields in ({"title": "Chosen name"}, {"provider": "codex"}):
        cid = conversation(store, **fields)["conversation_id"]
        message = submit(store, cid)
        assert SessionTitle(store, cid, message["message_id"]).request("context") is None
        result = store.conversation(cid)
        assert result["title_source"] == ("person" if "title" in fields else "fallback")


def test_budget_drops_late_response_and_requests_scoped_cancellation_once(store):
    cid = conversation(store)["conversation_id"]
    message = submit(store, cid)
    now = [100.0]
    title = SessionTitle(store, cid, message["message_id"], clock=lambda: now[0])
    assert title.request("context")
    assert title.expire() is None
    now[0] += TITLE_BUDGET_S
    cancellation = title.expire()
    assert json.loads(cancellation.line) == {"type": "control_cancel_request", "request_id": TITLE_REQUEST_ID}
    assert title.expire() is None
    title.receive(response())
    assert store.conversation(cid)["title_source"] == "fallback"
    assert SessionTitle(store, cid, message["message_id"]).request("retry") is None


@pytest.mark.parametrize("reply", [response(None), response(""), response("title", "error"),
                                  response("x" * 121), '{"subfleet-session-title":'])
def test_provider_errors_null_and_malformed_answers_keep_fallback(store, reply):
    cid = conversation(store)["conversation_id"]
    message = submit(store, cid)
    title = SessionTitle(store, cid, message["message_id"], clock=lambda: 100)
    title.request("context")
    title.receive(reply)
    assert store.conversation(cid)["title_source"] == "fallback"
    assert title.request("retry") is None


def test_title_store_failures_are_contained(store, monkeypatch):
    cid = conversation(store)["conversation_id"]
    message = submit(store, cid)
    title = SessionTitle(store, cid, message["message_id"])
    def failed(*args):
        raise RuntimeError("metadata unavailable")
    monkeypatch.setattr(store, "claim_title_generation", failed)
    monkeypatch.setattr(store, "generated_title", failed)
    assert title.request("context") is None
    title.receive(response())
    assert store.message(message["message_id"])["state"] == "queued"


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
        message = submit(reopened, cid)
        assert SessionTitle(reopened, cid, message["message_id"]).request("context") is None
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


ACCEPTED = json.dumps({"type": "command_lifecycle", "command_uuid": MID, "state": "started"})


@pytest.mark.parametrize("reply", [None, response("unused", "error"), response()])
def test_same_turn_relay_does_not_wait_for_title_before_serving_a_reply(relayed, reply):
    runner, clock, server, adir = relayed(recorded=True)
    runner._apply(runner.driver.start())
    runner._apply(runner.driver.feed(INIT_OK, 0))
    runner._apply(runner.driver.feed(ACCEPTED, 1))    # the request follows the provider's acceptance
    frames = read_log(adir / "stdin.jsonl")
    assert [row["tag"] for row in frames][-1] == TITLE_FRAME, (
        runner.store.conversation(runner.conversation_id), runner.replayed_message, runner.recorded)
    assert [row["tag"] for row in frames].index("user-message") < len(frames) - 1
    # The very next stdout lines can complete the real turn, even if its title
    # never responds. Receiving a title through that same stream is optional.
    lines = ([reply] if reply else []) + [json.dumps({"type": "assistant", "message": {
        "id": "reply", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "Done"}]}}),
        json.dumps({"type": "result", "is_error": False, "subtype": "success"})]
    (adir / "stdout").write_text("\n".join(lines) + "\n")
    runner._read_stdout()
    assert runner.driver.outcome.state == "complete"
    assert runner.final_text == "Done" and runner.stop_reason is None
    assert runner.store.conversation(runner.conversation_id)["title_source"] == (
        "generated" if reply == response() else "fallback")


def test_failed_optional_title_frame_in_relay_log_does_not_fail_turn_on_replay(relayed, tmp_path):
    adir = tmp_path / "a1"
    adir.mkdir()
    records = [logged_intent(1, TITLE_FRAME, "title request"), {"kind": "failed", "seq": 1, "errno": 32}]
    (adir / "stdin.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    runner, clock, server, adir = relayed()
    assert runner._handshake()
    assert runner.sent[TITLE_FRAME] == "failed"
    assert runner.stop_reason is None and not runner.relay_failed




# --- the runner's title discipline -----------------------------------------------------
# The title request is optional metadata the runner keeps out of its ordered outbox: it
# goes after the provider has taken the message this runner handed over, only when no
# frame of the turn waits, never after a stop, a cancel, a close or an outcome, never
# again on a replay, and through nothing but the relay interface (status, send, close).

import os
import tempfile
from contextlib import contextmanager

from hypothesis import example, given, settings, strategies as st

from subfleet import relay as relay_module
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.titles import TITLE_CANCEL_FRAME
from subfleet.conversations.turn import Frame, TurnSpec
from subfleet.relay import Ack, FrameTooLarge, RelayError
from tests.unit.test_turn_runner import SID

CLAUDE = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False})
STOPPED_AT = "2026-09-28T12:00:00.000Z"


def reply(text="Still responding"):
    return json.dumps({"type": "assistant", "message": {
        "id": "reply", "model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]}})


class InterfaceRelay:
    """Exactly the relay interface: `status`, `send` and `close`, with no timeout,
    socket or cap attribute to reach for (`__slots__`), applied through a real
    RelayServer's `apply` and log. `fail_next_title` makes the next title write lose
    its answer after (`reached`) or before (`unreached`) the relay took it, be
    `refused`, or exceed the cap (`large`); `failed` lists those it applied. `sends`
    records, for each frame sent, how many frames of the turn waited and whether a
    handover lock was held."""

    __slots__ = ("server", "runner", "fail_next_title", "failed", "sends")

    def __init__(self, server):
        self.server, self.runner, self.fail_next_title, self.failed, self.sends = server, None, None, [], []

    def status(self):
        return self.server.status()["status"]

    def send(self, seq, op, *, line=None, tag=None, sig=None):
        runner = self.runner
        self.sends.append((tag, len(runner.outbox), runner.handover.locked()))
        frame = {"seq": seq, "op": op, "line": line, "tag": tag, "sig": sig,
                 "sha256": relay_module.frame_sha256(op, line, sig)}
        failure = self.fail_next_title if tag in (TITLE_FRAME, TITLE_CANCEL_FRAME) else None
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
    """The first Claude turn of a fresh conversation: its store, relay and stdout.
    `runner()` makes the attempt's runner, again for a later daemon's replay."""

    def __init__(self, root: Path, *, title=None):
        self.adir = root / "a1"
        self.adir.mkdir()
        (self.adir / "stdout").write_bytes(b"")
        self.store = ConversationStore(root / "state")
        self.cid = self.store.create_conversation(provider="claude", workspace=str(root), workspace_kind="in-place",
                                                  settings=CLAUDE, origin="new", title=title)[0]["conversation_id"]
        self.store.submit_message(conversation_id=self.cid, message_id=MID, after_message_id=None,
                                  text="Fix the importer", attachments=[], settings=CLAUDE)
        self.spec = TurnSpec(provider="claude", message_id=MID, text="Fix the importer", model_id="opus[1m]",
                             permission="ask", native_session_id=None, new_session_id=SID, cwd=str(root))
        self.server = RelayServer(root / "unused.sock", self.adir / "stdin.jsonl")
        self.server._pipe = os.open(os.devnull, os.O_WRONLY)
        self.relay = InterfaceRelay(self.server)
        self.now = [1000.0]
        self.root = root

    def runner(self) -> TurnRunner:
        runner = TurnRunner(store=self.store, attempt={"attempt_id": "job/a1"}, spec=self.spec,
                            conversation_id=self.cid, attempt_dir=self.adir,
                            control_socket=str(self.root / "unused.sock"),
                            on_outcome=lambda r: None, on_contain=lambda a: None)
        runner.relay, self.relay.runner = self.relay, runner
        runner.title.clock = lambda: self.now[0]
        runner._apply(runner.driver.start())
        runner._read_stdout()                  # a replay reads what the provider already said
        return runner

    def say(self, runner, *lines):
        with open(self.adir / "stdout", "ab") as out:
            out.write(b"".join(line.encode() + b"\n" for line in lines))
        runner._read_stdout()

    def logged(self):
        return logged(self.adir)

    def requested(self):
        return self.store.one("SELECT title_requested_at FROM conversations WHERE conversation_id=?",
                              (self.cid,))["title_requested_at"]

    def title_sends(self):
        return [send for send in self.relay.sends if send[0] in (TITLE_FRAME, TITLE_CANCEL_FRAME)]

    def close(self):
        if self.server._pipe is not None:
            os.close(self.server._pipe)
        self.store.close()


@contextmanager
def title_turn(**kwargs):
    with tempfile.TemporaryDirectory(prefix="title-turn-", dir="/tmp") as directory:
        turn = TitleTurn(Path(directory), **kwargs)
        try:
            yield turn
        finally:
            turn.close()


def test_the_title_request_waits_for_acceptance_and_for_every_frame_of_the_turn():
    with title_turn() as turn:
        runner = turn.runner()
        turn.say(runner, INIT_OK)
        # The message was handed over, but the provider has not taken it: nothing yet.
        assert turn.logged() == ["init", "user-message", "settings"] and turn.requested() is None
        runner.outbox.append(Frame("probe", "write", "{}"))
        runner.resends, runner.resend_at = 1, runner.clock() + 3600     # a turn frame waits its turn
        turn.say(runner, ACCEPTED)
        assert turn.logged() == ["init", "user-message", "settings"]
        runner.resends = 0
        runner._send_outbox()
        assert turn.logged() == ["init", "user-message", "settings", "probe", TITLE_FRAME]
        assert turn.title_sends() == [(TITLE_FRAME, 0, False)]          # nothing waited; no lock held
        assert turn.requested() is not None


@pytest.mark.parametrize("barrier", ["recorded-stop", "asked-stop", "close", "outcome"])
def test_no_title_is_written_after_a_stop_a_cancel_a_close_or_an_outcome(barrier):
    """A recorded stop is a person's Stop or a message.cancel of the running turn
    (`stop_requested_at`, not yet drained); an asked one is turn.interrupt's, the wall
    limit's or a kill's command; an outcome is the provider's result (here an early one)."""
    with title_turn() as turn:
        runner = turn.runner()
        turn.say(runner, INIT_OK)
        if barrier == "recorded-stop":
            runner.store.update_message(MID, stop_requested_at=STOPPED_AT)
        elif barrier == "asked-stop":
            runner.interrupt("stopped")
        elif barrier == "close":
            runner.outbox.append(Frame("close", "close"))
            runner._send_outbox()
        else:
            turn.say(runner, RESULT)
        turn.say(runner, ACCEPTED, reply())
        runner._drain_commands()
        runner._send_outbox()
        turn.now[0] += TITLE_BUDGET_S
        runner._timers()
        runner._send_outbox()
        assert TITLE_FRAME not in turn.logged() and TITLE_CANCEL_FRAME not in turn.logged()
        assert turn.requested() is None and turn.store.conversation(turn.cid)["title_source"] == "fallback"
        assert not runner.relay_failed and runner.frame_refused is None


@pytest.mark.parametrize("how", ["stop-before-handover", "legacy-withhold"])
def test_a_withheld_message_never_asks_for_a_title(how):
    with title_turn() as turn:
        runner = turn.runner()
        if how == "stop-before-handover":
            runner.store.update_message(MID, stop_requested_at=STOPPED_AT)
        else:
            runner.withhold("legacy-owner")
        turn.say(runner, INIT_OK, ACCEPTED)
        runner._send_outbox()
        assert turn.logged() == ["init", "close"] and runner.withheld
        assert runner.driver.outcome.reason == "stopped-before-send" and turn.requested() is None


def test_a_relay_with_only_status_send_and_close_carries_a_titled_turn_and_its_stop():
    """The runner reaches for no relay attribute outside the interface: InterfaceRelay
    has no `timeout_s`, `_sock` or `frame_max`, and a first turn, its title and its
    stop all go through it."""
    with title_turn() as turn:
        runner = turn.runner()
        turn.say(runner, INIT_OK, ACCEPTED)
        runner.interrupt("stopped")
        runner._drain_commands()
        assert turn.logged() == ["init", "user-message", "settings", TITLE_FRAME, "interrupt"]
        assert not runner.relay_failed and runner.frame_refused is None and runner.outbox == []


@pytest.mark.parametrize("failure", ["reached", "unreached", "refused", "large"])
def test_a_lost_refused_or_oversized_title_write_is_never_retried_and_never_fails_the_turn(failure):
    with title_turn() as turn:
        runner = turn.runner()
        turn.say(runner, INIT_OK)
        turn.relay.fail_next_title = failure
        turn.say(runner, ACCEPTED)
        assert turn.logged().count(TITLE_FRAME) == (failure == "reached")
        assert not runner.relay_failed and runner.stop_reason is None and runner.frame_refused is None
        assert runner.outbox == [] and runner.handshaken             # stdout is still read, nothing resent
        turn.say(runner, reply())
        assert runner.final_text == "Still responding"
        runner._send_outbox()
        # The turn's next frame resynchronizes with the relay first, then goes once.
        runner.interrupt("stopped")
        runner._drain_commands()
        assert turn.logged()[-1] == "interrupt" and not runner.relay_failed
        assert [send[0] for send in turn.title_sends()] == [TITLE_FRAME]       # asked once, never again
        turn.now[0] += TITLE_BUDGET_S
        runner._timers()
        runner._send_outbox()
        assert TITLE_CANCEL_FRAME not in turn.logged()                # nor cancelled after the stop


@pytest.mark.parametrize("stopped", [False, True])
def test_the_title_cancellation_goes_once_after_the_budget_and_never_after_a_stop(stopped):
    with title_turn() as turn:
        runner = turn.runner()
        turn.say(runner, INIT_OK, ACCEPTED)
        if stopped:
            runner.interrupt("stopped")
            runner._drain_commands()
        for _ in range(3):
            turn.now[0] += TITLE_BUDGET_S
            runner._timers()
            runner._send_outbox()
        assert turn.logged().count(TITLE_CANCEL_FRAME) == (0 if stopped else 1)
        assert all(waiting == 0 and not locked for _, waiting, locked in turn.title_sends())


@pytest.mark.parametrize("accepted_first", [True, False])
def test_a_replay_never_sends_a_title_request_again(accepted_first):
    """A later daemon's runner replays what the provider said. It sends no title request:
    not the one the first runner wrote, and none for a message the first runner wrote
    (its conversation keeps the fallback title)."""
    with title_turn() as turn:
        first = turn.runner()
        turn.say(first, INIT_OK, *([ACCEPTED] if accepted_first else []))
        replay = turn.runner()
        assert replay.replayed_message
        turn.say(replay, *([] if accepted_first else [ACCEPTED]), reply())
        replay._send_outbox()
        assert turn.logged().count(TITLE_FRAME) == (1 if accepted_first else 0)
        assert turn.logged().count("user-message") == 1 and not replay.relay_failed


def test_a_stop_is_recorded_while_a_title_write_is_held_by_the_relay(relayed):
    """A title write takes no handover lock: a person's Stop (recorded under the
    message's handover lock, `ConversationService._interrupt`) never waits for it."""
    held, release = threading.Event(), threading.Event()

    class SlowTitleRelay(RelayServer):
        def apply(self, frame):
            if frame.get("tag") == TITLE_FRAME:
                held.set()
                release.wait(5)
            return super().apply(frame)

    runner, clock, server, adir = relayed(recorded=True, server_class=SlowTitleRelay)
    runner._apply(runner.driver.start())
    runner._apply(runner.driver.feed(INIT_OK, 0))
    worker = threading.Thread(target=lambda: runner._apply(runner.driver.feed(ACCEPTED, 1)))
    worker.start()
    try:
        assert held.wait(5), "the first turn asks its own process for its title"
        assert runner.handover.acquire(timeout=1), "a stop would wait behind the title write"
        try:
            runner.store.update_message(MID, stop_requested_at=STOPPED_AT)
        finally:
            runner.handover.release()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert logged(adir) == ["init", "user-message", "settings", TITLE_FRAME]
    assert not runner.relay_failed and runner.stop_reason is None
    runner.interrupt("stopped")
    runner._drain_commands()
    assert logged(adir)[-1] == "interrupt"


# Actions of one generated schedule. `init`, `accept`, `answer`, `text` and `result`
# are the provider's stdout; the barriers are a recorded stop (a person's Stop or a
# cancel), an asked stop (turn.interrupt, a limit, a kill), a close and an outcome;
# `lose-*`, `refused` and `large` make the next title write fail; `expire` runs the
# title budget out; `restart` hands the attempt to a later daemon's runner.
PROVIDER = ("init", "accept", "answer", "text", "result")
INTERVENTIONS = ("recorded-stop", "asked-stop", "close", "lose-reached", "lose-unreached", "refused", "large",
                 "expire", "restart", "tick")


# Half of the drawn actions move the provider on, so most schedules reach acceptance;
# the examples pin each barrier and a restart between the handover and the acceptance.
@settings(max_examples=150, deadline=None)
@given(actions=st.lists(st.one_of(st.sampled_from(PROVIDER), st.sampled_from(INTERVENTIONS)), min_size=1,
                        max_size=16),
       person_title=st.sampled_from([False, False, False, True]))
@example(actions=["init", "recorded-stop", "accept", "tick"], person_title=False)
@example(actions=["init", "asked-stop", "accept", "tick"], person_title=False)
@example(actions=["init", "close", "accept", "tick"], person_title=False)
@example(actions=["init", "result", "accept", "tick"], person_title=False)
@example(actions=["init", "restart", "accept", "tick"], person_title=False)
@example(actions=["init", "accept", "expire", "recorded-stop", "expire"], person_title=False)
@example(actions=["init", "lose-unreached", "accept", "expire", "asked-stop", "expire"], person_title=False)
@example(actions=["init", "accept", "answer", "restart", "expire", "text", "result"], person_title=False)
def test_property_a_title_is_asked_at_most_once_after_acceptance_and_never_past_a_barrier(actions, person_title):
    """Invariants, for every schedule:
    1. at most one title request and one cancellation are ever written, across replays;
    2. a title request follows the message frame and the provider's acceptance of it;
    3. once a stop, a cancel, a close or an outcome falls, no title frame is written;
    4. no title frame is written while a frame of the turn waits or a handover lock is
       held, and none is ever in the outbox;
    5. title failures never fail the relay, refuse a frame or stop the turn;
    6. liveness: a first turn accepted by the runner that handed it over, with no
       barrier and no failed title write, asks for its title (unless a person named it).
    """
    with title_turn(title="Chosen name" if person_title else None) as turn:
        runner, epoch = turn.runner(), 0
        said: list[str] = []
        frozen = None                          # (requests, cancellations) when the first barrier fell
        writer = accepted_at = accepted_by = None
        for action in actions:
            if action == "init" and "init" not in said:
                said.append("init")
                turn.say(runner, INIT_OK)
                if "user-message" in turn.logged() and writer is None:
                    writer = epoch
            elif action == "accept" and "init" in said and "accept" not in said:
                said.append("accept")
                accepted_at, accepted_by = len(turn.logged()), epoch
                turn.say(runner, ACCEPTED)
            elif action == "answer" and "accept" in said and "answer" not in said:
                said.append("answer")
                turn.say(runner, response())
            elif action == "text" and "accept" in said:
                turn.say(runner, reply())
            elif action == "result" and "init" in said and "result" not in said:
                said.append("result")
                turn.say(runner, RESULT)
            elif action == "recorded-stop":
                runner.store.update_message(MID, stop_requested_at=STOPPED_AT)
            elif action == "asked-stop":
                runner.interrupt("stopped")
                runner._drain_commands()
            elif action == "close":
                runner.outbox.append(Frame("close", "close"))
                runner._send_outbox()
            elif action in ("lose-reached", "lose-unreached", "refused", "large"):
                turn.relay.fail_next_title = action.removeprefix("lose-")
            elif action == "expire":
                turn.now[0] += TITLE_BUDGET_S
                runner._timers()
                runner._send_outbox()
            elif action == "restart":
                runner, epoch = turn.runner(), epoch + 1
            else:
                runner._drain_commands()
                runner._send_outbox()
            tags = turn.logged()
            requests, cancellations = tags.count(TITLE_FRAME), tags.count(TITLE_CANCEL_FRAME)
            assert requests <= 1 and cancellations <= 1                                           # 1
            if requests:
                assert tags.index("user-message") < tags.index(TITLE_FRAME)                       # 2
                assert accepted_at is not None and tags.index(TITLE_FRAME) >= accepted_at
            if frozen is None and (action in ("recorded-stop", "asked-stop", "close")
                                   or (action == "result" and "result" in said)):
                frozen = (requests, cancellations)
            if frozen is not None:
                assert (requests, cancellations) == frozen                                         # 3
            assert all(waiting == 0 and not locked for _, waiting, locked in turn.title_sends())  # 4
            assert not any(frame.tag in (TITLE_FRAME, TITLE_CANCEL_FRAME) for frame in runner.outbox)
            if frozen is None:
                assert not runner.relay_failed and runner.frame_refused is None                  # 5
                assert runner.stop_reason is None
        runner._drain_commands()
        runner._send_outbox()
        tags = turn.logged()
        unwritten = any(tag == TITLE_FRAME and failure != "reached" for tag, failure in turn.relay.failed)
        clean = (not person_title and frozen is None and not unwritten and accepted_by is not None
                 and accepted_by == writer)
        if clean:
            assert tags.count(TITLE_FRAME) == 1                                                    # 6
        if person_title:
            assert TITLE_FRAME not in tags
