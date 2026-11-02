"""Named mutants of the batch-fairness change, each run against the tests that should kill it.

Run from the repository root, nothing else writing the checkout:

    TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)rbf-tmp .venv/bin/python -I \
        docs/reports/2026-11-02-retention-batch-fairness/mutants.py [name ...]

Each mutant replaces one exact span (it must occur once), runs the tests
serially, and restores the file in `finally`. The result is written beside
this file as `mutants.json` and printed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
R, A = "subfleet/retention.py", "subfleet/retention_archive.py"
#: Each group is one pytest call, so a `-k` filter selects only within its own group.
NEW = [["tests/unit/test_retention_batch_fairness.py", "tests/unit/test_retention_batch_fairness_properties.py"]]
N3 = [["tests/unit/test_retention_archive.py", "-k", "slice or turns or parks or slow"]]

MUTANTS = [
    ("no-yield", R, "yielded = self._make_room(in_flight, order, by_id, counts, totals) if order else []",
     "yielded = []", NEW),
    ("no-reserve", R, "needed = len(in_flight) - (self.batch - 1)", "needed = len(in_flight) - self.batch", NEW),
    ("no-stall", R, "if hold.stalls >= STALL_SLICES:", "if False:", NEW),
    ("work-is-progress", R, "if retirement.last_work and retirement.last_reached > hold.reached:",
     "if retirement.last_work:", NEW),
    ("age-order", R, "return sorted(eligible, key=lambda job_id: (job_id not in self.leftovers, queue[job_id]))",
     "return list(eligible)", NEW),
    ("deadline-first-only", R, "if attempted and self.deadline is not None and self.clock() >= self.deadline:",
     "if retirements and self.deadline is not None and self.clock() >= self.deadline:", NEW),
    ("yield-fresh", R, "if job_id in holds and holds[job_id].slices > 0)",
     "if job_id in holds and holds[job_id].slices >= 0)", NEW),
    ("victim-least-sliced", R, "key=lambda job_id: -holds[job_id].slices)",
     "key=lambda job_id: holds[job_id].slices)", NEW),
    ("yield-not-waiting", R, "waiting = len(chosen) < len(order) or bool(yielded)",
     "waiting = len(chosen) < len(order)", NEW),
    ("refused-keeps-ticket", R, "self.state.queue.pop(job_id, None)      # its turn", "pass  # its turn", NEW),
    ("victim-no-ticket", R, "            self.state.queue[job_id] = self.state.tickets\n"
     "            self.state.tickets += 1\n            job = by_id.get(job_id)",
     "            job = by_id.get(job_id)", NEW),
    ("stall-threshold-1", R, "STALL_SLICES = 3", "STALL_SLICES = 1", NEW + N3),
    ("no-count-walk", A, "                        entries.append(entry)\n                        self.reached += 1",
     "                        entries.append(entry)", NEW + N3),
    ("no-count-read-back", A, "                    digest, sig = _read_back(fd, name, entry, self.ctx.check)\n"
     "                    self.reached += 1", "                    digest, sig = _read_back(fd, name, entry, self.ctx.check)",
     NEW + N3),
    ("no-retry-reset", A, "                    self.reached = reached          # a walk begun again counts from its start",
     "                    pass", NEW + N3),
]


def run(groups: list[list[str]]) -> tuple[int, str]:
    """Run each group until one fails; a group that selects no test counts as a failure of the runner."""
    env = {**os.environ, "RETENTION_FAIRNESS_EXAMPLES": os.environ.get("RETENTION_FAIRNESS_EXAMPLES", "25")}
    details = []
    for tests in groups:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-x", "-rf", *tests],
                              cwd=ROOT, env=env, capture_output=True, text=True)
        failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-300:]
        details.append("; ".join(failed) or tail)
        if proc.returncode == 5:
            raise SystemExit(f"no test selected by {tests}")
        if proc.returncode:
            return proc.returncode, details[-1]
    return 0, " | ".join(details)


def main(names: list[str]) -> None:
    results = {}
    for name, path, old, new, tests in MUTANTS:
        if names and name not in names:
            continue
        file = ROOT / path
        original = file.read_text()
        assert original.count(old) == 1, (name, original.count(old))
        try:
            file.write_text(original.replace(old, new))
            code, detail = run(tests)
        finally:
            file.write_text(original)
        results[name] = {"killed": code != 0, "detail": detail}
        print(f"{name:<22} {'KILLED' if code else 'SURVIVED'}  {detail}", flush=True)
    (HERE / "mutants.json").write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
