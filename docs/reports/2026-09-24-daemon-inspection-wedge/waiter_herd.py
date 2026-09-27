"""Waiter herd, measured on the real Daemon.wait: W waiters watching J running
jobs each, woken at R Hz as the control loop's worker completions woke them,
while one client times ordinary requests. Run against unfixed and fixed trees."""
import sys, tempfile, threading, time
from pathlib import Path
from subfleet import protocol
from subfleet.daemon import Daemon

W, J, R, SECONDS = 8, 6, 60, 10
root = Path(tempfile.mkdtemp(prefix="herd-"))
d = Daemon(root / "state")
jobs = []
for n in range(W * J // 2):
    job_id = f"20260924-1148{n:02d}-herd-{n}"
    d.store.add_job(job_id=job_id, request_id=f"herd-{n}", payload_digest="d", kind="run", state="running",
                    workdir=str(root), prompt_path=str(root / "p.md"), sandbox="read-only", caller_session="herd")
    jobs.append(job_id)
reads = [0]
for name in ("query", "one"):
    real = getattr(d.store, name)
    def counted(*a, _real=real, **k):
        reads[0] += 1
        return _real(*a, **k)
    setattr(d.store, name, counted)
stop = threading.Event()
waiters = [threading.Thread(target=d.wait, args=(protocol.WaitArgs(job_ids=jobs[i % len(jobs):][:J] or jobs[:J], deadline_s=SECONDS + 2),), daemon=True) for i in range(W)]
[t.start() for t in waiters]
def notifier():
    while not stop.is_set():
        d._notify(); time.sleep(1 / R)
threading.Thread(target=notifier, daemon=True).start()
time.sleep(.5)
reads[0] = 0
lat = []
end = time.monotonic() + SECONDS
while time.monotonic() < end:
    t = time.perf_counter()
    d.dispatch("list", {"mine": "herd", "running": True})
    d.dispatch("show", {"job_id": jobs[0]})
    lat.append(time.perf_counter() - t)
    time.sleep(.02)
stop.set()
lat.sort()
label = sys.argv[1] if len(sys.argv) > 1 else "run"
print(f"{label:8s} {W} waiters x {J} jobs, woken at {R} Hz: {reads[0]/SECONDS:8.0f} store reads/s | "
      f"list+show p50 {1000*lat[len(lat)//2]:6.1f} ms p90 {1000*lat[int(len(lat)*.9)]:6.1f} ms max {1000*lat[-1]:6.1f} ms")
d.stopping.set(); d._notify(); [t.join(5) for t in waiters]; d.close()
import shutil; shutil.rmtree(root, ignore_errors=True)
