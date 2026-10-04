"""Foreground validation, bounded to nine minutes per slice, with exact mutations.

Run from the assigned workspace with its .venv/bin/python. Every subprocess
group belongs to this script; timeout cleanup never matches a process name.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPORT = Path(__file__).resolve().parent
PYTHON = Path(os.environ.get("RETENTION_TEST_PYTHON", str(ROOT / ".venv/bin/python")))
TARGET = "tests/unit/test_retention_salvage_properties.py"


def run(name, tests, *, baseline=False):
    cwd = ROOT / ".baseline-release-217" if baseline else ROOT
    argv = [str(PYTHON), "-m", "pytest", "-q", *tests, "--junitxml=" + str(REPORT / (name + ".xml"))]
    start = time.monotonic()
    with (REPORT / (name + ".log")).open("w") as output:
        child = subprocess.Popen(argv, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, process_group=0)
        (REPORT / (name + ".process.json")).write_text(json.dumps({"pid": child.pid, "pgid": child.pid}) + "\n")
        timed_out = False
        try:
            rc = child.wait(timeout=540)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(child.pid, signal.SIGTERM)
            try:
                rc = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                rc = child.wait()
        finally:
            # Clean up any remaining descendants in this task's own group.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    result = dict(name=name, argv=argv, baseline=baseline,
                  sweep_examples=os.environ.get("RETENTION_SWEEP_EXAMPLES", "40"), returncode=rc,
                  timed_out=timed_out, seconds=round(time.monotonic() - start, 2))
    (REPORT / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in
                      ("name", "returncode", "timed_out", "seconds")}), flush=True)
    print((REPORT / (name + ".log")).read_text()[-18000:], flush=True)
    return rc


def mutations():
    source = ROOT / "subfleet/retention_archive.py"
    original = source.read_text()
    changes = {
        "mutation-missing-store": (
            'if entry["sig"]["t"] == "f" and not name and not entry.get("blob") and not entry.get("regen"):',
            'if False and entry["sig"]["t"] == "f" and not name and not entry.get("blob") and not entry.get("regen"):'),
        "mutation-corrupt-store": (
            'if digest != entry["sha256"]:\n                        os.unlink(name, dir_fd=fd)',
            'if False and digest != entry["sha256"]:\n                        os.unlink(name, dir_fd=fd)'),
    }
    for name, (before, after) in changes.items():
        assert original.count(before) == 1, name
        try:
            source.write_text(original.replace(before, after))
            # Explicit examples kill each mutant before generated shrinking.
            rc = run(name, [TARGET])
            if rc != 1:
                raise RuntimeError(f"{name} was not killed by an assertion (exit {rc})")
        finally:
            source.write_text(original)
        print(name + ": killed; source restored", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "mutations":
        mutations()
    elif sys.argv[1] == "slice":
        groups = json.loads((REPORT / "slices.json").read_text())
        sys.exit(run(sys.argv[3], groups[int(sys.argv[2])]))
    else:
        baseline = sys.argv[1] == "baseline"
        args = sys.argv[2:] if baseline else sys.argv[1:]
        sys.exit(run(args[0], args[1:], baseline=baseline))
