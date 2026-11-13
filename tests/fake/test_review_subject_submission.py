"""D-REVIEW-SUBJECT1: durable review subjects and round admission.

The daemon handles real submit requests; no provider or guardian is started.
The release/217 oracle keeps the no-subject path honest independently of the
new allocator, and the race uses two stores rather than one Python submit lock.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
import hashlib
import threading
from types import SimpleNamespace
import uuid

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, protocol, review_subject, store as store_module
from subfleet.adapters.base import AdapterError
from subfleet.daemon import Daemon
from subfleet.store import Store
from tests.fake.conftest import Harness
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - shared isolated fixture
from tests.fixtures.review_subject_release_217 import (
    ARGS_SHA256, ARGS_SOURCE, DIGEST_SHA256, DIGEST_SOURCE, SUBMIT_SHA256, SUBMIT_SOURCE,
)


STAMP = "2026-11-13T13:30:00Z"
NEW_FIELDS = {"caller_family", "review_subject", "review_round"}


def submit(service, harness, subject=None, **changes):
    # Refused submissions need no durable row. Give every probe a distinct
    # display name so filesystem artifacts from a refusal cannot alias a later
    # probe while several requests share the clock's one-second resolution.
    changes.setdefault("name", "review-" + uuid.uuid4().hex)
    args = harness.submit_args(task="review", tier="standard", review_subject=subject, **changes)
    return service.dispatch("submit", args)["job_id"]


def settle(service, job_id, result):
    """The durable state real finalization leaves, without running a provider."""
    if result == "cancelled":
        service.dispatch("kill", {"job_id": job_id})
        return
    aid = f"{job_id}/a1"
    with service.store.transaction("test.review_completed", job_id=job_id) as tx:
        tx.execute(
            "INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,"
            "reserved_at,finished_at,rc,outcome_class) VALUES(?,?,1,'codex-1','gpt-6-astra',?,?,?,?,?)",
            (aid, job_id, result, STAMP, STAMP, 0 if result == "succeeded" else 1,
             "ok" if result == "succeeded" else "unknown"),
        )
        tx.execute(
            "UPDATE jobs SET state=?,finished_at=?,accepted_attempt_id=?,rc=? WHERE job_id=?",
            (result, STAMP, aid if result == "succeeded" else None,
             0 if result == "succeeded" else 1, job_id),
        )


def assert_limit(service, harness, subject, **changes):
    before = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
              for table in ("jobs", "attempts", "events")}
    before_directories = set((service.root / "jobs").iterdir())
    with pytest.raises((AdapterError, protocol.ProtocolError)) as refusal:
        submit(service, harness, subject, **changes)
    assert refusal.value.reason == "review-round-limit"
    assert refusal.value.code == 7
    assert str(refusal.value) == (
        f"review: {subject} has had 4 rounds; a fifth needs a person. "
        "Hand the PR to its hub (~/reviews/hubs.md) or queue a decision for Max."
    )
    assert {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
            for table in before} == before
    assert set((service.root / "jobs").iterdir()) == before_directories


@settings(max_examples=30, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@example(sequence=["failed", "cancelled", "refused", "succeeded"] * 4)
@example(sequence=["succeeded"] * 4)
@given(sequence=st.lists(st.sampled_from(["succeeded", "failed", "cancelled", "refused"]),
                         max_size=18))
def test_i2_only_accepted_reviews_advance_assigned_rounds(state_daemon, sequence):
    """C-6.22, I2: failed/cancelled/refused submissions consume no accepted round."""
    service, harness = state_daemon
    subject = f"property-{uuid.uuid4().hex}"
    accepted = 0
    accepted_rounds = []
    # Every generated prefix is followed through the cap, including empty and
    # all-failure prefixes. An off-by-one cap must fail on accepting round 4.
    for result in [*sequence, *(["succeeded"] * 4)]:
        if result == "refused":
            assert_limit(service, harness, subject, review_round=5)
        elif accepted == 4:
            assert_limit(service, harness, subject)
        else:
            job_id = submit(service, harness, subject)
            row = service.store.get_job(job_id)
            assert row["review_subject"] == subject
            assert row["review_round"] == accepted + 1
            settle(service, job_id, result)
            if result == "succeeded":
                accepted += 1
                accepted_rounds.append(row["review_round"])
    assert accepted_rounds == [1, 2, 3, 4]
    assert_limit(service, harness, subject)


def test_i3_pr_url_spellings_share_accepted_history(state_daemon):
    """C-6.22, I3: case, files, slash, query and fragment normalize together."""
    service, harness = state_daemon
    canonical = "https://github.com/maxghenis/subfleet-v2/pull/307"
    first = submit(service, harness, "https://GitHub.COM/MaxGhenis/Subfleet-V2/pull/307/files/?diff=split#top")
    settle(service, first, "succeeded")
    second = submit(service, harness, canonical + "/?view=all#discussion")
    assert [service.store.get_job(job)["review_subject"] for job in (first, second)] == [canonical] * 2
    assert [service.store.get_job(job)["review_round"] for job in (first, second)] == [1, 2]


def test_explicit_round_can_repeat_an_accepted_round(state_daemon):
    """C-6.22: caller-given rounds retain their value after round 4 succeeds."""
    service, harness = state_daemon
    for round_ in range(1, 5):
        settle(service, submit(service, harness, "explicit", review_round=round_), "succeeded")
    repeated = submit(service, harness, "explicit", review_round=2)
    assert service.store.get_job(repeated)["review_round"] == 2
    assert_limit(service, harness, "explicit")


@pytest.mark.parametrize("task,changes", [
    ("research", {"review_subject": "pr"}),
    ("research", {"review_round": 1}),
    (None, {"review_subject": "pr"}),
    (None, {"review_round": 1}),
])
def test_other_tasks_refuse_review_subject_and_round(state_daemon, task, changes):
    """C-6.22: only review accepts subject/round, including on model-pinned jobs."""
    service, harness = state_daemon
    with pytest.raises((AdapterError, protocol.ProtocolError)) as error:
        service.dispatch("submit", harness.submit_args(task=task, **changes))
    assert error.value.reason == "review-fields"
    assert service.store.list_jobs() == []


def test_review_round_without_subject_is_refused(state_daemon):
    """C-6.22: no unscoped round can join or spend subject history."""
    service, harness = state_daemon
    with pytest.raises((AdapterError, protocol.ProtocolError)) as error:
        submit(service, harness, review_round=1)
    assert error.value.reason == "review-fields"
    assert service.store.list_jobs() == []


def test_i4_racing_submissions_allocate_inside_the_sql_transaction(state_daemon, monkeypatch):
    """C-6.22, I4: two independent stores share SQLite's write lock.

    Hold the first allocation until the second submission reaches its SQL
    transaction. BEGIN IMMEDIATE serializes it behind the first; when it reads
    history it must see the first pending job and refuse to spend round 2.
    Checking in_transaction also kills an allocation moved outside submission's
    transaction even when scheduling happens to conceal its stale read.
    """
    service, harness = state_daemon
    second_root = harness.root.parent / "second-state"
    second_root.mkdir()
    Harness(second_root)
    second = Daemon(second_root)
    second.store.close()
    second.store = Store(service.store.path)
    assert service._submit_lock is not second._submit_lock
    entered = threading.Event()
    second_transaction = threading.Event()
    release = threading.Event()
    real_assign = review_subject.assign_round
    real_transaction = second.store.transaction
    calls = []

    def assign(tx, subject, given=None):
        assert tx.in_transaction, "review round must be read inside its submission transaction"
        calls.append(threading.current_thread().name)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10), "test failed to release first allocator"
        return real_assign(tx, subject, given)

    def transaction(kind="state.changed", **kwargs):
        if kind == "job.submitted":
            second_transaction.set()
        return real_transaction(kind, **kwargs)

    monkeypatch.setattr(review_subject, "assign_round", assign)
    monkeypatch.setattr(second.store, "transaction", transaction)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first_future = workers.submit(submit, service, harness, "racing", name="racing-first")
            if not entered.wait(10):
                first_future.result(timeout=1)  # Preserve an allocator assertion's cause.
                pytest.fail("first submit never reached allocation")
            second_future = workers.submit(submit, second, harness, "racing", name="racing-second")
            try:
                assert second_transaction.wait(10), "second submit never reached its transaction"
                assert not second_future.done(), "a concurrent transaction bypassed SQLite's writer lock"
            finally:
                release.set()
            first = first_future.result(timeout=10)
            with pytest.raises(protocol.ProtocolError) as refusal:
                second_future.result(timeout=10)
        assert refusal.value.reason == "review-round-held" and refusal.value.code == 7
        assert refusal.value.details == {"job_id": first}
        assert service.store.get_job(first)["review_round"] == 1
        assert [job["job_id"] for job in service.store.list_jobs()] == [first]
        assert list((second.root / "jobs").iterdir()) == []
        assert len(calls) == 2
        settle(service, first, "failed")
        replacement = submit(service, harness, "racing", name="racing-replacement")
        assert service.store.get_job(replacement)["review_round"] == 1
    finally:
        release.set()
        second.close()


def _legacy_submit():
    assert hashlib.sha256(SUBMIT_SOURCE.encode()).hexdigest() == SUBMIT_SHA256
    assert hashlib.sha256(ARGS_SOURCE.encode()).hexdigest() == ARGS_SHA256
    assert hashlib.sha256(DIGEST_SOURCE.encode()).hexdigest() == DIGEST_SHA256
    namespace = dict(vars(daemon_module))
    args_namespace = dict(vars(protocol))
    exec(ARGS_SOURCE, args_namespace)
    legacy_args = args_namespace["SubmitArgs"]
    digest_namespace = dict(vars(daemon_module.ids))
    exec(DIGEST_SOURCE, digest_namespace)
    # Each baseline type/module is local: never replace the live protocol class
    # or mutate another test's imports while the oracle executes.
    namespace["protocol"] = SimpleNamespace(**{**vars(protocol), "SubmitArgs": legacy_args})
    namespace["ids"] = SimpleNamespace(**{**vars(daemon_module.ids),
                                         "payload_digest": digest_namespace["payload_digest"]})
    exec(SUBMIT_SOURCE, namespace)
    return namespace["submit"], legacy_args


@pytest.mark.parametrize("task", [None, "review", "research"])
def test_i5_no_subject_rows_and_events_match_release_217(state_daemon, tmp_path, monkeypatch, task):
    """C-6.22, I5: compare full submission rows/events to the baseline method."""
    service, harness = state_daemon
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_harness = Harness(legacy_root)
    legacy = Daemon(legacy_root)
    monkeypatch.setattr(daemon_module, "utcnow", lambda: STAMP)
    monkeypatch.setattr(store_module, "utc_now", lambda: STAMP)
    monkeypatch.setattr(daemon_module.ids, "job_id", lambda *args, **kwargs: "comparison-job")
    old_submit, old_args = _legacy_submit()
    # The old function's globals were copied before utcnow was patched.
    old_submit.__globals__["utcnow"] = lambda: STAMP
    args = harness.submit_args(request_id="comparison-request", task=task,
                               tier="standard" if task else None)
    baseline_fields = {field.name for field in fields(old_args)}
    try:
        # Both submitters inspect exactly the same fixture workdir/prompt, so
        # opaque digests are comparable without dropping any legacy column.
        old_reply = old_submit(legacy, old_args(**{k: v for k, v in args.items() if k in baseline_fields}))
        reply = service.dispatch("submit", args)
        assert reply == old_reply
        current = service.store.get_job(reply["job_id"])
        before = legacy.store.get_job(old_reply["job_id"])
        assert {key: current[key] for key in NEW_FIELDS} == dict.fromkeys(NEW_FIELDS)
        assert {key: before[key] for key in NEW_FIELDS} == dict.fromkeys(NEW_FIELDS)

        def canonical(value, root):
            if isinstance(value, dict):
                return {key: canonical(item, root) for key, item in value.items() if key not in NEW_FIELDS}
            if isinstance(value, list):
                return [canonical(item, root) for item in value]
            if isinstance(value, str):
                return value.replace(str(service.root), "<state>").replace(str(legacy.root), "<state>")
            return value

        assert canonical(current, service.root) == canonical(before, legacy.root)
        current_events = service.store.query("SELECT * FROM events WHERE job_id=? ORDER BY event_id", (reply["job_id"],))
        old_events = legacy.store.query("SELECT * FROM events WHERE job_id=? ORDER BY event_id", (old_reply["job_id"],))
        assert canonical(current_events, service.root) == canonical(old_events, legacy.root)
    finally:
        legacy.close()
    legacy_harness.check_notices()
