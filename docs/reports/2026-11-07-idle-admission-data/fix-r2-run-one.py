"""Run one test file with at most four workers, bounded, and append its result to a ledger.

The pytest process tree runs in its own process group (same session) and is
terminated, then killed, if it exceeds the bound, so nothing outlives this call.
"""

import json
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

file, name, bound = sys.argv[1], sys.argv[2], float(sys.argv[3])
workers = sys.argv[4] if len(sys.argv) > 4 else "4"
repo = Path.cwd()
scratch = Path(os.environ["TMPDIR"]) / "sf-ia-f2"
assert "sf-ia-f2" in str(scratch)
data = repo / "docs/reports/2026-11-07-idle-admission-data"
junit = data / f"fix-r2-{name}.xml"
log = data / f"fix-r2-{name}.log"
basetemp = scratch / f"pytest-{name}"
assert not basetemp.exists()
argv = ["uv", "run", "--no-sync", "--with", "pytest-xdist>=3.6", "pytest", "-q", "--tb=short",
        "-p", "no:cacheprovider", *(["-n", workers] if workers != "0" else []),
        f"--basetemp={basetemp}", f"--junitxml={junit}", file]


def load():
    return subprocess.run(["uptime"], text=True, capture_output=True).stdout.strip()


started_at, load_start, started = time.strftime("%H:%M:%SZ", time.gmtime()), load(), time.monotonic()
with open(log, "w") as out:
    child = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, process_group=0)
    try:
        code, finished = child.wait(timeout=bound), True
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        code, finished = child.returncode, False
elapsed = round(time.monotonic() - started, 1)
counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
failed = []
if junit.exists() and finished:
    for case in ET.parse(junit).getroot().iter("testcase"):
        label = case.get("name", "")
        if case.find("failure") is not None:
            counts["failed"] += 1
            failed.append(label)
        elif case.find("error") is not None:
            counts["errors"] += 1
            failed.append(label)
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
tail = log.read_text().strip().splitlines()[-1:] if log.exists() else []
row = {"file": file, "name": name, "argv": argv, "started_at": started_at, "load_start": load_start,
       "load_end": load(), "ended_at": time.strftime("%H:%M:%SZ", time.gmtime()), "elapsed_s": elapsed,
       "finished": finished, "returncode": code, "counts": counts, "failed": failed, "summary": tail}
ledger = data / "fix-r2-validation.json"
rows = json.loads(ledger.read_text()) if ledger.exists() else []
rows.append(row)
ledger.write_text(json.dumps(rows, indent=1) + "\n")
print(json.dumps({k: row[k] for k in ("file", "started_at", "ended_at", "elapsed_s", "finished", "returncode",
                                      "counts", "failed", "summary", "load_start", "load_end")}, indent=1))
