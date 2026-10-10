"""Provider-reported token usage, with no guessed counters (C-18.5).

Missing counters stay null. Accepted counters are nonnegative integers, and
where prompt is known, 0 <= cache_read <= prompt and cache_write <= prompt;
inconsistent records are refused, never clamped. Claude result usage covers
disjoint steer segments, while modelUsage is cumulative and is never summed.
Codex exec resumes expose thread totals and are labelled cumulative_thread.
Duplicate Claude message ids are one request: compatible partial counters merge
independently of duplicate order, and conflicting counters refuse the record.
No provider-reported usage means no record. Everything here is pure and stable.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

CLAUDE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
MODEL_FIELDS = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens", "thinkingTokens")
TTL_FIELDS = ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens")
CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")
APP_FIELDS = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")


class _Refused(ValueError):
    """C-18.5: malformed or contradictory provider counters are not evidence."""


def _counters(value: Any, fields: tuple[str, ...]) -> dict[str, int | None]:
    if value is not None and not isinstance(value, dict):
        raise _Refused("usage counters must be an object")
    source = value if isinstance(value, dict) else {}
    result = {}
    for key in fields:
        number = source.get(key)
        if number is not None and (type(number) is not int or number < 0):
            raise _Refused(key)
        result[key] = number
    return result


def _sum(values: Iterable[int | None]) -> int | None:
    values = tuple(values)
    return sum(values) if values and all(value is not None for value in values) else None


def _normalized(prompt: int | None, read: int | None, write: int | None,
                output: int | None, ttl: str | None = None) -> dict[str, Any]:
    if prompt is not None and any(value is not None and value > prompt for value in (read, write)):
        raise _Refused("cache counters exceed prompt")
    return {"prompt": prompt, "cache_read": read, "cache_write": write, "output": output,
            "cache_ttl": ttl, "cache_hit_share": read / prompt if prompt and read is not None else None}


def _claude_usage(value: Any) -> dict[str, Any]:
    result = _counters(value, CLAUDE_FIELDS)
    result["cache_creation"] = _counters(value.get("cache_creation") if isinstance(value, dict) else None,
                                          TTL_FIELDS)
    _normalized(_sum(result[key] for key in CLAUDE_FIELDS[:3]),
                result["cache_read_input_tokens"], result["cache_creation_input_tokens"], result["output_tokens"])
    return result


def _has_counters(value: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return any(value.get(key) is not None for key in fields)


def _ttl(usage: dict[str, Any]) -> str | None:
    hour, minute = (usage["cache_creation"][key] for key in TTL_FIELDS)
    if hour is None or minute is None:
        return None
    return "mixed" if hour and minute else "1h" if hour else "5m" if minute else None


def _claude(rows: Iterable[dict]) -> dict | None:
    results: list[dict] = []
    requests: dict[str, dict[str, int | None]] = {}
    request_usage: dict[str, dict[str, Any]] = {}
    result_ids: dict[str, dict] = {}
    for row in rows:
        if row.get("type") == "result":
            usage = _claude_usage(row.get("usage"))
            models = row.get("modelUsage")
            if models is not None:
                if not isinstance(models, dict):
                    raise _Refused("modelUsage must be an object")
                for counters in models.values():
                    _counters(counters, MODEL_FIELDS)
            raw = {"usage": usage, "modelUsage": models}
            rid = row.get("uuid")
            if isinstance(rid, str):
                if rid in result_ids:
                    if raw != result_ids[rid]:
                        raise _Refused("conflicting result id")
                    continue
                result_ids[rid] = raw
            results.append(raw)
        elif row.get("type") == "assistant" and row.get("parent_tool_use_id") is None:
            message = row.get("message")
            if not isinstance(message, dict) or message.get("model") == "<synthetic>":
                continue
            mid = message.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            usage = _claude_usage(message.get("usage"))
            request = {"input": usage["input_tokens"], "cache_read": usage["cache_read_input_tokens"],
                       "cache_write": usage["cache_creation_input_tokens"]}
            # First appearance orders requests even when that block's counters
            # arrive only in a later same-id assistant frame (C-18.5).
            previous = requests.setdefault(mid, dict(request))
            for key, value in request.items():
                if value is not None and previous[key] is not None and previous[key] != value:
                    raise _Refused("conflicting message id")
                if value is not None:
                    previous[key] = value
            reported = request_usage.setdefault(mid, usage)
            for source, target, fields in ((usage, reported, CLAUDE_FIELDS),
                                           (usage["cache_creation"], reported["cache_creation"], TTL_FIELDS)):
                for key in fields:
                    value = source[key]
                    if value is not None and target[key] is not None and target[key] != value:
                        raise _Refused("conflicting message usage")
                    if value is not None:
                        target[key] = value
    result_reported = any(_has_counters(result["usage"], CLAUDE_FIELDS)
                          or _has_counters(result["usage"]["cache_creation"], TTL_FIELDS)
                          or any(_has_counters(_counters(counters, MODEL_FIELDS), MODEL_FIELDS)
                                 for counters in (result["modelUsage"] or {}).values()) for result in results)
    if not result_reported and not any(_has_counters(usage, CLAUDE_FIELDS) or _has_counters(usage["cache_creation"], TTL_FIELDS)
                                       for usage in request_usage.values()):
        return None
    aggregate = {key: _sum(result["usage"][key] for result in results) for key in CLAUDE_FIELDS}
    aggregate["cache_creation"] = {key: _sum(result["usage"]["cache_creation"][key] for result in results)
                                    for key in TTL_FIELDS}
    # Final modelUsage may include preceding conversation history: keep it raw.
    raw = {"usage": aggregate, "modelUsage": results[-1]["modelUsage"] if results else None}
    if request_usage:
        raw["requests"] = dict(sorted(request_usage.items()))
    if len(results) > 1:
        raw["results"] = results
    values = list(requests.values())
    for request in values:
        _normalized(_sum(request.values()), request["cache_read"], request["cache_write"], None)
    return {"provider": "claude", "raw": raw,
            "normalized": _normalized(_sum(aggregate[key] for key in CLAUDE_FIELDS[:3]),
                                      aggregate["cache_read_input_tokens"], aggregate["cache_creation_input_tokens"],
                                      aggregate["output_tokens"], _ttl(aggregate)),
            "first_request": values[0] if values else None, "last_request": values[-1] if values else None,
            "cumulative_thread": False}


def _codex(rows: list[dict], resumed: bool, turn_id: str | None) -> dict | None:
    notifications = [row for row in rows if row.get("method") == "thread/tokenUsage/updated"]
    if notifications:
        if turn_id is None:
            # A turn's own acknowledgement supplies the id for retained streams.
            ids = {row["params"]["turn"]["id"] for row in rows
                   if row.get("method") == "turn/started" and isinstance(row.get("params"), dict)
                   and isinstance(row["params"].get("turn"), dict)
                   and isinstance(row["params"]["turn"].get("id"), str) and row["params"]["turn"]["id"]}
            ids.discard(None)
            if len(ids) != 1:
                return None
            turn_id = ids.pop()
        selected = [row for row in notifications if isinstance(row.get("params"), dict)
                    and row["params"].get("turnId") == turn_id]
        if not selected:
            return None
        thread_ids = {row["params"]["threadId"] for row in selected
                      if isinstance(row["params"].get("threadId"), str) and row["params"]["threadId"]}
        if len(thread_ids) > 1:
            raise _Refused("conflicting turn thread ids")
        own_thread = next(iter(thread_ids)) if thread_ids else None
        # `last` is one request, not the whole turn. Restored threads can begin
        # with historical totals; keep that scope instead of subtracting a guess.
        historical = False
        own_first = None
        for row in notifications:
            params = row.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("tokenUsage"), dict):
                continue
            # A foreign thread cannot establish this turn's historical usage.
            # Missing ids leave scope uncertain; never manufacture an identity.
            prior_thread = params.get("threadId")
            if own_thread is not None and isinstance(prior_thread, str) and prior_thread and prior_thread != own_thread:
                continue
            if params.get("turnId") == turn_id:
                first = params["tokenUsage"]
                total_first, last_first = _counters(first.get("total"), APP_FIELDS), _counters(first.get("last"), APP_FIELDS)
                for usage in (total_first, last_first):
                    _normalized(usage["inputTokens"], usage["cachedInputTokens"], usage["cacheWriteInputTokens"], usage["outputTokens"])
                if own_first is None:
                    own_first = (total_first, last_first)
                continue
            if own_first is None:
                prior = _counters(params["tokenUsage"].get("total"), APP_FIELDS)
                historical = historical or any(value is not None and value > 0 for value in prior.values())
        token = selected[-1]["params"].get("tokenUsage")
        if not isinstance(token, dict):
            return None
        total, last = _counters(token.get("total"), APP_FIELDS), _counters(token.get("last"), APP_FIELDS)
        for usage in (total, last):
            _normalized(usage["inputTokens"], usage["cachedInputTokens"], usage["cacheWriteInputTokens"], usage["outputTokens"])
        if not _has_counters(total, APP_FIELDS) and not _has_counters(last, APP_FIELDS):
            return None
        first_total, first_last = own_first
        comparable = [key for key in APP_FIELDS if first_total[key] is not None and first_last[key] is not None]
        cumulative = historical or any(first_total[key] != first_last[key] for key in comparable)
        if not all(key in comparable for key in ("inputTokens", "outputTokens")):
            cumulative = True
        return {"provider": "codex", "raw": {"total": total, "last": last,
                                                "modelContextWindow": token.get("modelContextWindow")},
                "normalized": _normalized(total["inputTokens"], total["cachedInputTokens"],
                                          total["cacheWriteInputTokens"], total["outputTokens"]),
                "cumulative_thread": cumulative}
    usage = None
    for row in rows:
        if row.get("type") != "turn.completed":
            continue
        candidate = _counters(row.get("usage"), CODEX_FIELDS)
        _normalized(candidate["input_tokens"], candidate["cached_input_tokens"],
                    candidate["cache_write_input_tokens"], candidate["output_tokens"])
        if _has_counters(candidate, CODEX_FIELDS):
            usage = candidate
    if usage is None:
        return None
    return {"provider": "codex", "raw": {"usage": usage},
            "normalized": _normalized(usage["input_tokens"], usage["cached_input_tokens"],
                                      usage["cache_write_input_tokens"], usage["output_tokens"]),
            "cumulative_thread": resumed}


def parse_usage(lines: Iterable[str], provider: str, *, resumed: bool = False,
                turn_id: str | None = None) -> dict | None:
    """C-18.5: parse fixture/artifact lines without I/O, raising or estimates.

    Share is null for unknown/zero prompt or unknown cache reads, otherwise in
    [0, 1]. Invalid counters or contradictory duplicates refuse the whole record.
    Claude request order is the order of distinct message ids' first appearance;
    the order of compatible duplicate frames never changes their counters.
    """
    if turn_id is not None and not isinstance(turn_id, str):
        return None
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind == "assistant":
            message = row.get("message")
            if isinstance(message, dict):
                rows.append({"type": kind, "parent_tool_use_id": row.get("parent_tool_use_id"),
                             "message": {key: message.get(key) for key in ("id", "model", "usage")}})
        elif kind in ("result", "turn.completed"):
            rows.append({key: row.get(key) for key in ("type", "uuid", "usage", "modelUsage")})
        elif row.get("method") in ("thread/tokenUsage/updated", "turn/started"):
            rows.append({"method": row["method"], "params": row.get("params")})
    try:
        if provider == "claude":
            return _claude(rows)
        if provider == "codex":
            return _codex(rows, resumed, turn_id)
    except _Refused:
        return None
    return None
