"""C-8.4: the dependency boundary includes the retired jobs/<id> directory.

The first test is review r1's job-directory probe turned into a regression.
This boundary was already present in release/217, apart from r1 findings 1–3.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from subfleet import dependencies, folders, procs, retention, retention_archive as rarch
from tests.unit.retention_world import World, snapshot


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, "worktree_cwd_pids", lambda *a, **k: frozenset())
    w = World(tmp_path)
    yield w
    w.close()


def run(w):
    return retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                 holders=lambda *a, **k: {})


def need(w, role="out_path", *, exporting=False):
    folder = w.root / "jobs" / "owner"
    outside = w.base / "outside"
    outside.mkdir(exist_ok=True)
    output = folder / "result.md"
    depends = {role: [str(output)] if role == "git" else str(output)}
    w.store.add_job(job_id="reader", request_id="reader", payload_digest="d", kind="dispatch",
                    state="succeeded" if exporting else "queued", workdir=str(outside),
                    out_path=str(output) if role == "out_path" else None,
                    prompt_path="/p", sandbox="read-only")
    w.store.add_event("job.submitted", job_id="reader", data={"depends": depends})
    if exporting:
        assert w.store.acquire_lease("out:" + folders.identity(str(output)), "reader")


def test_c8_4_output_in_another_jobs_directory_keeps_that_directory(world):
    world.job("owner", worktree=False)
    folder = world.root / "jobs" / "owner"
    outside = world.base / "outside"
    outside.mkdir()
    output = folder / "result.md"
    recorded = dependencies.record(str(outside), out_path=str(output))
    world.store.add_job(job_id="reader", request_id="reader", payload_digest="d", kind="dispatch",
                        state="queued", workdir=str(outside), out_path=str(output),
                        prompt_path="/p", sandbox="read-only")
    world.store.add_event("job.submitted", job_id="reader", data={"depends": recorded})
    before = snapshot(folder)
    result = run(world)
    assert "owner" not in result["pruned"], result
    assert snapshot(folder) == before
    world.store.update_job("reader", state="succeeded")
    assert "owner" in run(world)["pruned"]


@pytest.mark.parametrize("role", ["review_root", "out_path", "git"])
@pytest.mark.parametrize("stage", ["census", "fence", "commit"])
def test_c8_4_job_directory_dependencies_are_rechecked_atomically(world, monkeypatch, role, stage):
    world.job("owner", worktree=False)
    folder = world.root / "jobs" / "owner"
    before = snapshot(folder)
    begun = []
    original_begin = rarch.Retirement.begin
    original_final = rarch.Retirement.final_check

    def begin(self, *a, **k):
        begun.append(self.job_id)
        if stage == "fence":
            need(world, role)
        return original_begin(self, *a, **k)

    def final(self):
        original_final(self)
        if stage == "commit":
            need(world, role)
        # Exercise the commit's own journal-path recheck independently of its pin callback.
        self.ctx.pinned = lambda *_: None

    monkeypatch.setattr(rarch.Retirement, "begin", begin)
    monkeypatch.setattr(rarch.Retirement, "final_check", final)
    if stage == "census":
        need(world, role)
    result = run(world)
    assert "owner" not in result["pruned"], result
    assert snapshot(folder) == before
    assert bool(begun) == (stage != "census")
    if stage == "census":
        assert result["pin_reasons"]["owner"] == "job-directory-in-use"
    else:
        assert "job-directory-in-use" in result["deferred"]["owner"]


def test_c8_4_job_directory_pending_export_pins_until_written(world):
    world.job("owner", worktree=False)
    need(world, exporting=True)
    assert "owner" not in run(world)["pruned"]
    world.store.release_leases("reader")
    assert "owner" in run(world)["pruned"]


def test_c8_4_commit_rechecks_the_canonical_job_directory_fence(world, monkeypatch):
    """A symlinked jobs parent makes the journal path differ from its fence."""
    world.job("owner", worktree=False)
    storage = world.base / "job-storage"
    (world.root / "jobs").rename(storage)
    (world.root / "jobs").symlink_to(storage)
    folder = storage / "owner"
    before = snapshot(folder)
    original = rarch.Retirement.final_check

    def final(self):
        original(self)
        world.store.add_job(job_id="reader", request_id="reader", payload_digest="d", kind="dispatch",
                            state="queued", workdir="/outside", prompt_path="/p", sandbox="read-only")
        world.store.add_event("job.submitted", job_id="reader", data={
            "depends": {"out_path": str(folder / "result.md")}})
        self.ctx.pinned = lambda *_: None

    monkeypatch.setattr(rarch.Retirement, "final_check", final)
    result = run(world)
    assert "owner" not in result["pruned"], result
    assert snapshot(folder) == before
    assert result["deferred"]["owner"] == "pinned: job-directory-in-use"
