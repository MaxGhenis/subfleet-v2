"""C-15.9: durable, once-only quiet-caller completion delivery, using fake inboxes.

The real daemon writes notices and the real hook consumes them. Only the
transcript, registry and inbox are fake; no provider or session is launched.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import io
import itertools
import json
from pathlib import Path
import re
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import hooks, notify_push, render
from subfleet.conversations.wakes import COOLDOWN_S, MAX_STREAK
from subfleet.sessions import nudge, transcripts
from subfleet.sessions.transcripts import TurnState
from subfleet.store import Store
from tests.fake.test_notice_delivery import job_notice, pending_ids, surface
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - fixture


EPOCH = datetime(2026, 11, 12, 12, tzinfo=timezone.utc).timestamp()
EXAMPLES = itertools.count()


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class Callers:
    """A controllable clock, app registry and session transcripts."""

    def __init__(self, daemon, harness, monkeypatch):
        self.daemon, self.harness = daemon, harness
        self.now = EPOCH
        self.activity = {}
        self.states = {}
        self.entries = {}
        self.failures = {}
        self.sent = []
        self.attempts = []
        self.notices = {}
        monkeypatch.setenv(nudge.TICKLE_ENV, "on")
        monkeypatch.setattr(hooks, "wake_worker", lambda *args, **kwargs: None)
        monkeypatch.setattr(transcripts, "transcript_path", self.transcript_path)
        monkeypatch.setattr(transcripts, "turn_state", self.turn_state)
        monkeypatch.setattr(notify_push, "find_session", lambda sid: self.entries.get(sid))
        monkeypatch.setattr(notify_push, "push_to_session", self.push)
        self.restart()

    def restart(self, *, reopen=False):
        from subfleet.caller_wakes import CallerWakeEngine
        if reopen:
            path = self.daemon.store.path
            self.daemon.store.close()
            self.daemon.store = Store(path)
        self.engine = CallerWakeEngine(self.daemon, now=lambda: self.now)
        self.engine.recover()

    def register(self, sid, *, age=600, state="completed"):
        self.activity[sid] = self.now - age
        self.states[sid] = state
        path = self.harness.root / f"{sid}.jsonl"
        path.touch()
        self.entries[sid] = {
            "session_id": sid, "alive": True, "socket_present": True,
            "socket": str(self.harness.root / f"{sid}.sock"), "pid": 12345,
            "name": sid, "cwd": str(self.harness.workdir),
        }

    def transcript_path(self, sid, *args, **kwargs):
        return self.harness.root / f"{sid}.jsonl" if sid in self.activity else None

    def turn_state(self, path, *args, **kwargs):
        sid = Path(path).stem
        return TurnState(state=self.states[sid], timestamp=stamp(self.activity[sid]),
                         last_uuid=f"last-{sid}", path=str(path),
                         age_s=int(self.now - self.activity[sid]))

    def push(self, sid, body, **kwargs):
        self.attempts.append((sid, body))
        reason = self.failures.get(sid)
        if reason:
            return {"delivered": False, "reason": reason, "session_id": sid}
        self.sent.append((sid, body))
        return {"delivered": True, "session_id": sid}

    def finish(self, sid, *, created=None):
        job, notice = job_notice(self.daemon, self.harness, sid)
        self.daemon.store.update_notice(notice, created_at=stamp(self.now if created is None else created))
        self.notices[notice] = job
        return notice

    def row(self, notice):
        return self.daemon.store.one("SELECT * FROM notices WHERE notice_id=?", (notice,))

    def pass_once(self):
        return self.engine.pass_once()

    def counts(self):
        return Counter(notice for _, body in self.sent
                       for notice in self.notices if self.mentioned(notice, body))

    def mentioned(self, notice, body):
        # Sequential job ids can have one another as a prefix (run-1/run-10).
        return bool(re.search(re.escape(self.notices[notice]) + r"(?![\w-])", body))


@pytest.fixture
def callers(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    return Callers(daemon, harness, monkeypatch)


HISTORY = st.lists(st.tuples(
    st.sampled_from(("finish", "hook", "pass", "restart", "failure", "advance", "active")),
    st.integers(0, 1), st.integers(0, 650)), min_size=1, max_size=24)
PROPERTY = settings(max_examples=35, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])


def history(callers, operations):
    """Apply generated finishes, hook reads, temporary inbox failures and restarts."""
    prefix = f"history-{next(EXAMPLES)}"
    sessions = [f"{prefix}-{index}" for index in range(2)]
    for sid in sessions:
        callers.register(sid)
    shown = set()
    owned = set()
    for kind, index, amount in operations:
        sid = sessions[index]
        if kind == "finish":
            callers.now += amount / 10
            owned.add(callers.finish(sid))
        elif kind == "hook":
            context = surface(callers.daemon, callers.harness.root, sid)
            current = {notice for notice in owned if callers.mentioned(notice, context)}
            assert not current & shown, "a hook must never show a notice twice"
            assert not current & set(callers.counts()), "a woken notice is no longer new"
            shown |= current
        elif kind == "restart":
            callers.restart(reopen=True)
        elif kind == "failure":
            if amount % 2:
                callers.failures[sid] = "no-inbox-socket"
            else:
                callers.failures.pop(sid, None)
        elif kind == "advance":
            callers.now += amount
        elif kind == "active":
            callers.activity[sid] = callers.now
        else:
            callers.pass_once()
        counts = callers.counts()
        assert all(counts[notice] <= 1 for notice in owned), "Once: no replay across restart"
        assert not shown & set(counts), "Never both: hooks and wakes consume atomically"
    return owned, shown


@PROPERTY
@given(operations=HISTORY)
def test_c15_9_once_over_generated_finish_hook_failure_and_restart_histories(callers, operations):
    """Invariant 1 Once: successful delivery is durable across arbitrary passes and restarts."""
    owned, _ = history(callers, operations)
    callers.now += COOLDOWN_S + 600
    callers.failures.clear()
    callers.pass_once()
    callers.restart(reopen=True)
    callers.now += COOLDOWN_S
    callers.pass_once()
    assert all(callers.counts()[notice] <= 1 for notice in owned)


@PROPERTY
@given(operations=HISTORY)
def test_c15_9_never_both_over_generated_finish_hook_failure_and_restart_histories(callers, operations):
    """Invariant 2 Never both: a hook's read and the wake claim exclude one another."""
    owned, shown = history(callers, operations)
    for notice in owned:
        sid = callers.row(notice)["session_id"]
        context = surface(callers.daemon, callers.harness.root, sid)
        newly_shown = {n for n in owned if callers.mentioned(n, context)}
        assert not newly_shown & shown
        assert not newly_shown & set(callers.counts())
        shown |= newly_shown
    callers.now += COOLDOWN_S + 600
    callers.failures.clear()
    callers.restart(reopen=True)
    callers.pass_once()
    assert not shown & set(callers.counts())


@PROPERTY
@given(finish_age=st.integers(0, 900), activity_age=st.integers(0, 600),
       prehistory=st.lists(st.tuples(st.booleans(), st.booleans(), st.booleans(),
                                    st.integers(0, 120)), max_size=8))
def test_c15_9_eventually_over_generated_finish_hook_failure_and_restart_histories(
        callers, finish_age, activity_age, prehistory):
    """Invariant 4 Eventually: after the gate and a transient retry, the next pass delivers."""
    sid = f"eventually-{next(EXAMPLES)}"
    callers.register(sid, age=activity_age)
    notice = callers.finish(sid, created=callers.now - finish_age)
    shown = False
    for hook, failure, restart, advance in prehistory:
        callers.now += advance
        if hook:
            shown |= callers.mentioned(notice, surface(callers.daemon, callers.harness.root, sid))
        if failure:
            callers.failures[sid] = "no-peer-token"
        else:
            callers.failures.pop(sid, None)
        if restart:
            callers.restart(reopen=True)
        callers.pass_once()
    callers.failures.pop(sid, None)
    row = callers.row(notice)
    callers.now = max(callers.now, callers.activity[sid] + 300,
                      row["wake_next_at"] or 0)
    callers.restart(reopen=True)
    before = callers.counts()[notice]
    callers.pass_once()
    if shown:
        assert callers.counts()[notice] == 0
    else:
        assert callers.counts()[notice] == 1
        if not before:
            assert callers.row(notice)["wake_state"] == "delivered"


def test_c15_9_delivered_notice_survives_restart_without_replay(callers):
    callers.register("once")
    notice = callers.finish("once")
    callers.pass_once()
    assert callers.counts()[notice] == 1
    first = callers.row(notice)
    callers.restart(reopen=True)
    callers.now += COOLDOWN_S + 1
    callers.pass_once()
    assert callers.counts()[notice] == 1
    assert callers.row(notice) == first


def test_c15_9_daemon_pass_rescans_pending_notice_after_restart(callers):
    callers.register("daemon-pass")
    notice = callers.finish("daemon-pass")
    callers.restart(reopen=True)
    callers.daemon._caller_wakes = callers.engine
    callers.daemon._wake_callers()
    assert callers.counts()[notice] == 1


def test_c15_9_hook_shown_notice_never_wakes(callers):
    callers.register("hook-first")
    notice = callers.finish("hook-first")
    assert callers.notices[notice] in surface(callers.daemon, callers.harness.root, "hook-first")
    callers.pass_once()
    assert callers.counts()[notice] == 0


def test_c15_9_hook_during_inbox_send_does_not_show_claimed_notice(callers, monkeypatch):
    callers.register("wake-first")
    notice = callers.finish("wake-first")
    push = callers.push

    def interleaved(sid, body, **kwargs):
        assert surface(callers.daemon, callers.harness.root, sid) == ""
        return push(sid, body, **kwargs)

    monkeypatch.setattr(notify_push, "push_to_session", interleaved)
    callers.pass_once()
    assert callers.counts()[notice] == 1
    assert pending_ids(callers.daemon, "wake-first") == set()


def test_c15_9_offline_hook_receipt_survives_restart_and_excludes_a_wake(callers):
    """Never both also covers a read-only hook while the daemon is unavailable."""
    callers.register("offline-hook")
    notice = callers.finish("offline-hook")

    class OfflineClient:
        def call(self, *args, **kwargs):
            raise OSError("app restarting")

    def offline_hook():
        stdout = io.StringIO()
        assert hooks.session_event("UserPromptSubmit", {"session_id": "offline-hook"},
                                   callers.harness.root, client=OfflineClient(),
                                   stdout=stdout, env={}) == 0
        value = stdout.getvalue()
        return json.loads(value)["hookSpecificOutput"]["additionalContext"] if value else ""

    assert callers.mentioned(notice, offline_hook())
    assert offline_hook() == ""
    # An offline hook cannot change SQLite; its receipt closes the race later.
    assert callers.row(notice)["state"] == "pending"
    callers.restart(reopen=True)
    callers.pass_once()
    assert callers.counts()[notice] == 0
    assert callers.row(notice)["state"] == "surfaced"
    assert surface(callers.daemon, callers.harness.root, "offline-hook") == ""


@pytest.mark.parametrize("age", [0, 299, 299.999999])
def test_c15_9_quiet_only_never_wakes_a_recently_active_session(callers, age):
    """Invariant 3 Quiet only: a completed transcript still needs the full gate."""
    callers.register("active-caller", age=age)
    notice = callers.finish("active-caller")
    callers.pass_once()
    assert callers.counts()[notice] == 0
    assert callers.row(notice)["state"] == "pending"
    callers.now += 300 - age
    callers.pass_once()
    assert callers.counts()[notice] == 1


def test_c15_9_hook_activity_fences_an_old_completed_transcript(callers, monkeypatch):
    from subfleet import caller_wakes
    monkeypatch.setattr(caller_wakes, "time", SimpleNamespace(time=lambda: callers.now))
    callers.register("fresh-hook", age=3600)
    assert surface(callers.daemon, callers.harness.root, "fresh-hook") == ""
    notice = callers.finish("fresh-hook")
    callers.pass_once()
    assert callers.counts()[notice] == 0
    callers.now += 300
    callers.pass_once()
    assert callers.counts()[notice] == 1


def test_c15_9_busy_registry_holds_an_old_completed_transcript(callers):
    callers.register("busy", age=3600)
    notice = callers.finish("busy")
    callers.entries["busy"]["status"] = "busy"
    callers.pass_once()
    assert callers.counts()[notice] == 0


def test_c15_9_registry_activity_is_rechecked_before_inbox_claim(callers, monkeypatch):
    callers.register("busy-recheck", age=3600)
    notice = callers.finish("busy-recheck")
    turn_state = callers.turn_state

    def app_starts_a_turn(path, **kwargs):
        # A long-running tool may make the app busy before its transcript grows.
        callers.entries["busy-recheck"] = {**callers.entries["busy-recheck"], "status": "busy"}
        return turn_state(path, **kwargs)

    monkeypatch.setattr(transcripts, "turn_state", app_starts_a_turn)
    callers.pass_once()
    assert callers.counts()[notice] == 0


def test_c15_9_scrubbed_wake_uses_header_fields_never_notice_or_output_text(callers):
    """Invariant 5 Scrubbed: neither stored notice details nor output bytes enter the inbox."""
    callers.register("scrubbed")
    notice = callers.finish("scrubbed")
    secret = "OUTPUT CONTENT MUST NEVER BE A WAKE"
    out = callers.harness.root / "result.md"
    out.write_text(secret)
    callers.daemon.store.update_notice(notice, text=callers.row(notice)["text"] + "\n" + secret)
    callers.daemon.store.update_job(callers.notices[notice], out_path=str(out))
    callers.pass_once()
    body = callers.sent[-1][1]
    job = callers.daemon.store.get_job(callers.notices[notice])
    assert body == render.notice_header(job, callers.harness.root)
    assert secret not in body
    assert "rc=130" in body and "cancelled" in body


def test_c15_9_one_path_conversation_bound_session_is_left_to_wake_engine(callers):
    """Invariant 6 One path: a conversation's caller never gets this second wake."""
    callers.register("conversation")
    notice = callers.finish("conversation")
    conversation, _ = callers.daemon.conversations.store.create_conversation(
        provider="claude", workspace=str(callers.harness.workdir), workspace_kind="in-place",
        settings={"model": "opus", "permission": "ask"},
        origin="native", native_session_id="conversation")
    assert callers.daemon._conversation_binding("conversation") == (
        "conversation " + conversation["conversation_id"])
    callers.pass_once()
    assert callers.counts()[notice] == 0
    callers.restart(reopen=True)
    callers.pass_once()
    assert callers.counts()[notice] == 0


def test_c15_9_turn_jobs_stay_out_even_if_an_old_notice_exists(callers):
    """C-26.12: a historical turn notice cannot make a conversation turn wake itself."""
    callers.register("turn-caller")
    notice = callers.finish("turn-caller")
    callers.daemon.store.update_job(callers.notices[notice], kind="turn")
    callers.pass_once()
    assert callers.counts()[notice] == 0


def test_c15_9_prewrite_inbox_failure_retries_with_durable_backoff_until_reachable(callers):
    callers.register("restarting")
    notice = callers.finish("restarting")
    callers.failures["restarting"] = "no-inbox-socket"
    callers.pass_once()
    row = callers.row(notice)
    assert row["wake_state"] is None and row["wake_attempts"] == 1
    assert callers.now < row["wake_next_at"] <= callers.now + 300
    attempted = len(callers.attempts)
    callers.pass_once()
    assert len(callers.attempts) == attempted
    callers.restart(reopen=True)
    assert callers.row(notice) == row
    callers.failures.pop("restarting")
    callers.now = row["wake_next_at"]
    callers.pass_once()
    assert callers.counts()[notice] == 1
    assert callers.row(notice)["wake_state"] == "delivered"


def test_c15_9_missing_inbox_expires_after_twenty_four_hours(callers):
    callers.register("expired")
    notice = callers.finish("expired")
    callers.failures["expired"] = "no-peer-token"
    callers.pass_once()
    callers.restart(reopen=True)
    callers.now += 24 * 3600
    callers.pass_once()
    row = callers.row(notice)
    assert row["wake_state"] == "undeliverable"
    assert row["wake_reason"]
    callers.failures.clear()
    callers.now += 3600
    callers.pass_once()
    assert callers.counts()[notice] == 0


def test_c15_9_retry_deadline_expires_even_when_the_caller_became_busy(callers):
    callers.register("busy-expiry")
    notice = callers.finish("busy-expiry")
    callers.failures["busy-expiry"] = "no-peer-token"
    callers.pass_once()
    callers.now += 24 * 3600
    callers.entries["busy-expiry"]["status"] = "busy"
    callers.pass_once()
    assert callers.row(notice)["wake_state"] == "undeliverable"
    assert callers.counts()[notice] == 0


@pytest.mark.parametrize("entry", [None, {"alive": False, "socket_present": False}])
def test_c15_9_absent_or_closed_session_is_undeliverable_without_starting_it(callers, entry):
    callers.register("closed")
    notice = callers.finish("closed")
    if entry is None:
        callers.entries.pop("closed")
    else:
        callers.entries["closed"].update(entry)
    callers.pass_once()
    assert callers.row(notice)["wake_state"] == "undeliverable"
    assert callers.attempts == []


def test_c15_9_uncertain_send_after_crash_is_never_replayed(callers, monkeypatch):
    callers.register("crash")
    notice = callers.finish("crash")
    push = callers.push

    def crash_after_write(sid, body, **kwargs):
        push(sid, body, **kwargs)
        raise SystemExit("daemon died after inbox write")

    monkeypatch.setattr(notify_push, "push_to_session", crash_after_write)
    with pytest.raises(SystemExit, match="daemon died"):
        callers.pass_once()
    assert callers.row(notice)["wake_state"] == "claimed"
    monkeypatch.setattr(notify_push, "push_to_session", push)
    callers.restart(reopen=True)
    callers.now += COOLDOWN_S
    callers.pass_once()
    assert callers.counts()[notice] == 1
    assert callers.row(notice)["wake_state"] == "undeliverable"


@pytest.mark.parametrize("switch", ["notice", "nudge", "source", "cap"])
def test_c15_9_off_switches_source_and_zero_cap_hold_notices(callers, monkeypatch, switch):
    callers.register("held")
    notice = callers.finish("held")
    if switch == "notice":
        callers.daemon.policy["sessions"]["notice_wake_enabled"] = False
    elif switch == "nudge":
        monkeypatch.setenv(nudge.TICKLE_ENV, "off")
    elif switch == "cap":
        callers.daemon.policy["sessions"]["nudge_max_sessions"] = 0
    else:
        with callers.daemon.store.transaction() as tx:
            tx.execute("INSERT INTO caller_wake_sessions(session_id,source) VALUES ('held','compact')")
    callers.pass_once()
    assert callers.counts()[notice] == 0
    assert callers.row(notice)["state"] == "pending"


def test_c15_9_caps_bound_sessions_per_pass_and_group_their_notices(callers):
    callers.daemon.policy["sessions"]["nudge_max_sessions"] = 2
    for index in range(3):
        sid = f"capped-{index}"
        callers.register(sid)
        for _ in range(2):
            callers.finish(sid)
    callers.pass_once()
    assert len({sid for sid, _ in callers.sent}) == 2
    assert len(callers.sent) == 2
    assert sum(callers.counts().values()) == 4
    callers.pass_once()
    assert len({sid for sid, _ in callers.sent}) == 3
    assert sum(callers.counts().values()) == 6


def test_c15_9_streak_and_cooldown_survive_restart(callers):
    callers.register("bounded")
    notice = callers.finish("bounded")
    with callers.daemon.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,wake_streak,last_wake_at) VALUES (?,?,?)",
                   ("bounded", MAX_STREAK, callers.now - COOLDOWN_S + 1))
    callers.restart(reopen=True)
    callers.pass_once()
    assert callers.counts()[notice] == 0
    callers.now += 1
    callers.pass_once()
    assert callers.counts()[notice] == 1

def test_c15_9_proved_connect_failure_retries_after_store_reopen_without_raw_reason(callers, monkeypatch):
    """A stale inbox address retries across restart without persisting exception text."""
    sid = "connect-restart"
    private = "PRIVATE-CONNECT-EXCEPTION-DETAIL"
    callers.register(sid)
    notice = callers.finish(sid)

    def refused(session, body, **_kwargs):
        callers.attempts.append((session, body))
        return {"delivered": False, "session_id": session, "retryable": True,
                "reason": f"send-failed: ConnectionRefusedError: {private}"}

    monkeypatch.setattr(notify_push, "push_to_session", refused)
    callers.pass_once()
    row = callers.row(notice)
    assert row["state"] == "pending" and row["wake_state"] is None
    assert row["wake_reason"] == "inbox-unavailable"
    assert row["wake_attempts"] == 1 and row["wake_next_at"] > callers.now
    assert private not in json.dumps(row)
    assert not callers.daemon.store.query("SELECT 1 FROM events WHERE data_json LIKE ?", (f"%{private}%",))
    clock = callers.daemon.store.one("SELECT * FROM caller_wake_sessions WHERE session_id=?", (sid,))
    assert clock["wake_streak"] == 0 and clock["last_wake_at"] is None
    callers.restart(reopen=True)
    assert callers.row(notice) == row
    monkeypatch.setattr(notify_push, "push_to_session", callers.push)
    callers.now = row["wake_next_at"]
    callers.pass_once()
    assert callers.counts()[notice] == 1
    assert callers.row(notice)["wake_state"] == "delivered"
    callers.restart(reopen=True)
    callers.now += COOLDOWN_S
    callers.pass_once()
    assert callers.counts()[notice] == 1


@pytest.mark.parametrize("retryable", [False, None, "true"])
def test_c15_9_sendall_failure_stays_consumed_after_store_restart(callers, monkeypatch, retryable):
    """A possible partial write cannot be replayed; only boolean True proves pre-send."""
    sid = "sendall-unknown"
    private = "PRIVATE-SENDALL-EXCEPTION-DETAIL"
    callers.register(sid)
    notice = callers.finish(sid)

    def uncertain(session, body, **_kwargs):
        callers.attempts.append((session, body))
        return {"delivered": False, "session_id": session, "retryable": retryable,
                "reason": f"send-failed: BrokenPipeError: {private}"}

    monkeypatch.setattr(notify_push, "push_to_session", uncertain)
    callers.pass_once()
    row = callers.row(notice)
    assert row["state"] == "surfaced" and row["wake_state"] == "undeliverable"
    assert row["wake_reason"] == "send-uncertain" and row["wake_next_at"] is None
    assert private not in json.dumps(row)
    assert not callers.daemon.store.query("SELECT 1 FROM events WHERE data_json LIKE ?", (f"%{private}%",))
    attempts = len(callers.attempts)
    callers.restart(reopen=True)
    monkeypatch.setattr(notify_push, "push_to_session", callers.push)
    callers.now += COOLDOWN_S
    callers.pass_once()
    assert len(callers.attempts) == attempts
    assert callers.counts()[notice] == 0
    assert callers.row(notice)["wake_state"] == "undeliverable"


@pytest.mark.parametrize("delivered", [True, False], ids=["success", "safe-connect-failure"])
def test_c15_9_late_transport_result_never_changes_a_reused_notice_id(callers, monkeypatch, delivered):
    """Retention can delete an old notice while a different caller claims its reused id."""
    callers.register("old-caller")
    callers.register("new-caller")
    old_id = callers.finish("old-caller")
    old_job = callers.notices[old_id]
    replacement = []

    def transport_result_after_reuse(session, body, **_kwargs):
        assert session == "old-caller"
        assert callers.row(old_id)["wake_state"] == "claimed"
        with callers.daemon.store.transaction() as tx:
            tx.execute("DELETE FROM notices WHERE notice_id=?", (old_id,))
        new_id = callers.finish("new-caller")
        assert new_id == old_id and callers.notices[new_id] != old_job
        # Stand in for a concurrent delivery under the new caller's own lock.
        with callers.daemon.store.transaction() as tx:
            tx.execute("UPDATE notices SET state='surfaced',transport='caller-wake',"
                       "wake_state='claimed',wake_at=? WHERE notice_id=?",
                       (callers.now + 0.5, new_id))
        replacement.append(callers.row(new_id))
        if delivered:
            callers.sent.append((session, body))
            return {"delivered": True, "session_id": session}
        return {"delivered": False, "session_id": session, "retryable": True,
                "reason": "send-failed: ConnectionRefusedError: fixture"}

    monkeypatch.setattr(notify_push, "push_to_session", transport_result_after_reuse)
    callers.pass_once()
    assert len(replacement) == 1
    assert callers.row(old_id) == replacement[0], "the old transport result belongs only to the old claim"
