"""C-31.1: only needs-you events reach say; card delivery receipts are durable."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from subfleet import phone
from subfleet.conversations.store import ConversationStore, utcnow

SETTINGS = {"model": "opus", "effort": "high", "fast": False, "permission": "ask"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "state")
    service = SimpleNamespace(store=store, daemon=SimpleNamespace(policy={}, store=None),
                              log=logging.getLogger("phone-test"))
    bridge = phone.PhoneBridge(service, say="/never-a-real-say")
    calls = []

    def say(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "edited" if "--edit" in argv else f"sent message_id {1000 + len(calls)}", "")

    monkeypatch.setattr(phone, "run_say", say)
    conversation = store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                             settings=SETTINGS, origin="new", title="Ship a phone path")[0]
    message = store.submit_message(conversation_id=conversation["conversation_id"], message_id=str(uuid.uuid4()),
                                   after_message_id=None, text="Please finish the phone path", attachments=[],
                                   settings=SETTINGS)[0]
    store.update_message(message["message_id"], served={"lane_id": "claude-work", "account": "max@example.test"})
    yield SimpleNamespace(store=store, service=service, bridge=bridge, calls=calls, conversation=conversation, message=message)
    bridge.close()
    store.close()


def approval(s, *, kind="command", options=("allow", "allow-session", "deny"), display=None):
    return s.store.add_approval(message_id=s.message["message_id"], conversation_id=s.conversation["conversation_id"],
                                attempt_id="attempt-1", provider_request_id=str(uuid.uuid4()), kind=kind,
                                request={"secret": "do not copy the raw request"},
                                display=display or {"command": "python -m pytest"}, options=options)[0]


def card(s):
    return s.store.one("SELECT * FROM phone_cards ORDER BY created_at LIMIT 1")


def markup(argv):
    return json.loads(argv[argv.index("--markup") + 1])


def test_defaults_and_disabled():
    assert phone.enabled({})
    assert phone.settings({})["events"] == ["approvals", "questions", "blocks"]
    assert not phone.enabled({"phone": {"telegram": {"enabled": False}}})


def test_ordinary_completion_is_silent(setup):
    setup.store.set_state(setup.message["message_id"], "complete")
    setup.bridge.reconcile()
    assert setup.calls == []
    assert card(setup) is None


def test_approval_card_records_offered_decisions_context_and_mapping(setup):
    ap = approval(setup)
    setup.bridge.reconcile()
    argv = setup.calls[0]
    assert argv[argv.index("--class") + 1] == "decide"
    assert "--urgent" not in argv
    assert "Ship a phone path" in argv[-1]
    assert "claude-work" in argv[-1] and "max@example.test" in argv[-1]
    assert "python -m pytest" in argv[-1]
    assert "do not copy the raw request" not in argv[-1] and ap["nonce"] not in argv[-1]
    buttons = [row[0] for row in markup(argv)["inline_keyboard"]]
    assert [button["text"] for button in buttons] == ["Allow", "Allow for session", "Deny"]
    saved = card(setup)
    assert saved["approval_id"] == ap["approval_id"] and saved["telegram_message_id"] == 1001
    assert saved["state"] == "sent"
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)
    assert argv[argv.index("--key") + 1] == "sf:" + saved["token"]


def test_durable_send_claim_precedes_gateway(setup, monkeypatch):
    approval(setup)

    def say(argv, timeout):
        assert card(setup)["state"] == "sending"
        assert card(setup)["telegram_message_id"] is None
        return subprocess.CompletedProcess(argv, 0, "sent message_id 401", "")

    monkeypatch.setattr(phone, "run_say", say)
    setup.bridge.reconcile()
    assert card(setup)["telegram_message_id"] == 401


def test_card_survives_bridge_restart_without_duplicate(setup):
    approval(setup)
    setup.bridge.reconcile()
    first = card(setup)
    setup.bridge.close()
    other = phone.PhoneBridge(setup.service, say="/fake/say")
    try:
        other.reconcile()
        assert len(setup.calls) == 1 and card(setup) == first
    finally:
        other.close()


def test_unknown_send_is_not_retried(setup, monkeypatch):
    approval(setup)

    def say(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(phone, "run_say", say)
    setup.bridge.reconcile()
    assert card(setup)["state"] == "unknown"
    monkeypatch.setattr(phone, "run_say", lambda *a: pytest.fail("ambiguous card must not be resent"))
    setup.bridge.reconcile()


def test_interrupted_claim_is_unknown_after_restart(setup, monkeypatch):
    ap = approval(setup)
    setup.bridge._create("approval:" + ap["approval_id"], "approvals", setup.conversation["conversation_id"],
                         setup.message["message_id"], approval=ap)
    with setup.store.transaction() as tx:
        tx.execute("UPDATE phone_cards SET state='sending'")
    monkeypatch.setattr(phone, "run_say", lambda *a: pytest.fail("prior send may have succeeded"))
    setup.bridge.reconcile()
    assert card(setup)["state"] == "unknown"


@pytest.mark.parametrize("output,state", [("held for the next morning card (quiet hours)", "held"),
                                         ("deduped (same alert within 6h)", "deduped"),
                                         ("emailed fallback", "emailed"), ("sent message_id 0", "unknown"),
                                         ("telegram failed, emailed instead", "emailed"),
                                         ("sent message_id 7\nsent message_id 8", "unknown"), ("failed", "unknown")])
def test_non_telegram_receipts_never_invent_message_ids(setup, monkeypatch, output, state):
    setup.store.update_conversation(setup.conversation["conversation_id"], blocked_by="delivery-unknown")
    calls = []

    def say(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(phone, "run_say", say)
    setup.bridge.reconcile()
    assert card(setup)["state"] == state
    assert card(setup)["telegram_message_id"] is None
    assert calls[0][calls[0].index("--class") + 1] == "alert"
    setup.bridge.reconcile()
    assert len(calls) == 1


@pytest.mark.parametrize("state,decision,label", [("answered", {"decision": "allow-session"}, "Allow for session"),
                                                ("answered", {"decision": "deny"}, "Deny"),
                                                ("withdrawn", None, "Withdrawn")])
def test_app_resolution_edits_card_even_after_notifications_disabled(setup, state, decision, label):
    ap = approval(setup)
    setup.bridge.reconcile()
    if state == "answered":
        setup.store.answer_approval(ap["approval_id"], decision)
    else:
        setup.store.withdraw_approvals(attempt_id=ap["attempt_id"])
    setup.service.daemon.policy = {"phone": {"telegram": {"enabled": False}}}
    setup.bridge.reconcile()
    assert setup.calls[1][1:3] == ["--edit", "1001"]
    assert label in setup.calls[1][-1]
    assert markup(setup.calls[1]) == {"inline_keyboard": []}
    assert card(setup)["state"] == "resolved"
    setup.bridge.reconcile()
    assert len(setup.calls) == 2


def test_edit_failure_retries_without_resending(setup, monkeypatch):
    ap = approval(setup)
    setup.bridge.reconcile()
    setup.store.answer_approval(ap["approval_id"], {"decision": "deny"})
    original = phone.run_say
    monkeypatch.setattr(phone, "run_say", lambda argv, timeout: subprocess.CompletedProcess(argv, 1, "edit failed", ""))
    setup.bridge.reconcile()
    assert card(setup)["state"] == "sent" and card(setup)["error"] == "edit failed"
    monkeypatch.setattr(phone, "run_say", original)
    setup.bridge.reconcile()
    assert card(setup)["state"] == "resolved"
    assert all("--edit" in argv for argv in setup.calls[1:])


def test_question_buttons_follow_persisted_progress(setup):
    ap = approval(setup, kind="question", options=("answer", "deny"), display={"questions": [
        {"question": "Choose components", "multiSelect": True, "options": [{"label": "CLI"}, {"label": "Daemon"}]},
        {"question": "Choose launch", "options": [{"label": "Now"}, {"label": "Later"}]}]})
    setup.bridge.reconcile()
    labels = [row[0]["text"] for row in markup(setup.calls[0])["inline_keyboard"]]
    assert labels == ["CLI", "Daemon", "Finish", "Deny"]
    assert "Reply to answer" in setup.calls[0][-1]
    saved = card(setup)
    progress = json.loads(saved["progress_json"])
    progress.update(index=1, answers={"Choose components": "CLI"})
    with setup.store.transaction() as tx:
        tx.execute("UPDATE phone_cards SET progress_json=? WHERE token=?", (json.dumps(progress), saved["token"]))
    setup.bridge.reconcile()
    assert "Question 2/2" in setup.calls[1][-1]
    assert [row[0]["text"] for row in markup(setup.calls[1])["inline_keyboard"]] == ["Now", "Later", "Deny"]
    setup.store.answer_approval(ap["approval_id"], {"decision": "answer", "answers": {
        "Choose components": "CLI", "Choose launch": "Later"}})
    setup.bridge.reconcile()
    assert "Resolved: Answered" in setup.calls[2][-1] and "Later" in setup.calls[2][-1]
    assert markup(setup.calls[2]) == {"inline_keyboard": []}


def test_policy_selects_event_kinds(setup):
    setup.service.daemon.policy = {"phone": {"telegram": {"events": ["questions"]}}}
    approval(setup)
    setup.store.update_conversation(setup.conversation["conversation_id"], blocked_by="unfinished-turn")
    setup.bridge.reconcile()
    assert setup.calls == []
    approval(setup, kind="question", options=("answer",), display={"questions": [{"question": "Your answer?"}]})
    setup.bridge.reconcile()
    assert len(setup.calls) == 1


def test_disabled_pending_cards_do_not_starve_enabled_events(setup):
    for _ in range(phone.BATCH_SIZE + 1):
        approval(setup)
    setup.bridge._discover(set(phone.DEFAULT_EVENTS))
    setup.service.daemon.policy = {"phone": {"telegram": {"events": ["questions"]}}}
    approval(setup, kind="question", options=("answer",), display={"questions": [{"question": "Your answer?"}]})
    setup.bridge.reconcile()
    assert len(setup.calls) == 1 and "Your answer?" in setup.calls[0][-1]


def test_block_resolved_before_send_stays_silent(setup):
    cid = setup.conversation["conversation_id"]
    setup.store.update_conversation(cid, blocked_by="unfinished-turn")
    setup.bridge._discover({"blocks"})
    setup.store.update_conversation(cid, blocked_by=None)
    setup.bridge.reconcile()
    assert setup.calls == [] and card(setup)["state"] == "resolved"


@pytest.mark.parametrize("reason", ["handoff:req-123", "capacity", "external-writer", "legacy-owner"])
def test_transient_or_automatic_blocks_are_silent(setup, reason):
    setup.store.update_conversation(setup.conversation["conversation_id"], blocked_by=reason)
    setup.bridge.reconcile()
    assert setup.calls == []


def test_reply_queued_behind_unchanged_block_does_not_send_another_alert(setup):
    setup.store.set_state(setup.message["message_id"], "delivery-unknown")
    setup.store.update_conversation(setup.conversation["conversation_id"], blocked_by="delivery-unknown")
    setup.bridge.reconcile()
    setup.store.submit_message(conversation_id=setup.conversation["conversation_id"], message_id=str(uuid.uuid4()),
                               after_message_id=setup.message["message_id"], text="A queued reply", attachments=[], settings=SETTINGS)
    setup.bridge.reconcile()
    assert len(setup.calls) == 1


def test_done_needs_explicit_subscription_and_event_policy(setup):
    cid = setup.conversation["conversation_id"]
    with setup.store.transaction() as tx:
        tx.execute("INSERT INTO phone_notify VALUES (?,?,?)", (cid, 0, utcnow()))
    setup.store.set_state(setup.message["message_id"], "complete")
    setup.bridge.reconcile()
    assert setup.calls == [] and setup.store.one("SELECT * FROM phone_notify")
    setup.service.daemon.policy = {"phone": {"telegram": {"events": ["done"]}}}
    setup.bridge.reconcile()
    assert len(setup.calls) == 1
    assert setup.calls[0][setup.calls[0].index("--class") + 1] == "requested"
    assert not setup.store.query("SELECT * FROM phone_notify")
    setup.bridge.reconcile()
    assert len(setup.calls) == 1


def test_single_message_bound_and_secrets_scrubbed(setup):
    token = "sk-ant-api03-" + "a" * 80
    approval(setup, display={"command": "prefix " + token + " tail " + "long excerpt " * 1000})
    setup.bridge.reconcile()
    text = setup.calls[0][-1]
    assert token not in text and len(text) <= phone.CARD_MAX


def test_unicode_card_is_bounded_in_bytes(setup):
    approval(setup, display={"command": "🌍" * 2000})
    setup.bridge.reconcile()
    assert len(setup.calls[0][-1].encode("utf-8")) <= phone.CARD_MAX


def test_nonzero_gateway_does_not_create_owned_id(setup, monkeypatch):
    approval(setup)
    monkeypatch.setattr(phone, "run_say", lambda argv, timeout:
                        subprocess.CompletedProcess(argv, 1, "sent message_id 5", "failed after output"))
    setup.bridge.reconcile()
    assert card(setup)["telegram_message_id"] is None and card(setup)["state"] == "unknown"


def test_live_attempt_lane_overrides_preferred_lane(setup):
    setup.store.update_message(setup.message["message_id"], job_id="job-1", served={})
    setup.store.update_conversation(setup.conversation["conversation_id"], lane_id="preferred")
    setup.service.daemon.store = SimpleNamespace(one=lambda sql, params:
        {"lane_id": "actual"} if "attempts" in sql else {"label": "actual@example.test", "account_key": "acct"})
    approval(setup)
    setup.bridge.reconcile()
    assert "actual@example.test" in setup.calls[0][-1] and "Lane: actual" in setup.calls[0][-1]
    assert "preferred" not in setup.calls[0][-1]


def test_tick_is_nonblocking_and_close_prevents_late_writes(setup, monkeypatch):
    approval(setup)
    entered, release = threading.Event(), threading.Event()

    def say(argv, timeout):
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(argv, 0, "sent message_id 9", "")

    monkeypatch.setattr(phone, "run_say", say)
    setup.bridge.tick()
    assert entered.wait(1)
    setup.bridge.tick()  # Returns while the first gateway is still in flight.
    closer = threading.Thread(target=setup.bridge.close)
    closer.start()
    release.set()
    closer.join(2)
    assert not closer.is_alive()
    before = card(setup)
    setup.bridge.tick()
    setup.bridge.reconcile()
    assert card(setup) == before and before["state"] == "sent" and before["telegram_message_id"] == 9


def test_say_timeout_is_bounded_and_does_not_wait_for_children(tmp_path):
    script = tmp_path / "say"
    script.write_text(f"#!{sys.executable}\nimport subprocess,time\nsubprocess.Popen(['{sys.executable}', '-c', 'import time; time.sleep(60)'])\ntime.sleep(60)\n")
    script.chmod(0o700)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        phone.run_say([str(script)], 0.1)
    assert time.monotonic() - started < 2


def test_schema_two_migrates_phone_tables_preserving_conversations(tmp_path):
    root = tmp_path / "state"
    store = ConversationStore(root)
    conversation = store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                             settings=SETTINGS, origin="new")[0]
    store.close()
    with sqlite3.connect(root / "conversations.sqlite3") as db:
        db.executescript("DROP TABLE phone_cards; DROP TABLE phone_replies; DROP TABLE phone_notify; "
                         "DELETE FROM schema_version; INSERT INTO schema_version VALUES (2,'prior');")
    store = ConversationStore(root)
    try:
        assert store.conversation(conversation["conversation_id"])["workspace"] == "/w"
        assert [r["version"] for r in store.query("SELECT version FROM schema_version ORDER BY version")] == [2, 3]
        assert store.query("SELECT * FROM phone_cards") == []
        assert store.query("SELECT * FROM phone_replies") == []
        assert store.query("SELECT * FROM phone_notify") == []
    finally:
        store.close()
