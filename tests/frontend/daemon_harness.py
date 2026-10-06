"""The daemon's own conversation code, without the daemon, for the app's core tests.

`ServiceHarness` builds the real `ConversationService` over a real
`ConversationStore` in a temporary state root; its op handlers, `_view`,
`_receipt` and `_approval_view` produce the JSON the app decodes. Nothing is
dispatched (there is no scheduler), so messages stay `queued` unless a test
moves them; events come from the real provider drivers (`claude_turn`,
`codex_turn`) fed scripted provider output and stored with the store's own
`append_events`, as the turn runner does.

`ServiceServer` answers the protocol on an AF_UNIX socket with the service's
own `respond` (so errors carry the daemon's `"<reason>: <message>"` form), and
can drop or delay one answer to test the app's retries. Person-only checks are
bypassed here (they are the peer check's, tested end to end in
`test_core_live.py`).

Nothing here touches `~/.subfleet`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable
import uuid

from subfleet import protocol
from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.codex_turn import CodexTurn
from subfleet.conversations.peers import Verdict, peer_pid
from subfleet.conversations.service import ConversationService
from subfleet.conversations.turn import APPROVAL_NEEDED, RUNNING, TurnSpec
from subfleet.daemon import busy_answer

REPO = Path(__file__).resolve().parents[2]


class _MainStore:
    """The job store seams the conversation handlers touch: no jobs, no lanes."""

    def one(self, sql: str, params: tuple = ()) -> None:
        return None

    def query(self, sql: str, params: tuple = ()) -> list:
        return []

    def lane_rows(self) -> list:
        return []


class ServiceHarness:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = self.root / "work"
        self.workspace.mkdir(exist_ok=True)
        policy = json.loads((REPO / "subfleet/default_policy.json").read_text())
        self.daemon = SimpleNamespace(root=self.root, policy=policy, store=_MainStore(), _notify=lambda: None,
                                      requests=ThreadPoolExecutor(4), log=logging.getLogger("frontend-harness"))
        self.service = ConversationService(self.daemon)
        # Person-only operations are the peer check's (C-25.6), exercised live elsewhere.
        self.service._person = lambda peer, what: Verdict(True, "frontend harness", peer)
        self.store = self.service.store

    def close(self) -> None:
        self.service.close()
        self.daemon.requests.shutdown(wait=False)

    def call(self, op: str, **args) -> dict:
        return self.service.handle(op, args, os.getpid())

    def response_line(self, op: str, args: dict, request_id: str = "fixture") -> bytes:
        """The exact line the daemon writes for a request, errors included."""
        left, right = socket.socketpair()
        try:
            self.service.respond(left, threading.Lock(), protocol.Request(op=op, args=args, id=request_id), os.getpid())
            with right.makefile("rb") as reader:
                return reader.readline()
        finally:
            left.close()
            right.close()

    # --- fixtures -------------------------------------------------------------

    def settings(self, **extra) -> dict:
        return {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True, **extra}

    def create(self, provider: str = "claude", **extra) -> dict:
        args = {"request_id": str(uuid.uuid4()), "provider": provider, "workspace": str(self.workspace),
                "settings": self.settings(**extra.pop("settings", {})), **extra}
        return self.call("conversation.create", **args)["conversation"]

    def submit(self, cid: str, text: str, *, after: str | None = None, message_id: str | None = None) -> dict:
        return self.call("message.submit", conversation_id=cid, message_id=message_id or str(uuid.uuid4()),
                         after_message_id=after, text=text, attachments=[], settings=self.settings())

    def attempt(self, cid: str, mid: str, *, provider: str = "claude", seq: int = 1) -> "Attempt":
        return Attempt(self, cid, mid, provider=provider, attempt_id=f"job-{mid[:8]}/a{seq}")


class Attempt:
    """One turn attempt, driven by the real provider driver, stored as the runner stores it."""

    def __init__(self, harness: ServiceHarness, cid: str, mid: str, *, provider: str, attempt_id: str):
        self.harness = harness
        self.store = harness.store
        self.cid, self.mid, self.attempt_id = cid, mid, attempt_id
        spec = TurnSpec(provider=provider, message_id=mid, text="fixture", model_id="opus[1m]" if provider == "claude"
                        else "gpt-6-astra", permission="ask" if provider == "claude" else "read-only",
                        native_session_id=None, new_session_id=str(uuid.uuid4()) if provider == "claude" else None,
                        cwd=str(harness.workspace))
        self.driver = ClaudeTurn(spec, read_bytes=lambda path: b"") if provider == "claude" else CodexTurn(spec)
        self.offset = 0
        self.stdin_seq = 0
        self._apply(self.driver.start())

    def feed(self, *rows: dict) -> None:
        for row in rows:
            line = json.dumps(row)
            self._apply(self.driver.feed(line, self.offset))
            self.offset += len(line) + 1

    def respond(self, request_id: str, decision: str, message: str | None = None, answers: dict | None = None) -> None:
        if isinstance(self.driver, ClaudeTurn):
            step = self.driver.respond(request_id, decision, message, answers=answers)
        else:
            step = self.driver.respond(request_id, decision, message)
        self._apply(step)

    def _apply(self, step) -> None:
        """runner.TurnRunner._apply, without the relay."""
        batch = [("command" if e.source.startswith("cmd:") else "stdout", e.source, 0, e.kind, e.data)
                 for e in step.events]
        if batch:
            self.store.append_events(conversation_id=self.cid, message_id=self.mid, attempt_id=self.attempt_id,
                                     events=batch, stdout_offset=self.offset, stdin_seq=self.stdin_seq)
        self.stdin_seq += len(step.frames)
        for event in step.events:
            if event.kind == "accepted":
                self.store.set_state(self.mid, RUNNING, expect=("queued", "waiting", "starting"))
        for approval in step.approvals:
            self.store.add_approval(message_id=self.mid, conversation_id=self.cid, attempt_id=self.attempt_id,
                                    provider_request_id=approval.provider_request_id, kind=approval.kind,
                                    request=approval.request, display=approval.summary, options=approval.options)
            self.store.set_state(self.mid, APPROVAL_NEEDED, expect=("running", "starting"))
        if step.resolved:
            self.store.withdraw_approvals(attempt_id=self.attempt_id, provider_request_ids=list(step.resolved))
            if not self.store.approvals(message_id=self.mid) and self.driver.outcome is None:
                self.store.set_state(self.mid, RUNNING, expect=("approval-needed",))
        if step.outcome is not None:
            state = step.outcome.state
            self.store.set_state(self.mid, state, reason=step.outcome.reason,
                                 expect=("running", "approval-needed", "starting", "waiting", "queued"),
                                 served={"lane_id": "claude-1", "model": step.outcome.served_model})

    def compact(self) -> int:
        return self.store.compact(self.attempt_id)


# --- scripted provider output (the shapes tests/fake/interactive_claude.py emits) ---

LEVELS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_CATALOG = [
    {"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
     "supportedEffortLevels": LEVELS, "supportsFastMode": True},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
     "supportedEffortLevels": LEVELS, "supportsFastMode": True},
]


def claude_init(email: str = "fixture@example.invalid") -> dict:
    return {"type": "control_response", "response": {
        "subtype": "success", "request_id": "subfleet-init", "response": {
            "account": {"email": email}, "models": CLAUDE_CATALOG, "fast_mode_state": "off",
            "fast_mode_disabled_reason": None, "current_permission_mode": "default"}}}


def claude_stream(message_id: str, pieces: list[str], *, kind: str = "text", index: int = 0) -> list[dict]:
    rows = [{"type": "stream_event", "event": {"type": "message_start", "message": {"id": message_id}}}]
    delta_type, field = ("text_delta", "text") if kind == "text" else ("thinking_delta", "thinking")
    for piece in pieces:
        rows.append({"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": delta_type, field: piece}}})
    rows.append({"type": "stream_event", "event": {"type": "content_block_stop", "index": index}})
    return rows


def claude_block(message_id: str, index: int, block: dict, pieces: list[str] = ()) -> list[dict]:
    """One content block as Claude 2.1.280 frames it (observed 2026-09-24): its
    start, deltas, then an `assistant` row holding that block alone while it is
    still open, then its stop."""
    kind = block["type"]
    rows = [{"type": "stream_event", "event": {"type": "content_block_start", "index": index,
                                                "content_block": {"type": kind}}}]
    delta_type, field = {"text": ("text_delta", "text"), "thinking": ("thinking_delta", "thinking"),
                         "tool_use": ("input_json_delta", "partial_json")}[kind]
    for piece in pieces:
        rows.append({"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": delta_type, field: piece}}})
    rows.append(claude_assistant(message_id, [block]))
    rows.append({"type": "stream_event", "event": {"type": "content_block_stop", "index": index}})
    return rows


def claude_assistant(message_id: str, blocks: list[dict], model: str = "claude-opus-5-5") -> dict:
    return {"type": "assistant", "parent_tool_use_id": None,
            "message": {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": blocks}}


def claude_result(ok: bool = True, subtype: str = "success") -> dict:
    return {"type": "result", "subtype": subtype, "is_error": not ok, "num_turns": 1, "result": "",
            "errors": [], "permission_denials": [], "duration_ms": 5}


# --- steer (C-24.9): the daemon's real `message.steer`, into a real runner ------------------
#
# `message.steer` is the service's own op. It steers only into a live host: a message
# running or asking whose `TurnRunner` takes steers. `make_live` builds that runner
# for a host message, its driver fed the provider's answers up to a steerable turn
# and its relay a journal (no guardian socket, no provider process); the op then
# claims, refuses and answers exactly as the daemon does. `go_live_when_submitted`
# does it the moment the app's own `message.submit` of the host lands, for tests
# whose conversation the app creates. `record_state` and `record_steer_event` still
# write what a settlement or the driver would, where a test needs a later state.

STEER_REFUSALS = ("not-queued", "not-next", "no-live-turn", "settings-narrower", "not-steerable")
STEER_CAPS = {"type": "system", "subtype": "init", "capabilities": ["msg_lifecycle_v1", "interrupt_receipt_v1",
                                                                    "interrupt_cancel_queued_v1"]}


class JournalRelay:
    """The guardian's relay log (intent, then written) without a provider pipe."""

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


def make_live(harness: ServiceHarness, cid: str, mid: str):
    """A running host with a real runner that takes steers (C-24.9). Returns the runner."""
    from subfleet.conversations.runner import TurnRunner
    harness.store.set_state(mid, RUNNING, expect=("queued", "waiting", "starting"))
    message = harness.store.message(mid)
    attempt_id = f"job-{mid[:8]}/a1"
    adir = harness.root / "jobs" / attempt_id
    adir.mkdir(parents=True, exist_ok=True)
    spec = TurnSpec(provider="claude", message_id=mid, text="host", model_id=message["settings"]["model"],
                    permission=message["settings"]["permission"], native_session_id=None,
                    new_session_id=str(uuid.uuid4()), cwd=str(harness.workspace))
    runner = TurnRunner(store=harness.store, attempt={"attempt_id": attempt_id, "lane_id": "claude-1"}, spec=spec,
                        conversation_id=cid, attempt_dir=adir, control_socket=str(adir / "unused.sock"),
                        on_outcome=lambda r: None, on_contain=lambda a: None)
    runner.relay = JournalRelay(adir)
    runner.handshaken = runner.handshake_done_once = runner.replay_caught_up = True
    runner._restore_steers()
    runner._apply(runner.driver.start())
    for offset, row in enumerate((claude_init(), STEER_CAPS)):
        runner._apply(runner.driver.feed(json.dumps(row), offset))
    assert runner.steerable
    harness.service.runners[attempt_id] = runner
    return runner


def steer_frames(runner) -> list[str]:
    """The steer frames the runner wrote to its relay, in order (after draining its commands)."""
    from subfleet.relay import read_log
    runner._drain_commands()
    return [r["tag"].removeprefix("steer:") for r in read_log(runner.adir / "stdin.jsonl")
            if str(r.get("tag")).startswith("steer:")]


def go_live_when_submitted(harness: ServiceHarness, *mids: str, then: Callable[[str, str], None] | None = None) -> dict:
    """Make each named message a live host (`make_live`) when the app's submit of it
    lands; `then(cid, mid)` runs after. Returns the runners, filled as it happens."""
    real = harness.service.op_message_submit
    waiting, runners = set(mids), {}

    def submit(args, peer):
        receipt = real(args, peer)
        mid = receipt["message_id"]
        if mid in waiting and receipt["state"] == "queued":
            waiting.discard(mid)
            runners[mid] = make_live(harness, receipt["conversation_id"], mid)
            if then is not None:
                then(receipt["conversation_id"], mid)
        return receipt

    harness.service.op_message_submit = submit
    return runners


def after_submitted(harness: ServiceHarness, mid: str, action: Callable[[dict], None]) -> None:
    """Run `action(receipt)` once the app's submit of `mid` lands (before its steer)."""
    real = harness.service.op_message_submit

    def submit(args, peer):
        receipt = real(args, peer)
        if receipt["message_id"] == mid:
            action(receipt)
        return receipt

    harness.service.op_message_submit = submit


def steer_requests(server: "ServiceServer") -> list[str]:
    return [r["args"].get("message_id") for r in server.requests if r["op"] == "message.steer"]


def record_state(harness: ServiceHarness, mid: str, state: str, reason: str | None = None,
                 served: dict | None = None) -> None:
    """A message's state as the daemon records it (the store's `set_state`: its row and change row)."""
    fields = {"served": served} if served is not None else {}
    harness.store.set_state(mid, state, reason=reason, **fields)


def record_steer_event(turn: "Attempt", kind: str, steered: str, **data) -> None:
    """A steer event on the host's stream (`steer.delivered` where the provider took it)."""
    turn.store.append_events(conversation_id=turn.cid, message_id=turn.mid, attempt_id=turn.attempt_id,
                             events=[("stdout", f"{kind}:{steered}", 0, kind, {"message_id": steered, **data})],
                             stdout_offset=turn.offset, stdin_seq=turn.stdin_seq)


# --- a socket server around the service ---------------------------------------------


class ServiceServer:
    """The protocol on an AF_UNIX socket, answered by the real service.

    `faults[(op, key)]` applies once to the first matching request, where `key`
    is the request's `message_id` or `request_id` (or None for any):
    `"drop"` handles the request and closes without answering (a lost answer),
    `"refuse"` closes without handling it (the daemon never saw it),
    `"busy"` answers as the daemon does past its connection cap, without
    handling it (C-16.1: exit 69, request id ""),
    `"delay:<s>"` handles it and answers after a pause.
    """

    def __init__(self, harness: ServiceHarness):
        self.harness = harness
        self.directory = Path(tempfile.mkdtemp(prefix="sf-app-", dir="/tmp"))
        self.path = self.directory / "daemon.sock"
        self.faults: dict[tuple[str, str | None], str] = {}
        self.requests: list[dict] = []
        self.connections = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(16)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        finally:
            try:
                self.path.unlink()
                self.directory.rmdir()
            except OSError:
                pass

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self._connection, args=(conn,), daemon=True).start()

    def _fault(self, req: protocol.Request) -> str | None:
        key = req.args.get("message_id") or req.args.get("request_id")
        for candidate in ((req.op, key), (req.op, None)):
            if candidate in self.faults:
                return self.faults.pop(candidate)
        return None

    def _connection(self, conn: socket.socket) -> None:
        lock = threading.Lock()
        peer = peer_pid(conn)
        try:
            with conn.makefile("rb") as reader:
                line = reader.readline(1024 * 1024 + 1)
                if not line:
                    return
                try:
                    req = protocol.decode_request(line)
                except protocol.ProtocolError as exc:
                    conn.sendall(protocol.encode(protocol.fail("", 2, str(exc))))
                    return
                self.requests.append({"op": req.op, "id": req.id, "args": req.args, "raw": json.loads(line)})
                fault = self._fault(req)
                if fault == "refuse":
                    return
                if fault == "busy":
                    conn.sendall(busy_answer("the daemon is serving 512 connections"))
                    return
                if fault == "drop":
                    try:
                        self.harness.service.handle(req.op, req.args, peer)
                    except Exception:
                        pass
                    return
                if fault and fault.startswith("delay:"):
                    time.sleep(float(fault.split(":", 1)[1]))
                self.harness.service.respond(conn, lock, req, peer)
        finally:
            conn.close()


def wait_for(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")
