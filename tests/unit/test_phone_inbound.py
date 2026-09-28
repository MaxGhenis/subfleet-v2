"""Phone actions exercise the real conversation service and temporary store."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from types import SimpleNamespace

import pytest

from subfleet.conversations import phone
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError, utcnow


SETTINGS = {"model": "opus", "permission": "ask"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    daemon = SimpleNamespace(root=tmp_path, log=logging.getLogger("phone-test"), requests=None,
                             policy={}, _notify=lambda: None)
    svc = ConversationService(daemon)
    monkeypatch.setattr(svc, "_person", lambda peer, what: SimpleNamespace(pid=123, reason="terminal"))
    svc._phone_actions = threading.RLock()
    yield svc
    svc.runners.clear()
    svc.close()


def card(service, *, questions=None, approval=True, token="test-token", mid=101):
    store = service.store
    conversation, _ = store.create_conversation(provider="claude", workspace=str(service.root),
        workspace_kind="in-place", settings=SETTINGS, origin="new", title="Phone test")
    cid, message_id = conversation["conversation_id"], str(uuid.uuid4())
    store.submit_message(conversation_id=cid, message_id=message_id, after_message_id=None,
                         text="Original task", attachments=[], settings=SETTINGS)
    pending = None
    actions = {}
    if approval:
        pending, _ = store.add_approval(message_id=message_id, conversation_id=cid, attempt_id="test/a1",
            provider_request_id=f"request-{token}", kind="question" if questions is not None else "tool",
            request={"input": {"questions": questions}} if questions is not None else {"tool": "Read"},
            display={"questions": questions} if questions is not None else {"tool": "Read"},
            options=("answer", "deny") if questions is not None else ("allow", "allow-session", "deny"))
        calls = []
        service.runners["test/a1"] = SimpleNamespace(driver=SimpleNamespace(outcome=None), respond=lambda *a: calls.append(a))
        actions = {f"a{i}": {"kind": "decision", "decision": value} for i, value in enumerate(pending["options"])
                   if value != "answer"}
        for i, question in enumerate(questions or []):
            for j, option in enumerate(question.get("options", [])):
                actions[f"q{i}o{j}"] = {"kind": "option", "question": i, "option": option["label"]}
            if question.get("multiSelect"):
                actions[f"q{i}done"] = {"kind": "finish", "question": i}
    else:
        calls = []
    with store.transaction() as tx:
        tx.execute("INSERT INTO phone_cards(token,event_key,conversation_id,message_id,approval_id,kind,"
                   "telegram_message_id,state,actions_json,progress_json,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,'sent',?,?,?,?)", (token, f"event-{token}", cid, message_id,
                   pending["approval_id"] if pending else None, "question" if questions is not None else "approval",
                   mid, json.dumps(actions), json.dumps({"index": 0, "answers": {}, "selected": [], "consumed": []}),
                   utcnow(), utcnow()))
    return SimpleNamespace(cid=cid, mid=message_id, approval=pending, calls=calls, token=token, telegram=mid)


def question(text, labels=("One", "Two"), *, multi=False):
    return {"question": text, "multiSelect": multi, "options": [{"label": label} for label in labels]}


def tap(service, c, action):
    return phone.tap(service, {"data": f"sf:{c.token}:{action}"}, 123)


def reply(service, c, text="Please continue", update_id=1):
    args = {"telegram_message_id": c.telegram, "text": text}
    if update_id is not None:
        args["update_id"] = update_id
    return phone.reply(service, args, 123)


def test_owns_persists_and_disabled_policy_still_owns(service):
    c = card(service)
    assert phone.owns(service, {"telegram_message_id": c.telegram}, 123) == {"owned": True}
    assert phone.owns(service, {"telegram_message_id": 999}, 123) == {"owned": False}
    service.daemon.policy = {"phone": {"telegram": {"enabled": False}}}
    assert phone.owns(service, {"telegram_message_id": c.telegram}, 123)["owned"]
    with pytest.raises(ConversationError, match="disabled"):
        tap(service, c, "a0")
    with pytest.raises(ConversationError, match="disabled"):
        reply(service, c)


@pytest.mark.parametrize("action,decision", [("a0", "allow"), ("a1", "allow-session"), ("a2", "deny")])
def test_offered_approval_and_duplicate_dispatch_once(service, action, decision):
    c = card(service)
    assert tap(service, c, action)["route"] == "approval"
    assert tap(service, c, action)["duplicate"]
    assert len(c.calls) == 1
    assert c.calls[0][1] == decision
    assert service.store.approval(c.approval["approval_id"])["decision"]["source"] == "phone"
    events = service.store.events_after(c.cid, 0)["events"]
    assert len(events) == 1 and events[0]["data"]["source"] == "phone"
    assert not service.store.query("SELECT * FROM attempt_marks")
    other = "a2" if action != "a2" else "a0"
    with pytest.raises(ConversationError, match="different answer"):
        tap(service, c, other)


@pytest.mark.parametrize("data", ["sf:unknown:a0", "sf:test-token:allow", "sf:test-token:a0:extra", "oops", "sf:test-token:" + "a" * 70])
def test_unknown_or_unoffered_tap_fails_without_response(service, data):
    c = card(service)
    with pytest.raises(ConversationError):
        phone.tap(service, {"data": data}, 123)
    assert c.calls == []


@pytest.mark.parametrize("state", ["withdrawn", "turn-ended"])
def test_stale_approval_is_not_answered(service, state):
    c = card(service)
    if state == "withdrawn":
        service.store.withdraw_approvals(attempt_id="test/a1")
    else:
        service.runners.clear()
    with pytest.raises(ConversationError):
        tap(service, c, "a0")
    assert c.calls == []


def test_phone_preserves_person_check(service, monkeypatch):
    c = card(service)
    def reject(*args):
        raise ConversationError("person-only", "agent rejected", code=7)
    monkeypatch.setattr(service, "_person", reject)
    for op, args in ((phone.tap, {"data": f"sf:{c.token}:a0"}),
                     (phone.reply, {"telegram_message_id": c.telegram, "text": "hello"}),
                     (phone.owns, {"telegram_message_id": c.telegram}),
                     (phone.notify, {"conversation_id": c.cid})):
        with pytest.raises(ConversationError, match="agent rejected"):
            op(service, args, 123)
    assert c.calls == []


def test_questions_advance_and_replayed_old_button_does_not_answer_next(service):
    c = card(service, questions=[question("First?"), question("Second?")])
    with pytest.raises(ConversationError, match="currently shown"):
        tap(service, c, "q1o0")
    assert tap(service, c, "q0o1")["question_index"] == 1
    assert c.calls == []
    assert tap(service, c, "q0o1")["duplicate"]
    assert tap(service, c, "q1o0")["route"] == "answer"
    assert c.calls == [(f"request-{c.token}", "answer", None, {"First?": "Two", "Second?": "One"})]
    assert tap(service, c, "q1o0")["duplicate"]
    with pytest.raises(ConversationError):
        tap(service, c, "q1o1")


def test_partial_button_retry_after_app_resolution_is_stale(service):
    c = card(service, questions=[question("First?"), question("Second?")])
    tap(service, c, "q0o0")
    service.store.answer_approval(c.approval["approval_id"],
                                  {"decision": "answer", "answers": {"First?": "Two", "Second?": "Two"}})
    with pytest.raises(ConversationError, match="already answered"):
        tap(service, c, "q0o0")


def test_app_resolution_racing_question_progress_does_not_record_an_answer(service, monkeypatch):
    c = card(service, questions=[question("First?"), question("Second?")])
    original = phone._questions
    def resolved(approval):
        questions = original(approval)
        service.store.answer_approval(approval["approval_id"], {"decision": "deny"})
        return questions
    monkeypatch.setattr(phone, "_questions", resolved)
    with pytest.raises(ConversationError, match="already answered"):
        tap(service, c, "q0o0")
    row = service.store.one("SELECT progress_json FROM phone_cards WHERE token=?", (c.token,))
    assert json.loads(row["progress_json"])["index"] == 0
    assert not service.store.events_after(c.cid, 0)["events"]


def test_multiselect_is_additive_and_requires_finish(service):
    c = card(service, questions=[question("Choose?", multi=True)])
    with pytest.raises(ConversationError, match="choose an option"):
        tap(service, c, "q0done")
    assert tap(service, c, "q0o1")["pending"]
    assert tap(service, c, "q0o1")["duplicate"]
    tap(service, c, "q0o0")
    assert c.calls == []
    tap(service, c, "q0done")
    assert c.calls[0][3] == {"Choose?": "Two, One"}
    tap(service, c, "q0done")
    assert len(c.calls) == 1


def test_reply_answers_questions_then_queues_after_resolution(service):
    c = card(service, questions=[question("First?"), question("Second?")])
    result = reply(service, c, "Free text", 100)
    assert result["pending"]
    assert reply(service, c, "Free text", 100)["duplicate"]
    assert reply(service, c, "Another answer", 101)["route"] == "answer"
    assert c.calls[0][3] == {"First?": "Free text", "Second?": "Another answer"}
    assert reply(service, c, "Another answer", 101)["duplicate"]
    assert reply(service, c, "Next task", 102)["route"] == "queued"
    assert len(service.store.query("SELECT * FROM messages")) == 2


def test_question_reply_recovers_after_response_before_receipt(service, monkeypatch):
    c = card(service, questions=[question("Choose?")])
    original = service.op_approval_respond
    def interrupted(args, peer):
        original(args, peer)
        raise RuntimeError("process died before receipt")
    monkeypatch.setattr(service, "op_approval_respond", interrupted)
    with pytest.raises(RuntimeError):
        reply(service, c, "One", 200)
    monkeypatch.setattr(service, "op_approval_respond", original)
    assert reply(service, c, "One", 200)["duplicate"]
    assert len(c.calls) == 1
    assert len(service.store.query("SELECT * FROM messages")) == 1


def test_pending_question_reply_recovers_before_response(service, monkeypatch):
    c = card(service, questions=[question("Choose?")])
    original = service.op_approval_respond
    monkeypatch.setattr(service, "op_approval_respond", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        reply(service, c, "One", 201)
    monkeypatch.setattr(service, "op_approval_respond", original)
    assert reply(service, c, "One", 201)["route"] == "answer"
    assert len(c.calls) == 1


@pytest.mark.parametrize("retry_update_id", [None, 202])
def test_final_question_reply_without_update_id_can_resume_same_answer(service, monkeypatch, retry_update_id):
    c = card(service, questions=[question("First?"), question("Second?")])
    tap(service, c, "q0o0")
    original = service.op_approval_respond
    monkeypatch.setattr(service, "op_approval_respond", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        reply(service, c, "Free final answer", None)
    monkeypatch.setattr(service, "op_approval_respond", original)
    with pytest.raises(ConversationError, match="retry that same answer"):
        reply(service, c, "Different final answer", retry_update_id)
    assert reply(service, c, "Free final answer", retry_update_id)["route"] == "answer"
    assert c.calls[0][3] == {"First?": "One", "Second?": "Free final answer"}
    assert len(c.calls) == 1
    assert len(service.store.query("SELECT * FROM messages")) == 1


def test_reply_duplicate_conflict_and_queue_behind_block(service):
    c = card(service, approval=False)
    service.store.update_conversation(c.cid, blocked_by="delivery-unknown")
    first = reply(service, c, "Please inspect", 300)
    assert first["route"] == "queued" and first["blocked_by"] == "delivery-unknown"
    assert reply(service, c, "Please inspect", 300)["message_id"] == first["message_id"]
    with pytest.raises(ConversationError, match="different text"):
        reply(service, c, "Different text", 300)
    assert service.store.conversation(c.cid)["blocked_by"] == "delivery-unknown"
    assert len(service.store.query("SELECT * FROM messages")) == 2
    assert service.store.events_after(c.cid, 0)["events"][0]["data"]["source"] == "phone"
    assert not service.store.query("SELECT * FROM attempt_marks")


def test_reply_submit_recovers_before_receipt(service, monkeypatch):
    c = card(service, approval=False)
    original = service.op_message_submit
    def interrupted(args, peer):
        original(args, peer)
        raise RuntimeError("process died before receipt")
    monkeypatch.setattr(service, "op_message_submit", interrupted)
    with pytest.raises(RuntimeError):
        reply(service, c, "Next task", 400)
    monkeypatch.setattr(service, "op_message_submit", original)
    assert reply(service, c, "Next task", 400)["created"] is False
    assert len(service.store.query("SELECT * FROM messages")) == 2


def test_queued_reply_replay_recovers_original_settings_without_resubmitting(service, monkeypatch):
    c = card(service, approval=False)
    original, accepted = service.op_message_submit, []
    def interrupted(args, peer):
        accepted.append(original(args, peer))
        raise RuntimeError("process died before receipt")
    monkeypatch.setattr(service, "op_message_submit", interrupted)
    with pytest.raises(RuntimeError):
        reply(service, c, "Next task", 401)
    service.store.update_conversation(c.cid, settings={"model": "opus", "permission": "read-only", "effort": "high"})
    monkeypatch.setattr(service, "op_message_submit", lambda *a: pytest.fail("accepted reply must not be resubmitted"))
    receipt = reply(service, c, "Next task", 401)
    assert receipt["duplicate"] and receipt["created"] is False
    assert receipt["message_id"] == accepted[0]["message_id"]
    assert receipt["settings"] == accepted[0]["settings"]
    assert service.store.conversation(c.cid)["settings"]["permission"] == "read-only"
    assert len(service.store.query("SELECT * FROM messages")) == 2


@pytest.mark.parametrize("different_conversation", [False, True])
def test_queued_reply_without_receipt_still_rejects_conflicting_update(service, monkeypatch, different_conversation):
    c = card(service, approval=False)
    original = service.op_message_submit
    def interrupted(args, peer):
        original(args, peer)
        raise RuntimeError("process died before receipt")
    monkeypatch.setattr(service, "op_message_submit", interrupted)
    with pytest.raises(RuntimeError):
        reply(service, c, "Original text", 402)
    monkeypatch.setattr(service, "op_message_submit", lambda *a: pytest.fail("accepted update must not be resubmitted"))
    target = card(service, approval=False, token="another-card", mid=102) if different_conversation else c
    with pytest.raises(ConversationError, match="different content"):
        reply(service, target, "Original text" if different_conversation else "Changed text", 402)
    assert not service.store.query("SELECT * FROM phone_replies")
    assert reply(service, c, "Original text", 402)["duplicate"]


def test_reply_without_update_id_is_a_new_intent(service):
    c = card(service, approval=False)
    assert reply(service, c, update_id=None)["message_id"] != reply(service, c, update_id=None)["message_id"]


def test_reply_retries_if_app_queues_between_predecessor_read_and_submit(service, monkeypatch):
    c = card(service, approval=False)
    original, calls = service.op_message_submit, []
    def raced(args, peer):
        calls.append(args)
        if len(calls) == 1:
            original({**args, "message_id": str(uuid.uuid4()), "text": "From the app"}, peer)
        return original(args, peer)
    monkeypatch.setattr(service, "op_message_submit", raced)
    assert reply(service, c)["route"] == "queued"
    assert len(calls) == 2
    messages = service.store.query("SELECT message_id FROM messages ORDER BY seq")
    assert len(messages) == 3
    assert calls[-1]["after_message_id"] == messages[-2]["message_id"]


@pytest.mark.parametrize("field,value", [("telegram_message_id", -1), ("telegram_message_id", True),
    ("telegram_message_id", 2**65), ("text", " "), ("text", None), ("update_id", "other")])
def test_invalid_reply(service, field, value):
    c = card(service, approval=False)
    args = {"telegram_message_id": c.telegram, "text": "Hi", "update_id": 1, field: value}
    with pytest.raises(ConversationError):
        phone.reply(service, args, 123)


def test_notify_requires_done_policy_and_can_cancel(service):
    c = card(service, approval=False)
    with pytest.raises(ConversationError, match="enable the done event"):
        phone.notify(service, {"conversation_id": c.cid}, 123)
    service.daemon.policy = {"phone": {"telegram": {"events": ["done"]}}}
    assert phone.notify(service, {"conversation_id": c.cid}, 123)["enabled"]
    assert service.store.one("SELECT * FROM phone_notify WHERE conversation_id=?", (c.cid,))
    assert not phone.notify(service, {"conversation_id": c.cid, "enabled": False}, 123)["enabled"]
    assert not service.store.query("SELECT * FROM phone_notify")
