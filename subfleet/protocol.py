"""Socket protocol between the CLI (and other clients) and the daemon.

Newline-delimited JSON over a unix socket, one request line then one response
line (C-16). This module defines the wire shapes and the encode/decode
helpers; it has no I/O so both the client and the server import it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any

from .contracts import Exit

PROTOCOL_VERSION = 1

# C-29: the conversation ops (design §5), answered by subfleet/conversations/service.py.
CONVERSATION_OPS = (
    "capabilities", "conversation.list", "conversation.status", "conversation.open", "conversation.create", "conversation.settings",
    "conversation.rename", "conversation.unblock", "conversation.history", "conversation.events", "conversation.watch",
    "message.submit", "message.status", "message.cancel", "message.steer", "turn.interrupt", "message.resolve",
    "approval.list", "approval.get", "approval.respond", "attachment.add", "catalog.refresh", "models.list",
    "conversation.runs",
    "turn.diff", "conversation.diff",
    "conversation.handoff",
    "workspace.check",
    "conversation.wake",
    # C-25.7: suggested conversations; `suggestion.start` and `suggestion.dismiss` are a person's.
    "suggestion.create", "suggestion.list", "suggestion.start", "suggestion.dismiss", "suggestion.withdraw",
)

#: What `decode_request` answers for an op this daemon does not serve. A client
#: reads it as a version signal (the daemon is older than the op), not as a
#: failed request (C-25.1).
UNKNOWN_OP = "unknown op"

#: C-25.1, C-26.12: the capability that says `list` honours `kind` and
#: `include_turns`. A client sends those fields only to a daemon advertising it.
JOBS_KIND_CAPABILITY = "jobs.kind.v1"

#: C-8.5 (review P3-10): `submit` honours `push_branch`. A daemon without it drops
#: the field (C-16.2) and never pushes, so a client sends it only to one that lists this.
PUSH_CAPABILITY = "push.v1"

OPS = (
    "submit", "list", "show", "wait", "kill", "lanes", "readings", "why",
    "notice.pending", "notice.ack", "notice.mark", "notice.list", "notice.withdraw", "ping", "daemon.status",
    "gate.start", "gate.poll", "gate.continue",
    "sessions", "pick", "operations", "tasks",
    *CONVERSATION_OPS,
)


REFUSAL_MESSAGES = {
    "malformed-request": "request: malformed JSON or an unsupported number",
    "bad-encoding": "request: invalid UTF-8",
    "nested-request": "request: nested too deeply",
    "request-object": "request: must be a JSON object",
    "request-size": "request: exceeds 1 MiB",
    "bad-version": f"v: unsupported protocol version; this daemon speaks {PROTOCOL_VERSION}",
    # Keep the version-discovery prefix used by old clients (C-25.1).
    "unknown-op": f"{UNKNOWN_OP}: op must name a supported operation",
    "args-object": "args: must be an object",
    "invalid-args": "args: required fields are missing or invalid",
    "refused-args": "args: request was refused",
    "bad-id": "id: must be a safe token of at most 128 ASCII letters, digits, '-' or '_'",
    "bad-notice-id": "notice_id: must be an integer",
    "malformed-response": "malformed response: expected a JSON object with valid fields",
}
CORRELATION_ID_MAX = 128
_CORRELATION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class ProtocolError(Exception):
    """A request or handler refusal carrying the exit code the CLI should use."""

    def __init__(self, message: str, code: Exit = Exit.INVALID_INPUT, fix: str | None = None,
                 *, reason: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.fix = fix
        # Decoding/argument errors are tagged by request_error. Handler errors
        # carry separate reasons and checked structural detail, never prose.
        self.reason = reason
        self.details = details


def request_error(reason: str, code: Exit = Exit.INVALID_INPUT) -> ProtocolError:
    return ProtocolError(REFUSAL_MESSAGES[reason], code, reason=reason)


def correlation_id(value: Any) -> str:
    """Admit a bounded token before reflecting it, including on refusals.

    Empty/missing means no correlation. Nonnegative JSON integers are accepted
    as decimal tokens for older clients. Recognized credentials are refused even
    when their spelling would otherwise fit the token alphabet.
    """
    if type(value) is int and 0 <= value < 10 ** CORRELATION_ID_MAX:
        value = str(value)
    if type(value) is not str or (value and _CORRELATION_ID.fullmatch(value) is None):
        raise request_error("bad-id")
    if value:
        from .ids import holds_credential
        if holds_credential(value):         # read whole and from each `-` and `_` (C-1.5's check)
            raise request_error("bad-id")
    return value


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

#: A submit's `sandbox` when the caller named none: the daemon takes the
#: policy's `permissions` entry for the task, else its `*` entry (C-11.1).
POLICY_SANDBOX = "policy"


#: C-5.1, D-QOS-BOOKED1: what `run --qos` may request. `utility` is the clamp every provider
#: starts under; `default` asks to start it unclamped, at the guardian's QoS.
PROVIDER_QOS_REQUESTS = ("utility", "default")


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
    # C-12.9, d714: the MCP servers a writable Claude job starts, by name; none
    # unless named. The daemon finds their entries itself; a client sends names only.
    mcp_servers: list[str] = field(default_factory=list)
    # C-11.2, C-17.2: the provider the pin's flag names, `claude` for `-a` and
    # `codex` for `-H`. It narrows a name both providers answer to; a lane id
    # says its own provider. Not stored and not part of the request digest.
    pinned_provider: str | None = None
    # C-17.7: {"id", "label", "index", "size"} for a job submitted by `run --batch`.
    # A label for people and the app, never an input to routing or the digest.
    batch: dict | None = None
    # C-8.5: explicit host-side publication of this dispatch's accepted bundle.
    push_branch: str | None = None
    # C-11.1–2: None takes policy requires; a boolean is an explicit override.
    needs_shell: bool | None = None
    # C-5.1, D-QOS-BOOKED1: `run --qos`, `utility` or `default`; None is `utility`. A
    # `default` request starts the provider unclamped only at a launch where the job is
    # `priority` (C-6.16); the daemon decides at each launch, and `why` says which.
    provider_qos: str | None = None


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
    # C-26.12: turn jobs are the conversation's, so `list` leaves them out unless
    # asked. `kind` lists only that kind (`turn` lists only turns); without it,
    # `include_turns` lists every kind. A daemon that predates these fields
    # ignores them (C-16.2), so a client sends them only to a daemon whose
    # `capabilities` lists JOBS_KIND_CAPABILITY (C-25.1; `cli.cmd_runs`).
    kind: str | None = None
    include_turns: bool = False
    # C-16.3: only the job carrying this request id, for a client settling a
    # submit whose answer was lost. A daemon older than the field lists every
    # job (C-16.2), so the client filters the rows as well.
    request_id: str | None = None


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
    session_id: str | None = None
    reason: str | None = None


@dataclass
class WhyArgs:
    job_id: str | None = None
    task: str | None = None
    tier: str | None = None
    pinned_model: str | None = None
    exclusions: list[str] = field(default_factory=list)
    allow_desktop: bool = False
    needs_shell: bool | None = None
    sandbox: str | None = None


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


#: C-15.3: notice ids on the wire. A job notice (`notices`) keeps its own id; a
#: service notice (`service_notices`: a `ping` message, a nudge, an alert, or a
#: pin's notice) travels negated, so one list can carry both. Its `job_id` is
#: None, except that `notice.pending` names a pin notice's job (C-11.8). Every
#: op that takes an id back must route it with `notice_row`: on 2026-09-29,
#: `notice.mark` ran negated ids against `notices`, matched nothing, and left
#: 1,749 service notices `pending`, so every hook surfaced the same ones again.
SERVICE_NOTICE_TABLE = "service_notices"
JOB_NOTICE_TABLE = "notices"


def service_notice_on_wire(row: dict[str, Any]) -> dict[str, Any]:
    """A `service_notices` row with its wire id (C-15.3); `notice.pending` then names a pin's job."""
    return {**row, "notice_id": -row["notice_id"], "job_id": None}


def notice_row(notice_id: Any) -> tuple[str, int]:
    """The table a wire notice id names, and the row id it has there (C-15.3)."""
    if isinstance(notice_id, bool) or not isinstance(notice_id, int):
        raise request_error("bad-notice-id")
    if notice_id < 0:
        return SERVICE_NOTICE_TABLE, -notice_id
    return JOB_NOTICE_TABLE, notice_id


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
    note: str | None = None            # release-limits: operator's reason


@dataclass
class TasksArgs:
    """`tasks` (C-11.10, D-TASKHOLD1): `list` the holds in force, `hold` a task
    until an instant with a note, or `release` it."""
    action: str = "list"
    task: str | None = None
    until: str | None = None
    note: str | None = None


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
class NoticeListArgs:
    """`notice.list` (C-15.8): what `notices` shows; read-only, marks nothing."""

    session_id: str | None = None          # every session's when None
    resolved: bool = False                 # also `surfaced` and `acknowledged`


@dataclass
class NoticeWithdrawArgs:
    """`notice.withdraw` (C-15.8): delete a session's named, undelivered service
    notices (negated ids, as `notice.pending` lists them), with one event
    saying what went and why. A separate shape, as `NoticeMarkArgs` is."""

    session_id: str
    notice_ids: list[int] = field(default_factory=list)
    reason: str | None = None
    # One per id when given: the fingerprint the listing showed
    # (`store.notice_fingerprint`). An id is reused once the newest row is
    # deleted, so a row is withdrawn only while it is still the one listed.
    fingerprints: list[str] = field(default_factory=list)


@dataclass
class NoticeAckArgs:
    """`notice.ack` as the daemon reads it: `NoticeArgs` plus, for `notices --ack`
    (C-15.8), each listed row's fingerprint, so an acknowledgement reaches a row
    only while it is still the one listed. `NoticeArgs` keeps its shape, so the
    `notice.ack` lines `runs show` and the hooks send are unchanged."""

    session_id: str
    notice_ids: list[int] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)


def ping_writes(args: dict[str, Any]) -> bool:
    """C-15.8: whether a `ping` has text, and so may write a notice. No text, or
    only whitespace, is a liveness question that reads nothing (C-16.5, C-16.7)."""
    text = args.get("text")
    return isinstance(text, str) and bool(text.strip())


@dataclass
class PingArgs:
    """`ping` (v1 `notify`): push or park a message in a session inbox. With no
    text it is a liveness question and writes nothing (C-15.8, C-16.2)."""
    text: str = ""
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
        # Old clients require id even on uncorrelated busy/unknown-op replies.
        # Only invalid-id refusals omit it, so the rejected token is never sent.
        omit_id = (isinstance(obj, Response) and not obj.id and obj.error is not None
                   and obj.error.message == REFUSAL_MESSAGES["bad-id"])
        obj = asdict(obj)
        if omit_id:
            obj.pop("id", None)
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_request(line: bytes | str) -> Request:
    try:
        data = json.loads(line)
    except UnicodeDecodeError:
        raise request_error("bad-encoding") from None
    except ValueError:
        raise request_error("malformed-request") from None
    except RecursionError:
        # C-16.7: a line under 1 MiB can still nest past the parser's depth;
        # answered as malformed, not left to end the connection's reader.
        raise request_error("nested-request") from None
    if not isinstance(data, dict):
        raise request_error("request-object")
    if type(data.get("v")) is not int or data["v"] != PROTOCOL_VERSION:
        raise request_error("bad-version")
    op = data.get("op")
    if type(op) is not str or op not in OPS:
        raise request_error("unknown-op")
    req_id = correlation_id(data.get("id", ""))
    args = data.get("args", {})
    if not isinstance(args, dict):
        raise request_error("args-object")
    return Request(op=op, args=args, id=req_id)


def decode_response(line: bytes | str) -> Response:
    try:
        data = json.loads(line)
    except (ValueError, RecursionError):
        raise request_error("malformed-response", Exit.OPERATIONAL) from None
    if not isinstance(data, dict):
        raise request_error("malformed-response", Exit.OPERATIONAL)
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
    except TypeError:
        raise request_error("invalid-args") from None


def ok(req_id: str, result: dict[str, Any]) -> Response:
    return Response(id=correlation_id(req_id), ok=True, result=result)


def fail(req_id: str, code: Exit | int, message: str, fix: str | None = None) -> Response:
    try:
        req_id = correlation_id(req_id)
    except ProtocolError:
        req_id, code, message, fix = "", Exit.INVALID_INPUT, REFUSAL_MESSAGES["bad-id"], None
    return Response(id=req_id, ok=False, error=ErrorBody(code=int(code), message=message, fix=fix))


def refusal_text(error: BaseException, *, reason: str = "invalid-args") -> tuple[str, str | None]:
    """A refusal's public message and fix, wherever it goes: the wire, or a record a
    client reads (a job's notice, a message's `state_reason`; C-25.7).

    A decoding or argument error (`request_error`, or a `ProtocolError` naming a
    request reason, which may keep its own words for direct callers) reads its
    template; a handler refusal reads its own by reason and checked details
    (`refusals`). Exception messages, args and optional repair text are never inputs.
    """
    if isinstance(error, ProtocolError) and type(error.reason) is str and error.reason in REFUSAL_MESSAGES:
        return REFUSAL_MESSAGES[error.reason], None
    if isinstance(error, ProtocolError) or hasattr(error, "code"):
        from .refusals import handler_refusal
        return handler_refusal(error)
    if type(reason) is not str or reason not in REFUSAL_MESSAGES:
        reason = "invalid-args"
    return REFUSAL_MESSAGES[reason], None


def refuse(req_id: str, error: BaseException, *, reason: str = "invalid-args") -> Response:
    """Request and handler boundaries: closed templates (`refusal_text`) and checked exit codes."""
    message, fix = refusal_text(error, reason=reason)
    code = getattr(error, "code", Exit.INVALID_INPUT)
    if type(code) not in (int, Exit) or code not in tuple(Exit):
        code = Exit.INVALID_INPUT
    return fail(req_id, code, message, fix)
