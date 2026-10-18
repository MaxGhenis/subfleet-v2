"""Same measurement as census_storm_bigheap.py, driving the fixed per-attempt loop:
liveness at most every LIVENESS_INTERVAL_S (boot UUID cached) and a group-only
census every 0.5 s that identifies only members it has not recorded yet."""
import os, sqlite3, threading, time
from subfleet import procs
from subfleet.contracts import LIVENESS_INTERVAL_S, OWNED_CENSUS_INTERVAL_S

ballast = [{"k": i, "s": "x" * 40} for i in range(4_000_000)]
idle = [threading.Thread(target=threading.Event().wait, daemon=True) for _ in range(70)]
[t.start() for t in idle]

db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
db.executemany("INSERT INTO t(v) VALUES (?)", [("x" * 200,) for _ in range(2000)])
lock = threading.RLock()
def query(n=300):
    with lock:
        return [dict(zip(("id", "v"), r)) for r in db.execute("SELECT id, v FROM t LIMIT ?", (n,)).fetchall()]

me = procs.identity(os.getpid())
stop = threading.Event()
spawns = [0]
real_read = procs._read
def counting_read(argv, **kw):
    spawns[0] += 1
    return real_read(argv, **kw)
procs._read = counting_read

def attempt_loop(i):
    liveness_next = census_next = 0.0
    owned = set()
    while not stop.is_set():
        now = time.monotonic()
        if now >= liveness_next:
            liveness_next = now + LIVENESS_INTERVAL_S
            procs.liveness(me.pid, me.boot_id, me.proc_start)
            if time.monotonic() >= census_next:
                members = procs.group_members(os.getpgrp())
                fresh = {pid: procs.identity(pid) for pid in members.keys() - owned}   # pid -> start since PR #40's review fixes
                if fresh:
                    procs.same_process(me.pid, me.boot_id, me.proc_start)
                    owned |= set(fresh)
                census_next = time.monotonic() + OWNED_CENSUS_INTERVAL_S
        time.sleep(0.05)

def run(n_attempts, seconds=12):
    stop.clear(); spawns[0] = 0
    ths = [threading.Thread(target=attempt_loop, args=(i,), daemon=True) for i in range(n_attempts)]
    [t.start() for t in ths]
    time.sleep(1)
    s0 = spawns[0]
    lat = []
    def client():
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            t = time.perf_counter(); query(); lat.append(time.perf_counter() - t); time.sleep(0.02)
    cs = [threading.Thread(target=client) for _ in range(16)]
    t0 = time.monotonic(); [c.start() for c in cs]; [c.join() for c in cs]
    rate = (spawns[0] - s0) / (time.monotonic() - t0)
    stop.set(); [t.join() for t in ths]
    lat.sort()
    print(f"{n_attempts} simulated running attempts (fixed loop): {rate:6.1f} spawns/s | "
          f"300-row locked query p50 {1000*lat[len(lat)//2]:7.1f} ms p90 {1000*lat[int(len(lat)*.9)]:7.1f} ms "
          f"max {1000*lat[-1]:7.1f} ms (n={len(lat)})", flush=True)

run(0); run(3); run(7)
