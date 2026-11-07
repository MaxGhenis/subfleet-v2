"""The conversation loop (C-31.9, C-31.10, C-31.11).

Each turn checks the caps, sends the whole append-only history, appends the full
`response.content` to the history and to `messages.jsonl`, records a `response`
event, and runs the tools the model asked for. A credit refusal ends the attempt
after the neutral check; the `end` event and the exit code say how it ended.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from subfleet.api_agent import pricing
from subfleet.api_agent.records import Records, Redactor, text_of

EXIT = {"done": 0, "credit": 3, "refused-by-kind": 4, "workspace-limit": 5, "budget": 6, "turns": 6, "error": 1}
RETRIES = 2
BACKOFF_S = (5.0, 20.0)
CONTINUE_TEXT = ("Your last reply stopped at the output limit before it finished. Continue from where it "
                 "stopped.")
RESUMED_TEXT = "This job was resumed after an interruption. Check the worktree's state, then continue the job."
UNRUN_TEXT = ("This tool call was not run: the reply stopped at the output limit, so its input may be cut "
              "off. Send the call again if you still need it.")
UNKNOWN_TEXT = ("The previous attempt stopped before this call's result was recorded, so whether it ran is "
                "unknown. Check the worktree's state before you run it again.")


@dataclass
class ToolOutcome:
    """One tool call's result block and its `tool` event."""
    content: str
    is_error: bool
    rc: int | None
    denied: bool
    reason: str | None


@dataclass
class Session:
    model: str
    effort: str
    system: list[dict]
    tools: list[dict]
    history: list[dict]
    max_usd: float
    max_turns: int
    records: Records
    redact: Redactor
    api: object
    run_tool: Callable[[dict], ToolOutcome]
    sleep: Callable[[float], None] = time.sleep
    turns: int = 0
    usd: float = 0.0
    last_text: str = ""
    retries: int = field(default=0)

    # -- bookkeeping -------------------------------------------------------------------
    def _price(self, model: str | None, usage: dict | None) -> float:
        priced = model if model and pricing.known(model) else self.model
        return pricing.usd(priced, usage) if usage else 0.0

    def _append(self, message: dict) -> None:
        self.history.append(message)
        self.records.append_message(message)

    def finish(self, outcome: str) -> int:
        self.records.write_last(self.last_text)
        self.records.event("end", outcome=outcome, turns=self.turns, usd=round(self.usd, 6))
        return EXIT[outcome]

    def _error(self, status: int | None, error_type: str, message: str) -> None:
        self.records.event("error", status=status, error_type=error_type, message=self.redact(message))

    # -- the loop ----------------------------------------------------------------------
    def run(self) -> int:
        while True:
            if self.turns >= self.max_turns:
                return self.finish("turns")
            if self.usd >= self.max_usd:
                return self.finish("budget")
            failure = None
            try:
                reply = self.api.send(model=self.model, effort=self.effort, system=self.system,
                                      tools=self.tools, messages=self.history)
            except Exception as exc:  # noqa: BLE001 - the client raises Failure; anything else is an error
                failure = exc
            if failure is not None:
                ended = self._failed(failure)
                if ended is not None:
                    return ended
                continue
            self.retries = 0
            self.turns += 1
            cost = self._price(reply.model, reply.usage)
            self.usd += cost
            self._append({"role": "assistant", "content": reply.content})
            self.records.event("response", request_id=reply.request_id, model=reply.model, org_id=reply.org_id,
                               stop_reason=reply.stop_reason, usage=pricing.four(reply.usage),
                               usd=round(cost, 6))
            text = text_of(reply.content)
            if text:
                self.last_text = text
            ended = self._after(reply)
            if ended is not None:
                return ended

    def _after(self, reply) -> int | None:
        stop = reply.stop_reason
        uses = [block for block in reply.content if isinstance(block, dict) and block.get("type") == "tool_use"]
        if stop == "tool_use":
            self._append({"role": "user", "content": [self._result(block) for block in uses]})
            return None
        if stop == "pause_turn":
            return None
        if stop in ("end_turn", "stop_sequence"):
            return self.finish("done")
        if stop == "max_tokens":
            if uses:
                self._append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block.get("id"), "content": UNRUN_TEXT,
                     "is_error": True} for block in uses]})
            else:
                self._append({"role": "user", "content": [{"type": "text", "text": CONTINUE_TEXT}]})
            return None
        if stop == "refusal":
            self._error(None, "refusal", "the model declined to continue (stop_reason refusal)")
        else:
            self._error(None, "stop_reason", f"the response stopped for an unhandled reason: {stop}")
        return self.finish("error")

    def _result(self, block: dict) -> dict:
        try:
            outcome = self.run_tool(block)
        except Exception as exc:  # noqa: BLE001 - a broken tool is the model's error to read
            outcome = ToolOutcome(f"The harness could not run this tool: {type(exc).__name__}", True, None,
                                  False, None)
        self.records.event("tool", name=block.get("name"), rc=outcome.rc, denied=outcome.denied,
                           reason=self.redact(outcome.reason) if outcome.reason else None)
        return {"type": "tool_result", "tool_use_id": block.get("id"),
                "content": self.redact(outcome.content) or "(no output)", "is_error": outcome.is_error}

    def _failed(self, failure: Exception) -> int | None:
        kind = getattr(failure, "kind", "fatal")
        partial = getattr(failure, "partial_usage", None)
        if partial:
            self.usd += self._price(None, partial)
        status = getattr(failure, "status", None)
        error_type = getattr(failure, "error_type", type(failure).__name__)
        message = getattr(failure, "message", None) or type(failure).__name__
        self._error(status, error_type, message)
        if kind == "credit":
            return self._credit_check()
        if kind == "workspace-limit":
            self.records.event("credit", verdict="workspace-limit", check_status=None)
            return self.finish("workspace-limit")
        if kind == "retryable" and self.retries < RETRIES:
            self.sleep(BACKOFF_S[min(self.retries, len(BACKOFF_S) - 1)])
            self.retries += 1
            return None
        return self.finish("error")

    def _credit_check(self) -> int:
        check = self.api.credit_check()
        if check.usage:
            self.usd += pricing.usd("claude-haiku-5-5", check.usage)
        if check.verdict == "credit":
            self.records.event("credit", verdict="exhausted", check_status=check.status)
            return self.finish("credit")
        if check.verdict == "ok":
            self.records.event("credit", verdict="refused-by-kind", check_status=check.status)
            return self.finish("refused-by-kind")
        failure = check.failure
        self._error(check.status, getattr(failure, "error_type", "credit_check"),
                    "the neutral credit check was inconclusive: "
                    + (getattr(failure, "message", "") or "no answer"))
        return self.finish("error")


def resume_history(history: list[dict]) -> list[dict]:
    """The user message a resumed conversation needs before its next request, if any."""
    if not history:
        return []
    last = history[-1]
    if last.get("role") != "assistant":
        return []
    content = last.get("content")
    uses = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"] \
        if isinstance(content, list) else []
    if uses:
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": block.get("id"), "content": UNKNOWN_TEXT, "is_error": True}
            for block in uses]}]
    return [{"role": "user", "content": [{"type": "text", "text": RESUMED_TEXT}]}]
