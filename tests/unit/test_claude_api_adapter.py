"""C-31: the `claude-api` adapter: enrolment, the launch interface, classification.

Streams come from tests/fixtures/claude_api, fake files in the shape the
harness writes (docs/decisions/2026-11-03-api-credit-lanes.md). No test sends a
request, reads a real registry or starts a process.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import credit_lane
from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude_api import ENV_REMOVE, ClaudeApiAdapter, events, model_matches
from subfleet.adapters.registry import get_adapter
from subfleet.contracts import (
    Attestation, ClockSource, ClosureReason, Credential, ExitInfo, JobSpec, Lane, LaneOwner, Launch, OutcomeClass,
    Sandbox,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "claude_api"
EMAIL = "plan@example.org"
REFERENCE = f"claude-api-{EMAIL}"
KEY = "sk-ant-api03-" + "A1b2C3d4" * 10 + "-_Zz"
ORG = "00000000-0000-4000-8000-00000000000a"
OTHER_ORG = "00000000-0000-4000-8000-00000000000b"
MODEL = "claude-sonnet-5-5"
REGISTRY = {"version": 1, "accounts": {EMAIL: {
    "service": REFERENCE, "fingerprint": credit_lane.fingerprint(KEY), "renews_day": 15, "org_id": ORG}}}
#: A fixed instant: 2026-11-10 at 15:00 UTC is a weekday afternoon in every US zone.
NOW = dt.datetime(2026, 11, 10, 15, 0, tzinfo=dt.timezone.utc)


def adapter(registry=REGISTRY, budget=None, **policy) -> ClaudeApiAdapter:
    found = ClaudeApiAdapter(now=lambda: NOW, python="/opt/python/bin/python3", registry_reader=lambda: registry)
    found.credit_policy = {"max_usd_per_attempt": 25, "max_turns_per_attempt": 400, "denied_org_ids": [], **policy}
    found.credit_budget = {"credit_usd": 200.0, "ledger_usd": 0.0, "in_flight_usd": 0.0} if budget is None else budget
    return found


def lane(identity: str | None = ORG) -> Lane:
    return Lane("claude-api-1", "claude-api", f"claude-api:{EMAIL}",
                Credential("claude-api", REFERENCE, "keychain-token"), None, LaneOwner.V2, False,
                identity=identity, label=EMAIL)


def job(tmp_path: Path, sandbox: Sandbox = Sandbox.READ_ONLY, **fields) -> JobSpec:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the thing.\n", encoding="utf-8")
    values = dict(request_id="r1", kind="dispatch", workdir=str(tmp_path / "work"), prompt_path=str(prompt),
                  task="build", tier="standard", pinned_model=None, pinned_lane=None, sandbox=sandbox,
                  out_path=None, name=None)
    values.update(fields)
    return JobSpec(**values)


def build(tmp_path: Path, found: ClaudeApiAdapter | None = None, *, guard: dict | None = None, **fields) -> Launch:
    spec = job(tmp_path, **fields)
    return (found or adapter()).build_launch(
        spec, "job1/a1", tmp_path / "jobs" / "job1" / "a1", lane(), {"ANTHROPIC_API_KEY": KEY, "TMPDIR": "/tmp/x"},
        MODEL, "high", Path(spec.prompt_path), json.dumps(guard) if guard else None)


def test_the_registry_builds_this_adapter_for_the_provider():
    assert isinstance(get_adapter("claude-api"), ClaudeApiAdapter)
    assert ClaudeApiAdapter().provider == "claude-api"


def test_enrolment_records_the_account_plan_label_and_organization():
    info = adapter().enroll(Credential("claude-api", REFERENCE, "keychain-token"))
    assert info.account_key == f"claude-api:{EMAIL}"
    assert (info.plan, info.label, info.identity, info.home, info.readings) == ("api-credit", EMAIL, ORG, None, ())
    assert info.identity_status == "verified"


@pytest.mark.parametrize("credential", [
    Credential("claude-api", "claude-quota-plan@example.org", "keychain-token"),
    Credential("claude-api", REFERENCE, "env"), Credential("claude", REFERENCE, "keychain-token")])
def test_enrolment_refuses_any_other_credential(credential):
    with pytest.raises(AdapterError) as caught:
        adapter().enroll(credential)
    assert caught.value.reason == "credential-refused"


@pytest.mark.parametrize("registry", [None, {"accounts": {}}, {"accounts": {EMAIL: {"org_id": ORG}}},
                                      {"accounts": {EMAIL: {"fingerprint": credit_lane.fingerprint(KEY)}}}])
def test_enrolment_needs_a_fingerprint_and_an_organization(registry):
    with pytest.raises(AdapterError) as caught:
        adapter(registry=registry).enroll(Credential("claude-api", REFERENCE, "keychain-token"))
    assert caught.value.reason == "credential-refused"


def test_enrolment_refuses_a_denied_organization():
    with pytest.raises(AdapterError) as caught:
        adapter(denied_org_ids=[ORG]).enroll(Credential("claude-api", REFERENCE, "keychain-token"))
    assert caught.value.reason == "credential-refused"
    assert ORG not in str(caught.value)


def test_probe_returns_no_readings():
    assert adapter().probe(lane(), {}) == ()


def test_build_launch_is_exactly_the_interface(tmp_path):
    launch = build(tmp_path)
    adir = tmp_path / "jobs" / "job1" / "a1"
    assert launch.argv == (
        "/opt/python/bin/python3", "-I", "-m", "subfleet.api_agent", "--attempt-dir", str(adir),
        "--workdir", str(tmp_path / "work"), "--prompt", str(adir / "prompt.sent.md"), "--model", MODEL,
        "--effort", "high", "--sandbox", "read-only", "--max-usd", "25.00", "--max-turns", "400")
    assert launch.env_add == {"ANTHROPIC_API_KEY": KEY, "SUBFLEET_API_LANE": "claude-api-1"}
    assert launch.env_remove == ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                                 "CODEX_API_KEY", "OPENAI_API_KEY") == ENV_REMOVE
    assert launch.stdin_path is None
    assert launch.raw_stream_path == str(adir / "stream.jsonl")
    assert (adir / "prompt.sent.md").read_text() == "Do the thing.\n"
    assert launch.notes["credit_grant_usd"] == 25.0 and launch.notes["renews_day"] == 15
    assert launch.notes["identity"] == ORG and launch.notes["capability"] == "commands"
    # Invariant 1: the key only in env_add, never in argv or the notes.
    assert all(KEY not in arg for arg in launch.argv)
    assert KEY not in json.dumps(launch.notes)
    # Invariant 1: never the claude binary.
    assert not any(Path(arg).name == "claude" for arg in launch.argv)


def test_a_writable_job_needs_the_guard_hook(tmp_path):
    with pytest.raises(AdapterError) as caught:
        build(tmp_path, sandbox=Sandbox.WORKSPACE_WRITE)
    assert caught.value.reason == "api-guard-required"
    launch = build(tmp_path, sandbox=Sandbox.WORKSPACE_WRITE, network=True, guard={"guard_hook": "/g/hook.sh"})
    assert launch.argv[-3:] == ("--network", "--guard-hook", "/g/hook.sh")
    assert launch.notes["capability"] == "writable"


def test_a_writable_review_needs_the_publish_hook_and_passes_it(tmp_path):
    with pytest.raises(AdapterError) as caught:
        build(tmp_path, sandbox=Sandbox.WORKSPACE_WRITE, task="review", guard={"guard_hook": "/g/hook.sh"})
    assert caught.value.reason == "api-publish-hook-required"
    launch = build(tmp_path, sandbox=Sandbox.WORKSPACE_WRITE, task="review",
                   guard={"guard_hook": "/g/hook.sh", "publish_hook": "/p/hook"})
    assert launch.argv[-4:] == ("--guard-hook", "/g/hook.sh", "--publish-hook", "/p/hook")
    assert launch.notes.get("publish_denied")
    from subfleet.review_publish import hook_command, installed_publish_hooks
    assert installed_publish_hooks(launch.argv) == [hook_command(Path("/p/hook"))]


def test_a_read_only_job_passes_the_guard_hook_when_it_has_one(tmp_path):
    launch = build(tmp_path, guard={"guard_hook": "/g/hook.sh"})
    assert launch.argv[-2:] == ("--guard-hook", "/g/hook.sh")
    assert "--network" not in build(tmp_path, network=True).argv


@pytest.mark.parametrize("fields,reason", [({"isolated_review": True}, "api-isolated-review"),
                                           ({"kind": "turn"}, "api-turn")])
def test_isolated_reviews_and_turns_are_refused(tmp_path, fields, reason):
    with pytest.raises(AdapterError) as caught:
        build(tmp_path, **fields)
    assert caught.value.reason == reason


def test_a_launch_without_the_resolved_key_is_refused(tmp_path):
    spec = job(tmp_path)
    with pytest.raises(AdapterError) as caught:
        adapter().build_launch(spec, "job1/a1", tmp_path / "a1", lane(), {}, MODEL, "high",
                               Path(spec.prompt_path), None)
    assert caught.value.reason == "credential-refused"


def test_the_grant_is_the_remaining_budget_when_that_is_smaller(tmp_path):
    found = adapter(budget={"credit_usd": 200.0, "ledger_usd": 170.0, "in_flight_usd": 6.0})
    launch = build(tmp_path, found)
    assert launch.argv[launch.argv.index("--max-usd") + 1] == "14.00"    # 190 - 170 - 6
    assert launch.notes["credit_grant_usd"] == 14.0


def test_a_grant_under_a_dollar_does_not_launch_and_names_the_renewal(tmp_path):
    found = adapter(budget={"credit_usd": 200.0, "ledger_usd": 189.5, "in_flight_usd": 0.0})
    with pytest.raises(AdapterError) as caught:
        build(tmp_path, found)
    assert caught.value.reason == "api-credit-spent"
    expected = credit_lane.local_midnight(credit_lane.next_renewal(15, credit_lane.local_date(NOW)))
    assert caught.value.details["until_at"] == expected.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert caught.value.details["clock_source"] == "reported"
    assert not (tmp_path / "jobs" / "job1" / "a1" / "prompt.sent.md").exists()


def test_a_launch_without_a_ledger_is_refused(tmp_path):
    found = adapter()
    found.credit_budget = None
    with pytest.raises(AdapterError) as caught:
        build(tmp_path, found)
    assert caught.value.reason == "api-credit-ledger"


def test_resume_copies_the_source_history_and_adds_resume(tmp_path):
    source = tmp_path / "jobs" / "job0" / "a2"
    source.mkdir(parents=True)
    (source / "messages.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")
    (source / "launch.json").write_text(json.dumps({"argv": ["x", "--effort", "medium"]}), encoding="utf-8")
    spec = job(tmp_path, kind="resume")
    adir = tmp_path / "jobs" / "job1" / "a1"
    launch = adapter().resume_launch(spec, "job1/a1", adir, lane(), {"ANTHROPIC_API_KEY": KEY}, "job0/a2",
                                     Path(spec.prompt_path), None, MODEL)
    assert launch.argv[-1] == "--resume"
    assert launch.argv[launch.argv.index("--effort") + 1] == "medium"
    assert (adir / "messages.jsonl").read_text() == '{"role":"user"}\n'


@pytest.mark.parametrize("session", ["job0/a9", "../job0/a2", "job0", "-x", "job0/a2/../../x"])
def test_resume_refuses_without_the_source_history(tmp_path, session):
    spec = job(tmp_path, kind="resume")
    with pytest.raises(AdapterError) as caught:
        adapter().resume_launch(spec, "job1/a1", tmp_path / "jobs" / "job1" / "a1", lane(),
                                {"ANTHROPIC_API_KEY": KEY}, session, Path(spec.prompt_path), None, MODEL)
    assert caught.value.reason == "api-resume-history"


# --- classification --------------------------------------------------------------------------

def classified(tmp_path: Path, case: str, rc: int = 0, *, renews_day: int | None = 15, signal=None):
    adir = tmp_path / case
    shutil.copytree(FIXTURES / case, adir)
    launch = Launch(("x",), {}, (), str(tmp_path), None, str(adir / "stdout"), str(adir / "stderr"),
                    str(adir / "stream.jsonl"), "job1/a1", "claude-api-1",
                    notes={"identity": ORG, "renews_day": renews_day, "credit_grant_usd": 25.0,
                           "attempt_id": "job1/a1"})
    found = adapter()
    outcome = found.classify(adir, launch, ExitInfo(rc=rc, signal=signal, wall_s=1.0, child_pid=None))
    return found, adir, launch, outcome


def test_done_with_a_deliverable_is_ok_and_records_its_spend(tmp_path):
    found, adir, launch, outcome = classified(tmp_path, "done")
    assert outcome.cls == OutcomeClass.OK and outcome.closure is None
    assert outcome.evidence["usd"] == 0.024 and outcome.evidence["responses"] == 2
    assert outcome.evidence["model_answered"] is True and outcome.native_session_id == "job1/a1"
    assert found.deliverable(adir, launch, outcome) == b"The answer.\n"
    assert found.attest(adir, launch, outcome, MODEL).status == Attestation.ATTESTED


def test_credit_closes_the_lane_until_the_next_renewal(tmp_path):
    _, _, _, outcome = classified(tmp_path, "credit", rc=3)
    assert outcome.cls == OutcomeClass.LIMITED
    closure = outcome.closure
    seen = credit_lane.local_date(dt.datetime(2026, 11, 10, 15, 0, 3, tzinfo=dt.timezone.utc))
    until = credit_lane.exhausted_until(15, seen)
    assert (closure.scope, closure.reason, closure.lane_id) == ("account", ClosureReason.CREDITS, "claude-api-1")
    assert closure.until_at == credit_lane.local_midnight(until).astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    assert closure.clock_source == ClockSource.REPORTED
    assert outcome.evidence["credit"] == {"verdict": "exhausted", "check_status": 400}
    assert outcome.evidence["usd"] == 0.0


def test_credit_with_no_renewal_day_holds_one_day(tmp_path):
    _, _, _, outcome = classified(tmp_path, "credit", rc=3, renews_day=None)
    seen = credit_lane.local_date(dt.datetime(2026, 11, 10, 15, 0, 3, tzinfo=dt.timezone.utc))
    assert outcome.closure.until_at == credit_lane.local_midnight(seen + dt.timedelta(days=1)).astimezone(
        dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert outcome.closure.clock_source == ClockSource.GUESSED


def test_a_workspace_limit_closes_the_lane_for_one_day(tmp_path):
    _, _, _, outcome = classified(tmp_path, "workspace-limit", rc=5)
    seen = credit_lane.local_date(dt.datetime(2026, 11, 10, 15, 0, 3, tzinfo=dt.timezone.utc))
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.closure.until_at == credit_lane.local_midnight(seen + dt.timedelta(days=1)).astimezone(
        dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_refused_by_kind_fails_with_no_closure_and_an_alert(tmp_path):
    _, _, _, outcome = classified(tmp_path, "refused-by-kind", rc=4)
    assert outcome.cls == OutcomeClass.UNKNOWN and outcome.closure is None
    assert outcome.detail == "credit-refused-by-kind"
    assert outcome.evidence["alert"]["key"] == "credit-refused-by-kind"


@pytest.mark.parametrize("case,rc", [("budget", 6), ("turns", 6), ("error", 1)])
def test_budget_turns_and_errors_fail(tmp_path, case, rc):
    _, _, _, outcome = classified(tmp_path, case, rc=rc)
    assert outcome.cls == OutcomeClass.UNKNOWN and outcome.closure is None


def test_a_429_gets_a_short_guessed_closure(tmp_path):
    _, _, _, outcome = classified(tmp_path, "error-429", rc=1)
    assert outcome.cls == OutcomeClass.LIMITED
    assert outcome.closure.reason == ClosureReason.PROVIDER_LIMIT
    assert outcome.closure.clock_source == ClockSource.GUESSED
    assert outcome.closure.until_at == "2026-11-10T16:00:00Z"


def test_a_401_is_auth_dead(tmp_path):
    _, _, _, outcome = classified(tmp_path, "error-401", rc=1)
    assert outcome.cls == OutcomeClass.AUTH_DEAD


@pytest.mark.parametrize("rc,cls", [(0, OutcomeClass.UNKNOWN), (3, OutcomeClass.LIMITED), (4, OutcomeClass.UNKNOWN),
                                    (5, OutcomeClass.LIMITED), (6, OutcomeClass.UNKNOWN), (1, OutcomeClass.UNKNOWN),
                                    (-9, OutcomeClass.UNKNOWN)])
def test_with_no_end_event_the_exit_code_decides(tmp_path, rc, cls):
    _, _, _, outcome = classified(tmp_path, "no-end", rc=rc)
    assert outcome.cls == cls
    assert outcome.evidence["end"] is False and outcome.evidence["usd"] is None
    assert outcome.evidence["responses_usd"] == 0.3


def test_a_response_from_another_organization_fails_and_marks_a_mismatch(tmp_path):
    found, adir, launch, outcome = classified(tmp_path, "other-org")
    assert outcome.cls == OutcomeClass.UNKNOWN
    assert outcome.evidence["identity"]["status"] == "identity-mismatch"
    assert found.attest(adir, launch, outcome, MODEL).status == Attestation.MISMATCH


def test_another_model_fails_attestation(tmp_path):
    found, adir, launch, outcome = classified(tmp_path, "other-model")
    result = found.attest(adir, launch, outcome, MODEL)
    assert result.status == Attestation.MISMATCH and result.served_model == "claude-haiku-5-5"


def test_no_response_is_unattested(tmp_path):
    found, adir, launch, outcome = classified(tmp_path, "credit", rc=3)
    assert found.attest(adir, launch, outcome, MODEL).status == Attestation.UNATTESTED


def test_model_answered_is_a_response_event():
    found = ClaudeApiAdapter()
    assert found.model_answered({"v": 1, "type": "response"})
    assert not any(found.model_answered(event) for event in
                   ({"v": 1, "type": "start"}, {"v": 1, "type": "error"}, {"type": "response"}, "response"))


def test_every_fixture_classifies_by_its_end_event():
    """Each `end.outcome` has a fixture, and so does a stream with none."""
    outcomes = set()
    for folder in FIXTURES.iterdir():
        if folder.is_dir():
            ends = [event for event in events(folder / "stream.jsonl") if event["type"] == "end"]
            outcomes.add(ends[-1]["outcome"] if ends else None)
    assert outcomes == {"done", "credit", "workspace-limit", "refused-by-kind", "budget", "turns", "error", None}


@pytest.mark.parametrize("served,ok", [(MODEL, True), (MODEL + "-20261101", True), ("claude-sonnet-5", False),
                                       (MODEL + "-x", False), (None, False), ("claude-opus-5-5", False)])
def test_model_matches_only_the_model_or_its_dated_snapshot(served, ok):
    assert model_matches(served, MODEL) is ok


# --- invariants ------------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(day=st.one_of(st.none(), st.integers(1, 31)),
       seen=st.datetimes(min_value=dt.datetime(2020, 1, 1), max_value=dt.datetime(2040, 12, 31),
                         timezones=st.just(dt.timezone.utc)))
def test_a_credit_closure_ends_where_exhausted_until_says(day, seen, tmp_path_factory):
    """Invariant 5 through classify: the closure ends at local midnight of
    `claude_credits.exhausted_until` for the day the harness saw the exhaustion."""
    adir = tmp_path_factory.mktemp("credit")
    stamp = seen.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"v": 1, "type": "credit", "ts": stamp, "verdict": "exhausted", "check_status": 400},
            {"v": 1, "type": "end", "ts": stamp, "outcome": "credit", "turns": 0, "usd": 0.0}]
    (adir / "stream.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    launch = Launch(("x",), {}, (), str(adir), None, str(adir / "stdout"), str(adir / "stderr"),
                    str(adir / "stream.jsonl"), None, "claude-api-1", notes={"identity": ORG, "renews_day": day})
    outcome = adapter().classify(adir, launch, ExitInfo(rc=3, signal=None, wall_s=1.0, child_pid=None))
    local = credit_lane.local_date(seen)
    until = credit_lane.exhausted_until(day, local)
    assert outcome.closure.until_at == credit_lane.local_midnight(until).astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    assert credit_lane.local_midnight(until) > seen


@settings(max_examples=150, deadline=None)
@given(orgs=st.lists(st.sampled_from([ORG, OTHER_ORG, None]), min_size=1, max_size=6))
def test_the_first_foreign_organization_holds_the_lane(orgs, tmp_path_factory):
    """Invariant 7: any response from another organization than the lane's marks
    the lane a mismatch and fails attestation; responses all from its own pass."""
    adir = tmp_path_factory.mktemp("orgs")
    rows = [{"v": 1, "type": "response", "ts": "2026-11-10T15:00:00Z", "model": MODEL, "org_id": org, "usd": 0.01}
            for org in orgs]
    rows.append({"v": 1, "type": "end", "ts": "2026-11-10T15:00:01Z", "outcome": "done", "turns": 1, "usd": 0.01})
    (adir / "stream.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (adir / "last.md").write_text("ok\n", encoding="utf-8")
    launch = Launch(("x",), {}, (), str(adir), None, str(adir / "stdout"), str(adir / "stderr"),
                    str(adir / "stream.jsonl"), None, "claude-api-1", notes={"identity": ORG})
    found = adapter()
    outcome = found.classify(adir, launch, ExitInfo(rc=0, signal=None, wall_s=1.0, child_pid=None))
    foreign = any(org != ORG for org in orgs)
    assert (outcome.evidence.get("identity", {}).get("status") == "identity-mismatch") is foreign
    assert (outcome.cls == OutcomeClass.OK) is not foreign
    status = found.attest(adir, launch, outcome, MODEL).status
    assert status == (Attestation.MISMATCH if foreign else Attestation.ATTESTED)


EVENTS = st.lists(st.one_of(
    st.tuples(st.just("launch"), st.just(0.0)),
    st.tuples(st.just("end"), st.floats(min_value=0, max_value=2.0)),   # spend as a share of the grant
), max_size=40)


@settings(max_examples=300, deadline=None)
@given(credit=st.sampled_from([100.0, 200.0, 37.5]), cap=st.sampled_from([25.0, 5.0, 300.0]), steps=EVENTS,
       honest=st.booleans())
def test_spend_stays_under_the_cap(credit, cap, steps, honest):
    """Invariant 8, through `credit_lane.grant` and `credit_lane.spend`.

    With spends no larger than their grants, the ledger plus the grants in
    flight never exceeds 95% of `credit_usd`. With any spends (a harness
    overshoots by up to one response), each launch's grant still fits in what
    remained when it was made, no grant is negative, and nothing launches under
    $1."""
    since = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    attempts: list[dict] = []
    for kind, share in steps:
        ledger, in_flight = credit_lane.spend(attempts, since)
        if kind == "launch":
            value = credit_lane.grant(credit, cap, ledger, in_flight)
            assert 0 <= value <= cap
            assert value <= max(0.0, credit_lane.SPEND_SHARE * credit - ledger - in_flight) + 1e-9
            if value >= credit_lane.MIN_GRANT_USD:
                attempts.append({"live": True, "started": True, "finished_at": None,
                                 "evidence": {"credit_grant_usd": value}})
        else:
            live = [attempt for attempt in attempts if attempt["live"]]
            if not live:
                continue
            attempt = live[0]
            grant = attempt["evidence"]["credit_grant_usd"]
            spent = grant * (min(share, 1.0) if honest else share)
            attempt.update(live=False, finished_at="2026-11-02T00:00:00Z",
                           evidence={**attempt["evidence"], "classification": {"usd": spent}})
        ledger, in_flight = credit_lane.spend(attempts, since)
        if honest:
            assert ledger + in_flight <= credit_lane.SPEND_SHARE * credit + 1e-6


def test_the_ledger_counts_only_this_cycle_and_never_an_unstarted_attempt():
    since = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    attempts = [
        {"live": False, "started": True, "finished_at": "2026-10-31T23:59:59Z",
         "evidence": {"classification": {"usd": 50.0}}},                                   # last cycle
        {"live": False, "started": True, "finished_at": "2026-11-01T00:00:00Z",
         "evidence": {"classification": {"usd": 3.0}}},
        {"live": False, "started": True, "finished_at": "2026-11-02T00:00:00Z",
         "evidence": {"credit_grant_usd": 10.0, "classification": {"responses_usd": 2.0}}},  # no end event
        {"live": False, "started": False, "finished_at": "2026-11-02T00:00:00Z",
         "evidence": {"credit_grant_usd": 10.0}},                                           # never launched
        {"live": True, "started": True, "finished_at": None, "evidence": {"credit_grant_usd": 7.0}},
    ]
    assert credit_lane.spend(attempts, since) == (13.0, 7.0)
