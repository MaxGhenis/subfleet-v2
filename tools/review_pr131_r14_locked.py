"""Foreground review runner: one lock hold, bounded queue and wall time."""
import os
from pathlib import Path
import signal
import subprocess
import sys
from collections import Counter
import xml.etree.ElementTree as ET

log_path = Path(sys.argv[1])
xml_path = log_path.with_suffix(".xml")
with log_path.open("w") as output:
    child = subprocess.Popen(
        ["/usr/bin/lockf", "-t", os.environ.get("SF_REVIEW_LOCK_WAIT_S", "45"), "-k",
         "/private/tmp/claude-501/subfleet-suites.lock",
         "uv", "run", "pytest", "-q", "-o", "junit_family=legacy",
         "--junitxml", str(xml_path), *sys.argv[2:]],
        stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())})
    try:
        result = child.wait(timeout=1100)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        result = 124
print(log_path.read_text())
if xml_path.exists():
    counts = {}
    for case in ET.parse(xml_path).iter("testcase"):
        name = case.get("file", case.get("classname", "unknown"))
        count = counts.setdefault(name, Counter())
        status = next((tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None), "passed")
        count[status] += 1
    for name, count in sorted(counts.items()):
        print("FILE_COUNTS", name, dict(count))
sys.exit(result)
