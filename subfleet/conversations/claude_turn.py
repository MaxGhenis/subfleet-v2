"""One Claude conversation turn over stream-json (C-26.5, C-26.6, C-27; design §7).

The process is `claude -p --input-format stream-json --output-format
stream-json --verbose --include-partial-messages --replay-user-messages
--thinking-display summarized --permission-prompt-tool stdio …` (see `argv`). The driver sends the SDK
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
from dataclasses import replace
from typing import Any, Callable

from ..adapters.claude import model_matches_requested
from ..adapters.claude_stream import is_synthetic_api_error
from ..usage import parse_usage
from . import redact
from .reconcile import SETTINGS_FRAME
from .turn import (
    COMPLETE, FAILED, INTERRUPTED, Approval, Event, Frame, Image, Outcome, Step, SteerTracking, TurnSpec,
)

INIT_REQUEST_ID = "subfleet-init"
SETTINGS_REQUEST_ID = "subfleet-settings"
INTERRUPT_REQUEST_ID = "subfleet-interrupt"
UNSUPPORTED = "Subfleet does not support this request; answer it in the provider's own app."

#: Claude Code's ultracode (C-26.8): xhigh effort plus standing dynamic-workflow
#: orchestration. It is not a documented `--effort` value (2.1.280 lists low, medium, high,
#: xhigh, max): the CLI sets it per session through the `ultracode` settings key,
#: on models that offer xhigh. A conversation names it as its effort, and a turn
#: becomes `--effort xhigh` with `{"ultracode": true}` in its command-line settings.
ULTRACODE = "ultracode"
ULTRACODE_EFFORT = "xhigh"


def offered_efforts(levels: list[str]) -> list[str]:
    """The efforts a conversation may name for a model whose catalog lists `levels`:
    those, and ultracode wherever xhigh, the effort it runs at, is among them."""
    return [*levels, ULTRACODE] if ULTRACODE_EFFORT in levels and ULTRACODE not in levels else list(levels)

PERMISSION_FLAGS = {
    "ask": ("--permission-mode", "default", "--permission-prompt-tool", "stdio"),
    "accept-edits": ("--permission-mode", "acceptEdits", "--permission-prompt-tool", "stdio"),
    # Bypass still sends what no mode auto-approves (AskUserQuestion, ask rules)
    # to the person instead of letting `-p` deny it (design D-9).
    "bypass": ("--permission-mode", "bypassPermissions", "--permission-prompt-tool", "stdio"),
}
# Tools that schedule work for a session that will not exist after the turn
# (D-15), and plan mode, whose exit needs an approval kind Subfleet does not
# offer yet (review IR-24; design §15).
DISALLOWED_TOOLS = ("Monitor", "CronCreate", "ScheduleWakeup", "RemoteTrigger", "EnterPlanMode", "ExitPlanMode")
BACKGROUND_CEILING_MS = 120_000
QUESTION_TOOLS = ("AskUserQuestion",)

#: The status phase a streamed content block of each type starts (design §12).
BLOCK_PHASES = {"thinking": "thinking", "redacted_thinking": "thinking", "text": "writing",
                "tool_use": "preparing-tool", "server_tool_use": "preparing-tool"}
#: The status phase a `system` `status` row announces (2.1.280: `requesting`
#: before every API request, `compacting` while an automatic compaction runs,
#: which took 107 s on a 972k-token resume on 2026-09-24).
STATUS_PHASES = {"requesting": "requesting", "compacting": "compacting"}


def catalog_entry(models: Any, value: str, model_ref: str | None = None) -> dict | None:
    """The `initialize` catalog entry a turn's model resolves to (design D-19).

    The entry whose `value` is the conversation's model; else, when the
    conversation names the model itself (a policy id such as `claude-opus-5-5`,
    which `--model` accepts but the catalog lists as `opus[1m]` or `default`),
    the first entry resolving to the model admission routed the turn to. None
    when this account's catalog offers neither (observed 2026-09-24: values
    `default`, `opus[1m]`, `claude-fable-5-1[1m]`, `sonnet`, `haiku`)."""
    entries = [e for e in (models if isinstance(models, list) else ()) if isinstance(e, dict) and e.get("resolvedModel")]
    for entry in entries:
        if entry.get("value") == value:
            return entry
    if model_ref:
        for entry in entries:
            if strip_context(str(entry["resolvedModel"])) == model_ref:
                return entry
    return None


def expected_model(value: str, models: Any, model_ref: str | None = None) -> str | None:
    """The model a turn serves, `[1m]` removed, or None when the catalog does
    not offer it."""
    entry = catalog_entry(models, value, model_ref)
    return strip_context(str(entry["resolvedModel"])) if entry else None


def observed_catalog(models: Any) -> list[dict]:
    """The catalog as `models.json` keeps it: what each value serves and offers."""
    out = []
    for entry in models if isinstance(models, list) else ():
        if isinstance(entry, dict) and entry.get("value") and entry.get("resolvedModel"):
            levels = entry.get("supportedEffortLevels")
            out.append({"value": str(entry["value"]), "model": strip_context(str(entry["resolvedModel"])),
                        "context_1m": str(entry["resolvedModel"]).endswith("[1m]"),
                        "display": entry.get("displayName"),
                        "efforts": offered_efforts([str(x) for x in levels]) if entry.get("supportsEffort") is not False
                        and isinstance(levels, list) else [],
                        "fast": entry.get("supportsFastMode")})
    return out


def strip_context(model: str) -> str:
    return model[:-4] if model.endswith("[1m]") else model


def argv(spec: TurnSpec, *, claude_bin: str = "claude", read_only_flags: tuple[str, ...] = ()) -> list[str]:
    """The provider command for one turn. `read_only_flags` is the adapter's
    read-only set (C-14.3), passed in so there is one definition of it."""
    command = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages", "--replay-user-messages",
               # `-p` otherwise asks the API to omit thinking text: deltas arrive
               # empty (observed 2026-09-24, 2.1.280) and the person sees no thinking.
               "--thinking-display", "summarized",
               "--model", spec.model_id]
    ultracode = spec.effort == ULTRACODE
    if spec.effort:
        command += ["--effort", ULTRACODE_EFFORT if ultracode else spec.effort]
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
        command += ["--disallowedTools", ",".join(DISALLOWED_TOOLS)]
        # Command-line settings outrank project and local settings, so a workspace
        # file cannot switch the user's never-rules hook off (C-26.11).
        settings: dict[str, Any] = {"disableAllHooks": False}
        if spec.fast:
            settings["fastMode"] = True
        if ultracode:
            # C-26.8: the orchestration half of ultracode. Read-only turns get only
            # its effort: their tool set has no Workflow tool to orchestrate with.
            settings["ultracode"] = True
        command += ["--settings", json.dumps(settings, separators=(",", ":"))]
    else:
        raise ValueError(f"unknown permission {spec.permission!r}")
    if spec.permission == "read-only" and spec.fast:
        command += ["--settings", json.dumps({"fastMode": True}, separators=(",", ":"))]
    return command


def environment() -> dict[str, str]:
    """Variables every Claude turn adds (D-15)."""
    return {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": str(BACKGROUND_CEILING_MS)}


def _line(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class ClaudeTurn(SteerTracking):
    def __init__(self, spec: TurnSpec, *, read_bytes: Callable[[Image], bytes],
                 frame_recorded: Callable[[str], bool] = lambda tag: False):
        self.spec = spec
        self._read_bytes = read_bytes
        self._frame_recorded = frame_recorded
        self.phase = "new"            # new → initializing → sent → ended
        self.accepted = False
        self.answered = False
        self.limited = False
        self.interrupt_requested = False
        self.served_model: str | None = None
        self.expected_model: str | None = None            # from the initialize catalog (D-19)
        self.catalog: list[dict] | None = None            # the initialize catalog, for models.json
        self.outcome: Outcome | None = None
        self._usage_lines: list[str] = []                 # C-12.10: usage-bearing provider frames only
        self.terminal_after_end = False                   # a `result` arrived after the driver ended the turn
        self.pending: dict[str, dict[str, Any]] = {}     # request id → original can_use_tool request
        self._tools: dict[str, bool] = {}                 # tool_use id → hidden
        self._message_id: str | None = None               # current streamed assistant message
        self._buffers: dict[str, redact.DeltaBuffer] = {}
        # Content blocks already seen complete, per message. The CLI writes one
        # `assistant` row per finished block, each holding that block alone at
        # content index 0, in stream order: the nth block of a message is the
        # stream's block n, so an ordinal keys it the way its deltas were keyed
        # (51 of 51 rows on 2026-09-24, 2.1.280). A row holding several blocks
        # counts each.
        self._completed: dict[str, int] = {}
        self._phase: str | None = None                    # the last block phase announced
        self._init_steers()
        self.capabilities: set[str] = set()
        self.steer_waiting = False
        # Counts the spells of `steer_waiting`: each time it turns on. The runner's
        # watchdog clock starts afresh with each, however briefly it was off.
        self.steer_watch = 0
        # The unseen steers the watchdog's current round asked the CLI to cancel
        # (`expire_steers`); None while no round is under way.
        self._steer_round: set[str] | None = None
        # Steers the CLI started after the held result: they run as their own turn,
        # which the next result ends, or stdout's end cuts short (C-26.5).
        self._steer_turns: set[str] = set()
        self._last_result: dict | None = None
        self._last_result_offset = 0
        self._queued_turn_count = 0

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> Step:
        if self.spec.held_by:
            # C-26.3: another Claude process took the session after dispatch looked.
            # Nothing is sent; the message waits for it again (`readmit:external-writer`).
            return self._end(FAILED, "external-writer", source="cmd:start",
                             detail="pid " + ", ".join(str(pid) for pid in self.spec.held_by))
        self.phase = "initializing"
        request = {"type": "control_request", "request_id": INIT_REQUEST_ID,
                   "request": {"subtype": "initialize"}}
        events = [Event("status", {"phase": "starting-provider"}, "cmd:start")]
        if self.spec.route_json:
            # C-6.16: the turn left the lane holding its prompt cache; say where, why and until when.
            events.append(Event("route", json.loads(self.spec.route_json), "cmd:start"))
        return Step(frames=[Frame("init", "write", _line(request))], events=events)

    def interrupt(self) -> Step:
        """A person asked to stop this turn (C-24.7)."""
        if self.outcome is not None or self.interrupt_requested:
            return Step()
        self.interrupt_requested = True
        if self.phase in ("new", "initializing"):
            # The message was never written: nothing reached the model.
            return self._end(INTERRUPTED, "stopped-before-send", source="cmd:interrupt")
        request = {"type": "control_request", "request_id": INTERRUPT_REQUEST_ID,
                   "request": self._interrupt_request()}
        return Step(frames=[Frame("interrupt", "write", _line(request))],
                    events=[Event("status", {"phase": "stopping"}, "cmd:interrupt")])

    def _interrupt_request(self) -> dict:
        request: dict[str, Any] = {"subtype": "interrupt"}
        if self.steers and "interrupt_cancel_queued_v1" in self.capabilities:
            # Only a turn with steers has queued commands to sweep; a turn with none
            # sends the interrupt it sent before steer (design §5, invariant 5).
            request["cancel_queued"] = True
        return request

    @property
    def steerable(self) -> bool:
        return (self.phase == "sent" and self.outcome is None and not self.interrupt_requested
                and self._steer_round is None and "msg_lifecycle_v1" in self.capabilities)

    def steer(self, message_id: str, text: str, images: tuple[Image, ...] = ()) -> Step:
        if message_id in self.steers:
            return Step()
        if not self.steerable:
            return self.drop_steer(message_id, "not-steerable")
        # Built before the steer is tracked: an image that cannot be read raises,
        # and the runner sends the steer back to the queue (`TurnRunner._steer`).
        content = self._content(text, images)
        self.restore_steer(message_id, "unsent")
        message = {"type": "user", "uuid": message_id, "priority": "next", "parent_tool_use_id": None,
                   "session_id": self.spec.native_session_id or self.spec.new_session_id,
                   "message": {"role": "user", "content": content}}
        return Step(frames=[Frame(f"steer:{message_id}", "write", _line(message))],
                    events=[Event("steer.sent", {"message_id": message_id}, f"cmd:steer:{message_id}")])

    def drop_steer(self, message_id: str, detail: str) -> Step:
        """A steer that is not written after all (a stop or its cancel won the
        handover, its frame is over the relay cap, its input could not be built):
        a result held only for it ends the turn now, not after the watchdog's two
        rounds (C-26.5)."""
        step = super().drop_steer(message_id, detail)
        if self.outcome is None and self._last_result is not None:
            self._refresh_steer_waiting()
            step.extend(self._finish_held())
        return step

    def expire_steers(self) -> Step:
        """The runner's watchdog: called once `steer_waiting` has held for 15 s, and
        again 15 s later (`runner.STEER_GRACE_S`).

        `steer_waiting` holds only while the host's result is held for steers the
        CLI has not shown taking, and no steer runs as its own turn
        (`_refresh_steer_waiting`). The first call asks the CLI to cancel those
        unseen steers, and takes no new steer until the round resolves. The second,
        with no receipt either, gives up on the steers it named and no other, and
        the turn ends with its held result. A silent CLI is bounded, but absence of
        a cancellation receipt is NOT evidence of non-delivery: those steers settle
        unknown, never blindly requeued.
        """
        if not self.steer_waiting or self.outcome is not None:
            return Step()
        if self._steer_round is not None:
            for mid in self._steer_round:
                self._steer_pending.discard(mid)
            self._steer_round = None
            if not self._steer_pending:
                self._queued_turn_count = 0     # the queued turns the result counted were those
            self._refresh_steer_waiting()
            return self._finish_held()
        self._steer_round = {mid for mid in self._steer_pending if self.steers[mid]["fate"] == "unknown"}
        step = Step()
        for mid in sorted(self._steer_round):
            step.frames.append(self._cancel_frame(mid))
        return step

    @staticmethod
    def _cancel_frame(mid: str) -> Frame:
        request = {"type": "control_request", "request_id": f"cancel-steer:{mid}",
                   "request": {"subtype": "cancel_async_message", "message_uuid": mid}}
        return Frame(f"cancel-steer:{mid}", "write", _line(request))

    def interrupted_earlier(self) -> None:
        """Replay (C-26.6): the relay's log shows an interrupt an earlier runner
        wrote, so the provider has it; what follows reads as the stop's outcome."""
        self.interrupt_requested = True

    def withdraw(self) -> Step:
        """The runner did not hand the message frame over: a stop came first
        (C-24.7). The turn ends as one stopped before sending, and stdin closes."""
        if self.outcome is not None:
            return Step()
        self.interrupt_requested = True
        return self._end(INTERRUPTED, "stopped-before-send", source="cmd:interrupt")

    def respond(self, request_id: str, decision: str, message: str | None = None,
                answers: dict | None = None) -> Step:
        """A person's answer to one pending `can_use_tool` request (C-27.1, C-27.2)."""
        request = self.pending.pop(request_id, None)
        if request is None:
            return Step()
        if decision == "allow":
            result: dict[str, Any] = {"behavior": "allow", "updatedInput": request.get("input") or {}}
        elif decision == "answer":
            if not isinstance(answers, dict) or not answers:
                self.pending[request_id] = request
                raise ValueError("an answer needs the chosen answers")
            # The only change Subfleet makes to a request's input (C-27.2).
            result = {"behavior": "allow", "updatedInput": {**(request.get("input") or {}), "answers": answers}}
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
        frames = [Frame(f"approval:{request_id}", "write", _line(response))]
        if decision == "cancel-turn" and self._steer_pending:
            # The permission reply aborts only the active turn. A separate SDK
            # interrupt must sweep queued messages, or the CLI runs them next.
            frames.append(Frame("interrupt", "write", _line({
                "type": "control_request", "request_id": INTERRUPT_REQUEST_ID,
                "request": self._interrupt_request()})))
        return Step(frames=frames,
                    resolved=[request_id],
                    events=[Event("approval.resolved", {"request_id": request_id, "decision": decision},
                                  f"cmd:approval:{request_id}")])

    def eof(self, offset: int) -> Step:
        """stdout ended. Without a `result`, the turn's fate is for reconciliation (C-24.6).

        With the host's result held for steers the CLI never showed taking (a stop
        whose receipt never came, the process ending), the turn ended with that
        result: the outcome is the last result's (C-26.5), and those steers settle
        on their own evidence. Only a steer's own turn that stdout cut short leaves
        the turn's fate to reconciliation."""
        if self.outcome is not None:
            return Step()
        step = self._flush(f"{offset}:eof")
        if self._last_result is not None and not self._steer_turn_running():
            return step.extend(self._finish_result())
        reason = "stopped" if self.interrupt_requested else "ended-without-result"
        self.outcome = Outcome(INTERRUPTED if self.interrupt_requested else FAILED, reason,
                               accepted=self.accepted, answered=self.answered,
                               limited=self.limited, served_model=self.served_model, ended_by="eof", steers=self.steers,
                               usage=parse_usage(self._usage_lines, "claude"))
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
        if not isinstance(row, dict):
            return Step()
        if row.get("type") == "result":
            self._usage_lines.append(json.dumps({key: row.get(key) for key in ("type", "uuid", "usage", "modelUsage")}))
        elif row.get("type") == "assistant" and isinstance(row.get("message"), dict):
            message = row["message"]
            self._usage_lines.append(json.dumps({"type": "assistant", "parent_tool_use_id": row.get("parent_tool_use_id"),
                                                 "message": {key: message.get(key) for key in ("id", "model", "usage")}}))
        if self.outcome is not None:
            self.outcome = replace(self.outcome, usage=parse_usage(self._usage_lines, "claude"))
        source = _Sources(offset)
        kind = row.get("type")
        if kind == "command_lifecycle":
            return self._lifecycle(row, source)
        if kind == "control_response" and ((row.get("response") or {}).get("request_id") == INTERRUPT_REQUEST_ID
                or str((row.get("response") or {}).get("request_id", "")).startswith("cancel-steer:")):
            return self._steer_control(row.get("response") or {}, source)
        if self.phase == "ended":
            # After the terminal event: background output belongs to the same
            # message and never changes its outcome (C-26.5). A `result` after the
            # driver itself ended the turn (a model mismatch) says the provider
            # finished it (C-24.8).
            if kind == "result":
                self.terminal_after_end = True
                step = Step()
                for mid in row.get("user_message_uuids") or [row.get("user_message_uuid")]:
                    step.extend(self._steer_delivered(mid, source.next(), fate="consumed"))
                return step
            if kind == "control_response" and (row.get("response") or {}).get("request_id") == SETTINGS_REQUEST_ID:
                return self._settings(row["response"], source)      # C-26.8: evidence, not an outcome
            if kind in ("assistant", "user", "stream_event") and not row.get("parent_tool_use_id"):
                handler = {"assistant": self._assistant, "user": self._user, "stream_event": self._stream_event}[kind]
                step = handler(row, source)
                return Step(events=[e for e in step.events if e.kind != "status"])
            return Step()
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
        if response.get("request_id") == SETTINGS_REQUEST_ID:
            return self._settings(response, source)
        if response.get("request_id") != INIT_REQUEST_ID or self.phase != "initializing":
            return Step()
        if response.get("subtype") != "success":
            return self._end(FAILED, "provider-init-failed", detail=str(response.get("error") or "")[:300],
                             source=source.next())
        body = response.get("response") or {}
        account = (body.get("account") or {}).get("email")
        self.catalog = observed_catalog(body.get("models"))
        if self.spec.lane_email and account and account.lower() != self.spec.lane_email.lower():
            # C-10.6: the credential answered for another account; nothing is sent.
            return self._end(FAILED, "identity", detail=f"lane claims {self.spec.lane_email}, provider says {account}",
                             source=source.next())
        entry = catalog_entry(body.get("models"), self.spec.model_id, self.spec.model_ref)
        if entry is None:
            return self._end(FAILED, "settings-unsupported",
                             detail=f"{self.spec.model_id} is not in this account's model catalog",
                             source=source.next())
        self.expected_model = strip_context(str(entry["resolvedModel"]))
        effort_levels = _entry_efforts(entry)
        if self.spec.effort and self.spec.effort not in effort_levels and not self.spec.effort_default:
            # A named effort the account does not offer is refused before sending. A
            # policy default is not: the CLI then applies what the model allows (2.1.280
            # drops xhigh and ultracode on Haiku), and `get_settings` reports it.
            return self._end(FAILED, "effort-unsupported",
                             detail=f"{self.spec.model_id} offers {', '.join(effort_levels) or 'no effort levels'}",
                             source=source.next())
        if self.spec.fast and (body.get("fast_mode_disabled_reason") or body.get("fast_mode_state") != "on"
                               or entry.get("supportsFastMode") is False):
            # IR-23: Fast was asked for and this account or model will not serve it.
            # Nothing is sent, so the message can be admitted again elsewhere.
            reason = body.get("fast_mode_disabled_reason") or (
                "the model offers no Fast mode" if entry.get("supportsFastMode") is False else body.get("fast_mode_state"))
            return self._end(FAILED, "fast-unavailable", detail=str(reason), source=source.next())
        served = {"account": account, "fast_mode_state": body.get("fast_mode_state"),
                  "fast_mode_disabled_reason": body.get("fast_mode_disabled_reason"),
                  "permission_mode": body.get("current_permission_mode")}
        if self.spec.effort:
            # What the command line asked for; the effort served is what the provider's
            # `get_settings` answer reports (C-26.8), recorded when it arrives. It is asked
            # right after the message, so a message withheld before sending asks nothing.
            served["effort_requested"] = self.spec.effort
            if self.spec.effort_default:
                served["effort_default"] = True
        self.phase = "sent"
        frames = []
        if not self._frame_recorded("user-message"):
            try:
                content = self._content()
            except OSError:
                return self._end(FAILED, "attachment-missing", source=source.next(),
                                 detail="an image is missing, changed, or not private; add it again")
            message = {"type": "user", "uuid": self.spec.message_id, "parent_tool_use_id": None,
                       "session_id": self.spec.native_session_id or self.spec.new_session_id,
                       "message": {"role": "user", "content": content}}
            frames.append(Frame("user-message", "write", _line(message)))
        # Replay the state transition and events without reconstructing a payload
        # the relay already wrote. Its attachment may have been removed since.
        # C-26.8: `get_settings` follows the message; the runner drops it when an
        # earlier runner sent the message.
        ask = {"type": "control_request", "request_id": SETTINGS_REQUEST_ID, "request": {"subtype": "get_settings"}}
        frames.append(Frame(SETTINGS_FRAME, "write", _line(ask)))
        return Step(frames=frames,
                    events=[Event("served", served, source.next()),
                            Event("status", {"phase": "sent"}, source.next())])

    def _settings(self, response: dict, source: "_Sources") -> Step:
        """C-26.8: the effort the provider applied, from its `get_settings` answer.
        Ultracode is reported as its own flag beside the effort it runs at; a
        provider that applied none reports null, recorded as `effort: none`. A CLI
        without `get_settings` leaves the served effort unrecorded."""
        applied = (response.get("response") or {}).get("applied") if response.get("subtype") == "success" else None
        if not isinstance(applied, dict):
            return Step()
        effort = ULTRACODE if applied.get("ultracode") is True else (applied.get("effort") or "none")
        return Step(events=[Event("served", {"effort": str(effort)}, source.next())])

    def _content(self, text: str | None = None, images: tuple[Image, ...] | None = None) -> list[dict]:
        content: list[dict] = []
        text = self.spec.text if text is None else text
        if text:
            content.append({"type": "text", "text": text})
        for image in self.spec.images if images is None else images:
            data = base64.b64encode(self._read_bytes(image)).decode("ascii")
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
                "input": redact.bounded(redact._summary_text(name, request.get("input")),
                                        redact.INPUT_MAX),
                "reason": request.get("decision_reason"),
                "blocked_path": request.get("blocked_path"),
            }
            if name in QUESTION_TOOLS:
                kind, options = "question", ("answer", "deny", "cancel-turn")
                summary["questions"] = (request.get("input") or {}).get("questions")
            elif request.get("requires_user_interaction"):
                kind, options = "tool", ("deny", "cancel-turn")
            else:
                kind, options = "tool", ("allow", "deny", "cancel-turn")
            approval = Approval(request_id, kind, summary, options, request=request)
            return Step(approvals=[approval],
                        events=[Event("approval.requested", {"request_id": request_id, **summary,
                                                             "kind": kind, "options": list(options)}, source.next())])
        # Anything else (authentication refreshes, hooks, MCP messages) is refused (C-27.4).
        response = {"type": "control_response",
                    "response": {"subtype": "error", "request_id": request_id, "error": UNSUPPORTED}}
        return Step(frames=[Frame(f"refuse:{request_id}", "write", _line(response))] if request_id else [],
                    events=[Event("error", {"message": f"refused unsupported provider request {subtype!r}"},
                                  source.next())])

    def _system(self, row: dict, source: "_Sources") -> Step:
        if row.get("subtype") == "init":
            self.capabilities = {v for v in row.get("capabilities", []) if isinstance(v, str)}
        if row.get("subtype") == "status":
            if row.get("status") is None and row.get("compact_result"):
                # A compaction ended (either way); the request it held up goes next.
                return self._announce("requesting", source)
            return self._announce(STATUS_PHASES.get(str(row.get("status"))), source)
        if row.get("subtype") == "compact_boundary":
            # The model now works from a summary of the earlier conversation.
            meta = row.get("compact_metadata") or {}
            self._phase = "compacted"
            return Step(events=[Event("status", {"phase": "compacted", "trigger": meta.get("trigger"),
                                                 "pre_tokens": meta.get("pre_tokens"),
                                                 "post_tokens": meta.get("post_tokens")}, source.phase())])
        if row.get("subtype") == "notification" and row.get("key") == "fast-mode-overage-rejected":
            # IR-23: matched on the structured key; the turn continues at standard speed.
            return Step(events=[Event("served", {"fast_mode_state": "off", "fast_warning": "usage credits exhausted"},
                                      source.next())])
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
        if etype == "content_block_start":
            # Where the model is while nothing displayable streams: thinking whose
            # text the API omits, or a long tool input (design §12, the status strip).
            return self._announce(BLOCK_PHASES.get(str((event.get("content_block") or {}).get("type"))), source)
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return self._delta("text.delta", block, str(delta.get("text") or ""), source)
            if delta.get("type") == "thinking_delta":
                return self._delta("thinking.delta", block, str(delta.get("thinking") or ""), source)
        if etype == "content_block_stop":
            return self._flush(source.next(), block=block)
        return Step()

    def _announce(self, phase: str | None, source: "_Sources") -> Step:
        """A `status` event for where the model is, when that changed (design §12).
        It depends on stdout alone, so a replay after a restart makes the same
        events; after a stop the app keeps saying Stopping. Its source is the
        line's own `phase` slot, so it never moves another event's ordinal."""
        if phase is None or phase == self._phase:
            return Step()
        self._phase = phase
        return Step(events=[Event("status", {"phase": phase}, source.phase())])

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
        if message.get("model") == "<synthetic>" and not is_synthetic_api_error(row):
            # IR-25: a local placeholder ("No response requested.") is neither the
            # model's output nor an API error.
            return Step()
        if is_synthetic_api_error(row):
            text = "".join(b.get("text", "") for b in message.get("content") or [] if isinstance(b, dict))
            if row.get("error") in ("rate_limit", "billing_error"):
                self.limited = True
            return Step(events=[Event("error", {"message": redact.bounded_text(text), "kind": row.get("error")},
                                      source.next())])
        # Count this row's blocks first: a row that ends the turn (a model
        # mismatch) still takes its stream positions, or every later block of
        # the message would be keyed one short of its deltas.
        content = message.get("content") or []
        message_id = str(message.get("id") or self._message_id or "")
        start = self._completed.get(message_id, 0)
        self._completed[message_id] = start + len(content)
        step = Step()
        model = message.get("model")
        if isinstance(model, str) and model:
            check = self._check_model(model, source)
            step.extend(check)
            if check.outcome is not None:
                return step
        for offset, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            key = f"{message_id}:{start + offset}"
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
        if str(row.get("uuid")) in self.steers:
            step.extend(self._steer_delivered(str(row.get("uuid")), source.next()))
            self._refresh_steer_waiting()
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
        consumed = row.get("user_message_uuids") or [row.get("user_message_uuid")]
        for mid in consumed:
            if mid in self.steers:
                step.extend(self._steer_delivered(mid, source.next(), fate="consumed"))
                self._steer_pending.discard(mid)
        # A result ends the turn every started steer is part of, folded into it or
        # its own: one the CLI started (delivered) waits for nothing more, listed
        # here or not, and no steer's own turn runs past it.
        self._steer_pending -= {mid for mid in self._steer_pending if self.steers[mid]["fate"] == "delivered"}
        self._steer_turns.clear()
        self._last_result = row
        self._last_result_offset = source.offset
        self._queued_turn_count = row.get("queued_turn_count") or 0
        if self.steers and (self._steer_pending or self._queued_turn_count > 0):
            self._refresh_steer_waiting()
            return step
        return step.extend(self._finish_result(source))

    def _finish_result(self, source: "_Sources | None" = None) -> Step:
        if self._last_result is None or self.outcome is not None:
            return Step()
        row = self._last_result
        source = source or _Sources(self._last_result_offset)
        self.steer_waiting = False
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
                               "fast_mode_state": row.get("fast_mode_state"),
                               "stop_too_late": ok and self.interrupt_requested}, ended_by="provider")
        return end

    def _finish_held(self, source: "_Sources | None" = None) -> Step:
        """End the turn with the held result once nothing it waits for is left:
        no steer the CLI has not settled, no queued turn it counted, and no steer
        running as its own turn (whose result, not the held one, ends the turn)."""
        if self._steer_pending or self._queued_turn_count > 0 or self._steer_turn_running():
            return Step()
        return self._finish_result(source)

    def _steer_turn_running(self) -> bool:
        """A steer the CLI started after the held result runs as its own turn until
        the next result, even one a stop's receipt then calls cancelled: that turn
        was started, and only its result or stdout's end says how it ended."""
        return self._last_result is not None and bool(self._steer_turns)

    def _refresh_steer_waiting(self) -> None:
        # The 15 s watchdog bounds only a held result's unseen steers. While a steer
        # runs as its own turn the process is working: model and tool latency is
        # unrestricted, and a steer written meanwhile folds at that turn's next tool
        # boundary or runs after its result, which is then the one held (C-26.5).
        waiting = self._last_result is not None and not self._steer_turn_running() and (
            any(self.steers[mid]["fate"] == "unknown" for mid in self._steer_pending)
            or (not self._steer_pending and self._queued_turn_count > 0))
        if waiting and not self.steer_waiting:
            self.steer_watch += 1
        self.steer_waiting = waiting
        if not self.steer_waiting:
            self._steer_round = None            # steering resumes; a later round starts afresh

    def _lifecycle(self, row: dict, source: "_Sources") -> Step:
        mid, state = row.get("command_uuid"), row.get("state")
        if mid == self.spec.message_id and state == "started" and not self.accepted and self.phase != "ended":
            self.accepted = True
            return Step(events=[Event("accepted", {"message_id": self.spec.message_id, "by": "lifecycle"},
                                      source.next())])
        if mid not in self.steers:
            return Step()
        step = Step()
        if state == "started" and self._last_result is not None:
            self._steer_turns.add(mid)          # after the held result: its own turn
        if state in ("started", "completed"):
            step.extend(self._steer_delivered(mid, source.next(), fate="consumed" if state == "completed" else "delivered"))
        if state == "completed":
            self._steer_pending.discard(mid)
        elif state in ("cancelled", "discarded", "refused"):
            step.extend(self._steer_refused(mid, str(state), source.next(),
                                           fate="cancelled" if state != "refused" else "refused"))
        self._refresh_steer_waiting()
        # The lifecycle of a newly started turn follows its own result. A fold's
        # terminal lifecycle precedes it; only finish here when a result waits.
        return step.extend(self._finish_held(source))

    def _steer_control(self, response: dict, source: "_Sources") -> Step:
        if response.get("subtype") != "success":
            return Step()
        rid, body = str(response.get("request_id")), response.get("response") or {}
        step = Step()
        if rid.startswith("cancel-steer:") and body.get("cancelled") is True:
            step.extend(self._steer_refused(rid.partition(":")[2], "cancelled-after-result", source.next(), fate="cancelled"))
        elif rid == INTERRUPT_REQUEST_ID:
            for mid in body.get("cancelled") or []:
                step.extend(self._steer_refused(mid, "interrupt-cancelled", source.next(), fate="cancelled"))
            for mid in body.get("still_queued") or []:
                if mid in self.steers:
                    step.frames.append(self._cancel_frame(mid))
        if self._last_result is not None and not self._steer_pending:
            # The CLI's own receipts settled every steer the held result waited for:
            # the queued turns it counted were those, and the turn ends with it.
            self._queued_turn_count = 0
        self._refresh_steer_waiting()
        return step.extend(self._finish_held(source))

    # --- helpers ---------------------------------------------------------------

    def _check_model(self, model: str, source: "_Sources") -> Step:
        if model == "<synthetic>" or self.outcome is not None:
            return Step()
        served = strip_context(model)
        self.served_model = served
        expected = self.expected_model or strip_context(self.spec.model_id)
        if served == expected or model_matches_requested(served, expected):
            return Step()
        # C-26.8: stop at once; a turn on the wrong model is not the turn asked for.
        step = Step(frames=[] if self.interrupt_requested else [Frame("interrupt", "write", _line(
            {"type": "control_request", "request_id": INTERRUPT_REQUEST_ID, "request": self._interrupt_request()}))])
        self.interrupt_requested = True
        return step.extend(self._end(FAILED, "model-mismatch", detail=f"asked for {self.spec.model_id}, served {model}",
                                     source=source.next()))

    def _end(self, state: str, reason: str | None, *, source: str, detail: str | None = None,
             extra: dict | None = None, ended_by: str = "driver") -> Step:
        if self.outcome is not None:
            return Step()
        self.outcome = Outcome(state, reason, detail, accepted=self.accepted, answered=self.answered,
                               limited=self.limited, served_model=self.served_model, ended_by=ended_by, steers=self.steers,
                               usage=parse_usage(self._usage_lines, "claude"))
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

    def phase(self) -> str:
        """The line's one status phase, outside the ordinal sequence."""
        return f"{self.offset}:phase"


def _entry_efforts(entry: dict) -> list[str]:
    """The effort levels a catalog entry offers; one without them accepts none."""
    if entry.get("supportsEffort") is False:
        return []
    levels = entry.get("supportedEffortLevels")
    return offered_efforts([str(x) for x in levels]) if isinstance(levels, list) else []


def _effort_levels(models: Any, value: str) -> list[str] | None:
    """The effort levels the entry chosen by `value` offers, or None when the value is not listed."""
    entry = catalog_entry(models, value)
    return None if entry is None else _entry_efforts(entry)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content
                         if isinstance(item, dict) and item.get("type") == "text")
    return ""
