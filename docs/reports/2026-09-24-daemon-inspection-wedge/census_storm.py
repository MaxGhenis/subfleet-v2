"""Does the per-running-attempt probe loop (liveness every tick + containment
every 0.5 s, as Daemon._process_attempt does) inflate store-lock request
latency in the same process?  Read-only: only ps/sysctl are executed."""
import os, sqlite3, sys, threading, time
from subfleet import procs

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
def attempt_loop(i):
    census_next = 0.0
    while not stop.is_set():
        procs.liveness(me.pid, me.boot_id, me.proc_start); spawns[0] += 3
        if time.monotonic() >= census_next:
            c = procs.containment(os.getpgrp(), me.pid, None, f"20260924-000000-sim-{i}/a1", root="/nonexistent-sim-root")
            spawns[0] += 2 + 3 * len(c.live_pids)
            census_next = time.monotonic() + .5
        time.sleep(0.05)          # the control tick

def run(n_attempts, seconds=12):
    stop.clear(); spawns[0] = 0
    ths = [threading.Thread(target=attempt_loop, args=(i,), daemon=True) for i in range(n_attempts)]
    [t.start() for t in ths]
    time.sleep(1)
    lat = []
    def client():
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            t = time.perf_counter(); query(); lat.append(time.perf_counter() - t); time.sleep(0.02)
    cs = [threading.Thread(target=client) for _ in range(16)]
    t0 = time.monotonic(); [c.start() for c in cs]; [c.join() for c in cs]
    stop.set(); [t.join() for t in ths]
    lat.sort()
    print(f"{n_attempts} simulated running attempts: {spawns[0]/(time.monotonic()-t0+1):6.1f} spawns/s | "
          f"300-row locked query p50 {1000*lat[len(lat)//2]:7.1f} ms p90 {1000*lat[int(len(lat)*.9)]:7.1f} ms "
          f"max {1000*lat[-1]:7.1f} ms (n={len(lat)})", flush=True)

run(0); run(1); run(3); run(7)
