"""One Claude conversation turn over stream-json (C-26.5, C-26.6, C-27; design §7).

The process is `claude -p --input-format stream-json --output-format
stream-json --verbose --include-partial-messages --replay-user-messages
--permission-prompt-tool stdio …` (see `argv`). The driver sends the SDK
`initialize` control request, checks the account and settings it reports,
sends the user message carrying the message id as its `uuid`, maps the output
to events, turns `can_use_tool` requests into approvals, and ends the turn on
`result` by closing stdin.

Shapes are those Claude Code 2.1.280 accepted and produced in a local probe
(2026-09-24: `initialize` answered with `account`, `models`,
`fast_mode_state`; exit 0 on stdin EOF) and the SDK wire schema documented in
`docs/desktop/maps/provider-protocols.md` §2.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable

from ..adapters.claude import model_matches_requested
from ..adapters.claude_stream import is_synthetic_api_error
from . import redact
from .turn import (
    COMPLETE, FAILED, INTERRUPTED, Approval, Event, Frame, Outcome, Step, TurnSpec,
)

INIT_REQUEST_ID = "subfleet-init"
INTERRUPT_REQUEST_ID = "subfleet-interrupt"
UNSUPPORTED = "Subfleet does not support this request; answer it in the provider's own app."

PERMISSION_FLAGS = {
    "ask": ("--permission-mode", "default", "--permission-prompt-tool", "stdio"),
    "accept-edits": ("--permission-mode", "acceptEdits", "--permission-prompt-tool", "stdio"),
    "bypass": ("--permission-mode", "bypassPermissions"),
}


def argv(spec: TurnSpec, *, claude_bin: str = "claude", read_only_flags: tuple[str, ...] = ()) -> list[str]:
    """The provider command for one turn. `read_only_flags` is the adapter's
    read-only set (C-14.3), passed in so there is one definition of it."""
    command = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages", "--replay-user-messages",
               "--model", spec.model_id]
    if spec.effort:
        command += ["--effort", spec.effort]
    if spec.native_session_id:
        command += ["--resume", spec.native_session_id]
    elif spec.new_session_id:
        command += ["--session-id", spec.new_session_id]
    else:
        raise ValueError("a Claude turn needs a session to resume or a new session id")
    if spec.permission == "read-only":
        command += list(read_only_flags)
    elif spec.permission in PERMISSION_FLAGS:
        command += list(PERMISSION_FLAGS[spec.permission])
    else:
        raise ValueError(f"unknown permission {spec.permission!r}")
    if spec.fast:
        command += ["--settings", json.dumps({"fastMode": True}, separators=(",", ":"))]
    return command


def _line(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class ClaudeTurn:
    def __init__(self, spec: TurnSpec, *, read_bytes: Callable[[str], bytes]):
        self.spec = spec
        self._read_bytes = read_bytes
        self.phase = "new"            # new → initializing → sent → ended
        self.accepted = False
        self.answered = False
        self.limited = False
        self.interrupt_requested = False
        self.served_model: str | None = None
        self.outcome: Outcome | None = None
        self.pending: dict[str, dict[str, Any]] = {}     # request id → original can_use_tool request
        self._tools: dict[str, bool] = {}                 # tool_use id → hidden
        self._message_id: str | None = None               # current streamed assistant message
        self._buffers: dict[str, redact.DeltaBuffer] = {}

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> Step:
        self.phase = "initializing"
        request = {"type": "control_request", "request_id": INIT_REQUEST_ID,
                   "request": {"subtype": "initialize"}}
        return Step(frames=[Frame("init", "write", _line(request))],
                    events=[Event("status", {"phase": "starting-provider"}, "cmd:start")])

    def interrupt(self) -> Step:
        """A person asked to stop this turn (C-24.7)."""
        if self.outcome is not None or self.interrupt_requested:
            return Step()
        self.interrupt_requested = True
        if self.phase in ("new", "initializing"):
            # The message was never written: nothing reached the model.
            return self._end(INTERRUPTED, "stopped-before-send", source="cmd:interrupt")
        request = {"type": "control_request", "request_id": INTERRUPT_REQUEST_ID,
                   "request": {"subtype": "interrupt"}}
        return Step(frames=[Frame("interrupt", "write", _line(request))],
                    events=[Event("status", {"phase": "stopping"}, "cmd:interrupt")])

    def respond(self, request_id: str, decision: str, message: str | None = None) -> Step:
        """A person's answer to one pending `can_use_tool` request (C-27.1, C-27.2)."""
        request = self.pending.pop(request_id, None)
        if request is None:
            return Step()
        if decision == "allow":
            result: dict[str, Any] = {"behavior": "allow", "updatedInput": request.get("input") or {}}
        elif decision in ("deny", "cancel-turn"):
            result = {"behavior": "deny", "message": message or "Denied in Subfleet."}
            if decision == "cancel-turn":
                result["interrupt"] = True
                self.interrupt_requested = True
        else:
            raise ValueError(f"decision {decision!r} is not offered for a Claude tool request")
        if request.get("tool_use_id"):
            result["toolUseID"] = request["tool_use_id"]
        response = {"type": "control_response",
                    "response": {"subtype": "success", "request_id": request_id, "response": result}}
        return Step(frames=[Frame(f"approval:{request_id}", "write", _line(response))],
                    resolved=[request_id],
                    events=[Event("approval.resolved", {"request_id": request_id, "decision": decision},
                                  f"cmd:approval:{request_id}")])

    def eof(self, offset: int) -> Step:
        """stdout ended. Without a `result`, the turn's fate is for reconciliation (C-24.6)."""
        if self.outcome is not None:
            return Step()
        step = self._flush(f"{offset}:eof")
        reason = "stopped" if self.interrupt_requested else "ended-without-result"
        self.outcome = Outcome(INTERRUPTED if self.interrupt_requested else FAILED, reason,
                               accepted=self.accepted, answered=self.answered,
                               limited=self.limited, served_model=self.served_model)
        self.phase = "ended"
        step.outcome = self.outcome
        return step

    # --- input -----------------------------------------------------------------

    def feed(self, raw: bytes | str, offset: int) -> Step:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            row = json.loads(text)
        except ValueError:
            return Step()
        if not isinstance(row, dict) or self.phase == "ended":
            return Step()
        source = _Sources(offset)
        kind = row.get("type")
        if kind == "control_response":
            return self._control_response(row, source)
        if kind == "control_request":
            return self._control_request(row, source)
        if kind == "control_cancel_request":
            request_id = str(row.get("request_id") or "")
            if self.pending.pop(request_id, None) is not None:
                return Step(resolved=[request_id],
                            events=[Event("approval.resolved", {"request_id": request_id, "decision": "withdrawn"},
                                          source.next())])
            return Step()
        if row.get("parent_tool_use_id"):
            return Step()                       # a subagent's frames stay inside its tool call
        if kind == "system":
            return self._system(row, source)
        if kind == "stream_event":
            return self._stream_event(row, source)
        if kind == "assistant":
            return self._assistant(row, source)
        if kind == "user":
            return self._user(row, source)
        if kind == "rate_limit_event":
            return self._rate_limit(row, source)
        if kind == "result":
            return self._result(row, source)
        return Step()

    # --- handlers --------------------------------------------------------------

    def _control_response(self, row: dict, source: "_Sources") -> Step:
        response = row.get("response") or {}
        if response.get("request_id") != INIT_REQUEST_ID or self.phase != "initializing":
            return Step()
        if response.get("subtype") != "success":
            return self._end(FAILED, "provider-init-failed", detail=str(response.get("error") or "")[:300],
                             source=source.next())
        body = response.get("response") or {}
        account = (body.get("account") or {}).get("email")
        if self.spec.lane_identity and account and account.lower() != self.spec.lane_identity.lower():
            # C-10.6: the credential answered for another account; nothing is sent.
            return self._end(FAILED, "identity", detail=f"lane claims {self.spec.lane_identity}, provider says {account}",
                             source=source.next())
        effort_levels = _effort_levels(body.get("models"), self.spec.model_id)
        if self.spec.effort and effort_levels is not None and self.spec.effort not in effort_levels:
            return self._end(FAILED, "effort-unsupported",
                             detail=f"{self.spec.model_id} offers {', '.join(effort_levels) or 'no effort levels'}",
                             source=source.next())
        served = {"account": account, "fast_mode_state": body.get("fast_mode_state"),
                  "fast_mode_disabled_reason": body.get("fast_mode_disabled_reason"),
                  "permission_mode": body.get("current_permission_mode")}
        self.phase = "sent"
        message = {"type": "user", "uuid": self.spec.message_id, "parent_tool_use_id": None,
                   "session_id": self.spec.native_session_id or self.spec.new_session_id,
                   "message": {"role": "user", "content": self._content()}}
        return Step(frames=[Frame("user-message", "write", _line(message))],
                    events=[Event("served", served, source.next()),
                            Event("status", {"phase": "sent"}, source.next())])

    def _content(self) -> list[dict]:
        content: list[dict] = []
        if self.spec.text:
            content.append({"type": "text", "text": self.spec.text})
        for image in self.spec.images:
            data = base64.b64encode(self._read_bytes(image.path)).decode("ascii")
            content.append({"type": "image", "source": {"type": "base64", "media_type": image.media_type,
                                                        "data": data}})
        return content

    def _control_request(self, row: dict, source: "_Sources") -> Step:
        request_id = str(row.get("request_id") or "")
        request = row.get("request") or {}
        subtype = request.get("subtype")
        if subtype == "can_use_tool" and request_id:
            if request_id in self.pending:
                return Step()                   # re-announced after a restart (C-27.3)
            self.pending[request_id] = request
            name = str(request.get("tool_name") or "tool")
            summary = {
                "tool": name,
                "title": request.get("title") or request.get("display_name"),
                "description": request.get("description"),
                "input": redact.truncate(redact.scrub(redact._summary_text(name, request.get("input"))),
                                         redact.INPUT_MAX),
                "reason": request.get("decision_reason"),
                "blocked_path": request.get("blocked_path"),
            }
            options = ("deny", "cancel-turn") if request.get("requires_user_interaction") else ("allow", "deny", "cancel-turn")
            approval = Approval(request_id, "tool", summary, options)
            return Step(approvals=[approval],
                        events=[Event("approval.requested", {"request_id": request_id, **summary,
                                                             "options": list(options)}, source.next())])
        # Anything else (authentication refreshes, hooks, MCP messages) is refused (C-27.4).
        response = {"type": "control_response",
                    "response": {"subtype": "error", "request_id": request_id, "error": UNSUPPORTED}}
        return Step(frames=[Frame(f"refuse:{request_id}", "write", _line(response))] if request_id else [],
                    events=[Event("error", {"message": f"refused unsupported provider request {subtype!r}"},
                                  source.next())])

    def _system(self, row: dict, source: "_Sources") -> Step:
        if row.get("subtype") != "init":
            return Step()
        step = Step()
        model = row.get("model")
        if isinstance(model, str) and model:
            step.extend(self._check_model(model, source))
        step.events.append(Event("served", {"model": model, "permission_mode": row.get("permissionMode"),
                                            "fast_mode_state": row.get("fast_mode_state")}, source.next()))
        return step

    def _stream_event(self, row: dict, source: "_Sources") -> Step:
        event = row.get("event") or {}
        etype = event.get("type")
        if etype == "message_start":
            self._message_id = str((event.get("message") or {}).get("id") or "")
            return Step()
        block = f"{self._message_id}:{event.get('index')}"
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return self._delta("text.delta", block, str(delta.get("text") or ""), source)
            if delta.get("type") == "thinking_delta":
                return self._delta("thinking.delta", block, str(delta.get("thinking") or ""), source)
        if etype == "content_block_stop":
            return self._flush(source.next(), block=block)
        return Step()

    def _delta(self, kind: str, block: str, text: str, source: "_Sources") -> Step:
        self.answered = True
        buffer = self._buffers.setdefault(f"{kind}|{block}", redact.DeltaBuffer())
        ready = buffer.feed(text)
        return Step(events=[Event(kind, {"block": block, "text": ready}, source.next())]) if ready else Step()

    def _flush(self, source: str, *, block: str | None = None) -> Step:
        step = Step()
        for key in sorted(self._buffers):
            kind, _, name = key.partition("|")
            if block is not None and name != block:
                continue
            ready = self._buffers.pop(key).flush()
            if ready:
                step.events.append(Event(kind, {"block": name, "text": ready}, f"{source}:{kind}:{name}"))
        return step

    def _assistant(self, row: dict, source: "_Sources") -> Step:
        message = row.get("message") or {}
        if is_synthetic_api_error(row):
            text = "".join(b.get("text", "") for b in message.get("content") or [] if isinstance(b, dict))
            if row.get("error") in ("rate_limit", "billing_error"):
                self.limited = True
            return Step(events=[Event("error", {"message": redact.bounded_text(text), "kind": row.get("error")},
                                      source.next())])
        step = Step()
        model = message.get("model")
        if isinstance(model, str) and model:
            step.extend(self._check_model(model, source))
            if self.outcome is not None:
                return step
        message_id = str(message.get("id") or self._message_id or "")
        for index, block in enumerate(message.get("content") or []):
            if not isinstance(block, dict):
                continue
            key = f"{message_id}:{index}"
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                self.answered = True
                self._buffers.pop(f"text.delta|{key}", None)
                step.events.append(Event("text", {"block": key, "text": redact.bounded_text(block["text"])}, source.next()))
            elif btype == "thinking" and block.get("thinking"):
                self._buffers.pop(f"thinking.delta|{key}", None)
                step.events.append(Event("thinking", {"block": key, "text": redact.bounded_text(block["thinking"])},
                                         source.next()))
            elif btype == "tool_use":
                self.answered = True
                started = redact.tool_started(str(block.get("name") or "tool"), block.get("input"),
                                              tool_id=block.get("id"))
                self._tools[str(block.get("id"))] = started["hidden"]
                step.events.append(Event("tool.started", started, source.next()))
        return step

    def _user(self, row: dict, source: "_Sources") -> Step:
        if row.get("uuid") == self.spec.message_id and not self.accepted:
            self.accepted = True
            return Step(events=[Event("accepted", {"message_id": self.spec.message_id}, source.next())])
        step = Step()
        content = (row.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id"))
                    step.events.append(Event("tool.completed", redact.tool_completed(
                        tool_id, _result_text(block.get("content")), is_error=block.get("is_error"),
                        hidden=self._tools.get(tool_id, False)), source.next()))
        return step

    def _rate_limit(self, row: dict, source: "_Sources") -> Step:
        info = row.get("rate_limit_info") or {}
        if info.get("status") == "rejected":
            self.limited = True
        windows = {k: {"utilization": v.get("utilization"), "resets_at": v.get("resetsAt")}
                   for k, v in (info.get("unifiedWindows") or {}).items() if isinstance(v, dict)}
        return Step(events=[Event("limits", {"status": info.get("status"), "type": info.get("rateLimitType"),
                                             "resets_at": info.get("resetsAt"), "windows": windows}, source.next())])

    def _result(self, row: dict, source: "_Sources") -> Step:
        step = self._flush(source.next())
        ok = row.get("is_error") is False and row.get("subtype") == "success"
        if ok:
            state, reason = COMPLETE, None
        elif self.interrupt_requested:
            state, reason = INTERRUPTED, "stopped"
        elif self.limited:
            state, reason = FAILED, "limited"
        else:
            state, reason = FAILED, str(row.get("subtype") or "error")
        detail = None if ok else redact.bounded_text(
            "\n".join([str(row.get("result") or ""), *map(str, row.get("errors") or [])]).strip())[:1000]
        denials = row.get("permission_denials") or []
        end = self._end(state, reason, detail=detail, source=source.next(),
                        extra={"permission_denials": len(denials), "num_turns": row.get("num_turns"),
                               "fast_mode_state": row.get("fast_mode_state")})
        return step.extend(end)

    # --- helpers ---------------------------------------------------------------

    def _check_model(self, model: str, source: "_Sources") -> Step:
        if model == "<synthetic>":
            return Step()
        self.served_model = model
        if model_matches_requested(model, self.spec.model_id):
            return Step()
        # C-26.8: stop at once; a turn on the wrong model is not the turn asked for.
        step = Step(frames=[] if self.interrupt_requested else [Frame("interrupt", "write", _line(
            {"type": "control_request", "request_id": INTERRUPT_REQUEST_ID, "request": {"subtype": "interrupt"}}))])
        self.interrupt_requested = True
        return step.extend(self._end(FAILED, "model-mismatch", detail=f"asked for {self.spec.model_id}, served {model}",
                                     source=source.next()))

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
                **(extra or {})}
        return Step(frames=[Frame("close", "close")], resolved=withdrawn, outcome=self.outcome,
                    events=[Event("turn.completed", data, source)])


class _Sources:
    """Unique event sources within one stdout line: `<offset>:<ordinal>`."""

    def __init__(self, offset: int):
        self.offset = offset
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"{self.offset}:{self.n}"


def _effort_levels(models: Any, model_id: str) -> list[str] | None:
    """The effort levels Claude offers for `model_id`, or None when it does not say."""
    if not isinstance(models, list):
        return None
    for entry in models:
        if not isinstance(entry, dict):
            continue
        resolved = str(entry.get("resolvedModel") or "").split("[")[0]
        value = str(entry.get("value") or "")
        if model_id in (resolved, value) or (resolved and model_matches_requested(resolved, model_id)):
            levels = entry.get("supportedEffortLevels")
            if entry.get("supportsEffort") is False:
                return []
            return [str(x) for x in levels] if isinstance(levels, list) else None
    return None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content
                         if isinstance(item, dict) and item.get("type") == "text")
    return ""
