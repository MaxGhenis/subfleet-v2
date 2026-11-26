"""C-9.5, D-CAP1 end to end: the real daemon, guardian and adapters, with Codex replaying the
2026-10-13 refusal ("Selected model is at capacity. Please try a different model.",
`tests/fixtures/codex/model-at-capacity-sol`) and Claude answering.

An unpinned `hard` review whose chain prefers the Codex model and then Opus (the live chain's
shape) runs its second attempt on Opus at the next admission pass, not on the same lane and model
60 s later. A job pinned to the Codex model keeps that same-lane retry after 60 s.
"""

from __future__ import annotations

from datetime import datetime
import json

from subfleet.contracts import MODEL_CAPACITY_STEER_S, TRANSIENT_RETRY_DELAY_S

CODEX_REFUSES = {"SUBFLEET_FAKE_CODEX_SCENARIO": "model-at-capacity-sol"}
ASTRA, OPUS = "gpt-6-astra", "claude-opus-5-5"


def seconds(later: str, earlier: str) -> float:
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))   # noqa: E731
    return (parse(later) - parse(earlier)).total_seconds()


def submitted(result) -> str:
    assert result.rc == 0, result
    return result.stdout.strip().splitlines()[0].strip()


def test_c9_5_a_refused_hard_review_runs_next_on_its_chains_next_model(e2e):
    e2e.policy_update(lambda policy: policy["chains"].update(
        review=["haiku", "sonnet", ["astra", "opus"], ["astra", "opus"]]))
    e2e.start(scenario="success-allowed", env=CODEX_REFUSES)
    result = e2e.cli("run", "--task", "review", "--tier", "hard", "-s", "read-only", "-C", e2e.workdir,
                     "-p", e2e.prompt, "--allow-tmp", "--wait", timeout=180)
    identity = submitted(result)
    first, second = e2e.attempts(identity)
    assert (first["model_requested"], first["lane_id"][:6], first["outcome_class"]) == (ASTRA, "codex-", "transient")
    evidence = json.loads(first["evidence_json"])
    assert evidence["classification"]["model_capacity"] is True
    assert evidence["classification"]["admission"] == {
        "type": "error", "message": "Selected model is at capacity. Please try a different model."}
    assert evidence["capacity_steer"]["model_id"] == ASTRA
    assert seconds(evidence["capacity_steer"]["until"], first["finished_at"]) == MODEL_CAPACITY_STEER_S
    assert (second["model_requested"], second["lane_id"][:7], second["state"]) == (OPUS, "claude-", "succeeded")
    # The next pass, not the transient retry's 60 s (C-11.4's probe of the Claude lane may run first).
    assert seconds(second["reserved_at"], first["finished_at"]) < TRANSIENT_RETRY_DELAY_S
    job = e2e.job(identity)
    assert (job["state"], job["accepted_attempt_id"]) == ("succeeded", second["attempt_id"])
    assert e2e.rows("SELECT * FROM closures") == []
    assert all(row["enabled"] for row in e2e.rows("SELECT enabled FROM lanes"))
    why = e2e.cli("why", identity)
    assert why.rc == 0 and "at capacity for this job" in why.stdout, why


def test_c9_5_a_job_pinned_to_the_model_keeps_the_same_lane_retry_after_60_s(e2e):
    e2e.start(scenario="success-allowed", env=CODEX_REFUSES)
    identity = submitted(e2e.cli(*e2e.run_args("astra", "-s", "read-only", "--detach")))

    def retried():
        rows = e2e.attempts(identity)
        return rows if len(rows) >= 2 else None
    first, second = e2e.until(retried, timeout=150)[:2]
    try:
        assert first["outcome_class"] == "transient"
        evidence = json.loads(first["evidence_json"])
        assert evidence["classification"]["model_capacity"] is True and "capacity_steer" not in evidence
        assert (second["lane_id"], second["model_requested"]) == (first["lane_id"], ASTRA)
        assert seconds(second["reserved_at"], first["finished_at"]) >= TRANSIENT_RETRY_DELAY_S - 1
    finally:
        e2e.cli("kill", identity)
