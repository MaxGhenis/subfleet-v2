"""Review r3 of PR #127 (not for merge): idle cost at 187b2150 and the brief's edge scenarios."""
import threading
import time
import uuid

from subfleet.conversations import wakes
from tests.unit.test_conversation_service import submit, svc  # noqa: F401
from tests.unit.test_review_r2_repro import (T0, bound, fake_gh, iso, pr_node, run_under, settle_wakes, turn_job,
                                             wake_rows, wake_texts)


def forty_waiting(svc, runs_each):
    for n in range(40):
        cid = bound(svc)
        ids = [f"w{n}-{i}" for i in range(runs_each)]
        # Seed before the measurement with one durable commit per conversation;
        # per-job setup fsyncs are not part of the idle-control cost being measured.
        with svc.daemon.store.transaction("fixture.waiting-runs"):
            t = turn_job(svc, cid, n)
            for job_id in ids:
                run_under(svc, cid, t, job_id, state="running")
        svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(runs=ids or None, at=iso(T0 + 6 * 3600), now=T0))


def count_commits(svc, monkeypatch):
    commits = [0]
    for store in (svc.store, svc.daemon.store):
        original = store.notify if hasattr(store, "notify") else None
        if original is None:
            continue
        monkeypatch.setattr(store, "notify", lambda original=original: (commits.__setitem__(0, commits[0] + 1),
                                                                        original()))
    return commits


def test_r3_n5_cost_forced_and_paced(svc, monkeypatch):
    """N5 at 187b2150: 40 conversations x 16 running runs + a 6 h timer."""
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    forty_waiting(svc, 16)
    svc.wakes.tick()
    commits = count_commits(svc, monkeypatch)
    samples = []
    for _ in range(5):
        c0, w0 = time.process_time(), time.monotonic()
        for _ in range(20):
            svc.wakes.tick()                                   # forced evaluation, as round two measured
        samples.append(((time.process_time() - c0) / 20, (time.monotonic() - w0) / 20))
    forced_cpu = sorted(s[0] for s in samples)[2]
    forced_wall = sorted(s[1] for s in samples)[2]
    forced_commits = commits[0]
    commits[0] = 0
    c0, w0 = time.process_time(), time.monotonic()
    for n in range(200):                                       # ten simulated seconds at 20 Hz
        clock[0] = T0 + 10 + n * 0.05
        svc.wakes.control_tick()
    paced_cpu, paced_wall = (time.process_time() - c0) / 200, (time.monotonic() - w0) / 200
    print(f"R3 N5: forced median {forced_cpu * 1000:.2f} ms CPU / {forced_wall * 1000:.2f} ms wall per evaluation, "
          f"{forced_commits} commits in 100; paced control tick {paced_cpu * 1000:.3f} ms CPU / "
          f"{paced_wall * 1000:.3f} ms wall, {commits[0]} commits in 200 ticks")
    assert forced_commits == 0 and commits[0] == 0


def test_r3_n5b_reader_wakeups_forced_and_paced(svc):
    """N5b at 187b2150: 40 conversations on 6 h timers; one idle long-poll reader."""
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    for n in range(40):
        cid = bound(svc)
        svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(at=iso(T0 + 6 * 3600), now=T0))
    svc.wakes.tick()
    results = {}
    for mode in ("forced", "paced"):
        checks, stop = [0], threading.Event()

        def predicate():
            checks[0] += 1
            svc.store.query("SELECT MAX(seq) FROM changes")
            return stop.is_set()
        reader = threading.Thread(target=svc.store.wait, args=(predicate, 30))
        reader.start()
        time.sleep(0.2)
        checks[0] = 0
        started, ticks = time.monotonic(), 0
        while time.monotonic() - started < 1.0:
            if mode == "forced":
                svc.wakes.tick()
            else:
                clock[0] += 0.05                               # back-to-back, each one 50 ms of loop time
                svc.wakes.control_tick()
            ticks += 1
        results[mode] = (ticks, checks[0])
        stop.set()
        svc.store.notify()
        reader.join(5)
    print(f"R3 N5b: forced {results['forced'][0]} ticks in 1 s, reader re-ran {results['forced'][1]} times; "
          f"paced {results['paced'][0]} control ticks, reader re-ran {results['paced'][1]} times")
    # The reader's own periodic check (0.5 s) accounts for about 2 per second.
    assert results["forced"][1] <= 4 and results["paced"][1] <= 4


def test_r3_completion_scan_cost_grows_with_delivered_history(svc):
    """The paced scan still walks every run finished since activation whose notice the
    conversation itself surfaced: such runs never leave `_completions`' result."""
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    out = []
    cid = bound(svc)
    for batch in range(3):
        t = turn_job(svc, cid, f"h{batch}")
        # Preserve all 3,000 history rows and commit each batch before evaluation.
        with svc.daemon.store.transaction("fixture.delivered-history"):
            for i in range(1000):
                run_under(svc, cid, t, f"hist-{batch}-{i}")    # finished and announced
        svc.wakes.tick()
        settle_wakes(svc, cid)
        with svc.store.transaction() as tx:                    # keep the throttle out of the way
            tx.execute("UPDATE conversations SET wake_streak=0 WHERE conversation_id=?", (cid,))
        c0 = time.process_time()
        for _ in range(5):
            found = svc.wakes._completions()
        cost = (time.process_time() - c0) / 5
        out.append(((batch + 1) * 1000, cost, sum(len(v) for v in found.values())))
    print("R3 G: _completions CPU per paced scan: " +
          ", ".join(f"{n} delivered runs -> {c * 1000:.1f} ms (returns {r})" for n, c, r in out))


def test_r3_timer_registered_while_blocked_fires_after_unblock(svc):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(at=iso(T0 + 600), now=T0))
    for _ in range(30):
        clock[0] += 61
        svc.wakes.tick()
    blocked = len(wake_rows(svc, cid))
    svc.store.update_conversation(cid, blocked_by=None)
    clock[0] += 1
    svc.wakes.tick()
    print(f"R3 H blocked timer: wakes while blocked={blocked}, after unblock={len(wake_rows(svc, cid))}")
    assert blocked == 0 and len(wake_rows(svc, cid)) == 1


def test_r3_restart_between_ready_and_accepted_fires_once(svc, monkeypatch):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t = turn_job(svc, cid, 1, state="running")                 # a turn is live, so a ready event waits
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(prs=["o/r#1"], now=T0))
    monkeypatch.setattr(wakes, "query_prs", lambda _: {"o/r#1": {"state": "OPEN", "checks": [], "reviews": []}})
    clock[0] += 61
    svc.wakes.tick()                                           # baseline
    monkeypatch.setattr(wakes, "query_prs", lambda _: {"o/r#1": {"state": "MERGED", "merged_at": iso(clock[0]),
                                                                "checks": [], "reviews": []}})
    clock[0] += 61
    svc.wakes.tick()                                           # ready, deferred: the turn is live
    assert wake_rows(svc, cid) == []
    svc.daemon.store.update_job(t, state="succeeded")
    engine = wakes.WakeEngine(svc)                             # the daemon restarts here
    engine.now = lambda: clock[0]
    try:
        for _ in range(5):
            clock[0] += 1
            engine.control_tick()
            if engine._poll_future is not None:
                engine._poll_future.result(timeout=10)
    finally:
        engine.close()
    print(f"R3 H restart: wakes={len(wake_rows(svc, cid))} {wake_texts(svc, cid)}")
    assert len(wake_rows(svc, cid)) == 1


def test_r3_run_finishing_in_the_evaluation_gap_wakes_within_a_second(svc):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    t = turn_job(svc, cid, 1)
    run_under(svc, cid, t, "gap-run", state="running")
    svc.wakes.control_tick()                                   # a scan at T0
    clock[0] += 0.01
    svc.daemon.store.update_job("gap-run", state="succeeded")  # finishes just after it
    woke_at = None
    for n in range(1, 41):
        clock[0] = T0 + 0.01 + n * 0.05
        svc.wakes.control_tick()
        if wake_rows(svc, cid):
            woke_at = clock[0] - (T0 + 0.01)
            break
    print(f"R3 H gap: woken {woke_at:.2f} s after the run finished")
    assert woke_at is not None and woke_at <= 1.05
