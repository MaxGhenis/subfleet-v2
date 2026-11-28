"""C-6.21–22: named refusals, durable caller identity and exact retries."""
import json

import pytest

from subfleet import protocol
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize("family", ["gemini", "", True, 1, ["codex"]])
def test_invalid_caller_family_is_refused_by_name(state_daemon, family):
    service, harness = state_daemon
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", harness.submit_args(caller_family=family))
    assert error.value.reason == "caller-family"
    assert "caller_family" in protocol.refusal_text(error.value)[0]
    assert service.store.list_jobs() == []


@pytest.mark.parametrize("round_", [True, False, 0, -1, 1.5, "2"])
def test_invalid_round_is_refused_by_name(state_daemon, round_):
    service, harness = state_daemon
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", harness.submit_args(task="review", review_subject="key", review_round=round_))
    assert error.value.reason == "review-round"
    assert "review_round" in protocol.refusal_text(error.value)[0]
    assert service.store.list_jobs() == []


@pytest.mark.parametrize("family", ["claude", "codex", None])
def test_submission_and_job_views_keep_caller_and_review_fields(state_daemon, family):
    service, harness = state_daemon
    args = harness.submit_args(task="review", caller_family=family, caller_session="codex-thread",
                               review_subject="release-review", review_round=3)
    reply = service.dispatch("submit", args)
    job = service.dispatch("show", {"job_id": reply["job_id"]})["job"]
    assert {key: job[key] for key in ("caller_family", "caller_session", "review_subject", "review_round")} == {
        "caller_family": family, "caller_session": "codex-thread", "review_subject": "release-review", "review_round": 3}
    listed = service.dispatch("list", {"mine": "codex-thread"})["jobs"]
    assert len(listed) == 1
    assert {key: listed[0][key] for key in ("caller_family", "review_subject", "review_round")} == {
        "caller_family": family, "review_subject": "release-review", "review_round": 3}


def test_automatic_retry_keeps_its_round_and_changed_subject_is_a_new_payload(state_daemon):
    service, harness = state_daemon
    args = harness.submit_args(task="review", review_subject="https://GitHub.COM/Owner/Repo/pull/9/files/")
    first = service.dispatch("submit", args)
    # A different request cannot advance past the pending first round.
    with pytest.raises(protocol.ProtocolError) as held:
        service.dispatch("submit", harness.submit_args(task="review", review_subject="https://github.com/owner/repo/pull/9"))
    assert held.value.reason == "review-round-held" and held.value.details == {"job_id": first["job_id"]}
    replay = service.dispatch("submit", {**args, "review_subject": "https://github.com/owner/repo/pull/9?view=all"})
    assert replay["job_id"] == first["job_id"] and replay["created"] is False
    assert service.store.get_job(first["job_id"])["review_round"] == 1
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", {**args, "review_subject": "another-pr"})
    assert error.value.reason == "different-payload"


@pytest.mark.parametrize("state", ["queued", "running", "waiting"])
@pytest.mark.parametrize("result", ["failed", "cancelled"])
def test_pending_review_holds_then_releases_the_next_accepted_round(state_daemon, state, result):
    from tests.fake.test_review_subject_submission import settle, submit

    service, harness = state_daemon
    first = submit(service, harness, "held-review")
    with service.store.transaction("test.pending") as tx:
        tx.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, first))
    before = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
              for table in ("jobs", "events")}
    before_directories = set((service.root / "jobs").iterdir())
    with pytest.raises(protocol.ProtocolError) as held:
        submit(service, harness, "held-review")
    assert held.value.reason == "review-round-held" and held.value.code == 7
    assert held.value.details == {"job_id": first}
    assert protocol.refusal_text(held.value) == (
        f"review: the next round is held by active job {first}",
        "wait for that review to finish, then submit again")
    assert {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
            for table in before} == before
    assert set((service.root / "jobs").iterdir()) == before_directories
    settle(service, first, result)
    replacement = submit(service, harness, "held-review")
    assert service.store.get_job(replacement)["review_round"] == 1


def test_explicit_fifth_has_distinct_wire_refusal_and_no_artifacts(state_daemon):
    service, harness = state_daemon
    before = set((service.root / "jobs").iterdir())
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", harness.submit_args(task="review", review_subject="my-pr", review_round=5))
    assert error.value.reason == "review-round-limit" and error.value.code == 7
    assert protocol.refusal_text(error.value) == (
        "review: my-pr has had 4 rounds; a fifth needs a person. "
        "Hand the PR to its hub (~/reviews/hubs.md) or queue a decision for Max.", None)
    response = protocol.decode_response(protocol.encode(protocol.refuse("cap", error.value)))
    assert response.error.reason == "review-round-limit"
    assert response.error.code == 7 and response.error.details is None
    assert set((service.root / "jobs").iterdir()) == before
    assert service.store.list_jobs() == []


@pytest.mark.parametrize("raw", ["broken json", '{"review_round":NaN}',
                                json.dumps({"review_subject": "key", "review_round": "4"}),
                                json.dumps({"review_subject": "key", "review_round": True}),
                                json.dumps({"review_subject": "key", "review_round": 3.5}),
                                json.dumps({"review_subject": "key", "review_round": 99})])
def test_malformed_or_invalid_archival_history_cannot_block_a_review(state_daemon, raw):
    service, harness = state_daemon
    with service.store.transaction("test.history") as tx:
        tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,'retention.pruned',?)", ("before", raw))
    job = service.dispatch("submit", harness.submit_args(task="review", review_subject="key"))
    assert service.store.get_job(job["job_id"])["review_round"] == 1


def test_archival_history_keeps_the_highest_accepted_round(state_daemon):
    service, harness = state_daemon
    with service.store.transaction("test.history") as tx:
        for number in (3, 1, 2):
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,'retention.pruned',?)",
                       ("before", json.dumps({"review_subject": "key", "review_round": number})))
    job = service.dispatch("submit", harness.submit_args(task="review", review_subject="key"))
    assert service.store.get_job(job["job_id"])["review_round"] == 4


def test_resume_review_fields_match_the_sources_effective_task(state_daemon):
    from tests.fake.test_review_subject_submission import settle

    service, harness = state_daemon
    source = service.dispatch("submit", harness.submit_args(task="build"))["job_id"]
    settle(service, source, "succeeded")
    with service.store.transaction("test.native") as tx:
        tx.execute("UPDATE attempts SET native_session_id=? WHERE job_id=?",
                   ("0f466ff4-5430-498a-8e77-731fbd92381e", source))
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source,
                                                       task="review", review_subject="claimed-review"))
    assert error.value.reason == "review-fields"
    assert [job["job_id"] for job in service.store.list_jobs()] == [source]
