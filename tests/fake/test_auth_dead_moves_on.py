"""C-4.5, C-23.44: an auth-dead attempt closes its lane, and the job moves on.

Incident, 2026-09-30: between 15:43:20Z and 15:44:00Z admission reserved 37
attempts (37 jobs, 24 read-only and 13 writable) on claude-5, the only open Claude
lane once its five-hour closure ended. Its organisation had disabled Claude Code
subscription access in between. Each attempt finished `auth-dead: Your organization
has disabled Claude subscription access for Claude Code`. The first finish disabled
the lane (15:44:53Z) and nothing was reserved there after it, but every one of the 37
jobs failed with rc 5, although only the lane's credential was dead.

Two guards keep a misread from spreading (design review, 2026-09-30): a run that
exited 0 authenticated, so its auth-dead is its text's (#84 stops reading prose);
and a job auth-dead on a second lane ends there.
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


def finish(daemon, attempt, adir, rc=1):
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=rc))
    return daemon.store.get_attempt(attempt["attempt_id"])


def second_lane(daemon, lane_id="codex-2"):
    from dataclasses import replace
    lane = daemon.store.get_lane("codex-1")
    daemon.store.put_lane(replace(lane, lane_id=lane_id, account_key=f"codex:fake-{lane_id}"))


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


def test_c4_5_the_retry_runs_on_another_lane_and_a_second_dead_lane_ends_the_job(state_daemon, auth_dead):
    """C-4.5, C-17.3: dead on codex-1, the job is placed on codex-2; dead there too, it ends rc 5 with two
    attempts though it had a third, and both lanes are disabled."""
    daemon, harness = state_daemon
    second_lane(daemon)
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=3)
    finish(daemon, attempt, adir)
    daemon._admit()
    retried = daemon.store.list_attempts(job_id)[-1]
    assert (retried["seq"], retried["lane_id"]) == (2, "codex-2")
    daemon._pending_launches.discard(retried["attempt_id"])
    retried_dir = daemon.root / "jobs" / job_id / "a2"
    retried_dir.mkdir(mode=0o700)
    finish(daemon, retried, retried_dir)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 5)
    assert not daemon.store.get_lane("codex-1").enabled and not daemon.store.get_lane("codex-2").enabled
    notice, = daemon.store.list_notices()
    assert "\nearlier: a1 auth-dead on codex-1 (disabled; subfleet lanes enroll)" in notice["text"]


def test_c9_2_an_auth_dead_run_that_exited_0_is_not_retried(state_daemon, auth_dead):
    """C-9.2: a run that exited 0 authenticated; an auth-dead verdict on it is its text's (#84 stops reading
    prose), and moving it on would disable lane after lane. It ends rc 5 on its one lane, as before."""
    daemon, harness = state_daemon
    second_lane(daemon)
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=3)
    finish(daemon, attempt, adir, rc=0)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 5)
    assert daemon.store.get_lane("codex-2").enabled


def test_c6_11_why_says_where_a_retried_job_moved_on_from(state_daemon, auth_dead):
    """C-6.11, C-23.44: a waiting retry names the dead lane and the fix, since no notice says it."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=3)
    finish(daemon, attempt, adir)
    text = daemon.dispatch("why", {"job_id": job_id})["text"]
    assert f"Earlier attempts: a1 auth-dead on {attempt['lane_id']} (that lane is disabled; subfleet lanes enroll)" in text
