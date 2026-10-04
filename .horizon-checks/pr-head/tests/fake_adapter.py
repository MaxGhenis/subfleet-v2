"""Provider adapter used only by isolated daemon tests (C-12.1, C-12.8)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from subfleet.adapters.base import Adapter
from subfleet.contracts import (
    Attestation, AttestationResult, ClockSource, Closure, ClosureReason,
    Credential, ExitInfo, JobSpec, Lane, LaneInfo, Launch, Outcome,
    OutcomeClass, Reading,
)


class FakeAdapter(Adapter):
    """Launch a deterministic local process without provider credentials."""

    provider = "codex"

    def enroll(self, credential: Credential) -> LaneInfo:
        return LaneInfo(account_key="codex:fake", plan="fake", home=credential.ref,
                        readings=())

    def probe(self, lane: Lane, credential_env: dict[str, str]) -> tuple[Reading, ...]:
        return ()

    def build_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                     credential_env: dict[str, str], model_id: str, effort: str | None,
                     prompt_path: Path, guard_override: str | None) -> Launch:
        settings: dict = {}
        try:
            prompt = prompt_path.read_text()
        except OSError:
            prompt = ""
        # The daemon may prepend write/headless instructions and append a retry
        # checkpoint. Only a JSON object starting on its own line carries fake
        # settings; arbitrary braces in an ordinary prompt remain prompt text.
        decoder = json.JSONDecoder()
        offset = 0
        for line in prompt.splitlines(keepends=True):
            if line.lstrip().startswith("{"):
                try:
                    parsed, _ = decoder.raw_decode(prompt[offset:].lstrip())
                    if isinstance(parsed, dict) and set(parsed) & {"scenario", "delay_s", "marker"}:
                        settings = parsed
                        break
                except ValueError:
                    pass
            offset += len(line)
        env_add = {**credential_env, "SUBFLEET_LANE": lane.lane_id}
        for key, variable in (("scenario", "SUBFLEET_FAKE_SCENARIO"),
                              ("delay_s", "SUBFLEET_FAKE_DELAY_S"),
                              ("marker", "SUBFLEET_FAKE_MARKER")):
            if key in settings:
                env_add[variable] = str(settings[key])
        scenario = env_add.get("SUBFLEET_FAKE_SCENARIO",
                               os.environ.get("SUBFLEET_FAKE_SCENARIO", "ok"))
        binary = Path(__file__).parent / "bin" / "fakeprov"
        argv = ((str(attempt_dir / "intentionally-missing-provider"),)
                if scenario == "spawn-fail" else (sys.executable, str(binary)))
        return Launch(
            argv=argv, env_add=env_add,
            env_remove=("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
            cwd=job.workdir, stdin_path=str(prompt_path),
            stdout_path=str(attempt_dir / "stdout"),
            stderr_path=str(attempt_dir / "stderr"),
            raw_stream_path=None, native_session_id=None,
        )

    def classify(self, attempt_dir: Path, launch: Launch, exit_info: ExitInfo) -> Outcome:
        evidence = {"rc": exit_info.rc, "signal": exit_info.signal}
        if exit_info.rc == 0:
            return Outcome(OutcomeClass.OK, "fake provider succeeded", evidence=evidence)
        if exit_info.rc == 4:
            line = self.read_text(attempt_dir / "stderr").splitlines()[-1]
            quota = json.loads(line)
            until_at = datetime.fromtimestamp(quota["resets_at"], timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            closure = Closure(
                lane_id=launch.env_add["SUBFLEET_LANE"], scope=quota["scope"],
                until_at=until_at, reason=ClosureReason.PROVIDER_LIMIT,
                clock_source=ClockSource.REPORTED, source_event=line,
            )
            return Outcome(OutcomeClass.LIMITED, "fake provider hard limit",
                           evidence={**evidence, "quota": quota}, closure=closure)
        return Outcome(OutcomeClass.UNKNOWN, exit_info.spawn_error or "fake provider failed",
                       evidence=evidence)

    def attest(self, attempt_dir: Path, launch: Launch, outcome: Outcome,
               model_id: str) -> AttestationResult:
        return AttestationResult(Attestation.UNATTESTED, None,
                                 "fake provider supplies no served-model evidence")

    def deliverable(self, attempt_dir: Path, launch: Launch, outcome: Outcome) -> bytes | None:
        try:
            return (attempt_dir / "stdout").read_bytes() or None
        except FileNotFoundError:
            return None

    def resume_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                      credential_env: dict[str, str], native_session_id: str,
                      prompt_path: Path, guard_override: str | None,
                      model_id: str | None = None) -> Launch | None:
        """The same local process, but recording which session it continued.

        A `revive` job's whole point is that it resumes the named session rather
        than starting a new one (C-23.54), so the fake makes that observable:
        `native_session_id` reaches the attempt row, and a test can read it back.
        """
        launch = self.build_launch(job, attempt_id, attempt_dir, lane, credential_env,
                                   model_id or "fake-model", None, prompt_path,
                                   guard_override)
        return dataclasses.replace(launch, native_session_id=native_session_id)
