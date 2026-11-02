"""Retention's batch is fair (C-8.4; review r1 of #286, P3-4).

A pass takes at most `batch` jobs into flight. Before this, a job that parked
without progress kept its slot for ever, and once parked jobs filled every slot
no eligible job started again. These tests drive the real pass, and where they
can the real archive builder, on real repositories.

The trigger: a real archive parks every slice and gets no further when a file
in its quarantined tree changes between slices and reading that file outlasts
the slice. The builder parks only after new work (review of a9a6cbf4, N3), and
re-reading the changed file is new work, so `last_work` never reads 0; what
stops moving is how far the walk gets (`Retirement.last_reached`).
"""
from __future__ import annotations

import os
import time

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from tests.unit.retention_world import Clock, World, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


def run(w: World, **kwargs):
    kwargs.setdefault("max_jobs", 0)
    kwargs.setdefault("max_bytes", 0)
    kwargs.setdefault("holders", lambda watches, **_: {})
    return retention.maintenance(w.store, w.root, **kwargs)


HOT = 64 * 1024


def hot_job(w: World, job_id: str, created: str):
    """A job whose first file in walk order is `HOT` bytes, read slowly."""
    wt = w.job(job_id, created=created)
    (wt / "0-hot.bin").write_bytes(os.urandom(HOT))
    for n in range(3):
        (wt / f"z{n}.txt").write_text(f"cold {n}\n")
    return wt


def slow_hot_reads(monkeypatch, clock: Clock, seconds: float) -> None:
    original = rfs.read_hashes

    def slow(fd, size, fmt, check=None):
        if size == HOT:
            clock.advance(seconds)
        return original(fd, size, fmt, check)

    monkeypatch.setattr(rfs, "read_hashes", slow)


def rewrite_hot(w: World, wt, job_id: str) -> None:
    """The writer: the same size, new bytes, wherever the tree is now."""
    for path in (w.root / "retention" / job_id / "worktree" / "0-hot.bin", wt / "0-hot.bin"):
        if path.exists():
            path.write_bytes(os.urandom(HOT))
            return


# --- the trigger, with the real builder ----------------------------------------------------

def test_a_real_archive_parks_without_progress_while_a_file_changes_between_slices(world, monkeypatch):
    """Review r1 of #286, P3-4, with no stub: the hot file's read outlasts the
    slice and the file changes between slices. Each slice reads it again, writes
    a progress record for its new version, and parks at the next entry. With
    `batch=1` the job behind it must still start, and the hot job is put back
    and deferred, its archive progress kept."""
    w = world
    wt = hot_job(w, "job-hot", "2026-09-01T00:00:00Z")
    w.job("job-new", created="2026-09-01T00:01:00Z")
    clock = Clock()
    slow_hot_reads(monkeypatch, clock, 100)                 # longer than the 60 s slice
    reached = []
    original_archive = rarch.Retirement.archive

    def archive(self, slice_end):
        try:
            return original_archive(self, slice_end)
        finally:
            if self.job_id == "job-hot":
                reached.append((self.last_work, getattr(self, "last_reached", None)))

    monkeypatch.setattr(rarch.Retirement, "archive", archive)
    state = retention.RetentionState()
    results = []
    for _ in range(8):
        results.append(run(w, state=state, clock=clock, slice_s=60, batch=1))
        rewrite_hot(w, wt, "job-hot")
    started_new = next((n for n, r in enumerate(results) if "job-new" in r["pruned"]), None)
    assert started_new is not None and started_new <= 2, [(r["pruned"], r["in_flight"], r["deferred"])
                                                           for r in results]
    # Every hot slice did new work: the old `last_work` test never saw a stall.
    assert reached and all(work > 0 for work, _ in reached), reached
    stalled = [r["deferred"].get("job-hot", "") for r in results]
    assert any(reason.startswith("stalled") for reason in stalled), stalled
    # Put back where it was, its archive kept as an idle cache for the next try.
    assert (wt / "0-hot.bin").exists() and w.store.get_job("job-hot") is not None
    journal = rarch.load_journal(w.root, "job-hot")
    assert journal["state"] == "idle" and (w.root / "retention" / "job-hot" / "archive").is_dir()
    # Once the writer stops and the backoff passes, it retires.
    clock.advance(rarch.DEFER_ERROR_MAX_S + 1)
    for _ in range(6):
        last = run(w, state=state, clock=clock, slice_s=60, batch=1)
        if last["pruned"]:
            break
    assert last["pruned"] == ["job-hot"], last
