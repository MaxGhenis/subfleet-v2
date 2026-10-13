"""C-4.5, C-9.5, C-11.2, D-CAP1: a model-capacity refusal steers the job's next attempt.

2026-10-13: `20261013-024327-pr198-review-r6`, a `hard` review, ran 26 and then 11 minutes on
gpt-6.1-sol on codex-1, each attempt ended by Codex's "Selected model is at capacity. Please try
a different model.", the second asked 60 s after the first on the same lane and model (C-4.5's
transient retry); its third fell to Opus on claude-10, which was five-hour limited.
`20261013-015056-pr181-fix8` was refused three times, the third on codex-2: capacity is the
model's, not the lane's.

Now such an attempt (`transient`, `model_capacity` in its classification) of a job with no model
or lane pin that is no turn, resume or revive records a `capacity_steer` (the model, until
`MODEL_CAPACITY_STEER_S` after it finished), sets no same-lane retry, and leaves the job due on
the next pass; until then admission walks that model after the rest of the job's chain. No
closure, no lane disabled or excluded, no other job touched. A job pinned to the model keeps the
same-lane retry after 60 s. These drive the daemon's own finalization and admission in-process,
with the real Codex classifier reading the incident's stream (`tests/fixtures/codex/model-at-capacity-sol`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters.codex import CodexAdapter
from subfleet.adapters.registry import register
from subfleet.contracts import (MODEL_CAPACITY_STEER_S, TRANSIENT_RETRY_DELAY_S, Credential, Lane, LaneOwner,
                                Outcome, OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import after, utcnow
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401 (a fixture)
from tests.fake_adapter import FakeAdapter

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "model-at-capacity-sol"
ASTRA, OPUS = "gpt-6-astra", "claude-opus-5-5"


class Refused(FakeAdapter):
    """The real Codex classifier over the attempt's own stream: the incident's, replayed."""

    def classify(self, attempt_dir, launch, exit_info):
        return CodexAdapter().classify(attempt_dir, launch, exit_info)


class Disconnected(FakeAdapter):
    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.TRANSIENT, "Temporary Codex transport or capacity failure",
                       evidence={"rc": exit_info.rc, "model_answered": True,
                                 "admission": "stream disconnected before completion"})


@pytest.fixture
def fleet(state_daemon, monkeypatch):
    """codex-1 and codex-2, claude-1 and claude-2, each measured (so a `hard` job needs no probe,
    C-11.4), under the live policy's chains' shape: a `hard` review prefers the Codex model,
    then Opus (live: `["sol61", "opus"]`), and a `hard` build has the Codex model alone."""
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    register("claude", FakeAdapter)
    daemon.policy["chains"]["review"] = ["haiku", "sonnet", ["astra", "opus"], ["astra", "opus"]]
    first = daemon.store.get_lane("codex-1")
    home = str(harness.root / "home-codex-2")
    daemon.store.put_lane(replace(first, lane_id="codex-2", account_key="codex:codex-2",
                                  credential=Credential("codex", home, "home"), home=home))
    for identity in ("claude-1", "claude-2"):
        daemon.store.put_lane(Lane(identity, "claude", f"claude:{identity}@example.com",
                                   Credential("claude", f"/fake/{identity}", "home"), f"/fake/{identity}",
                                   LaneOwner.V2, False))
    for identity in ("codex-1", "codex-2", "claude-1", "claude-2"):
        daemon.store.add_reading(Reading(identity, "account", "seven_day", .1, after(86400),
                                         ReadingLabel.PROVIDER, "fixture", utcnow()))
    yield daemon, harness
    register("claude", FakeAdapter)


def submit(daemon, harness, **changes) -> str:
    args = {"pinned_model": None, "task": "review", "tier": "hard", **changes}
    return daemon.dispatch("submit", harness.submit_args(**args))["job_id"]


def admit(daemon, job_id) -> dict | None:
    """One admission pass; the job's newest attempt if the pass reserved one (no launch)."""
    before = len(daemon.store.list_attempts(job_id))
    daemon._admit()
    rows = daemon.store.list_attempts(job_id)
    for row in rows:
        daemon._pending_launches.discard(row["attempt_id"])
    return rows[-1] if len(rows) > before else None


def finish(daemon, attempt, adapter, *, rc=1, stdout=None, stderr=b""):
    """The attempt's provider exited with this output; finalize it under `adapter`'s verdict."""
    provider = daemon.store.get_lane(attempt["lane_id"]).provider
    register(provider, adapter)
    adir = daemon.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700, exist_ok=True)
    daemon._pending_launches.discard(attempt["attempt_id"])
    receipt = receipt_fixture(daemon, attempt, adir, rc=rc, stdout=stdout if stdout is not None else b"fixture result\n")
    (adir / "stderr").write_bytes(stderr)
    daemon._finalize(receipt)
    register(provider, FakeAdapter)
    return daemon.store.get_attempt(attempt["attempt_id"])


def refuse(daemon, attempt):
    """The incident's attempt: the real stream and stderr of pr198-review-r6/a1."""
    return finish(daemon, attempt, Refused, stdout=(FIXTURE / "stdout").read_bytes(),
                  stderr=(FIXTURE / "stderr").read_bytes())


def evidence(row) -> dict:
    return json.loads(row["evidence_json"])


def seconds(later: str, earlier: str) -> float:
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))   # noqa: E731
    return (parse(later) - parse(earlier)).total_seconds()


def test_c9_5_a_refused_hard_review_moves_to_its_chains_next_model_on_the_next_pass(fleet):
    """D-CAP1: a1 refused on the Codex model; a2 is Opus at the next pass, not the same pair 60 s on."""
    daemon, harness = fleet
    job_id = submit(daemon, harness)
    first = admit(daemon, job_id)
    assert (first["model_requested"], first["lane_id"][:5]) == (ASTRA, "codex")
    row = refuse(daemon, first)
    assert (row["state"], row["outcome_class"]) == ("failed", "transient")
    recorded = evidence(row)
    assert recorded["classification"]["model_capacity"] is True
    steer = recorded["capacity_steer"]
    assert steer["model_id"] == ASTRA
    assert seconds(steer["until"], row["finished_at"]) == MODEL_CAPACITY_STEER_S == 900
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"], job["rc"]) == ("waiting", "capacity", None)
    assert seconds(job["next_check_at"], row["finished_at"]) < TRANSIENT_RETRY_DELAY_S      # due now
    # The steer is the job's: no closure, no lane disabled, nothing excluded.
    assert daemon.store.query("SELECT * FROM closures") == []
    assert all(lane.enabled for lane in daemon.store.list_lanes())
    assert daemon._retry_pin(job)[1:] == ((), None)
    second = admit(daemon, job_id)
    assert second is not None, daemon.dispatch("why", {"job_id": job_id})["text"]
    assert (second["model_requested"], second["lane_id"][:6]) == (OPUS, "claude")
    decision = json.loads(daemon.store.list_decisions(job_id)[-1]["decision_json"])
    assert decision["chain"] == ["opus"]
    assert f"astra: at capacity for this job until {steer['until']}, walked last (C-9.5)" in decision["reason"]
    text = daemon.dispatch("why", {"job_id": job_id})["text"]
    assert "at capacity for this job" in text
    finish(daemon, second, FakeAdapter, rc=0)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"], job["accepted_attempt_id"]) == ("succeeded", 0, second["attempt_id"])


def test_c9_5_with_no_lane_for_the_next_model_the_job_goes_back_to_the_model_at_once(fleet):
    """C-9.5: a steer never holds a job. With both Claude lanes disabled, a2 is the Codex model
    again, on the next pass (no 60 s wait), on whichever of its lanes ranks first."""
    daemon, harness = fleet
    job_id = submit(daemon, harness)
    refuse(daemon, admit(daemon, job_id))
    for identity in ("claude-1", "claude-2"):
        daemon.store.update_lane(identity, enabled=0)
    second = admit(daemon, job_id)
    assert second is not None and second["model_requested"] == ASTRA


def test_c9_5_after_the_steer_ends_the_model_is_chosen_again(fleet):
    """C-9.5: the steer is bounded. Once `capacity_steer.until` has passed (here: the refusal's
    clocks moved 16 minutes back, as the store reads 16 minutes on), the job walks its chain in
    the policy's order and the Codex model takes it again."""
    daemon, harness = fleet
    job_id = submit(daemon, harness)
    row = refuse(daemon, admit(daemon, job_id))
    recorded = evidence(row)
    back = lambda value: (datetime.fromisoformat(value.replace("Z", "+00:00"))                # noqa: E731
                          - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recorded["capacity_steer"]["until"] = back(recorded["capacity_steer"]["until"])
    daemon.store.update_attempt(row["attempt_id"], evidence_json=json.dumps(recorded),
                                finished_at=back(row["finished_at"]))
    assert daemon._capacity_steer(daemon.store.get_job(job_id), daemon.store.list_attempts(job_id)) == {}
    second = admit(daemon, job_id)
    assert second is not None and second["model_requested"] == ASTRA
    decision = json.loads(daemon.store.list_decisions(job_id)[-1]["decision_json"])
    assert decision["chain"][0] == "astra" and "at capacity for this job" not in decision["reason"]


def test_c9_5_a_job_pinned_to_the_model_keeps_the_same_lane_retry_after_60_s(fleet):
    """C-4.5, C-9.5: `-m` is the operator's choice: the refusal is the old transient, the job
    waits 60 s and then runs once more on the same lane and model."""
    daemon, harness = fleet
    job_id = submit(daemon, harness, pinned_model="astra", task=None, tier=None)
    first = admit(daemon, job_id)
    row = refuse(daemon, first)
    recorded = evidence(row)
    assert recorded["classification"]["model_capacity"] is True        # the fact is recorded
    assert "capacity_steer" not in recorded                            # and steers nothing
    job = daemon.store.get_job(job_id)
    assert seconds(job["next_check_at"], row["finished_at"]) >= TRANSIENT_RETRY_DELAY_S - 1
    _, exclusions, pin = daemon._retry_pin(job)
    assert (pin["pinned_lane"], pin["pinned_model"]) == (first["lane_id"], ASTRA)
    assert admit(daemon, job_id) is None                               # not before its 60 s
    daemon.store.update_job(job_id, next_check_at=utcnow())
    second = admit(daemon, job_id)
    assert (second["lane_id"], second["model_requested"]) == (first["lane_id"], ASTRA)


def test_c9_5_a_lane_pinned_job_keeps_the_same_lane_retry(fleet):
    daemon, harness = fleet
    job_id = submit(daemon, harness, pinned_lane="codex-2")
    first = admit(daemon, job_id)
    assert first["lane_id"] == "codex-2"
    assert "capacity_steer" not in evidence(refuse(daemon, first))
    _, _, pin = daemon._retry_pin(daemon.store.get_job(job_id))
    assert (pin["pinned_lane"], pin["pinned_model"]) == ("codex-2", ASTRA)


def test_c9_5_an_ordinary_transient_keeps_the_same_lane_retry_after_60_s(fleet):
    """C-9.5 unchanged: a stream disconnect is not the model's refusal."""
    daemon, harness = fleet
    job_id = submit(daemon, harness)
    first = admit(daemon, job_id)
    row = finish(daemon, first, Disconnected)
    assert "capacity_steer" not in evidence(row) and "model_capacity" not in evidence(row)["classification"]
    job = daemon.store.get_job(job_id)
    assert seconds(job["next_check_at"], row["finished_at"]) >= TRANSIENT_RETRY_DELAY_S - 1
    _, _, pin = daemon._retry_pin(job)
    assert (pin["pinned_lane"], pin["pinned_model"]) == (first["lane_id"], ASTRA)


def test_c9_5_the_steer_is_the_jobs_and_another_job_still_gets_the_model(fleet):
    """D-CAP1: capacity is intermittent (of the 390 gpt-6.1-sol attempts started within 15 minutes
    of a refusal from 2026-10-01 to 2026-10-13, 350 were not refused), so one job's refusal moves
    only that job."""
    daemon, harness = fleet
    job_id = submit(daemon, harness)
    refuse(daemon, admit(daemon, job_id))
    other = submit(daemon, harness)
    assert admit(daemon, other)["model_requested"] == ASTRA


def test_c9_5_a_build_with_one_model_in_its_chain_retries_it_at_once(fleet):
    """D-CAP1, pr181-fix8: a `hard` build's chain is the Codex model alone (live `build`: sol61 at
    `hard`); with nothing to steer to the job runs on it again at the next pass, as routed afresh."""
    daemon, harness = fleet
    job_id = submit(daemon, harness, task="build")
    row = refuse(daemon, admit(daemon, job_id))
    assert evidence(row)["capacity_steer"]["model_id"] == ASTRA
    second = admit(daemon, job_id)
    assert second is not None and second["model_requested"] == ASTRA


def test_c4_5_a_refused_job_still_ends_after_max_attempts(fleet):
    """C-4.5: a refusal is never success and counts toward `max_attempts`: three refusals end it."""
    daemon, harness = fleet
    for identity in ("claude-1", "claude-2"):
        daemon.store.update_lane(identity, enabled=0)
    job_id = submit(daemon, harness)
    for _ in range(3):
        refuse(daemon, admit(daemon, job_id))
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
    assert [row["outcome_class"] for row in daemon.store.list_attempts(job_id)] == ["transient"] * 3


@pytest.mark.parametrize("changes,steerable", [
    ({}, True), ({"kind": "handoff"}, True), ({"kind": "gate-review"}, True),
    ({"pinned_model": "astra"}, False), ({"pinned_lane": "codex-1"}, False),
    ({"kind": "turn"}, False), ({"kind": "resume"}, False), ({"kind": "revive"}, False),
])
def test_c9_5_capacity_steer_reads_only_its_own_attempts_and_applies_to_unpinned_kinds(fleet, changes, steerable):
    """`Daemon._capacity_steer`: the steers the job's own transient attempts recorded whose end is
    still ahead, for a job a steer applies to (`scheduler.capacity_steerable`), and nothing else."""
    daemon, _ = fleet
    live, ended = after(600), after(-600)
    rows = [{"outcome_class": "transient", "evidence_json": json.dumps({"capacity_steer": {"model_id": ASTRA, "until": live}})},
            {"outcome_class": "transient", "evidence_json": json.dumps({"capacity_steer": {"model_id": OPUS, "until": ended}})},
            {"outcome_class": "limited", "evidence_json": json.dumps({"capacity_steer": {"model_id": "gpt-5.6-terra", "until": live}})},
            {"outcome_class": "transient", "evidence_json": "{"},
            {"outcome_class": "transient", "evidence_json": None}]
    job = {"kind": "dispatch", "pinned_model": None, "pinned_lane": None, **changes}
    assert daemon._capacity_steer(job, rows) == ({ASTRA: live} if steerable else {})
