"""C-24.4, C-26.5, C-26.8, C-27.1 to C-27.4, design D-11: the Codex turn driver.

Every frame the driver writes is checked against the schema codex-cli 0.153.3
generates for its stable app-server surface (tests/fixtures/codex/app-server-0.153.3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subfleet.conversations.codex_turn import (
    ID_HOOKS, ID_INIT, ID_MODELS, ID_THREAD, ID_TURN, CodexTurn, argv, check_hooks,
)
from subfleet.conversations.turn import Image, TurnSpec
from subfleet.guard.preflight import HOOK_KEY
from tests.schema_check import errors

SCHEMAS = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "app-server-0.153.3"
CLIENT_REQUEST = json.loads((SCHEMAS / "ClientRequest.json").read_text())
CLIENT_NOTIFICATION = json.loads((SCHEMAS / "ClientNotification.json").read_text())
RESPONSES = {
    "item/commandExecution/requestApproval": "CommandExecutionRequestApprovalResponse.json",
    "item/fileChange/requestApproval": "FileChangeRequestApprovalResponse.json",
    "item/permissions/requestApproval": "PermissionsRequestApprovalResponse.json",
    "item/tool/requestUserInput": "ToolRequestUserInputResponse.json",
    "mcpServer/elicitation/request": "McpServerElicitationRequestResponse.json",
}
MID = "5b8e5c3a-0000-4000-8000-000000000001"
CWD = "/Users/max/repo"
HASH = "sha256:" + "a" * 64


def spec(**kw):
    base = dict(provider="codex", message_id=MID, text="add a test", model_id="gpt-6-astra", permission="ask",
                native_session_id=None, effort="high", cwd=CWD, guard_hash=HASH)
    base.update(kw)
    return TurnSpec(**base)


def check_frames(step, methods_by_request=None):
    for frame in step.frames:
        if frame.op != "write":
            continue
        body = json.loads(frame.line)
        if "method" in body and "id" in body:
            problems = errors(body, CLIENT_REQUEST, CLIENT_REQUEST)
        elif "method" in body:
            problems = errors(body, CLIENT_NOTIFICATION, CLIENT_NOTIFICATION)
        else:
            method = (methods_by_request or {}).get(str(body["id"]))
            if "error" in body:
                problems = [] if isinstance(body["error"].get("code"), int) else ["error without code"]
            else:
                schema = json.loads((SCHEMAS / RESPONSES[method]).read_text())
                problems = errors(body["result"], schema, schema)
        assert not problems, (frame.tag, problems)


def hooks_ok(cwd=CWD, **guard):
    entry = {"key": HOOK_KEY, "enabled": True, "trustStatus": "trusted", "currentHash": HASH, **guard}
    return {"data": [{"cwd": cwd, "hooks": [entry], "warnings": [], "errors": []}]}


MODELS = {"data": [{"id": "gpt-6-astra", "model": "gpt-6-astra", "supportedReasoningEfforts": [
    {"reasoningEffort": "high", "description": ""}, {"reasoningEffort": "ultra", "description": ""}],
    "serviceTiers": [{"id": "priority", "name": "Fast", "description": ""}]}]}


def resp(rid, result=None, error=None):
    return json.dumps({"id": rid, **({"error": error} if error else {"result": result})})


def note(method, **params):
    return json.dumps({"method": method, "params": params})


def to_thread(turn):
    """Drive a fresh turn through initialize and both checks; return the thread step."""
    check_frames(turn.start())
    step = turn.feed(resp(ID_INIT, {"userAgent": "x"}), 0)
    assert [f.tag for f in step.frames] == ["initialized", "hooks", "models"]
    check_frames(step)
    assert turn.feed(resp(ID_HOOKS, hooks_ok()), 1).frames == []
    step = turn.feed(resp(ID_MODELS, MODELS), 2)
    check_frames(step)
    return step


def to_running(turn, thread_id="thr-1"):
    step = to_thread(turn)
    assert json.loads(step.frames[0].line)["method"] == "thread/start"
    step = turn.feed(resp(ID_THREAD, {"thread": {"id": thread_id, "status": {"type": "idle"}}, "model": "gpt-6-astra",
                                      "reasoningEffort": "high", "approvalPolicy": "on-request", "sandbox": {}}), 3)
    check_frames(step)
    body = json.loads(step.frames[0].line)
    assert body["method"] == "turn/start" and body["params"]["clientUserMessageId"] == MID
    ack = turn.feed(resp(ID_TURN, {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}), 4)
    assert turn.accepted and ack.events[0].kind == "accepted"
    return body


def test_argv_requires_the_verified_override():
    """D-11 a turn server never starts without the verified hooks override."""
    assert argv("/bin/codex", "hooks={x}") == ["/bin/codex", "app-server", "--listen", "stdio://", "-c", "hooks={x}"]
    with pytest.raises(ValueError):
        argv("/bin/codex", "")


def test_a_new_conversation_starts_a_thread_and_a_turn_with_the_settings():
    """C-26.8 model, effort, Fast tier, sandbox and approval policy go on the thread and the turn."""
    turn = CodexTurn(spec(fast=True, images=(Image("c" * 64, "image/png", "/state/attachments/c.png"),)))
    body = to_running(turn)
    params = body["params"]
    assert params["model"] == "gpt-6-astra" and params["effort"] == "high" and params["serviceTier"] == "priority"
    assert params["approvalPolicy"] == "on-request" and params["approvalsReviewer"] == "user"
    assert params["sandboxPolicy"] == {"type": "workspaceWrite", "networkAccess": False}
    assert params["input"] == [{"type": "text", "text": "add a test"},
                               {"type": "localImage", "path": "/state/attachments/c.png"}]


@pytest.mark.parametrize("permission,network,expected", [
    ("bypass", True, {"type": "workspaceWrite", "networkAccess": True}),
    ("ask", True, {"type": "workspaceWrite", "networkAccess": False}),          # asks the person instead
    ("accept-edits", True, {"type": "workspaceWrite", "networkAccess": False}),
    ("bypass", False, {"type": "workspaceWrite", "networkAccess": False}),
    ("read-only", True, {"type": "readOnly", "networkAccess": False}),
])
def test_d260_a_writable_turn_reaches_the_network_when_its_manifest_says_so(permission, network, expected):
    """d260: a bypass turn's shell reaches the network; ask and accept-edits keep
    asking the person for it, as a Claude turn's Bash does; read-only never."""
    turn = CodexTurn(spec(permission=permission, network=network))
    assert to_running(turn)["params"]["sandboxPolicy"] == expected


def test_resume_checks_the_thread_it_got_back():
    """C-26.3 a resumed thread must be the one asked for and not already running a turn."""
    turn = CodexTurn(spec(native_session_id="thr-9"))
    step = to_thread(turn)
    assert json.loads(step.frames[0].line)["params"]["threadId"] == "thr-9"
    busy = turn.feed(resp(ID_THREAD, {"thread": {"id": "thr-9", "status": {"type": "active", "activeFlags": []}},
                                      "model": "gpt-6-astra"}), 3)
    assert busy.outcome.reason == "external-writer" and not busy.outcome.accepted


@pytest.mark.parametrize("hooks, reason", [
    ({"data": []}, "exactly one workdir"),
    (hooks_ok(cwd="/elsewhere"), "different workdir"),
    (hooks_ok(enabled=False), "disabled"),
    (hooks_ok(trustStatus="untrusted"), "untrusted"),
    (hooks_ok(currentHash="sha256:" + "b" * 64), "another hash"),
])
def test_the_guard_must_be_proven_on_this_server_before_anything_is_sent(hooks, reason):
    """D-11 the turn server's own hooks/list must show the never-rules guard, as the preflight requires."""
    assert reason in check_hooks(hooks, CWD, HASH)
    turn = CodexTurn(spec())
    turn.start()
    turn.feed(resp(ID_INIT, {}), 0)
    step = turn.feed(resp(ID_HOOKS, hooks), 1)
    assert step.outcome.reason == "guard-refused"
    assert all(f.tag not in ("thread", "user-message") for f in step.frames)


@pytest.mark.parametrize("kw, words", [
    ({"model_id": "gpt-9"}, "not in this account"),
    ({"effort": "max"}, "not max"),
    ({"fast": True, "model_id": "gpt-5.2"}, "no Fast tier"),
])
def test_settings_the_catalog_does_not_offer_are_refused(kw, words):
    """C-26.8 a requested model, effort or Fast tier the account's catalog does not list never silently applies."""
    catalog = {"data": [*MODELS["data"], {"id": "gpt-5.2", "model": "gpt-5.2", "supportedReasoningEfforts": [
        {"reasoningEffort": "high", "description": ""}], "serviceTiers": []}]}
    turn = CodexTurn(spec(**kw))
    turn.start()
    turn.feed(resp(ID_INIT, {}), 0)
    turn.feed(resp(ID_HOOKS, hooks_ok()), 1)
    step = turn.feed(resp(ID_MODELS, catalog), 2)
    assert step.outcome.reason == "settings-unsupported" and words in step.outcome.detail


def test_streaming_items_and_completion():
    """C-25.5, C-26.5: deltas, reasoning summaries (never raw reasoning), commands and the end."""
    turn = CodexTurn(spec())
    to_running(turn)
    events = []
    for i, line in enumerate([
        note("item/agentMessage/delta", threadId="thr-1", turnId="turn-1", itemId="m1", delta="Working on it\n"),
        note("item/reasoning/textDelta", threadId="thr-1", turnId="turn-1", itemId="r1", delta="RAW CHAIN"),
        note("item/reasoning/summaryTextDelta", threadId="thr-1", turnId="turn-1", itemId="r1", summaryIndex=0,
             delta="Plan: run tests\n"),
        note("item/started", threadId="thr-1", turnId="turn-1", startedAtMs=1,
             item={"type": "commandExecution", "id": "c1", "command": "pytest -q", "status": "inProgress"}),
        note("item/completed", threadId="thr-1", turnId="turn-1", completedAtMs=2,
             item={"type": "commandExecution", "id": "c1", "command": "pytest -q", "aggregatedOutput": "1 failed",
                   "exitCode": 1, "status": "completed"}),
        note("item/completed", threadId="thr-1", turnId="turn-1", completedAtMs=3,
             item={"type": "agentMessage", "id": "m1", "text": "Working on it\nDone."}),
        note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "completed", "items": []}),
    ]):
        step = turn.feed(line, 10 + i)
        events += step.events
    kinds = [e.kind for e in events]
    assert kinds == ["text.delta", "thinking.delta", "status", "tool.started", "tool.completed", "text", "turn.completed"]
    assert "RAW CHAIN" not in json.dumps([e.data for e in events])
    assert events[4].data["is_error"] is True and events[4].data["preview"] == "1 failed"
    assert turn.outcome.state == "complete" and step.frames[-1].tag == "close"


def test_item_starts_announce_where_the_model_is_once_per_change():
    """Design §12: a reasoning item that streams no summary still shows as
    thinking; an agent message as writing; a tool item as `tool`. A repeat is
    not announced, and nothing is after the turn ended."""
    turn = CodexTurn(spec())
    to_running(turn)

    def started(offset, item):
        return turn.feed(note("item/started", threadId="thr-1", turnId="turn-1", startedAtMs=offset, item=item),
                         offset)

    def phases(step):
        return [e.data["phase"] for e in step.events if e.kind == "status"]
    assert phases(started(10, {"type": "reasoning", "id": "r1", "summary": [], "content": []})) == ["thinking"]
    assert phases(started(11, {"type": "reasoning", "id": "r2", "summary": [], "content": []})) == []
    tool = started(12, {"type": "commandExecution", "id": "c1", "command": "ls", "status": "inProgress"})
    assert [e.kind for e in tool.events] == ["status", "tool.started"] and phases(tool) == ["tool"]
    assert phases(started(13, {"type": "reasoning", "id": "r3", "summary": [], "content": []})) == ["thinking"]
    assert phases(started(14, {"type": "agentMessage", "id": "m1", "text": ""})) == ["writing"]
    assert started(15, {"type": "userMessage", "id": "u1", "content": []}).events == []
    turn.feed(note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "completed", "items": []}), 16)
    assert phases(started(17, {"type": "reasoning", "id": "r4", "summary": [], "content": []})) == []


def test_codex_compaction_and_other_work_items_have_phases_and_replays_match():
    """A context compaction is `compacting`, then `requesting` once it completes;
    an item type the driver does not show (a sub-agent, a sleep) is still work
    (`tool`). Phases never depend on a stop, and a tool.started keeps its
    ordinal whether or not a phase came first on its line (C-26.6)."""
    items = [("item/started", {"type": "contextCompaction", "id": "k1"}),
             ("item/completed", {"type": "contextCompaction", "id": "k1"}),
             ("item/started", {"type": "collabAgentToolCall", "id": "x1"}),
             ("item/started", {"type": "reasoning", "id": "r1", "summary": [], "content": []}),
             ("item/started", {"type": "commandExecution", "id": "c1", "command": "ls", "status": "inProgress"}),
             ("item/started", {"type": "agentMessage", "id": "m1", "text": ""})]

    def run(stop_after):
        turn = CodexTurn(spec())
        to_running(turn)
        out = []
        for i, (method, item) in enumerate(items):
            step = turn.feed(note(method, threadId="thr-1", turnId="turn-1", startedAtMs=i, item=item), 10 + i)
            out += [(e.source, e.kind, e.data) for e in step.events]
            if stop_after == i:
                turn.interrupt()
        return out
    stopped, replayed = run(stop_after=2), run(stop_after=None)
    assert stopped == replayed
    assert [data["phase"] for src, kind, data in stopped if kind == "status"] == [
        "compacting", "requesting", "tool", "thinking", "tool", "writing"]
    assert [(src, kind) for src, kind, data in stopped if src.startswith("14:")] == [
        ("14:phase", "status"), ("14:1", "tool.started")]


def test_notifications_for_another_thread_are_ignored():
    """C-26.3 a shared server's other threads never reach this conversation."""
    turn = CodexTurn(spec())
    to_running(turn)
    assert turn.feed(note("turn/completed", threadId="other", turn={"id": "x", "status": "failed", "items": []}), 9).events == []


@pytest.mark.parametrize("method, params, decision, expected", [
    ("item/commandExecution/requestApproval", {"command": "rm -rf build", "reason": "cleanup"}, "allow", {"decision": "accept"}),
    ("item/commandExecution/requestApproval", {"command": "curl x"}, "allow-session", {"decision": "acceptForSession"}),
    ("item/fileChange/requestApproval", {"reason": "edit"}, "deny", {"decision": "decline"}),
    ("item/fileChange/requestApproval", {}, "cancel-turn", {"decision": "cancel"}),
    ("item/permissions/requestApproval", {"cwd": CWD, "permissions": {"network": {"enabled": True}}}, "allow-turn",
     {"permissions": {"network": {"enabled": True}}, "scope": "turn"}),
    ("item/permissions/requestApproval", {"cwd": CWD, "permissions": {"network": {"enabled": True}}}, "deny",
     {"permissions": {}}),
])
def test_approvals_round_trip_with_scoped_schema_valid_replies(method, params, decision, expected):
    """C-27.1, C-27.2: a server approval request waits for a person; the reply is exactly
    what the person chose, valid against the pinned schema, and never an amendment."""
    turn = CodexTurn(spec())
    to_running(turn)
    request = json.dumps({"id": 42, "method": method, "params": {"threadId": "thr-1", "turnId": "turn-1",
                                                                  "itemId": "i", "startedAtMs": 1, **params}})
    step = turn.feed(request, 20)
    assert step.approvals and step.approvals[0].provider_request_id == "42"
    assert turn.feed(request, 20).approvals == []
    reply = turn.respond("42", decision)
    body = json.loads(reply.frames[0].line)
    assert body == {"id": 42, "result": expected}
    check_frames(reply, {"42": method})
    assert turn.respond("42", decision).frames == []


def test_unsupported_server_requests_are_refused_and_never_answered_with_credentials():
    """C-27.4 credential refreshes and unknown requests get -32601; user input gets an empty answer."""
    turn = CodexTurn(spec())
    to_running(turn)
    refresh = turn.feed(json.dumps({"id": 7, "method": "account/chatgptAuthTokens/refresh", "params": {}}), 30)
    body = json.loads(refresh.frames[0].line)
    assert body["error"]["code"] == -32601 and "result" not in body
    ask = turn.feed(json.dumps({"id": 8, "method": "item/tool/requestUserInput", "params": {"threadId": "thr-1"}}), 31)
    check_frames(ask, {"8": "item/tool/requestUserInput"})


def test_a_limit_ends_the_turn_as_limited():
    """C-26.7 a usage-limit failure is `limited`."""
    turn = CodexTurn(spec())
    to_running(turn)
    step = turn.feed(note("turn/completed", threadId="thr-1", turn={
        "id": "turn-1", "status": "failed", "items": [],
        "error": {"message": "You've hit your usage limit", "codexErrorInfo": "usageLimitExceeded"}}), 40)
    assert step.outcome.state == "failed" and step.outcome.reason == "limited" and step.outcome.limited


def test_turn_start_refused_for_a_limit_was_never_accepted():
    """C-26.7 a turn/start error leaves the message unaccepted."""
    turn = CodexTurn(spec())
    to_thread(turn)
    turn.feed(resp(ID_THREAD, {"thread": {"id": "thr-1", "status": {"type": "idle"}}, "model": "gpt-6-astra"}), 3)
    step = turn.feed(resp(ID_TURN, error={"code": -32000, "message": "limit", "codexErrorInfo": "usageLimitExceeded"}), 4)
    assert step.outcome.reason == "limited" and not step.outcome.accepted


def test_interrupt_waits_for_the_turn_id_then_uses_turn_interrupt():
    """C-24.7 a stop requested before the turn id is known is sent as soon as it is."""
    turn = CodexTurn(spec())
    to_thread(turn)
    turn.feed(resp(ID_THREAD, {"thread": {"id": "thr-1", "status": {"type": "idle"}}, "model": "gpt-6-astra"}), 3)
    early = turn.interrupt()
    assert early.frames == []
    step = turn.feed(resp(ID_TURN, {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}), 4)
    body = json.loads(step.frames[-1].line)
    assert body["method"] == "turn/interrupt" and body["params"] == {"threadId": "thr-1", "turnId": "turn-1"}
    check_frames(step)
    end = turn.feed(note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "interrupted", "items": []}), 5)
    assert end.outcome.state == "interrupted"


def test_model_mismatch_on_the_thread_stops_before_the_turn():
    """C-26.8 the thread reports the model it will serve; a different one ends the turn unsent."""
    turn = CodexTurn(spec())
    to_thread(turn)
    step = turn.feed(resp(ID_THREAD, {"thread": {"id": "thr-1", "status": {"type": "idle"}}, "model": "gpt-5.6-luna"}), 3)
    assert step.outcome.reason == "model-mismatch" and all(f.tag != "user-message" for f in step.frames)


@pytest.mark.parametrize("bad", [
    {"id": 5, "method": "turn/start", "params": {"threadId": "t", "input": [], "sandboxPolicy": {"type": "bogus"}}},
    {"id": 5, "method": "turn/start", "params": {"input": []}},
    {"id": 5, "method": "turn/unknownMethod", "params": {}},
    {"id": 4, "method": "thread/start", "params": {"sandbox": "everything"}},
])
def test_the_schema_check_is_not_vacuous(bad):
    """C-26.8 the pinned schema rejects malformed frames, so the checks above can fail."""
    assert errors(bad, CLIENT_REQUEST, CLIENT_REQUEST)


def test_a_turn_that_used_a_tool_ends_when_the_thread_goes_idle():
    """C-26.5: live 0.153.3 sends no `turn/completed` after a tool ran; `idle` after
    the turn started ends it, but only once asked to settle (the runner's grace)."""
    turn = CodexTurn(spec())
    to_running(turn)
    turn.feed(note("thread/status/changed", threadId="thr-1", status={"type": "active", "activeFlags": []}), 10)
    turn.feed(note("item/completed", threadId="thr-1", turnId="turn-1", completedAtMs=3,
                   item={"type": "agentMessage", "id": "m1", "text": "Done."}), 11)
    idle = turn.feed(note("thread/status/changed", threadId="thr-1", status={"type": "idle"}), 12)
    assert idle.outcome is None and turn.idle_pending
    step = turn.settle_idle()
    assert turn.outcome.state == "complete" and step.frames[-1].tag == "close"
    assert step.events[-1].kind == "turn.completed" and step.events[-1].data["ended_by"] == "thread-idle"
    assert turn.settle_idle().events == []          # once only


def test_idle_before_the_turn_started_is_not_an_end_and_turn_completed_wins():
    turn = CodexTurn(spec())
    to_thread(turn)
    turn.feed(note("thread/status/changed", threadId=None, status={"type": "idle"}), 3)
    assert not turn.idle_pending
    turn2 = CodexTurn(spec())
    to_running(turn2)
    turn2.feed(note("thread/status/changed", threadId="thr-1", status={"type": "idle"}), 10)
    done = turn2.feed(note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "completed", "items": []}), 11)
    assert done.outcome.state == "complete" and turn2.settle_idle().events == []


def test_idle_after_a_final_error_or_a_stop_settles_to_that():
    limited = CodexTurn(spec())
    to_running(limited)
    limited.feed(note("error", threadId="thr-1", turnId="turn-1", willRetry=False,
                      error={"message": "limit", "codexErrorInfo": "usageLimitExceeded"}), 10)
    limited.feed(note("thread/status/changed", threadId="thr-1", status={"type": "idle"}), 11)
    assert limited.settle_idle().outcome.reason == "limited"
    stopped = CodexTurn(spec())
    to_running(stopped)
    stopped.interrupt()
    stopped.feed(note("thread/status/changed", threadId="thr-1", status={"type": "idle"}), 11)
    assert stopped.settle_idle().outcome.state == "interrupted"


def test_a_blocking_hook_is_shown_as_activity():
    """C-26.11: the never-rules guard blocking a command reaches the person (observed live)."""
    turn = CodexTurn(spec())
    to_running(turn)
    step = turn.feed(note("hook/completed", threadId="thr-1", turnId="turn-1", run={
        "id": "pre-tool-use:0", "eventName": "preToolUse", "status": "blocked", "statusMessage": "never-rules guard",
        "entries": [{"kind": "feedback", "text": "[local-main] Branch from origin/main"}]}), 10)
    assert step.events[0].kind == "hook" and step.events[0].data["status"] == "blocked"
    assert "local-main" in step.events[0].data["feedback"]
    quiet = turn.feed(note("hook/completed", threadId="thr-1", run={"status": "completed", "entries": []}), 11)
    assert quiet.events == []


def test_each_outcome_says_what_ended_the_turn():
    """C-24.6: `turn/completed` and an error answer to `turn/start` are the provider's; a
    refusal on the thread is the driver's; the end of stdout is `eof`. A `turn/completed`
    after the driver ended the turn is noted."""
    done = CodexTurn(spec())
    to_running(done)
    end = done.feed(note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "completed",
                                                                  "items": []}), 40)
    assert end.outcome.ended_by == "provider"
    refused = CodexTurn(spec())
    to_thread(refused)
    refused.feed(resp(ID_THREAD, {"thread": {"id": "thr-1", "status": {"type": "idle"}}, "model": "gpt-6-astra"}), 3)
    assert refused.feed(resp(ID_TURN, error={"code": -32000, "message": "no"}), 4).outcome.ended_by == "provider"
    busy = CodexTurn(spec(native_session_id="thr-9"))
    to_thread(busy)
    step = busy.feed(resp(ID_THREAD, {"thread": {"id": "thr-9", "status": {"type": "active"}}, "model": "gpt-6-astra"}), 3)
    assert step.outcome.ended_by == "driver"
    busy.feed(note("turn/completed", threadId="thr-9", turn={"id": "t", "status": "completed", "items": []}), 4)
    assert busy.terminal_after_end
    cut = CodexTurn(spec())
    to_running(cut)
    assert cut.eof(50).outcome.ended_by == "eof"
