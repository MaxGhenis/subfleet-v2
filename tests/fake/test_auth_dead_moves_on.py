"""C-4.5, C-23.44: an auth-dead attempt closes its lane, and the job moves on.

Incident, 2026-09-30: between 15:43:20Z and 15:44:00Z admission reserved 45
attempts on claude-5, the only open Claude lane once its five-hour closure ended.
Its organisation had disabled Claude Code subscription access in between. Each
attempt finished `auth-dead: Your organization has disabled Claude subscription
access for Claude Code`. The first finish disabled the lane (15:44:53Z) and nothing
was reserved there after it, but every one of the 45 jobs failed with rc 5,
although only the lane's credential was dead.
"""

import pytest

from subfleet import daemon as module
from subfleet.contracts import Outcome, OutcomeClass
from tests.fake.test_state_contract import state_daemon, reserve, receipt_fixture  # noqa: F401 - fixtures
from tests.fake_adapter import FakeAdapter

ORG_BLOCK = ("auth-dead: Your organization has disabled Claude subscription access for Claude Code · "
             "Use an Anthropic API key instead, or ask your admin to enable access")


@pytest.fixture
def auth_dead(monkeypatch):
    class AuthDead(FakeAdapter):
        def classify(self, *args):
            return Outcome(OutcomeClass.AUTH_DEAD, ORG_BLOCK)
    monkeypatch.setattr(module, "get_adapter", lambda _: AuthDead())


def finish(daemon, attempt, adir):
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=1))
    return daemon.store.get_attempt(attempt["attempt_id"])


def test_c4_5_an_unpinned_job_whose_attempt_is_auth_dead_is_retried_and_its_lane_closed(state_daemon, auth_dead):
    """C-4.5, C-23.44 the lane is disabled in the same transaction; the job waits for another lane, not rc 5."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=3)
    row = finish(daemon, attempt, adir)
    assert (row["state"], row["outcome_class"], row["rc"]) == ("failed", "auth-dead", 1)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"], job["rc"], job["finished_at"]) == ("waiting", "capacity", None, None)
    assert not daemon.store.get_lane(attempt["lane_id"]).enabled
    assert daemon.store.list_notices() == []                 # not an end: nobody is told the job failed
    assert not daemon.store.query("SELECT 1 FROM leases WHERE holder=?", (attempt["attempt_id"],))


def test_c4_5_a_retried_auth_dead_job_is_never_placed_on_the_dead_lane(state_daemon, auth_dead):
    """C-23.44 the next pass finds the lane disabled: with no other lane the job waits, with no attempt there."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=3)
    finish(daemon, attempt, adir)
    daemon.store.update_job(job_id, next_check_at=None)
    daemon._admit()
    attempts = daemon.store.list_attempts(job_id)
    assert [a["attempt_id"] for a in attempts] == [attempt["attempt_id"]]
    assert daemon.store.get_job(job_id)["state"] in ("queued", "waiting")


@pytest.mark.parametrize("pinned,max_attempts", [(True, 3), (False, 1)])
def test_c17_3_a_pinned_or_exhausted_auth_dead_job_still_fails_with_rc_5(state_daemon, auth_dead, pinned, max_attempts):
    """C-17.3 a lane pin cannot move, and `max_attempts` bounds the retries: those end rc 5, as before."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=max_attempts,
                                    pinned_lane="codex-1" if pinned else None)
    finish(daemon, attempt, adir)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 5)
    assert not daemon.store.get_lane(attempt["lane_id"]).enabled
    assert len(daemon.store.list_notices()) == 1


def test_c9_3_the_2026_09_30_message_is_auth_dead():
    """C-9.3 the exact provider text of 2026-09-30 is the explicit organisation block."""
    from subfleet.adapters.claude import ORG_BLOCK_RE
    assert ORG_BLOCK_RE.search(ORG_BLOCK.split(": ", 1)[1])
