"""Measure (1) how far retention._size gets over the real worktrees in 60 s and
(2) what that walk does to store-lock-serialized query latency in the same process."""
import sqlite3, threading, time
from pathlib import Path
from subfleet import retention

db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
db.executemany("INSERT INTO t(v) VALUES (?)", [("x" * 200,) for _ in range(2000)])
lock = threading.RLock()
def query(n):
    with lock:
        return [dict(zip(("id", "v"), r)) for r in db.execute("SELECT id, v FROM t LIMIT ?", (n,)).fetchall()]

root = Path("/Users/maxghenis/.subfleet/worktrees")
dirs = sorted(p for p in root.iterdir() if p.is_dir())
result = {"sized": 0, "bytes": 0, "interrupted": None}
def walk():
    deadline = time.monotonic() + 60
    try:
        for d in dirs:
            result["bytes"] += retention._size(d, deadline=deadline)
            result["sized"] += 1
    except retention._Interrupted as exc:
        result["interrupted"] = str(exc)
t0 = time.monotonic()
th = threading.Thread(target=walk); th.start()
lat = []
while th.is_alive():
    t = time.perf_counter(); query(300); lat.append(time.perf_counter() - t)
    time.sleep(0.05)
th.join()
lat.sort()
print(f"walk: {time.monotonic()-t0:.1f}s, sized {result['sized']}/{len(dirs)} worktrees, {result['bytes']/1e9:.2f} GB counted, interrupted={result['interrupted']}")
print(f"300-row query under lock during walk: n={len(lat)} p50 {1000*lat[len(lat)//2]:.1f} ms p90 {1000*lat[int(len(lat)*.9)]:.1f} ms max {1000*lat[-1]:.1f} ms")
