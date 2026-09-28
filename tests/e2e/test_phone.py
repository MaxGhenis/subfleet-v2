"""C-31.1: patched CoS poller -> real CLI/socket/daemon -> fake provider and say.

When run inside a managed attempt, a test-only process census supplies the
gateway parent to the real peer judge. The production judge's marker/guardian
refusals have separate unit coverage. No production authentication is changed;
terminal runs retain real process inspection for the phone and app paths.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from test_conversations import Conversations, IN_A_SUBFLEET_ATTEMPT, pending_approval


REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")

# This observer belongs only to the isolated test daemon. The real judge still
# interprets the census and checks the exact gateway parent/script relationship.
PHONE_PEER_CENSUS = '''
if Path(sys.argv[0]).name == "subfleetd":
    from subfleet.conversations import service as phone_service
    from subfleet.conversations.peers import Proc, judge as inspect_phone_peer
    def phone_test_census(pid, **kwargs):
        gateway = kwargs["gateway_script"]
        parent = pid + 100000
        census = [Proc(pid, parent, "??", "subfleet phone"),
                  Proc(parent, 1, "??", f"{sys.executable} {gateway}")]
        return inspect_phone_peer(pid, chain=lambda unused: census,
                                  executable=lambda unused: sys.executable, **kwargs)
    phone_service.judge = phone_test_census
'''


class Phone:
    def __init__(self, e2e):
        self.e2e = e2e
        self.gateway = e2e.root / "cos"
        shutil.copytree(REPO / "tests/fixtures/phone/cos", self.gateway)
        applied = subprocess.run([
            "patch", "--batch", "-p1", "-i", str(REPO / "docs/desktop/phone/cos-tg-poller.patch")],
            cwd=self.gateway, capture_output=True, text=True, timeout=10)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        self.poller = self.gateway / "bin/tg-poller"
        self.transport = self.gateway / "api.jsonl"
        e2e.env.update({"SUBFLEET_FAKE_TURN_LOG": str(e2e.root / "turns.jsonl"),
                        "SUBFLEET_PHONE_POLLER": str(self.poller),
                        "SUBFLEET_SAY": str(self.gateway / "bin/say"),
                        "COS_HOME": str(self.gateway), "SAY_CHAT_ID": "42",
                        "SAY_TRANSPORT": f"file:{self.transport}",
                        "SAY_ARGV_FILE": str(self.gateway / "say-argv.jsonl"),
                        "SUBFLEET_BIN": str(Path(sys.executable).parent / "subfleet")})
        if IN_A_SUBFLEET_ATTEMPT:
            with (e2e.root / "observers/sitecustomize.py").open("a") as observer:
                observer.write(PHONE_PEER_CENSUS)
        e2e.start()
        self.conv = Conversations(e2e)

    def rows(self, sql, params=()):
        with sqlite3.connect(f"file:{self.e2e.root / 'conversations.sqlite3'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def api(self):
        return [json.loads(line) for line in self.transport.read_text().splitlines()] if self.transport.exists() else []

    def card(self, cid):
        def sent():
            rows = self.rows("SELECT * FROM phone_cards WHERE conversation_id=? "
                             "AND telegram_message_id IS NOT NULL ORDER BY created_at DESC", (cid,))
            return rows[0] if rows else None
        return self.e2e.until(sent, timeout=30)

    def rendered(self, card):
        # Only daemon cards have a keyboard and a Subfleet conversation heading.
        posts = [row["params"] for row in self.api() if row["method"] == "sendMessage"
                 and row["params"].get("text", "").startswith("Subfleet ·")]
        return next(post for post in posts if card["token"] in json.dumps(post.get("reply_markup")))

    def button(self, card, label):
        return next(button["callback_data"] for row in self.rendered(card)["reply_markup"]["inline_keyboard"]
                    for button in row if button["text"] == label)

    def poll(self, update):
        path = self.gateway / "incoming.json"
        path.write_text(json.dumps([update]))
        result = subprocess.run([sys.executable, str(self.poller), "--once", "--updates", str(path)],
                                env=self.e2e.env, cwd=self.gateway, text=True,
                                capture_output=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        result = json.loads(result.stdout.strip())["result"]
        assert "could not" not in result, result
        return result

    def tap(self, card, label, *, update_id=100):
        return self.poll({"update_id": update_id, "callback_query": {
            "id": f"cb-{update_id}", "data": self.button(card, label),
            "message": {"chat": {"id": 42}, "message_id": card["telegram_message_id"]}}})

    def reply(self, card, text, *, update_id=200):
        return self.poll({"update_id": update_id, "message": {
            "chat": {"id": 42}, "date": 1, "text": text,
            "reply_to_message": {"message_id": card["telegram_message_id"]}}})

    def edited(self, card):
        def found():
            return next((row["params"] for row in self.api() if row["method"] == "editMessageText"
                         and row["params"]["message_id"] == card["telegram_message_id"]
                         and "Resolved:" in row["params"]["text"]), None)
        edited = self.e2e.until(found, timeout=30)
        assert edited["reply_markup"] == {"inline_keyboard": []}
        return edited

    def app(self, op, **args):
        # Under the isolated synthetic census, use the same raw app protocol
        # operation. Terminal runs use the existing real controlling-tty path.
        return self.conv.request(op, **args) if IN_A_SUBFLEET_ATTEMPT else self.conv.as_person(op, **args)


@pytest.fixture
def phone(e2e):
    return Phone(e2e)


def test_phone_approval_and_queued_reply_reach_provider_once(phone):
    conv = phone.conv
    cid = conv.create(title="Phone build")
    mid = conv.submit(cid, "run something [fake:approval]")
    conv.until_state(mid, "approval-needed")
    card = phone.card(cid)
    text = phone.rendered(card)["text"]
    assert "Phone build" in text and "Lane: claude-" in text and "Account:" in text
    assert "unassigned" not in text

    # A live turn here has no steer support; both the receipt and stored message
    # truthfully say queued, and Telegram redelivery cannot duplicate that turn.
    assert "Reply queued." in phone.reply(card, "Continue with the follow-up", update_id=200)
    assert "Already recorded." in phone.reply(card, "Continue with the follow-up", update_id=200)
    receipts = phone.rows("SELECT * FROM phone_replies WHERE update_id='200'")
    assert len(receipts) == 1
    followup = receipts[0]["message_id"]
    assert conv.message(followup)["state"] == "queued"
    assert "Decision recorded." in phone.tap(card, "Allow")
    assert "Already recorded." in phone.tap(card, "Allow", update_id=101)
    assert conv.until_state(mid, "complete", "failed")["state"] == "complete"
    assert conv.until_state(followup, "complete", "failed")["state"] == "complete"
    assert "Resolved: Allow" in phone.edited(card)["text"]

    controls = [row for row in conv.stdin_rows() if row.get("type") == "control_response"]
    assert len(controls) == 1
    body = controls[0]["response"]["response"]
    assert body["behavior"] == "allow"
    assert body["updatedInput"] == {"command": "echo approved-by-person", "description": "Say hello"}
    assert len([row for row in conv.stdin_rows() if row.get("uuid") == followup]) == 1
    events = conv.events(cid)
    for kind in ("phone.approval.responded", "phone.message.submitted"):
        matching = [event for event in events if event["kind"] == kind]
        assert len(matching) == 1 and matching[0]["data"]["source"] == "phone"
    assert len(phone.rows("SELECT * FROM phone_cards")) == 1


@pytest.mark.parametrize("answer", ["button", "reply"])
def test_phone_question_option_and_free_text_use_normal_approval_response(phone, answer):
    conv = phone.conv
    cid = conv.create(title="Choose a color")
    mid = conv.submit(cid, "[fake:question]")
    conv.until_state(mid, "approval-needed")
    card = phone.card(cid)
    assert "Reply to answer" in phone.rendered(card)["text"]
    if answer == "button":
        expected = "Blue"
        assert "Answer recorded." in phone.tap(card, "Blue")
        assert "Already recorded." in phone.tap(card, "Blue", update_id=101)
    else:
        expected = "Cobalt, please"
        assert "Answer recorded." in phone.reply(card, expected)
        assert "Already recorded." in phone.reply(card, expected)
    assert conv.until_state(mid, "complete", "failed")["state"] == "complete"
    phone.edited(card)
    controls = [row for row in conv.stdin_rows() if row.get("type") == "control_response"]
    assert len(controls) == 1
    assert controls[0]["response"]["response"]["updatedInput"]["answers"] == {"Which color?": expected}
    assert [event for event in conv.events(cid) if event["kind"] == "phone.approval.responded"][0]["data"]["source"] == "phone"


def test_app_approval_response_edits_its_existing_phone_card(phone):
    conv = phone.conv
    cid = conv.create(title="Answer at the desktop")
    mid = conv.submit(cid, "[fake:approval]")
    approval = pending_approval(conv, cid)
    card = phone.card(cid)
    shown = phone.app("approval.get", approval_id=approval["approval_id"])
    assert shown["ok"], shown
    detail = shown["result"]
    response = phone.app("approval.respond", approval_id=approval["approval_id"], decision="deny",
                         nonce=detail["nonce"], request_sha256=detail["request_sha256"])
    assert response["ok"], response
    assert conv.until_state(mid, "complete", "failed")["state"] == "complete"
    assert "Resolved: Deny" in phone.edited(card)["text"]
    assert len(phone.rows("SELECT * FROM phone_cards")) == 1


def test_ordinary_completion_sends_no_card(phone):
    conv = phone.conv
    cid = conv.create(title="Ordinary work")
    first = conv.submit(cid, "A normal turn")
    assert conv.until_state(first, "complete", "failed")["state"] == "complete"
    # A later approval proves the asynchronous bridge has reconciled after the
    # ordinary completion, without relying on an arbitrary sleep.
    second = conv.submit(cid, "[fake:approval]", after_message_id=first)
    card = phone.card(cid)
    assert card["message_id"] == second
    assert [row["kind"] for row in phone.rows("SELECT kind FROM phone_cards")] == ["approvals"]
    posts = [row for row in phone.api() if row["method"] == "sendMessage"]
    assert len(posts) == 1 and "Approval needed" in posts[0]["params"]["text"]
