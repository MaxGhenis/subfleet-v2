"""D-SHUT1 through the CLI, daemon, guardian and detached Codex fixture fake."""

import json

import pytest


@pytest.mark.parametrize("case", ("cut-off-item-completed", "cut-off-item-started", "cut-off-error"))
def test_zero_exit_cut_off_turn_cannot_publish_output(e2e, case):
    path = e2e.root / "policy.json"
    policy = json.loads(path.read_text())
    policy["caps"]["max_attempts"] = 1
    path.write_text(json.dumps(policy))
    e2e.out.write_bytes(b"previous output\n")
    e2e.start(scenario=case)
    submitted = e2e.cli(*e2e.run_args("astra", "-o", e2e.out, "-d"))
    assert submitted.rc == 0, submitted
    identity = submitted.stdout.strip()
    e2e.until(lambda: e2e.job(identity)["state"] == "failed")
    job = e2e.job(identity)
    assert job["accepted_attempt_id"] is None
    attempt, = e2e.attempts(identity)
    assert (attempt["rc"], attempt["outcome_class"], attempt["killed_by"]) == (0, "transient", None)
    assert attempt["outcome_detail"] == "Codex exited before its turn completed"
    evidence = json.loads(attempt["evidence_json"])["classification"]
    assert evidence["turn_completed"] is False
    adir = e2e.root / "jobs" / identity / "a1"
    receipt = json.loads((adir / "exit.json").read_text())
    assert (receipt["rc"], receipt["signal"]) == (0, None)
    assert not (adir / "last.md").exists()
    assert b"turn.completed" not in (adir / "stream.jsonl").read_bytes()
    assert e2e.out.read_bytes() == b"previous output\n"
    assert not e2e.rows("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'", (attempt["attempt_id"],))
    notice, = e2e.rows("SELECT * FROM notices WHERE job_id=?", (identity,))
    assert notice["text"].splitlines()[0] == f"{identity}: failed; rc=0; deliverable=-; out=-"
    assert "Codex exited before its turn completed" in notice["text"]
    why = e2e.cli("why", identity)
    assert why.rc == 0, why
    explanation = ("Last attempt a1: Codex exited before its turn completed "
                   f"(last event: {evidence['last_event_type']})")
    assert explanation in why.stdout
    why_json = e2e.cli("why", identity, "--json")
    assert why_json.rc == 0, why_json
    assert json.loads(why_json.stdout)["attempt_explanation"] == explanation
    shown = e2e.cli("runs", "show", identity)
    assert shown.rc == 1, shown
    # C-17.1: v1's shape with nothing after `--- out.md ---`; evidence on stderr.
    metadata, result = shown.stdout.split("\n--- out.md ---\n")
    assert json.loads(metadata)["job"]["job_id"] == identity
    assert result == ""
    assert "No accepted result" in shown.stderr
    assert "Unaccepted evidence" in shown.stderr
    interim = (adir / "deliverable.md").read_text()
    if interim:
        assert interim not in shown.stdout
    result = e2e.cli("runs", "show", identity, "--out")
    assert result.rc == 1, result
    assert result.stdout == ""
    assert "no deliverable recorded" in result.stderr
