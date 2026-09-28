"""Independent review regressions for live-size and worktree-signature cache mutations."""

from __future__ import annotations

from subfleet import retention
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store

MIB = 1024 * 1024


def job(store, root, identity, order, size, **fields):
    values = dict(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                  workdir=str(root), prompt_path="/prompt", sandbox="read-only", state="succeeded",
                  created_at=f"2026-01-01T00:00:{order:02d}Z", finished_at="2026-01-02T00:00:00Z")
    values.update(fields)
    store.add_job(**values)
    directory = root / "jobs" / identity
    directory.mkdir(parents=True, exist_ok=True)
    grow(directory, size)
    return directory


def grow(directory, size):
    with (directory / "payload.bin").open("wb") as stream:
        stream.truncate(size)


def test_live_job_growth_is_seen_by_the_next_pass(tmp_path):
    """Kills `cache-reuses-live-measurement`: a running job's bytes are re-measured."""
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/h", "home"),
                            "/h", LaneOwner.V2, False))
        job(store, tmp_path, "old", 0, MIB)
        live = job(store, tmp_path, "live", 1, MIB, state="running", finished_at=None)
        store.add_attempt(attempt_id="live/a1", job_id="live", seq=1, lane_id="codex-1",
                          model_requested="m", state="running")
        assert retention.maintenance(store, tmp_path, max_bytes=3 * MIB)["pruned"] == []
        grow(live, 3 * MIB)
        assert retention.maintenance(store, tmp_path, max_bytes=3 * MIB)["pruned"] == ["old"]


def test_a_job_that_gains_a_worktree_is_measured_again(tmp_path):
    """Kills `cache-ignores-signature`: jobs.worktree set at reservation changes what is sized."""
    with Store(tmp_path / "state.sqlite3") as store:
        job(store, tmp_path, "old", 0, MIB)
        job(store, tmp_path, "later", 1, MIB, sandbox="workspace-write")
        assert retention.maintenance(store, tmp_path, max_bytes=3 * MIB)["pruned"] == []
        worktree = tmp_path / "worktrees" / "later"
        worktree.mkdir(parents=True)
        grow(worktree, 2 * MIB)
        # A worktree that is not a registered Git worktree fails removal, so
        # only the byte accounting is under test: "old" goes first either way.
        store.update_job("later", worktree=str(worktree))
        assert retention.maintenance(store, tmp_path, max_bytes=3 * MIB)["pruned"] == ["old"]
