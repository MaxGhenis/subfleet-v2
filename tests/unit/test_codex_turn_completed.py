"""D-SHUT1: a detached Codex exit receipt does not prove turn completion."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile

import pytest
from hypothesis import example, given, seed, settings, strategies as st

from subfleet.adapters.codex import CodexAdapter, model_answered
from subfleet.contracts import ExitInfo, OutcomeClass
from tests.unit.test_codex_adapter import FIXTURES, THREAD, _events, _exit, _job, _lane, _launch


INTERIM = "I am checking those cases before finishing the report."
FINAL = "The report is complete."
CA_ERROR = "error: invalid peer certificate: UnknownIssuer, url: wss://chatgpt.com/backend-api/codex/responses"
COMPLETED = {"type": "turn.completed"}
MESSAGE = {"type": "item.completed", "item": {"type": "agent_message", "text": INTERIM}}
CUT_OFF_CASES = ("cut-off-item-completed", "cut-off-item-started", "cut-off-error")


@pytest.mark.parametrize("case,last_type", zip(CUT_OFF_CASES, ("item.completed", "item.started", "error")))
def test_cut_off_fixtures_retry_with_completion_evidence(case, last_type):
    directory = FIXTURES / case
    assert not (directory / "last.md").exists()
    launch = replace(_launch(directory), raw_stream_path=str(directory / "stdout"))
    outcome = CodexAdapter().classify(directory, launch, _exit(int((directory / "rc").read_text())))
    assert outcome.cls == OutcomeClass.TRANSIENT
    assert outcome.detail == "Codex exited before its turn completed"
    assert outcome.evidence["turn_completed"] is False
    assert outcome.evidence["last_event_type"] == last_type
    assert outcome.evidence["model_answered"] is True
    assert outcome.closure is None


def test_success_fixture_still_completes():
    directory = FIXTURES / "success"
    outcome = CodexAdapter().classify(directory, _launch(directory), _exit(0))
    assert outcome.cls == OutcomeClass.OK
    assert outcome.evidence["turn_completed"] is True


@pytest.mark.parametrize("last", [INTERIM, FINAL])
def test_last_message_file_alone_does_not_prove_completion(tmp_path, last):
    (tmp_path / "last.md").write_text(last)
    outcome = CodexAdapter().classify(tmp_path, _events(tmp_path, MESSAGE), _exit(0))
    assert outcome.cls == OutcomeClass.TRANSIENT
    assert outcome.evidence["turn_completed"] is False


@pytest.mark.parametrize("message", [CA_ERROR, "failed to lookup address information", "write_stdin failed"])
def test_zero_exit_transport_error_with_interim_text_is_not_ok(tmp_path, message):
    outcome = CodexAdapter().classify(
        tmp_path, _events(tmp_path, MESSAGE, {"type": "error", "message": message}, stderr=message), _exit(0))
    assert outcome.cls == OutcomeClass.TRANSIENT
    assert outcome.detail == "Codex exited before its turn completed"
    assert outcome.evidence["admission"]["message"] == message


@pytest.mark.parametrize("complete", [False, True])
def test_turn_failed_retains_its_existing_class(tmp_path, complete):
    events = [MESSAGE, {"type": "turn.failed", "error": {"message": "Unrecognized terminal failure"}}]
    if complete:
        events.append(COMPLETED)
    (tmp_path / "last.md").write_text(INTERIM)
    assert CodexAdapter().classify(tmp_path, _events(tmp_path, *events), _exit(0)).cls == OutcomeClass.UNKNOWN


@pytest.mark.parametrize("complete", [False, True])
def test_resume_requires_this_attempts_completion(tmp_path, complete):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Continue.")
    adir = tmp_path / "a2"
    adapter = CodexAdapter()
    launch = adapter.resume_launch(_job(tmp_path, prompt), "job/a2", adir, _lane(tmp_path / "home"),
                                   {}, THREAD, prompt, None)
    assert "resume" in launch.argv
    # A predecessor's completion and last.md cannot prove this resume completed.
    old = tmp_path / "a1"
    old.mkdir()
    _events(old, MESSAGE, COMPLETED)
    (old / "last.md").write_text(FINAL)
    _events(adir, MESSAGE, *([COMPLETED] if complete else []))
    outcome = adapter.classify(adir, launch, _exit(0))
    assert outcome.cls == (OutcomeClass.OK if complete else OutcomeClass.TRANSIENT)
    assert outcome.native_session_id == THREAD


def test_completion_and_answered_have_distinct_evidence(tmp_path):
    # An empty-usage completion still ends the turn, while positive usage is
    # needed for model_answered when there are no model items (C-4.5, C-6.14).
    assert model_answered(COMPLETED) is False
    assert model_answered({**COMPLETED, "usage": {"output_tokens": 1}}) is True
    (tmp_path / "last.md").write_text(FINAL)
    outcome = CodexAdapter().classify(tmp_path, _events(tmp_path, COMPLETED), _exit(0))
    assert outcome.cls == OutcomeClass.OK
    assert outcome.evidence["model_answered"] is False
    assert outcome.evidence["turn_completed"] is True


@pytest.mark.parametrize("auth,admission,deliverable", [
    (auth, admission, deliverable)
    for auth in (False, True) for admission in (False, True) for deliverable in (False, True)
])
@pytest.mark.parametrize("complete", [False, True])
def test_authentication_admission_quota_precedence(tmp_path, auth, admission, deliverable, complete):
    """I3: with completion, authentication > successful/refused admission > quota.

    Without completion the refusal and quota signals still beat the new fallback.
    """
    events = [{"type": "error", "message": "quota exceeded"}]
    if admission:
        events.append({"type": "error", "message": "content filter blocked the response"})
    if auth:
        events.append({"type": "error", "message": "Your refresh token was revoked."})
    if deliverable:
        events.append(MESSAGE)
    if complete:
        events.append(COMPLETED)
    outcome = CodexAdapter().classify(tmp_path, _events(tmp_path, *events), _exit(0))
    expected = (OutcomeClass.AUTH_DEAD if auth else OutcomeClass.OK if complete and deliverable
                else OutcomeClass.CONTENT_FILTER if admission else OutcomeClass.LIMITED)
    assert outcome.cls == expected
    assert (outcome.evidence["authentication"] is not None) == auth
    assert (outcome.evidence["quota"] is not None) == (expected == OutcomeClass.LIMITED)


ERROR_TEXT = st.sampled_from(("Unrecognized terminal failure", CA_ERROR, "quota exceeded",
                              "Your refresh token was revoked.", "content filter blocked the response",
                              "Codex CLI too old", "stream disconnected before completion"))
ITEM = st.one_of(
    st.builds(dict, type=st.just("agent_message"), text=st.sampled_from(("", INTERIM, FINAL))),
    st.just({"type": "command_execution", "aggregated_output": "tool output"}),
)
EVENT = st.one_of(
    st.just({"type": "thread.started", "thread_id": THREAD}),
    st.just({"type": "turn.started"}),
    st.builds(dict, type=st.sampled_from(("item.started", "item.completed")), item=ITEM),
    st.sampled_from((COMPLETED, {**COMPLETED, "usage": {"input_tokens": 1, "output_tokens": 2}})),
    st.builds(dict, type=st.just("turn.failed"), error=st.builds(dict, message=ERROR_TEXT)),
    st.builds(dict, type=st.just("error"), message=ERROR_TEXT),
)
STREAM = st.lists(EVENT, max_size=25)
RC = st.sampled_from((0, 1, 143, 137))
LAST = st.sampled_from((None, "", INTERIM, FINAL))


def generated_attempt(directory, events, rc, last):
    launch = _events(directory, *events)
    if last is not None:
        (directory / "last.md").write_text(last)
    receipt = {"rc": rc, "signal": None, "wall_s": .1, "child_pid": 1234, "spawn_error": None}
    (directory / "exit.json").write_text(json.dumps(receipt))
    return launch, ExitInfo(**json.loads((directory / "exit.json").read_text()))


@seed(20261012)
@settings(max_examples=500, deadline=None)
@example(events=[MESSAGE], rc=0, last=INTERIM)
@given(events=STREAM, rc=RC, last=LAST)
def test_i1_ok_requires_completed_zero_exit_no_failure_and_deliverable(events, rc, last):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        launch, receipt = generated_attempt(directory, events, rc, last)
        adapter = CodexAdapter()
        outcome = adapter.classify(directory, launch, receipt)
        if outcome.cls == OutcomeClass.OK:
            assert any(event["type"] == "turn.completed" for event in events)
            assert receipt.rc == 0
            assert not any(event["type"] == "turn.failed" for event in events)
            assert adapter.deliverable(directory, launch, outcome)


@seed(20261013)
@settings(max_examples=500, deadline=None)
@example(events=[MESSAGE], rc=0, last=None)
@given(events=STREAM, rc=RC, last=LAST)
def test_i2_adding_completion_preserves_ok_removing_it_disqualifies_ok(events, rc, last):
    without = [event for event in events if event["type"] != "turn.completed"]
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        launch, receipt = generated_attempt(directory, without, rc, last)
        adapter = CodexAdapter()
        absent = adapter.classify(directory, launch, receipt)
        _events(directory, *without, COMPLETED)
        present = adapter.classify(directory, launch, receipt)
        if absent.cls == OutcomeClass.OK:
            assert present.cls == OutcomeClass.OK
        if present.cls == OutcomeClass.OK:
            _events(directory, *without)
            assert adapter.classify(directory, launch, receipt).cls != OutcomeClass.OK
