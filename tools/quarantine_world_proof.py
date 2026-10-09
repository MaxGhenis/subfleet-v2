"""Foreground, serial fixed/fresh process-world proof with progress evidence."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/reports/pr131-fix8"
CHILD = r'''
import json, os, sys
from pathlib import Path
import pytest
from tests.fake.test_quarantine_process_world import ProcessWorldMachine

progress = Path(sys.argv[1])
original = ProcessWorldMachine.teardown
completed = 0
def teardown(self):
    global completed
    try:
        return original(self)
    finally:
        completed += 1
        if completed % 20 == 0:
            # Counts fixture completions, including invalid draws. Exact valid
            # world counts are checked in pytest's Hypothesis statistics.
            progress.write_text(json.dumps({"examples_requested": int(os.environ["SF_WORLD_EXAMPLES"]),
                                            "fixtures_completed": completed}))
ProcessWorldMachine.teardown = teardown
sys.exit(pytest.main(["--assert=plain", "-q", "tests/fake/test_quarantine_process_world.py",
                      "--hypothesis-seed=" + sys.argv[2], "--hypothesis-show-statistics"]))
'''


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    config = {"fixed_examples": 2000, "fixed_seed": 131,
              "fresh_examples": 500, "fresh_seed": secrets.randbits(64), "stateful_step_count": 25}
    (EVIDENCE / "proof-config.json").write_text(json.dumps(config, indent=2) + "\n")
    paths = [ROOT / p for p in ("subfleet/procs.py", "subfleet/daemon.py",
                                "tests/fake/test_quarantine_process_world.py")]
    before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    progress = EVIDENCE / "proof-progress.json"
    try:
        for kind in ("fixed", "fresh"):
            count, seed = config[kind + "_examples"], config[kind + "_seed"]
            progress.write_text(json.dumps({"run": kind, "examples_requested": count,
                                            "fixtures_completed": 0}))
            filename = EVIDENCE / f"model-{kind}-{count}.txt"
            print(f"RUN {kind}: {count} worlds, seed {seed}", flush=True)
            with filename.open("w") as log:
                result = subprocess.run([sys.executable, "-B", "-c", CHILD, str(progress), str(seed)],
                    cwd=ROOT, env={**os.environ, "SF_WORLD_EXAMPLES": str(count),
                                   "PYTHONDONTWRITEBYTECODE": "1"}, stdout=log, stderr=subprocess.STDOUT)
            output = filename.read_text()
            quiet = result.returncode == 0 and f"{count} passing, 0 failing" in output
            print(f"{'QUIET' if quiet else 'FAILED'} {kind}: exit {result.returncode}", flush=True)
            if not quiet:
                return 1
    finally:
        after = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        assert before == after, "proof changed production or model bytes"
        (EVIDENCE / "proof-restoration.json").write_text(json.dumps(after, indent=2) + "\n")
        progress.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
