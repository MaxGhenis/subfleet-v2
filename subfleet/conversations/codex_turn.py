"""One Codex conversation turn over `codex app-server` (C-26.5, C-26.6, C-27; design §7, D-11).

The process is `<verified codex> app-server --listen stdio:// -c <verified
hooks override>` with the lane's `CODEX_HOME`. The driver speaks the stable
(non-experimental) JSON-RPC surface of codex-cli 0.153.3, whose schema
`codex app-server generate-json-schema` produces: `initialize`, then
`hooks/list` on this very server for this cwd (the never-rules guard must be
listed, enabled, trusted and at the pinned hash, exactly as the preflight
requires), `model/list` to validate the requested model, effort and Fast tier,
`thread/start` or `thread/resume`, and one `turn/start`. `turn/completed` ends
the turn and stdin is closed; the server exits on EOF (observed 2026-09-24:
exit 0 within 0.02 s).

A live 0.153.3 server sends `turn/completed` only for a turn that used no
tool: after a command ran, or a hook blocked one, the last thing it sends is
`thread/status/changed` to `idle` (observed 2026-09-24, see
docs/desktop/reviews/2026-09-24-live-probes.md). So `idle` after our turn
started marks the end: the driver sets `idle_pending`, and the runner calls
`settle_idle()` when no `turn/completed` has followed within a short grace.
The state then comes from what the stream showed: a stop request, a final
error, or else completion.

Wire notes: JSON-RPC 2.0 without the `"jsonrpc"` member (README at tag
rust-v0.153.3). Requests the driver sends use fixed ids per purpose, so a
replayed driver recognises the responses to the frames it already sent.
"""

from __future__ import annotations

import json
from typing import Any

from ..guard.preflight import HOOK_KEY
from . import redact
from .turn import COMPLETE, FAILED, INTERRUPTED, Approval, Event, Frame, Outcome, Step, TurnSpec

ID_INIT, ID_HOOKS, ID_MODELS, ID_THREAD, ID_TURN, ID_INTERRUPT = 1, 2, 3, 4, 5, 6
FAST_TIER = "priority"
LIMIT_ERRORS = ("usageLimitExceeded", "rateLimitExceeded")

# The thread is always opened read-only: a writable `thread/start` with a cwd
# writes `[projects."<cwd>"] trust_level = "trusted"` into the lane's
# config.toml (verified 2026-09-24), and a read-only thread with a writable
# turn policy does not. The writable policy rides on each turn (C-26.11).
THREAD_SANDBOX = "read-only"
POLICY = {
    # permission -> (approvalPolicy, turn sandboxPolicy)
    "ask": ("on-request", {"type": "workspaceWrite", "networkAccess": False}),
    "accept-edits": ("on-request", {"type": "workspaceWrite", "networkAccess": False}),
    "bypass": ("never", {"type": "workspaceWrite", "networkAccess": False}),
    "read-only": ("never", {"type": "readOnly", "networkAccess": False}),
}
WRITABLE = ("ask", "accept-edits", "bypass")
#: The status phase an item of each type starts. Any other item but the
#: person's own message is work the model is doing, `tool` (0.153.3 also has
#: collabAgentToolCall, sleep, imageGeneration, subAgentActivity, ...).
ITEM_PHASES = {"reasoning": "thinking", "agentMessage": "writing", "contextCompaction": "compacting",
               "userMessage": None}


def argv(executable: str, override: str, *, unified_exec_off: bool = False) -> list[str]:
    """The turn server's command. `override` is the verified `-c hooks=…` value
    (`PreflightResult.override`); a turn is never launched without it, and it
    carries C-23.6's switch whenever an exec launch would."""
    if not override or not override.startswith("hooks="):
        raise ValueError("a Codex turn requires the verified hooks override")
    command = [executable, "app-server", "--listen", "stdio://", "-c", override]
    if unified_exec_off:
        command += ["-c", "features.unified_exec=false"]
    return command


def check_hooks(result: Any, cwd: str, hooks_hash: str | None) -> str | None:
    """The preflight's checks (`guard/preflight.py:632-648`) applied to this
    server's own `hooks/list` answer. Returns a refusal reason, or None."""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return "hooks/list did not return exactly one workdir result"
    entry = data[0]
    if entry.get("cwd") != cwd:
        return "hooks/list answered for a different workdir"
    guards = [h for h in entry.get("hooks") or [] if isinstance(h, dict) and h.get("key") == HOOK_KEY]
    if len(guards) != 1:
        return "hooks/list did not list exactly one never-rules guard"
    guard = guards[0]
    if guard.get("enabled") is not True or guard.get("trustStatus") != "trusted" or (
            hooks_hash and guard.get("currentHash") != hooks_hash):
        return "the never-rules guard is disabled, untrusted, or at another hash"
    if entry.get("errors"):
        return "hooks/list reported errors"
    return None


def observed_catalog(models: Any) -> list[dict]:
    """`model/list` as `models.json` keeps it (design D-19)."""
    out = []
    for m in models if isinstance(models, list) else ():
        if not isinstance(m, dict) or not (m.get("id") or m.get("model")):
            continue
        model = str(m.get("model") or m.get("id"))
        out.append({"value": model, "model": model, "display": m.get("displayName"),
                    "efforts": [str(e.get("reasoningEffort")) for e in m.get("supportedReasoningEfforts") or []
                                if isinstance(e, dict) and e.get("reasoningEffort")],
                    "fast": any(isinstance(t, dict) and t.get("id") == FAST_TIER for t in m.get("serviceTiers") or []),
                    "image_input": ("image" in m["inputModalities"]) if isinstance(m.get("inputModalities"), list)
                    else None})
    return out


def _line(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _request(request_id: int, method: str, params: dict) -> str:
    return _line({"id": request_id, "method": method, "params": params})


class CodexTurn:
    def __init__(self, spec: TurnSpec):
        if spec.permission not in POLICY:
            raise ValueError(f"unknown permission {spec.permission!r}")
        self.spec = spec
        self.phase = "new"
        self.thread_id: str | None = spec.native_session_id
        self.turn_id: str | None = None
        self.accepted = False
        self.answered = False
        self.limited = False
        self.interrupt_requested = False
        self.served_model: str | None = None
        self.outcome: Outcome | None = None
        self.pending: dict[str, tuple[str, dict]] = {}     # request id -> (method, params)
        self.catalog: list[dict] | None = None              # model/list, for models.json
        self.idle_pending = False                           # the thread went idle after our turn started
        self.final_error: dict | None = None                # an `error` notification that will not be retried
        self._ready = {"hooks": False, "models": False}
        self._tools: dict[str, bool] = {}
        self._phase: str | None = None                    # the last item phase announced
        self._buffers: dict[str, redact.DeltaBuffer] = {}

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> Step:
        self.phase = "initializing"
        init = _request(ID_INIT, "initialize", {"clientInfo": {"name": "subfleet", "title": "Subfleet", "version": "2"}})
        return Step(frames=[Frame("init", "write", init)],
                    events=[Event("status", {"phase": "starting-provider"}, "cmd:start")])

    def interrupt(self) -> Step:
        if self.outcome is not None or self.interrupt_requested:
            return Step()
        self.interrupt_requested = True
        if self.phase in ("new", "initializing", "checking", "thread"):
            return self._end(INTERRUPTED, "stopped-before-send", source="cmd:interrupt")
        if self.turn_id:
            return self._send_interrupt("cmd:interrupt")
        return Step(events=[Event("status", {"phase": "stopping"}, "cmd:interrupt")])  # sent when the id arrives

    def _send_interrupt(self, source: str) -> Step:
        frame = Frame("interrupt", "write", _request(ID_INTERRUPT, "turn/interrupt",
                                                     {"threadId": self.thread_id, "turnId": self.turn_id}))
        return Step(frames=[frame], events=[Event("status", {"phase": "stopping"}, source)])

    def respond(self, request_id: str, decision: str, message: str | None = None) -> Step:
        found = self.pending.pop(request_id, None)
        if found is None:
            return Step()
        method, params = found
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            mapping = {"allow": "accept", "allow-session": "acceptForSession", "deny": "decline", "cancel-turn": "cancel"}
            if decision not in mapping:
                raise ValueError(f"decision {decision!r} is not offered for {method}")
            result: dict[str, Any] = {"decision": mapping[decision]}
            if decision == "cancel-turn":
                self.interrupt_requested = True
        elif method == "item/permissions/requestApproval":
            if decision == "allow-turn":
                result = {"permissions": params.get("permissions") or {}, "scope": "turn"}
            elif decision == "deny":
                result = {"permissions": {}}
            else:
                raise ValueError(f"decision {decision!r} is not offered for {method}")
        else:
            raise ValueError(f"no response is defined for {method}")
        frame = Frame(f"approval:{request_id}", "write", _line({"id": _rpc_id(request_id), "result": result}))
        return Step(frames=[frame], resolved=[request_id],
                    events=[Event("approval.resolved", {"request_id": request_id, "decision": decision},
                                  f"cmd:approval:{request_id}")])

    def eof(self, offset: int) -> Step:
        if self.outcome is not None:
            return Step()
        step = self._flush(f"{offset}:eof")
        reason = "stopped" if self.interrupt_requested else "ended-without-result"
        self.outcome = Outcome(INTERRUPTED if self.interrupt_requested else FAILED, reason,
                               accepted=self.accepted, answered=self.answered, limited=self.limited,
                               served_model=self.served_model)
        self.phase = "ended"
        step.outcome = self.outcome
        return step

    # --- input -----------------------------------------------------------------

    def feed(self, raw: bytes | str, offset: int) -> Step:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            msg = json.loads(text)
        except ValueError:
            return Step()
        if not isinstance(msg, dict):
            return Step()
        source = _Sources(offset)
        if self.phase == "ended":
            if "method" in msg and "id" not in msg and msg["method"].startswith("item/"):
                return Step(events=self._notification(msg["method"], msg.get("params") or {}, source).events)
            return Step()
        if "method" in msg and "id" in msg:
            return self._server_request(msg, source)
        if "method" in msg:
            return self._notification(msg["method"], msg.get("params") or {}, source)
        if "id" in msg:
            return self._response(msg, source)
        return Step()

    # --- responses to our requests --------------------------------------------

    def _response(self, msg: dict, source: "_Sources") -> Step:
        rid, error, result = msg.get("id"), msg.get("error"), msg.get("result")
        if rid == ID_INIT and self.phase == "initializing":
            if error:
                return self._end(FAILED, "provider-init-failed", detail=str(error)[:300], source=source.next())
            self.phase = "checking"
            return Step(frames=[
                Frame("initialized", "write", _line({"method": "initialized"})),
                Frame("hooks", "write", _request(ID_HOOKS, "hooks/list", {"cwds": [self.spec.cwd]})),
                Frame("models", "write", _request(ID_MODELS, "model/list", {})),
            ])
        if rid == ID_HOOKS and self.phase == "checking":
            refusal = str(error)[:300] if error else check_hooks(result, self.spec.cwd, self.spec.guard_hash)
            if refusal:
                # D-11: the guard is not proven on this server for this cwd; nothing is sent.
                return self._end(FAILED, "guard-refused", detail=refusal, source=source.next())
            self._ready["hooks"] = True
            return self._maybe_thread(source)
        if rid == ID_MODELS and self.phase == "checking":
            refusal = str(error)[:300] if error else self._check_models(result)
            if refusal:
                return self._end(FAILED, "settings-unsupported", detail=refusal, source=source.next())
            self._ready["models"] = True
            return self._maybe_thread(source)
        if rid == ID_THREAD and self.phase == "thread":
            return self._thread(result, error, source)
        if rid == ID_TURN and self.phase == "turn":
            if error:
                limited = _limit_error(error)
                self.limited = self.limited or limited
                return self._end(FAILED, "limited" if limited else "turn-start-failed",
                                 detail=str(error.get("message") if isinstance(error, dict) else error)[:300],
                                 source=source.next())
            turn = (result or {}).get("turn") or {}
            step = Step()
            if turn.get("id"):
                step.extend(self._accept(str(turn["id"]), source))
            self.phase = "running"
            return step
        return Step()

    def _check_models(self, result: Any) -> str | None:
        models = (result or {}).get("data") if isinstance(result, dict) else None
        if not isinstance(models, list):
            return "model/list returned no catalog"
        self.catalog = observed_catalog(models)
        entry = next((m for m in models if isinstance(m, dict) and self.spec.model_id in (m.get("id"), m.get("model"))), None)
        if entry is None:
            return f"{self.spec.model_id} is not in this account's model catalog"
        efforts = [e.get("reasoningEffort") for e in entry.get("supportedReasoningEfforts") or [] if isinstance(e, dict)]
        if self.spec.effort and self.spec.effort not in efforts:
            return f"{self.spec.model_id} offers effort {', '.join(efforts) or 'none'}, not {self.spec.effort}"
        tiers = [t.get("id") for t in entry.get("serviceTiers") or [] if isinstance(t, dict)]
        if self.spec.fast and FAST_TIER not in tiers:
            return f"{self.spec.model_id} offers no Fast tier on this account"
        modalities = entry.get("inputModalities")
        if self.spec.images and isinstance(modalities, list) and "image" not in modalities:
            return f"{self.spec.model_id} does not take images"
        return None

    def _maybe_thread(self, source: "_Sources") -> Step:
        if not all(self._ready.values()):
            return Step()
        self.phase = "thread"
        approval, _ = POLICY[self.spec.permission]
        common = {"cwd": self.spec.cwd, "model": self.spec.model_id, "sandbox": THREAD_SANDBOX,
                  "approvalPolicy": approval, "approvalsReviewer": "user",
                  "serviceTier": FAST_TIER if self.spec.fast else None}
        if self.spec.native_session_id:
            frame = Frame("thread", "write", _request(ID_THREAD, "thread/resume",
                                                      {"threadId": self.spec.native_session_id, "excludeTurns": True, **common}))
        else:
            frame = Frame("thread", "write", _request(ID_THREAD, "thread/start", common))
        return Step(frames=[frame], events=[Event("status", {"phase": "opening-thread"}, source.next())])

    def _thread(self, result: Any, error: Any, source: "_Sources") -> Step:
        if error:
            return self._end(FAILED, "thread-failed", detail=str(error)[:300], source=source.next())
        thread = (result or {}).get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if self.spec.native_session_id and thread_id != self.spec.native_session_id:
            return self._end(FAILED, "thread-mismatch", detail=f"resumed {thread_id or 'nothing'}", source=source.next())
        if (thread.get("status") or {}).get("type") == "active":
            # C-26.3: someone else is running a turn on this thread.
            return self._end(FAILED, "external-writer", detail="the thread already has an active turn",
                             source=source.next())
        self.thread_id = thread_id
        model = (result or {}).get("model")
        self.served_model = model if isinstance(model, str) else None
        if self.served_model and self.served_model != self.spec.model_id:
            return self._end(FAILED, "model-mismatch", detail=f"asked for {self.spec.model_id}, served {model}",
                             source=source.next())
        served = {"model": self.served_model, "effort": (result or {}).get("reasoningEffort"),
                  "service_tier": (result or {}).get("serviceTier"), "sandbox": (result or {}).get("sandbox"),
                  "approval_policy": (result or {}).get("approvalPolicy"), "native_session_id": thread_id}
        self.phase = "turn"
        approval, sandbox_policy = POLICY[self.spec.permission]
        params: dict[str, Any] = {
            "threadId": thread_id, "clientUserMessageId": self.spec.message_id,
            "input": self._input(), "model": self.spec.model_id, "cwd": self.spec.cwd,
            "approvalPolicy": approval, "approvalsReviewer": "user", "sandboxPolicy": sandbox_policy,
            "serviceTier": FAST_TIER if self.spec.fast else None,
        }
        if self.spec.effort:
            params["effort"] = self.spec.effort
        return Step(frames=[Frame("user-message", "write", _request(ID_TURN, "turn/start", params))],
                    events=[Event("served", served, source.next()), Event("status", {"phase": "sent"}, source.next())])

    def _input(self) -> list[dict]:
        items: list[dict] = []
        if self.spec.text:
            items.append({"type": "text", "text": self.spec.text})
        for image in self.spec.images:
            items.append({"type": "localImage", "path": image.path})
        return items

    def _accept(self, turn_id: str, source: "_Sources") -> Step:
        step = Step()
        if not self.turn_id:
            self.turn_id = turn_id
        if not self.accepted:
            self.accepted = True
            step.events.append(Event("accepted", {"message_id": self.spec.message_id, "turn_id": turn_id}, source.next()))
            if self.interrupt_requested and self.outcome is None:
                step.extend(self._send_interrupt(source.next()))
        return step

    # --- notifications ---------------------------------------------------------

    def _notification(self, method: str, params: dict, source: "_Sources") -> Step:
        if params.get("threadId") not in (None, self.thread_id):
            return Step()
        if method == "turn/started":
            turn = params.get("turn") or {}
            return self._accept(str(turn.get("id")), source) if turn.get("id") else Step()
        if method == "item/agentMessage/delta":
            return self._delta("text.delta", str(params.get("itemId")), str(params.get("delta") or ""), source)
        if method == "item/reasoning/summaryTextDelta":
            block = f"{params.get('itemId')}:{params.get('summaryIndex', 0)}"
            return self._delta("thinking.delta", block, str(params.get("delta") or ""), source)
        if method == "item/started":
            return self._item_started(params.get("item") or {}, source)
        if method == "item/completed":
            return self._item_completed(params.get("item") or {}, source)
        if method == "thread/status/changed":
            return self._thread_status(params.get("status") or {}, source)
        if method == "hook/completed":
            return self._hook(params.get("run") or {}, source)
        if method == "turn/diff/updated":
            diff = redact.truncate(redact.scrub(str(params.get("diff") or "")), 20_000)
            return Step(events=[Event("diff", {"diff": diff}, source.next())])
        if method == "error":
            error = params.get("error") or {}
            if not params.get("willRetry"):
                self.final_error = error if isinstance(error, dict) else {"message": str(error)}
                if _limit_error(error):
                    self.limited = True
            return Step(events=[Event("error", {"message": redact.bounded_text(str(error.get("message") or ""))[:2000],
                                                "kind": _error_kind(error), "will_retry": bool(params.get("willRetry"))},
                                      source.next())])
        if method == "account/rateLimits/updated":
            return Step(events=[Event("limits", {"rate_limits": params.get("rateLimits") or params}, source.next())])
        if method == "serverRequest/resolved":
            rid = str(params.get("requestId"))
            if self.pending.pop(rid, None) is not None:
                return Step(resolved=[rid], events=[Event("approval.resolved", {"request_id": rid, "decision": "withdrawn"},
                                                          source.next())])
            return Step()
        if method == "turn/completed":
            return self._completed(params.get("turn") or {}, source)
        return Step()

    def _thread_status(self, status: dict, source: "_Sources") -> Step:
        kind = status.get("type")
        if kind == "active":
            self.idle_pending = False
        elif kind == "idle" and self.accepted and self.outcome is None:
            self.idle_pending = True
        elif kind == "systemError" and self.outcome is None:
            return self._end(FAILED, "system-error", detail="the Codex thread reported a system error",
                             source=source.next())
        return Step()

    def _hook(self, run: dict, source: "_Sources") -> Step:
        """A hook the server ran: shown when it blocked, failed, or said something."""
        entries = [str(e.get("text") or "") for e in run.get("entries") or [] if isinstance(e, dict)]
        status = run.get("status")
        if status == "completed" and not any(entries):
            return Step()
        return Step(events=[Event("hook", {"event": run.get("eventName"), "status": status,
                                           "name": run.get("statusMessage"),
                                           "feedback": redact.bounded_text("\n".join(e for e in entries if e))[:2000]},
                                  source.next())])

    def settle_idle(self) -> Step:
        """End the turn from `thread/status/changed: idle` when no `turn/completed`
        followed (see the module docstring). Called by the runner after its grace."""
        if self.outcome is not None or not self.idle_pending:
            return Step()
        step = self._flush("cmd:idle")
        if self.interrupt_requested:
            state, reason = INTERRUPTED, "stopped"
        elif self.final_error is not None:
            state = FAILED
            reason = "limited" if self.limited else (_error_kind(self.final_error) or "failed")
        else:
            state, reason = COMPLETE, None
        detail = redact.bounded_text(str(self.final_error.get("message") or ""))[:1000] if self.final_error else None
        return step.extend(self._end(state, reason, detail=detail, source="cmd:idle",
                                     extra={"ended_by": "thread-idle"}))

    def _delta(self, kind: str, block: str, text: str, source: "_Sources") -> Step:
        self.answered = True
        ready = self._buffers.setdefault(f"{kind}|{block}", redact.DeltaBuffer()).feed(text)
        return Step(events=[Event(kind, {"block": block, "text": ready}, source.next())]) if ready else Step()

    def _flush(self, source: str) -> Step:
        step = Step()
        for key in sorted(self._buffers):
            kind, _, block = key.partition("|")
            ready = self._buffers.pop(key).flush()
            if ready:
                step.events.append(Event(kind, {"block": block, "text": ready}, f"{source}:{kind}:{block}"))
        return step

    def _item_started(self, item: dict, source: "_Sources") -> Step:
        itype, item_id = item.get("type"), str(item.get("id"))
        name, value = _tool_view(item)
        # Reasoning shows nothing until a summary part streams, if one ever does:
        # the status strip says where the model is (design §12).
        step = self._announce(ITEM_PHASES.get(str(itype), "tool"), source)
        if name is None:
            return step
        self.answered = True
        started = redact.tool_started(name, value, tool_id=item_id)
        self._tools[item_id] = started["hidden"]
        step.events.append(Event("tool.started", started, source.next()))
        return step

    def _announce(self, phase: str | None, source: "_Sources") -> Step:
        # After a stop, `stopping` stays the word until the turn ends.
        if phase is None or phase == self._phase or self.outcome is not None or self.interrupt_requested:
            return Step()
        self._phase = phase
        return Step(events=[Event("status", {"phase": phase}, source.next())])

    def _item_completed(self, item: dict, source: "_Sources") -> Step:
        itype, item_id = item.get("type"), str(item.get("id"))
        if itype == "userMessage":
            return Step()
        if itype == "agentMessage":
            self.answered = True
            self._buffers.pop(f"text.delta|{item_id}", None)
            return Step(events=[Event("text", {"block": item_id, "text": redact.bounded_text(str(item.get("text") or ""))},
                                      source.next())])
        if itype == "reasoning":
            summary = "\n".join(str(s) for s in item.get("summary") or [] if isinstance(s, str))
            for key in [k for k in self._buffers if k.startswith(f"thinking.delta|{item_id}:")]:
                self._buffers.pop(key)
            return Step(events=[Event("thinking", {"block": item_id, "text": redact.bounded_text(summary)}, source.next())]
                        ) if summary else Step()
        name, _ = _tool_view(item)
        if name is None:
            return Step()
        output, is_error = _tool_output(item)
        return Step(events=[Event("tool.completed", redact.tool_completed(item_id, output, is_error=is_error,
                                                                          hidden=self._tools.get(item_id, False)),
                                  source.next())])

    def _completed(self, turn: dict, source: "_Sources") -> Step:
        step = self._flush(source.next())
        if turn.get("id"):
            step.extend(self._accept(str(turn["id"]), source))
        status = turn.get("status")
        error = turn.get("error") or {}
        if status == "completed":
            state, reason = COMPLETE, None
        elif status == "interrupted":
            state, reason = INTERRUPTED, "stopped" if self.interrupt_requested else "interrupted-by-provider"
        else:
            limited = _limit_error(error) or self.limited
            self.limited = limited
            state, reason = FAILED, "limited" if limited else (_error_kind(error) or "failed")
        detail = redact.bounded_text(str(error.get("message") or ""))[:1000] if error else None
        return step.extend(self._end(state, reason, detail=detail, source=source.next(),
                                     extra={"duration_ms": turn.get("durationMs")}))

    # --- server requests -------------------------------------------------------

    def _server_request(self, msg: dict, source: "_Sources") -> Step:
        rid, method, params = str(msg.get("id")), str(msg.get("method")), msg.get("params") or {}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                      "item/permissions/requestApproval"):
            if params.get("threadId") != self.thread_id or rid in self.pending:
                return Step()
            self.pending[rid] = (method, params)
            if method == "item/commandExecution/requestApproval":
                kind, options = "command", ("allow", "allow-session", "deny", "cancel-turn")
                summary = {"command": redact.truncate(redact.scrub(str(params.get("command") or "")), redact.INPUT_MAX),
                           "cwd": params.get("cwd"), "reason": params.get("reason"),
                           # Every field that changes what is granted is shown (C-27.1).
                           "input_kind": params.get("kind"),
                           "network": params.get("networkApprovalContext"),
                           "execpolicy_amendment": params.get("proposedExecpolicyAmendment"),
                           "network_amendments": params.get("proposedNetworkPolicyAmendments")}
            elif method == "item/fileChange/requestApproval":
                kind, options = "file-change", ("allow", "allow-session", "deny", "cancel-turn")
                summary = {"reason": params.get("reason"), "grant_root": params.get("grantRoot")}
            else:
                kind, options = "permissions", ("allow-turn", "deny")
                summary = {"permissions": params.get("permissions"), "reason": params.get("reason"), "cwd": params.get("cwd")}
            return Step(approvals=[Approval(rid, kind, summary, options, request={"method": method, "params": params})],
                        events=[Event("approval.requested", {"request_id": rid, "kind": kind, **summary,
                                                             "options": list(options)}, source.next())])
        # C-27.4: refused, visibly; credentials are never supplied.
        if method == "item/tool/requestUserInput":
            reply = {"id": msg["id"], "result": {"answers": {}}}
        elif method == "mcpServer/elicitation/request":
            reply = {"id": msg["id"], "result": {"action": "cancel", "content": None}}
        elif method in ("applyPatchApproval", "execCommandApproval"):
            reply = {"id": msg["id"], "result": {"decision": "abort"}}
        else:
            reply = {"id": msg["id"], "error": {"code": -32601, "message": "not supported by Subfleet"}}
        return Step(frames=[Frame(f"refuse:{rid}", "write", _line(reply))],
                    events=[Event("error", {"message": f"refused unsupported provider request {method}"}, source.next())])

    # --- end -------------------------------------------------------------------

    def _end(self, state: str, reason: str | None, *, source: str, detail: str | None = None,
             extra: dict | None = None) -> Step:
        if self.outcome is not None:
            return Step()
        self.outcome = Outcome(state, reason, detail, accepted=self.accepted, answered=self.answered,
                               limited=self.limited, served_model=self.served_model)
        self.phase = "ended"
        withdrawn = sorted(self.pending)
        self.pending.clear()
        data = {"state": state, "reason": reason, "detail": detail, "served_model": self.served_model,
                "native_session_id": self.thread_id, "turn_id": self.turn_id, **(extra or {})}
        return Step(frames=[Frame("close", "close")], resolved=withdrawn, outcome=self.outcome,
                    events=[Event("turn.completed", data, source)])


class _Sources:
    def __init__(self, offset: int):
        self.offset = offset
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"{self.offset}:{self.n}"


def _rpc_id(request_id: str) -> Any:
    """Echo the server's id in its own type (integers stay integers)."""
    try:
        return int(request_id)
    except ValueError:
        return request_id


def _error_kind(error: Any) -> str | None:
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return next(iter(info))
    return None


def _limit_error(error: Any) -> bool:
    return _error_kind(error) in LIMIT_ERRORS


def _tool_view(item: dict) -> tuple[str | None, Any]:
    itype = item.get("type")
    if itype == "commandExecution":
        return "command", {"command": item.get("command")}
    if itype == "fileChange":
        paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict) and c.get("path")]
        return "edit", {"path": ", ".join(map(str, paths))}
    if itype == "mcpToolCall":
        return f"{item.get('server')}/{item.get('tool')}", item.get("arguments")
    if itype == "webSearch":
        return "web search", {"query": item.get("query")}
    if itype == "dynamicToolCall":
        return str(item.get("tool") or "tool"), item.get("arguments")
    return None, None


def _tool_output(item: dict) -> tuple[str, bool]:
    itype = item.get("type")
    if itype == "commandExecution":
        code = item.get("exitCode")
        return str(item.get("aggregatedOutput") or ""), bool(code) if code is not None else False
    if itype == "fileChange":
        return "", item.get("status") not in ("completed", None)
    if itype == "mcpToolCall":
        return json.dumps(item.get("result"))[:4000] if item.get("result") else "", bool(item.get("error"))
    return "", False
