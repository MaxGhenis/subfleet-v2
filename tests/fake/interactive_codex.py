"""A fake `codex app-server` for the guard preflight and conversation turns (C-14.2, C-26, C-27 tests).

`tests/bin/codex app-server …` hands over to `main` here. It answers the
guard preflight's `hooks/list` exactly as the previous fixture did, and speaks
enough of the stable 0.153.3 JSON-RPC surface for a turn: `initialize`,
`model/list`, `thread/start`, `thread/resume`, `turn/start`,
`turn/interrupt`, one command approval, and the notifications the Codex turn
driver reads. Message shapes follow the schema kept under
`tests/fixtures/codex/app-server-0.153.3/`. Threads persist as rollouts under
`$CODEX_HOME/sessions`, with `turn_context` records carrying the turn id and
model (attestation) and the client message id on the user item (reconcile).
It exits 0 on stdin EOF and never reaches the network.

Each turn picks its behaviour with a `[fake:<scenario>]` directive in its text:

    (none)            streamed text, then `completed`
    approval          asks to run a command; accept runs it, decline says so, cancel interrupts
    slow              streams until `turn/interrupt`, then `interrupted`
    limit             a full rate-limit window and a `usageLimitExceeded` failure
    exit-after-ack    `turn/started`, then exits
    wrong-model       the thread serves another model than asked for

Like a live 0.153.3 server (docs/desktop/reviews/2026-09-24-live-probes.md), it
reports the thread `active` when a turn starts and `idle` when it ends, and
sends `turn/completed` only for a turn that used no tool: after a command ran
or was declined, `idle` is the last word.

Environment: `SUBFLEET_FAKE_TURN_LOG` appends argv and every stdin line.
`SUBFLEET_FAKE_THREAD_ACTIVE=1` answers `thread/resume` with an active thread.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path

DIRECTIVE = re.compile(r"\[fake:([a-z-]+)\]")
EFFORTS = [{"reasoningEffort": e, "description": e} for e in ("low", "medium", "high", "xhigh")]
MODELS = [
    {"id": "gpt-6-astra", "model": "gpt-6-astra", "displayName": "GPT-6 Astra", "description": "fake",
     "supportedReasoningEfforts": EFFORTS, "defaultReasoningEffort": "medium",
     "serviceTiers": [{"id": "priority", "name": "Fast", "description": "fake"}], "inputModalities": ["text", "image"]},
    {"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "displayName": "GPT-5.6 Terra", "description": "fake",
     "supportedReasoningEfforts": EFFORTS[:3], "defaultReasoningEffort": "medium", "serviceTiers": [],
     "inputModalities": ["text"]},
]


def now_ms() -> int:
    return int(time.time() * 1000)


class Server:
    def __init__(self, argv: list[str]):
        self.argv = argv
        override = next(arg for arg in argv if arg.startswith("hooks="))
        hooks = tomllib.loads(override)["hooks"]
        self.hook_key, self.hook_state = next(iter(hooks["state"].items()))
        self.home = Path(os.environ.get("CODEX_HOME") or ".")
        self.inbox: "queue.Queue[dict | None]" = queue.Queue()
        self.interrupts: "queue.Queue[dict]" = queue.Queue()
        self.lock = threading.Lock()
        self.thread: dict | None = None
        self.rollout: Path | None = None
        self.tool_used = False
        self.log({"argv": argv, "pid": os.getpid()})

    def log(self, row: dict) -> None:
        path = os.environ.get("SUBFLEET_FAKE_TURN_LOG")
        if path:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")

    def send(self, row: dict) -> None:
        with self.lock:
            sys.stdout.write(json.dumps(row, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    def reader(self) -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            self.log({"stdin": raw, "pid": os.getpid()})
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("method") == "turn/interrupt":
                self.send({"id": row["id"], "result": {}})
                self.interrupts.put(row)
                continue
            self.inbox.put(row)
        self.inbox.put(None)

    # --- rollouts --------------------------------------------------------------

    def record(self, kind: str, payload: dict) -> None:
        if self.rollout is not None:
            with open(self.rollout, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                         "type": kind, "payload": payload}) + "\n")

    def find_rollout(self, thread_id: str) -> Path | None:
        sessions = self.home / "sessions"
        found = list(sessions.rglob(f"rollout-*{thread_id}.jsonl")) if sessions.is_dir() else []
        return found[0] if len(found) == 1 else None

    # --- requests --------------------------------------------------------------

    def run(self) -> int:
        threading.Thread(target=self.reader, daemon=True).start()
        while True:
            row = self.inbox.get()
            if row is None:
                return 0
            if "method" not in row or "id" not in row:
                continue
            method, rid, params = row["method"], row["id"], row.get("params") or {}
            handler = getattr(self, "on_" + method.replace("/", "_"), None)
            if handler is None:
                self.send({"id": rid, "error": {"code": -32601, "message": f"fake: no {method}"}})
                continue
            code = handler(rid, params)
            if code is not None:
                return code

    def on_initialize(self, rid, params):
        self.send({"id": rid, "result": {"userAgent": "codex-fake/0.153.3", "codexHome": str(self.home)}})

    def on_hooks_list(self, rid, params):
        self.send({"id": rid, "result": {"data": [{
            "cwd": params["cwds"][0], "errors": [], "hooks": [{
                "key": self.hook_key, "enabled": self.hook_state["enabled"],
                "trustStatus": "trusted", "currentHash": self.hook_state["trusted_hash"],
            }],
        }]}})

    def on_model_list(self, rid, params):
        self.send({"id": rid, "result": {"data": MODELS, "nextCursor": None}})

    def _thread_result(self, thread_id: str, params: dict, active: bool = False) -> dict:
        model = params.get("model") or "gpt-6-astra"
        self.thread = {"id": thread_id, "model": model, "cwd": params.get("cwd"),
                       "serviceTier": params.get("serviceTier")}
        return {"thread": {"id": thread_id, "status": {"type": "active" if active else "idle"}, "cwd": params.get("cwd")},
                "model": model, "reasoningEffort": params.get("effort") or "medium",
                "serviceTier": params.get("serviceTier"), "sandbox": {"type": "readOnly"},
                "approvalPolicy": params.get("approvalPolicy"), "cwd": params.get("cwd")}

    def on_thread_start(self, rid, params):
        thread_id = str(uuid.uuid4())
        day = time.strftime("%Y/%m/%d", time.gmtime())
        directory = self.home / "sessions" / day
        directory.mkdir(parents=True, exist_ok=True)
        self.rollout = directory / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime())}-{thread_id}.jsonl"
        self.record("session_meta", {"id": thread_id, "cwd": params.get("cwd"), "originator": "subfleet",
                                     "cli_version": "0.153.3"})
        self.send({"id": rid, "result": self._thread_result(thread_id, params)})

    def on_thread_resume(self, rid, params):
        thread_id = params.get("threadId")
        self.rollout = self.find_rollout(str(thread_id))
        if self.rollout is None:
            self.send({"id": rid, "error": {"code": -32600, "message": f"no rollout found for thread id {thread_id}"}})
            return
        active = os.environ.get("SUBFLEET_FAKE_THREAD_ACTIVE") == "1"
        self.send({"id": rid, "result": self._thread_result(str(thread_id), params, active=active)})

    def on_turn_start(self, rid, params):
        if self.thread is None or params.get("threadId") != self.thread["id"]:
            self.send({"id": rid, "error": {"code": -32600, "message": "fake: no such thread loaded"}})
            return
        text = " ".join(str(i.get("text") or "") for i in params.get("input") or [] if i.get("type") == "text")
        images = [i for i in params.get("input") or [] if i.get("type") in ("image", "localImage")]
        match = DIRECTIVE.search(text)
        scenario = match.group(1) if match else "reply"
        turn_id = f"turn-{uuid.uuid4().hex[:10]}"
        thread_id = self.thread["id"]
        served = "gpt-5.6-terra" if scenario == "wrong-model" else (params.get("model") or self.thread["model"])
        self.record("turn_context", {"turn_id": turn_id, "model": served, "cwd": params.get("cwd"),
                                     "approval_policy": params.get("approvalPolicy"),
                                     "sandbox_policy": params.get("sandboxPolicy")})
        self.record("response_item", {"type": "message", "role": "user", "client_id": params.get("clientUserMessageId"),
                                      "content": [{"type": "input_text", "text": text}]})
        turn = {"id": turn_id, "items": [], "status": "inProgress"}
        self.tool_used = False
        self.send({"id": rid, "result": {"turn": turn}})
        self.notify("thread/status/changed", {"threadId": thread_id, "status": {"type": "active", "activeFlags": []}})
        self.notify("turn/started", {"threadId": thread_id, "turn": turn})
        self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "completedAtMs": now_ms(),
                                       "item": {"type": "userMessage", "id": f"u-{turn_id}", "content": []}})
        if scenario == "exit-after-ack":
            return 1
        handler = getattr(self, "scenario_" + scenario.replace("-", "_"), None)
        if handler is None:
            return self.finish(thread_id, turn_id, "failed", error={"message": f"fake: no scenario {scenario}",
                                                                     "codexErrorInfo": "other"})
        reply = f"Fake Codex read {len(text)} characters" + (f" and {len(images)} image(s)" if images else "") + "."
        return handler(thread_id, turn_id, reply)

    # --- turn pieces -----------------------------------------------------------

    def say(self, thread_id: str, turn_id: str, text: str) -> None:
        item_id = f"msg-{uuid.uuid4().hex[:8]}"
        self.notify("item/started", {"threadId": thread_id, "turnId": turn_id, "startedAtMs": now_ms(),
                                     "item": {"type": "agentMessage", "id": item_id, "text": ""}})
        for piece in re.findall(r".{1,12}", text, re.S):
            self.notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id,
                                                    "itemId": item_id, "delta": piece})
        self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "completedAtMs": now_ms(),
                                       "item": {"type": "agentMessage", "id": item_id, "text": text}})
        self.record("response_item", {"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": text}]})

    def finish(self, thread_id: str, turn_id: str, status: str, *, error: dict | None = None):
        turn = {"id": turn_id, "items": [], "status": status, "durationMs": 5, "error": error}
        self.notify("thread/status/changed", {"threadId": thread_id, "status": {"type": "idle"}})
        if not self.tool_used:
            self.notify("turn/completed", {"threadId": thread_id, "turn": turn})
        return None

    def interrupted(self) -> bool:
        try:
            self.interrupts.get_nowait()
            return True
        except queue.Empty:
            return False

    # --- scenarios -------------------------------------------------------------

    def scenario_reply(self, thread_id, turn_id, reply):
        self.say(thread_id, turn_id, reply)
        return self.finish(thread_id, turn_id, "completed")

    def scenario_slow(self, thread_id, turn_id, reply):
        item_id = f"msg-{uuid.uuid4().hex[:8]}"
        for n in range(1200):
            if self.interrupted():
                return self.finish(thread_id, turn_id, "interrupted")
            self.notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id,
                                                    "itemId": item_id, "delta": f"tick {n}\n"})
            time.sleep(0.05)
        return self.finish(thread_id, turn_id, "completed")

    def scenario_approval(self, thread_id, turn_id, reply):
        item_id = f"cmd-{uuid.uuid4().hex[:8]}"
        command = "echo approved-by-person"
        self.tool_used = True
        item = {"type": "commandExecution", "id": item_id, "command": command, "commandActions": [],
                "cwd": self.thread["cwd"], "status": "inProgress"}
        self.notify("item/started", {"threadId": thread_id, "turnId": turn_id, "startedAtMs": now_ms(), "item": item})
        request_id = 900
        self.send({"id": request_id, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "startedAtMs": now_ms(),
            "command": command, "cwd": self.thread["cwd"], "reason": "fake: asks every time"}})
        while True:
            if self.interrupted():
                self.notify("serverRequest/resolved", {"threadId": thread_id, "requestId": request_id})
                return self.finish(thread_id, turn_id, "interrupted")
            try:
                row = self.inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if row is None:
                return 0
            if row.get("id") == request_id and "result" in row:
                break
        decision = (row.get("result") or {}).get("decision")
        self.notify("serverRequest/resolved", {"threadId": thread_id, "requestId": request_id})
        if decision in ("accept", "acceptForSession"):
            self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "completedAtMs": now_ms(), "item": {
                **item, "status": "completed", "aggregatedOutput": "approved-by-person\n", "exitCode": 0}})
            self.say(thread_id, turn_id, "The command ran.")
            return self.finish(thread_id, turn_id, "completed")
        if decision == "cancel":
            return self.finish(thread_id, turn_id, "interrupted")
        self.notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "completedAtMs": now_ms(),
                                       "item": {**item, "status": "declined"}})
        self.say(thread_id, turn_id, "Not run.")
        return self.finish(thread_id, turn_id, "completed")

    def scenario_limit(self, thread_id, turn_id, reply):
        resets = int(time.time()) + 2 * 3600
        self.notify("account/rateLimits/updated", {"rateLimits": {
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": resets},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": resets + 86400}}})
        error = {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}
        self.notify("error", {"threadId": thread_id, "turnId": turn_id, "willRetry": False, "error": error})
        return self.finish(thread_id, turn_id, "failed", error=error)

    def scenario_wrong_model(self, thread_id, turn_id, reply):
        return self.scenario_reply(thread_id, turn_id, reply)


def main(argv: list[str]) -> int:
    return Server(argv).run()
