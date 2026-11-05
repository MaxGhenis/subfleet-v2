"""C-8.4, C-15.2, C-15.3, C-23.26 (D-NOTICE-KEEP1): a job retired with its notice unread
leaves its session an outcome, and the session's next delivery shows it.

Against the real daemon in process (`state_daemon`): a cancelled job writes its C-15.1
notice for the session, the notice ages past `notice_keep_days`, a retention pass on the
daemon's own state root retires the job, and the session hooks (`surface`, through
`notice.pending` and `notice.mark`) then print the outcome: the notice's first line and
the retirement. The outcome names its job, so a session Subfleet launched (C-26.13) is
shown it too. It pins nothing, and once it has waited `notice_keep_days` unread the
daemon's own service-notice prune withdraws it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from subfleet import retention
from subfleet import retention_notices as rnotices
from tests.fake.test_notice_delivery import job_notice, no_wake, surface  # noqa: F401 - `no_wake` is a fixture
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - the fixture

DAY = 86400.0


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def age_notice(daemon, notice_id: int, days: float) -> None:
    with daemon.store.transaction("test.aged") as tx:
        tx.execute("UPDATE notices SET created_at=? WHERE notice_id=?",
                   (stamp(datetime.now(UTC) - timedelta(days=days)), notice_id))


def retire(daemon, keep_days: float = 7) -> dict:
    return retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                 notice_keep_s=keep_days * DAY, holders=lambda watches, **_: {})


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
@pytest.mark.parametrize("launched", [False, True], ids=["session", "launched"])
def test_the_outcome_reaches_the_session_on_its_next_delivery(state_daemon, no_wake, event, launched):
    daemon, harness = state_daemon
    job_id, notice_id = job_notice(daemon, harness, "s-gone")
    first_line = daemon.store.one("SELECT text FROM notices WHERE notice_id=?", (notice_id,))["text"].split("\n")[0]
    assert first_line.startswith(f"{job_id}: cancelled; rc=")
    age_notice(daemon, notice_id, 8)

    result = retire(daemon)
    assert job_id in result["pruned"] and daemon.store.get_job(job_id) is None
    assert daemon.store.query("SELECT * FROM notices WHERE job_id=?", (job_id,)) == []

    # `notice.pending` names the job, as it names a pin notice's (C-11.8).
    (pending,) = daemon.dispatch("notice.pending", {"session_id": "s-gone"})["notices"]
    assert pending["notice_id"] < 0 and pending["job_id"] == job_id
    (listed,) = daemon.dispatch("notice.list", {"session_id": "s-gone"})["notices"]
    assert listed["job_id"] == job_id

    env = {"SUBFLEET_JOB": "20261105-000000-turn"} if launched else {}
    context = surface(daemon, harness.root, "s-gone", event, env=env)
    assert context.startswith("subfleet: 1 message for this session:"), context
    assert "detached run" not in context                    # a message, not a run's end (C-15.3)
    lines = context.split("\n")
    assert first_line in lines
    retired = lines[lines.index(first_line) + 1]
    assert retired.startswith("retention retired this job at ") and "with its notice unread" in retired
    assert "retention.notice_keep_days (7 days)" in retired
    assert f"subfleet retention restore {job_id} --to DIR" in retired
    (row,) = daemon.store.query("SELECT * FROM service_notices WHERE session_id='s-gone'")
    assert row["state"] == "surfaced" and row["transport"] == f"hook:{event}"
    assert surface(daemon, harness.root, "s-gone", event, env=env) == ""     # shown once

    # Whatever carried it pins nothing: no pin names the job, even keeping for ever.
    for keep_s in (0.0, 7 * DAY):
        assert job_id not in retention._pin_reasons(daemon.store, set(), None, notice_keep_s=keep_s)


def test_a_fresh_notice_keeps_its_job_and_writes_nothing(state_daemon):
    daemon, harness = state_daemon
    job_id, notice_id = job_notice(daemon, harness, "s-away")
    age_notice(daemon, notice_id, 6)
    result = retire(daemon)
    assert job_id not in result["pruned"] and result["pin_reasons"][job_id] == "unread-notice"
    assert daemon.store.query("SELECT * FROM service_notices") == []


def test_the_daemon_withdraws_an_outcome_unread_for_notice_keep_days(state_daemon):
    """C-23.26: `_prune_service_notices`, once per retention call, withdraws an outcome
    no one was shown within the policy's `notice_keep_days`; one younger stays, and 0
    keeps them all."""
    daemon, harness = state_daemon
    written = {}
    for name, days in (("old", 8), ("young", 6)):
        job_id, notice_id = job_notice(daemon, harness, f"s-{name}")
        age_notice(daemon, notice_id, days + 8)
        with daemon.store.transaction("test.outcome") as conn:
            (record,) = rnotices.write_outcomes(conn, job_id, keep_s=7 * DAY, archive=None, name=None,
                                                now=datetime.now(UTC) - timedelta(days=days))
        written[name] = record["service_notice_id"]
    daemon.policy = {**daemon.policy, "retention": {**daemon.policy.get("retention", {}), "notice_keep_days": 0}}
    assert daemon._prune_service_notices() == 0
    daemon.policy = {**daemon.policy, "retention": {**daemon.policy["retention"], "notice_keep_days": 7}}
    assert daemon._prune_service_notices() == 1
    left = {row["notice_id"] for row in daemon.store.query("SELECT notice_id FROM service_notices")}
    assert left == {written["young"]}
    del daemon.policy["retention"]["notice_keep_days"]                  # the default, 7
    assert daemon._prune_service_notices() == 0
