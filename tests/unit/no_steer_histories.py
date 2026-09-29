"""Turn histories with no steer in them, for design §5 invariant 5 (C-24.9): a turn
that no steer reaches drives, writes and settles exactly as it did before steer.

`record()` runs every history through whichever `subfleet` is importable, at two
levels: the pure drivers (`ClaudeTurn`, `CodexTurn`: every frame, event, answer
and outcome), and the real runner loop (`TurnRunner._run` on its thread, a relay
journal for the guardian, the test as the provider) settled by the real service
(the host's row, its conversation's block, the relay log, the stored events and
`turn.json`). tests/fixtures/steer/no_steer_presteer.json is what the pre-steer
code did: commit 52ffde17, the merge base of the steer work. Recorded with

    mkdir /tmp/presteer && git archive 52ffde17 subfleet tests | tar -x -C /tmp/presteer
    cp tests/unit/no_steer_histories.py /tmp/presteer/tests/unit/
    cd /tmp/presteer && PYTHONPATH=/tmp/presteer <checkout>/.venv/bin/python \\
        -m tests.unit.no_steer_histories > <checkout>/tests/fixtures/steer/no_steer_presteer.json

(`PYTHONPATH` puts the old `subfleet` ahead of the checkout's editable install;
the recording's first line on stderr names the one it used.)

`test_steer_invariants.py` runs the same histories through the current code and
compares. Only what steer added to every turn is set aside: the empty `steers` of
an outcome and of `turn.json`.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import threading

from subfleet.guard.preflight import HOOK_KEY

MID = "7f1c9a0e-1111-4222-8333-444455556666"        # the host message
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"        # its Claude session
EMAIL = "max@example.org"
CWD = "/Users/max/repo"
GUARD = "sha256:" + "a" * 64

# --- Claude -------------------------------------------------------------------------------

CLAUDE_INIT = {"type": "control_response", "response": {
    "subtype": "success", "request_id": "subfleet-init", "response": {
        "account": {"email": EMAIL}, "fast_mode_state": "off",
        "models": [{"value": "opus", "resolvedModel": "claude-opus-5-5", "supportsEffort": True,
                    "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]}]}}}
CLAUDE_SETTINGS = {"type": "control_response", "response": {
    "subtype": "success", "request_id": "subfleet-settings", "response": {"applied": {"effort": "high"}}}}
SYSTEM_INITS = {
    "no-init": [],
    "plain-init": [{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": SID}],
    # A CLI that could take steers: the turn has none, so nothing may change.
    "steer-init": [{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": SID,
                    "capabilities": ["msg_lifecycle_v1", "interrupt_receipt_v1", "interrupt_cancel_queued_v1"]}],
}
CLAUDE_ACKS = {
    "replay": [{"type": "user", "uuid": MID, "isReplay": True, "message": {"role": "user", "content": "fix it"}}],
    "lifecycle": [{"type": "command_lifecycle", "command_uuid": MID, "state": "started"}],
    "none": [],
}
CLAUDE_TEXT = [
    {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg_1"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": "Looking\n"}}},
    {"type": "assistant", "message": {"id": "msg_1", "model": "claude-opus-5-5",
                                      "content": [{"type": "text", "text": "Looking into it."}]}},
]
CLAUDE_TOOL = [
    {"type": "assistant", "message": {"id": "msg_2", "model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
    {"type": "control_request", "request_id": "req-1", "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "pytest -q"}, "tool_use_id": "tu1"}},
    ["respond", "req-1", "allow"],
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "3 passed", "is_error": False}]}},
]
CLAUDE_STOP = [["interrupt"], {"type": "control_response", "response": {
    "subtype": "success", "request_id": "subfleet-interrupt", "response": {}}}]
CLAUDE_ENDS = {
    "success": [{"type": "result", "subtype": "success", "is_error": False, "result": "Fixed.", "num_turns": 2,
                 "user_message_uuids": [MID], "queued_turn_count": 0}],
    "error": [{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
               "errors": ["aborted"], "num_turns": 1}],
    "eof": [["eof"]],
}


def claude_cases() -> list[dict]:
    cases = []
    for init_name, inits in SYSTEM_INITS.items():
        for ack_name, acks in CLAUDE_ACKS.items():
            for body_name, body in (("text", CLAUDE_TEXT), ("tool", CLAUDE_TEXT + CLAUDE_TOOL)):
                for stop_name, stop in (("run", []), ("stop", CLAUDE_STOP)):
                    for end_name, end in CLAUDE_ENDS.items():
                        actions = [["start"], CLAUDE_INIT, *inits, *acks, CLAUDE_SETTINGS, *body, *stop, *end]
                        cases.append({"name": f"claude/{init_name}/{ack_name}/{body_name}/{stop_name}/{end_name}",
                                      "provider": "claude", "actions": actions})
    return cases


# --- Codex --------------------------------------------------------------------------------

CODEX_MODELS = {"data": [{"id": "gpt-6-astra", "model": "gpt-6-astra", "supportedReasoningEfforts": [
    {"reasoningEffort": "high", "description": ""}], "serviceTiers": []}]}
CODEX_HOOKS = {"data": [{"cwd": CWD, "hooks": [{"key": HOOK_KEY, "enabled": True, "trustStatus": "trusted",
                                                  "currentHash": GUARD}], "warnings": [], "errors": []}]}


def _resp(rid, result):
    return {"id": rid, "result": result}


def _note(method, **params):
    return {"method": method, "params": params}


CODEX_THREAD = [_resp(1, {"userAgent": "codex"}), _resp(2, CODEX_HOOKS), _resp(3, CODEX_MODELS),
                _resp(4, {"thread": {"id": "thr-1", "status": {"type": "idle"}}, "model": "gpt-6-astra",
                          "reasoningEffort": "high", "approvalPolicy": "on-request", "sandbox": {}})]
CODEX_ACCEPT = [_resp(5, {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}),
                _note("turn/started", threadId="thr-1", turn={"id": "turn-1", "status": "inProgress"})]
# An item that names the turn before the turn/start answer does (steer's turn id filter).
CODEX_EARLY = [_note("item/started", threadId="thr-1", turnId="turn-1",
                     item={"id": "early", "type": "reasoning", "summary": []})]
CODEX_TEXT = [
    _note("item/started", threadId="thr-1", turnId="turn-1", item={"id": "m1", "type": "agentMessage", "text": ""}),
    _note("item/agentMessage/delta", threadId="thr-1", turnId="turn-1", itemId="m1", delta="Looking\n"),
    _note("item/completed", threadId="thr-1", turnId="turn-1",
          item={"id": "m1", "type": "agentMessage", "text": "Looking into it."}),
]
CODEX_TOOL = [
    {"id": 42, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "thr-1", "turnId": "turn-1", "itemId": "c1", "startedAtMs": 1, "command": "pytest -q"}},
    ["respond", "42", "allow"],
    _note("item/started", threadId="thr-1", turnId="turn-1",
          item={"id": "c1", "type": "commandExecution", "command": "pytest -q", "status": "inProgress"}),
    _note("item/completed", threadId="thr-1", turnId="turn-1",
          item={"id": "c1", "type": "commandExecution", "command": "pytest -q", "status": "completed",
                "aggregatedOutput": "3 passed", "exitCode": 0}),
]
CODEX_STOP = [["interrupt"], _resp(6, {})]
CODEX_ENDS = {
    "completed": [_note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "completed", "items": []})],
    "failed": [_note("turn/completed", threadId="thr-1", turn={
        "id": "turn-1", "status": "failed", "items": [], "error": {"message": "model error"}})],
    "interrupted": [_note("turn/completed", threadId="thr-1",
                          turn={"id": "turn-1", "status": "interrupted", "items": []})],
    "eof": [["eof"]],
}


def codex_cases() -> list[dict]:
    cases = []
    for early_name, early in (("in-order", []), ("early-item", CODEX_EARLY)):
        for body_name, body in (("text", CODEX_TEXT), ("tool", CODEX_TEXT + CODEX_TOOL)):
            for stop_name, stop in (("run", []), ("stop", CODEX_STOP)):
                for end_name, end in CODEX_ENDS.items():
                    actions = [["start"], *CODEX_THREAD, *early, *CODEX_ACCEPT, *body, *stop, *end]
                    cases.append({"name": f"codex/{early_name}/{body_name}/{stop_name}/{end_name}",
                                  "provider": "codex", "actions": actions})
    return cases


def cases() -> list[dict]:
    return claude_cases() + codex_cases()


#: The histories also run through the runner loop: those a provider writes alone
#: (no person's answer or stop in the middle), for each way a turn ends.
def runner_cases() -> list[dict]:
    return [case for case in cases() if "/text/run/" in case["name"]
            and "/none/" not in case["name"] and "/plain-init/" not in case["name"]]


def spec(provider: str):
    from subfleet.conversations.turn import TurnSpec
    if provider == "claude":
        return TurnSpec(provider="claude", message_id=MID, text="fix it", model_id="opus", permission="ask",
                        native_session_id=SID, effort="high", lane_email=EMAIL)
    return TurnSpec(provider="codex", message_id=MID, text="fix it", model_id="gpt-6-astra", permission="ask",
                    native_session_id=None, effort="high", cwd=CWD, guard_hash=GUARD)


# --- the drivers ----------------------------------------------------------------------------


def _outcome(outcome) -> dict | None:
    if outcome is None:
        return None
    out = asdict(outcome)
    out.pop("steers", None)                 # steer's own field: empty for a turn with no steers
    return out


def _step(step) -> dict:
    return {"frames": [[f.tag, f.op, f.line] for f in step.frames],
            "events": [[e.kind, e.data, e.source] for e in step.events],
            "approvals": [asdict(a) for a in step.approvals], "resolved": list(step.resolved),
            "outcome": _outcome(step.outcome)}


def drive(case: dict) -> dict:
    """Every step a driver takes for one history, and its outcome."""
    from subfleet.conversations.claude_turn import ClaudeTurn
    from subfleet.conversations.codex_turn import CodexTurn
    turn = (ClaudeTurn(spec("claude"), read_bytes=lambda image: b"") if case["provider"] == "claude"
            else CodexTurn(spec("codex")))
    steps, offset = [], 0
    for action in case["actions"]:
        if isinstance(action, dict):
            line = json.dumps(action)
            step = turn.feed(line, offset)
            offset += len(line) + 1
        elif action[0] == "start":
            step = turn.start()
        elif action[0] == "interrupt":
            step = turn.interrupt()
        elif action[0] == "respond":
            step = turn.respond(action[1], action[2])
        else:
            step = turn.eof(offset)
        steps.append(_step(step))
    return {"steps": steps, "outcome": _outcome(turn.outcome)}


# --- the runner loop and the service's settlement --------------------------------------------


class JournalRelay:
    """The guardian's intent and written records, without a provider pipe."""

    def __init__(self, adir: Path):
        self.path = adir / "stdin.jsonl"

    def send(self, seq, op, *, line=None, tag=None, sig=None):
        from subfleet.relay import Ack, frame_sha256
        with self.path.open("a") as stream:
            stream.write(json.dumps({"kind": "intent", "seq": seq, "op": op, "tag": tag,
                                     "sha256": frame_sha256(op, line, sig), "line": line}) + "\n")
            stream.write(json.dumps({"kind": "written", "seq": seq}) + "\n")
        return Ack(seq, True)

    def close(self) -> None:
        pass


#: `turn.json` keys that differ run to run.
VOLATILE = ("ended_at", "recorded_at", "updated_at", "started_at", "wall_s")


def run(case: dict, timeout: float = 120.0) -> dict:
    """The history through the real runner loop, settled by the real service."""
    from subfleet.conversations.classify import read_turn
    from subfleet.conversations.runner import TurnRunner
    from subfleet.conversations.service import ConversationService
    from subfleet.relay import read_log
    from tests.unit.test_conversation_service import CODEX_SETTINGS, SETTINGS, FakeDaemon

    provider = case["provider"]
    with tempfile.TemporaryDirectory(prefix="no-steer-") as directory:
        root = Path(directory)
        daemon = FakeDaemon(root)
        service = ConversationService(daemon)
        try:
            settings = {**(SETTINGS if provider == "claude" else CODEX_SETTINGS), "permission": "ask"}
            cid = service.store.create_conversation(provider=provider, workspace=directory, workspace_kind="in-place",
                                                    settings=settings, origin="new")[0]["conversation_id"]
            service.store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None, text="fix it",
                                         attachments=[], settings=settings)
            service.store.set_state(MID, "running")
            adir = root / "attempt"
            adir.mkdir()
            with (adir / "stdout").open("w") as stdout:
                for action in case["actions"]:
                    if isinstance(action, dict):
                        stdout.write(json.dumps(action) + "\n")
            (adir / "exit.json").write_text(json.dumps({"rc": 0}))       # the provider wrote it all and exited
            settled = threading.Event()

            def on_outcome(runner):
                service._on_outcome(runner)
                settled.set()
            runner = TurnRunner(store=service.store, attempt={"attempt_id": "job/a1", "lane_id": f"{provider}-1"},
                                spec=spec(provider), conversation_id=cid, attempt_dir=adir,
                                control_socket=str(adir / "unused.sock"), on_outcome=on_outcome,
                                on_contain=lambda _: None)
            runner.relay = JournalRelay(adir)
            runner.handshaken = runner.handshake_done_once = True       # a journal relay has no status to ask
            runner.start()
            assert settled.wait(timeout), f"{case['name']}: not settled"
            runner.stop()
            assert runner.join(30), f"{case['name']}: runner still running"
            message = service.store.message(MID)
            turn = {k: v for k, v in (read_turn(adir) or {}).items() if k not in VOLATILE and k != "steers"}
            events = service.store.query("SELECT source, position, ordinal, kind, data_json FROM events "
                                         "WHERE conversation_id=? ORDER BY seq", (cid,))
            return {
                "host": {"state": message["state"], "state_reason": message["state_reason"],
                         "served": {k: v for k, v in (message.get("served") or {}).items() if k not in VOLATILE}},
                "blocked_by": service.store.conversation(cid)["blocked_by"],
                "changes": [[r["state"], r["state_reason"]] for r in service.store.query(
                    "SELECT state, state_reason FROM changes WHERE message_id=? ORDER BY seq", (MID,))],
                "frames": [[r["tag"], r["op"], r.get("line")] for r in read_log(adir / "stdin.jsonl")],
                "events": [[e["kind"], json.loads(e["data_json"]), e["source"], e["position"], e["ordinal"]]
                           for e in events],
                "turn": turn,
            }
        finally:
            service.close()
            daemon.store.close()


def record() -> dict:
    return {"driver": {case["name"]: drive(case) for case in cases()},
            "runner": {case["name"]: run(case) for case in runner_cases()}}


def dump(recording: dict, stream) -> None:
    """One history per line, so a new recording diffs history by history."""
    stream.write("{\n")
    for index, level in enumerate(("driver", "runner")):
        stream.write(f" {json.dumps(level)}: {{\n")
        rows = sorted(recording[level].items())
        for number, (name, value) in enumerate(rows):
            comma = "," if number < len(rows) - 1 else ""
            stream.write(f"  {json.dumps(name)}: {json.dumps(value, sort_keys=True, separators=(',', ':'))}{comma}\n")
        stream.write(" }" + ("," if index == 0 else "") + "\n")
    stream.write("}\n")


if __name__ == "__main__":
    import subfleet
    print(f"recording with {subfleet.__file__}", file=sys.stderr)
    dump(record(), sys.stdout)
