"""Round-seven reviewer probes for PR #127 (review artifacts, not for merge).

Restart crash matrix with two unresolved final intents and newer replacements,
plus the replay-order and replay-isolation probes behind the round-seven findings.
"""
import contextlib
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from subfleet.conversations import wakes
from subfleet.conversations.service import ConversationService
from tests.unit.test_conversation_service import EndedRunner, submit, svc  # noqa: F401
from tests.unit.test_review_r2_repro import bound, iso, settle_wakes, wake_rows, wake_texts
from tests.unit.test_review_r5_repro import SimulatedCrash
from tests.unit.test_review_r6_probes import finished_turn

REPO = Path(__file__).resolve().parents[2]


def record_final(svc, runner, text, settled_at):
    """Commit completion and its intent as `_settle_outcome` does, without the replay."""
    assert svc.store.set_state(runner.message_id, "complete", expect=("running",),
                               final_wake=(text, settled_at))


def finish_dispatched(svc, cid, mid, text):
    """`finished_turn` for a message the dispatcher has already given a job."""
    host = svc.store.message(mid)["job_id"]
    assert host, "message was not dispatched"
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


def complete_wakes(service, cid):
    for message in wake_rows(service, cid):
        if message["job_id"]:
            service.daemon.store.update_job(message["job_id"], state="succeeded")
    settle_wakes(service, cid)


OLDER = 'WAKE-ME: at={t900} note="Older final"'
NEWER = ('WAKE-ME: at={t1100} note="Newest superseded line"\n'
         'WAKE-ME: at={t1200} note="Newest final"')


def two_pending_intents(svc, now):
    """An old explicit timer, then two completed finals whose intents never replayed.

    The older final replaces the timer; the newer final replaces both, twice."""
    cid = bound(svc)
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(at=iso(now + 600), note="Old timer", now=now))
    older_text = OLDER.format(t900=iso(now + 900))
    older = finished_turn(svc, cid, older_text)
    record_final(svc, older, older_text, now + 10)
    newer_text = NEWER.format(t1100=iso(now + 1100), t1200=iso(now + 1200))
    newer = finished_turn(svc, cid, newer_text, after=older.message_id)
    record_final(svc, newer, newer_text, now + 20)
    assert len(svc.store.query("SELECT * FROM final_wake_intents")) == 2
    return cid, older.message_id, newer.message_id


BOUNDARIES = [
    "before-replay", "after-older-register", "before-older-ack-commit", "after-older-ack-commit",
    "after-newer-first-register", "after-newer-second-register", "before-newer-ack-commit",
    "after-newer-ack-commit", "no-crash",
]


def arm_crash(service, monkeypatch, boundary, older, newer, reached):
    """Raise SimulatedCrash (a BaseException: tick() cannot log it away) at one boundary."""
    def crash(*_args):
        reached.append(boundary)
        raise SimulatedCrash()

    if boundary == "before-replay":
        monkeypatch.setattr(service.wakes, "replay_final", crash)
    elif boundary in ("after-older-register", "after-newer-first-register", "after-newer-second-register"):
        register = service.wakes.register
        seen = []

        def interrupted(cid, request_id, spec, **kw):
            result = register(cid, request_id, spec, **kw)
            seen.append(request_id)
            mine = [r for r in seen if r.startswith(f"final:{newer if 'newer' in boundary else older}:")]
            if len(mine) == (2 if boundary == "after-newer-second-register" else 1):
                crash()
            return result

        monkeypatch.setattr(service.wakes, "register", interrupted)
    elif boundary in ("before-older-ack-commit", "before-newer-ack-commit"):
        target = older if "older" in boundary else newer
        transaction = service.store.transaction

        @contextlib.contextmanager
        def interrupted_transaction():
            with transaction() as tx:
                yield tx
                if not tx.execute("SELECT 1 FROM final_wake_intents WHERE message_id=?", (target,)).fetchone() and \
                        tx.execute("SELECT 1 FROM messages WHERE message_id=?", (target,)).fetchone():
                    crash()             # the DELETE ran; the transaction rolls back

        monkeypatch.setattr(service.store, "transaction", interrupted_transaction)
    elif boundary in ("after-older-ack-commit", "after-newer-ack-commit"):
        target = older if "older" in boundary else newer
        notify = service.store.notify
        fired = []

        def interrupted_notify():
            if not fired and not service.store.one("SELECT 1 FROM final_wake_intents WHERE message_id=?", (target,)):
                fired.append(True)
                crash()
            notify()

        monkeypatch.setattr(service.store, "notify", interrupted_notify)


def assert_one_newest_wake(service, cid, older, newer, clock, now):
    clock[0] = now + 950             # the old timer and the older final are overdue
    for _ in range(2):
        service.tick()
        complete_wakes(service, cid)
    early = wake_texts(service, cid)
    clock[0] = now + 1201
    for _ in range(3):
        service.tick()
        complete_wakes(service, cid)
    texts = wake_texts(service, cid)
    rows = service.store.query("SELECT request_id,kind,state FROM wake_requests WHERE conversation_id=? "
                               "ORDER BY rowid", (cid,))
    per_request = {}
    for row in rows:
        per_request[row["request_id"]] = per_request.get(row["request_id"], 0) + 1
    return early, texts, rows, per_request


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_restart_crash_matrix_with_two_pending_intents(svc, monkeypatch, boundary):
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    cid, older, newer = two_pending_intents(svc, now)
    svc.close()
    # First restart at +950: the old timer (+600) and the older final (+900) are overdue.
    clock[0] = now + 950
    crashed = ConversationService(svc.daemon)
    crashed.wakes.now = lambda: clock[0]
    reached = []
    with monkeypatch.context() as patch:
        patch.setattr(crashed, "_catalog_tick", lambda: None)
        arm_crash(crashed, patch, boundary, older, newer, reached)
        try:
            if boundary == "no-crash":
                crashed.tick()
            else:
                with pytest.raises(SimulatedCrash):
                    crashed.tick()
        finally:
            assert reached == ([] if boundary == "no-crash" else [boundary])
            crash_wakes = wake_texts(crashed, cid)
            crashed.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    monkeypatch.setattr(restarted, "_catalog_tick", lambda: None)
    try:
        early, texts, rows, per_request = assert_one_newest_wake(restarted, cid, older, newer, clock, now)
        print(f"R7 crash matrix {boundary}: at-crash={crash_wakes} early={early} final={texts} rows={rows}")
        assert crash_wakes == [], "a stale timer fired during the crashed restart"
        assert early == [], "a stale timer fired after the restart"
        assert len(texts) == 1 and "Newest final" in texts[0], texts
        for stale in ("Old timer", "Older final", "Newest superseded line"):
            assert stale not in texts[0]
        assert restarted.store.query("SELECT * FROM final_wake_intents") == []
        finals = {r: n for r, n in per_request.items() if r.startswith("final:")}
        assert len(finals) == 3 and set(finals.values()) == {1}, per_request  # each line registered once
        assert [r["state"] for r in rows] == ["superseded", "superseded", "superseded", "fired"], rows
    finally:
        restarted.close()


HARD_EXIT = r'''
import contextlib, os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from tests.unit.test_conversation_service import FakeDaemon
from subfleet.conversations.service import ConversationService
root, older, newer, boundary, at = sys.argv[2:]
service = ConversationService(FakeDaemon(Path(root)))
service.wakes.now = lambda: float(at)
service._catalog_tick = lambda: None
def die(*_args):
    os._exit(77)
if boundary == "before-replay":
    service.wakes.replay_final = die
elif "register" in boundary:
    register = service.wakes.register
    seen = []
    def interrupted(cid, request_id, spec, **kw):
        result = register(cid, request_id, spec, **kw)
        seen.append(request_id)
        mine = [r for r in seen if r.startswith("final:%s:" % (newer if "newer" in boundary else older))]
        if len(mine) == (2 if boundary == "after-newer-second-register" else 1):
            die()
        return result
    service.wakes.register = interrupted
elif boundary.startswith("before-") and boundary.endswith("-ack-commit"):
    target = older if "older" in boundary else newer
    transaction = service.store.transaction
    class Tx:
        def __init__(self, tx): self.tx = tx
        def __getattr__(self, name): return getattr(self.tx, name)
        def execute(self, sql, params=()):
            result = self.tx.execute(sql, params)
            if sql.startswith("DELETE FROM final_wake_intents") and params == (target,):
                die()           # deleted inside the open transaction, never committed
            return result
    @contextlib.contextmanager
    def interrupted_transaction():
        with transaction() as tx:
            yield Tx(tx)
    service.store.transaction = interrupted_transaction
elif boundary.endswith("-ack-commit"):
    target = older if "older" in boundary else newer
    notify = service.store.notify
    def interrupted_notify():
        if not service.store.one("SELECT 1 FROM final_wake_intents WHERE message_id=?", (target,)):
            die()
        notify()
    service.store.notify = interrupted_notify
elif boundary == "before-evaluation":
    service.wakes.control_tick = die
service.tick()
raise SystemExit("hard-exit boundary was not reached")
'''


@pytest.mark.parametrize("boundary", [b for b in BOUNDARIES if b != "no-crash"] + ["before-evaluation"])
def test_restart_hard_exit_matrix_with_two_pending_intents(svc, monkeypatch, boundary):
    """The same restart, in a foreground child that exits with no unwinding or close."""
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    cid, older, newer = two_pending_intents(svc, now)
    svc.close()
    child = subprocess.run([sys.executable, "-c", HARD_EXIT, str(REPO), str(svc.root), older, newer, boundary,
                            str(now + 950)], capture_output=True, text=True, timeout=60)
    assert child.returncode == 77, child.stdout + child.stderr
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    monkeypatch.setattr(restarted, "_catalog_tick", lambda: None)
    try:
        early, texts, rows, per_request = assert_one_newest_wake(restarted, cid, older, newer, clock, now)
        print(f"R7 hard exit {boundary}: early={early} final={texts} rows={rows}")
        assert early == []
        assert len(texts) == 1 and "Newest final" in texts[0], texts
        assert restarted.store.query("SELECT * FROM final_wake_intents") == []
        finals = {r: n for r, n in per_request.items() if r.startswith("final:")}
        assert len(finals) == 3 and set(finals.values()) == {1}, per_request
        assert [r["state"] for r in rows] == ["superseded", "superseded", "superseded", "fired"], rows
    finally:
        restarted.close()


class TransientReplayFailure:
    """`register` raising SQLite's busy error for chosen conversations while on."""

    def __init__(self, service, conversations):
        self.service, self.conversations, self.on, self.raised = service, set(conversations), True, 0
        self.register = service.wakes.register

    def __call__(self, cid, *args, **kwargs):
        if self.on and cid in self.conversations:
            self.raised += 1
            raise sqlite3.OperationalError("database is locked")
        return self.register(cid, *args, **kwargs)


@pytest.mark.parametrize("failing", [False, True], ids=["control", "replay-fails-during-overtaking-turn"])
def test_intents_replay_in_message_order_not_completion_order(svc, monkeypatch, failing):
    """A yielded wake (lower seq) completes after the person turn that overtook it."""
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    monkeypatch.setattr(svc, "_catalog_tick", lambda: None)
    # Not exactly +300 s: iso() rounds to microseconds and can land under the floor.
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(at=iso(now + 310), note="First check", now=now))
    clock[0] = now + 311
    svc.wakes.tick(poll=False)
    [woken] = wake_rows(svc, cid)
    assert woken["state"] == "queued"
    person = submit(svc, cid, "Person instructions")         # arrives before the wake dispatches
    svc._yield_wakes(cid)
    svc._dispatch()
    assert svc.store.message(person)["job_id"] and not svc.store.message(woken["message_id"])["job_id"]
    assert svc.store.message(person)["seq"] > woken["seq"]
    failure = TransientReplayFailure(svc, [cid])
    if failing:
        monkeypatch.setattr(svc.wakes, "register", failure)
    first = finish_dispatched(svc, cid, person, f'WAKE-ME: at={iso(now + 900)} note="Person turn: older re-arm"')
    clock[0] = now + 320
    try:
        svc._on_outcome(first)
    except sqlite3.OperationalError:
        assert failing
    svc.tick()                                                # replay fails again; the wake still dispatches
    assert svc.store.message(woken["message_id"])["job_id"]
    second = finish_dispatched(svc, cid, woken["message_id"],
                               f'WAKE-ME: at={iso(now + 1200)} note="Wake turn: newest re-arm"')
    failure.on = False                                        # the transient failure has cleared
    clock[0] = now + 340
    svc._on_outcome(second)
    replayed = svc.store.query("SELECT payload_json,state FROM wake_requests WHERE conversation_id=? "
                               "AND kind='time' ORDER BY rowid", (cid,))
    clock[0] = now + 901
    for _ in range(2):
        svc.tick()
        complete_wakes(svc, cid)
    early = wake_texts(svc, cid)[1:]
    clock[0] = now + 1201
    for _ in range(2):
        svc.tick()
        complete_wakes(svc, cid)
    texts = wake_texts(svc, cid)[1:]
    print(f"R7 completion order failing={failing}: raised={failure.raised} requests={replayed} "
          f"early={early} final={texts}")
    assert svc.store.query("SELECT * FROM final_wake_intents") == []
    assert early == [], "the older person turn's re-arm replayed last and fired"
    assert len(texts) == 1 and "Wake turn: newest re-arm" in texts[0], texts


@pytest.mark.parametrize("failing", [False, True], ids=["control", "earlier-conversation-replay-fails"])
def test_one_failing_replay_does_not_fence_other_conversations(svc, monkeypatch, failing):
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    monkeypatch.setattr(svc, "_catalog_tick", lambda: None)
    first, second = sorted([bound(svc), bound(svc)])          # replay_final orders by conversation id
    for cid in (first, second):
        svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(at=iso(now + 600), note="Old timer", now=now))
        text = f'WAKE-ME: at={iso(now + 1200)} note="Replacement {cid[:8]}"'
        record_final(svc, finished_turn(svc, cid, text), text, now + 10)
    failure = TransientReplayFailure(svc, [first])
    failure.on = failing
    monkeypatch.setattr(svc.wakes, "register", failure)
    clock[0] = now + 1201
    for _ in range(3):
        svc.tick()
        complete_wakes(svc, first)
        complete_wakes(svc, second)
    unaffected = wake_texts(svc, second)
    left = [r["message_id"] for r in svc.store.query("SELECT message_id FROM final_wake_intents")]
    print(f"R7 isolation failing={failing}: raised={failure.raised} second={unaffected} intents-left={len(left)}")
    assert len(unaffected) == 1 and f"Replacement {second[:8]}" in unaffected[0], \
        "a replay failure in another conversation fenced this conversation's due replacement"


@pytest.mark.parametrize("failing", [False, True], ids=["control", "other-conversation-replay-fails"])
def test_failing_final_replay_blocks_unrelated_recovery_and_explicit_wakes(svc, monkeypatch, failing):
    """`_replay_unsettled` and `conversation.wake` replay every conversation's intents first."""
    now = time.time()
    svc.wakes.now = lambda: now
    monkeypatch.setattr(svc, "_catalog_tick", lambda: None)
    broken = bound(svc)
    text = f'WAKE-ME: at={iso(now + 1200)} note="Broken replay"'
    record_final(svc, finished_turn(svc, broken, text), text, now)
    stranded = bound(svc)
    runner = finished_turn(svc, stranded, "Turn ended; its runner stopped with the daemon")
    idle = bound(svc)
    failure = TransientReplayFailure(svc, [broken])
    failure.on = failing
    monkeypatch.setattr(svc.wakes, "register", failure)
    adopted = []
    monkeypatch.setattr(svc, "_adopt",
                        lambda attempt, ended=False: adopted.append((attempt["attempt_id"], ended)) or True)
    svc.tick()
    try:
        receipt, error = svc.op_conversation_wake({
            "session_id": svc.store.conversation(idle)["native_session_id"], "request_id": str(uuid.uuid4()),
            "at": iso(now + 900), "note": "Explicit re-arm"}, None), None
    except Exception as exc:
        receipt, error = None, f"{type(exc).__name__}: {exc}"
    print(f"R7 blast radius failing={failing}: raised={failure.raised} adopted={adopted} "
          f"stranded-state={svc.store.message(runner.message_id)['state']} wake-op={receipt or error}")
    assert adopted == [(runner.attempt_id, True)], "an ended turn in another conversation was not replayed"
    assert error is None and receipt["kinds"] == ["time"], error


@pytest.mark.parametrize("hold", ["blocked", "throttled", "legacy-hold"])
def test_pending_intent_replays_while_held_and_fires_once_after(svc, monkeypatch, hold):
    """A replacement recorded before a crash replays while the conversation is held,
    then fires exactly once, and the superseded old timer never does."""
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    cid = bound(svc)
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(at=iso(now + 600), note="Old timer", now=now))
    text = f'WAKE-ME: at={iso(now + 1200)} note="Replacement"'
    record_final(svc, finished_turn(svc, cid, text), text, now + 10)     # crash before replay
    if hold == "blocked":
        svc.store.update_conversation(cid, blocked_by="delivery-unknown")
    elif hold == "legacy-hold":
        svc.store.set_legacy_hold(cid, "legacy-writer")
    else:
        with svc.store.transaction() as tx:
            tx.execute("UPDATE conversations SET wake_streak=?,last_wake_at=? WHERE conversation_id=?",
                       (wakes.MAX_STREAK, now + 1500 - wakes.COOLDOWN_S, cid))
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    monkeypatch.setattr(restarted, "_catalog_tick", lambda: None)
    try:
        seen = []
        for at in (601, 1201):
            clock[0] = now + at
            for _ in range(2):
                restarted.tick()
                complete_wakes(restarted, cid)
            seen.append(len(wake_rows(restarted, cid)))
        intents = len(restarted.store.query("SELECT * FROM final_wake_intents"))
        if hold == "blocked":
            restarted.store.update_conversation(cid, blocked_by=None)
        elif hold == "legacy-hold":
            restarted.store.set_legacy_hold(cid, None)
        clock[0] = now + 1501
        for _ in range(3):
            restarted.tick()
            complete_wakes(restarted, cid)
        texts = wake_texts(restarted, cid)
        print(f"R7 held {hold}: wakes-while-held={seen} intents-while-held={intents} final={texts}")
        assert seen == [0, 0] and intents == 0
        assert len(texts) == 1 and "Replacement" in texts[0] and "Old timer" not in texts[0], texts
    finally:
        restarted.close()
