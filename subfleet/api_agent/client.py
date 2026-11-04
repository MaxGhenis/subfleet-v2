"""The one module in Subfleet that imports `anthropic` (C-31.7, C-31.11).

`harness.main` imports it only after the key and every other `ANTHROPIC_*` variable
have left `os.environ`, since the SDK reads `ANTHROPIC_LOG` when it is imported and
`ANTHROPIC_CUSTOM_HEADERS` when a client is built. The client gets its key and its
endpoint explicitly, which turns off the SDK's own credential and base-URL lookups,
and no request header ever leaves this module.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import anthropic
import httpx2

from subfleet.api_agent.cli import TEST_URL_ENV  # noqa: F401 - re-exported for tests
from subfleet.home_paths import under_pytest

ENDPOINT = "https://api.anthropic.com"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
COMPACTION_BETA = "compact-2026-01-12"
COMPACTION = {"edits": [{"type": "compact_20260112"}]}
MAX_TOKENS = 64_000
SDK_RETRIES = 2
CREDIT_CHECK_MODEL = "claude-haiku-5-5"
CREDIT_TEXT = "credit balance is too low"
WORKSPACE_TEXT = "you have reached your specified workspace api usage limits"
#: Mid-stream errors the SDK does not retry, which the loop retries itself.
RETRYABLE_TYPES = frozenset({"overloaded_error", "api_error"})
MESSAGE_CAP = 2_000

for _name in ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def endpoint(test_url: str | None, environ: Mapping[str, str] | None = None) -> str:
    """`https://api.anthropic.com`, unless a pytest session names a loopback fake."""
    if not test_url or not under_pytest(environ):
        return ENDPOINT
    try:
        parts = urlsplit(test_url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return ENDPOINT
    if parts.scheme not in ("http", "https") or host not in LOOPBACK or parts.username or parts.password:
        return ENDPOINT
    return test_url


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


@dataclass
class Reply:
    """One completed response."""
    content: list[dict]
    stop_reason: str | None
    model: str | None
    request_id: str | None
    org_id: str | None
    usage: dict


@dataclass
class Failure(Exception):
    """A request that ended without a response.

    `kind` is `credit` (a 400 whose message says the credit balance is too low),
    `workspace-limit`, `retryable` (a mid-stream overload or API error, or a broken
    connection) or `fatal`. `partial_usage` is what a stream reported before it broke.
    """
    status: int | None
    error_type: str
    message: str
    kind: str
    partial_usage: dict | None = field(default=None)

    def __str__(self) -> str:
        return f"{self.error_type} ({self.status}): {self.message}"


@dataclass
class CreditCheck:
    """The neutral request's answer: `credit` (refused the same way), `ok`, or
    `inconclusive`, with its HTTP status."""
    verdict: str
    status: int | None
    usage: dict | None = None
    failure: Failure | None = None


def _status_failure(exc: anthropic.APIStatusError, partial: dict | None = None) -> Failure:
    status = exc.status_code if isinstance(exc.status_code, int) and exc.status_code >= 400 else None
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    error_type = error.get("type") if isinstance(error.get("type"), str) else "api_status_error"
    message = error.get("message") if isinstance(error.get("message"), str) else exc.message
    text = _normal(message or "")
    if status == 400 and CREDIT_TEXT in text:
        kind = "credit"
    elif status is not None and status < 500 and WORKSPACE_TEXT in text:
        kind = "workspace-limit"
    elif status is None and error_type in RETRYABLE_TYPES:
        kind = "retryable"
    else:
        kind = "fatal"
    return Failure(status, error_type, (message or "")[:MESSAGE_CAP], kind, partial)


def _usage(message) -> dict:
    usage = getattr(message, "usage", None)
    return usage.to_dict(mode="json") if usage is not None else {}


class Api:
    """The harness's client: one key, one endpoint, no environment lookups."""

    def __init__(self, key: str, base_url: str) -> None:
        # trust_env=False: no .netrc and no environment CA bundle; the harness has
        # already removed the proxy variables the SDK's transport reads regardless.
        self._http = anthropic.DefaultHttpxClient(trust_env=False)
        self._client = anthropic.Anthropic(api_key=key, base_url=base_url, max_retries=SDK_RETRIES,
                                           http_client=self._http, open_telemetry=False)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - closing never decides an outcome
            pass

    def send(self, *, model: str, effort: str, system: list[dict], tools: list[dict],
             messages: list[dict]) -> Reply:
        """One streaming request; raises `Failure`."""
        stream = None
        try:
            with self._client.beta.messages.stream(
                    model=model, max_tokens=MAX_TOKENS, system=system, messages=messages, tools=tools,
                    thinking={"type": "adaptive"}, output_config={"effort": effort},
                    context_management=COMPACTION, cache_control={"type": "ephemeral"},
                    betas=[COMPACTION_BETA]) as stream:
                message = stream.get_final_message()
                headers = stream.response.headers
                return Reply(content=[block.to_dict(mode="json") for block in message.content],
                             stop_reason=message.stop_reason, model=message.model,
                             request_id=headers.get("request-id") or getattr(message, "_request_id", None),
                             org_id=headers.get("anthropic-organization-id"), usage=_usage(message))
        except anthropic.APIStatusError as exc:
            raise _status_failure(exc, self._partial(stream)) from None
        except anthropic.APIConnectionError as exc:
            kind = "retryable"
            raise Failure(None, "timeout" if isinstance(exc, anthropic.APITimeoutError) else "connection",
                          type(exc).__name__, kind, self._partial(stream)) from None
        except httpx2.HTTPError as exc:
            raise Failure(None, "connection", type(exc).__name__, "retryable", self._partial(stream)) from None
        except (ValueError, KeyError) as exc:  # a stream the SDK could not assemble
            raise Failure(None, "stream", type(exc).__name__, "fatal", self._partial(stream)) from None

    @staticmethod
    def _partial(stream) -> dict | None:
        if stream is None:
            return None
        try:
            snapshot = stream.current_message_snapshot
        except (AssertionError, AttributeError):
            return None
        return _usage(snapshot) if snapshot is not None else None

    def credit_check(self) -> CreditCheck:
        """The neutral request: Haiku 5.5, `max_tokens` 1, no system prompt, "ok"."""
        try:
            raw = self._client.messages.with_raw_response.create(
                model=CREDIT_CHECK_MODEL, max_tokens=1, messages=[{"role": "user", "content": "ok"}])
            message = raw.parse()
            return CreditCheck("ok", raw.status_code, _usage(message))
        except anthropic.APIStatusError as exc:
            failure = _status_failure(exc)
            if failure.kind == "credit":
                return CreditCheck("credit", failure.status, failure=failure)
            return CreditCheck("inconclusive", failure.status, failure=failure)
        except (anthropic.APIConnectionError, httpx2.HTTPError) as exc:
            return CreditCheck("inconclusive", None,
                               failure=Failure(None, "connection", type(exc).__name__, "fatal"))
