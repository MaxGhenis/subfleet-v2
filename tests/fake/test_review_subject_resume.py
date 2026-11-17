"""A resumed review finishes its source round instead of losing its subject."""

from subfleet import protocol
from tests.fake.test_review_subject_submission import settle, submit
from tests.fake.test_state_contract import state_daemon  # noqa: F401


def test_failed_review_resume_finishes_the_original_round(state_daemon):
    service, harness = state_daemon
    subject = 'synthetic-review-resume'
    source = submit(service, harness, subject)
    settle(service, source, 'failed')
    service.store.update_attempt(f'{source}/a1', native_session_id='synthetic-native-thread')
    resumed = service.submit(protocol.SubmitArgs(**harness.submit_args(
        kind='resume', parent_job_id=source, name='resumed-review')))["job_id"]
    row = service.store.get_job(resumed)
    assert (row['task'], row['review_subject'], row['review_round']) == ('review', subject, 1)
    settle(service, resumed, 'succeeded')
    following = submit(service, harness, subject)
    assert service.store.get_job(following)['review_round'] == 2
