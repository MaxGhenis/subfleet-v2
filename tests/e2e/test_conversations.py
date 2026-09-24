"""Conversations end to end: the real daemon, guardian, relay, drivers and adapters,
with the fake interactive providers on PATH (C-24 to C-30, design §4 to §11).

Person-only operations (C-25.6) are sent from a process with a controlling
terminal, which `script(1)` provides, so the daemon's real peer check decides
them; nothing here stubs the check.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

import pytest

from subfleet.conversations.service import CONTINUATION_TEXT

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")

CLIENT = r'''
import json, socket, sys
request = json.loads(sys.argv[2])
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(30)
    client.connect(sys.argv[1])
    client.sendall((json.dumps(request) + "\n").encode())
    with client.makefile("rb") as stream:
        sys.stdout.write("RESPONSE " + stream.readline().decode())
'''


class Conversations:
    def __init__(self, e2e):
        self.e2e = e2e
        self.log = e2e.root / "turns.jsonl"

    def request(self, op: str, **args) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(60)
            client.connect(str(self.e2e.root / "daemon.sock"))
            client.sendall((json.dumps({"v": 1, "id": "t", "op": op, "args": args}) + "\n").encode())
            with client.makefile("rb") as stream:
                return json.loads(stream.readline())

    def call(self, op: str, **args) -> dict:
        response = self.request(op, **args)
        assert response["ok"], response
        return response["result"]

    def as_person(self, op: str, **args) -> dict:
        """The request from a process with a controlling terminal (C-25.6)."""
        request = json.dumps({"v": 1, "id": "p", "op": op, "args": args})
        out = subprocess.run(["/usr/bin/script", "-q", "/dev/null", sys.executable, "-c", CLIENT,
                              str(self.e2e.root / "daemon.sock"), request],
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        # script(1) may echo control characters before the child's own output.
        line = next((l for l in out.stdout.splitlines() if "RESPONSE " in l), None)
        assert line, (out.stdout, out.stderr)
        return json.loads(line.split("RESPONSE ", 1)[1])

    def as_agent(self, op: str, **args) -> dict:
        """The same request from a process with no terminal, as a headless agent's."""
        request = json.dumps({"v": 1, "id": "a", "op": op, "args": args})
        out = subprocess.run([sys.executable, "-c", CLIENT, str(self.e2e.root / "daemon.sock"), request],
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        return json.loads(out.stdout[len("RESPONSE "):])

    def create(self, provider="claude", model="opus[1m]", permission="ask", **extra) -> str:
        settings = {"model": model, "permission": permission, "effort": extra.pop("effort", None),
                    "fast": extra.pop("fast", False)}
        args = {"provider": provider, "request_id": str(uuid.uuid4()), "workspace": str(self.e2e.workdir),
                "settings": settings, **extra}
        return self.call("conversation.create", **args)["conversation"]["conversation_id"]

    def submit(self, cid: str, text: str, **extra) -> str:
        mid = str(uuid.uuid4())
        self.call("message.submit", conversation_id=cid, message_id=mid, text=text, **extra)
        return mid

    def message(self, mid: str) -> dict:
        return self.call("message.status", message_ids=[mid])["messages"][0]

    def until_state(self, mid: str, *states: str, timeout: float = 30) -> dict:
        def reached():
            message = self.message(mid)
            return message if message["state"] in states else None
        return self.e2e.until(reached, timeout=timeout)

    def attempt(self, mid: str, turn_seq: int = 0) -> dict:
        """The message's turn attempt once its job has finished (finalization is after the message settles)."""
        request = f"turn:{mid}:{turn_seq}"
        self.e2e.until(lambda: (self.e2e.rows("SELECT state FROM jobs WHERE request_id=?", (request,)) or [{}])[0]
                       .get("state") in ("succeeded", "failed", "cancelled", "lost"), timeout=30)
        return self.e2e.rows("SELECT a.* FROM attempts a JOIN jobs j USING(job_id) WHERE j.request_id=? "
                             "ORDER BY a.seq DESC LIMIT 1", (request,))[0]

    def events(self, cid: str) -> list[dict]:
        out, after = [], 0
        while True:
            page = self.call("conversation.events", conversation_id=cid, after=after)
            out += page["events"]
            if not page["events"]:
                return out
            after = page["next"]

    def turn_log(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def stdin_rows(self) -> list[dict]:
        """What turn processes read; the guard preflight's own app-server is left out."""
        log = self.turn_log()
        turns = {row["pid"] for row in log if "argv" in row
                 and ("--listen" in row["argv"] or "--input-format" in row["argv"])}
        return [json.loads(row["stdin"]) for row in log if "stdin" in row and row.get("pid") in turns]


@pytest.fixture
def conv(e2e):
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.start()
    return Conversations(e2e)


def test_a_claude_conversation_streams_completes_and_continues_in_the_same_session(conv):
    """C-24.1, C-24.4, C-25.5, C-26.1, C-26.5: a message becomes one turn job whose
    provider sees it once, with its id as the uuid; events stream; a follow-up
    resumes the same native session."""
    cid = conv.create()
    first = conv.submit(cid, "hello there")
    done = conv.until_state(first, "complete", "failed", "delivery-unknown")
    assert done["state"] == "complete", done
    kinds = [e["kind"] for e in conv.events(cid)]
    assert "accepted" in kinds and "text" in kinds and kinds[-1] == "turn.completed"
    text = [e for e in conv.events(cid) if e["kind"] == "text"][-1]["data"]["text"]
    assert text == "Fake Claude read 11 characters."

    conversation = conv.call("conversation.open", conversation_id=cid)["conversation"]
    session = conversation["native_session_id"]
    assert session and done["served"]["lane_id"].startswith("claude-")
    sent = [row for row in conv.stdin_rows() if row.get("type") == "user"]
    assert [row["uuid"] for row in sent] == [first]

    second = conv.submit(cid, "and again", after_message_id=first)
    assert conv.until_state(second, "complete", "failed", "delivery-unknown")["state"] == "complete"
    launches = [row["argv"] for row in conv.turn_log() if "argv" in row]
    assert launches[0][launches[0].index("--session-id") + 1] == session
    assert launches[1][launches[1].index("--resume") + 1] == session

    job = conv.e2e.rows("SELECT * FROM jobs WHERE request_id=?", (f"turn:{first}:0",))[0]
    assert job["kind"] == "turn" and job["state"] == "succeeded"
    models = json.loads((conv.e2e.root / "conversations" / "models.json").read_text())
    assert "opus[1m]" in models["claude"]["claude-opus-5-5"]["values"]


def pending_approval(conv, cid: str) -> dict:
    return conv.e2e.until(lambda: next(iter(conv.call("approval.list", conversation_id=cid)["approvals"]), None),
                          timeout=30)


def test_a_tool_approval_is_a_persons_decision_and_reaches_the_provider_once(conv):
    """C-25.6, C-27.1, C-27.2: the approval waits for a person; an agent's answer is
    refused; the person's answer is the provider's control response, input unchanged."""
    cid = conv.create()
    mid = conv.submit(cid, "run something [fake:approval]")
    conv.until_state(mid, "approval-needed")
    approval = pending_approval(conv, cid)
    assert approval["kind"] == "tool" and approval["display"]["tool"] == "Bash"
    assert approval["options"] == ["allow", "deny", "cancel-turn"]

    refused = conv.as_agent("approval.get", approval_id=approval["approval_id"])
    assert not refused["ok"] and "person-only" in refused["error"]["message"]
    shown = conv.as_person("approval.get", approval_id=approval["approval_id"])
    assert shown["ok"], shown
    detail = shown["result"]
    answer = dict(approval_id=approval["approval_id"], decision="allow", nonce=detail["nonce"],
                  request_sha256=detail["request_sha256"])
    agent = conv.as_agent("approval.respond", **answer)
    assert not agent["ok"] and "person-only" in agent["error"]["message"]
    assert conv.message(mid)["state"] == "approval-needed"

    person = conv.as_person("approval.respond", **answer)
    assert person["ok"], person
    assert conv.until_state(mid, "complete", "failed")["state"] == "complete"
    responses = [row for row in conv.stdin_rows() if row.get("type") == "control_response"]
    assert len(responses) == 1
    body = responses[0]["response"]["response"]
    assert body["behavior"] == "allow"
    assert body["updatedInput"] == {"command": "echo approved-by-person", "description": "Say hello"}
    kinds = [e["kind"] for e in conv.events(cid)]
    assert kinds.count("approval.requested") == 1 and "tool.completed" in kinds
    # A repeated answer is recognised, not sent again.
    again = conv.as_person("approval.respond", **answer)
    assert again["ok"] and again["result"].get("duplicate")
    assert len([r for r in conv.stdin_rows() if r.get("type") == "control_response"]) == 1


def test_a_denied_question_and_an_answered_question(conv):
    """C-27.2: deny carries the person's message; AskUserQuestion is answered with the
    chosen answers added to the request's own input."""
    cid = conv.create()
    first = conv.submit(cid, "[fake:approval]")
    approval = pending_approval(conv, cid)
    detail = conv.as_person("approval.get", approval_id=approval["approval_id"])["result"]
    conv.as_person("approval.respond", approval_id=approval["approval_id"], decision="deny",
                   message="not today", nonce=detail["nonce"], request_sha256=detail["request_sha256"])
    assert conv.until_state(first, "complete", "failed")["state"] == "complete"
    text = [e for e in conv.events(cid) if e["kind"] == "text"][-1]["data"]["text"]
    assert text == "Not run: not today"

    second = conv.submit(cid, "[fake:question]", after_message_id=first)
    conv.until_state(second, "approval-needed")
    question = pending_approval(conv, cid)
    assert question["kind"] == "question" and question["options"] == ["answer", "deny", "cancel-turn"]
    detail = conv.as_person("approval.get", approval_id=question["approval_id"])["result"]
    conv.as_person("approval.respond", approval_id=question["approval_id"], decision="answer",
                   answers={"Which color?": "Blue"}, nonce=detail["nonce"], request_sha256=detail["request_sha256"])
    assert conv.until_state(second, "complete", "failed")["state"] == "complete"
    text = [e for e in conv.events(cid) if e["kind"] == "text"][-1]["data"]["text"]
    assert text == 'You chose {"Which color?": "Blue"}.'


def test_interrupt_ends_a_running_turn_with_the_providers_own_stop(conv):
    """C-24.7, D-13: the control interrupt ends the turn; no signal, no containment."""
    cid = conv.create()
    mid = conv.submit(cid, "count [fake:slow]")
    conv.until_state(mid, "running")
    conv.e2e.until(lambda: any(e["kind"] == "text.delta" for e in conv.events(cid)), timeout=20)
    conv.call("turn.interrupt", message_id=mid)
    done = conv.until_state(mid, "interrupted", "failed", "complete", timeout=20)
    assert done["state"] == "interrupted" and done["state_reason"] == "stopped"
    interrupts = [r for r in conv.stdin_rows() if (r.get("request") or {}).get("subtype") == "interrupt"]
    assert len(interrupts) == 1
    conversation = conv.call("conversation.open", conversation_id=cid)["conversation"]
    assert conversation["blocked_by"] is None
    assert conv.attempt(mid)["killed_by"] is None


def test_a_usage_limit_fails_over_with_a_labelled_continuation_never_a_resend(conv):
    """C-26.7, D-6: the limited message is not sent again; a labelled continuation
    runs on another account in the same session."""
    cid = conv.create()
    mid = conv.submit(cid, "big job [fake:limit]")
    failed = conv.until_state(mid, "failed", "complete")
    assert failed["state_reason"] == "limited"
    status = conv.call("conversation.open", conversation_id=cid)
    follow = [m for m in status["messages"] if m["origin"] == "failover"]
    assert len(follow) == 1 and follow[0]["continues"] == mid
    done = conv.until_state(follow[0]["message_id"], "complete", "failed")
    assert done["state"] == "complete"
    assert done["served"]["lane_id"] != failed["served"]["lane_id"]
    users = [r for r in conv.stdin_rows() if r.get("type") == "user"]
    assert [u["uuid"] for u in users] == [mid, follow[0]["message_id"]]
    assert users[1]["message"]["content"][0]["text"] == CONTINUATION_TEXT
    session = status["conversation"]["native_session_id"]
    launches = [row["argv"] for row in conv.turn_log() if "argv" in row]
    assert launches[1][launches[1].index("--resume") + 1] == session


def test_a_turn_that_ends_without_a_result_blocks_the_conversation_until_a_person_decides(conv):
    """C-24.8, D-13, IR-5: a delivered message with no terminal event could be resumed
    by the next one; the conversation waits for a person."""
    cid = conv.create()
    mid = conv.submit(cid, "[fake:exit-after-ack]")
    ended = conv.until_state(mid, "failed", "interrupted", "complete", "delivery-unknown")
    assert ended["state"] == "failed" and ended["state_reason"] == "ended-without-result"
    assert conv.call("conversation.open", conversation_id=cid)["conversation"]["blocked_by"] == "unfinished-turn"
    nxt = conv.submit(cid, "next", after_message_id=mid)
    import time
    time.sleep(1.0)
    assert conv.message(nxt)["state"] == "queued"
    refused = conv.as_agent("conversation.unblock", conversation_id=cid, choice="leave", confirm=True)
    assert not refused["ok"]
    unblocked = conv.as_person("conversation.unblock", conversation_id=cid, choice="leave", confirm=True)
    assert unblocked["ok"], unblocked
    assert conv.until_state(nxt, "complete", "failed")["state"] == "complete"
    users = [r for r in conv.stdin_rows() if r.get("type") == "user"]
    note = users[1]["message"]["content"][0]["text"]
    assert note.startswith("[Subfleet] The previous turn was stopped") and users[2]["uuid"] == nxt


def test_a_codex_conversation_is_read_only_until_the_guard_is_proven_live(conv):
    """C-26.11, IR-32, D-11: writable Codex refused; a read-only thread starts read-only,
    the guard is checked on the turn's own server, and the thread continues."""
    refused = conv.request("conversation.create", provider="codex", request_id=str(uuid.uuid4()),
                           workspace=str(conv.e2e.workdir), settings={"model": "gpt-6-astra", "permission": "ask"})
    assert not refused["ok"] and "codex-read-only" in refused["error"]["message"]
    cid = conv.create(provider="codex", model="gpt-6-astra", permission="read-only")
    first = conv.submit(cid, "look around")
    done = conv.until_state(first, "complete", "failed", "delivery-unknown")
    assert done["state"] == "complete", done
    conversation = conv.call("conversation.open", conversation_id=cid)["conversation"]
    thread, lane = conversation["native_session_id"], conversation["lane_id"]
    assert thread and lane.startswith("codex-")
    rows = conv.stdin_rows()
    methods = [r.get("method") for r in rows if r.get("method")]
    assert methods[:6] == ["initialize", "initialized", "hooks/list", "model/list", "thread/start", "turn/start"]
    start = next(r for r in rows if r.get("method") == "thread/start")
    assert start["params"]["sandbox"] == "read-only"
    turn = next(r for r in rows if r.get("method") == "turn/start")
    assert turn["params"]["clientUserMessageId"] == first and turn["params"]["model"] == "gpt-6-astra"
    assert turn["params"]["sandboxPolicy"]["type"] == "readOnly"

    second = conv.submit(cid, "more", after_message_id=first)
    assert conv.until_state(second, "complete", "failed")["state"] == "complete"
    resumed = [r for r in conv.stdin_rows() if r.get("method") == "thread/resume"]
    assert len(resumed) == 1 and resumed[0]["params"]["threadId"] == thread
    assert conv.message(second)["served"]["lane_id"] == lane
    assert conv.attempt(second)["attestation"] == "attested"


def test_a_codex_approval_and_limit(conv):
    """C-27.1, IR-10: a command approval maps to the server's decision; a usage limit is a
    limited outcome with the reached window's reset."""
    flag = conv.e2e.root / "conversations" / "codex-writable-verified.json"
    flag.parent.mkdir(exist_ok=True)
    flag.write_text(json.dumps({"verified_at": "test"}))
    cid = conv.create(provider="codex", model="gpt-6-astra", permission="ask")
    mid = conv.submit(cid, "[fake:approval]")
    approval = pending_approval(conv, cid)
    assert approval["kind"] == "command" and approval["display"]["command"] == "echo approved-by-person"
    detail = conv.as_person("approval.get", approval_id=approval["approval_id"])["result"]
    conv.as_person("approval.respond", approval_id=approval["approval_id"], decision="allow",
                   nonce=detail["nonce"], request_sha256=detail["request_sha256"])
    assert conv.until_state(mid, "complete", "failed")["state"] == "complete"
    answer = next(r for r in conv.stdin_rows() if r.get("id") == 900)
    assert answer == {"id": 900, "result": {"decision": "accept"}}
    turn = next(r for r in conv.stdin_rows() if r.get("method") == "turn/start")
    assert turn["params"]["sandboxPolicy"] == {"type": "workspaceWrite", "networkAccess": False}

    limited = conv.submit(cid, "[fake:limit]", after_message_id=mid)
    assert conv.until_state(limited, "failed", "complete")["state_reason"] == "limited"
    assert conv.attempt(limited)["outcome_class"] == "limited"
    closures = conv.e2e.rows("SELECT * FROM closures WHERE lane_id=?",
                             (conv.message(limited)["served"]["lane_id"],))
    assert closures and closures[-1]["clock_source"] == "reported"


def test_a_daemon_restart_mid_turn_adopts_the_turn_and_sends_nothing_twice(conv):
    """C-26.4, C-27.3, design §11: the guardian and provider outlive the daemon; the new
    daemon replays stdout, finds the approval it already stored, and the relay log keeps
    every frame to one write."""
    cid = conv.create()
    mid = conv.submit(cid, "[fake:approval]")
    conv.until_state(mid, "approval-needed")
    before = pending_approval(conv, cid)
    conv.e2e.crash()
    conv.e2e.start()
    assert conv.message(mid)["state"] == "approval-needed"
    approvals = conv.call("approval.list", conversation_id=cid)["approvals"]
    assert [a["approval_id"] for a in approvals] == [before["approval_id"]]
    detail = conv.as_person("approval.get", approval_id=before["approval_id"])
    assert detail["ok"], detail
    answer = conv.as_person("approval.respond", approval_id=before["approval_id"], decision="allow",
                            nonce=detail["result"]["nonce"], request_sha256=detail["result"]["request_sha256"])
    assert answer["ok"], answer
    assert conv.until_state(mid, "complete", "failed", timeout=40)["state"] == "complete"
    rows = conv.stdin_rows()
    assert [r["uuid"] for r in rows if r.get("type") == "user"] == [mid]
    assert len([r for r in rows if (r.get("request") or {}).get("subtype") == "initialize"]) == 1
    assert len([r for r in rows if r.get("type") == "control_response"]) == 1
    kinds = [e["kind"] for e in conv.events(cid)]
    assert kinds.count("approval.requested") == 1 and kinds.count("turn.completed") == 1
    assert conv.attempt(mid)["outcome_class"] == "ok"


# --- labelled handoffs (C-30.3, design D-18, review IR-28) -----------------------------

FAKE_SECRET = "sk-ant-FAKEFAKEFAKEFAKEFAKEFAKE1234"        # shaped like a key; not one
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlLXZhbHVl"
CODEX_READ_ONLY = {"model": "gpt-6-astra", "permission": "read-only"}


def hand_off(conv, source: dict, to: dict, request_id: str | None = None) -> tuple[dict, dict]:
    args = {"request_id": request_id or str(uuid.uuid4()), "from": source, "to": to}
    return conv.call("conversation.handoff", **args), args


def sent_texts(conv, method_or_type: str) -> dict[str, str]:
    """What each turn process was given, by message id: Codex `turn/start` input or
    Claude user message."""
    out = {}
    for row in conv.stdin_rows():
        if row.get("method") == "turn/start" == method_or_type:
            out[row["params"]["clientUserMessageId"]] = " ".join(
                i.get("text", "") for i in row["params"]["input"] if i.get("type") == "text")
        elif row.get("type") == "user" == method_or_type:
            out[row["uuid"]] = " ".join(b.get("text", "") for b in row["message"]["content"] if b.get("type") == "text")
    return out


def test_a_claude_conversation_hands_off_to_codex_with_its_pending_messages(conv):
    """C-30.3, D-18, IR-28, C-23.14: a new Codex conversation whose first message is a
    scrubbed brief of the Claude transcript, recording where it came from; the
    source's queued messages move behind it in order and run there, never in the
    source; the source keeps its history; the same request returns the same
    handoff."""
    cid = conv.create()
    first = conv.submit(cid, f"Port the importer [fake:reply] with token {FAKE_SECRET}")
    assert conv.until_state(first, "complete", "failed")["state"] == "complete"
    stopped = conv.submit(cid, "[fake:exit-after-ack]", after_message_id=first)
    assert conv.until_state(stopped, "failed", "complete", "delivery-unknown")["state_reason"] == "ended-without-result"
    third = conv.submit(cid, "third message", after_message_id=stopped)
    fourth = conv.submit(cid, "fourth message", after_message_id=third)
    source = conv.call("conversation.open", conversation_id=cid)["conversation"]
    assert source["blocked_by"] == "unfinished-turn"          # so the two stay queued (C-24.8)
    conv.attempt(stopped)                                      # its job has finalized: no live turn
    session = source["native_session_id"]
    [transcript] = (conv.e2e.root / "user-home" / ".claude" / "projects").glob(f"*/{session}.jsonl")
    before = transcript.read_bytes()

    out, args = hand_off(conv, {"conversation_id": cid}, {"provider": "codex", "settings": CODEX_READ_ONLY})
    new = out["conversation"]
    assert out["created"] and new["origin"] == "handoff" and new["provider"] == "codex"
    record = new["handoff_from"]
    assert (record["provider"], record["native_session_id"], record["transcript"]) == ("claude", session,
                                                                                        str(transcript))
    assert [pair["from"] for pair in record["moved"]] == [third, fourth]
    for mid in (third, fourth):
        assert conv.message(mid)["state"] == "cancelled"
        assert conv.message(mid)["state_reason"] == f"handed-off:{new['conversation_id']}"
    again = conv.call("conversation.handoff", **args)
    assert not again["created"] and again["conversation"]["conversation_id"] == new["conversation_id"]

    brief, moved = out["brief"]["message_id"], [m["message_id"] for m in out["moved"]]
    for mid in (brief, *moved):
        assert conv.until_state(mid, "complete", "failed", "delivery-unknown")["state"] == "complete", mid
    sent = sent_texts(conv, "turn/start")
    assert list(sent) == [brief, *moved], "brief first, then the moved messages in order"
    assert [sent[m] for m in moved] == ["third message", "fourth message"]
    text = sent[brief]
    assert hashlib.sha256(text.encode()).hexdigest() == record["brief_sha256"]
    assert text.startswith("# Cross-agent handoff") and "- Source provider: Claude Code" in text
    assert f"- Source session: {session}" in text and f"- Source transcript: {transcript}" in text
    assert FAKE_SECRET not in text and "[REDACTED]" in text and "Port the importer" in text
    # Never the same native session: the new conversation has its own Codex thread.
    opened = conv.call("conversation.open", conversation_id=new["conversation_id"])["conversation"]
    assert opened["native_session_id"] and opened["native_session_id"] != session
    # The source keeps its history, and its transcript was never written again.
    kept = {m["message_id"]: m["state"] for m in conv.call("conversation.open", conversation_id=cid)["messages"]}
    assert kept == {first: "complete", stopped: "failed", third: "cancelled", fourth: "cancelled"}
    assert transcript.read_bytes() == before
    assert list(sent_texts(conv, "user")) == [first, stopped], "nothing moved was sent to Claude"


def test_a_codex_conversation_waiting_on_its_limited_lane_hands_off_to_claude(conv):
    """C-30.3, D-5, D-18, IR-2, IR-28: a Codex conversation pinned to a limited lane
    moves to Claude. Its waiting message is withdrawn by the job store's guard
    (its job cancelled, no attempt ever made) and moves with the queued one
    behind it; the brief is read from the Codex rollout and scrubbed."""
    cid = conv.create(provider="codex", model="gpt-6-astra", permission="read-only")
    first = conv.submit(cid, f"Look around [fake:reply] using Bearer {FAKE_JWT}")
    assert conv.until_state(first, "complete", "failed")["state"] == "complete"
    limited = conv.submit(cid, "[fake:limit]", after_message_id=first)
    assert conv.until_state(limited, "failed", "complete")["state_reason"] == "limited"
    waiting = conv.submit(cid, "after the limit", after_message_id=limited)
    request = f"turn:{waiting}:0"
    conv.e2e.until(lambda: conv.message(waiting)["state"] == "waiting" and (
        conv.e2e.rows("SELECT state FROM jobs WHERE request_id=?", (request,)) or [{}])[0].get("state") == "waiting",
        timeout=30)
    behind = conv.submit(cid, "one more", after_message_id=waiting)
    source = conv.call("conversation.open", conversation_id=cid)["conversation"]
    thread, lane = source["native_session_id"], source["lane_id"]
    [rollout] = (conv.e2e.root / lane / "sessions").rglob(f"rollout-*{thread}.jsonl")
    before = rollout.read_bytes()

    out, _args = hand_off(conv, {"conversation_id": cid},
                          {"provider": "claude", "settings": {"model": "opus[1m]", "permission": "ask"}})
    record = out["handoff_from"]
    assert (record["provider"], record["native_session_id"], record["lane_id"]) == ("codex", thread, lane)
    assert record["transcript"] == str(rollout)
    assert [pair["from"] for pair in record["moved"]] == [waiting, behind]
    job = conv.e2e.rows("SELECT job_id, state FROM jobs WHERE request_id=?", (request,))[0]
    assert job["state"] == "cancelled" and conv.e2e.attempts(job["job_id"]) == []

    brief, moved = out["brief"]["message_id"], [m["message_id"] for m in out["moved"]]
    for mid in (brief, *moved):
        assert conv.until_state(mid, "complete", "failed", "delivery-unknown")["state"] == "complete", mid
    sent = sent_texts(conv, "user")
    assert list(sent) == [brief, *moved]
    assert [sent[m] for m in moved] == ["after the limit", "one more"]
    text = sent[brief]
    assert hashlib.sha256(text.encode()).hexdigest() == record["brief_sha256"]
    assert "- Source provider: Codex" in text and f"- Source thread: {thread}" in text
    assert FAKE_JWT not in text and "[REDACTED]" in text and "Look around" in text
    replies = [e["data"]["text"] for e in conv.events(out["conversation"]["conversation_id"]) if e["kind"] == "text"]
    assert replies[0] == f"Fake Claude read {len(text)} characters."
    kept = {m["message_id"]: (m["state"], m["state_reason"]) for m in
            conv.call("conversation.open", conversation_id=cid)["messages"]}
    new = out["conversation"]["conversation_id"]
    assert kept == {first: ("complete", None), limited: ("failed", "limited"),
                    waiting: ("cancelled", f"handed-off:{new}"), behind: ("cancelled", f"handed-off:{new}")}
    assert rollout.read_bytes() == before
    assert [r["params"]["clientUserMessageId"] for r in conv.stdin_rows() if r.get("method") == "turn/start"] == [
        first, limited], "the waiting message never reached Codex"


def test_a_handoff_is_refused_while_the_source_has_a_live_turn(conv):
    """IR-28, C-24.7: while a turn runs, nothing is withdrawn and no conversation is
    created; the queued message stays where it was."""
    cid = conv.create()
    running = conv.submit(cid, "count [fake:slow]")
    conv.until_state(running, "running")
    queued = conv.submit(cid, "next", after_message_id=running)
    args = {"request_id": str(uuid.uuid4()), "from": {"conversation_id": cid},
            "to": {"provider": "codex", "settings": CODEX_READ_ONLY}}
    refused = conv.request("conversation.handoff", **args)
    assert not refused["ok"] and refused["error"]["code"] == 2
    assert refused["error"]["message"].startswith("live-turn")
    assert conv.message(queued)["state"] == "queued"
    listed = conv.call("conversation.list", include_catalog=False)["conversations"]
    assert [c["origin"] for c in listed] == ["new"]
    conv.call("turn.interrupt", message_id=running)
    assert conv.until_state(running, "interrupted", "failed", "complete", timeout=20)["state"] == "interrupted"
