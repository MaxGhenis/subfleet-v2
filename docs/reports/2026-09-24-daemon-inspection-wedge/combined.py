"""Combined reproduction on the real Daemon (request path, wait, store):
A running-attempt inspection loops (as that tree's _process_attempt paces them),
W waiters x J jobs woken at the control loop's rate, a mirror stand-in
stat-walking ~/.claude/projects, and a daemon-sized heap. Read-only outside a
temp state root; only ps/sysctl are executed."""
import os, sys, tempfile, threading, time
from pathlib import Path
from subfleet import procs, protocol
from subfleet.daemon import Daemon

FIXED = sys.argv[1] == "fixed"
A, W, J, SECONDS = 3, 8, 6, 15
R = 20 * (A + 2)                      # a notify per worker completion: A attempts + admission + timer
ballast = [{"k": i, "s": "x" * 40} for i in range(4_000_000)]
idle = [threading.Thread(target=threading.Event().wait, daemon=True) for _ in range(70)]
[t.start() for t in idle]

root = Path(tempfile.mkdtemp(prefix="combined-"))
d = Daemon(root / "state")
jobs = []
for n in range(24):
    job_id = f"20260924-1148{n:02d}-combined-{n}"
    d.store.add_job(job_id=job_id, request_id=f"c-{n}", payload_digest="d", kind="run", state="running",
                    workdir=str(root), prompt_path=str(root / "p.md"), sandbox="read-only", caller_session="c")
    jobs.append(job_id)
stop = threading.Event()
me = procs.identity(os.getpid())

def inspection(i):                     # what one running attempt costs per that tree's _process_attempt
    liveness_next = census_next = 0.0
    owned = set()
    while not stop.is_set():
        now = time.monotonic()
        if not FIXED:
            procs.liveness(me.pid, me.boot_id, me.proc_start)
            if time.monotonic() >= census_next:
                procs.containment(os.getpgrp(), me.pid, None, f"20260924-000000-sim-{i}/a1", root="/nonexistent")
                procs.same_process(me.pid, me.boot_id, me.proc_start)
                census_next = time.monotonic() + .5
        elif now >= liveness_next:
            liveness_next = now + 1.0
            procs.liveness(me.pid, me.boot_id, me.proc_start)
            if time.monotonic() >= census_next:
                members = procs.group_members(os.getpgrp())
                fresh = {pid: procs.identity(pid) for pid in members.keys() - owned}   # pid -> start since PR #40's review fixes
                if fresh:
                    procs.same_process(me.pid, me.boot_id, me.proc_start); owned |= set(fresh)
                census_next = time.monotonic() + .5
        time.sleep(.05)

def mirror_standin():                  # the mirror's stat storm, read-only
    base = Path.home() / ".claude" / "projects"
    while not stop.is_set():
        for dirpath, _, files in os.walk(base):
            for name in files:
                try:
                    (Path(dirpath) / name).stat()
                except OSError:
                    pass
                if stop.is_set():
                    return

def notifier():
    while not stop.is_set():
        d._notify(); time.sleep(1 / R)

threads = [threading.Thread(target=inspection, args=(i,), daemon=True) for i in range(A)]
threads += [threading.Thread(target=d.wait, args=(protocol.WaitArgs(job_ids=jobs[(i * 3) % 18:][:J], deadline_s=SECONDS + 3),), daemon=True) for i in range(W)]
threads += [threading.Thread(target=mirror_standin, daemon=True), threading.Thread(target=notifier, daemon=True)]
[t.start() for t in threads]
time.sleep(1)
lat = []
end = time.monotonic() + SECONDS
while time.monotonic() < end:
    t = time.perf_counter()
    d.dispatch("list", {"mine": "c", "running": True})
    d.dispatch("show", {"job_id": jobs[0]})
    lat.append(time.perf_counter() - t)
    time.sleep(.02)
stop.set(); d.stopping.set(); d._notify()
import shutil; shutil.rmtree(root, ignore_errors=True)
lat.sort()
print(f"{sys.argv[1]:8s} A={A} W={W}x{J} notify {R} Hz + mirror stand-in + 1 GB heap: "
      f"list+show p50 {1000*lat[len(lat)//2]:7.1f} ms p90 {1000*lat[int(len(lat)*.9)]:7.1f} ms "
      f"max {1000*lat[-1]:7.1f} ms n={len(lat)}", flush=True)
os._exit(0)
