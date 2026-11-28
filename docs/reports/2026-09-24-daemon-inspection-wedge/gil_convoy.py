"""Does one CPU-bound thread inflate sqlite fetchall time (per-row GIL release)?"""
import os, sqlite3, sys, threading, time

db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
db.executemany("INSERT INTO t(v) VALUES (?)", [("x" * 200,) for _ in range(2000)])
lock = threading.RLock()

def query(n):
    with lock:
        return [dict(zip(("id", "v"), r)) for r in db.execute("SELECT id, v FROM t LIMIT ?", (n,)).fetchall()]

def measure(label):
    out = []
    for n in (10, 300, 2000):
        t = time.perf_counter(); query(n); out.append(f"{n} rows {1000*(time.perf_counter()-t):7.1f} ms")
    print(f"{label:34s}", " | ".join(out), flush=True)

stop = threading.Event()
def cpu_hog():                      # pure-Python work, like the mirror's JSON/dict churn
    x = 0
    while not stop.is_set():
        x = (x * 31 + 7) % 1000003
def stat_hog():                     # a stat storm, like mirror/_size (Path + stat per file)
    from pathlib import Path
    root = Path(os.path.expanduser("~/.subfleet/jobs"))
    while not stop.is_set():
        for d, _, files in os.walk(root):
            for f in files:
                try: (Path(d) / f).stat()
                except OSError: pass
                if stop.is_set(): return

print("switch interval", sys.getswitchinterval())
measure("baseline (no background thread)")
for hog in (cpu_hog, stat_hog):
    stop.clear(); th = threading.Thread(target=hog, daemon=True); th.start(); time.sleep(0.2)
    measure(f"with {hog.__name__}")
    stop.set(); th.join()
# N contending request threads + one hog: per-request latency
stop.clear(); th = threading.Thread(target=cpu_hog, daemon=True); th.start()
lat = []
def client():
    for _ in range(5):
        t = time.perf_counter(); query(300); lat.append(time.perf_counter() - t)
ts = [threading.Thread(target=client) for _ in range(16)]
t0 = time.perf_counter(); [t.start() for t in ts]; [t.join() for t in ts]
stop.set(); th.join()
lat.sort()
print(f"16 clients x5 x300-row queries + cpu_hog: wall {time.perf_counter()-t0:.1f}s p50 {lat[len(lat)//2]:.2f}s max {lat[-1]:.2f}s")
lat.clear()
ts = [threading.Thread(target=client) for _ in range(16)]
t0 = time.perf_counter(); [t.start() for t in ts]; [t.join() for t in ts]
lat.sort()
print(f"16 clients x5 x300-row queries, no hog: wall {time.perf_counter()-t0:.1f}s p50 {lat[len(lat)//2]:.3f}s max {lat[-1]:.3f}s")
