"""Codex adapter contracts exercised without a daemon or a provider process."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import (
    Attestation, ClockSource, Credential, ExitInfo, JobSpec, Lane, LaneOwner,
    Launch, Outcome, OutcomeClass, Sandbox,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
REQUIRED_CASES = (
    "success", "limit-with-clock", "limit-no-clock", "credits-rejection",
    "auth-401", "refresh-token-revoked", "cli-too-old", "content-filter",
    "stream-disconnect", "model-at-capacity", "spawn-fail", "model-scoped-limit",
)
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
THREAD = "01991c18-2680-7000-8000-000000000001"
MODEL = "gpt-6-astra"
GUARD_OVERRIDE = 'hooks={PreToolUse=[{matcher="Bash",hooks=[{type="command",command="/guard/hook"}]}]}'


def _job(workdir: Path, prompt: Path, sandbox: Sandbox = Sandbox.READ_ONLY) -> JobSpec:
    return JobSpec(
        request_id="codex-adapter-test", kind="dispatch", workdir=str(workdir),
        prompt_path=str(prompt), task=None, tier=None, pinned_model=MODEL,
        pinned_lane="codex-4", sandbox=sandbox, out_path=None, name="adapter-test",
    )


def _lane(home: Path) -> Lane:
    return Lane("codex-4", "codex", "codex:account-4", Credential("codex", str(home), "home"),
                str(home), LaneOwner.V2, False)


def _launch(attempt_dir: Path, home: Path | None = None) -> Launch:
    return Launch(
        argv=("codex", "exec", "--json", "-m", MODEL),
        env_add={"CODEX_HOME": str(home or attempt_dir / "home")},
        env_remove=("CODEX_API_KEY", "OPENAI_API_KEY"), cwd=str(attempt_dir),
        stdin_path=None, stdout_path=str(attempt_dir / "stdout"),
        stderr_path=str(attempt_dir / "stderr"), raw_stream_path=str(attempt_dir / "stream.jsonl"),
        native_session_id=None, lane_id="codex-4",
    )


def _exit(rc: int = 1, signal: int | None = None, spawn_error: str | None = None) -> ExitInfo:
    return ExitInfo(rc, signal, 0.1, 1234, spawn_error)


def _events(attempt_dir: Path, *events: dict, stderr: str = "") -> Launch:
    (attempt_dir / "stream.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    (attempt_dir / "stderr").write_text(stderr)
    return _launch(attempt_dir)


def test_required_fixture_corpus_exists():
    """C-12.7 Every required replay has all four files and provenance metadata."""
    for case in REQUIRED_CASES:
        directory = FIXTURES / case
        for name in ("stdout", "stderr", "rc", "expected.json"):
            assert (directory / name).is_file(), f"{case}/{name} is missing"
        expected = json.loads((directory / "expected.json").read_text())
        assert isinstance(expected["synthetic"], bool)
        assert expected["provenance"]


@pytest.mark.parametrize("case", REQUIRED_CASES)
def test_classifies_fixture_corpus(case):
    """C-9.2 C-9.4 C-9.6 C-12.7 Fixture evidence determines class, scope, clock and session."""
    directory = FIXTURES / case
    expected = json.loads((directory / "expected.json").read_text())
    launch = replace(_launch(directory), raw_stream_path=str(directory / "stdout"))
    rc = int((directory / "rc").read_text())
    outcome = CodexAdapter(now=lambda: NOW).classify(directory, launch, _exit(rc))
    assert outcome.cls.value == expected["class"]
    assert outcome.native_session_id == expected["native_session_id"]
    assert (outcome.closure is not None) == expected["closure"]
    assert outcome.evidence["rc"] == rc
    assert outcome.evidence["signal"] is None
    assert (outcome.closure.scope if outcome.closure else None) == expected["scope"]
    assert (outcome.closure.clock_source.value if outcome.closure else None) == expected["clock_source"]
    if outcome.closure:
        assert outcome.closure.lane_id == "codex-4"
        assert outcome.closure.source_event
        assert outcome.closure.until_at == expected.get("until_at", "2026-09-05T13:00:00Z")


@pytest.mark.parametrize("sandbox", list(Sandbox))
@pytest.mark.parametrize("effort", [None, "high"])
def test_build_launch_preserves_contract_and_captures_sent_prompt(tmp_path, sandbox, effort):
    """C-5.1 C-6.7 C-10.5 C-12.3 C-14.3 Launch records flags, credentials and exact sent bytes."""
    home, workdir, attempt = tmp_path / "home", tmp_path / "work", tmp_path / "attempt"
    prompt = tmp_path / "prompt.md"
    prompt.write_bytes(b"Caller prompt.\r\n")
    credential_env = {"CODEX_HOME": str(home), "SUBFLEET_ATTEMPT": "job/a1", "SUBFLEET_JOB": "job"}
    launch = CodexAdapter(codex_bin="/test/bin/codex").build_launch(
        _job(workdir, prompt, sandbox), "job/a1", attempt, _lane(home), credential_env,
        MODEL, effort, prompt, GUARD_OVERRIDE,
    )
    assert launch.argv[:5] == ("/test/bin/codex", "exec", "--json", "-m", MODEL)
    assert launch.argv[launch.argv.index("--sandbox") + 1] == sandbox.value
    overrides = [launch.argv[index + 1] for index, value in enumerate(launch.argv) if value == "-c"]
    assert GUARD_OVERRIDE in overrides
    assert (f"model_reasoning_effort={effort}" in overrides) == (effort is not None)
    if effort is None:
        assert not any(value.startswith("model_reasoning_effort=") for value in overrides)
    assert launch.argv[launch.argv.index("--output-last-message") + 1] == str(attempt / "last.md")
    assert launch.cwd == str(workdir)
    assert launch.stdin_path == str(attempt / "prompt.sent.md")
    assert Path(launch.stdin_path).read_bytes() == prompt.read_bytes() == b"Caller prompt.\r\n"
    assert launch.stdout_path == str(attempt / "stdout")
    assert launch.stderr_path == str(attempt / "stderr")
    assert launch.raw_stream_path == str(attempt / "stream.jsonl")
    assert launch.env_add == credential_env
    assert launch.env_add is not credential_env
    assert launch.env_remove == ("CODEX_API_KEY", "OPENAI_API_KEY")
    assert launch.native_session_id is None
    assert launch.lane_id == "codex-4"
    assert set(tmp_path.iterdir()) == {prompt, attempt}
    assert list(attempt.iterdir()) == [Path(launch.stdin_path)]


def test_read_only_launch_can_omit_guard(tmp_path):
    """C-12.3 A read-only launch without a guard does not invent a hooks override."""
    (tmp_path / "prompt").write_bytes(b"Caller prompt.\n")
    launch = CodexAdapter().build_launch(
        _job(tmp_path, tmp_path / "prompt"), "job/a1", tmp_path, _lane(tmp_path / "home"),
        {"CODEX_HOME": str(tmp_path / "home")}, MODEL, None, tmp_path / "prompt", None,
    )
    assert not any(value.startswith("hooks=") for value in launch.argv)


def test_workspace_write_requires_guard_override(tmp_path):
    """C-14.3 An executable workspace-write launch requires the guard override."""
    with pytest.raises(AdapterError) as exc:
        CodexAdapter().build_launch(
            _job(tmp_path, tmp_path / "prompt", Sandbox.WORKSPACE_WRITE), "job/a1", tmp_path,
            _lane(tmp_path / "home"), {"CODEX_HOME": str(tmp_path / "home")},
            MODEL, None, tmp_path / "prompt", None,
        )
    assert exc.value.code == 7
    assert exc.value.fix


@pytest.mark.parametrize("sandbox", list(Sandbox))
def test_resume_launch_keeps_home_sandbox_and_guard(tmp_path, sandbox):
    """C-12.3 C-14.3 Native resume preserves the lane home, sandbox and guard override."""
    home = tmp_path / "lane-home"
    prompt = tmp_path / "continuation.md"
    prompt.write_bytes(b"Continue the work.\n")
    launch = CodexAdapter().resume_launch(
        _job(tmp_path, prompt, sandbox), "job/a2", tmp_path / "a2", _lane(home),
        {"CODEX_HOME": str(home)}, THREAD, prompt, GUARD_OVERRIDE,
    )
    assert launch is not None
    assert launch.argv[:2] == ("codex", "exec")
    assert launch.argv[launch.argv.index("resume") + 1] == THREAD
    assert launch.env_add["CODEX_HOME"] == str(home)
    assert launch.argv[launch.argv.index("--sandbox") + 1] == sandbox.value
    assert GUARD_OVERRIDE in launch.argv
    assert "--json" in launch.argv
    assert launch.stdin_path == str(tmp_path / "a2" / "prompt.sent.md")
    assert Path(launch.stdin_path).read_bytes() == prompt.read_bytes()
    assert launch.cwd == str(tmp_path)
    assert launch.native_session_id == THREAD
    assert launch.env_remove == ("CODEX_API_KEY", "OPENAI_API_KEY")
    assert launch.lane_id == "codex-4"


def test_resume_keeps_native_model_instead_of_forwarding_policy_alias(tmp_path):
    """C-1.6 C-12.3 Native resume preserves the thread model instead of sending an unresolved policy pin."""
    home = tmp_path / "lane-home"
    prompt = tmp_path / "continuation.md"
    prompt.write_bytes(b"Continue the work.\n")
    job = replace(_job(tmp_path, prompt), pinned_model="astra")
    launch = CodexAdapter().resume_launch(
        job, "job/a2", tmp_path / "a2", _lane(home), {"CODEX_HOME": str(home)},
        THREAD, prompt, GUARD_OVERRIDE,
    )
    assert "-m" not in launch.argv
    assert "astra" not in launch.argv
    assert launch.argv[-3:] == ("resume", THREAD, "-")


def test_authentication_precedes_admission_and_quota(tmp_path):
    """C-9.2 C-9.3 A revoked refresh token beats concurrent admission and quota errors."""
    launch = _events(tmp_path, {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}},
                     {"type": "error", "message": "Your refresh token was revoked."},
                     stderr="content filter blocked the response\n")
    outcome = CodexAdapter().classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.AUTH_DEAD
    assert outcome.closure is None


def test_content_filter_admission_precedes_quota(tmp_path):
    """C-9.2 C-4.5 Admission content filtering takes precedence over quota-looking evidence."""
    launch = _events(tmp_path, {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}},
                     stderr="Request blocked by content filter.\n")
    outcome = CodexAdapter().classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.CONTENT_FILTER
    assert outcome.closure is None


@pytest.mark.parametrize("message", [
    "Server returned HTTP 503 Service Unavailable", "HTTP 500 Internal Server Error",
    "stream disconnected before completion", "The model is at capacity. Try again later.",
    "DNS resolution failed for chatgpt.com", "TLS certificate verification failed",
    "unexpected status 401 Unauthorized from responses endpoint",
])
def test_transient_failures_do_not_close_lane(tmp_path, message):
    """C-9.3 C-9.5 Network/capacity errors and a non-usage 401 are transient without a closure."""
    launch = _events(tmp_path, {"type": "error", "message": message})
    outcome = CodexAdapter().classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.TRANSIENT
    assert outcome.closure is None


@pytest.mark.parametrize("message", [
    "content filter blocked this request", "trusted access is required",
    "I can't help with that request", "I can’t assist with that request",
])
def test_content_filter_variants_are_not_transient(tmp_path, message):
    """C-9.2 C-4.5 v1 content-filter refusals are classified for reconciliation, not retry."""
    launch = _events(tmp_path, stderr=message)
    outcome = CodexAdapter().classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.CONTENT_FILTER
    assert outcome.closure is None


def test_model_scoped_limit_keeps_reported_reset(tmp_path):
    """C-9.4 C-9.6 A structured model rejection closes that model until the exact provider clock."""
    reset = datetime(2026, 9, 6, 15, 0, tzinfo=UTC)
    launch = _events(tmp_path, {"type": "thread.started", "thread_id": THREAD},
                     {"type": "turn.failed", "error": {"message": "You've hit your usage limit.",
                      "scope": MODEL, "resets_at": int(reset.timestamp())}})
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.native_session_id == THREAD
    assert outcome.closure is not None
    assert outcome.closure.scope == MODEL
    assert outcome.closure.clock_source == ClockSource.REPORTED
    assert outcome.closure.until_at == "2026-09-06T15:00:00Z"
    assert outcome.closure.source_event


def test_unreported_limit_clock_is_one_hour_and_marked_guessed(tmp_path):
    """C-9.4 A clockless limit closes the account for precisely one guessed hour."""
    launch = _events(tmp_path, stderr="Usage limit reached.\n")
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.closure is not None
    assert outcome.closure.scope == "account"
    assert outcome.closure.clock_source == ClockSource.GUESSED
    until = datetime.fromisoformat(outcome.closure.until_at.replace("Z", "+00:00"))
    assert until == NOW + timedelta(hours=1)


def test_success_text_is_not_failure_evidence(tmp_path):
    """C-9.2 C-12.6 An agent discussing limit/auth/filter errors is still a successful deliverable."""
    text = "The tests cover refresh token was revoked, usage limit reached, and content filter failures."
    launch = _events(tmp_path, {"type": "thread.started", "thread_id": THREAD},
                     {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                     {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 15}})
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(0))
    assert outcome.cls == OutcomeClass.OK
    assert outcome.closure is None
    assert outcome.native_session_id == THREAD


def test_recovered_stream_error_allows_completed_success(tmp_path):
    """C-9.2 C-9.5 A recovered transport error does not override a completed successful admission."""
    launch = _events(tmp_path, {"type": "error", "message": "stream disconnected before completion"},
                     {"type": "item.completed", "item": {"type": "agent_message", "text": "Recovered and done."}},
                     {"type": "turn.completed"})
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(0))
    assert outcome.cls == OutcomeClass.OK
    assert outcome.closure is None


@pytest.mark.parametrize("message,expected", [
    ("You've hit your usage limit.", OutcomeClass.LIMITED),
    ("Unrecognized terminal failure", OutcomeClass.UNKNOWN),
])
def test_terminal_failure_wins_over_zero_rc_and_leftover_deliverable(tmp_path, message, expected):
    """C-9.2 C-12.6 A terminal failure cannot be accepted because rc is zero and an older file exists."""
    launch = _events(tmp_path, {"type": "turn.failed", "error": {"message": message}})
    (tmp_path / "last.md").write_text("Left over from before the failure.")
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit(0))
    assert outcome.cls == expected
    assert outcome.evidence["rc"] == 0


@pytest.mark.parametrize("stream", ["", "not json\n", '{"type":"turn.completed"}\n'])
def test_zero_rc_without_deliverable_is_unknown(tmp_path, stream):
    """C-12.6 An empty or damaged successful stream cannot manufacture a deliverable."""
    launch = _launch(tmp_path)
    Path(launch.raw_stream_path).write_text(stream)
    (tmp_path / "last.md").write_bytes(b"")
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(0))
    assert outcome.cls == OutcomeClass.UNKNOWN
    assert outcome.evidence["rc"] == 0


def test_nonzero_rc_cannot_be_success_with_partial_deliverable(tmp_path):
    """C-9.2 C-12.6 A partial artifact does not turn an unclassified nonzero exit into success."""
    (tmp_path / "last.md").write_text("Partial result")
    outcome = CodexAdapter().classify(tmp_path, _launch(tmp_path), _exit(17, 15))
    assert outcome.cls == OutcomeClass.UNKNOWN
    assert outcome.evidence["rc"] == 17
    assert outcome.evidence["signal"] == 15


def test_spawn_failure_keeps_provider_exit_receipt(tmp_path):
    """C-5.2 C-9.2 A spawn failure stays unknown with its raw receipt for diagnosis."""
    outcome = CodexAdapter().classify(tmp_path, _launch(tmp_path), _exit(127, spawn_error="No such file"))
    assert outcome.cls == OutcomeClass.UNKNOWN
    assert outcome.evidence["rc"] == 127
    assert "No such file" in str(outcome.evidence)


def test_deliverable_prefers_exact_last_file_bytes(tmp_path):
    """C-12.6 Last-message capture preserves bytes and takes precedence over stream text."""
    launch = _events(tmp_path, {"type": "item.completed", "item": {"type": "agent_message", "text": "older"}})
    payload = b"# Final report\r\n\nExact bytes: \xff\n"
    (tmp_path / "last.md").write_bytes(payload)
    assert CodexAdapter().deliverable(tmp_path, launch, Outcome(OutcomeClass.OK, "done")) == payload


def test_deliverable_falls_back_to_last_completed_agent_message(tmp_path):
    """C-12.6 Empty last.md falls back to the final completed agent message, ignoring tools."""
    launch = _events(tmp_path,
                     {"type": "item.completed", "item": {"type": "agent_message", "text": "earlier"}},
                     {"type": "item.completed", "item": {"type": "agent_message", "text": "Final π\n"}},
                     {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "tool output"}})
    (tmp_path / "last.md").write_bytes(b"")
    assert CodexAdapter().deliverable(tmp_path, launch, Outcome(OutcomeClass.OK, "done")) == "Final π\n".encode()


def test_deliverable_missing_is_none(tmp_path):
    """C-12.6 No final-message file or completed agent item means no deliverable."""
    assert CodexAdapter().deliverable(tmp_path, _launch(tmp_path), Outcome(OutcomeClass.UNKNOWN, "empty")) is None


@pytest.mark.parametrize("served,expected", [(MODEL, Attestation.ATTESTED), ("gpt-5.6-sol", Attestation.MISMATCH)])
def test_attestation_reads_matching_thread_rollout(tmp_path, served, expected):
    """C-12.5 Served model comes from the same home's matching thread rollout, including mismatches."""
    home = tmp_path / "home"
    sessions = home / "sessions" / "2026" / "09" / "05"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-2026-09-05T12-00-00-{THREAD}.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": THREAD}}) + "\n" +
                       json.dumps({"type": "turn_context", "payload": {"model": served}}) + "\n")
    result = CodexAdapter().attest(tmp_path, _launch(tmp_path, home),
                                  Outcome(OutcomeClass.OK, "done", native_session_id=THREAD), MODEL)
    assert result.status == expected
    assert result.served_model == served
    assert str(rollout) in result.evidence


@pytest.mark.parametrize("thread", [None, THREAD])
def test_attestation_missing_matching_rollout_is_unattested(tmp_path, thread):
    """C-12.5 Requested argv model and unrelated rollouts cannot attest the serving model."""
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-other-thread.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "different-thread"}}) + "\n" +
        json.dumps({"type": "turn_context", "payload": {"model": MODEL}}) + "\n")
    result = CodexAdapter().attest(tmp_path, _launch(tmp_path, home),
                                  Outcome(OutcomeClass.OK, "done", native_session_id=thread), MODEL)
    assert result.status == Attestation.UNATTESTED
    assert result.served_model is None


def test_attestation_does_not_trust_thread_id_only_in_filename(tmp_path):
    """C-12.5 A filename collision cannot attest a rollout whose recorded thread identity differs."""
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"rollout-{THREAD}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "different-thread"}}) + "\n" +
        json.dumps({"type": "turn_context", "payload": {"model": MODEL}}) + "\n")
    result = CodexAdapter().attest(tmp_path, _launch(tmp_path, home),
                                  Outcome(OutcomeClass.OK, "done", native_session_id=THREAD), MODEL)
    assert result.status == Attestation.UNATTESTED


def test_subscription_upgrade_link_is_not_an_old_cli_error(tmp_path):
    """C-9.2 C-9.4 A usage rejection with a subscription upgrade link still closes quota."""
    launch = _events(tmp_path, {"type": "turn.failed", "error": {"message":
        "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/codex) "
        "or buy credits; try again at 2026-09-05T18:00:00Z."}})
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.closure.reason.value == "provider-limit"
    assert outcome.closure.until_at == "2026-09-05T18:00:00Z"


def test_event_timestamp_is_not_a_reported_reset_clock(tmp_path):
    """C-9.4 An observation timestamp alone cannot supply the provider's reset clock."""
    launch = _events(tmp_path, {"type": "turn.failed", "timestamp": "2026-09-05T11:59:00Z",
        "error": {"message": "You've hit your usage limit."}})
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit())
    assert outcome.closure.clock_source == ClockSource.GUESSED
    assert outcome.closure.until_at == "2026-09-05T13:00:00Z"


def test_reported_reset_clock_uses_explicit_timezone(tmp_path):
    """C-9.4 A provider's local reset clock keeps v1's explicit-zone and rollover semantics."""
    launch = _events(tmp_path, {"type": "turn.failed", "error": {"message":
        "You've hit your usage limit. Try again at 6:00 PM (America/Los_Angeles)."}})
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit())
    assert outcome.closure.clock_source == ClockSource.REPORTED
    assert outcome.closure.until_at == "2026-09-06T01:00:00Z"


def test_explicit_account_scope_beats_requested_model_metadata(tmp_path):
    """C-9.4 C-9.6 Account limits retain their explicit scope even when a requested model is recorded."""
    launch = _events(tmp_path, {"type": "turn.failed", "model": MODEL, "error": {
        "message": "Usage limit reached.", "scope": "account", "model": MODEL}})
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit())
    assert outcome.closure.scope == "account"


def test_credit_rejection_code_without_limit_phrase(tmp_path):
    """C-9.4 C-12.7 A structured insufficient-credits rejection closes the account for credits."""
    launch = _events(tmp_path, {"type": "turn.failed", "error": {
        "code": "insufficient_credits", "message": "Request rejected."}})
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.closure.reason.value == "credits"


def test_access_token_error_outside_usage_endpoint_does_not_prove_auth_death(tmp_path):
    """C-9.3 A response endpoint token error lacks the usage-401 or refresh-revocation evidence."""
    launch = _events(tmp_path, {"type": "turn.failed", "error": {
        "code": "token_revoked", "message": "HTTP 401 from responses endpoint."}})
    outcome = CodexAdapter().classify(tmp_path, launch, _exit())
    assert outcome.cls == OutcomeClass.TRANSIENT
    assert outcome.closure is None


def test_spawn_failure_cannot_attest_historic_rollout(tmp_path):
    """C-5.2 C-12.5 A failed provider spawn cannot attest a model from a preexisting thread."""
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {
        "id": THREAD, "model": MODEL}}) + "\n")
    launch = replace(_launch(tmp_path, home), native_session_id=THREAD)
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(127, spawn_error="No executable"))
    assert CodexAdapter().attest(tmp_path, launch, outcome, MODEL).status == Attestation.UNATTESTED


@pytest.mark.parametrize("with_receipts,timestamp", [
    (False, "2026-09-05T11:00:00Z"), (True, "2026-09-05T11:00:00Z"),
    (True, "2026-09-05T12:00:00.500Z"), (True, "2026-09-05T12:01:00.500Z"),
    (True, None),
])
def test_resume_cannot_attest_using_only_old_turns(tmp_path, with_receipts, timestamp):
    """C-12.5 A native continuation needs model evidence belonging to its own recorded attempt."""
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": THREAD, "model": MODEL}}) + "\n" +
        json.dumps({"type": "turn_context", "timestamp": timestamp,
                    "payload": {"model": MODEL}}) + "\n")
    if with_receipts:
        (tmp_path / "start.json").write_text(json.dumps({"started_at": "2026-09-05T12:00:00Z"}))
        (tmp_path / "exit.json").write_text(json.dumps({"finished_at": "2026-09-05T12:01:00Z"}))
    launch = replace(_launch(tmp_path, home), native_session_id=THREAD)
    outcome = Outcome(OutcomeClass.OK, "done", native_session_id=THREAD)
    assert CodexAdapter().attest(tmp_path, launch, outcome, MODEL).status == Attestation.UNATTESTED


@pytest.mark.parametrize("served,expected", [(MODEL, Attestation.ATTESTED), ("gpt-5.6-sol", Attestation.MISMATCH)])
def test_resume_attestation_uses_only_current_receipt_interval(tmp_path, served, expected):
    """C-12.5 Old and later turns cannot change the model attestation for this resumed attempt."""
    home = tmp_path / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": THREAD, "model": "gpt-5.6-sol"}},
        {"type": "turn_context", "timestamp": "2026-09-05T11:00:00Z", "payload": {"model": "gpt-5.6-sol"}},
        {"type": "turn_context", "timestamp": "2026-09-05T12:00:30Z", "payload": {"model": served}},
        {"type": "turn_context", "timestamp": "2026-09-05T13:00:00Z", "payload": {"model": "gpt-5.6-sol"}},
    ]
    (sessions / "rollout.jsonl").write_text("".join(json.dumps(event) + "\n" for event in records))
    (tmp_path / "start.json").write_text(json.dumps({"started_at": "2026-09-05T12:00:00Z"}))
    (tmp_path / "exit.json").write_text(json.dumps({"finished_at": "2026-09-05T12:01:00Z"}))
    launch = replace(_launch(tmp_path, home), native_session_id=THREAD)
    result = CodexAdapter().attest(tmp_path, launch, Outcome(OutcomeClass.OK, "done", native_session_id=THREAD), MODEL)
    assert result.status == expected
    assert result.served_model == served
