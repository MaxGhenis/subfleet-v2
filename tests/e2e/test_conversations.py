"""Conversations end to end: the real daemon, guardian, relay, drivers and adapters,
with the fake interactive providers on PATH (C-24 to C-30, design §4 to §11).

Person-only operations (C-25.6) are sent from a process with a controlling
terminal, which `script(1)` provides, so the daemon's real peer check decides
them; nothing here stubs the check.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
import uuid

import pytest

from subfleet.conversations.service import CONTINUATION_TEXT

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")
TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled", "lost")

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


#: This test run is itself a Subfleet turn's or job's descendant (C-25.6 markers).
IN_A_SUBFLEET_ATTEMPT = any(os.environ.get(name) for name in ("SUBFLEET_ATTEMPT", "SUBFLEET_JOB"))


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
        if IN_A_SUBFLEET_ATTEMPT:
            # The peer check walks every ancestor, and a Subfleet guardian sits above
            # any process a turn or job starts: its descendants are agents by design
            # (C-25.6), so no person request can be made from here.
            pytest.skip("person-only requests cannot come from inside a Subfleet turn or job; "
                        "run this test from a terminal")
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


@pytest.fixture
def hooked(e2e):
    """Conversations whose fake Claude runs `subfleet hook <event>` the way
    Claude Code runs a command hook, inside the turn process the daemon and its
    guardian launched, so the hook sees whatever environment they gave it."""
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.env["SUBFLEET_FAKE_HOOK_COMMAND"] = f"{sys.executable} -m subfleet hook"
    e2e.start()
    return Conversations(e2e)


def test_a_turns_session_hooks_see_the_daemons_markers_and_surface_no_ping(hooked):
    """C-26.13 with C-5.1: every turn runs SessionStart (`startup`, then
    `resume`) and UserPromptSubmit with the markers `Daemon._launch` set, and
    neither says anything while the only notice pending for the session is a
    `ping`: a turn's session is the conversation's, not one the sessions kit may
    wake, and its prompt is the person's message, not a place for a nudge. The
    `ping` stays pending."""
    conv = hooked
    cid = conv.create()
    first = conv.submit(cid, "hello there")
    assert conv.until_state(first, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    session = conv.call("conversation.open", conversation_id=cid)["conversation"]["native_session_id"]
    assert session
    pinged = conv.call("ping", session_id=session, text="subfleet: a resume nudge for this session")
    assert pinged["notice_id"] is not None

    second = conv.submit(cid, "and again", after_message_id=first)
    assert conv.until_state(second, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"

    ran = [row for row in conv.turn_log() if "hook" in row]
    assert [(row["hook"], row["source"]) for row in ran] == [
        ("SessionStart", "startup"), ("UserPromptSubmit", None),
        ("SessionStart", "resume"), ("UserPromptSubmit", None)], ran
    assert all(row.get("rc") == 0 and row.get("stdout") == "" for row in ran), ran
    jobs = [conv.e2e.rows("SELECT job_id FROM jobs WHERE request_id=?", (f"turn:{mid}:0",))[0]["job_id"]
            for mid in (first, second)]
    for row, job_id in zip(ran, [jobs[0], jobs[0], jobs[1], jobs[1]]):
        assert row["markers"] == {"SUBFLEET_JOB": job_id, "SUBFLEET_ATTEMPT": f"{job_id}/a1",
                                  "SUBFLEET_ROOT": str(conv.e2e.root)}, row
    notices = conv.e2e.rows("SELECT state FROM service_notices WHERE session_id=?", (session,))
    assert [row["state"] for row in notices] == ["pending"]


@pytest.fixture
def dispatching(e2e):
    """`hooked`, and a fake Claude whose `[fake:bash]` turn runs `subfleet run`
    as its Bash tool would: in the turn's process tree, with CLAUDECODE and
    CLAUDE_CODE_SESSION_ID naming the conversation's session."""
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.env["SUBFLEET_FAKE_HOOK_COMMAND"] = f"{sys.executable} -m subfleet hook"
    e2e.env["SUBFLEET_FAKE_BASH_COMMAND"] = shlex.join(
        [sys.executable, "-m", "subfleet.cli", *e2e.run_args("astra", "--name", "from-a-turn")])
    e2e.start()
    return Conversations(e2e)


def test_a_job_a_turn_dispatched_reaches_the_next_turn_and_a_ping_does_not(dispatching):
    """C-26.13 with C-15.1, C-15.2 and C-15.3: a detached job an agent in turn 1
    dispatched carries the conversation's session as its caller, so its
    completion notice is that session's. Turn 1's hooks ran before it existed,
    and nothing between turns delivers it, so it is still `pending` when turn 2
    starts; turn 2's `SessionStart` surfaces it once and marks it `surfaced`.
    A `ping` left for the same session is surfaced by neither hook and stays
    `pending`."""
    conv = dispatching
    e2e = conv.e2e
    cid = conv.create()
    first = conv.submit(cid, "send this out for review [fake:bash]")
    assert conv.until_state(first, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    session = conv.call("conversation.open", conversation_id=cid)["conversation"]["native_session_id"]
    assert session
    ran = [row for row in conv.turn_log() if "bash" in row]
    assert len(ran) == 1 and ran[0]["rc"] == 0 and ran[0]["session_id"] == session, ran
    job_id = ran[0]["stdout"].strip()
    job = e2e.job(job_id)
    assert (job["kind"], job["caller_session"]) == ("dispatch", session)
    e2e.until(lambda: e2e.job(job_id)["state"] in TERMINAL_JOB_STATES, timeout=60)
    notice = e2e.until(lambda: next(iter(e2e.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))), None),
                       timeout=30)
    assert (notice["session_id"], notice["state"]) == (session, "pending")
    assert all(row["stdout"] == "" for row in conv.turn_log() if "hook" in row)
    pinged = conv.call("ping", session_id=session, text="subfleet: a resume nudge for this session")
    assert pinged["notice_id"] is not None

    second = conv.submit(cid, "what came back?", after_message_id=first)
    assert conv.until_state(second, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    hooks_ran = [row for row in conv.turn_log() if "hook" in row]
    assert [(row["hook"], row["source"]) for row in hooks_ran] == [
        ("SessionStart", "startup"), ("UserPromptSubmit", None),
        ("SessionStart", "resume"), ("UserPromptSubmit", None)], hooks_ran
    started, prompted = hooks_ran[2], hooks_ran[3]
    assert started["rc"] == 0 and prompted["rc"] == 0
    context = json.loads(started["stdout"])["hookSpecificOutput"]["additionalContext"]
    assert "1 detached run dispatched by this session" in context and job_id in context
    assert prompted["stdout"] == "", "surfaced once, by the first hook of the turn"
    assert "resume nudge" not in started["stdout"] + prompted["stdout"]
    assert e2e.rows("SELECT state, transport FROM notices WHERE job_id=?", (job_id,)) == [
        {"state": "surfaced", "transport": "hook:SessionStart"}]
    assert [row["state"] for row in e2e.rows(
        "SELECT state FROM service_notices WHERE session_id=?", (session,))] == ["pending"]


def test_a_conversations_session_is_refused_by_resume_revive_and_the_sessions_kit(conv):
    """C-26.3, C-26.13 through the real CLI and daemon: after one turn the
    `sessions` op reports the session as a conversation's (not a lane's);
    `subfleet resume` of the turn job, `sessions revive`, `sessions continue`
    in the interrupted and cold scopes (revive and handoff), v1's `subfleet
    revive <id>` through the front door, and `handoff` naming the session are
    each refused with exit 7, and each fix names the Subfleet app. Nothing is
    submitted."""
    cid = conv.create()
    mid = conv.submit(cid, "hello there")
    assert conv.until_state(mid, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    session = conv.call("conversation.open", conversation_id=cid)["conversation"]["native_session_id"]
    turn_job = conv.attempt(mid)["job_id"]
    facts = conv.call("sessions", action="state", session_ids=[])
    assert session in facts["conversation_sessions"] and session not in facts["lane_sessions"]

    e2e = conv.e2e
    before = e2e.rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"]
    resumed = e2e.cli("resume", turn_job, "keep going")
    assert resumed.rc == 7, resumed
    assert "is a conversation turn" in resumed.stderr and "Subfleet app" in resumed.stderr
    revived = e2e.cli("sessions", "revive", session, "--revive", "--force", "--json")
    assert revived.rc == 7, revived
    assert json.loads(revived.stdout)["reason"].startswith("bound to a Subfleet conversation")
    nudged = e2e.cli("sessions", "continue", "--session", session, "--delay", "0")
    assert nudged.rc == 7 and "Subfleet app" in nudged.stderr, nudged
    handed = e2e.cli("handoff", session, "--to", "opus", "--dry-run")
    assert handed.rc == 7 and "Subfleet app" in handed.stderr, handed
    cold = e2e.cli("sessions", "continue", "--scope", "cold", session, "--revive", "--force")
    assert cold.rc == 7 and "Subfleet app" in cold.stderr, cold
    cold_handoff = e2e.cli("sessions", "continue", "--scope", "cold", "--session", session,
                           "--handoff", "--to", "opus")
    assert cold_handoff.rc == 7 and "Subfleet app" in cold_handoff.stderr, cold_handoff
    front_door = subprocess.run([str(Path(sys.executable).parent / "subfleet"), "revive", session,
                                 "--revive"], env=e2e.env, input="", capture_output=True,
                                text=True, timeout=60)
    assert front_door.returncode == 7 and "Subfleet app" in front_door.stderr, front_door
    assert e2e.rows("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == before
    assert e2e.rows("SELECT * FROM service_notices WHERE session_id=?", (session,)) == []


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
    time.sleep(1.0)
    assert conv.message(nxt)["state"] == "queued"
    # C-26.14: a queued message has no turn yet; the unfinished turn still has its changes (none).
    assert conv.call("turn.diff", message_id=nxt)["reason"] == "no-turn"
    unfinished = conv.call("turn.diff", message_id=mid)
    assert unfinished["available"] and unfinished["files"] == []
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
    # d260: only a bypass turn's shell reaches the network; an ask turn asks.
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


def test_a_worktree_conversation_runs_its_turns_in_its_own_worktree(conv):
    """D-16, D-25, C-24.1, C-26.12: `conversation.create` with a worktree cuts one on its
    own branch, records and returns its path and branch, and every turn runs there; the
    checkout it was cut from keeps its branch. A turn job leaves no notice (IR-17)."""
    e2e = conv.e2e
    created = conv.call("conversation.create", provider="claude", request_id=str(uuid.uuid4()),
                        workspace=str(e2e.workdir), workspace_kind="worktree",
                        settings={"model": "opus[1m]", "permission": "ask", "effort": None, "fast": False})
    view = created["conversation"]
    cid, worktree = view["conversation_id"], view["worktree"]
    assert worktree["branch"] == f"subfleet/{cid}" and view["workspace"] == worktree["path"]
    assert Path(worktree["path"]).resolve() == (e2e.root / "worktrees" / f"conversation-{cid}").resolve()
    branch = subprocess.run(["git", "-C", worktree["path"], "symbolic-ref", "--short", "HEAD"],
                            capture_output=True, text=True, env=e2e.env).stdout.strip()
    assert branch == f"subfleet/{cid}"

    mid = conv.submit(cid, "work in the worktree")
    assert conv.until_state(mid, "complete", "failed", "delivery-unknown")["state"] == "complete"
    launched = [row for row in conv.turn_log() if "argv" in row]
    assert [os.path.realpath(row["cwd"]) for row in launched] == [os.path.realpath(worktree["path"])]
    source = subprocess.run(["git", "-C", str(e2e.workdir), "symbolic-ref", "--short", "HEAD"],
                            capture_output=True, text=True, env=e2e.env).stdout.strip()
    assert source == "feature/e2e"
    opened = conv.call("conversation.open", conversation_id=cid)["conversation"]
    assert opened["worktree"] == worktree
    job = conv.attempt(mid)["job_id"]
    assert e2e.rows("SELECT * FROM notices WHERE job_id=?", (job,)) == []


def test_the_daemon_keeps_the_catalog_and_the_list_reports_it(conv):
    """C-30.1, D-23: the control loop runs the catalog out of process; `catalog.refresh`
    starts a run without waiting; `conversation.list` reports the catalog's state and
    finds a session written after the daemon started."""
    e2e = conv.e2e
    project = Path(e2e.env["CLAUDE_FAKE_PROJECTS_DIR"]) / "-elsewhere"
    project.mkdir(parents=True, exist_ok=True)
    session = str(uuid.uuid4())
    (project / f"{session}.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": str(e2e.workdir), "sessionId": session,
         "message": {"role": "user", "content": "an earlier desktop session"}}) + "\n")
    listed = conv.call("conversation.list")["catalog"]
    assert listed["state"] in ("absent", "fresh") and "refreshing" in listed

    def indexed():
        conv.call("catalog.refresh")
        catalog = conv.call("conversation.list", query="earlier desktop")["catalog"]
        return catalog if [i["native_session_id"] for i in catalog["items"]] == [session] else None

    catalog = e2e.until(indexed, timeout=60)
    assert catalog["state"] == "fresh" and catalog["generated_at"] and catalog["stale_after_s"] == 180
    assert (e2e.root / "catalog.json").is_file()


def test_receipts_carry_the_persons_text_for_other_clients(conv):
    """C-25.2: `conversation.open` and `message.status` receipts include the message's
    text (bounded), so a client that did not send it can still show it."""
    cid = conv.create()
    mid = conv.submit(cid, "hello from another client")
    conv.until_state(mid, "complete", "failed", "delivery-unknown")
    assert conv.message(mid)["text"] == "hello from another client"
    assert conv.message(mid)["text_truncated"] is False
    opened = conv.call("conversation.open", conversation_id=cid)["messages"]
    assert [m["text"] for m in opened] == ["hello from another client"]


# --- stops, reconciliation, refusals and withdrawals (C-24.6 to C-24.8, IR-3, IR-7, IR-23) ---


@pytest.fixture
def conv_with(e2e):
    """The daemon started with extra environment for the fakes, and policy clocks."""
    def start(env: dict | None = None, clocks: dict | None = None) -> Conversations:
        e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
        if clocks:
            e2e.policy_update(lambda policy: policy.setdefault("conversations", {}).update(clocks))
        e2e.start(env=env)
        return Conversations(e2e)
    return start


def relay_log(conv, mid: str, turn_seq: int = 0) -> list[dict]:
    from subfleet.relay import read_log
    job = conv.e2e.rows("SELECT job_id FROM jobs WHERE request_id=?", (f"turn:{mid}:{turn_seq}",))[0]["job_id"]
    return read_log(conv.e2e.root / "jobs" / job / "a1" / "stdin.jsonl")


def reconciled(conv, cid: str, mid: str) -> dict:
    return next(e["data"] for e in conv.events(cid)
                if e["kind"] == "status" and e["message_id"] == mid and e["data"].get("phase") == "reconciled")


def test_a_stubborn_turn_is_stopped_by_sigint_through_the_relay_on_the_policy_clock(conv_with):
    """C-24.7, C-24.8, D-13, IR-3: the provider's interrupt is ignored; SIGINT reaches the
    provider child through the guardian's relay at `stop_sigint_after_s`, before stdin is
    closed and with no containment. A provider that answers SIGINT with a `result` (as the
    real CLI did) ends interrupted and leaves the conversation free; one that dies without a
    result ends interrupted and blocks it `unfinished-turn`, its delivery proven.

    The policy clock is the one used: each stop ends well inside the default 10 s SIGINT
    delay (C-24.7), so a runner built with the default clocks fails this test."""
    conv = conv_with(clocks={"stop_sigint_after_s": 0.5, "stop_close_after_s": 20, "stop_contain_after_s": 30})
    cid = conv.create()
    first = conv.submit(cid, "keep going [fake:stubborn-result]")
    conv.until_state(first, "running")
    conv.e2e.until(lambda: any(e["kind"] == "text.delta" for e in conv.events(cid)), timeout=20)
    asked = time.monotonic()
    conv.call("turn.interrupt", message_id=first)
    done = conv.until_state(first, "interrupted", "failed", "complete", "delivery-unknown", timeout=20)
    assert time.monotonic() - asked < 6, "SIGINT came on the default clock, not the policy's"
    assert (done["state"], done["state_reason"]) == ("interrupted", "stopped")
    frames = [(r["tag"], r["op"], r["status"]) for r in relay_log(conv, first)]
    assert frames[2:4] == [("interrupt", "write", "written"), ("signal:int", "signal", "written")]
    assert frames[-1] == ("close", "close", "written")          # the driver's, after the result
    assert conv.call("conversation.open", conversation_id=cid)["conversation"]["blocked_by"] is None
    assert conv.attempt(first)["killed_by"] is None                # no containment

    second = conv.submit(cid, "keep going [fake:stubborn]", after_message_id=first)
    conv.until_state(second, "running")
    conv.e2e.until(lambda: sum(e["kind"] == "text.delta" and e["message_id"] == second
                               for e in conv.events(cid)) > 0, timeout=20)
    asked = time.monotonic()
    conv.call("turn.interrupt", message_id=second)
    ended = conv.until_state(second, "interrupted", "failed", "complete", "delivery-unknown", timeout=20)
    assert time.monotonic() - asked < 6, "SIGINT came on the default clock, not the policy's"
    assert (ended["state"], ended["state_reason"]) == ("interrupted", "stopped")
    assert [r["tag"] for r in relay_log(conv, second)][-1] == "signal:int"   # stdin never closed
    assert conv.call("conversation.open", conversation_id=cid)["conversation"]["blocked_by"] == "unfinished-turn"
    evidence = reconciled(conv, cid, second)
    assert evidence["delivery"] == "delivered" and evidence["evidence"]["acknowledged"] is True
    assert evidence["evidence"]["native"] == "found"
    assert conv.attempt(second)["killed_by"] is None


def test_a_turn_that_ignores_every_stop_is_contained_on_the_policy_clock(conv_with):
    """C-24.7, D-13 step 4, IR-3: interrupt, SIGINT and closing stdin all fail; containment
    follows at `stop_contain_after_s`, well inside the default 30 s, and the delivered turn
    blocks its conversation (C-24.8)."""
    conv = conv_with(clocks={"stop_sigint_after_s": 0.3, "stop_close_after_s": 0.6, "stop_contain_after_s": 1.0})
    cid = conv.create()
    mid = conv.submit(cid, "[fake:immovable]")
    conv.until_state(mid, "running")
    asked = time.monotonic()
    conv.call("turn.interrupt", message_id=mid)
    ended = conv.until_state(mid, "interrupted", "failed", "complete", "delivery-unknown", timeout=30)
    assert time.monotonic() - asked < 15, "containment came on the default clock, not the policy's"
    assert (ended["state"], ended["state_reason"]) == ("interrupted", "stopped")
    assert [r["tag"] for r in relay_log(conv, mid)][-3:] == ["interrupt", "signal:int", "close"]
    attempt = conv.attempt(mid)
    assert attempt["killed_by"] == "stopped" and attempt["state"] == "interrupted"
    assert conv.call("conversation.open", conversation_id=cid)["conversation"]["blocked_by"] == "unfinished-turn"


def test_a_claude_turn_on_the_wrong_model_fails_model_mismatch(conv):
    """C-26.8: the served model differs from the request: the driver stops the turn with the
    provider's own interrupt; the message fails `model-mismatch`. The provider finished the
    turn after the stop (a `result`), so nothing is left to resume (C-24.8)."""
    cid = conv.create()
    mid = conv.submit(cid, "[fake:wrong-model]")
    done = conv.until_state(mid, "failed", "complete", "interrupted", "delivery-unknown")
    assert (done["state"], done["state_reason"]) == ("failed", "model-mismatch")
    assert [(r.get("request") or {}).get("subtype") for r in conv.stdin_rows()
            if r.get("type") == "control_request"] == ["initialize", "interrupt"]
    final = [e for e in conv.events(cid) if e["kind"] == "turn.completed"][-1]["data"]
    assert final["reason"] == "model-mismatch" and "claude-haiku-4-5-20251001" in final["detail"]
    assert conv.call("conversation.open", conversation_id=cid)["conversation"]["blocked_by"] is None


def test_a_message_read_but_never_acknowledged_is_delivery_unknown_until_a_person_resolves_it(conv):
    """C-24.6, D-14: the relay wrote the message, the provider exited without acknowledging
    it and the transcript does not hold it: neither delivered nor provably not. It blocks
    its conversation, is never sent again, and a person's `message.resolve` frees it."""
    cid = conv.create()
    mid = conv.submit(cid, "[fake:exit-before-ack]")
    unknown = conv.until_state(mid, "delivery-unknown", "failed", "complete")
    assert unknown["state"] == "delivery-unknown"
    assert unknown["state_reason"].startswith("ended-without-result: frame written, native record absent")
    evidence = reconciled(conv, cid, mid)["evidence"]
    assert evidence["frame"] == "written" and evidence["process_gone"] and not evidence["acknowledged"]
    conversation = conv.call("conversation.open", conversation_id=cid)["conversation"]
    assert conversation["blocked_by"] == "delivery-unknown"
    assert conversation["native_session_id"] is None               # the session was never created

    nxt = conv.submit(cid, "after that", after_message_id=mid)
    time.sleep(1.0)
    assert conv.message(nxt)["state"] == "queued"
    refused = conv.as_agent("message.resolve", message_id=mid, resolution="not-delivered", confirm=True)
    assert not refused["ok"] and "person-only" in refused["error"]["message"]
    resolved = conv.as_person("message.resolve", message_id=mid, resolution="not-delivered", confirm=True)
    assert resolved["ok"], resolved
    assert (resolved["result"]["state"], resolved["result"]["state_reason"]) == ("failed", "resolved-not-delivered")
    assert conv.until_state(nxt, "complete", "failed")["state"] == "complete"
    assert [r["uuid"] for r in conv.stdin_rows() if r.get("type") == "user"] == [mid, nxt]
    launches = [row["argv"] for row in conv.turn_log() if "argv" in row]
    assert "--session-id" in launches[1] and "--resume" not in launches[1]


def test_fast_unavailable_is_readmitted_at_most_max_readmits_times_then_fails(conv_with):
    """IR-23, C-24.6, C-26.8: `initialize` reports Fast off; nothing is sent, so the same
    message is carried by a new turn job, at most MAX_READMITS times, then fails
    not-delivered. The session id Subfleet minted is never resumed: the provider never
    created it."""
    from subfleet.conversations.reconcile import MAX_READMITS
    conv = conv_with(env={"SUBFLEET_FAKE_FAST": "off"})
    cid = conv.create(fast=True)
    mid = conv.submit(cid, "quickly")
    done = conv.until_state(mid, "failed", "complete", "delivery-unknown", timeout=60)
    assert (done["state"], done["state_reason"]) == ("failed", "not-delivered: fast-unavailable")
    jobs = conv.e2e.rows("SELECT request_id FROM jobs WHERE kind='turn' ORDER BY created_at")
    assert [j["request_id"] for j in jobs] == [f"turn:{mid}:{n}" for n in range(MAX_READMITS + 1)]
    assert not [r for r in conv.stdin_rows() if r.get("type") == "user"]
    launches = [row["argv"] for row in conv.turn_log() if "argv" in row]
    assert len(launches) == MAX_READMITS + 1
    assert all("--session-id" in argv and "--resume" not in argv for argv in launches)
    reasons = [e["data"]["reason"] for e in conv.events(cid) if e["kind"] == "turn.completed"]
    assert reasons == ["fast-unavailable"] * (MAX_READMITS + 1)
    conversation = conv.call("conversation.open", conversation_id=cid)["conversation"]
    assert conversation["blocked_by"] is None and conversation["native_session_id"] is None


def test_message_cancel_withdraws_a_queued_message_and_tombstones_an_unknown_one(conv):
    """C-24.7, IR-2, IR-7: a message queued behind a running turn is withdrawn and never
    sent. Withdrawing an id the daemon never received leaves a tombstone, so a late
    submit of that id is answered cancelled and never dispatched."""
    cid = conv.create()
    running = conv.submit(cid, "count [fake:slow]")
    conv.until_state(running, "running")
    queued = conv.submit(cid, "later", after_message_id=running)
    assert conv.message(queued)["state"] == "queued"
    withdrawn = conv.call("message.cancel", message_id=queued)
    assert (withdrawn["state"], withdrawn["state_reason"]) == ("cancelled", "withdrawn")
    refused = conv.request("message.cancel", message_id=running)
    assert not refused["ok"] and "too-late" in refused["error"]["message"]

    ghost = str(uuid.uuid4())
    unknown = conv.request("message.cancel", message_id=ghost)
    assert not unknown["ok"] and "unknown-message" in unknown["error"]["message"]
    tomb = conv.call("message.cancel", message_id=ghost, conversation_id=cid)
    assert (tomb["state"], tomb["state_reason"], tomb["origin"]) == ("cancelled", "withdrawn-before-receipt",
                                                                     "tombstone")
    late = conv.call("message.submit", conversation_id=cid, message_id=ghost, after_message_id=queued,
                     text="sent before the withdrawal was known")
    assert (late["state"], late["created"]) == ("cancelled", False)

    conv.call("turn.interrupt", message_id=running)
    assert conv.until_state(running, "interrupted", "failed", "complete")["state"] == "interrupted"
    after = conv.submit(cid, "now", after_message_id=queued)
    assert conv.until_state(after, "complete", "failed")["state"] == "complete"
    assert [r["uuid"] for r in conv.stdin_rows() if r.get("type") == "user"] == [running, after]
    turn_jobs = {j["request_id"].split(":")[1] for j in conv.e2e.rows("SELECT request_id FROM jobs WHERE kind='turn'")}
    assert turn_jobs == {running, after}
    assert conv.message(ghost)["state"] == "cancelled" and conv.message(queued)["state"] == "cancelled"


def test_a_codex_thread_with_an_active_turn_is_an_external_writer(conv_with):
    """C-26.3, IR-23, C-24.6: `thread/resume` reports an active turn: someone else is
    writing. The turn ends `external-writer` before `turn/start`, so the message was not
    delivered. It waits for the writer (`readmit:external-writer`) however long that
    takes (d216 and C-26.3, 2026-09-24: a wait never uses up the re-admissions a failing
    provider gets; a Codex one is looked at again every 30 s, since only a provider
    start can see the other writer); a stop withdraws it. The conversation is not blocked."""
    conv = conv_with(env={"SUBFLEET_FAKE_THREAD_ACTIVE": "1"})
    cid = conv.create(provider="codex", model="gpt-6-astra", permission="read-only")
    first = conv.submit(cid, "look around")
    assert conv.until_state(first, "complete", "failed", "delivery-unknown")["state"] == "complete"
    second = conv.submit(cid, "and more", after_message_id=first)

    def waiting_for_the_writer():
        message = conv.call("message.status", message_ids=[second])["messages"][0]
        return message if (message["state"], message["state_reason"]) == ("waiting", "readmit:external-writer") else None
    conv.e2e.until(waiting_for_the_writer, timeout=30)
    rows = conv.stdin_rows()
    assert [r["params"]["clientUserMessageId"] for r in rows if r.get("method") == "turn/start"] == [first]
    reasons = [e["data"]["reason"] for e in conv.events(cid)
               if e["kind"] == "turn.completed" and e["message_id"] == second]
    assert reasons and set(reasons) == {"external-writer"}
    evidence = reconciled(conv, cid, second)
    assert evidence["delivery"] == "not-delivered" and evidence["evidence"]["native"] == "absent"
    assert conv.call("conversation.open", conversation_id=cid)["conversation"]["blocked_by"] is None
    assert conv.call("turn.interrupt", message_id=second)["state"] in ("cancelled", "waiting")
    stopped = conv.until_state(second, "cancelled", "failed", "interrupted", timeout=60)
    assert stopped["state"] == "cancelled" and stopped["state_reason"] == "withdrawn"


def test_a_person_who_resolves_a_claude_message_delivered_then_chooses_how_to_go_on(conv):
    """C-24.6, C-24.8: resolved `delivered`, the message ended with no `result`, so the
    conversation moves to `unfinished-turn` and binds the session its turn named; the
    person's `leave` sends the note first, and the original is never sent again."""
    cid = conv.create()
    mid = conv.submit(cid, "[fake:exit-before-ack]")
    assert conv.until_state(mid, "delivery-unknown", "failed", "complete")["state"] == "delivery-unknown"
    resolved = conv.as_person("message.resolve", message_id=mid, resolution="delivered", confirm=True)
    assert resolved["ok"] and resolved["result"]["state_reason"] == "resolved-delivered"
    conversation = conv.call("conversation.open", conversation_id=cid)["conversation"]
    assert conversation["blocked_by"] == "unfinished-turn"
    session = conversation["native_session_id"]
    first_launch = next(row["argv"] for row in conv.turn_log() if "argv" in row)
    assert session == first_launch[first_launch.index("--session-id") + 1]
    again = conv.as_person("message.resolve", message_id=mid, resolution="delivered", confirm=True)
    assert not again["ok"] and "not-ambiguous" in again["error"]["message"]
    nxt = conv.submit(cid, "go on", after_message_id=mid)
    assert conv.as_person("conversation.unblock", conversation_id=cid, choice="leave", confirm=True)["ok"]
    assert conv.until_state(nxt, "complete", "failed")["state"] == "complete"
    users = [r for r in conv.stdin_rows() if r.get("type") == "user"]
    assert [u["uuid"] for u in users][0] == mid and users[-1]["uuid"] == nxt
    assert users[1]["message"]["content"][0]["text"].startswith("[Subfleet] The previous turn was stopped")
    launches = [row["argv"] for row in conv.turn_log() if "argv" in row]
    assert all(argv[argv.index("--resume") + 1] == session for argv in launches[1:])


def workspace_git(e2e, *args) -> str:
    return subprocess.run(["git", "-C", str(e2e.workdir), *args], env=e2e.env, capture_output=True, text=True,
                          check=True).stdout


def changed(result: dict) -> dict:
    return {f["path"]: (f["status"], f["additions"], f["deletions"]) for f in result["files"]}


def test_each_turn_and_the_whole_conversation_show_their_changes(conv):
    """C-26.14, C-26.10, C-25.1, design D-25: a writable turn is bracketed by two
    working-tree snapshots; `turn.diff` shows that turn's changes and nothing the person
    did between turns; `conversation.diff` shows everything since the first turn began;
    HEAD, the real index and the refs are untouched, and no salvage ref is written; the
    daemon advertises the diff ops as a capability, with `conversation_schema` still 1.
    (Which pool runs them, C-25.3, is tests/fake/test_conversation_pools.py.)"""
    e2e = conv.e2e
    head = workspace_git(e2e, "rev-parse", "HEAD").strip()
    index = (e2e.workdir / ".git" / "index").read_bytes()
    refs = workspace_git(e2e, "for-each-ref")
    cid = conv.create()
    first = conv.submit(cid, "edit two files [fake:write]")
    assert conv.until_state(first, "complete", "failed", "delivery-unknown")["state"] == "complete"
    attempt = conv.attempt(first)
    one = conv.call("turn.diff", message_id=first)
    t1 = first[:8]
    assert one["available"] and one["to"]["live"] is False and one["root"] == str(e2e.workdir)
    assert changed(one) == {f"fake-{t1}.txt": ("added", 3, 0), "tracked.txt": ("modified", 1, 0)}
    assert f"+edited by {t1}\n" in one["diff"] and not one["truncated"] and not one["files_truncated"]
    assert one["from"]["head"] == one["to"]["head"] == head
    # The receipt records HEAD before and after and both snapshots (C-26.10).
    assert attempt["baseline_tree"] == one["from"]["tree"]
    assert json.loads(attempt["evidence_json"])["turn_trees"] == {
        "head_before": head, "head_after": head, "start_tree": one["from"]["tree"],
        "end_tree": one["to"]["tree"], "error": None}

    (e2e.workdir / "person.txt").write_text("the person's own edit\n")
    second = conv.submit(cid, "and more [fake:write]", after_message_id=first)
    assert conv.until_state(second, "complete", "failed", "delivery-unknown")["state"] == "complete"
    conv.attempt(second)
    two = conv.call("turn.diff", message_id=second)
    t2 = second[:8]
    assert changed(two) == {f"fake-{t2}.txt": ("added", 3, 0), "tracked.txt": ("modified", 1, 0)}
    whole = conv.call("conversation.diff", conversation_id=cid)
    assert whole["available"] and whole["to"]["live"] is True and whole["from"]["message_id"] == first
    assert changed(whole) == {f"fake-{t1}.txt": ("added", 3, 0), f"fake-{t2}.txt": ("added", 3, 0),
                              "person.txt": ("added", 1, 0), "tracked.txt": ("modified", 2, 0)}
    assert whole["stats"] == {"files": 4, "additions": 9, "deletions": 0, "complete": True}
    only = conv.call("turn.diff", message_id=second, path="tracked.txt")
    assert list(changed(only)) == ["tracked.txt"] and f"fake-{t2}" not in only["diff"]
    refused = conv.request("turn.diff", message_id=second, path="../outside")
    assert not refused["ok"] and refused["error"]["code"] == 2

    assert workspace_git(e2e, "rev-parse", "HEAD").strip() == head
    assert (e2e.workdir / ".git" / "index").read_bytes() == index
    assert workspace_git(e2e, "for-each-ref") == refs
    capabilities = conv.call("capabilities")
    assert "diff.v1" in capabilities["capabilities"] and capabilities["conversation_schema"] == 1
    assert capabilities["limits"]["diff_bytes"] == 512 * 1024 and capabilities["limits"]["diff_files"] == 1000


def test_a_running_turns_changes_are_live_until_it_ends(conv):
    """C-26.14: before a turn's end snapshot, `turn.diff` compares its start with the
    working tree now and says so; a stopped turn still gets its end snapshot."""
    cid = conv.create()
    mid = conv.submit(cid, "write, then keep going [fake:write] [fake:slow]")
    conv.until_state(mid, "running")
    tag = mid[:8]

    def live():
        result = conv.call("turn.diff", message_id=mid)
        return result if result["available"] and result["files"] else None

    during = conv.e2e.until(live, timeout=20)
    assert during["to"]["live"] is True
    assert changed(during) == {f"fake-{tag}.txt": ("added", 3, 0), "tracked.txt": ("modified", 1, 0)}
    conv.call("turn.interrupt", message_id=mid)
    assert conv.until_state(mid, "interrupted", "failed", "complete", timeout=20)["state"] == "interrupted"
    conv.attempt(mid)
    after = conv.call("turn.diff", message_id=mid)
    assert after["to"]["live"] is False and changed(after) == changed(during)
    assert after["to"]["tree"] == during["to"]["tree"]


def test_turns_that_cannot_show_changes_say_why(conv):
    """C-26.14: a read-only turn, a workspace outside git, and a message that has not
    started a turn each answer `available: false` with the reason, in the same shape."""
    cid = conv.create(permission="read-only")
    mid = conv.submit(cid, "look around")
    assert conv.until_state(mid, "complete", "failed", "delivery-unknown")["state"] == "complete"
    conv.attempt(mid)
    read_only = conv.call("turn.diff", message_id=mid)
    assert (read_only["available"], read_only["reason"]) == (False, "read-only-turn")
    assert read_only["files"] == [] and read_only["diff"] == "" and read_only["truncated"] is False
    assert conv.call("conversation.diff", conversation_id=cid)["reason"] == "no-snapshot"

    plain = conv.e2e.root / "plain"
    plain.mkdir()
    other = conv.create(workspace=str(plain))
    wrote = conv.submit(other, "[fake:write]")
    assert conv.until_state(wrote, "complete", "failed", "delivery-unknown")["state"] == "complete"
    conv.attempt(wrote)
    assert (plain / "tracked.txt").read_text() == f"edited by {wrote[:8]}\n"
    outside = conv.call("turn.diff", message_id=wrote)
    assert (outside["available"], outside["reason"]) == (False, "no-snapshot")
    assert conv.call("conversation.diff", conversation_id=other)["reason"] == "no-snapshot"
