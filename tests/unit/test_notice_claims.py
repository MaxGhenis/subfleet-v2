"""C-15.9: offline hook receipts fence delivery through daemon restarts."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import caller_wakes, hooks, notice_claims, notify_push, protocol
from subfleet.conversations.wakes import COOLDOWN_S, MAX_STREAK
from subfleet.sessions import nudge, transcripts
from subfleet.sessions.transcripts import TurnState
from subfleet.client import DaemonUnavailable
from subfleet.store import Store


SESSION = "receipt-session"
TEXT = "job-done succeeded; rc=0; -o /fake/result.md"
#: When the `wake_engine` clock starts (1,800,000,000 s): a notice from then is
#: inside the 24 h window, where C-15.9 may wake for it.
CREATED = "2027-01-15T08:00:00Z"


class OfflineClient:
    def call(self, *_args, **_kwargs):
        raise DaemonUnavailable("fixture daemon is stopped")


@pytest.fixture
def core(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.add_job(job_id="job-done", request_id="job-done", payload_digest="digest",
                  kind="dispatch", workdir="/fake/work", prompt_path="/fake/prompt",
                  sandbox="read-only", state="succeeded", caller_session=SESSION)
    daemon = SimpleNamespace(root=tmp_path, store=store, _pin_notice_jobs=lambda _rows: {})
    try:
        yield daemon
    finally:
        daemon.store.close()


def add_notice(core, *, service=False, text=TEXT, created_at=CREATED):
    with core.store.transaction() as tx:
        table = "service_notices" if service else "notices"
        if service:
            row_id = tx.execute(
                "INSERT INTO service_notices(session_id,text,state,created_at) VALUES (?,?,'pending',?)",
                (SESSION, text, created_at)).lastrowid
        else:
            row_id = tx.execute(
                "INSERT INTO notices(job_id,session_id,text,state,created_at) "
                "VALUES ('job-done',?,?,'pending',?)", (SESSION, text, created_at)).lastrowid
    return -row_id if service else row_id


def offline_hook(core, *, event="UserPromptSubmit", source=None, prompt=None, session=SESSION):
    stream = io.StringIO()
    assert hooks.session_event(event, {"session_id": session, "source": source, "prompt": prompt},
                               core.root, client=OfflineClient(), stdout=stream, env={}) == 0
    return stream.getvalue()


def online_surface(core):
    return caller_wakes.surface(core, protocol.NoticePendingArgs(
        session_id=SESSION, surface="UserPromptSubmit"))["notices"]


@pytest.mark.parametrize("service", [False, True], ids=["job", "service"])
def test_c15_9_offline_hooks_print_once_and_reconcile_after_daemon_restart(core, service):
    """Receipt files survive while SQLite is untouched, then replay on restart."""
    wire_id = add_notice(core, service=service)
    table, row_id = protocol.notice_row(wire_id)
    first = offline_hook(core)
    assert TEXT in first
    assert offline_hook(core) == ""
    assert core.store.one(f"SELECT state FROM {table} WHERE notice_id=?", (row_id,)) == {"state": "pending"}
    receipt = notice_claims.receipt_path(core.root, SESSION)
    saved = json.loads(receipt.read_text())
    assert len(saved) == 1 and saved[0]["notice_id"] == wire_id
    assert set(saved[0]) == {"notice_id", "fingerprint"}
    assert TEXT not in receipt.read_text()

    core.store.close()
    core.store = Store(core.root / "state.sqlite3")
    assert online_surface(core) == []
    assert core.store.one(f"SELECT state,transport FROM {table} WHERE notice_id=?", (row_id,)) == {
        "state": "surfaced", "transport": "hook:offline"}
    assert not receipt.exists()
    assert online_surface(core) == []
    assert offline_hook(core) == ""


def test_c15_9_offline_receipt_lock_refuses_a_racing_daemon_callback(core, monkeypatch):
    """A daemon callback during offline read sees no claimable rows."""
    wire_id = add_notice(core)
    original = hooks._offline_pending
    raced = []

    def pending(root, session):
        raced.append(online_surface(core))
        return original(root, session)

    monkeypatch.setattr(hooks, "_offline_pending", pending)
    assert TEXT in offline_hook(core)
    assert raced == [[]]
    assert core.store.one("SELECT state FROM notices WHERE notice_id=?", (wire_id,))["state"] == "pending"
    assert online_surface(core) == []
    assert core.store.one("SELECT state FROM notices WHERE notice_id=?", (wire_id,))["state"] == "surfaced"


@pytest.mark.parametrize("service", [False, True], ids=["job", "service"])
def test_c15_9_receipts_do_not_consume_a_reused_notice_id(core, service):
    """Only the id plus its creation time and text fingerprint names a notice."""
    old_id = add_notice(core, service=service)
    assert TEXT in offline_hook(core)
    table, row_id = protocol.notice_row(old_id)
    with core.store.transaction() as tx:
        tx.execute(f"DELETE FROM {table} WHERE notice_id=?", (row_id,))
    new_text = "new completion with a reused id"
    new_id = add_notice(core, service=service, text=new_text, created_at="2026-11-11T12:01:00Z")
    assert new_id == old_id
    rows = online_surface(core)
    assert len(rows) == 1 and rows[0]["text"] == new_text and rows[0]["notice_id"] == new_id
    assert not notice_claims.receipt_path(core.root, SESSION).exists()


@pytest.mark.parametrize("contents", [
    "{not-json", "{}", "[null]", "[{}]",
    '[{"notice_id":[],"fingerprint":"bad"}]',
    '[{"notice_id":1,"fingerprint":{}}]',
    '[{"notice_id":true,"fingerprint":"bad"}]',
    '[{"notice_id":"1","fingerprint":"bad"}]',
])
def test_c15_9_corrupt_receipts_fail_closed_without_blocking_the_hook(core, contents):
    """A malformed receipt cannot authorize printing or make a prompt fail."""
    add_notice(core)
    path = notice_claims.receipt_path(core.root, SESSION)
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    assert offline_hook(core) == ""
    assert path.read_text() == contents
    assert core.store.one("SELECT state FROM notices")["state"] == "pending"


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_c15_9_receipt_write_failure_prints_nothing_and_leaves_no_partial_receipt(core, monkeypatch, failure):
    """The hook prints only after its delivery receipt is durably published."""
    add_notice(core)

    def refused(*_args, **_kwargs):
        raise OSError("fixture receipt publication failed")

    monkeypatch.setattr(notice_claims.os, failure, refused)
    assert offline_hook(core) == ""
    path = notice_claims.receipt_path(core.root, SESSION)
    assert not path.exists()
    assert not path.with_suffix(".tmp").exists()
    assert core.store.one("SELECT state FROM notices")["state"] == "pending"


def wake_engine(core, monkeypatch, clock):
    """Real durable decisions with fake transcript/registry/inbox observations."""
    core.policy = {"sessions": {"notice_wake_quiet_s": 300}}
    core.sessions = lambda _args: {"sessions": {}}
    core._conversation_binding = lambda _session: None
    core._lane_session_ids = lambda: []
    monkeypatch.setenv(nudge.TICKLE_ENV, "on")
    monkeypatch.setattr(hooks, "wake_worker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(notice_claims.time, "time", lambda: clock[0])
    monkeypatch.setattr(transcripts, "transcript_path", lambda _session: None)
    stale = datetime.fromtimestamp(clock[0] - 3600, timezone.utc).isoformat()
    monkeypatch.setattr(transcripts, "turn_state", lambda *_args, **_kwargs:
                        TurnState(state="completed", timestamp=stale, last_uuid="stale-turn"))
    monkeypatch.setattr(notify_push, "find_session", lambda _session:
                        {"alive": True, "socket_present": True})
    sent = []

    def push(session, body, **kwargs):
        sent.append((session, body))
        return {"delivered": True}

    monkeypatch.setattr(notify_push, "push_to_session", push)
    return caller_wakes.CallerWakeEngine(core, now=lambda: clock[0]), sent


@pytest.mark.parametrize("had_notice", [False, True], ids=["empty", "already-receipted"])
def test_c15_9_offline_prompt_activity_holds_a_new_notice_until_the_gate(core, monkeypatch, had_notice):
    """An offline prompt counts even when it prints no newly pending notice."""
    clock = [1800000000.0]
    engine, sent = wake_engine(core, monkeypatch, clock)
    if had_notice:
        add_notice(core)
        assert TEXT in offline_hook(core)
        clock[0] += 100
    assert offline_hook(core) == ""
    active_at = clock[0]
    fresh = add_notice(core, text="newly finished job notice")
    engine.pass_once()
    assert sent == []
    assert core.store.one("SELECT active_at FROM caller_wake_sessions WHERE session_id=?",
                          (SESSION,))["active_at"] == active_at
    clock[0] = active_at + 299.999
    engine.pass_once()
    assert sent == []
    clock[0] = active_at + 300
    engine.pass_once()
    assert len(sent) == 1
    assert core.store.one("SELECT wake_state FROM notices WHERE notice_id=?", (fresh,))["wake_state"] == "delivered"


@pytest.mark.parametrize("source,woken", [("clear", False), ("compact", True)])
def test_c15_9_offline_session_start_source_fences_a_later_notice(core, monkeypatch, source, woken):
    """A `clear` holds the notice; an autocompaction does not (review r1, finding 7)."""
    clock = [1800000000.0]
    engine, sent = wake_engine(core, monkeypatch, clock)
    assert offline_hook(core, event="SessionStart", source=source) == ""
    add_notice(core)
    clock[0] += 301
    engine.pass_once()
    assert len(sent) == int(woken)
    assert core.store.one("SELECT source FROM caller_wake_sessions WHERE session_id=?", (SESSION,)) == {
        "source": source}


def test_c15_9_an_offline_wake_prompt_is_activity_but_not_personal_input(core, monkeypatch):
    """Review r1, finding 2, offline: the turn a wake starts moves the quiet clock but
    keeps the unattended bounds; a person's prompt resets them."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    wake = notify_push.envelope("job-done: succeeded; rc=0; deliverable=-; out=-")
    assert offline_hook(core, prompt=wake) == ""
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": clock[0], "source": None, "human_at": None}
    clock[0] += 1
    assert offline_hook(core, prompt="And the next one?") == ""
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": clock[0], "source": None, "human_at": clock[0]}


def test_c15_9_offline_human_prompt_resets_bounds_even_before_a_later_restart(core, monkeypatch):
    clock = [1800000000.0]
    engine, sent = wake_engine(core, monkeypatch, clock)
    with core.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source,wake_streak,last_wake_at) "
                   "VALUES (?,?,'compact',?,?)", (SESSION, clock[0] - 600, MAX_STREAK, clock[0] - 1))
    assert offline_hook(core) == ""
    human_at = clock[0]
    clock[0] += 1
    assert offline_hook(core, event="SessionStart", source="resume") == ""
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": clock[0], "source": "resume", "human_at": human_at}
    add_notice(core)
    clock[0] += 300
    assert clock[0] - human_at < COOLDOWN_S
    engine.pass_once()
    assert len(sent) == 1
    assert core.store.one("SELECT wake_streak,source FROM caller_wake_sessions WHERE session_id=?", (SESSION,)) == {
        "wake_streak": 1, "source": "resume"}


@pytest.mark.parametrize("event,source,personal", [
    ("UserPromptSubmit", None, True),
    ("SessionStart", "clear", False),
], ids=["prompt", "session-start"])
def test_c15_9_offline_activity_survives_a_busy_delivery_lock(core, monkeypatch, event, source, personal):
    """A missed delivery claim still preserves personal activity and start source."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    wire_id = add_notice(core)
    last_wake = clock[0] - 1
    with core.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source,wake_streak,last_wake_at) "
                   "VALUES (?,?,'compact',?,?)", (SESSION, clock[0] - 600, MAX_STREAK, last_wake))
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        assert offline_hook(core, event=event, source=source) == ""
        assert notice_claims.read_activity(core.root, SESSION) == {
            "at": clock[0], "source": source, "human_at": clock[0] if personal else None}
        assert core.store.one("SELECT state FROM notices WHERE notice_id=?", (wire_id,)) == {"state": "pending"}
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.reconcile(core, SESSION)
    assert core.store.one("SELECT active_at,source,wake_streak,last_wake_at FROM caller_wake_sessions "
                          "WHERE session_id=?", (SESSION,)) == {
        "active_at": clock[0], "source": source,
        "wake_streak": 0 if personal else MAX_STREAK,
        "last_wake_at": None if personal else last_wake}


def test_c15_9_overlapping_activity_publications_keep_human_time_and_latest_source(core, monkeypatch):
    """A later start may publish first without replacing an earlier personal prompt."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    human_at = clock[0]
    fsync = notice_claims.os.fsync
    interleaved = []

    def sync(fd):
        if not interleaved:
            interleaved.append(True)
            clock[0] += 1
            notice_claims.record_offline_activity(core.root, SESSION, event="SessionStart", source="resume")
        return fsync(fd)

    monkeypatch.setattr(notice_claims.os, "fsync", sync)
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.record_offline_activity(core.root, SESSION, event="UserPromptSubmit", source=None)
    assert interleaved == [True]
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": clock[0], "source": "resume", "human_at": human_at}


def test_c15_9_reconcile_preserves_activity_published_after_its_snapshot(core, monkeypatch):
    """Replaying one snapshot cannot consume the next prompt or its human reset."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    with core.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source,wake_streak,last_wake_at) "
                   "VALUES (?,?,'compact',?,?)", (SESSION, clock[0] - 600, MAX_STREAK, clock[0] - 1))
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.record_offline_activity(core.root, SESSION, event="SessionStart", source="compact")
    transaction = core.store.transaction
    interleaved = []
    human_at = clock[0] + 10

    def racing_transaction(*args, **kwargs):
        if args and args[0] == "notice.offline_surfaced" and not interleaved:
            interleaved.append(True)
            clock[0] = human_at
            notice_claims.record_offline_activity(core.root, SESSION, event="UserPromptSubmit", source=None)
            clock[0] += 1
            notice_claims.record_offline_activity(core.root, SESSION, event="SessionStart", source="resume")
        return transaction(*args, **kwargs)

    monkeypatch.setattr(core.store, "transaction", racing_transaction)
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.reconcile(core, SESSION)
    assert interleaved == [True]
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": clock[0], "source": "resume", "human_at": human_at}
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.reconcile(core, SESSION)
    assert core.store.one("SELECT active_at,source,wake_streak,last_wake_at FROM caller_wake_sessions "
                          "WHERE session_id=?", (SESSION,)) == {
        "active_at": clock[0], "source": "resume", "wake_streak": 0, "last_wake_at": None}
    assert notice_claims.read_activity(core.root, SESSION) is None


def test_c15_9_stale_offline_activity_preserves_newer_online_activity_and_bounds(core, monkeypatch):
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    assert offline_hook(core) == ""
    with core.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source,wake_streak,last_wake_at) "
                   "VALUES (?,?,'compact',?,?)", (SESSION, clock[0] + 10, MAX_STREAK, clock[0] + 1))
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.reconcile(core, SESSION)
    assert core.store.one("SELECT active_at,source,wake_streak,last_wake_at FROM caller_wake_sessions "
                          "WHERE session_id=?", (SESSION,)) == {
        "active_at": clock[0] + 10, "source": "compact", "wake_streak": MAX_STREAK,
        "last_wake_at": clock[0] + 1}


def test_c15_9_a_corrupt_receipt_does_not_starve_another_quiet_caller(core, monkeypatch):
    clock = [1800000000.0]
    engine, sent = wake_engine(core, monkeypatch, clock)
    add_notice(core)
    path = notice_claims.receipt_path(core.root, SESSION)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    with core.store.transaction() as tx:
        tx.execute("INSERT INTO notices(job_id,session_id,text,state,created_at) "
                   "VALUES ('job-done','other-caller','other notice','pending',?)", (CREATED,))
    engine.pass_once()
    assert [session for session, _body in sent] == ["other-caller"]
    assert core.store.one("SELECT state FROM notices WHERE session_id=?", (SESSION,)) == {"state": "pending"}


@pytest.mark.parametrize("invalid_at", [10 ** 1000, float("nan")], ids=["huge-integer", "nonfinite"])
def test_c15_9_invalid_activity_timestamps_fail_closed_without_blocking_the_hook(core, invalid_at):
    add_notice(core)
    path = notice_claims.activity_path(core.root, SESSION)
    path.parent.mkdir(parents=True)
    contents = json.dumps({"at": invalid_at, "source": "resume", "human_at": None})
    path.write_text(contents)
    assert offline_hook(core) == ""
    assert path.read_text() == contents
    assert core.store.one("SELECT state FROM notices")["state"] == "pending"


# --- review r4 of #307: a bounded activity journal and bounded activity-lock waits ---

OTHER = "other-caller"
#: How long a test lets a hook or a pass run while another descriptor holds the
#: activity lock. Before review r4 both waited for as long as the holder held it;
#: now each wait ends after `notice_claims.ACTIVITY_LOCK_WAIT_S`. The margin is
#: for the rest of the hook's work under a loaded test machine.
STALL_S = 15.0


def activity_records(root, session):
    """The activity record files a session keeps: the aggregate, then published records."""
    aggregate = notice_claims.activity_path(root, session)
    folder = aggregate.with_suffix(".d")
    published = sorted(folder.glob("*.json")) if folder.exists() else []
    return ([aggregate] if aggregate.exists() else []) + published


def session_entries(root, session):
    """Every file or folder the offline hooks keep for one session."""
    receipts = notice_claims.receipt_path(root, session)
    key = receipts.name.split(".")[0]
    return sorted(path for path in receipts.parent.rglob("*") if path.name.startswith(key)
                  or path.parent.name.startswith(key))


def hold_activity_lock(root, session):
    """Hold the session's activity lock from another file descriptor, as a stopped
    or hung hook process would."""
    path = notice_claims.activity_path(root, session).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_c15_9_offline_hooks_keep_a_constant_number_of_activity_files(core, monkeypatch):
    """Review r4 of #307, P2: N offline hooks with the daemon unreachable left N
    activity files, and each hook read them all. A hook that holds the delivery lock
    now folds the records into the aggregate file, so one record remains, holding the
    same merged activity, and the daemon replays that value."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    add_notice(core)
    wake = notify_push.envelope("job-done: succeeded; rc=0; deliverable=-; out=-")
    runs = [("UserPromptSubmit", None, "a person's prompt"), ("SessionStart", "compact", None),
            ("UserPromptSubmit", None, wake), ("SessionStart", "resume", None)] * 3
    human_at, counts = None, []
    for index, (event, source, prompt) in enumerate(runs):
        clock[0] += 7
        output = offline_hook(core, event=event, source=source, prompt=prompt)
        assert (TEXT in output) == (index == 0)          # printed once; its receipt holds it after
        if prompt is not None and prompt != wake:
            human_at = clock[0]
        counts.append(len(activity_records(core.root, SESSION)))
    assert counts == [1] * len(runs), "activity files after each offline hook"
    assert activity_records(core.root, SESSION) == [notice_claims.activity_path(core.root, SESSION)]
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": clock[0], "source": "resume", "human_at": human_at}
    # The receipt, the two lock files, the aggregate and the empty records folder.
    assert len(session_entries(core.root, SESSION)) <= 5, session_entries(core.root, SESSION)
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.reconcile(core, SESSION)
    assert core.store.one("SELECT active_at,source,wake_streak FROM caller_wake_sessions "
                          "WHERE session_id=?", (SESSION,)) == {
        "active_at": clock[0], "source": "resume", "wake_streak": 0}
    assert activity_records(core.root, SESSION) == []


def test_c15_9_a_corrupt_activity_record_still_fences_only_its_own_caller(core, monkeypatch):
    """Review r4 of #307, P2: folding never drops a corrupt record. Its caller's hooks
    print nothing and publish nothing, and the pass leaves its notice; another caller's
    hooks still fold to one record, and that caller is woken."""
    clock = [1800000000.0]
    engine, sent = wake_engine(core, monkeypatch, clock)
    add_notice(core)
    corrupt = notice_claims.activity_path(core.root, SESSION).with_suffix(".d") / (
        f"{1:020d}-{'0' * 32}.json")
    corrupt.parent.mkdir(parents=True)
    contents = json.dumps({"at": "not a time", "source": None, "human_at": None})
    corrupt.write_text(contents)
    counts = []
    for _ in range(6):
        clock[0] += 7
        assert offline_hook(core) == ""
        assert offline_hook(core, session=OTHER) == ""
        counts.append(len(activity_records(core.root, OTHER)))
    assert counts == [1] * 6, "the other caller's activity files after each offline hook"
    assert activity_records(core.root, SESSION) == [corrupt]
    assert corrupt.read_text() == contents
    with core.store.transaction() as tx:
        tx.execute("INSERT INTO notices(job_id,session_id,text,state,created_at) "
                   "VALUES ('job-done',?,'other notice','pending',?)", (OTHER, CREATED))
    clock[0] += 301
    engine.pass_once()
    assert [session for session, _body in sent] == [OTHER]
    assert core.store.one("SELECT state,wake_state FROM notices WHERE session_id=?", (SESSION,)) == {
        "state": "pending", "wake_state": None}
    assert activity_records(core.root, SESSION) == [corrupt]


@pytest.mark.parametrize("when", ["before-replace", "after-replace"])
def test_c15_9_a_record_published_during_a_fold_survives_it(core, monkeypatch, when):
    """Review r4 of #307, P2: a fold unlinks exactly the records in its snapshot. A record
    another hook publishes after that snapshot (without the activity lock, once its wait
    ran out) and before the unlinks, here just before or just after the aggregate's
    replace, survives, and the next fold takes it."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    assert offline_hook(core) == ""
    human_at = clock[0]
    aggregate = notice_claims.activity_path(core.root, SESSION)
    late = aggregate.with_suffix(".d") / f"{1:020d}-{'f' * 32}.json"
    late_at = clock[0] + 5
    replace = os.replace
    published = []

    def publish_late():
        published.append(True)
        late.write_text(json.dumps({"at": late_at, "source": "resume", "human_at": None}))

    def racing_replace(source, destination):
        if Path(destination) == aggregate and not published and when == "before-replace":
            publish_late()
        replace(source, destination)
        if Path(destination) == aggregate and not published and when == "after-replace":
            publish_late()

    monkeypatch.setattr(notice_claims.os, "replace", racing_replace)
    clock[0] += 1
    assert offline_hook(core, event="SessionStart", source="compact") == ""
    monkeypatch.setattr(notice_claims.os, "replace", replace)
    assert published == [True]
    assert activity_records(core.root, SESSION) == [aggregate, late]
    assert json.loads(aggregate.read_text()) == {"at": clock[0], "source": "compact", "human_at": human_at}
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": late_at, "source": "resume", "human_at": human_at}
    clock[0] = late_at + 1
    assert offline_hook(core, event="SessionStart", source="startup") == ""
    assert activity_records(core.root, SESSION) == [aggregate]
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": late_at + 1, "source": "startup", "human_at": human_at}


def test_c15_9_a_crash_between_a_folds_replace_and_its_unlinks_loses_nothing(core, monkeypatch):
    """Review r4 of #307, P2: the aggregate is replaced before the folded records are
    unlinked. A crash between the two leaves both; reading them merges duplicates to the
    same value, and the daemon replays it once."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    with core.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source,wake_streak,last_wake_at) "
                   "VALUES (?,?,'compact',?,?)", (SESSION, clock[0] - 600, MAX_STREAK, clock[0] - 1))
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held                                      # the hooks below fold nothing
        assert offline_hook(core) == ""
        human_at = clock[0]
        clock[0] += 1
        assert offline_hook(core, event="SessionStart", source="compact") == ""
    assert len(activity_records(core.root, SESSION)) == 2
    aggregate = notice_claims.activity_path(core.root, SESSION)
    replace = os.replace

    class Crash(BaseException):
        """The hook process dies here."""

    def crash_after_replace(source, destination):
        replace(source, destination)
        if Path(destination) == aggregate:
            raise Crash

    monkeypatch.setattr(notice_claims.os, "replace", crash_after_replace)
    clock[0] += 1
    with pytest.raises(Crash):
        offline_hook(core, event="SessionStart", source="resume")
    monkeypatch.setattr(notice_claims.os, "replace", replace)
    merged = {"at": clock[0], "source": "resume", "human_at": human_at}
    assert json.loads(aggregate.read_text()) == merged
    assert len(activity_records(core.root, SESSION)) == 4      # the aggregate and its three records
    assert notice_claims.read_activity(core.root, SESSION) == merged
    with notice_claims.delivery_lock(core.root, SESSION) as held:
        assert held
        notice_claims.reconcile(core, SESSION)
    assert core.store.one("SELECT active_at,source,wake_streak,last_wake_at FROM caller_wake_sessions "
                          "WHERE session_id=?", (SESSION,)) == {
        "active_at": clock[0], "source": "resume", "wake_streak": 0, "last_wake_at": None}
    assert activity_records(core.root, SESSION) == []


def test_c15_9_the_activity_lock_waits_a_bounded_time(core):
    """Review r4 of #307, P2: `activity_lock` waited with no timeout. It now gives up
    after `wait_s` and says so, as `delivery_lock` does."""
    fd = hold_activity_lock(core.root, SESSION)
    try:
        start = time.monotonic()
        with notice_claims.activity_lock(core.root, SESSION, wait_s=0.2) as held:
            elapsed = time.monotonic() - start
            assert held is False
        assert 0.2 <= elapsed < 0.2 + STALL_S
    finally:
        os.close(fd)
    with notice_claims.activity_lock(core.root, SESSION, wait_s=0.2) as held:
        assert held is True


def test_c15_9_an_offline_hook_returns_while_another_process_holds_the_activity_lock(core, monkeypatch):
    """Review r4 of #307, P2: an offline hook waited as long as another holder kept the
    activity lock. It now waits a bounded time, publishes its activity all the same
    (round 3: activity is never lost) and still prints its notice."""
    clock = [1800000000.0]
    wake_engine(core, monkeypatch, clock)
    add_notice(core)
    fd = hold_activity_lock(core.root, SESSION)
    result = {}
    hook = threading.Thread(target=lambda: result.update(output=offline_hook(core)), daemon=True)
    try:
        hook.start()
        hook.join(STALL_S)
        assert not hook.is_alive(), "the hook waited out the activity lock's holder"
    finally:
        os.close(fd)
        hook.join(60)
    assert TEXT in result["output"]
    assert notice_claims.read_activity(core.root, SESSION) == {
        "at": clock[0], "source": None, "human_at": clock[0]}
