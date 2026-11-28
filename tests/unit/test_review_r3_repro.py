"""Review r3 of PR #127 (not for merge). Scenarios against 187b2150, each asserting the
behaviour a sound build must have. Helpers come from the round-two repro file."""
import json
import threading
import time
import uuid

import pytest

from subfleet.conversations import wakes
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import submit, svc  # noqa: F401
from tests.unit.test_review_r2_repro import (T0, bound, calls_of, fake_gh, iso, pr_node, run_under, settle_wakes,
                                             turn_job, wake_rows, wake_texts)


def start_turn(svc, cid, mid, n, at):
    """The wake message `mid` becomes a running turn whose job was created at `at`."""
    job = turn_job(svc, cid, n, state="running")
    with svc.daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (iso(at), job))
    with svc.store.transaction() as tx:
        tx.execute("UPDATE messages SET job_id=?,state='running' WHERE message_id=?", (job, mid))
    return job


def end_turn(svc, mid, job):
    svc.daemon.store.update_job(job, state="succeeded")
    with svc.store.transaction() as tx:
        tx.execute("UPDATE messages SET state='complete' WHERE message_id=?", (mid,))


def poll_now(svc):
    """What the control loop's PR worker does once a minute (gh runs for real)."""
    pending = svc.store.query("SELECT * FROM wake_requests WHERE state='pending' AND kind='pr' AND ready_json IS NULL")
    svc.wakes._poll_prs(pending)


# --- A: a PR watch's undelivered event is dropped when the next turn re-arms it ----------------------

@pytest.mark.parametrize("polled_during_turn", [True, False], ids=["ready-then-superseded", "unpolled-then-superseded"])
def test_r3_ci_result_landing_just_before_an_automatic_wake_survives_the_rearm(svc, tmp_path, monkeypatch,
                                                                               polled_during_turn):
    """Turn 1 dispatched a review run and armed `WAKE-ME: prs=o/r#1`. CI fails 10 s before the
    review run finishes. The run's automatic wake starts turn 2 before the next minute's PR
    poll. Turn 2 reads the review and re-lists its PR watch, as agents do. The CI failure
    happened, was never announced, and must still wake the conversation."""
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t1 = turn_job(svc, cid, 1)
    run_under(svc, cid, t1, "review-run", state="running")
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    svc.wakes.from_final(cid, "turn-1", "Dispatched a review; waiting for CI.\nWAKE-ME: prs=o/r#1")
    clock[0] = T0 + 61
    poll_now(svc)                                              # baseline: CI running
    # CI fails at +100; the review finishes at +110; the next PR poll would be at +121.
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(T0 + 100)),))}})
    svc.daemon.store.update_job("review-run", state="succeeded")
    clock[0] = T0 + 111
    svc.wakes.tick(poll=False)                                 # the paced completion scan
    assert len(wake_rows(svc, cid)) == 1 and "review-run finished" in wake_texts(svc, cid)[0]
    mid2 = wake_rows(svc, cid)[0]["message_id"]
    job2 = start_turn(svc, cid, mid2, 2, at=T0 + 112)
    if polled_during_turn:
        clock[0] = T0 + 121
        poll_now(svc)                                          # sees the failure; the turn is live, so it waits
        svc.wakes.tick(poll=False)
        assert len(wake_rows(svc, cid)) == 1
        ready = svc.store.one("SELECT ready_json FROM wake_requests WHERE conversation_id=? AND state='pending'", (cid,))
        assert ready and ready["ready_json"]
    clock[0] = T0 + 118 if not polled_during_turn else T0 + 400
    end_turn(svc, mid2, job2)
    svc.wakes.from_final(cid, mid2, "The review is in; CI should be done soon.\nWAKE-ME: prs=o/r#1")
    for _ in range(20):                                        # 20 polls; nothing else changes
        clock[0] += 61
        poll_now(svc)
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)
    texts = wake_texts(svc, cid)
    rows = svc.store.query("SELECT kind,state,ready_json,created_at FROM wake_requests WHERE conversation_id=? "
                           "ORDER BY rowid", (cid,))
    print(f"R3 A ({'polled' if polled_during_turn else 'unpolled'}): wakes={len(texts)} requests={rows}")
    assert len(texts) == 2 and "PR state changed: o/r#1" in texts[1], "the CI failure reached nobody"


def test_r3_control_ci_result_after_the_woken_turn_began_is_announced(svc, tmp_path, monkeypatch):
    """Control for A: the same history with CI failing 3 s after turn 2 began wakes once more."""
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t1 = turn_job(svc, cid, 1)
    run_under(svc, cid, t1, "review-run", state="running")
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    svc.wakes.from_final(cid, "turn-1", "WAKE-ME: prs=o/r#1")
    clock[0] = T0 + 61
    poll_now(svc)
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(T0 + 115)),))}})
    svc.daemon.store.update_job("review-run", state="succeeded")
    clock[0] = T0 + 111
    svc.wakes.tick(poll=False)
    mid2 = wake_rows(svc, cid)[0]["message_id"]
    job2 = start_turn(svc, cid, mid2, 2, at=T0 + 112)
    clock[0] = T0 + 121
    poll_now(svc)
    clock[0] = T0 + 400
    end_turn(svc, mid2, job2)
    svc.wakes.from_final(cid, mid2, "WAKE-ME: prs=o/r#1")
    for _ in range(10):
        clock[0] += 61
        poll_now(svc)
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)
    print(f"R3 A control: wakes={len(wake_texts(svc, cid))}")
    assert len(wake_texts(svc, cid)) == 2


def test_r3_cli_rearm_mid_turn_drops_a_ready_pr_event(svc, tmp_path, monkeypatch):
    """`subfleet wake --pr` during a turn re-arms with the call time as its threshold. If the
    pending watch had already seen CI finish during this turn, that event is superseded with
    no baseline and never announced."""
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t1 = turn_job(svc, cid, 1)
    run_under(svc, cid, t1, "build-run", state="running")
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    svc.wakes.from_final(cid, "turn-1", "WAKE-ME: prs=o/r#1")
    clock[0] = T0 + 61
    poll_now(svc)                                              # baseline: CI running
    svc.daemon.store.update_job("build-run", state="succeeded")
    clock[0] = T0 + 100
    svc.wakes.tick(poll=False)                                 # automatic wake for build-run
    mid2 = wake_rows(svc, cid)[0]["message_id"]
    job2 = start_turn(svc, cid, mid2, 2, at=T0 + 101)
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(T0 + 160)),))}})
    clock[0] = T0 + 170
    poll_now(svc)                                              # CI failed mid-turn: ready, deferred
    clock[0] = T0 + 400                                        # the agent, still busy, re-arms by CLI
    svc.op_conversation_wake({"calling_job": job2, "request_id": str(uuid.uuid4()), "prs": ["o/r#1"],
                              "note": "check CI"}, None)
    clock[0] = T0 + 420
    end_turn(svc, mid2, job2)                                  # no WAKE-ME line: the CLI call armed it
    for _ in range(20):                                        # 20 polls; nothing else changes
        clock[0] += 61
        poll_now(svc)
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)
    texts = wake_texts(svc, cid)
    rows = svc.store.query("SELECT kind,state,ready_json,created_at FROM wake_requests WHERE conversation_id=? "
                           "ORDER BY rowid", (cid,))
    print(f"R3 B (CLI re-arm): wakes={len(texts)} requests={rows}")
    assert len(texts) == 2 and "PR state changed: o/r#1" in texts[1], "the mid-turn CI failure reached nobody"


# --- C: the PR worker's update lands between the tick's read and the claim ---------------------------

def test_r3_pr_event_racing_a_claim_is_not_absorbed_into_the_baseline(svc, tmp_path, monkeypatch):
    """`WAKE-ME: runs=R prs=o/r#1`. R finishes; while the tick builds the run's wake, the PR
    worker (its own thread) records CI's failure on the still-pending PR kind. The claim
    fires every kind of the request, the message names only R, and the failure becomes the
    baseline for the re-arm, so it is never announced."""
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t1 = turn_job(svc, cid, 1)
    run_under(svc, cid, t1, "review-run", state="running")
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    svc.wakes.from_final(cid, "turn-1", "WAKE-ME: runs=review-run prs=o/r#1")
    clock[0] = T0 + 61
    poll_now(svc)
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("COMPLETED", "FAILURE", iso(T0 + 120)),))}})
    svc.daemon.store.update_job("review-run", state="succeeded")
    clock[0] = T0 + 125
    original = svc.store.submit_message

    def interleaved(**kw):                                     # the worker's commit wins the race
        worker = threading.Thread(target=poll_now, args=(svc,))
        worker.start()
        worker.join()
        return original(**kw)
    monkeypatch.setattr(svc.store, "submit_message", interleaved)
    svc.wakes.tick(poll=False)
    monkeypatch.setattr(svc.store, "submit_message", original)
    first = wake_texts(svc, cid)
    settle_wakes(svc, cid)
    svc.wakes.from_final(cid, "turn-2", "Review read; waiting on CI.\nWAKE-ME: prs=o/r#1")
    for _ in range(20):
        clock[0] += 61
        poll_now(svc)
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)
    texts = wake_texts(svc, cid)
    print(f"R3 C (race): first={first} wakes={len(texts)}")
    assert any("o/r#1" in t for t in texts), "CI's failure was absorbed into the baseline and never announced"


# --- D: refusal persistence ---------------------------------------------------------------------------

REFUSED = {"data": {"p0": {"pullRequest": pr_node()["pullRequest"]}, "p1": {"pullRequest": None}},
           "errors": [{"type": "NOT_FOUND", "path": ["p1", "pullRequest"],
                       "message": "Could not resolve to a PullRequest with the number of 2."}]}


def test_r3_a_refused_pr_on_a_copied_line_takes_the_valid_watch_and_timer_with_it(svc, tmp_path, monkeypatch):
    """#2 was refused once. The woken agent copies its line, now also asking for a timer. The
    whole line is refused, so #1's merge and the timer reach nobody."""
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    fake_gh(tmp_path, monkeypatch, REFUSED, rc=1)
    svc.wakes.from_final(cid, "turn-1", "WAKE-ME: prs=o/r#1,o/r#2")
    clock[0] += 61
    poll_now(svc)
    svc.wakes.tick(poll=False)
    assert "PR watch refused: o/r#2" in wake_texts(svc, cid)[0]
    settle_wakes(svc, cid)
    svc.wakes.from_final(cid, "turn-2", f"WAKE-ME: prs=o/r#1,o/r#2 at={iso(clock[0] + 3600)} note=\"check both\"")
    registered = svc.store.query("SELECT kind,state FROM wake_requests WHERE conversation_id=? AND state='pending'",
                                 (cid,))
    merged = {"data": {"p0": pr_node(state="MERGED", merged_at=iso(clock[0] + 120))}}
    fake_gh(tmp_path, monkeypatch, merged)
    for _ in range(20):                                        # 20 polls
        clock[0] += 61
        poll_now(svc)
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)
    texts = wake_texts(svc, cid)
    print(f"R3 D1: pending after re-arm={registered} wakes={len(texts)} texts={texts}")
    assert registered, "the valid PR watch and the timer on the copied line were refused with #2"


def test_r3_a_refused_pr_can_be_watched_once_it_exists(svc, tmp_path, monkeypatch):
    """The agent arms a watch on the PR it is about to open, or gh's account lacks access for a
    while. Once the PR resolves, the conversation must be able to watch it again."""
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}},
                                    "errors": [{"type": "NOT_FOUND", "path": ["p0", "pullRequest"],
                                                "message": "Could not resolve to a PullRequest"}]}, rc=1)
    svc.wakes.from_final(cid, "turn-1", "WAKE-ME: prs=o/r#7")
    clock[0] += 61
    poll_now(svc)
    svc.wakes.tick(poll=False)
    settle_wakes(svc, cid)
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": pr_node(checks=(("IN_PROGRESS", None, None),))}})
    outcome = []
    for attempt in range(3):                                   # an hour later, a day later, a week later
        clock[0] += [3600, 86400, 7 * 86400][attempt]
        try:
            svc.op_conversation_wake({"session_id": svc.store.conversation(cid)["native_session_id"],
                                      "request_id": str(uuid.uuid4()), "prs": ["o/r#7"]}, None)
            outcome.append("accepted")
        except ConversationError as exc:
            outcome.append(str(exc))
    print(f"R3 D2: re-arm outcomes after the PR resolves: {outcome}")
    assert "accepted" in outcome, outcome


# --- E: the pr-polled mark under many registrations with the real poller thread ------------------------

def test_r3_many_registrations_still_poll_gh_at_most_once_a_minute(svc, tmp_path, monkeypatch):
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    calls = fake_gh(tmp_path, monkeypatch, {"data": {f"p{i}": pr_node(checks=(("IN_PROGRESS", None, None),))
                                                    for i in range(40)}})
    commits = [0]
    original = svc.store.notify
    monkeypatch.setattr(svc.store, "notify", lambda: (commits.__setitem__(0, commits[0] + 1), original()))
    cids = [bound(svc) for _ in range(40)]
    first_seen, registered_at, submitted = {}, {}, [0]
    real_submit = svc.wakes.poller.submit

    def counting_submit(*a, **kw):
        submitted[0] += 1
        return real_submit(*a, **kw)
    monkeypatch.setattr(svc.wakes.poller, "submit", counting_submit)
    step = 0.05
    for n in range(int(300 / step)):                           # five minutes of 20 Hz control ticks
        clock[0] = T0 + n * step
        if n % 20 == 0 and n // 20 < 120:                      # 120 registrations, one per second
            k = n // 20
            request_id = str(uuid.uuid4())
            svc.wakes.register(cids[k % 40], request_id, wakes.normalize(prs=[f"o/r#{k % 40 + 1}"], now=clock[0]))
            registered_at[request_id] = clock[0]
        svc.wakes.control_tick()
        if svc.wakes._poll_future is not None:
            svc.wakes._poll_future.result(timeout=30)          # gh is fast; keep the fake clock coherent
        if n % 20 == 0 or svc.wakes._poll_future is not None:
            for row in svc.store.query("SELECT request_id FROM wake_requests WHERE observed_json IS NOT NULL"):
                first_seen.setdefault(row["request_id"], clock[0])
    for row in svc.store.query("SELECT request_id FROM wake_requests WHERE observed_json IS NOT NULL"):
        first_seen.setdefault(row["request_id"], clock[0])
    live = [r["request_id"] for r in svc.store.query("SELECT request_id FROM wake_requests WHERE state='pending'")]
    latency = [first_seen[r] - registered_at[r] for r in live if r in first_seen]
    gh = calls_of(calls)
    print(f"R3 E: gh calls={gh} in 300 s with 120 registrations; poller submissions={submitted[0]}; "
          f"commits+notifies={commits[0]}; surviving watches polled {len(latency)}/{len(live)}, "
          f"first-poll latency max {max(latency):.1f} s")
    assert gh <= 6


# --- F: the timer floor at settlement (round one's F5, scenario as originally written) --------------------

@pytest.mark.parametrize("written_at,settles_at,minutes", [(5, 40, 5), (5, 125, 6), (30, 70, 5)])
def test_r3_timer_written_n_minutes_out_during_the_turn_is_kept(svc, written_at, settles_at, minutes):
    """The agent runs `date`, writes `WAKE-ME: at=<now + N min>` and its turn settles a little
    later (or up to 120 s later, while a background `subfleet wait` keeps Claude -p alive)."""
    cid = bound(svc)
    mid = submit(svc, cid, "work, then check back")
    job = turn_job(svc, cid, 1)
    with svc.daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (iso(T0), job))
    with svc.store.transaction() as tx:
        tx.execute("UPDATE messages SET job_id=?,state='complete' WHERE message_id=?", (job, mid))
    svc.wakes.now = lambda: T0 + 600 + settles_at
    svc.wakes.from_final(cid, mid, f"Back soon.\nWAKE-ME: at={iso(T0 + 600 + written_at + minutes * 60)}")
    kinds = [r["kind"] for r in svc.store.query("SELECT kind FROM wake_requests WHERE conversation_id=?", (cid,))]
    print(f"R3 F: written +{written_at}s for {minutes} min, settled +{settles_at}s: requests={kinds}")
    assert kinds == ["time"]


def test_r3_second_watch_in_a_poll_window_is_polled_within_a_minute(svc, tmp_path, monkeypatch):
    """Watch 1 polls at T. Watch 2 is registered at T+1 and watch 3 at T+59. Watch 2 must be
    polled when the once-a-minute mark allows (T+60), not a minute after watch 3."""
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    calls = fake_gh(tmp_path, monkeypatch, {"data": {f"p{i}": pr_node(checks=(("IN_PROGRESS", None, None),))
                                                    for i in range(3)}})
    cids = [bound(svc) for _ in range(3)]
    ids = []

    def at(t, register=None):
        clock[0] = T0 + t
        if register is not None:
            ids.append(str(uuid.uuid4()))
            svc.wakes.register(cids[register], ids[-1], wakes.normalize(prs=[f"o/r#{register + 1}"], now=clock[0]))
        svc.wakes.control_tick()
        if svc.wakes._poll_future is not None:
            svc.wakes._poll_future.result(timeout=30)

    at(0, register=0)
    at(1, register=1)
    at(59, register=2)
    first_poll = None
    t = 59.0
    while t < 200 and first_poll is None:
        t += 0.05
        at(t)
        row = svc.store.one("SELECT observed_json FROM wake_requests WHERE request_id=?", (ids[1],))
        if row["observed_json"]:
            first_poll = t
    print(f"R3 E2: watch registered at +1 s first polled at +{first_poll:.2f} s ({calls_of(calls)} gh calls)")
    assert first_poll is not None and first_poll <= 61
