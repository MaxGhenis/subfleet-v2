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
import time
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import caller_wakes, hooks, notify_push, render
from subfleet.caller_wakes import MAX_GROUPS_PER_PASS, RETRY_WINDOW_S, STALE_REASON
from subfleet.conversations.wakes import COOLDOWN_S, MAX_STREAK
from subfleet.sessions import nudge, transcripts
from subfleet.sessions.transcripts import TurnState
from subfleet.store import Store
from tests.fake.test_notice_delivery import InProcess, job_notice, pending_ids, surface
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - fixture

#: The real inbox transport, taken before the `callers` fixture replaces it.
REAL_PUSH = notify_push.push_to_session


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
    """Review r1, finding 6: an app restart or account switch looks like this, so the
    notice backs off and retries for the 24 h window, then ends undeliverable."""
    callers.register("closed")
    notice = callers.finish("closed")
    if entry is None:
        callers.entries.pop("closed")
    else:
        callers.entries["closed"].update(entry)
    callers.pass_once()
    row = callers.row(notice)
    assert row["state"] == "pending" and row["wake_state"] is None and row["wake_attempts"] == 1
    assert row["wake_reason"] == ("session-not-registered" if entry is None else "session-not-running")
    assert callers.now < row["wake_next_at"] <= callers.now + 300
    callers.now += 24 * 3600
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
            tx.execute("INSERT INTO caller_wake_sessions(session_id,source) VALUES ('held','clear')")
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


# --- review r1 of #307: probes A to E and findings 4 to 8 ---------------------


def hook_prompt(callers, sid, prompt, *, client=None):
    """The UserPromptSubmit hook for `prompt`; the context it gave Claude ("" for none)."""
    stdout = io.StringIO()
    assert hooks.session_event("UserPromptSubmit", {"session_id": sid, "prompt": prompt},
                               callers.harness.root, client=client or InProcess(callers.daemon),
                               stdout=stdout, env={}) == 0
    printed = stdout.getvalue()
    return json.loads(printed)["hookSpecificOutput"]["additionalContext"] if printed else ""


def test_c15_9_a_notice_posttooluse_offered_is_never_woken(callers):
    """Review r1, probe A1: layer 2 showed the result and marked it `offered`; the
    caller then went quiet. No completion wake may repeat it."""
    sid = "post-tool-use"
    callers.register(sid)
    notice = callers.finish(sid)
    job = callers.daemon.store.get_job(callers.notices[notice])
    stderr = io.StringIO()
    assert hooks._deliver(InProcess(callers.daemon), sid, dict(job), stderr=stderr,
                          deadline=time.monotonic() + 60, now=time.monotonic) == 2
    assert callers.mentioned(notice, stderr.getvalue())
    assert callers.row(notice)["state"] == "offered"
    callers.activity[sid] = callers.now
    callers.now += 301
    callers.pass_once()
    assert callers.counts()[notice] == 0
    assert callers.row(notice)["wake_state"] is None
    # Layer 3 still shows an offered notice at the next prompt (C-15.2).
    assert callers.mentioned(notice, surface(callers.daemon, callers.harness.root, sid))


@pytest.mark.parametrize("launched", [False, True], ids=["ordinary", "subfleet-launched"])
def test_c15_9_a_result_wait_printed_is_never_woken(callers, monkeypatch, capsys, launched):
    """Review r1, probe A2: `subfleet wait` (as an attached `run`) printed the terminal
    result. It marks the caller's own notice `offered`, never acknowledged (C-15.3), and
    inside a process Subfleet launched it marks nothing, for C-24.10's repair."""
    from subfleet import cli
    sid = f"waited-{launched}"
    callers.register(sid)
    notice = callers.finish(sid)
    job = callers.notices[notice]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    for name in hooks.LAUNCH_MARKERS:
        monkeypatch.delenv(name, raising=False)
    if launched:
        monkeypatch.setenv("SUBFLEET_JOB", "a-conversation-turn")
    monkeypatch.setattr(cli, "_client", lambda *args, **kwargs: InProcess(callers.daemon))
    cli.wait_jobs(cli.build_parser().parse_args(["wait", job]), [job], timeout=30)
    assert job in capsys.readouterr().err
    row = callers.row(notice)
    if launched:
        assert row["state"] == "pending" and row["transport"] is None
        return
    assert row["state"] == "offered" and row["transport"] == "wait"
    callers.activity[sid] = callers.now
    callers.now += 301
    callers.pass_once()
    assert callers.counts()[notice] == 0


def streak_cycles(callers, monkeypatch, prompt):
    """Review r1, probe B: finish, 5 minutes of quiet, a pass, and the prompt hook the
    pushed turn fires (`wake`), a person's prompt (`person`), or no hook (`None`)."""
    monkeypatch.setattr(caller_wakes, "time", SimpleNamespace(time=lambda: callers.now))
    sid = f"streak-{prompt}"
    callers.register(sid)
    wakes = []
    for _ in range(MAX_STREAK + 3):
        callers.finish(sid)
        callers.now += 301
        before = len(callers.sent)
        callers.pass_once()
        wakes.append(len(callers.sent) - before)
        if prompt is not None and wakes[-1]:
            text = notify_push.envelope(callers.sent[-1][1]) if prompt == "wake" else "Thanks, carry on."
            assert hook_prompt(callers, sid, text) == ""
    return wakes


@pytest.mark.parametrize("prompt", ["wake", None], ids=["wake-prompt", "no-hook-control"])
def test_c15_9_the_wakes_own_prompt_leaves_the_streak_bound_in_force(callers, monkeypatch, prompt):
    """Review r1, probe B and its control: the wake's own envelope is not personal
    input, so at most MAX_STREAK wakes land before C-24.10's COOLDOWN_S."""
    wakes = streak_cycles(callers, monkeypatch, prompt)
    assert wakes == [1] * MAX_STREAK + [0] * 3
    clock = callers.daemon.store.one("SELECT * FROM caller_wake_sessions WHERE session_id=?",
                                     (f"streak-{prompt}",))
    assert clock["wake_streak"] == MAX_STREAK and clock["last_wake_at"] is not None


def test_c15_9_a_persons_prompt_still_resets_the_streak(callers, monkeypatch):
    """Any prompt other than Subfleet's own envelope is personal input (C-15.9)."""
    assert streak_cycles(callers, monkeypatch, "person") == [1] * (MAX_STREAK + 3)


def test_c15_9_a_notice_older_than_the_window_is_stale_on_its_first_attempt(callers):
    """Review r1, probe C: a month-old notice must not wake its quiet caller; the
    window's edge is stale too, and the hooks still show both."""
    sid = "month-old"
    callers.register(sid)
    old = callers.finish(sid, created=callers.now - 30 * 86400)
    edge = callers.finish(sid, created=callers.now - RETRY_WINDOW_S)
    fresh = callers.finish(sid, created=callers.now - RETRY_WINDOW_S + 60)
    callers.pass_once()
    assert callers.counts() == Counter({fresh: 1})
    for notice in (old, edge):
        row = callers.row(notice)
        assert (row["state"], row["wake_state"], row["wake_reason"], row["wake_attempts"]) == (
            "pending", "stale", STALE_REASON, 0)
    context = surface(callers.daemon, callers.harness.root, sid)
    assert callers.mentioned(old, context) and callers.mentioned(edge, context)
    callers.now += COOLDOWN_S
    callers.pass_once()
    assert callers.counts() == Counter({fresh: 1})


def test_c15_9_a_backlog_never_wakes_for_stale_notices_and_wakes_at_most_the_cap_per_pass(callers):
    """Review r1, probe E: 96 callers with old notices, 32 of them live and quiet, as on
    install. Only fresh notices wake, at most `max_sessions` callers in each pass."""
    live = [f"backlog-live-{index:02d}" for index in range(32)]
    gone = [f"backlog-gone-{index:02d}" for index in range(64)]
    for sid in live:
        callers.register(sid)
    # One finished job stands behind every old notice: the engine reads a notice's
    # job only to render a wake, so 96 more submissions would add time, not cover.
    origin = callers.finish("backlog-origin", created=callers.now - 40 * 86400)
    old = {origin}
    with callers.daemon.store.transaction() as tx:
        for index, sid in enumerate(live + gone):
            old.add(tx.execute(
                "INSERT INTO notices(job_id,session_id,text,state,created_at) VALUES (?,?,?,'pending',?)",
                (callers.notices[origin], sid, f"an old notice ({index})",
                 stamp(callers.now - (2 + index % 28) * 86400))).lastrowid)
    callers.notices.update(dict.fromkeys(old, callers.notices[origin]))
    fresh = {}
    for index, sid in enumerate(live + gone):
        if (sid in live and index % 2 == 0) or (sid in gone and index % 4 == 0):
            fresh[callers.finish(sid, created=callers.now - 600)] = sid
    assert len(fresh) == 32
    cap = int(nudge.caps(callers.daemon.policy)["max_sessions"])
    per_pass = []
    for _ in range(8):
        before = len(callers.sent)
        result = callers.pass_once()
        per_pass.append(len(callers.sent) - before)
        assert result["sessions"] <= cap and result["examined"] <= MAX_GROUPS_PER_PASS
        callers.now += 3
    assert max(per_pass) == cap and sum(per_pass) == 16
    counts = callers.counts()
    assert set(counts) == {notice for notice, sid in fresh.items() if sid in live}
    assert set(counts.values()) == {1}
    assert {callers.row(notice)["wake_state"] for notice in old} == {"stale"}
    away = [callers.row(notice) for notice, sid in fresh.items() if sid in gone]
    assert {(row["state"], row["wake_state"], row["wake_reason"]) for row in away} == {
        ("pending", None, "session-not-registered")}


def test_c15_9_a_lane_refusal_leaves_the_notice_for_the_hook(callers, monkeypatch):
    """Review r1, probe D: the real transport refuses a lane before any token or socket
    use, so the notice is unread again and retries on the backoff."""
    sid = "lane"
    callers.register(sid)
    notice = callers.finish(sid)

    def never(*_args, **_kwargs):
        raise AssertionError("a lane refusal reads no token and opens no socket")

    monkeypatch.setattr(notify_push, "push_to_session", REAL_PUSH)
    monkeypatch.setattr(notify_push, "peer_token", never)
    monkeypatch.setattr(notify_push, "send_to_socket", never)
    monkeypatch.setattr(callers.daemon, "_lane_session_ids", lambda: [sid])
    callers.pass_once()
    row = callers.row(notice)
    assert (row["state"], row["transport"], row["wake_state"], row["wake_reason"]) == (
        "pending", None, None, notify_push.LANE_REFUSAL)
    assert row["wake_attempts"] == 1 and callers.now < row["wake_next_at"] <= callers.now + 300
    clock = callers.daemon.store.one("SELECT * FROM caller_wake_sessions WHERE session_id=?", (sid,))
    assert clock["wake_streak"] == 0 and clock["last_wake_at"] is None
    assert callers.mentioned(notice, surface(callers.daemon, callers.harness.root, sid))


def test_c15_9_a_surface_claim_after_its_client_gave_up_marks_nothing(callers):
    """Review r1, finding 5: the hook's client times out and prints offline; a notice
    that arrives before its queued claim runs must stay unread, not be marked shown."""
    sid = "late-claim"
    callers.register(sid)
    first = callers.finish(sid)
    asked = []

    class GaveUp:
        timeout = 15.0

        def call(self, op, args=None, **kwargs):
            asked.append((op, dict(args or {}), kwargs))
            raise OSError("timed out waiting for the daemon")

    assert callers.mentioned(first, hook_prompt(callers, sid, "go on", client=GaveUp()))
    [(op, args, kwargs)] = asked
    assert args["deadline_s"] == 15.0 - hooks.SURFACE_REPLY_MARGIN_S and kwargs["timeout"] == 15.0
    second = callers.finish(sid)                    # after the offline read
    late = callers.daemon.dispatch(op, args, arrived=time.monotonic() - 15.0)
    assert late["notices"] == []
    assert callers.row(second)["state"] == "pending"
    assert callers.row(first)["transport"] == "hook:offline"     # its receipt reconciled
    gone = callers.daemon.dispatch(op, args, arrived=time.monotonic(), client_gone=lambda: True)
    assert gone["notices"] == [] and callers.row(second)["state"] == "pending"
    in_time = callers.daemon.dispatch(op, args, arrived=time.monotonic())
    assert [row["notice_id"] for row in in_time["notices"]] == [second]
    assert callers.row(second)["state"] == "surfaced"


def test_c15_9_a_caller_back_from_an_app_restart_is_woken_within_the_window(callers):
    """Review r1, finding 6: a missing registry row is retried, not terminal at once."""
    sid = "restarted"
    callers.register(sid)
    notice = callers.finish(sid)
    entry = callers.entries.pop(sid)
    for _ in range(5):
        callers.pass_once()
        callers.now = callers.row(notice)["wake_next_at"]
    assert callers.row(notice)["wake_attempts"] == 5
    callers.entries[sid] = {**entry, "pid": 23456}
    callers.pass_once()
    assert callers.counts()[notice] == 1
    assert callers.row(notice)["wake_state"] == "delivered"


@pytest.mark.parametrize("source,woken", [("compact", True), ("startup", True), ("resume", True),
                                          ("clear", False)])
def test_c15_9_compaction_keeps_completion_wakes_and_clear_holds_them(callers, source, woken):
    """Review r1, finding 7: an autocompacted session is still the caller; a cleared
    context has none of the work, so its notices wait for the next prompt."""
    sid = f"after-{source}"
    callers.register(sid)
    with callers.daemon.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,source) VALUES (?,?)", (sid, source))
    notice = callers.finish(sid)
    callers.pass_once()
    assert callers.counts()[notice] == int(woken)
    assert callers.row(notice)["state"] == ("surfaced" if woken else "pending")


def test_c15_9_a_held_caller_is_not_read_again_until_something_changes(callers, monkeypatch):
    """Review r1, finding 8: no transcript tail read for a caller already known to be
    held until its notices, registry row, transcript or clock change, or a gate lapses."""
    sid = "held-memo"
    callers.register(sid, age=0)
    notice = callers.finish(sid)
    reads = []

    def counted(path, **kwargs):
        reads.append(path)
        return callers.turn_state(path, **kwargs)

    monkeypatch.setattr(transcripts, "turn_state", counted)
    callers.pass_once()
    assert len(reads) == 1
    for _ in range(5):
        callers.now += 10
        callers.pass_once()
    assert len(reads) == 1, "held and unchanged: no tail read"
    callers.entries[sid] = {**callers.entries[sid], "status_updated_at": (callers.now - 3600) * 1000}
    callers.pass_once()
    assert len(reads) == 2, "a registry change"
    second = callers.finish(sid)
    callers.pass_once()
    assert len(reads) == 3, "a new notice"
    (callers.harness.root / f"{sid}.jsonl").write_text("{}\n")
    callers.pass_once()
    assert len(reads) == 4, "the transcript changed"
    callers.now = callers.activity[sid] + 300
    callers.pass_once()
    assert callers.counts() == Counter({notice: 1, second: 1}), "the quiet gate lapsed"


def test_c15_9_a_pass_examines_a_bounded_number_of_callers_and_reaches_them_all(callers, monkeypatch):
    """Review r1, finding 8: one pass examines at most MAX_GROUPS_PER_PASS callers, and
    the next starts after the last it examined."""
    monkeypatch.setattr(caller_wakes, "MAX_GROUPS_PER_PASS", 3)
    sids = [f"rotate-{index}" for index in range(7)]
    for sid in sids:
        callers.register(sid, age=0)                # all held by the quiet gate
        callers.finish(sid)
    reads = []

    def counted(path, **kwargs):
        reads.append(Path(path).stem)
        return callers.turn_state(path, **kwargs)

    monkeypatch.setattr(transcripts, "turn_state", counted)
    seen = []
    for _ in range(3):
        before = len(reads)
        assert callers.pass_once()["examined"] == 3
        seen.append(reads[before:])
    assert seen == [sids[:3], sids[3:6], sids[6:]]
    callers.now += 300
    for _ in range(3):
        callers.pass_once()
    assert len(callers.sent) == len(sids)


def test_c15_9_a_header_that_cannot_render_leaves_no_claim_behind(callers, monkeypatch):
    """Review r1, the P3 at caller_wakes.py:212: headers render before the claim, so a
    failure there leaves the notice unread and retried, never `claimed` until a restart."""
    sid = "unrenderable"
    callers.register(sid)
    notice = callers.finish(sid)
    header = render.notice_header

    def broken(*_args, **_kwargs):
        raise KeyError("a job row without a field")

    monkeypatch.setattr(render, "notice_header", broken)
    callers.pass_once()
    row = callers.row(notice)
    assert (row["state"], row["wake_state"], row["wake_reason"]) == ("pending", None, "header-unavailable")
    assert callers.attempts == []
    monkeypatch.setattr(render, "notice_header", header)
    callers.now = row["wake_next_at"]
    callers.pass_once()
    assert callers.counts()[notice] == 1



# --- review r2 of #307: one reservation per notice, and activity under the lock ---


def post_tool_use(callers, sid, notice, client):
    """Layer 2's delivery of `notice`'s job, as `_wait_and_deliver` makes it once the job
    is terminal: (exit code, whether Claude was shown the result)."""
    stderr = io.StringIO()
    job = callers.daemon.store.get_job(callers.notices[notice])
    code = hooks._deliver(client, sid, dict(job), stderr=stderr,
                          deadline=time.monotonic() + 60, now=time.monotonic)
    return code, code == 2 and callers.mentioned(notice, stderr.getvalue())


class HookClient(InProcess):
    """The hook's daemon client, with the attributes `hooks.Client` has."""

    def __init__(self, callers, *, after_first_notice_call=None):
        super().__init__(callers.daemon)
        self.root, self.timeout = callers.daemon.root, 30.0
        self.after = after_first_notice_call

    def call(self, op, args=None, **kwargs):
        answer = super().call(op, args, **kwargs)
        if op == "notice.pending" and self.after is not None:
            after, self.after = self.after, None
            after()
        return answer


def test_posttooluse_pending_read_then_engine_send_never_both(callers, monkeypatch):
    """Review r2 of #307, P1 1, first ordering: PostToolUse asks the daemon for the job's
    notice, a wake pass runs before it prints, and the result reaches the caller once."""
    monkeypatch.setattr(caller_wakes, "CLAIM_LOCK_WAIT_S", 0.05, raising=False)
    sid = "post-tool-use-then-wake"
    callers.register(sid)
    notice = callers.finish(sid)
    code, printed = post_tool_use(callers, sid, notice,
                                  HookClient(callers, after_first_notice_call=callers.pass_once))
    assert callers.counts()[notice] + int(printed) == 1, "never both: one inbox wake or one hook print"
    assert printed and code == 2, "PostToolUse claimed it first, so PostToolUse shows it"
    assert callers.row(notice)["wake_state"] is None
    callers.now += COOLDOWN_S + 600
    callers.pass_once()
    assert callers.counts()[notice] == 0


def test_engine_claim_then_posttooluse_delivery_never_both(callers, monkeypatch):
    """Review r2 of #307, P1 1, second ordering: the wake has claimed the notice and is
    sending when PostToolUse delivers; PostToolUse lost the claim, so it shows nothing,
    not even its no-notice fallback."""
    import threading
    monkeypatch.setattr(caller_wakes, "CLAIM_LOCK_WAIT_S", 30.0, raising=False)
    sid = "wake-then-post-tool-use"
    callers.register(sid)
    notice = callers.finish(sid)
    push = callers.push
    hook = {}

    def deliver():
        hook["result"] = post_tool_use(callers, sid, notice, HookClient(callers))

    def sending(session, body, **kwargs):
        # The claim has committed; the hook runs while the wake holds the caller.
        assert callers.row(notice)["wake_state"] == "claimed"
        hook["thread"] = threading.Thread(target=deliver)
        hook["thread"].start()
        hook["thread"].join(0.5)
        return push(session, body, **kwargs)

    monkeypatch.setattr(notify_push, "push_to_session", sending)
    callers.pass_once()
    hook["thread"].join(60)
    assert not hook["thread"].is_alive()
    code, printed = hook["result"]
    assert callers.counts()[notice] + int(printed) == 1, "never both: one inbox wake or one hook print"
    assert callers.counts()[notice] == 1 and (code, printed) == (0, False)
    assert callers.row(notice)["wake_state"] == "delivered"


@pytest.mark.parametrize("signal", ["all", "prompt-hook", "registry", "transcript"])
def test_human_prompt_during_header_render_prevents_wake(callers, monkeypatch, signal):
    """Review r2 of #307, P1 2: a person types while the pass renders the wake's headers.
    The prompt hook ran before the claim, so no wake may follow: its activity is recorded
    though the pass holds the delivery lock, and the claim judges the registry and the
    transcript as they are after rendering. `all` is the review's probe; each other case
    moves one signal alone."""
    monkeypatch.setattr(caller_wakes, "time", SimpleNamespace(time=lambda: callers.now))
    sid = f"typing-{signal}"
    callers.register(sid)
    notice = callers.finish(sid)
    header = render.notice_header
    typed = []

    def person_types(*args, **kwargs):
        if not typed:
            typed.append(signal)
            if signal in ("all", "transcript"):
                callers.activity[sid] = callers.now                 # the prompt's record
            if signal in ("all", "registry"):
                callers.entries[sid] = {**callers.entries[sid], "status": "busy"}
            if signal in ("all", "prompt-hook"):
                # The real UserPromptSubmit hook; the pass holds the lock, so it prints nothing.
                assert hook_prompt(callers, sid, "one more thing, please") == ""
        return header(*args, **kwargs)

    monkeypatch.setattr(render, "notice_header", person_types)
    callers.pass_once()
    assert typed == [signal]
    assert callers.counts()[notice] == 0, "the caller was active before the claim"
    row = callers.row(notice)
    assert (row["state"], row["transport"], row["wake_state"]) == ("pending", None, None)
    if signal == "prompt-hook":
        clock = callers.daemon.store.one("SELECT * FROM caller_wake_sessions WHERE session_id=?", (sid,))
        assert clock["active_at"] == callers.now
    if signal != "registry" and signal != "all":
        callers.now += 301                                          # quiet again: one wake
        callers.pass_once()
        assert callers.counts()[notice] == 1


def test_c15_9_a_posttooluse_print_that_fails_gives_its_claim_back(callers):
    """Review r2 of #307, with r1's finding 10: a crash between the claim and the print
    leaves the notice retryable. Exit 0 shows the harness nothing, so the hook gives
    the claim back and the wake delivers the notice, once."""
    sid = "broken-stderr"
    callers.register(sid)
    notice = callers.finish(sid)

    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("the harness closed the hook's stderr")

    job = callers.daemon.store.get_job(callers.notices[notice])
    assert hooks._deliver(HookClient(callers), sid, dict(job), stderr=Broken(),
                          deadline=time.monotonic() + 60, now=time.monotonic) == 0
    row = callers.row(notice)
    assert (row["state"], row["transport"], row["offered_at"], row["wake_state"]) == ("pending", None, None, None)
    callers.pass_once()
    assert callers.counts()[notice] == 1


def test_c15_9_posttooluse_prints_its_job_line_only_when_the_session_has_no_notice(callers):
    """Review r2 of #307, P1 1: the line from the job row is printed only when its claim
    succeeded, that is, when the session has no notice for the job at all. A notice a
    prompt hook already showed is held elsewhere: PostToolUse prints nothing."""
    sid = "line-from-the-job"
    callers.register(sid, age=0)                    # active: no wake in this test
    notice = callers.finish(sid)
    assert callers.mentioned(notice, surface(callers.daemon, callers.harness.root, sid))
    assert post_tool_use(callers, sid, notice, HookClient(callers)) == (0, False)
    job = callers.notices[notice]
    with callers.daemon.store.transaction() as tx:
        tx.execute("DELETE FROM notices WHERE notice_id=?", (notice,))
    stderr = io.StringIO()
    assert hooks._deliver(HookClient(callers), sid, dict(callers.daemon.store.get_job(job)), stderr=stderr,
                          deadline=time.monotonic() + 60, now=time.monotonic) == 2
    assert stderr.getvalue().startswith(f"{job}: ") and "no notice row for this job" in stderr.getvalue()


def test_c15_9_a_posttooluse_claim_after_its_client_gave_up_marks_nothing(callers):
    """As finding 5 of r1 for the prompt hooks: a PostToolUse claim the daemon reaches
    after its deadline, or after its client has gone, reserves nothing, so the notice
    stays for the wake."""
    sid = "late-post-tool-use"
    callers.register(sid)
    notice = callers.finish(sid)
    args = {"session_id": sid, "surface": "PostToolUse", "job_id": callers.notices[notice], "deadline_s": 5.0}
    late = callers.daemon.dispatch("notice.pending", args, arrived=time.monotonic() - 5.0)
    gone = callers.daemon.dispatch("notice.pending", args, arrived=time.monotonic(), client_gone=lambda: True)
    assert late == gone == {"notices": [], "absent": False}
    assert callers.row(notice)["state"] == "pending"
    callers.pass_once()
    assert callers.counts()[notice] == 1


def test_c15_9_posttooluse_honours_an_offline_hooks_receipt(callers):
    """One reservation across every path: a notice an offline prompt hook printed (its
    receipt not yet reconciled) is not printed again by PostToolUse, nor woken."""
    sid = "offline-then-post-tool-use"
    callers.register(sid)
    notice = callers.finish(sid)

    class OfflineClient:
        def call(self, *args, **kwargs):
            raise OSError("app restarting")

    assert callers.mentioned(notice, hook_prompt(callers, sid, "status?", client=OfflineClient()))
    assert callers.row(notice)["state"] == "pending"            # only the receipt knows
    assert post_tool_use(callers, sid, notice, HookClient(callers)) == (0, False)
    assert callers.row(notice)["transport"] == "hook:offline"
    callers.now += 600
    callers.pass_once()
    assert callers.counts()[notice] == 0


@pytest.mark.parametrize("failure", ["busy", "socket"])
def test_offline_prompt_under_render_lock_prevents_wake(callers, monkeypatch, failure):
    """Review r3: a completed offline prompt holds the wake even under its lock."""
    from subfleet import notice_claims
    from subfleet.client import DaemonError, DaemonUnavailable

    monkeypatch.setattr(notice_claims, "time", SimpleNamespace(time=lambda: callers.now))
    sid = f"offline-render-{failure}"
    callers.register(sid)
    notice = callers.finish(sid)
    header = render.notice_header
    typed = []

    class OfflineClient:
        def call(self, *args, **kwargs):
            if failure == "busy":
                raise DaemonError(69, "fixture daemon is busy")
            raise DaemonUnavailable("fixture socket is unavailable")

    def person_types(*args, **kwargs):
        if not typed:
            typed.append(True)
            assert hook_prompt(callers, sid, "one more thing", client=OfflineClient()) == ""
        return header(*args, **kwargs)

    monkeypatch.setattr(render, "notice_header", person_types)
    callers.pass_once()
    assert typed == [True]
    assert callers.counts()[notice] == 0, "the offline prompt completed before the claim"
    assert callers.row(notice)["state"] == "pending"
    callers.now += 301
    callers.pass_once()
    assert callers.counts()[notice] == 1


@pytest.mark.parametrize("connection", ["offline"])
def test_personal_prompt_under_taken_lock_resets_streak(callers, monkeypatch, connection):
    """A personal prompt resets eight unattended wakes even if it claims nothing."""
    from subfleet import notice_claims

    clock = SimpleNamespace(time=lambda: callers.now)
    monkeypatch.setattr(notice_claims, "time", clock)
    monkeypatch.setattr(caller_wakes, "time", clock)
    sid = f"personal-taken-lock-{connection}"
    callers.register(sid)
    notice = callers.finish(sid)
    with callers.daemon.store.transaction() as tx:
        tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,wake_streak,last_wake_at) "
                   "VALUES (?,?,?,?)", (sid, callers.now - 600, MAX_STREAK, callers.now - 1))

    class OfflineClient:
        def call(self, *args, **kwargs):
            raise OSError("fixture app is restarting")

    with notice_claims.delivery_lock(callers.daemon.root, sid) as held:
        assert held
        assert hook_prompt(callers, sid, "please continue", client=OfflineClient()) == ""
    callers.activity[sid] = callers.now
    callers.entries[sid] = {**callers.entries[sid], "status": "idle"}
    callers.now += 301
    callers.pass_once()
    assert callers.counts()[notice] == 1, "personal activity must reset the unattended streak"
    assert callers.daemon.store.one("SELECT wake_streak FROM caller_wake_sessions WHERE session_id=?", (sid,)) == {
        "wake_streak": 1}
