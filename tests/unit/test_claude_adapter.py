"""C-6.7, C-9.1 to C-9.8, C-10.2, C-12.4, C-12.6, C-14.3: the Claude adapter.

Every fixture under `tests/fixtures/claude/` is driven to the `expected.json` beside
it, and the launch line is compared against v1's own, reconstructed from
`bin/subfleet-claude`.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet.adapters import claude as claude_adapter
from subfleet.adapters.claude import (
    ADMISSION_WINDOW, ENROLL_MODEL, ENROLL_PROMPT, ENV_REMOVE, HEADLESS_BLOCK,
    ClaudeAdapter, apply_headless_block, encode_project_dir, iso_from_epoch, iso_utc,
    model_matches_requested, parse_reset_clock, reconstruct_v1_argv,
)
from subfleet.adapters.base import AdapterError
from subfleet.contracts import (
    GUESSED_CLOSURE_S, HEADLESS_MARKER, Attestation, ClockSource, ClosureReason,
    Credential, OutcomeClass, ReadingLabel, Sandbox,
)
from tests.conftest import (
    FIXTURES, NOW, case_names, exit_info, load_expected, make_job, make_lane,
    make_launch, stage_case, stage_transcript,
)


# --- the fixture corpus ------------------------------------------------------


def _classify(adapter, tmp_path, case, *, with_transcript=True):
    expected = load_expected(case)
    attempt_dir, rc = stage_case(case, tmp_path / "a1")
    session_id = expected["session_id"]
    projects = adapter._config_projects_dir()
    if with_transcript:
        stage_transcript(case, projects, session_id)
    launch = make_launch(
        attempt_dir, session_id=session_id, model_id=expected["requested_model"],
        projects_dir=projects,
    )
    return expected, launch, adapter.classify(attempt_dir, launch, exit_info(rc))


@pytest.mark.parametrize("case", case_names())
def test_fixture_classifies_to_its_expected_class(adapter, tmp_path, case):
    """C-9.2 every fixture reaches the outcome class its `expected.json` records, and
    the raw rc rides along with it."""
    expected, _launch, outcome = _classify(adapter, tmp_path, case)
    assert outcome.cls == OutcomeClass(expected["class"]), outcome.detail
    assert expected["detail_contains"].lower() in outcome.detail.lower()
    assert outcome.evidence["rc"] == expected["rc"]


@pytest.mark.parametrize("case", case_names())
def test_fixture_readings_match(adapter, tmp_path, case):
    """C-9.1, C-9.8 the readings a fixture yields are exactly the recorded ones —
    fractions, labels, scopes, windows, and ISO clocks."""
    expected, _launch, outcome = _classify(adapter, tmp_path, case)
    got = [
        {
            "scope": r.scope,
            "window": r.window,
            "utilization": r.utilization,
            "resets_at": r.resets_at,
            "label": r.label.value,
            "source": r.source,
        }
        for r in outcome.readings
    ]
    want = [
        {
            "scope": r["scope"],
            "window": r["window"],
            "utilization": r["utilization"],
            "resets_at": (
                r.get("resets_at") if "resets_at" in r
                else iso_from_epoch(r["resets_at_epoch"])
            ),
            "label": r["label"],
            "source": r["source"],
        }
        for r in expected["readings"]
    ]
    assert got == want
    for reading in outcome.readings:
        assert reading.lane_id == "claude-1"
        assert reading.attempt_id == "job-1/a1"
        assert reading.observed_at == iso_utc(NOW)


@pytest.mark.parametrize("case", case_names())
def test_fixture_closure_matches(adapter, tmp_path, case):
    """C-9.4, C-9.6 a `limited` fixture produces a closure with the recorded scope,
    reason, clock, and clock source; no other class produces one (C-9.5)."""
    expected, _launch, outcome = _classify(adapter, tmp_path, case)
    want = expected["closure"]
    if want is None:
        assert outcome.closure is None
        return
    closure = outcome.closure
    assert closure is not None
    assert closure.lane_id == "claude-1"
    assert closure.scope == want["scope"]
    assert closure.reason == ClosureReason(want["reason"])
    assert closure.clock_source == ClockSource(want["clock_source"])
    assert closure.source_event == want["source_event"]
    if "until_at_epoch" in want:
        assert closure.until_at == iso_from_epoch(want["until_at_epoch"])
    elif "until_at_offset_s" in want:
        assert closure.until_at == iso_utc(
            NOW + timedelta(seconds=want["until_at_offset_s"])
        )
    else:
        assert closure.until_at == want["until_at"]


@pytest.mark.parametrize("case", case_names())
def test_fixture_deliverable_matches(adapter, tmp_path, case):
    """C-12.6 the deliverable is the attempt's final assistant text, or nothing."""
    expected, launch, outcome = _classify(adapter, tmp_path, case)
    got = adapter.deliverable(Path(launch.stdout_path).parent, launch, outcome)
    want = expected["deliverable"]
    assert got == (want.encode("utf-8") if want is not None else None)


@pytest.mark.parametrize("case", case_names())
def test_fixture_attestation_matches(adapter, tmp_path, case):
    """C-12.5 attestation is what the fixture's transcript (or its absence) proves."""
    expected, launch, outcome = _classify(adapter, tmp_path, case)
    result = adapter.attest(
        Path(launch.stdout_path).parent, launch, outcome, expected["requested_model"]
    )
    assert result.status == Attestation(expected["attestation"]["status"]), result.evidence
    assert result.served_model == expected["attestation"]["served_model"]
    assert result.evidence


# --- the four experiment-0 payloads (milestone 2, C-21) ----------------------


@pytest.mark.parametrize(
    "case,windows",
    [
        ("success-allowed", {"five_hour": 0.05, "seven_day": 0.25}),
        ("allowed-out-of-credits-overage", {"five_hour": 0.29, "seven_day": 0.18}),
        ("allowed-on-table-exhausted-lane", {"five_hour": 0.42, "seven_day": 0.33}),
    ],
)
def test_allowed_experiment_zero_payloads_give_two_provider_readings(
    adapter, tmp_path, case, windows
):
    """C-9.8, C-21 milestone 2: each allowed payload yields two `provider` readings,
    scope `account`, with the server's fractions and ISO 8601 UTC clocks."""
    _expected, _launch, outcome = _classify(adapter, tmp_path, case)
    assert outcome.cls is OutcomeClass.OK
    assert len(outcome.readings) == 2
    assert {r.window: r.utilization for r in outcome.readings} == windows
    for reading in outcome.readings:
        assert reading.label is ReadingLabel.PROVIDER
        assert reading.scope == "account"
        assert reading.source == "rate_limit_event"
        assert reading.resets_at is not None
        assert reading.resets_at.endswith("Z")
        datetime.fromisoformat(reading.resets_at.replace("Z", "+00:00"))
        assert 0.0 <= reading.utilization <= 1.0


def test_rejected_experiment_zero_payload_closes_the_model_scope(adapter, tmp_path):
    """C-9.8, C-21 milestone 2: the rejected Fable probe becomes `limited`, scope
    `claude-fable-5-1`, reason `credits`, `clock_source: reported`, and yields no
    utilization reading."""
    _expected, _launch, outcome = _classify(adapter, tmp_path, "rejected-credits-fable")
    assert outcome.cls is OutcomeClass.LIMITED
    closure = outcome.closure
    assert closure is not None
    assert closure.scope == "claude-fable-5-1"
    assert closure.reason is ClosureReason.CREDITS
    assert closure.clock_source is ClockSource.REPORTED
    assert closure.until_at == iso_from_epoch(1790812800) == "2026-10-01T00:00:00Z"
    assert closure.source_event == "rate_limit_event"

    assert len(outcome.readings) == 1
    reading = outcome.readings[0]
    assert reading.label is ReadingLabel.ADMISSION_OBSERVED
    assert reading.scope == "claude-fable-5-1"
    assert reading.window == ADMISSION_WINDOW
    assert reading.utilization is None
    assert all(r.utilization is None for r in outcome.readings)
    assert outcome.evidence["rate_limit"]["error_code"] == "credits_required"


def test_a_cached_exhausted_table_does_not_survive_a_live_reading(adapter, tmp_path):
    """C-9.1 the lane v1's cached table called EXHAUSTED at 147% of the week reports
    0.42 and 0.33 and is admitted: only a `provider` reading is capacity evidence."""
    _e, _l, outcome = _classify(adapter, tmp_path, "allowed-on-table-exhausted-lane")
    assert outcome.cls is OutcomeClass.OK
    assert outcome.closure is None
    assert max(r.utilization for r in outcome.readings) < 0.5


def test_the_limit_closure_clock_equals_the_events_clock(adapter, tmp_path):
    """C-9.4, C-21 milestone 2: a limit fixture's `until_at` is the event's own clock."""
    _e, _l, outcome = _classify(adapter, tmp_path, "limit-weekly-with-clock")
    assert outcome.closure.until_at == iso_from_epoch(1789056000)
    assert outcome.closure.clock_source is ClockSource.REPORTED


def test_a_limit_without_a_clock_is_guessed_at_now_plus_3600(adapter, tmp_path):
    """C-9.4, C-21 milestone 2: no clock anywhere means now + 3600 s, marked guessed."""
    _e, _l, outcome = _classify(adapter, tmp_path, "limit-no-clock")
    assert outcome.closure.clock_source is ClockSource.GUESSED
    assert outcome.closure.until_at == iso_utc(NOW + timedelta(seconds=GUESSED_CLOSURE_S))
    assert GUESSED_CLOSURE_S == 3600


def test_a_prose_clock_counts_as_reported(adapter, tmp_path):
    """C-9.4 the provider reported the clock, in words: `reported`, not `guessed`."""
    _e, _l, outcome = _classify(adapter, tmp_path, "limit-session-with-clock")
    assert outcome.closure.clock_source is ClockSource.REPORTED
    assert outcome.closure.until_at == "2026-09-05T22:40:00Z"   # 6:40pm America/New_York
    assert outcome.closure.scope == "account"


# --- classification precedence (C-9.2, C-9.3) -------------------------------


def test_an_organisation_block_is_auth_dead_even_with_system_init(adapter, tmp_path):
    """C-9.3 an explicit organisation-block message is auth evidence on its own."""
    _e, _l, outcome = _classify(adapter, tmp_path, "org-block")
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    assert outcome.evidence["system_init"] is True
    assert outcome.closure is None          # the adapter never invents an auth hold


def test_a_401_signature_with_system_init_is_not_auth_dead(adapter, tmp_path):
    """C-9.3 a credential-shaped signature alongside a successful `system/init` means
    the credential works; something else answered 401. Never a 30-day cooldown."""
    _e, _l, outcome = _classify(adapter, tmp_path, "auth-signature-false-positive")
    assert outcome.cls is OutcomeClass.TRANSIENT
    assert outcome.evidence["auth_signature_false_positive"]
    assert outcome.closure is None
    # The sensor still reported, so the lane's capacity is not lost to the failure.
    assert len(outcome.readings) == 2


def test_a_401_without_system_init_is_auth_dead(adapter, tmp_path):
    """C-9.3 no `system/init` and a 401: the credential is dead."""
    _e, _l, outcome = _classify(adapter, tmp_path, "auth-401")
    assert outcome.cls is OutcomeClass.AUTH_DEAD
    assert outcome.evidence["system_init"] is False


def test_the_host_version_gate_never_cools_a_lane(adapter, tmp_path):
    """C-9.2 a CLI too old for the model is the host's fault: `cli-too-old`, no closure,
    no reading, so the lane stays a candidate for every other model."""
    for case in ("cli-too-old", "cli-too-old-current"):
        _e, _l, outcome = _classify(adapter, tmp_path / case, case)
        assert outcome.cls is OutcomeClass.CLI_TOO_OLD
        assert outcome.closure is None
        assert outcome.readings == ()


def test_a_throttled_server_is_transient_not_limited(adapter, tmp_path):
    """C-9.5 'Server is temporarily limiting requests (not your usage limit)' contains
    the words 'usage limit' and is not one: transient, and never a closure."""
    _e, _l, outcome = _classify(adapter, tmp_path, "transient-server-throttle")
    assert outcome.cls is OutcomeClass.TRANSIENT
    assert outcome.closure is None


def test_a_stream_disconnect_is_transient(adapter, tmp_path):
    """C-9.5 stream drops and connection failures are retryable and write no closure."""
    _e, _l, outcome = _classify(adapter, tmp_path, "stream-disconnect")
    assert outcome.cls is OutcomeClass.TRANSIENT
    assert outcome.closure is None
    assert outcome.evidence["stream_truncated_tail"] is True


def test_an_empty_deliverable_with_rc_zero_is_unknown(adapter, tmp_path):
    """C-12.6 rc 0 with nothing delivered is `unknown`, not `ok` — and the readings
    the sensor produced survive the failure."""
    _e, _l, outcome = _classify(adapter, tmp_path, "empty-result-rc0")
    assert outcome.cls is OutcomeClass.UNKNOWN
    assert len(outcome.readings) == 2
    assert all(r.label is ReadingLabel.PROVIDER for r in outcome.readings)


def test_a_refusal_is_content_filter(adapter, tmp_path):
    """C-9.2 `stop_reason: refusal` is `content-filter`: not a lane fault, not a retry."""
    _e, _l, outcome = _classify(adapter, tmp_path, "content-filter")
    assert outcome.cls is OutcomeClass.CONTENT_FILTER
    assert outcome.closure is None


def test_a_spawn_failure_is_unknown_with_its_rc(adapter, tmp_path):
    """C-5.2, C-9.2 rc 127 with no stream classifies as `unknown` and keeps the rc and
    the guardian's spawn error."""
    attempt_dir, rc = stage_case("spawn-failure", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id="e" * 8 + "-eeee-4eee-8eee-eeeeeeeeeeee",
                         model_id="claude-opus-5")
    outcome = adapter.classify(
        attempt_dir, launch, exit_info(rc, spawn_error="No such file or directory")
    )
    assert outcome.cls is OutcomeClass.UNKNOWN
    assert outcome.evidence["rc"] == 127
    assert outcome.evidence["spawn_error"] == "No such file or directory"


def test_stderr_noise_never_demotes_a_successful_run(adapter, tmp_path):
    """C-9.2 a real, benign stderr warning on an rc-0 run stays `ok`."""
    _e, _l, outcome = _classify(adapter, tmp_path, "ok-with-background-task-warning")
    assert outcome.cls is OutcomeClass.OK
    assert outcome.closure is None


def test_overage_is_never_admission_evidence(adapter, tmp_path):
    """C-9.8 an event whose overage lane is refused and out of credits, while `status`
    is `allowed`, is admitted: `overageStatus` is recorded and nothing more."""
    _e, _l, outcome = _classify(adapter, tmp_path, "allowed-out-of-credits-overage")
    assert outcome.cls is OutcomeClass.OK
    assert outcome.closure is None
    assert outcome.evidence["rate_limit"]["overage_status"] == "rejected"
    assert outcome.evidence["rate_limit"]["overage_disabled_reason"] == "out_of_credits"


def test_a_rejection_keeps_its_windows_in_evidence_not_in_readings(adapter, tmp_path):
    """C-9.8 a rejected event yields no utilization reading; its window numbers are
    kept in the outcome's evidence so nothing is lost and nothing is misread."""
    _e, _l, outcome = _classify(adapter, tmp_path, "limit-weekly-with-clock")
    assert [r.label for r in outcome.readings] == [ReadingLabel.ADMISSION_OBSERVED]
    windows = outcome.evidence["rate_limit"]["windows"]
    assert windows["five_hour"]["utilization"] == 0.61
    assert windows["seven_day"]["utilization"] == 1.0


# --- no percentages anywhere (C-9.1, C-21 milestone 2) ----------------------


def test_no_adapter_code_path_renders_a_percentage():
    """C-9.1, C-21 milestone 2: readings carry fractions and labels only. Rendering is
    the routing lane's job, and it must find nothing else to render."""
    for module in (claude_adapter, __import__(
        "subfleet.adapters.claude_stream", fromlist=["x"]
    )):
        source = Path(module.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        # Strip docstrings before looking for formatting: prose may say "percentage".
        code = re.sub(r'"""(?:.|\n)*?"""', "", code)
        assert "* 100" not in code, f"{module.__name__} multiplies a fraction by 100"
        assert "100 *" not in code
        assert "used_percent" not in code
        assert not re.search(r'["\'][^"\']*%[^"\']*["\']', code), (
            f"{module.__name__} formats a percentage"
        )


def test_readings_are_fractions_not_percentages(adapter, tmp_path):
    """C-9.1 the fraction on a reading is the server's own number, unscaled."""
    _e, _l, outcome = _classify(adapter, tmp_path, "success-allowed")
    assert sorted(r.utilization for r in outcome.readings) == [0.05, 0.25]


# --- launch construction (C-12.4, C-6.7) ------------------------------------


def _build(adapter, tmp_path, sandbox, prompt="Do the thing.\n", *, effort=None):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    attempt_dir = tmp_path / "a1"
    job = make_job(str(workdir), str(prompt_path), sandbox=sandbox)
    return adapter.build_launch(
        job, "job-1/a1", attempt_dir, make_lane(),
        {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-REDACTED"},
        "claude-opus-5", effort, prompt_path, None,
    )


@pytest.mark.parametrize("sandbox", [Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE])
def test_launch_argv_equals_v1s_line_for_the_same_inputs(adapter, tmp_path, sandbox):
    """C-12.4, C-21 milestone 2: for each sandbox the argv equals v1's, rebuilt from
    `bin/subfleet-claude`, apart from the two documented v2 changes (stream-json and
    --verbose, so the rate_limit_event is readable)."""
    launch = _build(adapter, tmp_path, sandbox)
    assert launch.argv == reconstruct_v1_argv(
        str(adapter.claude_bin), "claude-opus-5", launch.native_session_id, sandbox.value,
    )


def test_isolated_read_only_matches_v1s_isolated_permission_args(adapter):
    """C-12.4 v1's isolated review branch: no web tools, plus the review root."""
    assert adapter.permission_args(
        Sandbox.READ_ONLY, isolated=True, review_root="/tmp/review"
    ) == reconstruct_v1_argv(
        "claude", "m", "s", "read-only", isolated=True, review_root="/tmp/review",
    )[9:]


def test_read_only_fails_closed_on_the_tool_surface(adapter, tmp_path):
    """C-12.4 a read-only launch names its tools twice and removes every settings-driven
    channel, so an operator's bypassPermissions default cannot reintroduce a writer."""
    argv = _build(adapter, tmp_path, Sandbox.READ_ONLY).argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep,WebSearch,WebFetch"
    for flag in ("--safe-mode", "--no-chrome", "--strict-mcp-config",
                 "--disable-slash-commands", "--setting-sources"):
        assert flag in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'


def test_workspace_write_takes_the_bypass_flag(adapter, tmp_path):
    """C-12.4 workspace-write is exactly v1's single flag; the never-rules hook in
    `~/.claude/settings.json` is what constrains it (C-14.3)."""
    argv = _build(adapter, tmp_path, Sandbox.WORKSPACE_WRITE).argv
    assert argv[-1] == "--dangerously-skip-permissions"
    assert "--permission-mode" not in argv


def test_launch_carries_the_credential_only_in_env_add(adapter, tmp_path):
    """C-10.5, C-12.4 the credential value appears in `env_add` and nowhere else, and
    `ANTHROPIC_API_KEY` is removed from the child's environment."""
    launch = _build(adapter, tmp_path, Sandbox.READ_ONLY)
    assert launch.env_add == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-REDACTED"}
    assert "ANTHROPIC_API_KEY" in launch.env_remove
    assert "ANTHROPIC_AUTH_TOKEN" in launch.env_remove
    assert ENV_REMOVE == launch.env_remove
    blob = json.dumps([launch.argv, launch.notes, launch.cwd, launch.stdin_path])
    assert "sk-ant-oat01-REDACTED" not in blob


def test_launch_paths_and_session_id(adapter, tmp_path):
    """C-12.2, C-12.4 the raw stream, stdout, stderr, cwd, and the chosen session id."""
    launch = _build(adapter, tmp_path, Sandbox.READ_ONLY)
    attempt_dir = tmp_path / "a1"
    assert launch.raw_stream_path == str(attempt_dir / "stream.jsonl")
    assert launch.stdout_path == str(attempt_dir / "stdout")
    assert launch.stderr_path == str(attempt_dir / "stderr")
    assert launch.stdin_path == str(attempt_dir / "prompt.sent.md")
    assert launch.cwd == str(tmp_path / "work")
    assert launch.native_session_id == "00000000-0000-4000-8000-000000000000"
    assert launch.argv[launch.argv.index("--session-id") + 1] == launch.native_session_id


def test_launch_records_the_expected_transcript_and_its_offset(adapter, tmp_path):
    """C-12.5 the transcript path and the byte offset at launch, so a resumed session's
    earlier turns can never be read as this attempt's."""
    projects = adapter._config_projects_dir()
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    encoded = encode_project_dir(str(workdir.resolve()))
    (projects / encoded).mkdir(parents=True, exist_ok=True)
    existing = projects / encoded / "00000000-0000-4000-8000-000000000000.jsonl"
    existing.write_text("x" * 512, encoding="utf-8")

    launch = _build(adapter, tmp_path, Sandbox.READ_ONLY)
    assert launch.notes["transcript_path"] == str(existing)
    assert launch.notes["transcript_offset"] == 512
    assert launch.notes["lane_id"] == "claude-1"
    assert launch.notes["attempt_id"] == "job-1/a1"
    assert launch.notes["model_id"] == "claude-opus-5"


def test_effort_is_passed_through_only_when_the_caller_asks(adapter, tmp_path):
    """C-12.4 the contract's Claude line carries no effort; when the router pins one,
    it is emitted after `--verbose` and before the permission flags, so the v1 parity
    of the permission block is untouched."""
    plain = _build(adapter, tmp_path, Sandbox.READ_ONLY)
    assert "--effort" not in plain.argv
    with_effort = _build(adapter, tmp_path, Sandbox.READ_ONLY, effort="high")
    index = with_effort.argv.index("--effort")
    assert with_effort.argv[index + 1] == "high"
    assert with_effort.argv[index - 1] == "--verbose"


# --- the prompt actually sent (C-6.7) ---------------------------------------


def test_the_headless_block_is_prepended_and_prompt_md_is_untouched(adapter, tmp_path):
    """C-6.7 `prompt.md` keeps the caller's bytes; the headless block goes into
    `prompt.sent.md`, which is the launch's stdin."""
    original = "Summarise the diff.\n"
    launch = _build(adapter, tmp_path, Sandbox.READ_ONLY, original)
    assert (tmp_path / "prompt.md").read_text(encoding="utf-8") == original
    sent = Path(launch.stdin_path).read_text(encoding="utf-8")
    assert sent.startswith(HEADLESS_MARKER)
    assert sent.endswith(original)
    assert "HEADLESS EXECUTION" in sent
    assert "run_in_background" in sent


def test_a_prompt_that_already_carries_the_marker_is_sent_verbatim(adapter, tmp_path):
    """C-6.7 the marker line suppresses the prepend, so a caller who wrote their own
    headless block does not get two."""
    original = f"{HEADLESS_MARKER}\nMy own rules.\nDo the thing.\n"
    launch = _build(adapter, tmp_path, Sandbox.READ_ONLY, original)
    assert Path(launch.stdin_path).read_text(encoding="utf-8") == original


def test_apply_headless_block_is_idempotent():
    """C-6.7 applying the block twice changes nothing the second time."""
    once = apply_headless_block("body")
    assert apply_headless_block(once) == once
    assert HEADLESS_BLOCK.startswith(HEADLESS_MARKER)


def test_prompt_sent_is_written_atomically_with_owner_only_mode(adapter, tmp_path):
    """C-2.3, C-8.1 the sent prompt is an artifact: mode 0600, and no temp file left."""
    launch = _build(adapter, tmp_path, Sandbox.READ_ONLY)
    sent = Path(launch.stdin_path)
    assert sent.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in sent.parent.iterdir() if p.name.startswith(".")] == []


# --- resume (C-12.4) ---------------------------------------------------------


def test_resume_launch_uses_resume_not_session_id(adapter, tmp_path):
    """C-12.4 `claude -p --resume <session id>` on the same lane, same model, same
    permission flags; `--session-id` is not passed alongside `--resume`."""
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("continue\n", encoding="utf-8")
    job = make_job(str(workdir), str(prompt), sandbox=Sandbox.WORKSPACE_WRITE)
    launch = adapter.resume_launch(
        job, "job-1/a2", tmp_path / "a2", make_lane(),
        {"CLAUDE_CODE_OAUTH_TOKEN": "t"}, "abc-123", prompt, None,
        model_id="claude-opus-5",
    )
    assert launch is not None
    assert "--session-id" not in launch.argv
    assert launch.argv[:4] == (str(adapter.claude_bin), "-p", "--resume", "abc-123")
    assert launch.argv[-1] == "--dangerously-skip-permissions"
    assert launch.argv[launch.argv.index("--model") + 1] == "claude-opus-5"
    assert launch.native_session_id == "abc-123"
    assert launch.notes["resumed_from"] == "abc-123"
    assert Path(launch.stdin_path).read_text(encoding="utf-8").startswith(HEADLESS_MARKER)


def test_resume_falls_back_to_the_jobs_pinned_model(adapter, tmp_path):
    """C-4.6 an attempt never changes model; with no explicit id the job's pin is used."""
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("continue\n", encoding="utf-8")
    job = make_job(str(workdir), str(prompt), pinned_model="claude-fable-5-1")
    launch = adapter.resume_launch(
        job, "job-1/a2", tmp_path / "a2", make_lane(), {}, "abc-123", prompt, None,
    )
    assert launch.argv[launch.argv.index("--model") + 1] == "claude-fable-5-1"


# --- helpers (C-1.7, C-12.5) -------------------------------------------------


def test_iso_helpers_produce_the_stores_timestamp_form():
    """C-1.7 ISO 8601 UTC, `Z` suffix, second precision, epoch seconds converted."""
    assert iso_utc(datetime(2026, 9, 5, 16, 0, 0, 123456, tzinfo=timezone.utc)) == (
        "2026-09-05T16:00:00Z"
    )
    assert iso_from_epoch(1788624000) == "2026-09-05T16:00:00Z"
    assert iso_from_epoch(None) is None
    assert iso_utc(
        datetime(2026, 9, 5, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
    ) == "2026-09-05T16:00:00Z"


def test_project_dir_encoding_matches_claude_codes_own():
    """C-12.5 `/`, `.` and `_` each become `-`; case is preserved. Verified against a
    live transcript whose own `cwd` field names the source path."""
    assert encode_project_dir(
        "/Users/maxghenis/PolicyEngine/_buildo-runtime/out/candidate-26/continuation-v2"
    ) == "-Users-maxghenis-PolicyEngine--buildo-runtime-out-candidate-26-continuation-v2"
    assert encode_project_dir("/Users/maxghenis/.axiom/worktrees/x") == (
        "-Users-maxghenis--axiom-worktrees-x"
    )
    assert encode_project_dir("/Users/maxghenis/CosilicoAI") == "-Users-maxghenis-CosilicoAI"


@pytest.mark.parametrize(
    "served,requested,expected",
    [
        ("claude-opus-5", "claude-opus-5", True),
        ("claude-opus-5-20260101", "claude-opus-5", True),
        ("claude-fable-5-1", "claude-opus-5", False),
        ("claude-opus-5", "opus", True),
        ("claude-haiku-4-5-20251001", "haiku", True),
        ("claude-opus-5", "fable", False),
        (None, "claude-opus-5", False),
        ("claude-opus-5", None, False),
        ("", "claude-opus-5", False),
    ],
)
def test_model_matching_is_v1s_rule(served, requested, expected):
    """C-12.5 v1's `model_matches_requested`, including the short-alias branch."""
    assert model_matches_requested(served, requested) is expected


def test_reset_clock_parsing_handles_zones_and_rollover():
    """C-9.4 v1's `parse_reset_clock`, ported: the stated zone wins, and a clock already
    past rolls to the next day."""
    now = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)     # 4pm America/New_York
    assert iso_utc(parse_reset_clock(
        "resets 6:40pm (America/New_York)", now)) == "2026-09-05T22:40:00Z"
    assert iso_utc(parse_reset_clock(
        "try again at 11:33 PM (America/New_York)", now)) == "2026-09-06T03:33:00Z"
    # 3pm New York is already past 4pm New York: tomorrow.
    assert iso_utc(parse_reset_clock(
        "resets 3:00pm (America/New_York)", now)) == "2026-09-06T19:00:00Z"
    assert parse_reset_clock("no clock here", now) is None
    assert parse_reset_clock("resets 13:70pm (America/New_York)", now) is None


def test_the_epoch_limit_form_is_understood(adapter, tmp_path):
    """C-9.4 Claude's `Claude AI usage limit reached|<epoch>` form supplies a reported
    clock rather than a guessed one."""
    attempt = tmp_path / "a1"
    attempt.mkdir()
    (attempt / "stream.jsonl").write_text(json.dumps({
        "type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5",
    }) + "\n" + json.dumps({
        "type": "result", "subtype": "success", "is_error": True, "session_id": "s",
        "result": "Claude AI usage limit reached|1788624000",
    }) + "\n", encoding="utf-8")
    (attempt / "stderr").write_text("", encoding="utf-8")
    launch = make_launch(attempt, session_id="s", model_id="claude-opus-5")
    outcome = adapter.classify(attempt, launch, exit_info(1))
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure.clock_source is ClockSource.REPORTED
    assert outcome.closure.until_at == "2026-09-05T16:00:00Z"


# --- enrolment (C-10.2) ------------------------------------------------------


class _Runner:
    """A `subprocess.run` stand-in that answers the keychain and the Haiku turn."""

    def __init__(self, *, token="sk-ant-oat01-REDACTED", token_rc=0,
                 stdout="", stderr="", rc=0):
        self.token, self.token_rc = token, token_rc
        self.stdout, self.stderr, self.rc = stdout, stderr, rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0].endswith("security"):
            return subprocess.CompletedProcess(argv, self.token_rc, self.token, "")
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def _enroll_stream(session_id="s", model=ENROLL_MODEL, info=None):
    rows = [
        {"type": "system", "subtype": "init", "session_id": session_id, "model": model},
        {"type": "assistant", "session_id": session_id,
         "message": {"model": model, "content": [{"type": "text", "text": "ok"}]}},
    ]
    if info is not None:
        rows.append({"type": "rate_limit_event", "session_id": session_id,
                     "rate_limit_info": info})
    rows.append({"type": "result", "subtype": "success", "is_error": False,
                 "result": "ok", "session_id": session_id})
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def test_enroll_runs_one_haiku_turn_and_reads_the_sensor(tmp_path):
    """C-10.2 one Haiku turn under the token with `--output-format stream-json
    --verbose`, then the `rate_limit_event`."""
    info = json.loads(
        (FIXTURES / "success-allowed" / "stdout").read_text(encoding="utf-8")
        .splitlines()[2]
    )["rate_limit_info"]
    runner = _Runner(stdout=_enroll_stream(info=info))
    adapter = ClaudeAdapter(runner=runner, now=lambda: NOW, projects_dir=tmp_path)
    lane_info = adapter.enroll(
        Credential(provider="claude", ref="claude-quota-max@axiom.org",
                   kind="keychain-token")
    )
    assert lane_info.account_key == "claude:max@axiom.org"
    assert {r.window: r.utilization for r in lane_info.readings} == {
        "five_hour": 0.05, "seven_day": 0.25
    }
    turn = [c for c in runner.calls if c[0] != "security"][0]
    assert turn[1:] == [
        "-p", ENROLL_PROMPT, "--model", ENROLL_MODEL,
        "--output-format", "stream-json", "--verbose", "--max-turns", "1",
    ]


def test_enroll_refuses_when_system_init_never_arrives(tmp_path):
    """C-10.2, C-9.3 no `system/init` means the credential never authenticated: exit 5."""
    runner = _Runner(stdout="", stderr="API Error: 401 Unauthorized", rc=1)
    adapter = ClaudeAdapter(runner=runner, now=lambda: NOW, projects_dir=tmp_path)
    with pytest.raises(AdapterError) as caught:
        adapter.enroll(Credential(provider="claude", ref="claude-quota-max@axiom.org",
                                  kind="keychain-token"))
    assert caught.value.code == 5
    assert "401" in str(caught.value)
    assert caught.value.fix


def test_enroll_refuses_an_organisation_blocked_account(tmp_path):
    """C-9.3 an organisation block is auth-dead at enrolment too, `system/init` or not."""
    stream = _enroll_stream()[:-1] + "\n" + json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "session_id": "s",
        "errors": ["Your organization has disabled Claude subscription access for "
                   "Claude Code"],
    }) + "\n"
    adapter = ClaudeAdapter(runner=_Runner(stdout=stream, rc=1), now=lambda: NOW,
                            projects_dir=tmp_path)
    with pytest.raises(AdapterError) as caught:
        adapter.enroll(Credential(provider="claude", ref="claude-quota-max@x.org",
                                  kind="keychain-token"))
    assert caught.value.code == 5


def test_enroll_refuses_a_missing_keychain_item_without_naming_a_value(tmp_path):
    """C-10.5 a missing credential is exit 5 with the re-enrolment fix, and nothing
    about the item's contents."""
    adapter = ClaudeAdapter(runner=_Runner(token="", token_rc=1), now=lambda: NOW,
                            projects_dir=tmp_path)
    with pytest.raises(AdapterError) as caught:
        adapter.enroll(Credential(provider="claude", ref="claude-quota-nobody@x.org",
                                  kind="keychain-token"))
    assert caught.value.code == 5
    assert "claude setup-token" in (caught.value.fix or "")


def test_enroll_verifies_a_home_lanes_account_against_its_config(tmp_path):
    """C-1.3, C-1.4 a home lane's account comes from its own `.claude.json`; a
    reference that contradicts it is refused rather than silently rebound."""
    home = tmp_path / "lane-home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "max@thesisinstitute.org"}}),
        encoding="utf-8",
    )
    adapter = ClaudeAdapter(runner=_Runner(stdout=_enroll_stream()), now=lambda: NOW,
                            projects_dir=tmp_path)
    info = adapter.enroll(Credential(provider="claude", ref=str(home), kind="home"))
    assert info.account_key == "claude:max@thesisinstitute.org"
    assert info.home == str(home)


def test_enroll_rejects_a_non_claude_credential(tmp_path):
    """C-1.6 providers are exactly `codex` and `claude`; the wrong one is exit 2."""
    adapter = ClaudeAdapter(runner=_Runner(), now=lambda: NOW, projects_dir=tmp_path)
    with pytest.raises(AdapterError) as caught:
        adapter.enroll(Credential(provider="codex", ref="/Users/max/.codex", kind="home"))
    assert caught.value.code == 2


# --- probing (C-9.1, C-11.4) -------------------------------------------------


def test_probe_with_model_asks_about_the_model_the_job_wants(tmp_path):
    """C-11.4 before expensive work reaches an unmeasured lane, the probe is pinned to
    that job's model: a model-scoped exhaustion is invisible to a Haiku turn."""
    stdout = (FIXTURES / "rejected-credits-fable" / "stdout").read_text(encoding="utf-8")
    runner = _Runner(stdout=stdout, rc=1)
    adapter = ClaudeAdapter(runner=runner, now=lambda: NOW, projects_dir=tmp_path)
    readings = adapter.probe_with_model(
        make_lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "t"}, "claude-fable-5-1"
    )
    assert len(readings) == 1
    assert readings[0].label is ReadingLabel.ADMISSION_OBSERVED
    assert readings[0].scope == "claude-fable-5-1"
    assert readings[0].utilization is None
    assert readings[0].lane_id == "claude-1"
    turn = runner.calls[-1]
    assert turn[turn.index("--model") + 1] == "claude-fable-5-1"


def test_probe_defaults_to_the_haiku_turn(tmp_path):
    """C-9.1, C-10.2 the default probe is the cheapest turn that still reports."""
    info = json.loads(
        (FIXTURES / "allowed-on-table-exhausted-lane" / "stdout")
        .read_text(encoding="utf-8").splitlines()[2]
    )["rate_limit_info"]
    runner = _Runner(stdout=_enroll_stream(info=info))
    adapter = ClaudeAdapter(runner=runner, now=lambda: NOW, projects_dir=tmp_path)
    readings = adapter.probe(make_lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert {r.window: r.utilization for r in readings} == {
        "five_hour": 0.42, "seven_day": 0.33
    }
    assert runner.calls[-1][runner.calls[-1].index("--model") + 1] == ENROLL_MODEL


def test_a_probe_that_cannot_run_returns_no_readings(tmp_path):
    """C-9.1 a provider with no answer returns an empty tuple, not an exception: the
    classifier, not the prober, decides a lane is dead."""

    def boom(argv, **kwargs):
        raise OSError("no such binary")

    adapter = ClaudeAdapter(runner=boom, now=lambda: NOW, projects_dir=tmp_path)
    assert adapter.probe(make_lane(), {}) == ()


def test_a_probe_run_never_puts_the_credential_in_argv(tmp_path):
    """C-10.5 the credential reaches the child through the environment only."""
    runner = _Runner(stdout=_enroll_stream())
    adapter = ClaudeAdapter(runner=runner, now=lambda: NOW, projects_dir=tmp_path)
    adapter.probe(make_lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-SECRET"})
    assert all("SECRET" not in " ".join(call) for call in runner.calls)


# --- what the adapter hands the daemon (C-12.1, C-21 milestone 2) ------------


def test_a_hard_limit_hands_the_router_everything_an_exclusion_needs(adapter, tmp_path):
    """C-9.6, C-11.2, C-21 milestone 2: a hard limit closes the lane, and the closure
    carries the lane id, the scope, and an expiry — exactly the three things C-11.2
    rejects a candidate lane on, so the next attempt can carry the exclusion."""
    _e, _l, outcome = _classify(adapter, tmp_path, "limit-weekly-with-clock")
    closure = outcome.closure
    assert closure is not None
    assert closure.lane_id == "claude-1"
    assert closure.scope == "account"          # every model on the lane is shut
    assert closure.until_at > iso_utc(NOW)     # it expires by clock, nothing else
    assert closure.reason is ClosureReason.PROVIDER_LIMIT

    # A model-scoped credit exhaustion shuts one model and leaves the lane usable.
    _e, _l, credits = _classify(adapter, tmp_path / "credits", "rejected-credits-fable")
    assert credits.closure.scope == "claude-fable-5-1"
    assert credits.closure.reason is ClosureReason.CREDITS


def test_classify_attest_and_deliverable_never_spawn_or_write(adapter, tmp_path):
    """C-12.1 adapters return data. The three post-mortem methods read the attempt's
    files and nothing else: no subprocess, and no new file in the attempt directory."""
    def explode(*args, **kwargs):     # any subprocess use fails the test loudly
        raise AssertionError("classify/attest/deliverable must not spawn a process")

    adapter._runner = explode
    attempt_dir, rc = stage_case("success-allowed", tmp_path / "a1")
    before = sorted(p.name for p in attempt_dir.iterdir())
    launch = make_launch(attempt_dir, session_id="281408bb-280e-43da-9455-b9f3ddebf275",
                         model_id="claude-haiku-4-5-20251001")
    outcome = adapter.classify(attempt_dir, launch, exit_info(rc))
    adapter.attest(attempt_dir, launch, outcome, "claude-haiku-4-5-20251001")
    adapter.deliverable(attempt_dir, launch, outcome)
    assert sorted(p.name for p in attempt_dir.iterdir()) == before


def test_evidence_always_records_which_evidence_answered_what(adapter, tmp_path):
    """C-9.2 the classifier records which evidence answered each question, and always
    keeps the raw rc and signal beside the class."""
    for case in case_names():
        _expected, _launch, outcome = _classify(adapter, tmp_path / case, case)
        assert "rc" in outcome.evidence and "signal" in outcome.evidence
        assert outcome.evidence["system_init"] in (True, False)
        if outcome.cls is not OutcomeClass.UNKNOWN:
            assert "answered" in outcome.evidence, outcome.detail


def test_the_launch_notes_are_json_serialisable_and_hold_no_secret(adapter, tmp_path):
    """C-10.5, C-12.2 the daemon persists `notes` beside the attempt: it must round-trip
    through JSON and must never carry a credential value."""
    launch = _build(adapter, tmp_path, Sandbox.READ_ONLY)
    restored = json.loads(json.dumps(launch.notes))
    assert restored == launch.notes
    assert "TOKEN" not in json.dumps(restored).upper() or "OAUTH_TOKEN" not in restored
    assert not any(
        isinstance(v, str) and v.startswith("sk-ant-") for v in restored.values()
    )


def test_link_raw_stream_publishes_stdout_as_the_stream_without_copying(adapter, tmp_path):
    """C-8.2 `--output-format stream-json` writes the stream to stdout, so the raw
    stream artifact is a link to the same bytes, not a second copy."""
    attempt_dir = tmp_path / "a1"
    attempt_dir.mkdir()
    (attempt_dir / "stdout").write_text("{\"type\":\"result\"}\n", encoding="utf-8")
    launch = make_launch(attempt_dir, session_id="s", model_id="claude-opus-5")
    stream = claude_adapter.link_raw_stream(attempt_dir, launch)
    assert stream is not None and stream.name == "stream.jsonl"
    assert stream.stat().st_ino == (attempt_dir / "stdout").stat().st_ino
    # Calling it twice is harmless.
    assert claude_adapter.link_raw_stream(attempt_dir, launch) == stream


def test_the_adapter_reads_stdout_when_no_stream_file_was_published(adapter, tmp_path):
    """C-12.2 the classifier does not depend on the daemon having published the stream
    under its own name: `stdout` is the same bytes and is read when `stream.jsonl` is
    absent."""
    attempt_dir = tmp_path / "a1"
    attempt_dir.mkdir()
    source = FIXTURES / "success-allowed"
    (attempt_dir / "stdout").write_bytes((source / "stdout").read_bytes())
    (attempt_dir / "stderr").write_bytes(b"")
    launch = make_launch(attempt_dir, session_id="281408bb-280e-43da-9455-b9f3ddebf275",
                         model_id="claude-haiku-4-5-20251001")
    outcome = adapter.classify(attempt_dir, launch, exit_info(0))
    assert outcome.cls is OutcomeClass.OK
    assert len(outcome.readings) == 2
