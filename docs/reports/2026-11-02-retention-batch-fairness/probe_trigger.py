"""Probe: a real archive that parks every slice without progress (review r1 of #286, P3-4).

Run from the repository root against whatever code is checked out:

    TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)rbf-tmp .venv/bin/python -I \
        docs/reports/2026-11-02-retention-batch-fairness/probe_trigger.py

One job (`job-hot`) has a 64 KiB file first in walk order whose read costs
100 s of a 60 s slice, and the file is rewritten (same size, new bytes)
between passes. A second, newer job (`job-new`) is small. `batch=1`. The
probe prints, per pass, what the pass reported and what the hot job's slice
recorded. The real builder, real files and a real Git repository; only the
clock and the read cost are simulated.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from subfleet import retention                                  # noqa: E402
from subfleet import retention_archive as rarch                 # noqa: E402
from subfleet import retention_fs as rfs                        # noqa: E402
from subfleet import retention_git as rgit                      # noqa: E402
from tests.unit.retention_world import Clock, World             # noqa: E402

HOT = 64 * 1024


def main() -> None:
    rgit.temp_roots = lambda: {"/nonexistent-temporary-root"}
    with tempfile.TemporaryDirectory() as tmp:
        w = World(Path(tmp))
        wt = w.job("job-hot", created="2026-09-01T00:00:00Z")
        (wt / "0-hot.bin").write_bytes(os.urandom(HOT))
        for n in range(3):
            (wt / f"z{n}.txt").write_text(f"cold {n}\n")
        w.job("job-new", created="2026-09-01T00:01:00Z")
        clock = Clock()
        read = rfs.read_hashes

        def slow(fd, size, fmt, check=None):
            if size == HOT:
                clock.advance(100)
            return read(fd, size, fmt, check)

        rfs.read_hashes = slow
        slices = []
        archive = rarch.Retirement.archive

        def recorded(self, slice_end):
            try:
                return archive(self, slice_end)
            finally:
                if self.job_id == "job-hot":
                    slices.append((self.last_work, getattr(self, "last_reached", "n/a")))

        rarch.Retirement.archive = recorded
        state = retention.RetentionState()
        print("pass pruned       in_flight    more  progressed  hot slice (last_work, last_reached)  deferred")
        for n in range(8):
            before = len(slices)
            r = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {},
                                      state=state, clock=clock, slice_s=60, batch=1)
            hot = slices[before] if len(slices) > before else "-"
            print(f"{n + 1:>4} {str(r['pruned']):<12} {str(r['in_flight']):<12} {str(r['more']):<5} "
                  f"{str(r['progressed']):<11} {str(hot):<36} {r['deferred']}")
            for path in (w.root / "retention" / "job-hot" / "worktree" / "0-hot.bin", wt / "0-hot.bin"):
                if path.exists():
                    path.write_bytes(os.urandom(HOT))
                    break
        print("job-new still in the store:", w.store.get_job("job-new") is not None)
        w.close()


if __name__ == "__main__":
    main()
