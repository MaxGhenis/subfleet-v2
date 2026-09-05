"""Provider adapter interface (C-12).

Adapters translate between subfleet's vocabulary and one provider CLI. They
return data and never touch the store, spawn processes, or log secrets. The
daemon owns processes (through the guardian) and persistence; adapters own
argument construction, classification, attestation, and deliverable capture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..contracts import (
    AttestationResult, Credential, ExitInfo, JobSpec, Lane, LaneInfo, Launch, Outcome, Reading,
)


class AdapterError(Exception):
    """A refusal or precondition failure the daemon maps to an exit code."""

    def __init__(self, message: str, code: int = 7, fix: str | None = None):
        super().__init__(message)
        self.code = code
        self.fix = fix


class Adapter(ABC):
    provider: str

    @abstractmethod
    def enroll(self, credential: Credential) -> LaneInfo:
        """Validate a credential and read the account identity (C-10.2).

        Codex: read auth.json, refuse API-key logins and free plans, probe usage.
        Claude: one Haiku turn under the token, read the rate_limit_event.
        May run a short subprocess; must not write anywhere but a temp dir.
        """

    @abstractmethod
    def probe(self, lane: Lane, credential_env: dict[str, str]) -> tuple[Reading, ...]:
        """Return fresh readings for the lane, or an empty tuple with no exception
        when the provider offers no sensor (C-9.1)."""

    @abstractmethod
    def build_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                     credential_env: dict[str, str], model_id: str, effort: str | None,
                     prompt_path: Path, guard_override: str | None) -> Launch:
        """Argv, environment additions and removals, cwd, and file paths for one
        attempt (C-12.2, C-12.3, C-12.4). `credential_env` already holds the
        resolved secret under the provider's variable name; pass it through
        `env_add` and never anywhere else."""

    @abstractmethod
    def classify(self, attempt_dir: Path, launch: Launch, exit_info: ExitInfo) -> Outcome:
        """Authentication, then admission, then quota (C-9.2). Read stdout,
        stderr, and the raw stream from `attempt_dir`; return readings and a
        closure when the evidence supports them."""

    @abstractmethod
    def attest(self, attempt_dir: Path, launch: Launch, outcome: Outcome,
               model_id: str) -> AttestationResult:
        """Served-model attestation (C-12.5). Never a false positive."""

    @abstractmethod
    def deliverable(self, attempt_dir: Path, launch: Launch, outcome: Outcome) -> bytes | None:
        """The final assistant text for this attempt only (C-12.6), or None."""

    @abstractmethod
    def resume_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                      credential_env: dict[str, str], native_session_id: str,
                      prompt_path: Path, guard_override: str | None) -> Launch | None:
        """A native continuation on the same lane, or None when unsupported."""

    # --- helpers adapters may share -----------------------------------------

    @staticmethod
    def read_text(path: Path, limit: int = 4_000_000) -> str:
        try:
            with path.open("rb") as fh:
                return fh.read(limit).decode("utf-8", "replace")
        except FileNotFoundError:
            return ""
