"""Time from SIGKILL of a guardian's group to a verified-empty census, clamped vs unclamped (C-5.6's kill_settle_s)."""
import json, os, signal, statistics, subprocess, sys, tempfile, time
from pathlib import Path
from subfleet import procs
REPO = Path.cwd()
def trial(mode):
    d = Path(tempfile.mkdtemp(prefix="qos-exit-"))
    cmd = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(d), "--cwd", str(d), "--stdout-path", str(d / "o"),
           "--stderr-path", str(d / "e"), "--", sys.executable, "-c",
           "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('ready').touch(); time.sleep(60)"]
    env = {**os.environ, "PYTHONPATH": str(REPO), "SUBFLEET_ATTEMPT": "exit/a1", "SUBFLEET_JOB": "exit", "SUBFLEET_PROVIDER_QOS": mode}
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    while not (d / "ready").exists():
        if time.monotonic() - t0 > 60: raise SystemExit("no ready")
        time.sleep(.01)
    start = json.loads((d / "start.json").read_text())
    procs.signal_group(p.pid, signal.SIGKILL, boot_id=start["boot_id"], proc_start=start["proc_start"])
    killed = time.monotonic()
    p.wait(10)
    reads = 0
    while True:
        reads += 1
        if procs.containment(p.pid, p.pid, None, "exit/a1").verified_empty:
            return time.monotonic() - killed, reads
        if time.monotonic() - killed > 30:
            return float("inf"), reads
        time.sleep(.05)
res = {"utility": [], "inherit": []}
for n in range(int(sys.argv[1])):
    for mode in (("utility", "inherit") if n % 2 == 0 else ("inherit", "utility")):
        res[mode].append(trial(mode))
print("load", [round(x) for x in os.getloadavg()])
for mode, rows in res.items():
    ts = sorted(t for t, _ in rows)
    print(mode, "n", len(ts), "p50", round(ts[len(ts)//2], 3), "max", round(ts[-1], 3), "first-read-empty", sum(1 for _, r in rows if r == 1), "over 3 s", sum(t > 3 for t in ts))
