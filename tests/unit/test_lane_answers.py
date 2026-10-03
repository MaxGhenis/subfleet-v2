"""C-4.5, C-6.14, C-9.3: what shows a model answering, and what does not.

2026-09-30 15:43Z: an organisation disabled claude-5's Claude Code access. Every
attempt there wrote `system/init` and then Claude Code's own placeholder (model
`<synthetic>`, `is_api_error_message`, error `oauth_org_not_allowed`, every usage
counter zero, `duration_api_ms` 0): the CLI spoke, no model did. These pin the
predicate the daemon proves a lane with (C-6.14), the evidence a finished attempt
records (`model_answered`), and that `auth-dead` is read only from what the CLI and
the provider wrote, never from a model's own words (C-9.3), since an `auth-dead`
job now moves on to the next lane and disables each one it leaves (C-4.5).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import capacity, scheduler
from subfleet.adapters import claude_stream, codex
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.claude_stream import parse_stream
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Launch, OutcomeClass
from tests.conftest import FIXTURES, exit_info, load_expected, make_launch, stage_case

ORG_BLOCK = ("Your organization has disabled Claude subscription access for Claude Code · "
             "Use an Anthropic API key instead, or ask your admin to enable access")
SID = "55555555-5555-4555-8555-5555555555b9"


def rows(case: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / case / "stdout").read_text().splitlines() if line.strip()]


# --- Claude: model_answered over single events ------------------------------------------------

def test_c6_14_the_incident_stream_shows_no_model_answering():
    """The 2026-09-30 shape: hook events, `system/init`, the placeholder and an error
    `success` result. Not one event says a model answered; `system/init` least of all."""
    events = rows("org-block-synthetic")
    assert [claude_stream.model_answered(row) for row in events] == [False] * len(events)
    assert any(row.get("type") == "system" and row.get("subtype") == "init" for row in events)
    assert parse_stream((FIXTURES / "org-block-synthetic" / "stdout").read_text()).answered is False


@pytest.mark.parametrize("case,answered", [
    ("org-block-synthetic", False),     # the incident: the CLI's placeholder only
    ("auth-401", False),                # no system/init, a 401 in stderr
    ("spawn-failure", False),           # no stream at all
    ("stream-disconnect", False),
    ("cli-too-old", False),
    ("success-allowed", True),          # a served Haiku turn (live probe payloads)
    ("content-filter", True),           # the model itself declined
])
def test_c6_14_fixtures_answer_as_their_streams_say(case, answered):
    assert parse_stream((FIXTURES / case / "stdout").read_text()).answered is answered


def served(model="claude-opus-5-5", **usage):
    return {"type": "assistant", "message": {"model": model, "role": "assistant", "type": "message",
                                             "content": [{"type": "text", "text": "hi"}],
                                             "usage": {"input_tokens": 0, "output_tokens": 0, **usage}}}


def test_c6_14_each_claude_event_kind():
    assert claude_stream.model_answered(served())                              # a model id: served
    assert claude_stream.model_answered(served(model=None, output_tokens=4))   # usage alone: served
    assert not claude_stream.model_answered(served(model="<synthetic>"))       # the sentinel
    assert not claude_stream.model_answered({**served(), "is_api_error_message": True})
    assert not claude_stream.model_answered({**served(), "isApiErrorMessage": True})
    assert not claude_stream.model_answered(served(model=""))
    assert claude_stream.model_answered({"type": "result", "usage": {"input_tokens": 9}})
    assert not claude_stream.model_answered({"type": "result", "usage": {"input_tokens": 0}})
    assert not claude_stream.model_answered({"type": "result", "usage": {"input_tokens": True}})
    # Claude Code 2.1.284 writes these while the model streams its thinking, before any frame.
    assert claude_stream.model_answered({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50})
    assert not claude_stream.model_answered({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 0})
    for row in ({"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
                {"type": "system", "subtype": "hook_started"}, {"type": "rate_limit_event"},
                {"type": "user"}, "assistant", None, [], {"type": "assistant", "message": "text"}):
        assert not claude_stream.model_answered(row)


@settings(max_examples=300, deadline=None)
@given(st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
                    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
                    max_leaves=12))
def test_c6_14_the_claude_predicate_never_raises_and_a_placeholder_never_answers(value):
    """For any decoded JSON value: the predicate answers a bool and raises nothing; and
    anything `is_synthetic_api_error` recognizes as Claude Code's placeholder is no answer."""
    answer = claude_stream.model_answered(value)
    assert isinstance(answer, bool)
    if claude_stream.is_synthetic_api_error(value):
        assert answer is False


# --- Claude: auth-dead is the CLI's word, never the model's -----------------------------------

@pytest.fixture
def adapter(tmp_path):
    return ClaudeAdapter(projects_dir=tmp_path / "projects")


def classify(adapter, tmp_path, stdout: str, *, rc: int = 0, stderr: str = ""):
    attempt_dir = tmp_path / "a1"
    attempt_dir.mkdir(parents=True)
    for name in ("stdout", "stream.jsonl"):
        (attempt_dir / name).write_text(stdout)
    (attempt_dir / "stderr").write_text(stderr)
    launch = make_launch(attempt_dir, session_id=SID, model_id="claude-opus-5-5",
                         projects_dir=tmp_path / "projects")
    return adapter.classify(attempt_dir, launch, exit_info(rc))


def line(row: dict) -> str:
    return json.dumps(row) + "\n"


INIT = {"type": "system", "subtype": "init", "session_id": SID, "model": "claude-opus-5-5"}


def test_c9_3_the_incident_stream_is_auth_dead_and_records_no_answer(adapter, tmp_path):
    expected = load_expected("org-block-synthetic")
    attempt_dir, rc = stage_case("org-block-synthetic", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id=expected["session_id"], model_id=expected["requested_model"],
                         projects_dir=tmp_path / "projects")
    outcome = adapter.classify(attempt_dir, launch, exit_info(rc))
    assert outcome.cls == OutcomeClass.AUTH_DEAD
    assert "organization has disabled" in outcome.detail
    assert outcome.evidence["model_answered"] is False
    assert outcome.evidence["system_init"] is True


def test_c9_3_a_model_quoting_the_block_is_not_auth_dead(adapter, tmp_path):
    """A review of this classifier answers with its phrases. That is the model's text in
    a served frame and a success result: the run is `ok`, and its lane stays enabled."""
    text = f"The classifier matches {ORG_BLOCK!r} and 'refresh token was revoked' and a 401."
    stdout = (line(INIT) + line({**served(), "message": {**served()["message"],
                                                        "content": [{"type": "text", "text": text}]}})
              + line({"type": "result", "subtype": "success", "is_error": False, "result": text,
                      "usage": {"input_tokens": 40, "output_tokens": 20}}))
    outcome = classify(adapter, tmp_path, stdout)
    assert outcome.cls == OutcomeClass.OK, outcome.detail
    assert outcome.evidence["model_answered"] is True


@pytest.mark.parametrize("where", ["placeholder", "error-result", "errors", "stderr"])
def test_c9_3_the_block_in_the_clis_own_words_is_auth_dead(adapter, tmp_path, where):
    placeholder = {"type": "assistant", "error": "oauth_org_not_allowed", "is_api_error_message": True,
                   "message": {"model": "<synthetic>", "role": "assistant", "type": "message",
                               "content": [{"type": "text", "text": ORG_BLOCK if where == "placeholder" else "x"}],
                               "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                                         "cache_read_input_tokens": 0}}}
    result = {"type": "result", "subtype": "success", "is_error": True,
              "result": ORG_BLOCK if where == "error-result" else "failed",
              "errors": [ORG_BLOCK] if where == "errors" else []}
    stdout = line(INIT) + (line(placeholder) if where == "placeholder" else "") + line(result)
    outcome = classify(adapter, tmp_path, stdout, rc=1, stderr=ORG_BLOCK if where == "stderr" else "")
    assert outcome.cls == OutcomeClass.AUTH_DEAD, outcome.detail
    assert outcome.evidence["model_answered"] is False


def test_c9_3_a_401_only_the_model_wrote_before_init_is_not_auth_dead(adapter, tmp_path):
    """Without `system/init` a credential failure in the CLI's words is `auth-dead`; the
    same words in a served frame are not the lane's."""
    frame = served()
    frame["message"]["content"] = [{"type": "text", "text": "the handler answers 401 Unauthorized"}]
    assert classify(adapter, tmp_path / "model", line(frame), rc=1).cls != OutcomeClass.AUTH_DEAD
    assert classify(adapter, tmp_path / "cli", "", rc=1,
                    stderr="API Error: 401 authentication_error").cls == OutcomeClass.AUTH_DEAD


# --- Codex ----------------------------------------------------------------------------------------

def test_c6_14_each_codex_event_kind():
    for item in sorted(codex.MODEL_ITEMS):
        for kind in ("item.started", "item.updated", "item.completed"):
            assert codex.model_answered({"type": kind, "item": {"type": item}})
    assert not codex.model_answered({"type": "item.completed", "item": {"type": "error", "message": "x"}})
    assert codex.model_answered({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 0}})
    assert not codex.model_answered({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}})
    for event in ({"type": "thread.started", "thread_id": "x"}, {"type": "turn.started"},
                  {"type": "turn.failed", "error": {"message": "401"}}, {"type": "error", "message": "x"},
                  {"type": "item.completed"}, {"type": "item.completed", "item": "agent_message"}, None, 3):
        assert not codex.model_answered(event)


def test_c6_14_codex_classification_records_whether_the_model_answered(tmp_path):
    adapter = CodexAdapter()
    for case, answered in (("success", True), ("refresh-token-revoked", False), ("limit-with-clock", False)):
        source = FIXTURES.parent / "codex" / case
        attempt_dir = tmp_path / case
        attempt_dir.mkdir()
        for name in ("stdout", "stderr", "last.md"):
            if (source / name).exists():
                (attempt_dir / name).write_bytes((source / name).read_bytes())
        launch = Launch(argv=("codex", "exec", "--json"), env_add={"CODEX_HOME": str(attempt_dir / "home")},
                        env_remove=(), cwd=str(attempt_dir), stdin_path=None,
                        stdout_path=str(attempt_dir / "stdout"), stderr_path=str(attempt_dir / "stderr"),
                        raw_stream_path=str(attempt_dir / "stream.jsonl"), native_session_id=None,
                        lane_id="codex-4")
        rc = int((source / "rc").read_text().strip())
        assert adapter.classify(attempt_dir, launch, exit_info(rc)).evidence["model_answered"] is answered, case


# --- pilot marks (C-6.14) ----------------------------------------------------------------------

def attempt(aid, lane="codex-1", state="running", kind="dispatch"):
    return {"attempt_id": aid, "lane_id": lane, "state": state, "kind": kind}


def test_c6_14_a_cold_lane_is_marked_by_its_least_unanswered_detached_attempt():
    marks = capacity.pilot_marks([attempt("j2/a1"), attempt("j1/a1"), attempt("t/a1", kind="turn")],
                                 answered={}, lane_answers={}, now=1000.0, idle_s=900)
    assert marks == {"codex-1": "proving:j1/a1"}
    # An attempt that answered is no pilot; the next unanswered one is.
    assert capacity.pilot_marks([attempt("j2/a1"), attempt("j1/a1")], answered={"j1/a1": 1.0},
                                lane_answers={}, now=1000.0, idle_s=900) == {"codex-1": "proving:j2/a1"}
    # A lane that answered within `idle_s` is not being proven; one past it is.
    assert capacity.pilot_marks([attempt("j1/a1")], answered={}, lane_answers={"codex-1": 200.0},
                                now=1000.0, idle_s=900) == {}
    assert capacity.pilot_marks([attempt("j1/a1")], answered={}, lane_answers={"codex-1": 100.0},
                                now=1000.0, idle_s=900) == {"codex-1": "proving:j1/a1"}
    # A turn is never a pilot, an ended attempt neither, and null holds nothing.
    assert capacity.pilot_marks([attempt("t/a1", kind="turn"), attempt("j/a1", state="failed")],
                                answered={}, lane_answers={}, now=1000.0, idle_s=900) == {}
    assert capacity.pilot_marks([attempt("j1/a1")], answered={}, lane_answers={}, now=1000.0, idle_s=None) == {}


ATTEMPTS = st.lists(st.builds(attempt, st.text("abcd/", min_size=1, max_size=4),
                              st.sampled_from(["codex-1", "codex-2", "claude-1"]),
                              st.sampled_from(["reserved", "starting", "running", "finalizing", "failed", "succeeded"]),
                              st.sampled_from(["dispatch", "turn", "resume"])),
                    max_size=12, unique_by=lambda row: row["attempt_id"])


@settings(max_examples=300, deadline=None)
@given(ATTEMPTS, st.data())
def test_c6_14_pilot_marks_name_exactly_the_cold_lanes_with_an_unanswered_detached_attempt(attempts, data):
    """For all rows, answers and clocks: a lane is marked exactly when it is cold (never
    answered, or not within `idle_s`) and has a live detached attempt that has not
    answered; the mark names the least such attempt; the result does not depend on the
    order the rows come in; and marking a view keeps any reason already there."""
    ids = [row["attempt_id"] for row in attempts]
    answered = {aid: 0.0 for aid in data.draw(st.lists(st.sampled_from(ids), unique=True) if ids else st.just([]))}
    lane_answers = data.draw(st.dictionaries(st.sampled_from(["codex-1", "codex-2", "claude-1"]),
                                             st.floats(0, 2000, allow_nan=False)))
    now, idle = data.draw(st.floats(0, 3000, allow_nan=False)), data.draw(st.sampled_from([1.0, 60.0, 900.0]))
    marks = capacity.pilot_marks(attempts, answered=answered, lane_answers=lane_answers, now=now, idle_s=idle)
    shuffled = data.draw(st.permutations(attempts))
    assert capacity.pilot_marks(shuffled, answered=answered, lane_answers=lane_answers, now=now, idle_s=idle) == marks
    for lane in ("codex-1", "codex-2", "claude-1"):
        cold = lane not in lane_answers or now - lane_answers[lane] >= idle
        pilots = sorted(row["attempt_id"] for row in attempts if row["lane_id"] == lane and row["kind"] != "turn"
                        and row["state"] in capacity.ACTIVE_ATTEMPT_STATES and row["attempt_id"] not in answered)
        assert marks.get(lane) == (f"proving:{pilots[0]}" if cold and pilots else None)
    view = {"unavailable_lanes": {"codex-1": "probe:admission:x"}}
    capacity.mark_pilots(view, marks)
    assert view["unavailable_lanes"]["codex-1"] == "probe:admission:x"
    assert all(view["unavailable_lanes"][lane] == mark for lane, mark in marks.items() if lane != "codex-1")


# --- the scheduler: a pilot holds detached jobs only (C-6.14) -----------------------------------

POLICY = {"tiers": ["standard"], "chains": {"review": ["astra"]}, "models": {"astra": {"provider": "codex",
          "id": "gpt-6-astra"}}, "caps": {}, "fallback": "upward-only"}
LANE = {"lane_id": "codex-1", "provider": "codex", "owner": "v2", "enabled": True, "desktop": False}


@pytest.mark.parametrize("kind,held", [("dispatch", True), ("turn", False)])
def test_c6_14_a_pilot_holds_detached_jobs_and_never_a_turn(kind, held):
    view = {"now": "2026-10-03T12:00:00Z", "lanes": [LANE], "unavailable_lanes": {"codex-1": "proving:j/a1"}}
    decision = scheduler.evaluate(POLICY, view, {"job_id": "x", "kind": kind, "pinned_model": "astra"})
    assert (decision.chosen_lane is None) is held
    if held:
        rejection = decision.evaluations[0]["rejections"][0]
        assert rejection["reasons"] == ["no-slot"] and rejection["slot_block"] == "proving:j/a1"
        assert scheduler.dominant_rejection(decision) == "lane-proving"
        # Room, not a standing refusal: such a job is never held for good (C-11.8).
        assert scheduler.refused_for_good(POLICY, decision, {"job_id": "x"}) is None
    # A probe's lease still holds a turn (C-11.4): the pilot never hides it.
    view["unavailable_lanes"] = {"codex-1": "probe:admission:j"}
    assert scheduler.evaluate(POLICY, view, {"job_id": "x", "kind": kind, "pinned_model": "astra"}).chosen_lane is None


def test_c6_14_a_lane_full_for_another_reason_keeps_its_label():
    view = {"now": "2026-10-03T12:00:00Z", "lanes": [LANE, {**LANE, "lane_id": "codex-2"}],
            "unavailable_lanes": {"codex-1": "proving:j/a1", "codex-2": "probe:admission:k"}}
    decision = scheduler.evaluate(POLICY, view, {"job_id": "x", "kind": "dispatch", "pinned_model": "astra"})
    assert scheduler.dominant_rejection(decision) == "no-slot"
