"""C-8.4 (D-NOTICE-KEEP1): an unread notice keeps its job for a set time, not for ever.

An unread notice (`pending` or `offered`) addressed to a session pins its job for
`retention.notice_keep_days` after the notice's `created_at`, and nothing after that;
0 keeps it for ever; a notice with no session pins nothing at any age (IR-17). The
offline survey and a pass agree on every pin. A job retired with its notice still
unread leaves that session an outcome notice, which pins nothing and ends.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import retention
from subfleet import retention_notices as rnotices
from subfleet.contracts import NOTICE_RETENTION_KEEP_DAYS
from subfleet.policy import RETENTION_DEFAULTS
from subfleet.retention_survey import survey
from subfleet.store import Store, pin_notice_jobs
from tests.unit.retention_world import World, trust_temporary_directories

DAY = 86400.0
#: Half a second past the second, so the cutoff falls inside a stored second.
NOW = datetime(2026, 11, 12, 6, 0, 0, 500000, tzinfo=UTC)
PROPERTY = settings(max_examples=60, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.too_slow])


def stamp(moment: datetime) -> str:
    """The notices' own spelling (C-1.7): UTC, seconds, `Z`."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def ago(seconds: float, now: datetime = NOW) -> str:
    return stamp(now - timedelta(seconds=seconds))


def add_job(store: Store, root: Path, job_id: str, *, state: str = "succeeded") -> None:
    store.add_job(job_id=job_id, request_id=job_id, payload_digest="digest", kind="dispatch", workdir=str(root),
                  prompt_path="/prompt", sandbox="read-only", state=state, created_at="2026-10-01T00:00:00Z",
                  finished_at="2026-10-01T00:01:00Z")
    directory = root / "jobs" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stdout").write_bytes(b"output of " + job_id.encode())


def header(job_id: str) -> str:
    return f"{job_id}: succeeded; rc=0; deliverable=-; out=/tmp/out-{job_id}.md"


def add_notice(store: Store, job_id: str, session: str | None, created_at: str, state: str = "pending") -> int:
    notice_id = store.add_notice(job_id, header(job_id) + "\nattempt a1: ok, rc=0: done", session, state=state)
    store.update_notice(notice_id, created_at=created_at)
    return notice_id


def pins(store: Store, keep_s: float, now: datetime = NOW) -> dict[str, str]:
    return retention._pin_reasons(store, set(), None, notice_keep_s=keep_s, now=now)


@contextmanager
def scratch():
    """A store and state root for one example (Hypothesis runs many per test)."""
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None) as directory:
        root = Path(directory)
        (root / "jobs").mkdir()
        with Store(root / "state.sqlite3") as store:
            yield store, root


@pytest.fixture
def retained(tmp_path):
    (tmp_path / "jobs").mkdir()
    with Store(tmp_path / "state.sqlite3") as store:
        yield store, tmp_path


def no_holders(watches, **_):
    return {}


# --- the setting -----------------------------------------------------------------------------

def test_the_default_is_seven_days_and_the_pass_uses_it():
    assert NOTICE_RETENTION_KEEP_DAYS == 7 and RETENTION_DEFAULTS["notice_keep_days"] == 7
    import inspect
    assert inspect.signature(retention.maintenance).parameters["notice_keep_s"].default == 7 * DAY


@pytest.mark.parametrize("keep_s", [-1.0, float("nan")])
def test_a_negative_or_unordered_keep_is_refused(retained, keep_s):
    store, root = retained
    with pytest.raises(ValueError, match="nonnegative"):
        retention.maintenance(store, root, notice_keep_s=keep_s, holders=no_holders)


# --- boundaries ------------------------------------------------------------------------------

@pytest.mark.parametrize("keep_days", [1, 7, 30])
def test_a_notice_just_under_the_keep_pins_and_one_just_over_does_not(retained, keep_days):
    """C-8.4: one second younger than the keep time pins its job; one second older does not."""
    store, root = retained
    keep_s = keep_days * DAY
    for job_id, age in (("under", keep_s - 1), ("at", keep_s), ("over", keep_s + 1)):
        add_job(store, root, job_id)
        add_notice(store, job_id, "session-1", ago(age))
    # `at` was written at 06:00:00, half a second before the cutoff (NOW less the keep):
    # stored times have whole seconds, so it is older than the keep, by half a second.
    assert pins(store, keep_s) == {"under": "unread-notice"}
    assert pins(store, keep_s, NOW - timedelta(seconds=0.5)) == {"under": "unread-notice"}


def test_zero_keeps_an_unread_notice_for_ever(retained):
    """C-8.4: `notice_keep_days` 0 is the rule before D-NOTICE-KEEP1: no age expires."""
    store, root = retained
    for job_id, created in (("ten-years", "2016-11-12T06:00:00Z"), ("today", ago(1)),
                            ("unparsed", "not a time")):
        add_job(store, root, job_id)
        add_notice(store, job_id, "session-1", created)
    assert pins(store, 0) == dict.fromkeys(("ten-years", "today", "unparsed"), "unread-notice")
    assert set(pins(store, 7 * DAY)) == {"today", "unparsed"}    # a time that does not parse keeps its job


def test_zero_keeps_for_ever_through_a_pass(retained):
    store, root = retained
    add_job(store, root, "ancient")
    add_notice(store, "ancient", "session-1", "2016-01-01T00:00:00Z")
    add_job(store, root, "free")
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0, notice_keep_s=0, holders=no_holders)
    assert result["pruned"] == ["free"] and result["pin_reasons"]["ancient"] == "unread-notice"
    assert store.get_job("ancient") is not None


@pytest.mark.parametrize("session", [None, ""])
@pytest.mark.parametrize("keep_s", [0.0, 7 * DAY, 30 * DAY])
def test_a_notice_with_no_session_pins_nothing_at_any_age(retained, session, keep_s):
    """C-8.4, IR-17: no session can read it, so it keeps nothing, fresh or old."""
    store, root = retained
    for job_id, age in (("fresh", 1), ("old", 400 * DAY)):
        add_job(store, root, job_id)
        add_notice(store, job_id, session, ago(age))
    assert pins(store, keep_s) == {}


@pytest.mark.parametrize("state", ["surfaced", "acknowledged"])
def test_a_read_notice_pins_nothing_at_any_age(retained, state):
    store, root = retained
    add_job(store, root, "read")
    add_notice(store, "read", "session-1", ago(1), state=state)
    assert pins(store, 0) == {} and pins(store, 7 * DAY) == {}


def test_offered_is_unread(retained):
    store, root = retained
    add_job(store, root, "offered")
    add_notice(store, "offered", "session-1", ago(1), state="offered")
    assert pins(store, 7 * DAY) == {"offered": "unread-notice"}


def test_one_fresh_notice_keeps_a_job_whatever_its_other_notices_are(retained):
    store, root = retained
    add_job(store, root, "two")
    add_notice(store, "two", "session-1", ago(30 * DAY))
    add_notice(store, "two", "session-2", ago(DAY))
    assert pins(store, 7 * DAY) == {"two": "unread-notice"}


def test_ages_compare_as_times_not_as_strings(retained):
    """D-NOTICE-KEEP1: notices store `...Z` seconds, turn retention spells its cutoff
    with milliseconds and `+00:00`; compared as strings, a notice written in the
    cutoff's own second but before it reads as younger (`Z` sorts after `.`), and a
    spelling with a space for `T` reads a day early. Each is compared as a time."""
    store, root = retained
    keep_s = 7 * DAY
    cut = NOW - timedelta(seconds=keep_s)                     # 2026-11-05T06:00:00.500+00:00
    cases = {
        "same-second-before": (stamp(cut), False),            # 06:00:00Z, half a second older than the cut
        "next-second": (stamp(cut + timedelta(seconds=1)), True),
        "millis-after": ((cut + timedelta(milliseconds=250)).isoformat(timespec="milliseconds"), True),
        "millis-before": ((cut - timedelta(milliseconds=250)).isoformat(timespec="milliseconds"), False),
        "space-naive-after": ((cut + timedelta(hours=1)).replace(tzinfo=None).isoformat(sep=" ",
                                                                                      timespec="seconds"), True),
        "offset-after": ((cut + timedelta(hours=2)).astimezone(timezone(timedelta(hours=-5)))
                         .isoformat(timespec="seconds"), True),           # 03:00:00-05:00 is 08:00 UTC
    }
    for job_id, (created, _) in cases.items():
        add_job(store, root, job_id)
        add_notice(store, job_id, "session-1", created)
    assert set(pins(store, keep_s)) == {job_id for job_id, (_, fresh) in cases.items() if fresh}


def test_a_keep_too_long_for_the_calendar_keeps_everything(retained):
    """A keep that reaches before year 1 expires nothing rather than raising."""
    store, root = retained
    add_job(store, root, "kept")
    add_notice(store, "kept", "session-1", "2016-01-01T00:00:00Z")
    assert pins(store, 1e300) == {"kept": "unread-notice"}
    assert rnotices.cutoff(NOW, 1e300) is None and rnotices.cutoff(NOW, 0) is None


# --- properties --------------------------------------------------------------------------------

JOBS = [f"j{n}" for n in range(5)]
SESSIONS = st.sampled_from([None, "", "s1", "s2"])
STATES = st.sampled_from(["pending", "offered", "surfaced", "acknowledged"])
KEEPS = st.sampled_from([0.0, 3600.0, 7 * DAY, 30 * DAY])
EDGES = [3600 - 1, 3600, 3600 + 1, 7 * DAY - 1, 7 * DAY, 7 * DAY + 1, 30 * DAY - 1, 30 * DAY, 30 * DAY + 1]
AGES = st.one_of(st.integers(min_value=-3600, max_value=40 * 86400), st.sampled_from(EDGES))
NOTICES = st.lists(st.tuples(st.sampled_from(JOBS), SESSIONS, STATES, AGES), max_size=12)


def seed(store: Store, root: Path, notices) -> list[int]:
    for job_id in JOBS:
        add_job(store, root, job_id)
    return [add_notice(store, job_id, session, ago(age), state) for job_id, session, state, age in notices]


def model(notices, keep_s: float, now: datetime = NOW) -> set[str]:
    """The jobs with at least one pending or offered notice that has a session and is
    younger than the keep time (as stored: whole seconds), or any age when it is 0."""
    oldest = now - timedelta(seconds=keep_s) if keep_s else None
    return {job_id for job_id, session, state, age in notices
            if state in ("pending", "offered") and session
            and (oldest is None or datetime.fromisoformat(ago(age, now)) > oldest)}


@PROPERTY
@given(notices=NOTICES, keep_s=KEEPS)
def test_property_the_pinned_set_is_the_jobs_with_a_fresh_unread_notice_to_a_session(notices, keep_s):
    with scratch() as (store, root):
        seed(store, root, notices)
        reasons = pins(store, keep_s)
        assert set(reasons) == model(notices, keep_s)
        assert set(reasons.values()) <= {"unread-notice"}


@PROPERTY
@given(notices=NOTICES, keep_s=KEEPS, steps=st.lists(st.integers(min_value=0, max_value=20 * 86400),
                                                    min_size=1, max_size=4))
def test_property_the_pinned_set_only_shrinks_as_the_clock_moves_forward(notices, keep_s, steps):
    with scratch() as (store, root):
        seed(store, root, notices)
        now, before = NOW, set(pins(store, keep_s, NOW))
        for step in steps:
            now += timedelta(seconds=step)
            after = set(pins(store, keep_s, now))
            assert after <= before, (now, after - before)
            before = after


@PROPERTY
@given(notices=NOTICES.filter(bool), keep_s=KEEPS, data=st.data())
def test_property_acknowledging_a_notice_never_adds_a_pin(notices, keep_s, data):
    with scratch() as (store, root):
        ids = seed(store, root, notices)
        before = set(pins(store, keep_s))
        for notice_id in data.draw(st.lists(st.sampled_from(ids), min_size=1, unique=True)):
            store.update_notice(notice_id, state=data.draw(st.sampled_from(["acknowledged", "surfaced"])))
            after = set(pins(store, keep_s))
            assert after <= before, after - before
            before = after


# --- the survey and a pass agree ----------------------------------------------------------------

#: (job, session, age in days, state): the ages straddle 7 and 30 days by a day or more,
#: so the seconds between the survey and the pass change nothing.
DIFFERENTIAL = [
    ("hour", "s1", 1 / 24, "pending"), ("six", "s1", 6, "pending"), ("eight", "s2", 8, "offered"),
    ("twenty-nine", "s1", 29, "pending"), ("thirty-one", "s2", 31, "pending"), ("year", "s1", 400, "pending"),
    ("no-session", None, 1, "pending"), ("blank-session", "", 400, "pending"), ("read", "s1", 1, "surfaced"),
    ("acknowledged", "s2", 400, "acknowledged"), ("no-notice", None, None, None),
]


@pytest.mark.parametrize("keep_days", [0, 7, 30])
def test_the_survey_and_a_pass_give_the_same_pins(retained, keep_days):
    """D-NOTICE-KEEP1: the survey reads `notice_keep_days` from the state root's policy
    as the daemon hands it to the pass, so both keep and retire the same jobs."""
    store, root = retained
    now = datetime.now(UTC)
    for job_id, session, days, state in DIFFERENTIAL:
        add_job(store, root, job_id)
        if days is not None:
            add_notice(store, job_id, session, ago(days * DAY, now), state)
    (root / "policy.json").write_text(json.dumps({"retention": {"notice_keep_days": keep_days}}))
    report = survey(root, sizes=False, holders=False, sample_throughput=False,
                    budgets={"detached": (0, 0), "turn": (0, 0)})
    predicted = {row["job_id"] for row in report["candidates"]}
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0, notice_keep_s=keep_days * DAY,
                                   holders=no_holders)
    everyone = {job_id for job_id, *_ in DIFFERENTIAL}
    assert set(result["pin_reasons"]) == everyone - predicted
    assert Counter(result["pin_reasons"].values()) == Counter(report["kept"]["jobs_by_reason"])
    assert set(result["pruned"]) == predicted
    assert report["notice_keep_days"] == keep_days
    expected_kept = {0: {"hour", "six", "eight", "twenty-nine", "thirty-one", "year"},
                     7: {"hour", "six"}, 30: {"hour", "six", "eight", "twenty-nine"}}[keep_days]
    assert set(result["pin_reasons"]) == expected_kept


def test_the_sampler_reads_the_same_keep(retained, monkeypatch):
    """`survey --sample` picks among unpinned jobs: an expired notice's job is one."""
    from subfleet import retention_survey
    store, root = retained
    seen = []
    real = retention._pin_reasons

    def spy(*args, **kwargs):
        seen.append(kwargs.get("notice_keep_s"))
        return real(*args, **kwargs)
    monkeypatch.setattr(retention_survey.ret, "_pin_reasons", spy)
    (root / "policy.json").write_text(json.dumps({"retention": {"notice_keep_days": 3}}))
    retention_survey.sample(root, 1)
    assert seen == [3 * DAY]


# --- a real retirement -------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


@pytest.mark.parametrize("age_days, retired", [(8, True), (6, False)])
def test_a_pass_retires_a_job_whose_only_pin_is_an_expired_notice(world, age_days, retired):
    """With its worktree, on a fake state root: an expired notice keeps nothing and the
    job is archived and retired; the same job with a fresh notice stays, pinned by it."""
    w = world
    worktree = w.job("job-n")
    add_notice(w.store, "job-n", "session-gone", ago(age_days * DAY, datetime.now(UTC)))
    result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, notice_keep_s=7 * DAY,
                                   holders=no_holders)
    if retired:
        assert result["pruned"] == ["job-n"], result
        assert w.store.get_job("job-n") is None and not worktree.exists()
        rows = json.loads((w.root / "archive" / "job-n" / "rows.json").read_text())["rows"]
        assert [n["session_id"] for n in rows["notices"]] == ["session-gone"]     # the notice is archived
        assert "job-n" not in pins(w.store, 0, datetime.now(UTC))
    else:
        assert result["pruned"] == [] and result["pin_reasons"] == {"job-n": "unread-notice"}
        assert w.store.get_job("job-n") is not None and worktree.exists()
        assert w.store.query("SELECT * FROM service_notices") == []


# --- the outcome -------------------------------------------------------------------------------

def outcomes(store: Store, session: str | None = None) -> list[dict]:
    where, params = (" WHERE session_id=?", (session,)) if session else ("", ())
    return store.query(f"SELECT * FROM service_notices{where} ORDER BY notice_id", params)


def retire(store: Store, root: Path, keep_s: float = 7 * DAY) -> dict:
    return retention.maintenance(store, root, max_jobs=0, max_bytes=0, notice_keep_s=keep_s, holders=no_holders)


def test_retiring_a_job_with_an_unread_notice_writes_its_session_the_outcome(retained):
    store, root = retained
    add_job(store, root, "gone")
    add_notice(store, "gone", "session-gone", ago(8 * DAY, datetime.now(UTC)))
    result = retire(store, root)
    assert result["pruned"] == ["gone"]
    (row,) = outcomes(store)
    assert row["session_id"] == "session-gone" and row["state"] == "pending"
    first, second = row["text"].split("\n")
    assert first == header("gone")
    assert second.startswith(f"retention retired this job at {row['created_at']} with its notice unread (queued ")
    assert "retention.notice_keep_days (7 days)" in second
    archive = root.resolve() / "archive" / "gone"
    assert f"Its files and rows are in {archive};" in second and archive.is_dir()
    assert "`subfleet retention restore gone --to DIR`" in second
    # The event names it, as a pin notice's does, and only by id, session and creation time.
    (event,) = [e for e in store.list_events("gone") if e["kind"] == rnotices.RETIRED_EVENT]
    data = json.loads(event["data_json"])
    assert (data["service_notice_id"], data["session_id"], data["created_at"]) == (
        row["notice_id"], "session-gone", row["created_at"])
    assert pin_notice_jobs(store.query, [row]) == {row["notice_id"]: "gone"}
    (pruned,) = [json.loads(e["data_json"]) for e in store.list_events("gone")
                 if e["kind"] == "retention.pruned" and e["data_json"] != "{}"]
    assert pruned["outcome_notices"] == [row["notice_id"]]


def test_one_outcome_per_session_and_none_for_a_notice_no_one_can_read_or_has_read(retained):
    store, root = retained
    old = ago(9 * DAY, datetime.now(UTC))
    add_job(store, root, "many")
    add_notice(store, "many", "s1", old)
    add_notice(store, "many", "s1", ago(8 * DAY, datetime.now(UTC)))      # one more for s1 (C-13.1's `again`)
    add_notice(store, "many", "s2", old, state="offered")
    add_notice(store, "many", "s3", old, state="surfaced")                # read: nothing to tell
    add_notice(store, "many", None, old)                                  # no reader (IR-17)
    add_notice(store, "many", "", old)
    assert retire(store, root)["pruned"] == ["many"]
    told = {row["session_id"]: row["text"] for row in outcomes(store)}
    assert set(told) == {"s1", "s2"}
    assert told["s1"].count(header("many")) == 1                          # the same first line once
    assert told["s1"].split("\n")[1].count("queued ") == 1


def test_a_job_without_an_unread_notice_writes_no_outcome(retained):
    store, root = retained
    add_job(store, root, "quiet")
    assert retire(store, root)["pruned"] == ["quiet"]
    assert outcomes(store) == [] and not [e for e in store.list_events("quiet")
                                          if e["kind"] == rnotices.RETIRED_EVENT]


def test_outcome_and_job_deletion_roll_back_together(retained, monkeypatch):
    store, root = retained
    add_job(store, root, "rollback")
    add_notice(store, "rollback", "s1", ago(8 * DAY, datetime.now(UTC)))
    original = rnotices.write_outcomes

    def fail_after_writing(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("outcome transaction interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(rnotices, "write_outcomes", fail_after_writing)
        with pytest.raises(RuntimeError, match="outcome transaction interrupted"):
            retire(store, root)
    assert store.get_job("rollback") is not None
    assert len(store.query("SELECT * FROM notices WHERE job_id='rollback'")) == 1
    assert outcomes(store) == []
    assert store.query("SELECT * FROM events WHERE kind=?", (rnotices.RETIRED_EVENT,)) == []
    assert retire(store, root)["pruned"] == ["rollback"]
    assert len(outcomes(store)) == 1


def test_the_outcome_pins_nothing(retained):
    """Written beside a job that still has its rows, a fresh outcome adds no pin; once
    the job is retired, no pin names it at any keep, for ever included."""
    store, root = retained
    now = datetime.now(UTC)
    add_job(store, root, "job")
    add_notice(store, "job", "s1", ago(8 * DAY, now))
    assert pins(store, 7 * DAY, now) == {}
    with store.transaction("test.outcome") as conn:
        rnotices.write_outcomes(conn, "job", keep_s=7 * DAY, archive=None, name=None, now=now)
    assert outcomes(store, "s1") and pins(store, 7 * DAY, now) == {}
    assert retire(store, root)["pruned"] == ["job"]
    assert len(outcomes(store, "s1")) == 2
    for keep_s in (0.0, 7 * DAY):
        assert pins(store, keep_s, datetime.now(UTC)) == {}


def test_an_outcome_for_a_fractional_keep_names_it(retained):
    assert "(0.5 days)" in rnotices.outcome_text(["a: x"], ["t"], "r", 0.5 * DAY, None, None)
    assert "(1 day)" in rnotices.outcome_text(["a: x"], ["t"], "r", DAY, None, None)
    text = rnotices.outcome_text(["a: x"], ["t"], "r", 0, None, None)
    assert "notice_keep_days" not in text and "Its files" not in text


# --- how the outcome ends ----------------------------------------------------------------------

def write_outcome(store: Store, root: Path, job_id: str, session: str, written: datetime) -> int:
    add_job(store, root, job_id)
    add_notice(store, job_id, session, ago(8 * DAY, written))
    with store.transaction("test.outcome") as conn:
        (record,) = rnotices.write_outcomes(conn, job_id, keep_s=7 * DAY, archive=None, name=None, now=written)
    return record["service_notice_id"]


def test_an_undelivered_outcome_is_withdrawn_after_the_keep_and_a_delivered_one_is_left(retained):
    """C-23.26 (D-NOTICE-KEEP1): an outcome no one was shown goes `notice_keep_days`
    after it was written; one a hook surfaced goes by the 14-day rule instead; a ping
    of the same age is never pruned by age; 0 withdraws nothing."""
    store, root = retained
    old = write_outcome(store, root, "old", "s-old", NOW - timedelta(days=8))
    young = write_outcome(store, root, "young", "s-young", NOW - timedelta(days=6))
    seen = write_outcome(store, root, "seen", "s-seen", NOW - timedelta(days=8))
    with store.transaction() as tx:
        tx.execute("UPDATE service_notices SET state='surfaced' WHERE notice_id=?", (seen,))
        tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) VALUES('s-old','a ping',"
                   "'pending',?)", (stamp(NOW - timedelta(days=30)),))
    assert rnotices.expire_outcomes(store, 0, now=NOW) == []
    assert rnotices.expire_outcomes(store, 7 * DAY, now=NOW) == [old]
    left = {row["notice_id"]: row["text"] for row in outcomes(store)}
    assert old not in left and {young, seen} <= set(left) and "a ping" in left.values()
    (event,) = [e for e in store.query("SELECT * FROM events WHERE kind=?", (rnotices.EXPIRED_EVENT,))
                if e["data_json"] != "{}"]
    assert json.loads(event["data_json"])["service_notice_ids"] == [old]
    assert json.loads(event["data_json"])["jobs"] == ["old"]
    assert rnotices.expire_outcomes(store, 7 * DAY, now=NOW) == []          # nothing more to go


def test_a_row_that_reused_an_outcomes_id_is_not_withdrawn(retained):
    """Service notice ids are reused (no AUTOINCREMENT): a ping that takes a withdrawn or
    deleted outcome's id is matched against the event by session and creation time too."""
    store, root = retained
    outcome = write_outcome(store, root, "reused", "s1", NOW - timedelta(days=8))
    with store.transaction() as tx:
        tx.execute("DELETE FROM service_notices WHERE notice_id=?", (outcome,))
        tx.execute("INSERT INTO service_notices(notice_id,session_id,text,state,created_at) "
                   "VALUES(?,'s1','a later ping','pending',?)", (outcome, stamp(NOW - timedelta(days=9))))
    assert rnotices.expire_outcomes(store, 7 * DAY, now=NOW) == []
    assert [row["text"] for row in outcomes(store)] == ["a later ping"]


def test_a_row_surfaced_between_the_read_and_the_delete_stays(retained, monkeypatch):
    """The candidates are read before the deleting transaction; a hook that surfaces
    the outcome in between keeps it (the delete names the row whole, unread included)."""
    store, root = retained
    outcome = write_outcome(store, root, "raced", "s1", NOW - timedelta(days=8))
    real = store.transaction

    @contextmanager
    def surfaced_first(*args, **kwargs):
        with real("test.hook") as tx:
            tx.execute("UPDATE service_notices SET state='surfaced' WHERE notice_id=?", (outcome,))
        with real(*args, **kwargs) as tx:
            yield tx
    monkeypatch.setattr(store, "transaction", surfaced_first)
    assert rnotices.expire_outcomes(store, 7 * DAY, now=NOW) == []
    assert [row["state"] for row in outcomes(store)] == ["surfaced"]


def test_nothing_due_writes_nothing(retained, monkeypatch):
    """Once per retention call, every few seconds in a catch-up: no write transaction
    unless an outcome is due."""
    store, root = retained
    write_outcome(store, root, "young", "s1", NOW - timedelta(days=6))
    with store.transaction() as tx:
        tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) VALUES('s1','old ping',"
                   "'pending',?)", (stamp(NOW - timedelta(days=60)),))

    def refuse(*args, **kwargs):
        raise AssertionError("no transaction when nothing is due")
    monkeypatch.setattr(store, "transaction", refuse)
    assert rnotices.expire_outcomes(store, 7 * DAY, now=NOW) == []


def test_naming_an_outcome_is_an_index_step(retained):
    """`notice.pending` names an outcome's job through `events_notice_retired`, not a
    walk of every outcome event, with no statistics (nothing here runs ANALYZE)."""
    from subfleet.store import PIN_NOTICE_ID
    store, _ = retained
    plan = " ".join(row["detail"] for row in store.query(
        f"EXPLAIN QUERY PLAN SELECT event_id,job_id,data_json FROM events "
        f"WHERE kind='{rnotices.RETIRED_EVENT}' AND {PIN_NOTICE_ID} IN (1,2)"))
    assert "events_notice_retired (kind=? AND <expr>=?)" in plan, plan


def test_only_events_that_name_a_service_notice_are_read(retained):
    store, _ = retained
    with pytest.raises(ValueError, match="names a service notice"):
        pin_notice_jobs(store.query, [{"notice_id": 1, "session_id": "s", "created_at": "t"}], kinds=("job.ended",))
