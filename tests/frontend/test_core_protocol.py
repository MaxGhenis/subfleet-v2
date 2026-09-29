"""The app's protocol models against JSON the daemon's own code produces (C-25.1, C-25.2, C-29.1).

Results come from the real `ConversationService` handlers over a real store
(`daemon_harness.py`), events from the real provider drivers. Each is decoded
by the Swift model for its op and encoded again: nothing the daemon sent may be
lost. Requests the Swift side encodes are fed to the daemon's own
`protocol.decode_request` and handlers.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import uuid

import pytest

from subfleet import protocol
from subfleet.conversations.service import CAPABILITIES
from subfleet.conversations.store import ConversationError
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import (
    make_live, ServiceHarness, claude_assistant, claude_init, claude_result, claude_stream,
)

pytestmark = needs_swift


def strip_nulls(value):
    """Optional fields left out on encoding are equal to nulls on the wire."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


@pytest.fixture
def harness():
    root = Path(tempfile.mkdtemp(prefix="sf-app-h-", dir="/tmp"))
    harness = ServiceHarness(root)
    yield harness
    harness.close()


def roundtrip(core_probe, tmp_path, op: str, result: dict) -> dict:
    path = write_json(tmp_path / f"{op}.json", result)
    return json.loads(run_probe(core_probe, "roundtrip", op, path, raw=True))


def assert_lossless(core_probe, tmp_path, op: str, result: dict) -> dict:
    back = roundtrip(core_probe, tmp_path, op, result)
    assert strip_nulls(back) == strip_nulls(json.loads(json.dumps(result))), op
    return back


def test_c25_2_the_app_knows_every_conversation_op(core_probe):
    """Swift's op table is the daemon's CONVERSATION_OPS, in order."""
    assert run_probe(core_probe, "ops") == list(protocol.CONVERSATION_OPS)


def populated(harness: ServiceHarness) -> dict:
    """A conversation with a finished turn, an approval turn and a queued follow-up."""
    conversation = harness.create(title="Fixture")
    cid = conversation["conversation_id"]
    first = harness.submit(cid, "hello")
    turn = harness.attempt(cid, first["message_id"])
    turn.feed(claude_init(), {"type": "user", "uuid": first["message_id"], "isReplay": True,
                              "message": {"role": "user", "content": "hello"}},
              {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "permissionMode": "default",
               "fast_mode_state": "off"},
              *claude_stream("msg_a", ["Hel", "lo\nthere"]),
              claude_assistant("msg_a", [{"type": "text", "text": "Hello\nthere"}]), claude_result())
    second = harness.submit(cid, "run it", after=first["message_id"])
    asking = harness.attempt(cid, second["message_id"])
    asking.feed(claude_init(), {"type": "user", "uuid": second["message_id"], "isReplay": True,
                                "message": {"role": "user", "content": "run it"}},
                claude_assistant("msg_b", [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                            "input": {"command": "echo token=sk-ant-abcdefghijklmnopqrstu", "description": "Say"}}]),
                {"type": "control_request", "request_id": "perm-1", "request": {
                    "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "toolu_1",
                    "input": {"command": "echo token=sk-ant-abcdefghijklmnopqrstu", "description": "Say"},
                    "decision_reason": "asks every time", "blocked_path": "/etc/hosts"}})
    third = harness.submit(cid, "later", after=second["message_id"])
    return {"cid": cid, "first": first, "second": second, "third": third, "asking": asking}


def test_c25_2_every_result_decodes_without_losing_a_field(core_probe, tmp_path, harness, monkeypatch):
    """C-25.2: each op's real result survives the Swift model unchanged."""
    fixture = populated(harness)
    cid = fixture["cid"]
    approval = harness.call("approval.list", conversation_id=cid)["approvals"][0]
    # A catalog with one native session, as catalog.build writes it.
    (harness.root / "catalog.json").write_text(json.dumps({"generated_at": "2026-09-24T12:00:00.000Z", "complete": True,
        "items": [{"provider": "claude", "native_session_id": str(uuid.uuid4()), "path": "/x/y.jsonl", "home": None,
                   "title": None, "first_prompt": "fix the build", "cwd": str(harness.workspace), "model": "claude-opus-5-5",
                   "permission_mode": "default", "mtime": 1790000000.25, "continuable": True, "continue_blocker": None,
                   "archived": False, "live_elsewhere": True}]}))
    (harness.root / "conversations").mkdir(exist_ok=True)
    results = {
        "capabilities": harness.call("capabilities"),
        "models.list": harness.call("models.list", provider="claude"),
        "conversation.list": harness.call("conversation.list", limit=2),
        "conversation.open": harness.call("conversation.open", conversation_id=cid),
        "conversation.create": harness.call("conversation.create", request_id="fixed-id", provider="claude",
                                            workspace=str(harness.workspace), settings=harness.settings()),
        "conversation.settings": harness.call("conversation.settings", conversation_id=cid,
                                              settings={"effort": "high", "fast": True}),
        "conversation.history": harness.call("conversation.history", conversation_id=cid),
        "conversation.events": harness.call("conversation.events", conversation_id=cid, after=0),
        "conversation.watch": harness.call("conversation.watch", after=0),
        "message.submit": harness.submit(cid, "fourth", after=fixture["third"]["message_id"]),
        "message.status": harness.call("message.status", message_ids=[fixture["first"]["message_id"], str(uuid.uuid4())]),
        "message.cancel": harness.call("message.cancel", message_id=fixture["third"]["message_id"]),
        "approval.list": harness.call("approval.list", conversation_id=cid),
        "approval.get": harness.call("approval.get", approval_id=approval["approval_id"]),
        "attachment.add": None,
        "catalog.refresh": {"requested": True, "running": True},     # the op starts a process; its shape
    }
    image = tmp_path / "pixel.png"
    image.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                                    "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4a50000000049454e44ae426082"))
    results["attachment.add"] = harness.call("attachment.add", path=str(image))
    # conversation.runs reads the job store; the harness has none, so one real
    # store holds a run with two attempts and the real op answers from it.
    from subfleet.contracts import Credential, Lane, LaneOwner
    from subfleet.store import Store
    jobs = Store(tmp_path / "state.sqlite3")
    jobs.put_lane(Lane("codex-2", "codex", "codex:two", Credential("codex", "/h", "home"), "/h", LaneOwner.V2, False))
    runs_cid = harness.create()["conversation_id"]
    harness.store.update_conversation(runs_cid, native_session_id="s-runs")
    jobs.add_job(job_id="j-1", request_id="r-1", payload_digest="d", kind="dispatch", workdir="/w", prompt_path="/p",
                 sandbox="workspace-write", name="review-pr", caller_session="s-runs", state="running",
                 task="review", tier="hard", started_at="2026-09-25T02:00:00Z")
    jobs.add_attempt(attempt_id="j-1/a1", job_id="j-1", seq=1, lane_id="codex-2", model_requested="gpt-6-astra",
                     model_served="gpt-6-astra", state="running")
    main_store, harness.daemon.store = harness.daemon.store, jobs
    try:
        results["conversation.runs"] = harness.call("conversation.runs", conversation_id=runs_cid)
    finally:
        harness.daemon.store = main_store
    assert results["conversation.runs"]["runs"][0]["lane_id"] == "codex-2"
    # Unblock and resolve need their blocked states, set as the daemon sets them.
    harness.store.update_conversation(cid, blocked_by="unfinished-turn")
    results["conversation.unblock"] = harness.call("conversation.unblock", conversation_id=cid, choice="continue",
                                                   confirm=True)
    unknown = harness.submit(cid, "ambiguous", after=json.loads(json.dumps(results["message.submit"]))["message_id"])
    harness.store.set_state(unknown["message_id"], "delivery-unknown", reason="no-evidence")
    results["message.resolve"] = harness.call("message.resolve", message_id=unknown["message_id"],
                                              resolution="not-delivered", confirm=True)
    # message.steer (C-24.9) answers a Receipt with `steered_into`: the daemon's own op,
    # steering into a live runner for a turn that runs (daemon_harness.make_live).
    steered = harness.submit(cid, "steer me", after=unknown["message_id"])
    live = harness.submit(cid, "a running turn", after=steered["message_id"])
    make_live(harness, cid, live["message_id"])
    results["message.steer"] = harness.call("message.steer", message_id=steered["message_id"],
                                            into=live["message_id"])
    assert results["message.steer"]["steered_into"] == live["message_id"]
    # approval.respond answers through the live runner; the harness stands in for it.
    runner = SimpleNamespace(driver=SimpleNamespace(outcome=None), respond=lambda *a: None,
                             interrupt=lambda reason: None, stop=lambda: None, join=lambda timeout: True,
                             message_id=fixture["second"]["message_id"], finished=threading.Event())
    harness.service.runners[harness.store.approval(approval["approval_id"])["attempt_id"]] = runner
    detail = results["approval.get"]
    results["approval.respond"] = harness.call("approval.respond", approval_id=approval["approval_id"], decision="allow",
                                               nonce=detail["nonce"], request_sha256=detail["request_sha256"])
    results["turn.interrupt"] = harness.call("turn.interrupt", message_id=fixture["second"]["message_id"])
    # The diff ops' unavailable shapes; tests/frontend/test_core_diff.py has the real diffs.
    results["turn.diff"] = harness.call("turn.diff", message_id=fixture["third"]["message_id"])
    results["conversation.diff"] = harness.call("conversation.diff", conversation_id=cid)
    # A real handoff (C-30.3): a Claude conversation bound to a fixture transcript,
    # with one pending message, handed to Codex.
    from tests import sessions_fixtures as fx
    claude = fx.claude_home(tmp_path, monkeypatch)
    session = str(uuid.uuid4())
    fx.transcript(claude, session, [fx.typed_prompt("Port the importer.", uuid="p0", at=fx.ago(3600)),
                                    fx.assistant_text("the manifest is done", uuid="a0", at=fx.ago(60))],
                  cwd=str(harness.workspace))
    source, _ = harness.store.create_conversation(
        provider="claude", workspace=str(harness.workspace), workspace_kind="in-place", settings=harness.settings(),
        origin="native", native_session_id=session, title="handoff source")
    harness.submit(source["conversation_id"], "still to do")
    results["conversation.handoff"] = harness.call(
        "conversation.handoff", request_id="handoff-1", **{"from": {"conversation_id": source["conversation_id"]},
                                                           "to": {"provider": "codex", "settings": {
                                                               "model": "gpt-6-astra", "permission": "read-only"}}})
    assert len(results["conversation.handoff"]["moved"]) == 1 and results["conversation.handoff"]["created"]
    # An idempotent steer receipt exercises the fixed wire shape without launching
    # a provider (the daemon-side steer tests cover the initial durable claim).
    steered = harness.submit(source["conversation_id"], "already delivered",
                             after=harness.store.messages(source["conversation_id"])[-1]["message_id"])
    harness.store.set_state(steered["message_id"], "steered", reason=f"steered:{fixture['first']['message_id']}",
                            served={"steered_into": fixture["first"]["message_id"]})
    results["message.steer"] = harness.call("message.steer", message_id=steered["message_id"])
    assert set(results) == set(protocol.CONVERSATION_OPS)
    for op, result in results.items():
        assert_lossless(core_probe, tmp_path, op, result)

    # Spot checks on what the app reads from them.
    assert results["capabilities"]["capabilities"] == list(CAPABILITIES)
    kinds = [e["kind"] for e in results["conversation.events"]["events"]]
    assert {"status", "served", "accepted", "text.delta", "text", "tool.started", "approval.requested",
            "turn.completed"} <= set(kinds)
    shown = roundtrip(core_probe, tmp_path, "approval.get", results["approval.get"])
    assert shown["masked"] and shown["masked"][0]["rule"] == "token"
    assert "[masked token" in json.dumps(shown["request"])
    assert results["message.status"]["messages"][1] == {"message_id": results["message.status"]["messages"][1]["message_id"],
                                                        "state": "unknown"}


def test_c25_2_requests_the_app_encodes_are_the_daemons_requests(core_probe, tmp_path, harness):
    """The Swift request lines pass `protocol.decode_request`, and the handlers accept their args."""
    conversation = harness.create()
    cid = conversation["conversation_id"]
    mid = str(uuid.uuid4())
    settings = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
    requests = {
        "message.submit": {"conversation_id": cid, "message_id": mid, "after_message_id": None, "text": "hi",
                           "attachments": [], "settings": settings},
        "conversation.settings": {"conversation_id": cid, "settings": {**settings, "effort": None}},
        "conversation.events": {"conversation_id": cid, "after": 0, "wait_s": 0},
        "conversation.watch": {"after": 0, "wait_s": 0},
        "message.status": {"message_ids": [mid]},
        "conversation.open": {"conversation_id": cid},
        "conversation.list": {"query": "fix", "limit": 5, "include_catalog": True},
        "models.list": {"provider": "claude"},
        "capabilities": {},
        "approval.list": {"conversation_id": cid},
        "conversation.history": {"conversation_id": cid},
        "turn.interrupt": {"message_id": mid},
        "message.steer": {"message_id": mid},
    }
    for op, args in requests.items():
        line = run_probe(core_probe, "request", op, write_json(tmp_path / "args.json", args), "app-1", raw=True)
        assert line.endswith("\n") and line.count("\n") == 1
        request = protocol.decode_request(line.encode())
        assert (request.op, request.id) == (op, "app-1")
        wire = json.loads(line)
        assert wire["v"] == 1 and set(wire) == {"v", "id", "op", "args"}
        if op == "message.submit":
            # The first message says it has no predecessor with an explicit null.
            assert "after_message_id" in wire["args"] and wire["args"]["after_message_id"] is None
            assert wire["args"]["settings"]["effort"] is None and "effort" in wire["args"]["settings"]
        if op in ("turn.interrupt", "message.steer"):
            with pytest.raises(Exception):         # the message is queued, not running
                harness.call(op, **request.args)
            continue
        harness.call(op, **request.args)
    # The encoded submit was accepted as the conversation's first message.
    assert harness.call("message.status", message_ids=[mid])["messages"][0]["seq"] == 1
    unblock = run_probe(core_probe, "request", "conversation.unblock",
                        write_json(tmp_path / "u.json", {"conversation_id": cid, "choice": "leave"}), raw=True)
    assert json.loads(unblock)["args"] == {"conversation_id": cid, "choice": "leave", "confirm": True}
    resolve = run_probe(core_probe, "request", "message.resolve",
                        write_json(tmp_path / "r.json", {"message_id": mid, "resolution": "not-delivered"}), raw=True)
    assert json.loads(resolve)["args"] == {"message_id": mid, "resolution": "not-delivered", "confirm": True}


def test_c25_2_refusals_decode_with_their_reason(core_probe, tmp_path, harness):
    """The daemon's own error lines: `out-of-order` is recognised; an unknown op and a stray id are handled."""
    cid = harness.create()["conversation_id"]
    args = {"conversation_id": cid, "message_id": str(uuid.uuid4()), "after_message_id": str(uuid.uuid4()),
            "text": "x", "attachments": [], "settings": harness.settings()}
    line = harness.response_line("message.submit", args, "app-9")
    decoded = run_probe(core_probe, "response", "message.submit", write_json(tmp_path / "line", json.loads(line)), "app-9")
    assert decoded["error"]["kind"] == "daemon" and decoded["error"]["code"] == 2
    assert decoded["error"]["reason"] == "out-of-order"
    assert decoded["error"]["detail"] == "the message's predecessor has not been accepted yet"
    assert decoded["error"]["fix"].startswith("send the earlier message first")

    def refuse(peer, what):
        # service._person's refusal, as an agent's request gets it.
        raise ConversationError("person-only", f"{what} is a person's decision: only the Subfleet app or a "
                                "terminal may do this", code=7, fix="answer it in the Subfleet app")
    harness.service._person = refuse
    refusal = harness.response_line("conversation.unblock", {"conversation_id": cid, "choice": "leave", "confirm": True})
    decoded = run_probe(core_probe, "response", "conversation.unblock", write_json(tmp_path / "p", json.loads(refusal)),
                        "fixture")
    assert decoded["error"]["code"] == 7 and decoded["error"]["reason"] == "person-only"

    # An older daemon answers an op it does not know with id "" (daemon._connection).
    try:
        protocol.decode_request(b'{"v":1,"id":"app-1","op":"conversation.frobnicate","args":{}}')
    except protocol.ProtocolError as exc:
        unknown = protocol.encode(protocol.fail("", 2, str(exc)))
    decoded = run_probe(core_probe, "response", "capabilities", write_json(tmp_path / "u", json.loads(unknown)), "app-1")
    assert decoded["error"]["kind"] == "daemon" and decoded["error"]["message"].startswith("unknown op")

    ok = protocol.encode(protocol.ok("someone-else", {"requested": True, "running": False}))
    decoded = run_probe(core_probe, "response", "catalog.refresh", write_json(tmp_path / "o", json.loads(ok)), "app-1")
    assert decoded["error"]["kind"] == "malformed"
    wrong = protocol.encode(protocol.ok("app-1", {"requested": "yes"}))
    decoded = run_probe(core_probe, "response", "catalog.refresh", write_json(tmp_path / "w", json.loads(wrong)), "app-1")
    assert decoded["error"]["kind"] == "malformed"


@pytest.mark.parametrize("op,args,seconds", [
    ("conversation.events", {"conversation_id": "cv", "after": 0, "wait_s": 25}, 40),
    ("conversation.watch", {"after": 3, "wait_s": 25}, 40),
    ("conversation.events", {"conversation_id": "cv", "after": 0}, 15),
    ("message.submit", {"conversation_id": "cv", "message_id": "m", "after_message_id": None, "text": "x",
                        "attachments": [], "settings": {"model": "opus", "permission": "ask"}}, 15),
])
def test_design_12_timeouts(core_probe, tmp_path, op, args, seconds):
    """15 s, or wait_s + 15 for a long poll."""
    assert run_probe(core_probe, "timeout", op, write_json(tmp_path / "a.json", args)) == seconds


def test_c25_1_capabilities_decide_whether_conversation_ops_are_sent(core_probe, tmp_path, harness):
    capabilities = harness.call("capabilities")
    assert run_probe(core_probe, "availability", write_json(tmp_path / "c.json", capabilities)) == {"ready": True}
    older = {**capabilities, "conversation_schema": 2}
    assert "schema" in run_probe(core_probe, "availability", write_json(tmp_path / "c.json", older))["incompatible"]
    without = {**capabilities, "capabilities": ["events.v1"]}
    assert "conversations.v1" in run_probe(core_probe, "availability", write_json(tmp_path / "c.json", without))["incompatible"]
