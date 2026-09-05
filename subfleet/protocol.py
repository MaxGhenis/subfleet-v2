"""Socket protocol between the CLI (and other clients) and the daemon.

Newline-delimited JSON over a unix socket, one request line then one response
line (C-16). This module defines the wire shapes and the encode/decode
helpers; it has no I/O so both the client and the server import it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any

from .contracts import Exit

PROTOCOL_VERSION = 1

OPS = (
    "submit", "list", "show", "wait", "kill", "lanes", "readings", "why",
    "notice.pending", "notice.ack", "ping", "daemon.status",
)


class ProtocolError(Exception):
    """Raised on a malformed line; carries the exit code the CLI should use."""

    def __init__(self, message: str, code: Exit = Exit.INVALID_INPUT, fix: str | None = None):
        super().__init__(message)
        self.code = code
        self.fix = fix


@dataclass
class Request:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    v: int = PROTOCOL_VERSION


@dataclass
class ErrorBody:
    code: int
    message: str
    fix: str | None = None


@dataclass
class Response:
    id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None
    v: int = PROTOCOL_VERSION


# --- Argument shapes per op (C-16.2). Unknown keys are ignored; missing
# required keys are Exit.INVALID_INPUT. The daemon validates with these.

@dataclass
class SubmitArgs:
    request_id: str
    kind: str
    workdir: str
    prompt_path: str
    sandbox: str
    task: str | None = None
    tier: str | None = None
    pinned_model: str | None = None
    pinned_lane: str | None = None
    out_path: str | None = None
    name: str | None = None
    exclusions: list[str] = field(default_factory=list)
    allow_desktop: bool = False
    allow_tmp: bool = False
    in_place: bool = False
    independent: bool = False
    parent_job_id: str | None = None
    caller_session: str | None = None
    caller_pid: int | None = None
    no_preamble: bool = False
    dry_run: bool = False
    max_attempts: int | None = None
    max_wall_s: int | None = None
    max_tokens_observed: int | None = None


@dataclass
class ListArgs:
    mine: str | None = None        # caller session id; None lists all
    running: bool = False
    last: int | None = None


@dataclass
class ShowArgs:
    job_id: str


@dataclass
class WaitArgs:
    job_ids: list[str] = field(default_factory=list)
    mine: str | None = None
    last: bool = False
    deadline_s: int = 60            # server-side cap is WAIT_POLL_MAX_S (C-15.4)


@dataclass
class KillArgs:
    job_id: str
    confirm_dead: bool = False
    force_release: bool = False
    operator_note: str | None = None


@dataclass
class WhyArgs:
    job_id: str | None = None
    task: str | None = None
    tier: str | None = None
    pinned_model: str | None = None
    exclusions: list[str] = field(default_factory=list)
    allow_desktop: bool = False


@dataclass
class NoticeArgs:
    session_id: str
    notice_ids: list[int] = field(default_factory=list)   # for ack


# --- Encoding -----------------------------------------------------------------

def encode(obj: Any) -> bytes:
    """One JSON line, UTF-8, trailing newline."""
    if is_dataclass(obj):
        obj = asdict(obj)
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_request(line: bytes | str) -> Request:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"malformed request: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("request must be a JSON object")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {data.get('v')!r}; this daemon speaks {PROTOCOL_VERSION}")
    op = data.get("op")
    if op not in OPS:
        raise ProtocolError(f"unknown op {op!r}")
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise ProtocolError("args must be an object")
    return Request(op=op, args=args, id=str(data.get("id", "")))


def decode_response(line: bytes | str) -> Response:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"malformed response: {exc}", Exit.OPERATIONAL) from exc
    err = data.get("error")
    return Response(
        id=str(data.get("id", "")),
        ok=bool(data.get("ok")),
        result=data.get("result"),
        error=ErrorBody(**{k: err.get(k) for k in ("code", "message", "fix")}) if err else None,
        v=int(data.get("v", PROTOCOL_VERSION)),
    )


def coerce_args(cls: type, args: dict[str, Any]) -> Any:
    """Build an args dataclass, ignoring unknown keys and reporting missing ones."""
    names = {f.name for f in fields(cls)}
    known = {k: v for k, v in args.items() if k in names}
    try:
        return cls(**known)
    except TypeError as exc:
        raise ProtocolError(f"invalid arguments for {cls.__name__}: {exc}") from exc


def ok(req_id: str, result: dict[str, Any]) -> Response:
    return Response(id=req_id, ok=True, result=result)


def fail(req_id: str, code: Exit | int, message: str, fix: str | None = None) -> Response:
    return Response(id=req_id, ok=False, error=ErrorBody(code=int(code), message=message, fix=fix))
