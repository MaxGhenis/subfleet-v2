"""Run one pytest invocation for fix r1 and record it.

usage: run.py <label> [pytest args...]
Records uptime at start and end, the full command, exit status, elapsed time and
the last summary line in runs.jsonl beside this file; the log goes to logs/<label>.log.
Runs in the foreground with a SIGALRM deadline (RUN_ALARM seconds, default 560) so
no process outlives the tool call.
"""
import json, os, signal, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
assert "sf-sbh-f1" in str(HERE)
label, args = sys.argv[1], sys.argv[2:]
logs = HERE / "logs"; logs.mkdir(exist_ok=True)
basetemp = HERE / f"pytest-{label}"
assert not basetemp.exists(), basetemp
env = dict(os.environ)
env.pop("PYTHONPATH", None)
env["TMPDIR"] = os.popen("getconf DARWIN_USER_TEMP_DIR").read().strip()
env["UV_CACHE_DIR"] = env["TMPDIR"] + "uv-cache"
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["HYPOTHESIS_STORAGE_DIRECTORY"] = str(HERE / "hypothesis")
cmd = [str(HERE / "venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
       "--basetemp", str(basetemp), *args]
alarm = int(os.environ.get("RUN_ALARM", "560"))
up0 = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
t0 = time.time()
with open(logs / f"{label}.log", "w") as log:
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    try:
        rc = proc.wait(timeout=alarm)
        stopped = False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            rc = proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            rc = proc.wait()
        stopped = True
el = time.time() - t0
up1 = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
tail = (logs / f"{label}.log").read_text(errors="replace").strip().splitlines()
summary = next((l for l in reversed(tail) if " passed" in l or " failed" in l or " error" in l
                or "no tests ran" in l), tail[-1] if tail else "")
rec = {"label": label, "cmd": cmd, "rc": rc, "stopped_by_alarm": stopped, "elapsed_s": round(el, 1),
       "uptime_start": up0, "uptime_end": up1, "summary": summary}
with open(HERE / "runs.jsonl", "a") as fh:
    fh.write(json.dumps(rec) + "\n")
print(json.dumps(rec, indent=1))
