"""C-8.4/C-26.12: deadline-limited maintenance converges instead of restarting."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import retention
from subfleet.store import Store


MIB = 1024 * 1024


class Clock:
    """Charge only filesystem work, avoiding machine-speed-dependent deadlines."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def add_job(store, root, identity, *, order, parts=1, size=MIB, kind="dispatch", nested=True,
            state="succeeded"):
    store.add_job(
        job_id=identity, request_id=identity, payload_digest="digest", kind=kind,
        workdir=str(root), prompt_path="/prompt", sandbox="read-only", state=state,
        created_at=f"2026-01-01T00:00:{order:02d}Z", finished_at="2026-01-02T00:00:00Z",
    )
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    for index in range(parts):
        target = directory / f"part-{index}" if nested else directory
        target.mkdir(exist_ok=True)
        # Large logical sizes put pressure on the real byte budget without
        # allocating hundreds of megabytes in every regression run.
        with (target / f"payload-{index}.bin").open("wb") as stream:
            stream.truncate(size)
    return directory


def slow_walk(monkeypatch, store):
    clock = Clock()
    original = retention.os.walk

    def walk(*args, **kwargs):
        for item in original(*args, **kwargs):
            assert not store.connection.in_transaction
            clock.now += 1
            yield item

    monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(retention.os, "walk", walk)
    return clock


@pytest.mark.parametrize("kind", ["dispatch", "turn"])
@pytest.mark.parametrize("pinned_count,budget_jobs", [(0, 3), (2, 3), (2, 0)])
def test_repeated_short_passes_reach_budget_or_only_pinned_jobs(tmp_path, monkeypatch, kind,
                                                               pinned_count, budget_jobs):
    """A finite stable pool eventually fits its byte budget or contains only pins.

    Every individual job needs more scanning time than a maintenance pass, and
    the total pool needs much more. The property applies independently to both
    pools, including a budget that pinned data alone makes impossible to meet.
    """
    with Store(tmp_path / "state.sqlite3") as store:
        identities = [f"job-{index}" for index in range(9)]
        job_bytes = 5 * MIB
        for index, identity in enumerate(identities):
            add_job(store, tmp_path, identity, order=index, parts=5, kind=kind)
        pinned = set(identities[:pinned_count])
        clock = slow_walk(monkeypatch, store)
        budget = budget_jobs * job_bytes
        limits = {"turn_max_bytes" if kind == "turn" else "max_bytes": budget}
        pruned = []
        interruptions = 0
        for _ in range(100):
            result = retention.maintenance(
                store, tmp_path, turn_keep_s=0, referenced_job_ids=pinned,
                deadline=clock.now + 2.5, **limits,
            )
            pruned.extend(result["pruned"])
            interruptions += result.get("interrupted") == "deadline"
            remaining = {row["job_id"] for row in store.list_jobs()}
            assert pinned <= remaining
            if len(remaining) * job_bytes <= budget or remaining <= pinned:
                break
        else:
            pytest.fail("bounded passes never reached the byte budget or exhausted unpinned jobs")

        assert interruptions > 1
        assert pruned
        assert pruned == [identity for identity in identities if identity not in remaining]
        for identity in identities:
            assert (tmp_path / "jobs" / identity).exists() == (identity in remaining)


def test_interrupted_pass_keeps_pruning_done_before_later_scan(tmp_path, monkeypatch):
    """Count pressure removes a measured oldest job before walking all other jobs."""
    with Store(tmp_path / "state.sqlite3") as store:
        oldest = add_job(store, tmp_path, "oldest", order=0, nested=False)
        later = add_job(store, tmp_path, "later", order=1, parts=8)
        clock = slow_walk(monkeypatch, store)

        result = retention.maintenance(
            store, tmp_path, max_jobs=0, max_bytes=100 * MIB, deadline=clock.now + 2.5,
        )

        assert result["interrupted"] == "deadline"
        assert result["pruned"] == ["oldest"]
        assert store.get_job("oldest") is None
        assert not oldest.exists()
        assert store.get_job("later") is not None
        assert later.exists()
        assert any(event["kind"] == "retention.pruned" for event in store.list_events("oldest"))


def test_scan_resumes_inside_one_large_directory(tmp_path, monkeypatch):
    """A directory with more files than fit in one deadline cannot starve pruning."""
    with Store(tmp_path / "state.sqlite3") as store:
        directory = add_job(store, tmp_path, "many-files", order=0, parts=37, nested=False)
        clock = Clock()
        original = Path.lstat
        measured = []

        def lstat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.parent == directory and path.suffix == ".bin":
                assert not store.connection.in_transaction
                measured.append(path.name)
                clock.now += 1
            return result

        monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=clock.monotonic))
        monkeypatch.setattr(Path, "lstat", lstat)
        for _ in range(30):
            result = retention.maintenance(store, tmp_path, max_bytes=MIB, deadline=clock.now + 3.5)
            if store.get_job("many-files") is None:
                break
        else:
            pytest.fail("every short pass restarted the same large directory")

        assert result["pruned"] == ["many-files"]
        assert len(set(measured)) == 37
        assert not directory.exists()


@pytest.mark.parametrize("state", ["succeeded", "running"])
def test_large_old_pinned_job_does_not_starve_later_prunable_job(tmp_path, monkeypatch, state):
    """Pinned terminal and active jobs cannot restart the same long scan forever."""
    with Store(tmp_path / "state.sqlite3") as store:
        pinned_dir = add_job(store, tmp_path, "pinned", order=0, parts=20, state=state)
        prunable_dir = add_job(store, tmp_path, "prunable", order=1, nested=False)
        clock = slow_walk(monkeypatch, store)
        for _ in range(30):
            retention.maintenance(
                store, tmp_path, max_bytes=MIB, referenced_job_ids={"pinned"},
                deadline=clock.now + 2.5,
            )
            if store.get_job("prunable") is None:
                break
        else:
            pytest.fail("rescanning pinned data prevented a later unpinned job from being pruned")

        assert store.get_job("pinned") is not None
        assert pinned_dir.exists()
        assert not prunable_dir.exists()


def test_unreadable_size_after_partial_removal_cannot_force_newer_deletion(tmp_path, monkeypatch):
    """A failed removal invalidates its old byte total when remaining bytes are unknown."""
    with Store(tmp_path / "state.sqlite3") as store:
        older = add_job(store, tmp_path, "older", order=0, size=10 * MIB, nested=False)
        newer = add_job(store, tmp_path, "newer", order=1, nested=False)
        original_remove = retention.shutil.rmtree
        original_size = retention._size
        removal_started = False

        def partial_remove(path):
            nonlocal removal_started
            if Path(path) == older:
                (older / "payload-0.bin").unlink()
                removal_started = True
                raise PermissionError("cannot finish removing directory")
            original_remove(path)

        def remaining_size(path, **kwargs):
            if Path(path) == older and removal_started:
                raise PermissionError("cannot measure directory after partial removal")
            return original_size(path, **kwargs)

        monkeypatch.setattr(retention.shutil, "rmtree", partial_remove)
        monkeypatch.setattr(retention, "_size", remaining_size)
        result = retention.maintenance(store, tmp_path, max_bytes=2 * MIB)

        assert result["pruned"] == []
        assert store.get_job("older") is not None
        assert store.get_job("newer") is not None
        assert newer.exists()
        assert result["bytes_after"] is None
        assert result["pools"]["detached"]["measured_bytes"] == MIB
        assert any(event["kind"] == "retention.remove_error" for event in store.list_events("older"))


def test_pin_added_after_removal_cannot_leave_stale_bytes_for_newer_jobs(tmp_path):
    """Keeping a newly pinned row must not count its already removed files twice."""
    with Store(tmp_path / "state.sqlite3") as store:
        older = add_job(store, tmp_path, "older", order=0, size=10 * MIB, nested=False)
        newer = add_job(store, tmp_path, "newer", order=1, nested=False)
        calls = 0

        def pins():
            nonlocal calls
            calls += 1
            return {"older"} if calls >= 3 else set()

        result = retention.maintenance(store, tmp_path, max_bytes=2 * MIB, pins=pins)

        assert result["pruned"] == []
        assert store.get_job("older") is not None
        assert not older.exists()
        assert store.get_job("newer") is not None
        assert newer.exists()
        assert "older" in result["protected"]


def test_removal_error_is_audited_even_when_remeasurement_hits_deadline(tmp_path, monkeypatch):
    """The failure event precedes a potentially interrupted remaining-byte walk."""
    with Store(tmp_path / "state.sqlite3") as store:
        older = add_job(store, tmp_path, "older", order=0, size=10 * MIB, nested=False)
        newer = add_job(store, tmp_path, "newer", order=1, nested=False)
        original_remove = retention.shutil.rmtree
        original_size = retention._size
        clock = Clock()
        removal_started = False

        def partial_remove(path):
            nonlocal removal_started
            if Path(path) == older:
                (older / "payload-0.bin").unlink()
                removal_started = True
                raise PermissionError("cannot finish removing directory")
            original_remove(path)

        def interrupted_size(path, **kwargs):
            if Path(path) == older and removal_started:
                clock.now = 10
            return original_size(path, **kwargs)

        monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=clock.monotonic))
        monkeypatch.setattr(retention.shutil, "rmtree", partial_remove)
        monkeypatch.setattr(retention, "_size", interrupted_size)
        result = retention.maintenance(store, tmp_path, max_bytes=2 * MIB, deadline=1)

        assert result["interrupted"] == "deadline"
        assert result["errors"][0]["job_id"] == "older"
        assert result["pruned"] == []
        assert store.get_job("older") is not None
        assert store.get_job("newer") is not None
        assert newer.exists()
        assert any(event["kind"] == "retention.remove_error" for event in store.list_events("older"))


def test_interrupted_read_only_git_check_keeps_completed_size_measurement(tmp_path, monkeypatch):
    """Git verification that expires a pass must not force the whole tree scan again."""
    with Store(tmp_path / "state.sqlite3") as store:
        directory = add_job(store, tmp_path, "job", order=0, nested=False)
        worktree = tmp_path / "worktrees" / "job"
        worktree.mkdir(parents=True)
        (worktree / "tracked").write_text("unchanged")
        store.update_job("job", sandbox="workspace-write", worktree=str(worktree))
        clock = slow_walk(monkeypatch, store)
        commands = []

        def git(path, *args, cancel=None, deadline=None, **kwargs):
            assert not store.connection.in_transaction
            retention._checkpoint(cancel, deadline)
            commands.append(args)
            if args[:2] == ("worktree", "list"):
                return f"worktree {worktree}\nHEAD {'0' * 40}\ndetached\n"
            if args[0] == "status":
                # This fits a fresh pass, but not a pass that just walked both
                # the job artifacts and the allocated worktree.
                clock.now += 2
                retention._checkpoint(cancel, deadline)
                return ""
            assert args[:2] == ("worktree", "remove")
            retention.shutil.rmtree(worktree)
            return ""

        monkeypatch.setattr(retention, "_git", git)
        for _ in range(5):
            result = retention.maintenance(store, tmp_path, max_jobs=0, deadline=clock.now + 2.5)
            if store.get_job("job") is None:
                break
        else:
            pytest.fail("read-only Git interruption discarded the completed scan every pass")

        assert result["pruned"] == ["job"]
        assert len([args for args in commands if args[0] == "status"]) == 2
        assert not directory.exists()
        assert not worktree.exists()
