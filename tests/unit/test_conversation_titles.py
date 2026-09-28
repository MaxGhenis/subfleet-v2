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
from tests.unit.test_turn_runner import INIT_OK, logged_intent, relayed  # noqa: F401
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


@pytest.mark.parametrize("reply", [None, response("unused", "error"), response()])
def test_same_turn_relay_does_not_wait_for_title_before_serving_a_reply(relayed, reply):
    runner, clock, server, adir = relayed(recorded=True)
    runner._apply(runner.driver.start())
    runner._apply(runner.driver.feed(INIT_OK, 0))
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


def test_title_ack_timeout_keeps_stdout_polling_and_restores_turn_timeout(relayed):
    held, release = threading.Event(), threading.Event()
    class SlowTitleRelay(RelayServer):
        def apply(self, frame):
            result = super().apply(frame)
            if frame.get("tag") == TITLE_FRAME:
                held.set()
                release.wait(5)
            return result
    runner, clock, server, adir = relayed(recorded=True, server_class=SlowTitleRelay)
    runner._apply(runner.driver.start())
    completed = threading.Event()
    def start_turn():
        try:
            runner._apply(runner.driver.feed(INIT_OK, 0))
        finally:
            completed.set()
    worker = threading.Thread(target=start_turn)
    worker.start()
    try:
        assert held.wait(2), "the first turn must ask its own process for its title"
        assert completed.wait(1), "a lost title ack must not use the turn's 30-second wait"
        assert runner.relay.timeout_s == 5
        assert runner.handshaken and runner.optional_ack_lost
        (adir / "stdout").write_text(json.dumps({"type": "assistant", "message": {
            "id": "reply", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "Still responding"}]}}) + "\n")
        assert runner._read_stdout()
        assert runner.final_text == "Still responding" and runner.stop_reason is None
    finally:
        release.set()
        worker.join(3)
