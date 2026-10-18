"""Milestone 2 through the CLI, daemon, and real Claude adapter (C-21)."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude"


def expected(scenario):
    return json.loads((FIXTURES / scenario / "expected.json").read_text())


def iso_epoch(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_array(value):
    return json.loads(value) if isinstance(value, str) else value


def submitted(result):
    job_id = result.stdout.strip()
    assert re.fullmatch(r"\d{8}-\d{6}-[a-z0-9-]+", job_id), result
    return job_id


def test_allowed_readings_percentages_and_raw_stream(e2e):
    """C-1.7, C-6.7, C-8.2, C-9.1, C-9.8, C-12.4–C-12.6: real Claude evidence persists."""
    scenario = "success-allowed"
    want = expected(scenario)
    e2e.start(scenario=scenario)
    result = e2e.cli(*e2e.run_args("haiku", "--wait"))
    assert result.rc == 0, result
    job_id = submitted(result)
    shown = e2e.show(job_id)
    assert shown["job"]["state"] == "succeeded"
    assert Path(shown["job"]["prompt_path"]).read_bytes() == e2e.prompt.read_bytes()
    attempt, = shown["attempts"]
    assert attempt["lane_id"] == "claude-1"
    assert attempt["attestation"] == "attested"
    assert attempt["model_served"] == want["attestation"]["served_model"]

    readings = e2e.rows("SELECT * FROM readings WHERE attempt_id=? ORDER BY window",
                        (attempt["attempt_id"],))
    assert len(readings) == 2
    for reading, wanted in zip(readings, want["readings"], strict=True):
        assert reading["lane_id"] == "claude-1"
        assert reading["label"] == wanted["label"] == "provider"
        assert reading["scope"] == wanted["scope"] == "account"
        assert reading["window"] == wanted["window"]
        assert reading["utilization"] == wanted["utilization"]
        assert 0 <= reading["utilization"] <= 1
        assert reading["resets_at"] == iso_epoch(wanted["resets_at_epoch"])
        assert reading["source"] == "rate_limit_event"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", reading["observed_at"])

    status = e2e.cli("status")
    assert status.rc == 0, status
    lane_line, = [line for line in status.stdout.splitlines() if line.startswith("claude-1 ")]
    assert "five_hour 5%" in lane_line
    assert "seven_day 25%" in lane_line
    assert not e2e.rows("SELECT * FROM closures WHERE lane_id='claude-1'")

    artifacts = {artifact["role"]: artifact for artifact in shown["artifacts"]}
    assert {"deliverable", "stdout", "stderr", "raw-stream", "launch", "prompt-sent"} <= artifacts.keys()
    assert Path(artifacts["raw-stream"]["path"]).name == "stream.jsonl"
    assert Path(artifacts["raw-stream"]["path"]).read_bytes() == Path(artifacts["stdout"]["path"]).read_bytes()
    assert Path(artifacts["deliverable"]["path"]).read_text() == want["deliverable"]
    launch = json.loads(Path(artifacts["launch"]["path"]).read_text())
    notes = launch["notes"]
    assert notes["lane_id"] == attempt["lane_id"]
    assert notes["attempt_id"] == attempt["attempt_id"]
    assert notes["model_id"] == attempt["model_requested"]
    assert notes["session_id"] == attempt["native_session_id"]
    assert notes["transcript_path"] == attempt["transcript_path"]
    assert notes["transcript_offset"] == 0
    assert launch["stdin_path"] == artifacts["prompt-sent"]["path"]
    sent = Path(launch["stdin_path"]).read_bytes()
    assert sent.count(b"<!-- subfleet:headless -->") == 1
    assert sent.endswith(e2e.prompt.read_bytes())
    assert sent == (e2e.root / "claude-stdin.md").read_bytes()


def test_credits_rejection_closes_model_and_retry_explains_exclusion(e2e):
    """C-4.5, C-9.1, C-9.4, C-9.6, C-9.8, C-11.5: a credits rejection retries with evidence.

    The replayed payload was recorded from a Fable request; a credits rejection closes the
    requested model, which since Fable's retirement (2026-09-27) is Opus."""
    scenario = "rejected-credits-fable"
    want = expected(scenario)
    # Both fake lanes replay the rejection; two attempts give a terminal result.
    policy_path = e2e.root / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["caps"]["max_attempts"] = 2
    policy_path.write_text(json.dumps(policy))
    e2e.start(scenario=scenario)
    requested = policy["models"]["opus"]["id"]
    result = e2e.cli(*e2e.run_args("opus", "--wait"))
    assert result.rc == 4, result  # C-17.3: the final attempt was limited; 3 is for a job never admitted
    job_id = submitted(result)
    shown = e2e.show(job_id)
    assert shown["job"]["state"] == "failed"
    assert [attempt["lane_id"] for attempt in shown["attempts"]] == ["claude-1", "claude-2"]
    assert "claude-1" in json_array(shown["job"]["exclusions"])
    first, second = shown["attempts"]
    for attempt in (first, second):
        assert attempt["outcome_class"] == "limited"
        assert attempt["rc"] == want["rc"]
        closure, = e2e.rows("SELECT * FROM closures WHERE lane_id=?", (attempt["lane_id"],))
        assert want["closure"]["scope"] == want["requested_model"] == "claude-fable-5-1"
        assert closure["scope"] == attempt["model_requested"] == requested
        assert closure["reason"] == "credits"
        assert closure["clock_source"] == "reported"
        assert closure["until_at"] == iso_epoch(want["closure"]["until_at_epoch"])
        assert closure["source_event"] == "rate_limit_event"
        reading, = e2e.rows("SELECT * FROM readings WHERE attempt_id=?", (attempt["attempt_id"],))
        assert reading["label"] == "admission-observed"
        assert reading["window"] == "admission"
        assert reading["scope"] == requested
        assert reading["utilization"] is None
        assert reading["resets_at"] == closure["until_at"]
        evidence = json.loads(attempt["evidence_json"])["classification"]
        assert "credits_required" in json.dumps(evidence)

    status = e2e.cli("status")
    assert status.rc == 0, status
    lane_lines = [line for line in status.stdout.splitlines() if line.startswith(("claude-1 ", "claude-2 "))]
    assert len(lane_lines) == 2
    for line in lane_lines:
        assert "admission-observed" in line
        assert "%" not in line

    why = e2e.cli("why", job_id)
    assert why.rc == 0, why
    assert "claude-2" in why.stdout
    assert re.search(r"claude-1[^\n]*excluded", why.stdout), why.stdout
    why_json = e2e.cli("why", job_id, "--json")
    assert why_json.rc == 0, why_json
    decision = json.loads(why_json.stdout)["decision"]
    assert decision["chosen_lane"] == "claude-2"
    rejections = [rejected for evaluation in decision["evaluations"]
                  for rejected in evaluation.get("rejections", evaluation.get("rejected", []))]
    assert any(row["lane_id"] == "claude-1" and "excluded" in row["reason"] for row in rejections)


def test_limit_without_clock_is_guessed_one_hour(e2e):
    """C-9.4, C-9.6: a real Claude no-clock rejection persists a guessed hour."""
    e2e.start(scenario="limit-no-clock")
    before = datetime.now(timezone.utc).replace(microsecond=0)
    result = e2e.cli(*e2e.run_args("opus", "-a", "claude-1", "--wait"))
    assert result.rc == 4, result
    after = datetime.now(timezone.utc)
    job_id = submitted(result)
    attempt, = e2e.attempts(job_id)
    assert attempt["outcome_class"] == "limited"
    closure, = e2e.rows("SELECT * FROM closures WHERE lane_id='claude-1'")
    assert closure["scope"] == "account"
    assert closure["reason"] == "provider-limit"
    assert closure["clock_source"] == "guessed"
    until = datetime.fromisoformat(closure["until_at"].replace("Z", "+00:00"))
    assert before + timedelta(seconds=3600) <= until <= after + timedelta(seconds=3600)


def test_model_downgrade_persists_transcript_attestation(e2e):
    """C-12.5, C-17.1: runs show exposes mismatch and the transcript's served model.

    The fixture models a Fable request answered by an older Opus; with Fable retired
    (2026-09-27) the request is Opus 5.5, and an Opus 5 answer is the same downgrade."""
    want = expected("model-downgrade")
    e2e.start(scenario="model-downgrade")
    result = e2e.cli(*e2e.run_args("opus", "--wait"))
    assert result.rc == 0, result
    shown = e2e.show(submitted(result))
    assert shown["job"]["state"] == "succeeded"
    attempt, = shown["attempts"]
    assert attempt["outcome_class"] == "ok"
    assert want["requested_model"] == "claude-fable-5-1"
    assert attempt["model_requested"] == "claude-opus-5-5"
    assert attempt["attestation"] == "mismatch"
    assert attempt["model_served"] == want["attestation"]["served_model"] == "claude-opus-5"
    assert Path(attempt["transcript_path"]).is_file()


def test_a_credential_that_belongs_to_another_account_yields_no_capacity(e2e):
    """C-1.4, C-9.1, C-10.6, C-11.2, C-17.4: the 2026-09-05 incident, end to end.

    `claude-1`'s credential reports the Axiom identity from
    `tests/fixtures/claude/identity/profile-mismatch.json` while the lane is
    recorded as its own account. The turn itself succeeds — the provider did the
    work — but not one usage window is stored against that lane, the finding is
    on the attempt for anyone to read, and the lane stops being a candidate until
    an operator re-enrols it. The rest of the fleet keeps working.
    """
    e2e.start(scenario="success-allowed",
              env={"SUBFLEET_FAKE_PROFILE": "1=profile-mismatch"})
    result = e2e.cli(*e2e.run_args("haiku", "-a", "claude-1", "--wait"))
    assert result.rc == 0, result
    job_id = submitted(result)

    shown = e2e.show(job_id)
    attempt, = shown["attempts"]
    assert attempt["lane_id"] == "claude-1"
    assert attempt["outcome_class"] == "ok"          # C-10.6 governs capacity only
    identity = json.loads(attempt["evidence_json"])["classification"]["identity"]
    assert identity["status"] == "identity-mismatch"
    assert identity["identity"]["email"] == "max@axiom.org"
    assert identity["expected"] == "e2e-account-1:e2e-org-1"

    # C-9.1: no reading at all, so C-17.4 can render no percentage for the lane.
    assert not e2e.rows("SELECT * FROM readings WHERE lane_id='claude-1'")
    lane_row, = e2e.rows("SELECT * FROM lanes WHERE lane_id='claude-1'")
    assert lane_row["identity_status"] == "mismatch"
    status = e2e.cli("status")
    assert status.rc == 0, status
    lane_line, = [line for line in status.stdout.splitlines()
                  if line.startswith("claude-1 ")]
    assert "%" not in lane_line and "no reading" in lane_line
    assert "identity-mismatch" in lane_line      # C-10.6: and why, not just silence
    lanes = e2e.cli("lanes")
    assert lanes.rc == 0, lanes
    assert "mismatch" in [line for line in lanes.stdout.splitlines()
                          if line.startswith("claude-1 ")][0]

    # C-11.2: not a candidate, and `why` says which rule rejected it.
    why = e2e.cli("why", "--task", "review", "--tier", "standard")
    assert why.rc == 0, why
    rejected = [line for line in why.stdout.splitlines()
                if "claude-1" in line and "identity-mismatch" in line]
    assert rejected, why.stdout
    assert "chose claude-2" in why.stdout

    # The fleet is not stranded: the lane whose credential is its own still runs.
    second = e2e.cli(*e2e.run_args("haiku", "--wait"))
    assert second.rc == 0, second
    attempt, = e2e.show(submitted(second))["attempts"]
    assert attempt["lane_id"] == "claude-2"
    readings = e2e.rows("SELECT * FROM readings WHERE lane_id='claude-2'")
    assert {row["window"] for row in readings} == {"five_hour", "seven_day"}


def test_a_setup_token_lane_keeps_working_without_profile_scope(e2e):
    """C-10.6: a 403 from the profile endpoint is a setup token, not a defect.

    The lane's stream readings are stored, and the evidence beside them says the
    identity was the one recorded at enrolment rather than one just verified.
    """
    e2e.setup_token_lane(1)
    e2e.start(scenario="success-allowed",
              env={"SUBFLEET_FAKE_PROFILE": "profile-no-scope"})
    result = e2e.cli(*e2e.run_args("haiku", "-a", "claude-1", "--wait"))
    assert result.rc == 0, result
    attempt, = e2e.show(submitted(result))["attempts"]
    identity = json.loads(attempt["evidence_json"])["classification"]["identity"]
    assert identity["status"] == "identity-enrolled"
    readings = e2e.rows("SELECT * FROM readings WHERE lane_id='claude-1'")
    assert {row["window"] for row in readings} == {"five_hour", "seven_day"}
    assert all(row["label"] == "provider" for row in readings)
    status = e2e.cli("status")
    lane_line, = [line for line in status.stdout.splitlines()
                  if line.startswith("claude-1 ")]
    assert "five_hour 5%" in lane_line


def test_empty_result_with_zero_rc_is_unknown(e2e):
    """C-9.2, C-12.6: rc 0 with empty text is unknown and cannot succeed."""
    e2e.start(scenario="empty-result-rc0")
    result = e2e.cli(*e2e.run_args("haiku", "--wait"))
    assert result.rc == 1, result
    job_id = submitted(result)
    shown = e2e.show(job_id)
    assert shown["job"]["state"] == "failed"
    assert shown["job"]["accepted_attempt_id"] is None
    attempt, = shown["attempts"]
    assert attempt["rc"] == 0
    assert attempt["outcome_class"] == "unknown"
    assert "empty" in attempt["outcome_detail"].lower()
