"""Pure parser for Claude Code `--output-format stream-json --verbose` output.

Nothing here reads the filesystem, spawns a process, or holds a secret: it takes
text and returns a typed summary the adapter classifies against. Everything is
tolerant — an unknown event type, a row that is not an object, a line that is not
JSON, and a truncated final line are all recorded rather than raised, because a
killed or disconnected lane is exactly the case we most need to classify (C-9.5).

The event shapes are the ones the installed Claude Code 2.1.260 binary validates
its own output against (its embedded schema definitions, read 2026-09-05):

    system/init        {type, subtype:"init", session_id, model, cwd, tools,
                        mcp_servers, permissionMode, apiKeySource, output_style,
                        claude_code_version, slash_commands, skills, plugins, uuid}
    assistant          {type:"assistant", message:<API message>, parent_tool_use_id,
                        error?:<error kind>, uuid, session_id, request_id?}
    rate_limit_event   {type:"rate_limit_event", rate_limit_info:<see RateLimitInfo>,
                        uuid, session_id}
    result (success)   {type:"result", subtype:"success", is_error,
                        api_error_status?, num_turns, result, stop_reason,
                        duration_ms, duration_api_ms, total_cost_usd, usage,
                        modelUsage, permission_denials, uuid, session_id}
    result (error)     {type:"result", subtype:"error_during_execution" |
                        "error_max_turns" | "error_max_budget_usd" |
                        "error_max_structured_output_retries", is_error, errors:[str],
                        num_turns, stop_reason, ... , uuid, session_id}
    system/api_retry   {type:"system", subtype:"api_retry", attempt, max_retries,
                        retry_delay_ms, error_status, error, uuid, session_id}

`error` on an assistant or api_retry frame is one of a closed set the adapter maps
straight onto outcome classes:

    authentication_failed, oauth_org_not_allowed, account_on_hold, billing_error,
    rate_limit, overloaded, invalid_request, model_not_found, server_error,
    unknown, max_output_tokens
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# The closed `error` enum Claude Code stamps on assistant / api_retry frames.
ERROR_KINDS = (
    "authentication_failed",
    "oauth_org_not_allowed",
    "account_on_hold",
    "billing_error",
    "rate_limit",
    "overloaded",
    "invalid_request",
    "model_not_found",
    "server_error",
    "unknown",
    "max_output_tokens",
)

#: `error` values that name a credential or organisation problem (C-9.3).
AUTH_ERROR_KINDS = ("authentication_failed", "oauth_org_not_allowed", "account_on_hold")

#: `error` values that name a retryable server-side condition (C-9.5).
TRANSIENT_ERROR_KINDS = ("overloaded", "server_error", "rate_limit")

#: The window keys `unifiedWindows` may carry. Kept verbatim as reading windows;
#: Claude reports them by name, so nothing here classifies by slot position.
WINDOW_KEYS = ("five_hour", "seven_day", "seven_day_overage_included")

#: `rate_limit_info.status` values.
ADMISSION_ALLOWED = ("allowed", "allowed_warning")
ADMISSION_REJECTED = "rejected"


def is_synthetic_api_error(row: Any) -> bool:
    """Recognize Claude Code's local error placeholder, not a served model.

    Stream events use snake_case and saved transcripts use camelCase for the
    marker. Require the exact sentinel, an explicit API-error marker, a known
    error kind, text-only content and zero usage. Any other model or unfamiliar
    shape remains model evidence and must still fail closed at attestation.
    This predicate never removes the frame's error or text from classification.
    """
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return False
    if (row.get("is_api_error_message") is not True and
            row.get("isApiErrorMessage") is not True):
        return False
    message = row.get("message")
    if (row.get("error") not in ERROR_KINDS or not isinstance(message, dict) or
            message.get("model") != "<synthetic>" or message.get("role") != "assistant" or
            message.get("type") != "message"):
        return False
    usage, content = message.get("usage"), message.get("content")
    token_fields = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    if not isinstance(usage, dict) or not all(type(usage.get(key)) is int and usage[key] == 0
                                            for key in token_fields):
        return False

    def zero_details(value: Any) -> bool:
        return isinstance(value, dict) and all(
            (type(count) is int and count == 0) or zero_details(count)
            for count in value.values())

    # Aggregate zeros cannot override conflicting detailed work counters. These
    # optional detail objects may be absent/null, but supplied counters must be
    # integer zero, including any new counters within the known detail objects.
    for key in ("cache_creation", "server_tool_use", "input_tokens_details", "output_tokens_details"):
        details = usage.get(key)
        if details is not None and not zero_details(details):
            return False
    return isinstance(content, list) and bool(content) and all(
        isinstance(block, dict) and block.get("type") == "text" and
        isinstance(block.get("text"), str) for block in content)


@dataclass(frozen=True)
class InitEvent:
    """`system/init`. Its mere presence proves the credential authenticated (C-9.3)."""

    session_id: str | None = None
    model: str | None = None
    cwd: str | None = None
    claude_code_version: str | None = None
    permission_mode: str | None = None
    api_key_source: str | None = None
    tools: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AssistantMessage:
    """One `assistant` frame. `model` is what the server said served it."""

    model: str | None = None
    text: str = ""
    stop_reason: str | None = None
    session_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Window:
    """One `unifiedWindows` entry. `utilization` is a fraction, never a percentage;
    it can legitimately exceed 1 when usage runs past a window's cap."""

    utilization: float | None = None
    resets_at: int | None = None   # unix epoch seconds


@dataclass(frozen=True)
class RateLimitInfo:
    """`rate_limit_event.rate_limit_info` (C-9.8).

    `overage_*` is parsed here so the adapter can record it, and is never read as
    admission evidence: `status` alone answers admission.
    """

    status: str | None = None
    resets_at: int | None = None
    rate_limit_type: str | None = None
    utilization: float | None = None
    windows: dict[str, Window] = field(default_factory=dict)
    overage_status: str | None = None
    overage_resets_at: int | None = None
    overage_disabled_reason: str | None = None
    is_using_overage: bool | None = None
    error_code: str | None = None
    can_user_purchase_credits: bool | None = None
    has_chargeable_saved_payment_method: bool | None = None
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def rejected(self) -> bool:
        return self.status == ADMISSION_REJECTED

    @property
    def allowed(self) -> bool:
        return self.status in ADMISSION_ALLOWED


@dataclass(frozen=True)
class ResultEvent:
    """The terminal `result` frame, in either its success or its error shape."""

    subtype: str | None = None
    is_error: bool | None = None
    text: str = ""                  # `result` on the success shape
    errors: tuple[str, ...] = ()    # `errors` on the error shape
    num_turns: int | None = None
    duration_ms: int | None = None
    api_error_status: int | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ApiRetry:
    """`system/api_retry`: a retryable API failure the CLI absorbed."""

    attempt: int | None = None
    max_retries: int | None = None
    retry_delay_ms: int | None = None
    error_status: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class StreamSummary:
    """Everything the adapter needs from one attempt's raw stream."""

    init: InitEvent | None = None
    assistants: tuple[AssistantMessage, ...] = ()
    rate_limits: tuple[RateLimitInfo, ...] = ()
    result: ResultEvent | None = None
    api_retries: tuple[ApiRetry, ...] = ()
    session_id: str | None = None
    lines_total: int = 0
    lines_parsed: int = 0
    bad_lines: int = 0
    truncated_tail: bool = False
    unknown_types: tuple[str, ...] = ()

    # --- convenience the classifier leans on --------------------------------

    @property
    def rate_limit(self) -> RateLimitInfo | None:
        """The last `rate_limit_event`; the CLI re-emits one whenever the numbers
        move, and the last one is the state at exit."""
        return self.rate_limits[-1] if self.rate_limits else None

    @property
    def has_init(self) -> bool:
        return self.init is not None

    @property
    def assistant_models(self) -> tuple[str, ...]:
        return tuple(a.model for a in self.assistants
                     if a.model and not is_synthetic_api_error(a.raw))

    @property
    def error_kinds(self) -> tuple[str, ...]:
        kinds = [a.error for a in self.assistants if a.error]
        kinds += [r.error for r in self.api_retries if r.error]
        return tuple(kinds)

    @property
    def final_text(self) -> str:
        """The stream's own rendering of the final message (C-12.6 fallback)."""
        if self.result is not None and self.result.text:
            return self.result.text
        for message in reversed(self.assistants):
            if message.text:
                return message.text
        return ""

    def texts(self) -> tuple[str, ...]:
        """Every human-readable string the stream carries, for text classification.
        Deliberately excludes the prompt: nothing here echoes what was sent."""
        out: list[str] = []
        for message in self.assistants:
            if message.text:
                out.append(message.text)
        if self.result is not None:
            if self.result.text:
                out.append(self.result.text)
            out.extend(self.result.errors)
        return tuple(out)


# --- parsing ----------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def message_text(message: Any) -> str:
    """Concatenate the `text` blocks of an API message's content, exactly.

    Nothing is re-rendered: this is the same extraction v1's `prefer_transcript_text`
    performs, so a deliverable taken from a transcript and one taken from a stream
    are the same bytes.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text") or ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def parse_rate_limit_info(info: Any) -> RateLimitInfo | None:
    """Turn a `rate_limit_info` object into the typed form (C-9.8)."""
    if not isinstance(info, dict):
        return None
    windows: dict[str, Window] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for key, value in unified.items():
            if not isinstance(value, dict):
                continue
            utilization = _as_float(value.get("utilization"))
            resets_at = _as_int(value.get("resetsAt"))
            if utilization is None and resets_at is None:
                continue
            windows[str(key)] = Window(utilization=utilization, resets_at=resets_at)
    return RateLimitInfo(
        status=_as_str(info.get("status")),
        resets_at=_as_int(info.get("resetsAt")),
        rate_limit_type=_as_str(info.get("rateLimitType")),
        utilization=_as_float(info.get("utilization")),
        windows=windows,
        overage_status=_as_str(info.get("overageStatus")),
        overage_resets_at=_as_int(info.get("overageResetsAt")),
        overage_disabled_reason=_as_str(info.get("overageDisabledReason")),
        is_using_overage=_as_bool(info.get("isUsingOverage")),
        error_code=_as_str(info.get("errorCode")),
        can_user_purchase_credits=_as_bool(info.get("canUserPurchaseCredits")),
        has_chargeable_saved_payment_method=_as_bool(
            info.get("hasChargeableSavedPaymentMethod")
        ),
        raw=info,
    )


def _rows(text: str) -> tuple[list[tuple[Any, bool]], int, int, bool]:
    """Split raw output into (row, ok) pairs.

    A whole-file JSON object is accepted as one row so an attempt captured with
    `--output-format json` (v1's shape) still parses. Only the *last* line failing
    to decode counts as a truncated tail; an interior failure is a bad line.
    """
    stripped = text.strip()
    if stripped.startswith("{") and "\n" in stripped:
        try:
            whole = json.loads(stripped)
        except ValueError:
            whole = None
        if isinstance(whole, dict):
            return [(whole, True)], 1, 1, False

    lines = [line for line in text.splitlines() if line.strip()]
    rows: list[tuple[Any, bool]] = []
    bad = 0
    truncated = False
    for index, line in enumerate(lines):
        try:
            rows.append((json.loads(line), True))
        except ValueError:
            if index == len(lines) - 1:
                truncated = True
            else:
                bad += 1
    return rows, len(lines), len(rows), truncated


def parse_stream(text: str) -> StreamSummary:
    """Parse one attempt's raw stream into a `StreamSummary`. Never raises."""
    rows, total, parsed, truncated = _rows(text or "")
    return _summarize((row for row, _ok in rows),
                      {"total": total, "parsed": parsed, "truncated": truncated})


def _summarize(rows: Iterable[Any], counts: dict[str, Any]) -> StreamSummary:
    """Consume decoded events without retaining unrelated tool/user payloads."""

    init: InitEvent | None = None
    assistants: list[AssistantMessage] = []
    rate_limits: list[RateLimitInfo] = []
    result: ResultEvent | None = None
    retries: list[ApiRetry] = []
    unknown: list[str] = []
    bad = 0
    session_id: str | None = None

    for row in rows:
        if not isinstance(row, dict):
            bad += 1
            continue
        session_id = session_id or _as_str(row.get("session_id"))
        kind = row.get("type")

        if kind == "system":
            subtype = row.get("subtype")
            if subtype == "init":
                if init is None:
                    init = InitEvent(
                        session_id=_as_str(row.get("session_id")),
                        model=_as_str(row.get("model")),
                        cwd=_as_str(row.get("cwd")),
                        claude_code_version=_as_str(row.get("claude_code_version")),
                        permission_mode=_as_str(row.get("permissionMode")),
                        api_key_source=_as_str(row.get("apiKeySource")),
                        tools=tuple(
                            t for t in (row.get("tools") or []) if isinstance(t, str)
                        ),
                        raw=row,
                    )
            elif subtype == "api_retry":
                retries.append(
                    ApiRetry(
                        attempt=_as_int(row.get("attempt")),
                        max_retries=_as_int(row.get("max_retries")),
                        retry_delay_ms=_as_int(row.get("retry_delay_ms")),
                        error_status=_as_int(row.get("error_status")),
                        error=_as_str(row.get("error")),
                    )
                )
            elif isinstance(subtype, str):
                unknown.append(f"system/{subtype}")

        elif kind == "assistant":
            message = row.get("message")
            model = None
            stop_reason = None
            if isinstance(message, dict):
                model = _as_str(message.get("model"))
                stop_reason = _as_str(message.get("stop_reason"))
            assistants.append(
                AssistantMessage(
                    model=model,
                    text=message_text(message),
                    stop_reason=stop_reason,
                    session_id=_as_str(row.get("session_id")),
                    error=_as_str(row.get("error")),
                    raw=row,
                )
            )

        elif kind == "rate_limit_event":
            info = parse_rate_limit_info(row.get("rate_limit_info"))
            if info is not None:
                rate_limits.append(
                    replace(info, session_id=_as_str(row.get("session_id")))
                )

        elif kind == "result":
            errors = row.get("errors")
            result = ResultEvent(
                subtype=_as_str(row.get("subtype")),
                is_error=_as_bool(row.get("is_error")),
                text=row.get("result") if isinstance(row.get("result"), str) else "",
                errors=tuple(e for e in (errors or []) if isinstance(e, str)),
                num_turns=_as_int(row.get("num_turns")),
                duration_ms=_as_int(row.get("duration_ms")),
                api_error_status=_as_int(row.get("api_error_status")),
                stop_reason=_as_str(row.get("stop_reason")),
                session_id=_as_str(row.get("session_id")),
                raw=row,
            )

        elif isinstance(kind, str):
            unknown.append(kind)
        else:
            bad += 1

    return StreamSummary(
        init=init,
        assistants=tuple(assistants),
        rate_limits=tuple(rate_limits),
        result=result,
        api_retries=tuple(retries),
        session_id=session_id,
        lines_total=counts["total"],
        lines_parsed=counts["parsed"],
        bad_lines=max(0, bad + counts["total"] - counts["parsed"] - (1 if counts["truncated"] else 0)),
        truncated_tail=counts["truncated"],
        unknown_types=tuple(dict.fromkeys(unknown)),
    )


def parse_lines(lines: Iterable[str]) -> StreamSummary:
    """Read JSONL through EOF without materializing the complete raw stream.

    Every assistant and provider verdict is retained. Large unrelated tool/user
    frames are released after their line is parsed. A legacy pretty-printed JSON
    object keeps the whole-object compatibility of `parse_stream`.
    """
    from itertools import chain

    iterator = iter(lines)
    first = next((line for line in iterator if line.strip()), "")
    if first.lstrip().startswith("{"):
        try:
            json.loads(first)
        except ValueError:
            # Legacy whole-object JSON may put fields before its first newline.
            # Keep its established decoder/fallback rather than misread it as
            # truncated JSONL. Ordinary complete JSONL rows stay incremental.
            return parse_stream("\n".join(chain((first,), iterator)))
    counts = {"total": 0, "parsed": 0, "truncated": False}

    def decoded():
        for line in chain((first,), iterator):
            if not line.strip():
                continue
            counts["total"] += 1
            counts["truncated"] = False
            try:
                row = json.loads(line)
            except ValueError:
                counts["truncated"] = True
                continue
            counts["parsed"] += 1
            yield row

    return _summarize(decoded(), counts)
