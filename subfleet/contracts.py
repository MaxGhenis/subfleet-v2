"""Shared vocabulary for subfleet v2.

Every enum and dataclass here mirrors a clause in docs/acceptance-contract.md
(cited as C-x.y). Modules import from here instead of redefining strings so
the store, the daemon, the adapters, and the CLI cannot drift apart.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- Identifiers (C-1) -------------------------------------------------------

PROVIDERS = ("codex", "claude")
JOB_ID_SLUG_MAX = 40
REQUEST_ID_MAX = 128
HEADLESS_MARKER = "<!-- subfleet:headless -->"  # C-6.7


class JobState(str, enum.Enum):  # C-4.1
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"

    @property
    def terminal(self) -> bool:
        return self in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.LOST}


class WaitReason(str, enum.Enum):  # C-4.1
    CAPACITY = "capacity"
    DEPENDENCY = "dependency"
    APPROVAL = "approval"
    UNCERTAIN = "uncertain"


class AttemptState(str, enum.Enum):  # C-4.2
    RESERVED = "reserved"
    STARTING = "starting"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    LOST = "lost"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"

    @property
    def terminal(self) -> bool:
        return self in {
            AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.INTERRUPTED,
            AttemptState.LOST, AttemptState.CANCELLED, AttemptState.QUARANTINED,
        }


class OutcomeClass(str, enum.Enum):  # C-9.2
    OK = "ok"
    LIMITED = "limited"
    AUTH_DEAD = "auth-dead"
    CLI_TOO_OLD = "cli-too-old"
    CONTENT_FILTER = "content-filter"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


class ReadingLabel(str, enum.Enum):  # C-9.1
    PROVIDER = "provider"
    STALE_PROVIDER = "stale-provider"
    ADMISSION_OBSERVED = "admission-observed"
    LOCAL_BACKOFF = "local-backoff"
    UNKNOWN = "unknown"


class ClosureReason(str, enum.Enum):  # C-9.6
    PROVIDER_LIMIT = "provider-limit"
    CREDITS = "credits"
    AUTH_DEAD = "auth-dead"
    OPERATOR_HOLD = "operator-hold"
    COOLDOWN = "cooldown"


class ClockSource(str, enum.Enum):  # C-9.4
    REPORTED = "reported"
    GUESSED = "guessed"


class Attestation(str, enum.Enum):  # C-12.5
    ATTESTED = "attested"
    MISMATCH = "mismatch"
    UNATTESTED = "unattested"


class NoticeState(str, enum.Enum):  # C-15.3
    PENDING = "pending"
    OFFERED = "offered"
    ACKNOWLEDGED = "acknowledged"
    SURFACED = "surfaced"


class ActionState(str, enum.Enum):  # C-19.1
    PENDING = "pending"
    EXECUTING = "executing"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Sandbox(str, enum.Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class LaneOwner(str, enum.Enum):  # C-10.4
    V1 = "v1"
    V2 = "v2"


# --- Exit codes (C-17.3) -----------------------------------------------------

class Exit(enum.IntEnum):
    OK = 0
    OPERATIONAL = 1
    INVALID_INPUT = 2
    NO_LANE = 3
    HARD_LIMIT_PINNED = 4
    AUTH_DEAD = 5
    CLI_TOO_OLD = 6
    REFUSED = 7
    DAEMON_UNAVAILABLE = 69
    QUEUED = 75
    WAIT_TIMEOUT = 124
    JOB_LOST = 125
    CANCELLED = 130


# --- Defaults (C-6.4, C-9, C-11.3, C-18.1) -----------------------------------

DEFAULT_CAPS: dict[str, int] = {
    "max_active_attempts": 4,
    "max_in_flight_per_lane": 2,
    "max_in_flight_unmeasured": 1,
    "max_wall_s": 21600,
    "max_attempts": 3,
    "max_child_jobs": 8,
}
READING_TTL_S = 120
GUESSED_CLOSURE_S = 3600
TRANSIENT_RETRY_DELAY_S = 60
START_GRACE_S = 10
TERM_GRACE_S = 15
HEADROOM_FLOOR = 0.15
WAIT_POLL_MAX_S = 60
PROBE_INTERVAL_S = 300
RETENTION_MAX_JOBS = 500
RETENTION_MAX_BYTES = 2 * 1024 ** 3

# Codex window durations in minutes (C-9.7).
WINDOW_KEYS = {300: "five_hour", 10080: "seven_day"}


# --- Data carried between modules -------------------------------------------

@dataclass(frozen=True)
class Credential:
    """A credential reference, never a value (C-10.1, C-10.5)."""
    provider: str
    ref: str                # keychain item name or home directory path
    kind: str               # "keychain-token" | "home"
    epoch: int = 1


@dataclass(frozen=True)
class Lane:
    lane_id: str
    provider: str
    account_key: str        # C-1.4
    credential: Credential
    home: str | None
    owner: LaneOwner
    desktop: bool
    enabled: bool = True


@dataclass(frozen=True)
class Reading:  # C-9.1
    lane_id: str
    scope: str              # "account" or a model id
    window: str             # "five_hour" | "seven_day" | "<minutes>"
    utilization: float | None
    resets_at: str | None   # ISO 8601 UTC
    label: ReadingLabel
    source: str             # "wham" | "oauth-usage" | "rate_limit_event" | "rollout" | "probe"
    observed_at: str
    attempt_id: str | None = None


@dataclass(frozen=True)
class Closure:  # C-9.6
    lane_id: str
    scope: str
    until_at: str
    reason: ClosureReason
    clock_source: ClockSource
    source_event: str | None


@dataclass(frozen=True)
class JobSpec:
    """What `submit` carries (C-6)."""
    request_id: str
    kind: str               # "dispatch" | "probe" | "gate-review" | "revive" | "handoff"
    workdir: str
    prompt_path: str
    task: str | None
    tier: str | None
    pinned_model: str | None
    pinned_lane: str | None
    sandbox: Sandbox
    out_path: str | None
    name: str | None
    exclusions: tuple[str, ...] = ()
    allow_desktop: bool = False
    allow_tmp: bool = False
    in_place: bool = False
    independent: bool = False
    parent_job_id: str | None = None
    caller_session: str | None = None
    caller_pid: int | None = None
    no_preamble: bool = False
    max_attempts: int = DEFAULT_CAPS["max_attempts"]
    max_wall_s: int = DEFAULT_CAPS["max_wall_s"]
    max_tokens_observed: int | None = None


@dataclass(frozen=True)
class Launch:  # C-12.2
    argv: tuple[str, ...]
    env_add: dict[str, str]         # merged over a sanitised environment; may hold the credential
    env_remove: tuple[str, ...]     # keys stripped before spawn (API keys)
    cwd: str
    stdin_path: str | None          # prompt file fed on stdin, or None
    stdout_path: str
    stderr_path: str
    raw_stream_path: str | None
    native_session_id: str | None   # Claude --session-id chosen up front
    notes: dict[str, Any] = field(default_factory=dict)
    """Adapter-chosen, JSON-serialisable facts about this launch that the adapter
    needs back at classification, attestation, and resume time and that no other
    parameter carries: the lane id and attempt id a `Reading` or `Closure` must be
    stamped with, the model requested, and for Claude the transcript path expected
    under `~/.claude/projects/` with its byte size at launch (`transcript_offset`),
    which bounds the attempt's own range inside a transcript a resume appends to
    (C-12.5, C-12.6). The daemon persists it beside the attempt and hands it back
    unchanged; it never holds a secret."""


@dataclass(frozen=True)
class ExitInfo:  # from the guardian's exit.json (C-5.2)
    rc: int
    signal: int | None
    wall_s: float
    child_pid: int | None
    spawn_error: str | None = None


@dataclass(frozen=True)
class Outcome:  # C-9.2 .. C-9.5
    cls: OutcomeClass
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)   # which evidence answered auth/admission/quota
    readings: tuple[Reading, ...] = ()
    closure: Closure | None = None
    native_session_id: str | None = None                    # thread id / session uuid when learned late
    transcript_path: str | None = None
    served_model: str | None = None


@dataclass(frozen=True)
class AttestationResult:
    status: Attestation
    served_model: str | None
    evidence: str


@dataclass(frozen=True)
class LaneInfo:  # returned by enroll (C-10.2)
    account_key: str
    plan: str | None
    home: str | None
    readings: tuple[Reading, ...]


@dataclass(frozen=True)
class Decision:  # C-11.5
    chain: tuple[str, ...]
    evaluations: tuple[dict[str, Any], ...]  # per model: candidates, rejections with reasons
    chosen_lane: str | None
    chosen_model: str | None
    reason: str
    policy_hash: str


def attempt_dir(state_root: Path, job_id: str, seq: int) -> Path:
    """C-2.3 layout helper shared by daemon, guardian, adapters, and CLI."""
    return state_root / "jobs" / job_id / f"a{seq}"
