"""C-15.9: offline hook receipts fence delivery through daemon restarts."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
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


def add_notice(core, *, service=False, text=TEXT, created_at="2026-11-11T12:00:00Z"):
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


def offline_hook(core, *, event="UserPromptSubmit", source=None):
    stream = io.StringIO()
    assert hooks.session_event(event, {"session_id": SESSION, "source": source}, core.root,
                               client=OfflineClient(), stdout=stream, env={}) == 0
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


@pytest.mark.parametrize("source", ["compact", "clear"])
def test_c15_9_offline_session_start_source_fences_a_later_notice(core, monkeypatch, source):
    clock = [1800000000.0]
    engine, sent = wake_engine(core, monkeypatch, clock)
    assert offline_hook(core, event="SessionStart", source=source) == ""
    add_notice(core)
    clock[0] += 301
    engine.pass_once()
    assert sent == []
    assert core.store.one("SELECT source FROM caller_wake_sessions WHERE session_id=?", (SESSION,)) == {
        "source": source}


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
                   "VALUES ('job-done','other-caller','other notice','pending','2026-11-11T12:00:00Z')")
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
