"""C-12.8, C-23.43: a process fixture with explicitly synthetic model evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from subfleet.contracts import Attestation, AttestationResult, Launch, OutcomeClass
from subfleet.guardian import atomic_publish
from tests.fake_adapter import FakeAdapter

FIXTURE = Path(__file__).parents[1] / "fixtures/gates/peer-verdict.txt"


class FakeGateAdapter(FakeAdapter):
    """C-12.8: retain ordinary fake probes and run gate reviews in a real child."""

    def build_launch(self, job, attempt_id, attempt_dir, lane, credential_env,
                     model_id, effort, prompt_path, guard_override):
        if job.kind != "gate-review":
            return super().build_launch(job, attempt_id, attempt_dir, lane, credential_env,
                                        model_id, effort, prompt_path, guard_override)
        assert job.isolated_review and job.sandbox == "read-only" and job.review_root
        return Launch(
            argv=(sys.executable, "-m", "tests.fake.gate_peer", "--model", model_id,
                  "--evidence", str(attempt_dir / "fake-model-evidence.json")),
            env_add={"SUBFLEET_LANE": lane.lane_id},
            env_remove=("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
            cwd=job.workdir, stdin_path=str(prompt_path),
            stdout_path=str(attempt_dir / "stdout"), stderr_path=str(attempt_dir / "stderr"),
            raw_stream_path=None, native_session_id=None,
            notes={"fake_gate_peer": True, "attempt_id": attempt_id,
                   "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        )

    def attest(self, attempt_dir, launch, outcome, model_id):
        if not launch.notes.get("fake_gate_peer"):
            return super().attest(attempt_dir, launch, outcome, model_id)
        path = attempt_dir / "fake-model-evidence.json"
        try:
            evidence = json.loads(path.read_text())
            if (outcome.cls != OutcomeClass.OK or not evidence.get("synthetic")
                    or evidence["attempt_id"] != launch.notes["attempt_id"]
                    or evidence["fixture_sha256"] != launch.notes["fixture_sha256"]):
                raise ValueError("fake fixture evidence does not belong to this attempt")
            served = evidence["model_served"]
            return AttestationResult(
                Attestation.ATTESTED if served == model_id else Attestation.MISMATCH,
                served, f"synthetic gate peer fixture evidence: {path}",
            )
        except (OSError, ValueError, KeyError):
            return AttestationResult(Attestation.UNATTESTED, None,
                                     "fake gate peer did not publish its fixture evidence")


def main(argv=None):
    """C-12.8: replay the sentinel fixture using the daemon's copied exact revision."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    assert "artifact_revision" in prompt
    revision = json.loads(Path("artifact.json").read_text())
    fixture = FIXTURE.read_bytes()
    evidence = {"synthetic": True, "attempt_id": os.environ["SUBFLEET_ATTEMPT"],
                "model_served": args.model, "fixture_sha256": hashlib.sha256(fixture).hexdigest()}
    atomic_publish(args.evidence, json.dumps(evidence).encode())
    sys.stdout.write(fixture.decode().replace("REVISION", json.dumps(revision)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
