"""Round-six reviewer regressions, including real SQLite hard-exit recovery."""
import contextlib
import json
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

from subfleet.conversations import wakes
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationStore
from tests.unit.test_conversation_service import EndedRunner, submit, svc  # noqa: F401
from tests.unit.test_review_r2_repro import bound, iso, wake_rows, wake_texts, settle_wakes
from tests.unit.test_review_r5_repro import SimulatedCrash


def finished_turn(svc, cid, text, after=None):
    mid = submit(svc, cid, after=after)
    svc._dispatch()
    host = svc.store.message(mid)["job_id"]
    svc.daemon.store.add_attempt(attempt_id=host + "/a1", job_id=host, seq=1,
                                lane_id="claude-1", model_requested="opus", state="succeeded")
    svc.daemon.store.update_job(host, state="succeeded")
    adir = svc.root / "jobs" / host / "a1"
    adir.mkdir(parents=True)
    (adir / "stdin.jsonl").write_text('{}\n')
    (adir / "turn.json").write_text(json.dumps({
        "state": "complete", "ended_by": "provider", "served": {},
        "native_session_id": svc.store.conversation(cid)["native_session_id"],
        "final_text": text}))
    runner = EndedRunner(adir, mid, cid)
    runner.attempt_id = host + "/a1"
    runner.attempt["attempt_id"] = runner.attempt_id
    svc.store.set_state(mid, "running")
    return runner


def reopen_store(svc):
    svc.wakes.close()
    svc.store.close()
    svc.store = ConversationStore(svc.root)
    svc.wakes = wakes.WakeEngine(svc)


@pytest.mark.parametrize("upgrade", [False, True], ids=["current-store", "pre-intent-store"])
@pytest.mark.parametrize("boundary", [
    "before-completion-commit", "after-completion-commit", "before-register",
    "after-register", "before-ack-commit", "after-ack-commit", "no-crash",
])
def test_completion_intent_all_transaction_boundaries(svc, monkeypatch, upgrade, boundary):
    cid = bound(svc)
    now = time.time()
    if upgrade:
        with svc.store.transaction() as tx:
            tx.execute("DROP TABLE final_wake_intents")
        reopen_store(svc)
        assert svc.store.one("SELECT name FROM sqlite_master WHERE name='final_wake_intents'")
        assert svc.store.one("SELECT MAX(version) v FROM schema_version")["v"] == 2
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    runner = finished_turn(svc, cid, f'WAKE-ME: at={iso(now + 600)} note="Recovered timer"')
    mid = runner.message_id

    with monkeypatch.context() as crash:
        transaction = svc.store.transaction
        if boundary in ("before-completion-commit", "before-ack-commit"):
            @contextlib.contextmanager
            def fail_before_commit():
                with transaction() as tx:
                    yield tx
                    intent = tx.execute("SELECT 1 FROM final_wake_intents WHERE message_id=?", (mid,)).fetchone()
                    registered = tx.execute("SELECT 1 FROM wake_requests").fetchone()
                    if (boundary == "before-completion-commit" and intent and not registered) or (
                            boundary == "before-ack-commit" and registered and not intent):
                        raise SimulatedCrash()
            crash.setattr(svc.store, "transaction", fail_before_commit)
        elif boundary in ("after-completion-commit", "after-ack-commit"):
            notify = svc.store.notify
            def fail_after_commit():
                intent = svc.store.one("SELECT 1 FROM final_wake_intents WHERE message_id=?", (mid,))
                registered = svc.store.one("SELECT 1 FROM wake_requests")
                if (boundary == "after-completion-commit" and intent and not registered) or (
                        boundary == "after-ack-commit" and registered and not intent):
                    raise SimulatedCrash()
                notify()
            crash.setattr(svc.store, "notify", fail_after_commit)
        elif boundary == "before-register":
            crash.setattr(svc.wakes, "register", lambda *_args, **_kw: (_ for _ in ()).throw(SimulatedCrash()))
        elif boundary == "after-register":
            register = svc.wakes.register
            def fail_after_register(*args, **kwargs):
                register(*args, **kwargs)
                raise SimulatedCrash()
            crash.setattr(svc.wakes, "register", fail_after_register)
        if boundary == "no-crash":
            svc._on_outcome(runner)
        else:
            with pytest.raises(SimulatedCrash):
                svc._on_outcome(runner)

    if boundary == "before-completion-commit":
        assert svc.store.message(mid)["state"] == "running"
        assert svc.store.query("SELECT * FROM final_wake_intents") == []
    else:
        assert svc.store.message(mid)["state"] == "complete"
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    def replay(_attempt, *, ended=False):
        assert ended
        restarted._on_outcome(runner)
        return True
    monkeypatch.setattr(restarted, "_adopt", replay)
    try:
        for _ in range(3):
            restarted._replay_unsettled()
        clock[0] = now + 601
        for _ in range(3):
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
        assert len(wake_rows(restarted, cid)) == 1
        assert "Recovered timer" in wake_texts(restarted, cid)[0]
        assert restarted.store.query("SELECT * FROM final_wake_intents") == []
        assert len(restarted.store.query("SELECT * FROM wake_requests")) == 1
        print(f"R6 boundary {upgrade=} {boundary=}: exactly one wake")
    finally:
        restarted.close()


@pytest.mark.parametrize("crash_after_first", [False, True], ids=["no-crash", "partial-registration"])
def test_service_tick_finishes_final_batch_before_firing_superseded_timer(svc, monkeypatch, crash_after_first):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    text = (f'WAKE-ME: at={iso(now + 300)} note="Superseded earlier timer"\n'
            f'WAKE-ME: at={iso(now + 600)} note="Replacement timer"')
    runner = finished_turn(svc, cid, text)
    if crash_after_first:
        with monkeypatch.context() as crash:
            register = svc.wakes.register
            def interrupt(*args, **kwargs):
                register(*args, **kwargs)
                raise SimulatedCrash()
            crash.setattr(svc.wakes, "register", interrupt)
            with pytest.raises(SimulatedCrash):
                svc._on_outcome(runner)
    else:
        svc._on_outcome(runner)
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    monkeypatch.setattr(restarted, "_catalog_tick", lambda: None)
    try:
        clock[0] = now + 601
        restarted.tick()  # Preserve the production order, including dispatch and recovery.
        first = wake_texts(restarted, cid)
        for message in wake_rows(restarted, cid):
            if message["job_id"]:
                restarted.daemon.store.update_job(message["job_id"], state="succeeded")
            restarted.store.set_state(message["message_id"], "complete")
        clock[0] += 2
        restarted.tick()
        texts = wake_texts(restarted, cid)
        print(f"R6 real tick {crash_after_first=}: first={first}; final={texts}")
        assert len(texts) == 1, "partial final replay fired the superseded timer and its replacement"
        assert "Replacement timer" in texts[0]
        assert "Superseded earlier timer" not in texts[0]
    finally:
        restarted.close()


@pytest.mark.parametrize("boundary", ["unregistered", "registered-unacked"])
def test_recovered_intent_yields_to_newer_person_and_rearm(svc, monkeypatch, boundary):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    runner = finished_turn(svc, cid, f'WAKE-ME: at={iso(now + 600)} note="Older request"')
    with monkeypatch.context() as crash:
        register = svc.wakes.register
        def interrupt(*args, **kwargs):
            if boundary == "registered-unacked":
                register(*args, **kwargs)
            raise SimulatedCrash()
        crash.setattr(svc.wakes, "register", interrupt)
        with pytest.raises(SimulatedCrash):
            svc._on_outcome(runner)
    newer = submit(svc, cid, "My newer instructions", after=runner.message_id)
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    monkeypatch.setattr(restarted, "_catalog_tick", lambda: None)
    try:
        clock[0] = now + 601
        restarted.tick()
        assert wake_rows(restarted, cid) == []
        assert restarted.store.message(newer)["job_id"]
        assert restarted.store.conversation(cid)["wake_streak"] == 0
        restarted.op_conversation_wake({
            "session_id": restarted.store.conversation(cid)["native_session_id"],
            "request_id": str(uuid.uuid4()), "at": iso(now + 1200), "note": "Newer request"}, None)
        job = restarted.store.message(newer)["job_id"]
        restarted.daemon.store.update_job(job, state="succeeded")
        restarted.store.set_state(newer, "complete")
        restarted.wakes.tick(poll=False)
        assert wake_rows(restarted, cid) == []
        clock[0] = now + 1201
        for _ in range(3):
            restarted._replay_unsettled()
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
        assert len(wake_rows(restarted, cid)) == 1
        assert "Newer request" in wake_texts(restarted, cid)[0]
        assert "Older request" not in wake_texts(restarted, cid)[0]
    finally:
        restarted.close()


@pytest.mark.parametrize("crash_before_rearm", [False, True], ids=["no-crash", "completed-newer-intent"])
def test_service_tick_recovers_newer_completed_turn_before_old_timer(svc, monkeypatch, crash_before_rearm):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    older = finished_turn(svc, cid, f'WAKE-ME: at={iso(now + 600)} note="Old instructions"')
    svc._on_outcome(older)
    clock[0] += 60
    newer = finished_turn(svc, cid, f'WAKE-ME: at={iso(now + 1200)} note="New instructions"',
                          after=older.message_id)
    if crash_before_rearm:
        with monkeypatch.context() as crash:
            crash.setattr(svc.wakes, "from_final", lambda *_: (_ for _ in ()).throw(SimulatedCrash()))
            with pytest.raises(SimulatedCrash):
                svc._on_outcome(newer)
    else:
        svc._on_outcome(newer)
    assert svc.store.message(newer.message_id)["state"] == "complete"
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    monkeypatch.setattr(restarted, "_catalog_tick", lambda: None)
    try:
        clock[0] = now + 601
        restarted.tick()
        early = wake_texts(restarted, cid)
        for message in wake_rows(restarted, cid):
            if message["job_id"]:
                restarted.daemon.store.update_job(message["job_id"], state="succeeded")
            restarted.store.set_state(message["message_id"], "complete")
        clock[0] = now + 1201
        restarted.tick()
        texts = wake_texts(restarted, cid)
        print(f"R6 newer complete {crash_before_rearm=}: early={early}; final={texts}")
        assert early == [], "old timer fired after the newer person turn completed its replacement intent"
        assert len(texts) == 1 and "New instructions" in texts[0]
    finally:
        restarted.close()


@pytest.mark.parametrize("migration_boundary", ["before-table", "after-table"])
def test_interrupted_additive_intent_migration_is_retryable(svc, monkeypatch, migration_boundary):
    cid = bound(svc)
    now = time.time()
    request_id = str(uuid.uuid4())
    svc.wakes.now = lambda: now
    svc.wakes.register(cid, request_id, wakes.normalize(at=iso(now + 600), note="Pre-upgrade request", now=now))
    with svc.store.transaction() as tx:
        tx.execute("DROP TABLE final_wake_intents")
    svc.close()
    connect = sqlite3.connect
    opened = []

    class InterruptedConnection(sqlite3.Connection):
        def executescript(self, sql):
            marker = "CREATE TABLE IF NOT EXISTS final_wake_intents"
            if marker in sql:
                super().executescript(sql.split(marker, 1)[0] if migration_boundary == "before-table" else sql)
                raise SimulatedCrash()
            return super().executescript(sql)

    def interrupted_connect(*args, **kwargs):
        db = connect(*args, factory=InterruptedConnection, **kwargs)
        opened.append(db)
        return db

    with monkeypatch.context() as crash:
        crash.setattr(sqlite3, "connect", interrupted_connect)
        try:
            with pytest.raises(SimulatedCrash):
                ConversationStore(svc.root)
        finally:
            for db in opened:
                db.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: now + 601
    try:
        assert restarted.store.one("SELECT name FROM sqlite_master WHERE name='final_wake_intents'")
        assert restarted.store.one("SELECT MAX(version) v FROM schema_version")["v"] == 2
        assert restarted.store.one("SELECT state FROM wake_requests WHERE request_id=?", (request_id,))["state"] == "pending"
        for _ in range(3):
            restarted._replay_unsettled()
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
        assert len(wake_rows(restarted, cid)) == 1
        assert "Pre-upgrade request" in wake_texts(restarted, cid)[0]
        print(f"R6 migration {migration_boundary}: preserved request fired exactly once")
    finally:
        restarted.close()


def test_control_evaluation_cannot_fire_old_timer_during_final_registration(svc, monkeypatch):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    older = finished_turn(svc, cid, f'WAKE-ME: at={iso(now + 600)} note="Old instructions"')
    svc._on_outcome(older)
    newer = finished_turn(svc, cid, f'WAKE-ME: at={iso(now + 1200)} note="New instructions"',
                          after=older.message_id)
    clock[0] = now + 601
    register = svc.wakes.register
    evaluations = []

    def interleaved_registration(*args, **kwargs):
        # The control worker does not take the service lock held by _on_outcome.
        # Run its real store reads/claim at the completion-before-register boundary.
        assert svc.store.message(newer.message_id)["state"] == "complete"
        assert svc.store.one("SELECT 1 FROM final_wake_intents WHERE message_id=?", (newer.message_id,))
        svc.wakes.tick(poll=False)
        evaluations.append(wake_texts(svc, cid))
        return register(*args, **kwargs)

    monkeypatch.setattr(svc.wakes, "register", interleaved_registration)
    svc._on_outcome(newer)
    print(f"R6 interleaved completion/registration: wakes={evaluations}")
    assert evaluations == [[]], "the control worker fired the old timer before final rearm registered"


@pytest.mark.parametrize("boundary", ["before-refusal-event", "after-refusal-event"])
def test_interrupted_final_refusal_is_visible_once_and_keeps_valid_line(svc, monkeypatch, boundary):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    runner = finished_turn(svc, cid, 'WAKE-ME: unknown=bad\n' +
                           f'WAKE-ME: at={iso(now + 600)} note="Valid later line"')
    with monkeypatch.context() as crash:
        append = svc.store.append_events
        def interrupt(*args, **kwargs):
            if boundary == "after-refusal-event":
                append(*args, **kwargs)
            raise SimulatedCrash()
        crash.setattr(svc.store, "append_events", interrupt)
        with pytest.raises(SimulatedCrash):
            svc._on_outcome(runner)
    assert svc.store.message(runner.message_id)["state"] == "complete"
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    try:
        for _ in range(3):
            restarted._replay_unsettled()
        clock[0] = now + 601
        for _ in range(3):
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
        refusals = [json.loads(row["data_json"]) for row in restarted.store.query(
            "SELECT data_json FROM events WHERE message_id=? AND kind='status'", (runner.message_id,))
            if json.loads(row["data_json"]).get("phase") == "wake-refused"]
        print(f"R6 refusal {boundary}: events={refusals}; wakes={wake_texts(restarted, cid)}")
        assert len(refusals) == 1 and "invalid WAKE-ME field" in refusals[0]["detail"]
        assert len(wake_rows(restarted, cid)) == 1 and "Valid later line" in wake_texts(restarted, cid)[0]
        assert restarted.store.query("SELECT * FROM final_wake_intents") == []
    finally:
        restarted.close()


@pytest.mark.parametrize("boundary", [
    "before-completion-commit", "after-completion-commit", "after-registration",
    "before-ack-commit", "after-ack-commit",
])
def test_hard_exit_recovers_actual_wal_at_registration_boundaries(svc, boundary):
    """A foreground child exits without context-manager cleanup or sqlite close."""
    cid = bound(svc)
    now = time.time()
    text = f'WAKE-ME: at={iso(now + 600)} note="Hard-exit recovery"'
    runner = finished_turn(svc, cid, text)
    svc.close()
    script = r'''
import contextlib, logging, os, sys
from pathlib import Path
from types import SimpleNamespace
from subfleet.store import Store
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.wakes import WakeEngine
root, cid, mid, text, now, boundary = sys.argv[1:]
store = ConversationStore(root)
jobs = Store(Path(root) / 'state.sqlite3')
service = SimpleNamespace(store=store, daemon=SimpleNamespace(store=jobs), log=logging.getLogger('hard-exit'))
engine = WakeEngine(service)
engine.now = lambda: float(now)
transaction = store.transaction
class InterruptTx:
    def __init__(self, tx): self.tx = tx
    def __getattr__(self, name): return getattr(self.tx, name)
    def execute(self, sql, params=()):
        result = self.tx.execute(sql, params)
        if (boundary == 'before-completion-commit' and sql.startswith('INSERT INTO final_wake_intents')) or (
                boundary == 'before-ack-commit' and sql.startswith('DELETE FROM final_wake_intents')):
            os._exit(77)
        return result
@contextlib.contextmanager
def interrupted_transaction():
    with transaction() as tx: yield InterruptTx(tx)
if boundary in ('before-completion-commit', 'before-ack-commit'):
    store.transaction = interrupted_transaction
store.set_state(mid, 'complete', expect=('running',), final_wake=(text, float(now)))
if boundary == 'after-completion-commit': os._exit(77)
if boundary == 'after-registration':
    register = engine.register
    def interrupted_register(*args, **kwargs):
        register(*args, **kwargs)
        os._exit(77)
    engine.register = interrupted_register
if boundary == 'after-ack-commit':
    notify = store.notify
    def interrupted_notify():
        if not store.one('SELECT 1 FROM final_wake_intents WHERE message_id=?', (mid,)):
            os._exit(77)
        notify()
    store.notify = interrupted_notify
engine.from_final(cid, mid, text)
raise AssertionError('hard-exit boundary was not reached')
'''
    child = subprocess.run([sys.executable, "-c", script, str(svc.root), cid, runner.message_id,
                            text, str(now), boundary], capture_output=True, text=True, timeout=30)
    assert child.returncode == 77, child.stderr
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: now
    try:
        state = restarted.store.message(runner.message_id)["state"]
        assert state == ("running" if boundary == "before-completion-commit" else "complete")
        if state == "running":
            restarted._on_outcome(runner)
        for _ in range(3):
            restarted._replay_unsettled()
        restarted.wakes.now = lambda: now + 601
        for _ in range(3):
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
        print(f"R6 hard exit {boundary}: state={state}; wakes={wake_texts(restarted, cid)}")
        assert len(wake_rows(restarted, cid)) == 1
        assert "Hard-exit recovery" in wake_texts(restarted, cid)[0]
        assert restarted.store.query("SELECT * FROM final_wake_intents") == []
    finally:
        restarted.close()
