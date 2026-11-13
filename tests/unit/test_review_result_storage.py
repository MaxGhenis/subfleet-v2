"""D-REVIEW-CONTRACT1: acceptance, retries and restart retain one review result."""

import json

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.daemon import Daemon
from subfleet.offline import Offline
from subfleet.review_result import parse_review_result
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
    expected = parse_review_result((adir / "deliverable.md").read_text())
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

    monkeypatch.setattr(daemon_module, "parse_review_result", forbidden_parse)
    service._finalize(receipt_fixture(service, attempt, adir, stdout=REVIEWS[0]))
    assert service.store.get_job(job_id)["state"] == "succeeded"
    assert_views(service, job_id, None)
    assert not review_events(service, job_id)
    assert "review" not in json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])


def test_failed_review_does_not_parse_or_emit_review(state_daemon, monkeypatch):
    service, harness = state_daemon
    job_id, attempt, adir = reserve(service, harness, task="review")
    monkeypatch.setattr(daemon_module, "parse_review_result", lambda text: pytest.fail("failed review parsed"))
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
