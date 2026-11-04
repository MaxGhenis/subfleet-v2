"""A local fake of the Messages API for the API credit lanes' harness (C-31.7).

A threaded HTTP server on 127.0.0.1 that answers `POST /v1/messages` from a script:
streamed replies as real server-sent events (message_start, content_block_start,
deltas, content_block_stop, message_delta, message_stop), JSON replies for
non-streaming requests, error replies, and errors that arrive mid-stream. It records
every request's path, headers and body, and sets `request-id` and
`anthropic-organization-id` on every response. It runs inside the test process and
stops with it; nothing here reaches the network.
"""
from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CREDIT_MESSAGE = ("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing "
                  "to upgrade or purchase credits.")
WORKSPACE_MESSAGE = ("You have reached your specified workspace API usage limits. You will regain access on "
                     "2026-12-01 at 00:00 UTC.")
DEFAULT_ORG = "11111111-2222-4333-8444-555555555555"


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def tool_use(use_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": use_id, "name": name, "input": tool_input}


def thinking(value: str, signature: str = "sig-fake-1") -> dict:
    return {"type": "thinking", "thinking": value, "signature": signature}


def bash(use_id: str, command: str) -> dict:
    return tool_use(use_id, "bash", {"command": command})


@dataclass
class Reply:
    """A model reply. `usage` merges over small default counts."""
    content: list[dict]
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)
    model: str | None = None


@dataclass
class Error:
    """An HTTP error before any event."""
    status: int
    message: str
    error_type: str = "invalid_request_error"


@dataclass
class StreamError:
    """A stream that starts, then sends `event: error`."""
    error_type: str = "overloaded_error"
    message: str = "Overloaded"


@dataclass
class Recorded:
    path: str
    headers: dict[str, str]
    body: dict


Script = Callable[[dict, int], "Reply | Error | StreamError"]


def _usage(extra: dict) -> dict:
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    usage.update(extra)
    return usage


def _sse(name: str, payload: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


def stream_body(reply: Reply, model: str, message_id: str) -> bytes:
    usage = _usage(reply.usage)
    start_usage = {key: usage[key] for key in ("input_tokens", "cache_read_input_tokens",
                                               "cache_creation_input_tokens")}
    start_usage["output_tokens"] = 1
    chunks = [_sse("message_start", {"type": "message_start", "message": {
        "id": message_id, "type": "message", "role": "assistant", "model": reply.model or model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": start_usage}})]
    for index, block in enumerate(reply.content):
        kind = block["type"]
        if kind == "text":
            chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": index,
                                                       "content_block": {"type": "text", "text": ""}}))
            for piece in (block["text"][:len(block["text"]) // 2], block["text"][len(block["text"]) // 2:]):
                if piece:
                    chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                               "delta": {"type": "text_delta", "text": piece}}))
        elif kind == "tool_use":
            chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": index,
                                                       "content_block": {"type": "tool_use", "id": block["id"],
                                                                         "name": block["name"], "input": {}}}))
            raw = json.dumps(block["input"])
            for piece in (raw[:len(raw) // 2], raw[len(raw) // 2:]):
                chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                           "delta": {"type": "input_json_delta",
                                                                     "partial_json": piece}}))
        elif kind == "thinking":
            chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": index,
                                                       "content_block": {"type": "thinking", "thinking": "",
                                                                         "signature": ""}}))
            chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                       "delta": {"type": "thinking_delta",
                                                                 "thinking": block["thinking"]}}))
            chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                       "delta": {"type": "signature_delta",
                                                                 "signature": block["signature"]}}))
        else:
            raise ValueError(f"the fake does not stream {kind} blocks")
        chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
    delta_usage = dict(usage)
    chunks.append(_sse("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": reply.stop_reason, "stop_sequence": None},
                                         "usage": delta_usage}))
    chunks.append(_sse("message_stop", {"type": "message_stop"}))
    return b"".join(chunks)


class FakeAnthropic:
    """`with FakeAnthropic(script) as api:` serves until the block ends.

    `script(body, index)` answers the index-th request (0-based); a list is taken in
    order, its last entry repeating. `org_id=None` sends no organization header.
    """

    def __init__(self, script: Script | list, *, org_id: str | None = DEFAULT_ORG) -> None:
        if isinstance(script, list):
            entries = list(script)

            def scripted(body: dict, index: int):
                return entries[min(index, len(entries) - 1)]

            self._script = scripted
        else:
            self._script = script
        self.org_id = org_id
        self.requests: list[Recorded] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def messages(self) -> list[Recorded]:
        """The model requests, without the neutral credit check."""
        return [request for request in self.requests if request.body.get("stream")]

    def __enter__(self) -> "FakeAnthropic":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                return

            def do_POST(self):  # noqa: N802 - http.server's naming
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw or b"{}")
                with fake._lock:
                    index = len(fake.requests)
                    fake.requests.append(Recorded(self.path, {k.lower(): v for k, v in self.headers.items()},
                                                  body))
                    number = next(fake._ids)
                answer = fake._script(body, index)
                request_id = f"req_fake_{number:04d}"
                if self.path.split("?")[0] != "/v1/messages":
                    answer = Error(404, f"no such path {self.path}", "not_found_error")
                if isinstance(answer, Error):
                    payload = json.dumps({"type": "error", "error": {"type": answer.error_type,
                                                                     "message": answer.message},
                                          "request_id": request_id}).encode()
                    self._send(answer.status, "application/json", payload, request_id)
                    return
                model = body.get("model", "claude-opus-5-5")
                if isinstance(answer, StreamError):
                    payload = (_sse("message_start", {"type": "message_start", "message": {
                        "id": f"msg_{number}", "type": "message", "role": "assistant", "model": model,
                        "content": [], "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 50, "output_tokens": 1}}})
                        + _sse("error", {"type": "error", "error": {"type": answer.error_type,
                                                                     "message": answer.message}}))
                    self._send(200, "text/event-stream", payload, request_id)
                    return
                if body.get("stream"):
                    self._send(200, "text/event-stream", stream_body(answer, model, f"msg_{number}"), request_id)
                    return
                payload = json.dumps({"id": f"msg_{number}", "type": "message", "role": "assistant",
                                      "model": answer.model or model, "content": answer.content,
                                      "stop_reason": answer.stop_reason, "stop_sequence": None,
                                      "usage": _usage(answer.usage)}).encode()
                self._send(200, "application/json", payload, request_id)

            def _send(self, status: int, kind: str, payload: bytes, request_id: str) -> None:
                self.send_response(status)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(payload)))
                self.send_header("request-id", request_id)
                if fake.org_id is not None:
                    self.send_header("anthropic-organization-id", fake.org_id)
                self.send_header("x-should-retry", "false")
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-anthropic", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)
