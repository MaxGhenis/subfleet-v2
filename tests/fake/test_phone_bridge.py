"""Phone workflow across real subprocess/CLI/socket/store/say boundaries.

The scheduler/guardian and peer census are explicit test doubles, so this runs
without macOS process inspection. A tiny provider subprocess emits real Claude
approval frames and receives responses produced by the real ClaudeTurn driver.
The broader real-process cases live in tests/e2e/test_phone.py.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

import pytest

from subfleet import protocol
from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from subfleet.conversations.turn import TurnSpec
from tests.unit.conftest import FakeDaemon as SocketServer


REPO = Path(__file__).resolve().parents[2]
SETTINGS = {"model": "opus", "permission": "ask"}
PROVIDER = '''import json, pathlib, sys
mode, record = sys.argv[1:]
if mode == "normal":
    value = json.loads(sys.stdin.readline())
    pathlib.Path(record).write_text(json.dumps(value))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}), flush=True)
else:
    body = {"subtype": "can_use_tool", "tool_name": "Bash",
            "input": {"command": "echo approved-by-person"}}
    if mode == "question":
        body.update(tool_name="AskUserQuestion", input={"questions": [
            {"question": "Which color?", "multiSelect": False,
             "options": [{"label": "Blue"}, {"label": "Red"}]}]})
    print(json.dumps({"type": "control_request", "request_id": "fixture-request", "request": body}), flush=True)
    received = []
    for line in sys.stdin:
        received.append(json.loads(line))
        pathlib.Path(record).write_text(json.dumps(received))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}), flush=True)
'''


class FakeProviderRunner:
    def __init__(self, service, cid, mid, mode, root):
        self.service, self.mid = service, mid
        self.record = root / f"provider-{mid}.json"
        self.process = subprocess.Popen([sys.executable, "-u", "-c", PROVIDER, mode, str(self.record)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, cwd=root)
        self.driver = ClaudeTurn(TurnSpec(provider="claude", message_id=mid, text="fixture task",
            model_id="opus", permission="ask", native_session_id=None, new_session_id=str(uuid.uuid4())),
            read_bytes=lambda path: Path(path).read_bytes())
        offered = self.driver.feed(self.process.stdout.readline(), 0).approvals
        assert len(offered) == 1
        item = offered[0]
        self.approval = service.store.add_approval(message_id=mid, conversation_id=cid,
            attempt_id=mid + "/a1", provider_request_id=item.provider_request_id, kind=item.kind,
            request=item.request, display=item.summary, options=item.options)[0]
        service.store.set_state(mid, "approval-needed")
        service.runners[mid + "/a1"] = self

    def respond(self, request_id, decision, message, answers):
        step = self.driver.respond(request_id, decision, message, answers)
        for frame in step.frames:
            assert frame.op == "write"
            self.process.stdin.write(frame.line + "\n")
        self.process.stdin.flush()
        completed = self.driver.feed(self.process.stdout.readline(), 1000)
        assert completed.outcome.state == "complete"
        self.service.store.set_state(self.mid, completed.outcome.state)

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


class Bridge:
    def __init__(self, root, monkeypatch):
        self.root, self.runners = root, []
        gateway = root / "cos"
        shutil.copytree(REPO / "tests/fixtures/phone/cos", gateway)
        applied = subprocess.run(["patch", "--batch", "-p1", "-i",
            str(REPO / "docs/desktop/phone/cos-tg-poller.patch")], cwd=gateway,
            capture_output=True, text=True, timeout=10)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        self.poller, self.api_file = gateway / "bin/tg-poller", gateway / "api.jsonl"
        variables = {"SUBFLEET_HOME": str(root), "SUBFLEET_SAY": str(gateway / "bin/say"),
                     "COS_HOME": str(gateway), "SAY_TRANSPORT": f"file:{self.api_file}", "SAY_CHAT_ID": "42",
                     "SUBFLEET_BIN": str(Path(sys.executable).parent / "subfleet")}
        for key, value in variables.items():
            monkeypatch.setenv(key, value)
        self.env = {**os.environ, "PYTHONPATH": str(REPO)}
        for key in ("SUBFLEET_ATTEMPT", "SUBFLEET_JOB", "SUBFLEET_ROOT", "SAY_RESULT", "SAY_RETURN_CODE"):
            self.env.pop(key, None)
        daemon = SimpleNamespace(root=root, log=logging.getLogger("phone-e2e"), requests=None,
                                 policy={}, _notify=lambda: None)
        self.service = ConversationService(daemon)
        monkeypatch.setattr(self.service, "_person", lambda peer, what: SimpleNamespace(
            pid=123, reason="fake owner gateway peer (test only)"))

        def dispatch(request):
            try:
                return self.service.handle(request.op, request.args, 123)
            except ConversationError as error:
                return protocol.fail(request.id, error.code, str(error), error.fix)

        self.socket = SocketServer(root, {op: dispatch for op in protocol.CONVERSATION_OPS})

    def seed(self, mode="tool"):
        store = self.service.store
        conversation = store.create_conversation(provider="claude", workspace=str(self.root),
            workspace_kind="in-place", settings=SETTINGS, origin="new", title="Phone fixture")[0]
        cid, mid = conversation["conversation_id"], str(uuid.uuid4())
        store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None,
                             text="Please finish", attachments=[], settings=SETTINGS)
        store.update_message(mid, served={"lane_id": "claude-test", "account": "owner@example.test"})
        runner = FakeProviderRunner(self.service, cid, mid, mode, self.root)
        self.runners.append(runner)
        self.service.phone.reconcile()
        card = store.one("SELECT * FROM phone_cards WHERE message_id=?", (mid,))
        assert card["state"] == "sent" and card["telegram_message_id"] > 0
        return cid, mid, runner, card

    def calls(self):
        return [json.loads(line) for line in self.api_file.read_text().splitlines()] if self.api_file.exists() else []

    def action(self, card, label):
        return next(button["callback_data"] for call in self.calls() if call["method"] == "sendMessage"
                    for row in call["params"].get("reply_markup", {}).get("inline_keyboard", [])
                    for button in row if button["text"] == label and card["token"] in button["callback_data"])

    def poll(self, card, *, label=None, text=None, update_id=100):
        if label:
            update = {"update_id": update_id, "callback_query": {"id": f"cb-{update_id}",
                "data": self.action(card, label),
                "message": {"chat": {"id": 42}, "message_id": card["telegram_message_id"]}}}
        else:
            update = {"update_id": update_id, "message": {"chat": {"id": 42}, "date": 1, "text": text,
                "reply_to_message": {"message_id": card["telegram_message_id"]}}}
        path = self.root / "updates.json"
        path.write_text(json.dumps([update]))
        result = subprocess.run([sys.executable, str(self.poller), "--once", "--updates", str(path)],
                                env=self.env, cwd=self.root, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        note = json.loads(result.stdout)["result"]
        assert "could not" not in note, note
        self.service.phone.reconcile()
        return note

    def process_queued(self, mid):
        store = self.service.store
        message = store.message(mid)
        assert message["state"] == "queued"
        record = self.root / f"normal-{mid}.json"
        payload = {"message_id": mid, "text": store.message_text(message)}
        completed = subprocess.run([sys.executable, "-c", PROVIDER, "normal", str(record)],
                                   input=json.dumps(payload) + "\n", text=True, capture_output=True,
                                   timeout=10, cwd=self.root)
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["subtype"] == "success"
        store.set_state(mid, "complete")
        self.service.phone.reconcile()
        return json.loads(record.read_text())

    def close(self):
        self.socket.close()
        self.service.runners.clear()
        for runner in self.runners:
            runner.close()
        self.service.close()


@pytest.fixture
def bridge(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="sf-phone-", dir="/tmp") as directory:
        harness = Bridge(Path(directory), monkeypatch)
        try:
            yield harness
        finally:
            harness.close()


def test_real_phone_cli_approval_reply_and_duplicate_update_workflow(bridge):
    cid, mid, runner, card = bridge.seed()
    posted = bridge.calls()[0]["params"]
    assert "Phone fixture" in posted["text"] and "claude-test" in posted["text"]
    assert "owner@example.test" in posted["text"]
    assert "Reply queued." in bridge.poll(card, text="Follow up", update_id=200)
    assert "Already recorded." in bridge.poll(card, text="Follow up", update_id=200)
    receipt = bridge.service.store.one("SELECT * FROM phone_replies WHERE update_id='200'")
    assert bridge.service.store.message(receipt["message_id"])["state"] == "queued"
    assert "Decision recorded." in bridge.poll(card, label="Allow")
    assert "Already recorded." in bridge.poll(card, label="Allow", update_id=101)
    responses = json.loads(runner.record.read_text())
    assert len(responses) == 1
    assert responses[0]["response"]["response"] == {
        "behavior": "allow", "updatedInput": {"command": "echo approved-by-person"}}
    assert bridge.service.store.message(mid)["state"] == "complete"
    assert bridge.process_queued(receipt["message_id"])["text"] == "Follow up"
    edits = [call for call in bridge.calls() if call["method"] == "editMessageText"]
    assert len(edits) == 1
    assert edits[0]["params"]["reply_markup"] == {"inline_keyboard": []}
    assert "Resolved: Allow" in edits[0]["params"]["text"]
    assert len(bridge.service.store.query("SELECT * FROM phone_cards")) == 1
    events = bridge.service.store.events_after(cid, 0)["events"]
    assert {event["kind"] for event in events} >= {"phone.approval.responded", "phone.message.submitted"}
    assert all(event["data"]["source"] == "phone" for event in events)


@pytest.mark.parametrize("answer", ["button", "text"])
def test_real_phone_cli_answers_questions_and_edits_card(bridge, answer):
    _, mid, runner, card = bridge.seed("question")
    assert "Reply to answer" in bridge.calls()[0]["params"]["text"]
    kwargs, expected = ({"label": "Blue"}, "Blue") if answer == "button" else ({"text": "Cobalt"}, "Cobalt")
    assert "Answer recorded." in bridge.poll(card, **kwargs)
    assert "Already recorded." in bridge.poll(card, **kwargs)
    responses = json.loads(runner.record.read_text())
    assert len(responses) == 1
    assert responses[0]["response"]["response"]["updatedInput"]["answers"] == {"Which color?": expected}
    assert bridge.service.store.message(mid)["state"] == "complete"
    assert [call for call in bridge.calls() if call["method"] == "editMessageText"][-1]["params"]["reply_markup"] == {"inline_keyboard": []}


def test_desktop_answer_edits_phone_and_ordinary_completion_is_silent(bridge):
    _, mid, runner, card = bridge.seed()
    approval = runner.approval
    bridge.service.handle("approval.respond", {"approval_id": approval["approval_id"],
        "decision": "deny", "nonce": approval["nonce"], "request_sha256": approval["request_sha256"]}, 123)
    bridge.service.phone.reconcile()
    edits = [call for call in bridge.calls() if call["method"] == "editMessageText"]
    assert len(edits) == 1 and "Resolved: Deny" in edits[0]["params"]["text"]
    assert bridge.service.store.message(mid)["state"] == "complete"
    # The existing completion and a subsequent ordinary turn are both silent.
    conversation = bridge.service.store.conversation(card["conversation_id"])
    next_id = str(uuid.uuid4())
    bridge.service.handle("message.submit", {"conversation_id": conversation["conversation_id"],
        "message_id": next_id, "after_message_id": mid, "text": "ordinary work"}, 123)
    assert bridge.process_queued(next_id)["text"] == "ordinary work"
    assert len([call for call in bridge.calls() if call["method"] == "sendMessage"]) == 1
