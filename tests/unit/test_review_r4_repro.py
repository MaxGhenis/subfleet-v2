"""PR #127 round-four regressions and wake delivery controls."""
import json
import uuid

import pytest

from subfleet.conversations import wakes
from subfleet.conversations.store import ConversationError, ConversationStore
from tests.unit.test_conversation_service import submit, svc  # noqa: F401
from tests.unit.test_review_r2_repro import (
    T0, bound, calls_of, fake_gh, iso, pr_node, run_under, settle_wakes,
    turn_job, wake_rows, wake_texts,
)
from tests.unit.test_review_r3_repro import poll_now


def arm(svc, cid, request_id=None, **kwargs):
    return svc.wakes.register(cid, request_id or str(uuid.uuid4()),
                              wakes.normalize(now=svc.wakes.now(), **kwargs))


def poll_and_settle(svc, cid, clock, n=6):
    for _ in range(n):
        clock[0] += 61
        poll_now(svc)
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)


@pytest.mark.parametrize("in_flight", [False, True], ids=["before-first-poll", "during-first-poll"])
def test_unobserved_event_survives_rearm(svc, tmp_path, monkeypatch, in_flight):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    seed = bound(svc)
    calls = fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(
        checks=(("IN_PROGRESS", None, None),))}})
    arm(svc, seed, prs=["o/r#1"])
    svc.wakes.control_tick()
    svc.wakes._poll_future.result(timeout=10)  # another conversation used this minute's gh allowance
    clock[0] = T0 + 1
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(
        checks=(("COMPLETED", "FAILURE", iso(T0 + 20)),))}})
    if in_flight:
        original = wakes.query_prs

        def interleaved(targets):
            snapshots = original(targets)  # actual gh subprocess queried the old watch
            clock[0] = T0 + 62
            arm(svc, cid, prs=["o/r#1"])
            return snapshots

        monkeypatch.setattr(wakes, "query_prs", interleaved)
        clock[0] = T0 + 61
        svc.wakes.control_tick()  # the real PR executor reads the superseded request
        svc.wakes._poll_future.result(timeout=10)
        monkeypatch.setattr(wakes, "query_prs", original)
    else:
        clock[0] = T0 + 31
        arm(svc, cid, prs=["o/r#1"])
    poll_and_settle(svc, cid, clock)
    rows = svc.store.query("SELECT state,created_at,ready_json FROM wake_requests WHERE conversation_id=?", (cid,))
    print(f"R4 unobserved in_flight={in_flight}: gh={calls_of(calls)} wakes={wake_texts(svc,cid)} rows={rows}")
    assert len(wake_rows(svc, cid)) == 1, "CI failed during an armed watch but its first poll/rearm lost it"


@pytest.mark.parametrize("legacy", [False, True], ids=["persisted-window", "pre-upgrade-watch"])
def test_unobserved_window_survives_restart_and_multiple_rearms(svc, tmp_path, monkeypatch, legacy):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1"])
    if legacy:
        with svc.store.transaction() as tx:
            tx.execute("DROP TABLE IF EXISTS wake_pr_windows")
    svc.wakes.close()
    svc.store.close()
    svc.store = ConversationStore(svc.root)
    svc.wakes = wakes.WakeEngine(svc)
    svc.wakes.now = lambda: clock[0]
    for offset in (31, 40, 50):
        clock[0] = T0 + offset
        arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(
        checks=(("COMPLETED", "FAILURE", iso(T0 + 20)),))}})
    poll_and_settle(svc, cid, clock)
    assert len(wake_rows(svc, cid)) == 1
    assert "PR state changed: o/r#1" in wake_texts(svc, cid)[0]


def test_retained_target_window_does_not_extend_to_new_targets(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1"])
    clock[0] = T0 + 31
    arm(svc, cid, prs=["o/r#1", "o/r#2"])
    fake_gh(tmp_path, monkeypatch, {"data": {f"p{i}": pr_node(
        checks=(("COMPLETED", "FAILURE", iso(T0 + 20)),)) for i in range(2)}})
    poll_and_settle(svc, cid, clock)
    assert len(wake_rows(svc, cid)) == 1
    assert "PR state changed: o/r#1" in wake_texts(svc, cid)[0]
    assert "o/r#2" not in wake_texts(svc, cid)[0]


def test_removed_target_readded_starts_a_new_window(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    for offset, targets in ((0, ["o/r#1"]), (31, ["o/r#2"]), (62, ["o/r#1", "o/r#2"])):
        clock[0] = T0 + offset
        arm(svc, cid, prs=targets)
    fake_gh(tmp_path, monkeypatch, {"data": {
        "p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(T0 + 20)),)),
        "p1": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    poll_and_settle(svc, cid, clock)
    assert wake_rows(svc, cid) == []


@pytest.mark.parametrize("event", ["merged", "closed", "checks", "review"])
def test_event_after_refusal_is_delivered_when_access_returns(svc, tmp_path, monkeypatch, event):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1"])
    calls = fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}}})
    clock[0] += 61
    svc.wakes.tick()
    assert len(wake_rows(svc, cid)) == 1 and "refused" in wake_texts(svc, cid)[0]
    settle_wakes(svc, cid)
    clock[0] = T0 + 70
    arm(svc, cid, prs=["o/r#1"])
    clock[0] = T0 + 122
    poll_now(svc)  # still inaccessible; the persisted refusal suppresses a second notification
    assert len(wake_rows(svc, cid)) == 1
    kwargs = {"state": "MERGED", "merged_at": iso(T0 + 150)} if event == "merged" else (
        {"state": "CLOSED", "closed_at": iso(T0 + 150)} if event == "closed" else (
        {"checks": (("COMPLETED", "FAILURE", iso(T0 + 150)),)} if event == "checks" else
        {"reviews": (("review-new", iso(T0 + 150)),), "checks": (("IN_PROGRESS", None, None),)}))
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(**kwargs)}})
    poll_and_settle(svc, cid, clock)
    refusals = svc.store.query("SELECT * FROM wake_pr_refusals WHERE conversation_id=?", (cid,))
    print(f"R4 resolved {event}: gh={calls_of(calls)} wakes={wake_texts(svc,cid)} refusals={refusals}")
    assert refusals == []
    assert len(wake_rows(svc, cid)) == 2, "a real event newer than registration vanished at the first good read"


def test_refusal_recovery_keeps_event_window_through_a_later_rearm(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}}})
    clock[0] = T0 + 61
    svc.wakes.tick()
    settle_wakes(svc, cid)
    clock[0] = T0 + 70
    arm(svc, cid, prs=["o/r#1"])
    clock[0] = T0 + 122
    poll_now(svc)
    clock[0] = T0 + 170
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(state="MERGED", merged_at=iso(T0 + 150))}})
    poll_and_settle(svc, cid, clock)
    assert len(wake_rows(svc, cid)) == 2
    assert "PR state changed: o/r#1" in wake_texts(svc, cid)[1]


def test_unannounced_refusal_survives_rearm(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    job = turn_job(svc, cid, 1, state="running")
    arm(svc, cid, prs=["o/r#1"])
    calls = fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}}})
    clock[0] = T0 + 61
    svc.wakes.tick()
    assert wake_rows(svc, cid) == []
    ready = svc.store.one("SELECT ready_json FROM wake_requests WHERE state='pending'")
    assert ready["ready_json"] == '["o/r#1"]'
    clock[0] = T0 + 70
    svc.op_conversation_wake({"calling_job": job, "request_id": str(uuid.uuid4()), "prs": ["o/r#1"]}, None)
    svc.daemon.store.update_job(job, state="succeeded")
    poll_and_settle(svc, cid, clock)
    print(f"R4 undelivered refusal: gh={calls_of(calls)} wakes={wake_texts(svc,cid)} "
          f"refusals={svc.store.query('SELECT * FROM wake_pr_refusals')}")
    assert len(wake_rows(svc, cid)) == 1, "an observed refusal was suppressed even though nobody received it"


def test_claim_deferral_converges_on_the_next_tick(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    parent = turn_job(svc, cid, 1)
    run_under(svc, cid, parent, "run", state="running")
    arm(svc, cid, runs=["run"], prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    clock[0] += 61
    poll_now(svc)
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(T0 + 100)),))}})
    svc.daemon.store.update_job("run", state="succeeded")
    clock[0] = T0 + 125
    original = svc.store.submit_message
    deferred = []

    def interleaved(**kwargs):
        if not deferred:
            poll_now(svc)
        try:
            return original(**kwargs)
        except ConversationError as exc:
            deferred.append(str(exc))
            raise

    monkeypatch.setattr(svc.store, "submit_message", interleaved)
    svc.wakes.tick(poll=False)
    assert wake_rows(svc, cid) == [] and len(deferred) == 1
    poll_and_settle(svc, cid, clock)
    print(f"R4 deferral: deferred={deferred} wakes={wake_texts(svc,cid)}")
    assert len(deferred) == 1 and len(wake_rows(svc, cid)) == 1
    assert "run finished" in wake_texts(svc, cid)[0] and "PR state changed" in wake_texts(svc, cid)[0]


def test_carried_ready_survives_multiple_rearms_and_fires_once(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    svc.store.update_conversation(cid, blocked_by="live-turn")
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(state="MERGED", merged_at=iso(T0 + 30))}})
    clock[0] = T0 + 61
    svc.wakes.tick()
    for n in range(5):
        clock[0] += 1
        arm(svc, cid, prs=["o/r#1"])
    svc.store.update_conversation(cid, blocked_by=None)
    svc.wakes.tick(poll=False)
    settle_wakes(svc, cid)
    for _ in range(5):
        arm(svc, cid, prs=["o/r#1"])
        poll_and_settle(svc, cid, clock, n=2)
    print(f"R4 carried: wakes={wake_texts(svc,cid)}")
    assert len(wake_rows(svc, cid)) == 1


def test_resolved_old_event_does_not_spuriously_wake(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}}})
    clock[0] += 61
    svc.wakes.tick()
    settle_wakes(svc, cid)
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(state="MERGED", merged_at=iso(T0 - 3600))}})
    poll_and_settle(svc, cid, clock)
    print(f"R4 old resolution: wakes={wake_texts(svc,cid)}")
    assert len(wake_rows(svc, cid)) == 1


@pytest.mark.parametrize("written,settled,minutes", [(5, 40, 5), (5, 125, 6), (30, 70, 5)])
def test_timer_floor_and_delivery_at_precise_turn_start(svc, written, settled, minutes):
    cid = bound(svc)
    mid = submit(svc, cid)
    job = turn_job(svc, cid, 1)
    with svc.daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (iso(T0), job))
    with svc.store.transaction() as tx:
        tx.execute("UPDATE messages SET state='complete',job_id=? WHERE message_id=?", (job, mid))
    clock = [T0 + settled]
    svc.wakes.now = lambda: clock[0]
    due = T0 + written + minutes * 60
    svc.wakes.from_final(cid, mid, f"WAKE-ME: at={iso(due)}")
    assert svc.store.one("SELECT kind FROM wake_requests")["kind"] == "time"
    svc.wakes.tick(poll=False)
    assert wake_rows(svc, cid) == []
    clock[0] = due
    svc.wakes.tick(poll=False)
    settle_wakes(svc, cid)
    svc.wakes.from_final(cid, mid, f"WAKE-ME: at={iso(due)}")
    svc.wakes.tick(poll=False)
    assert len(wake_rows(svc, cid)) == 1


@pytest.mark.parametrize("seconds_later", [120, 295, 360])
def test_old_text_resubmitted_in_later_turn_obeys_new_floor(svc, seconds_later):
    cid = bound(svc)
    text = f"WAKE-ME: at={iso(T0 + 360)}"
    mid = submit(svc, cid)
    job = turn_job(svc, cid, 1)
    with svc.daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (iso(T0 + seconds_later), job))
    with svc.store.transaction() as tx:
        tx.execute("UPDATE messages SET state='complete',job_id=? WHERE message_id=?", (job, mid))
    svc.wakes.now = lambda: T0 + seconds_later + 1
    svc.wakes.from_final(cid, mid, text)
    print(f"R4 resubmit start=+{seconds_later}: requests={svc.store.query('SELECT * FROM wake_requests')}")
    assert svc.store.query("SELECT * FROM wake_requests") == []


def test_one_bad_target_does_not_silence_valid_target_after_rearm(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1", "o/r#2"])
    fake_gh(tmp_path, monkeypatch, {"data": {
        "p0": pr_node(checks=(("IN_PROGRESS", None, None),)), "p1": {"pullRequest": None}}})
    clock[0] += 61
    svc.wakes.tick()
    assert len(wake_rows(svc, cid)) == 1
    settle_wakes(svc, cid)
    arm(svc, cid, prs=["o/r#1", "o/r#2"], at=iso(T0 + 600))
    fake_gh(tmp_path, monkeypatch, {"data": {
        "p0": pr_node(state="MERGED", merged_at=iso(T0 + 100)), "p1": {"pullRequest": None}}})
    poll_and_settle(svc, cid, clock)
    print(f"R4 mixed: wakes={wake_texts(svc,cid)}")
    assert len(wake_rows(svc, cid)) == 2 and "PR state changed: o/r#1" in wake_texts(svc, cid)[1]


def test_poll_calls_are_separated_by_a_minute_with_many_rearms(svc, tmp_path, monkeypatch):
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    cids = [bound(svc) for _ in range(40)]
    calls = fake_gh(tmp_path, monkeypatch, {"data": {
        f"p{i}": pr_node(checks=(("IN_PROGRESS", None, None),)) for i in range(40)}})
    for i, cid in enumerate(cids):
        arm(svc, cid, prs=[f"o/r#{i + 1}"])
    original = wakes.query_prs
    stamps = []

    def timed(targets):
        stamps.append(clock[0])
        return original(targets)

    monkeypatch.setattr(wakes, "query_prs", timed)
    for n in range(6000):
        clock[0] = T0 + n / 20
        if n % 20 == 0:
            k = (n // 20) % 40
            arm(svc, cids[k], prs=[f"o/r#{k + 1}"])
        svc.wakes.control_tick()
        if svc.wakes._poll_future is not None:
            svc.wakes._poll_future.result(timeout=10)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    print(f"R4 minute cap: gh={calls_of(calls)} at={[t-T0 for t in stamps]}, gaps={gaps}")
    assert calls_of(calls) == len(stamps) == 5
    assert min(gaps) >= 60


def test_one_bad_target_does_not_silence_timer_after_rearm(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    fake_gh(tmp_path, monkeypatch, {"data": {
        "p0": pr_node(checks=(("IN_PROGRESS", None, None),)), "p1": {"pullRequest": None}}})
    arm(svc, cid, prs=["o/r#1", "o/r#2"])
    clock[0] = T0 + 61
    svc.wakes.tick()
    settle_wakes(svc, cid)
    arm(svc, cid, prs=["o/r#1", "o/r#2"], at=iso(T0 + 600))
    poll_and_settle(svc, cid, clock, n=10)
    print(f"R4 mixed timer: wakes={wake_texts(svc,cid)}")
    assert len(wake_rows(svc, cid)) == 2 and "Scheduled check-back is due" in wake_texts(svc, cid)[1]


def test_rearm_between_ready_read_and_claim_fires_only_the_replacement(svc, tmp_path, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(state="MERGED", merged_at=iso(T0 + 30))}})
    clock[0] = T0 + 61
    poll_now(svc)
    original = svc.store.submit_message
    replacement = str(uuid.uuid4())
    attempts = []

    def interleaved(**kwargs):
        if not attempts:
            arm(svc, cid, request_id=replacement, prs=["o/r#1"])
        attempts.append(kwargs["wake_claim"]["requests"])
        return original(**kwargs)

    monkeypatch.setattr(svc.store, "submit_message", interleaved)
    svc.wakes.tick(poll=False)
    assert wake_rows(svc, cid) == []
    poll_and_settle(svc, cid, clock)
    rows = svc.store.query("SELECT request_id,state,message_id FROM wake_requests WHERE conversation_id=?", (cid,))
    print(f"R4 carried claim race: attempts={attempts} wakes={wake_texts(svc,cid)} requests={rows}")
    assert len(wake_rows(svc, cid)) == 1
    assert [r["request_id"] for r in rows if r["state"] == "fired"] == [replacement]
