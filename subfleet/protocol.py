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
    "notice.pending", "notice.ack", "notice.mark", "ping", "daemon.status",
    "gate.start", "gate.poll", "gate.continue",
    "sessions", "pick", "operations",
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
    isolated_review: bool = False
    review_root: str | None = None
    round_lease: str | None = None
    unmeasured_reserve_reason: str | None = None
    # C-11.2, C-17.2: the provider the pin's flag names, `claude` for `-a` and
    # `codex` for `-H`. It narrows a name both providers answer to; a lane id
    # says its own provider. Not stored and not part of the request digest.
    pinned_provider: str | None = None
    # C-17.7: {"id", "label", "index", "size"} for a job submitted by `run --batch`.
    # A label for people and the app, never an input to routing or the digest.
    batch: dict | None = None


@dataclass
class GateStartArgs:
    """C-17.1/C-23.8: caller-approved exact revision for a new gate."""
    gate_command: str
    target: str
    peer: str
    main_approve: bool = False
    expect_head: str | None = None
    expect_base: str | None = None
    expect_sha256: str | None = None
    workdir: str | None = None
    brief: str | None = None
    peer_account: str | None = None
    exclude_account: list[str] = field(default_factory=list)
    max_rounds: int | None = None
    on_agreement: str = "proceed"
    merge_method: str | None = "merge"
    main_model: str | None = None
    dry_run: bool = False


@dataclass
class GateContinueArgs:
    """C-17.1/C-23.8: fresh main approval or reconciliation of an existing gate."""
    gate_id: str
    main_approve: bool = False
    expect_head: str | None = None
    expect_base: str | None = None
    expect_sha256: str | None = None
    response: str | None = None
    peer_account: str | None = None
    exclude_account: list[str] = field(default_factory=list)
    max_rounds: int | None = None
    dry_run: bool = False
    gate_command: str = "continue"


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


@dataclass
class NoticeMarkArgs:
    """`notice.mark` (C-15.3): the non-terminal halves of the delivery ladder.

    A separate shape from `NoticeArgs` on purpose. The CLI builds its
    `notice.ack` payload by `asdict`-ing `NoticeArgs`, so a field added there
    would appear on `notice.ack`'s wire line and change an op that already
    ships. `notice.mark` is new, so it carries the new fields.
    """

    session_id: str
    notice_ids: list[int] = field(default_factory=list)
    state: str = "surfaced"                               # or `offered`
    transport: str | None = None                          # how it was delivered


@dataclass
class LanesArgs:
    """`lanes` sub-actions (C-17.1). Additive: the CLI needs stable key names."""
    action: str = "list"
    lane_id: str | None = None
    credential: str | None = None
    until: str | None = None
    owner: str | None = None            # for transfer: "v1" | "v2"
    dry_run: bool = False               # transfer: print the diff, write nothing
    confirm_v1_edit: bool = False       # transfer: --i-understand-v1-edit
    request_id: str | None = None       # touch, touch-status: the operator touch to collect
    wait_s: float | None = None         # touch-status: long-poll seconds, at most 30 (C-16.4)


@dataclass
class ReadingsArgs:
    lane_id: str | None = None
    scope: str | None = None


@dataclass
class PickArgs:
    family: str = "codex"
    model: str | None = None
    exclusions: list[str] = field(default_factory=list)
    min_headroom: float | None = None


@dataclass
class OperationsArgs:
    command: str
    dry_run: bool = False
    target: str | None = None
    hours: float = 24


@dataclass
class PingArgs:
    """`ping` (v1 `notify`): push or park a message in a session inbox."""
    text: str
    session_id: str | None = None


@dataclass
class SessionsArgs:
    """`sessions` (C-23.33, C-23.35, C-23.55): the sessions kit's store seam.

    The kit reads Claude Code's transcripts and registry itself, and it decides
    eligibility against the transcript as it reads it (C-23.34). What it cannot
    do is write the store — the daemon owns that (C-3.4) — so the three durable
    facts it needs live behind this one op:

    * `state` reads back, for each named session, the operator's retirement flag
      (C-23.35), the last recorded nudge (C-23.33's dedupe and cooldown), and
      whether a revive lease is held (C-23.55), plus the lane session ids the
      ledger knows so a headless run is never treated as a session (C-23.31).
    * `nudged` is the reservation: it re-checks the dedupe key and the cooldown
      inside one transaction and records the nudge, so two sweeps racing each
      other cannot both send. Delivery stays with `ping`.
    * `retire` and `unretire` set and clear the durable retirement flag.

    Additive: an older daemon ignores the op and answers "unknown op", which the
    client reports as a daemon too old for this verb rather than as a silent
    success.
    """

    action: str = "state"                                 # state|nudged|retire|unretire
    session_id: str | None = None                         # nudged|retire|unretire
    session_ids: list[str] = field(default_factory=list)  # state
    dedupe_key: str | None = None                         # C-23.33's interruption point
    cooldown_s: float | None = None                       # C-23.33's per-session cooldown
    kind: str = "nudge"                                   # nudge|muster, for the record
    force: bool = False                                   # the operator named this session
    reason: str | None = None                             # retire
    detail: dict[str, Any] = field(default_factory=dict)  # recorded verbatim on the event


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
