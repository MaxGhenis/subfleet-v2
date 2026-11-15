"""D-REVIEW-CONTRACT1: acceptance, retries and restart retain one review result."""

import json

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.daemon import Daemon
from subfleet.offline import Offline
from subfleet.review_result import MAX_REVIEW_BYTES, parse_review_deliverable, parse_review_result
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.fake_adapter import FakeAdapter


REVIEWS = [
    b"VERDICT: REQUEST_CHANGES\n**P2 \xe2\x80\x94 Bound the lookup's read snapshot.**\nDetail.\n",
    b"VERDICT: APPROVE\r\nFINDINGS: none\r\n",
    b"REQUEST CHANGES\nP1: A regression\n",
    b"VERDICT: APPROVE\nP1: A regression\n",
    b"VERDICT: APPROVE\nNo declared findings.\n",
]


def review_events(service, job_id):
    return [row for row in service.store.list_events(job_id) if row["kind"] == "job.review_result"]


def assert_views(service, job_id, expected):
    shown = service.dispatch("show", {"job_id": job_id})
    assert shown["review"] == shown["job"]["review"] == expected
    [listed] = [row for row in service.dispatch("list", {})["jobs"] if row["job_id"] == job_id]
    assert listed["review"] == expected
    assert "_review_evidence_json" not in listed
    offline = Offline(service.root)
    assert offline.show_job(job_id)["review"] == expected
    [listed] = [row for row in offline.list_jobs() if row["job_id"] == job_id]
    assert listed["review"] == expected


@pytest.mark.parametrize("text", REVIEWS)
def test_accepted_review_equals_fresh_parse_after_restart(state_daemon, text):
    """I5: the same accepted bytes, including invalid format, survive a real daemon reopen."""
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    assert_views(service, job_id, None)
    finalizing = receipt_fixture(service, attempt, adir, stdout=text)
    service._finalize(finalizing)
    accepted = service.store.get_job(job_id)
    assert accepted["state"] == "succeeded" and accepted["accepted_attempt_id"] == attempt["attempt_id"]
    expected = parse_review_deliverable((adir / "deliverable.md").read_bytes())
    evidence = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["review"] == expected
    assert_views(service, job_id, expected)
    [event] = review_events(service, job_id)
    assert event["attempt_id"] == attempt["attempt_id"]
    assert json.loads(event["data_json"]) == expected
    service._finalize(finalizing)  # Replayed work must not emit a second result event.
    assert len(review_events(service, job_id)) == 1
    service.close()
    with_restarted = Daemon(harness.root)
    try:
        assert_views(with_restarted, job_id, expected)
        assert len(review_events(with_restarted, job_id)) == 1
    finally:
        with_restarted.close()


@pytest.mark.parametrize(("text", "verdict", "consistent"), [
    pytest.param(b"VERDICT: APPROVE\nFINDINGS: none\n\nExample:\n- ```text\n"
                 b"  sample\n  ```\n\nP1: Actual regression\n", "approve", False, id="list-fence"),
    pytest.param(b"VERDICT: REQUEST_CHANGES\nP1: 1,000 jobs are dropped when the index rolls over\n",
                 "request_changes", True, id="numeric-comma"),
    pytest.param(b"VERDICT: REQUEST_CHANGES\nP1: 500 (Internal Server Error) on every runs request\n",
                 "request_changes", True, id="numeric-status"),
    pytest.param(b"VERDICT: REQUEST_CHANGES\nP1: Zero (0) requests reach the handler after restart\n",
                 "request_changes", True, id="numeric-zero"),
])
def test_review_regressions_keep_explicit_counts_in_every_view(state_daemon, text, verdict, consistent):
    """Check stored counts against the contract, independently of a fresh parse."""
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    service._finalize(receipt_fixture(service, attempt, adir, stdout=text))
    expected = {"verdict": verdict, "levels": {"P0": 0, "P1": 1, "P2": 0, "P3": 0},
                "valid": True, "reason": None, "consistent": consistent}
    assert service.store.get_job(job_id)["accepted_attempt_id"] == attempt["attempt_id"]
    assert json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])["review"] == expected
    [event] = review_events(service, job_id)
    assert json.loads(event["data_json"]) == expected
    assert_views(service, job_id, expected)
    service.close()
    restarted = Daemon(harness.root)
    try:
        assert_views(restarted, job_id, expected)
        assert len(review_events(restarted, job_id)) == 1
    finally:
        restarted.close()


def test_retry_reports_only_accepted_attempt_review(state_daemon, monkeypatch):
    """I6/M4: an earlier attempt's opposite verdict cannot become the job's review."""
    service, harness = state_daemon
    job_id, first, first_dir = reserve(service, harness, task="review")
    real_classify = FakeAdapter.classify

    def classify(adapter, adir, launch, exit_info):
        if exit_info.rc == 1:
            return Outcome(OutcomeClass.TRANSIENT, "fixture retry")
        return real_classify(adapter, adir, launch, exit_info)

    monkeypatch.setattr(FakeAdapter, "classify", classify)
    service._finalize(receipt_fixture(service, first, first_dir, rc=1, stdout=REVIEWS[1]))
    assert service.store.get_job(job_id)["state"] == "waiting"
    assert not review_events(service, job_id)
    assert "review" not in json.loads(service.store.get_attempt(first["attempt_id"])["evidence_json"])
    # A previous version or imported row can carry metadata even though its
    # attempt was not accepted. Select by the acceptance pointer, not row order.
    evidence = json.loads(service.store.get_attempt(first["attempt_id"])["evidence_json"])
    evidence["review"] = parse_review_result(REVIEWS[1].decode())
    service.store.update_attempt(first["attempt_id"], evidence_json=json.dumps(evidence))
    assert_views(service, job_id, None)
    service.store.update_job(job_id, next_check_at="2000-01-01T00:00:00Z")
    service._admit()
    second = service.store.list_attempts(job_id)[-1]
    assert second["seq"] == 2
    service._pending_launches.discard(second["attempt_id"])
    second_dir = service.root / "jobs" / job_id / "a2"
    second_dir.mkdir()
    service._finalize(receipt_fixture(service, second, second_dir, stdout=REVIEWS[0]))
    expected = parse_review_result(REVIEWS[0].decode())
    assert service.store.get_job(job_id)["accepted_attempt_id"] == second["attempt_id"]
    assert_views(service, job_id, expected)
    [event] = review_events(service, job_id)
    assert event["attempt_id"] == second["attempt_id"]
    assert json.loads(event["data_json"]) == expected
    service.close()
    restarted = Daemon(harness.root)
    try:
        assert_views(restarted, job_id, expected)
    finally:
        restarted.close()


@pytest.mark.parametrize("task", [None, "build", "lookup"])
def test_non_review_acceptance_never_parses_or_emits_review(state_daemon, monkeypatch, task):
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task=task)

    def forbidden_parse(text):
        pytest.fail("non-review acceptance called the review parser")

    monkeypatch.setattr(daemon_module, "parse_review_deliverable", forbidden_parse)
    service._finalize(receipt_fixture(service, attempt, adir, stdout=REVIEWS[0]))
    assert service.store.get_job(job_id)["state"] == "succeeded"
    assert_views(service, job_id, None)
    assert not review_events(service, job_id)
    assert "review" not in json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])


def test_failed_review_does_not_parse_or_emit_review(state_daemon, monkeypatch):
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    monkeypatch.setattr(daemon_module, "parse_review_deliverable", lambda text: pytest.fail("failed review parsed"))
    service._finalize(receipt_fixture(service, attempt, adir, rc=3, stdout=REVIEWS[0]))
    assert service.store.get_job(job_id)["state"] == "failed"
    assert_views(service, job_id, None)
    assert not review_events(service, job_id)


def test_older_review_record_has_null_review(state_daemon):
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    service._finalize(receipt_fixture(service, attempt, adir, stdout=REVIEWS[1]))
    evidence = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    del evidence["review"]
    service.store.update_attempt(attempt["attempt_id"], evidence_json=json.dumps(evidence))
    assert_views(service, job_id, None)


def test_a_cancel_that_vetoes_an_ok_review_stores_no_result(state_daemon):
    """A1 review r1, P2: the parse follows `accepted`, not `ok`. A provider that
    answered well (rc 0, a valid review) after its job's cancel was requested is
    not accepted, so neither its evidence nor an event carries a review."""
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    finalizing = receipt_fixture(service, attempt, adir, stdout=REVIEWS[0])
    service.store.update_job(job_id, cancel_requested_at=daemon_module.utcnow())
    service._finalize(finalizing)
    job = service.store.get_job(job_id)
    assert (job["state"], job["accepted_attempt_id"]) == ("cancelled", None)
    attempt = service.store.get_attempt(attempt["attempt_id"])
    assert (attempt["state"], attempt["rc"], attempt["outcome_class"]) == ("interrupted", 0, "ok")
    assert "review" not in json.loads(attempt["evidence_json"])
    assert not review_events(service, job_id)
    assert_views(service, job_id, None)


def test_the_parse_runs_before_the_acceptance_transaction(state_daemon, monkeypatch):
    """A1 review r1, P2: the parse never holds the store's writer; only storing
    the result and its event happen inside the accepting transaction."""
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    real, calls = daemon_module.parse_review_deliverable, []

    def observed(payload):
        calls.append(service.store._holds_writer())
        return real(payload)

    monkeypatch.setattr(daemon_module, "parse_review_deliverable", observed)
    service._finalize(receipt_fixture(service, attempt, adir, stdout=REVIEWS[0]))
    assert calls == [False]
    assert service.store.get_job(job_id)["accepted_attempt_id"] == attempt["attempt_id"]
    assert_views(service, job_id, parse_review_deliverable(REVIEWS[0]))


def test_a_gate_round_is_never_parsed(state_daemon, monkeypatch):
    """A1 review r1, P1: a gate round answers in sentinel JSON (C-23.9); the
    review format is not asked of it, so it is not parsed or shown."""
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness)
    service.store.update_job(job_id, kind="gate-review")
    assert service.store.get_job(job_id)["task"] is None
    monkeypatch.setattr(daemon_module, "parse_review_deliverable", lambda payload: pytest.fail("gate round parsed"))
    service._finalize(receipt_fixture(service, attempt, adir, stdout=REVIEWS[1]))
    assert service.store.get_job(job_id)["accepted_attempt_id"] == attempt["attempt_id"]
    assert "review" not in json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert not review_events(service, job_id)
    assert_views(service, job_id, None)


def test_an_oversized_review_is_stored_bounded_and_invalid(state_daemon):
    """A1 review r1, P2: an accepted deliverable over MAX_REVIEW_BYTES is read
    only to a line break inside the bound, and its round is recorded invalid."""
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    payload = REVIEWS[0] + b"x" * MAX_REVIEW_BYTES + b"\nP1: past the bound\n"
    service._finalize(receipt_fixture(service, attempt, adir, stdout=payload))
    stored = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])["review"]
    assert stored == parse_review_deliverable((adir / "deliverable.md").read_bytes())
    assert stored["levels"] == {"P0": 0, "P1": 0, "P2": 1, "P3": 0}
    assert stored["valid"] is False and "over the 4194304-byte bound" in stored["reason"]
