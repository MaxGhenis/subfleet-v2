"""Retention pass invariants (C-8.4, C-26.12; design revision 2, section 3).

- J2 and recovery: a crash at any step leaves a job either whole (rows and files) or
  retired with a verified archive that restores it; the next pass finishes the rest.
- A1: bytes and job counts after a pass are the counts before, less what was pruned.
- Oldest first, minimal, pins held: pruning stops once a pool fits, never touches a
  pinned or live job, and never keeps an older prunable job while a newer one goes.
- G1: a deadline-limited walk resumes across passes; every such pass makes progress.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import retention
from subfleet import retention_archive as archive
from subfleet.contracts import Credential, Lane, LaneOwner, Exit
from subfleet.store import Store


class Crash(BaseException):
    """Process death at an injected point: nothing after it runs in this pass."""


def lane(store):
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                        "/home/one", LaneOwner.V2, False))


def add(store, root, identity, *, order, size=10, kind="dispatch", state="succeeded", files=None):
    fields = dict(job_id=identity, request_id=identity, payload_digest="digest", kind=kind, workdir=str(root),
                  prompt_path="/prompt", sandbox="read-only", state=state,
                  created_at=f"2026-01-01T00:{order // 60:02d}:{order % 60:02d}Z", finished_at="2026-01-02T00:00:00Z")
    if kind == "turn":
        fields.update(request_id=f"turn:{identity}:0", name="turn-cv-1", in_place=1, max_attempts=1)
    store.add_job(**fields)
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    for name, data in (files or {"stdout": b"x" * size}).items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return directory


def tree(directory: Path) -> dict:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# J2 and recovery: crash injection at every step
# ---------------------------------------------------------------------------

CRASH_POINTS = ["walk", "rows", "archive-rename", "load", "commit", "delete", "mid-delete", "release"]


def arm(monkeypatch, point, root):
    """Make the named step raise Crash the first time it runs."""
    fired = []

    def once(original, *, after=0):
        count = [0]

        def wrapper(*args, **kwargs):
            count[0] += 1
            if not fired and count[0] > after:
                fired.append(point)
                raise Crash(point)
            return original(*args, **kwargs)
        return wrapper

    if point == "walk":
        monkeypatch.setattr(archive, "_add_file", once(archive._add_file))
    elif point == "rows":
        monkeypatch.setattr(archive, "write_file", once(archive.write_file))
    elif point == "archive-rename":
        original = os.rename

        def rename(src, dst, *args, **kwargs):
            if not fired and str(src).find(archive.PARTIAL_PREFIX) >= 0:
                fired.append(point)
                raise Crash(point)
            return original(src, dst, *args, **kwargs)
        monkeypatch.setattr(archive.os, "rename", rename)
    elif point == "load":
        monkeypatch.setattr(archive, "load", once(archive.load))
    elif point == "commit":
        monkeypatch.setattr(archive, "rows_snapshot", once(archive.rows_snapshot, after=1))
    elif point == "delete":
        monkeypatch.setattr(archive, "delete_archived", once(archive.delete_archived))
    elif point == "mid-delete":
        monkeypatch.setattr(archive.os, "unlink", once(os.unlink, after=1))
    elif point == "release":
        monkeypatch.setattr(retention, "_release", once(retention._release))
    return fired


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_j2_a_crash_at_any_step_leaves_the_job_whole_or_archived(tmp_path, monkeypatch, point):
    """J2, 6.3: rows present means the directory is untouched; rows gone means a verified
    archive restores it. The next pass finishes, and no lease is left behind."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        files = {"stdout": b"out" * 50, "a1/transcript.jsonl": b'{"x": 1}\n' * 20, "a1/deliverable.md": b"# done"}
        directory = add(store, tmp_path, "job", order=0, files=files)
        original = tree(directory)
        fired = arm(monkeypatch, point, tmp_path)
        with pytest.raises(Crash):
            retention.maintenance(store, tmp_path, max_jobs=0)
        assert fired == [point]
        if store.get_job("job") is not None:
            assert tree(directory) == original                     # J2: nothing deleted yet
        else:
            restored = tmp_path / "restored"
            archive.restore(tmp_path / "archive" / "job", restored)
            assert tree(restored) == original
        monkeypatch.undo()
        retention.maintenance(store, tmp_path, max_jobs=0)          # recovery, then the rest
        retention.maintenance(store, tmp_path, max_jobs=0)
        assert store.get_job("job") is None
        assert not directory.exists()
        assert not (tmp_path / "retention-conflicts").exists()
        assert store.list_leases() == []
        assert [name for name in os.listdir(tmp_path / "archive")] == ["job"]
        final = tmp_path / "archive" / "job"
        archive.verify(final)
        again = tmp_path / "restored-again"
        archive.restore(final, again)
        assert tree(again) == original


def test_recovery_never_deletes_a_directory_without_a_verified_archive(tmp_path):
    """6.3 row 3: rows gone (a hand-edited store) and no archive: the directory is kept."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        directory = add(store, tmp_path, "job", order=0)
        store.acquire_lease("retire:job", "retention:job")
        with store.transaction() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id='job'")
        result = retention.maintenance(store, tmp_path, max_jobs=0)
        kept = tmp_path / "retention-conflicts" / "job"
        assert tree(kept) == {"stdout": b"x" * 10}
        assert not directory.exists()
        assert result["conflicts"] and store.list_leases() == []


def test_recovery_leaves_an_older_archive_of_another_request_alone(tmp_path):
    """6.3: an archive at the final name is discarded only if it names this job's request."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        add(store, tmp_path, "job", order=0)
        older = archive.archive(tmp_path / "missing", tmp_path / "archive", "job",
                                {"job_id": "job", "rows": {"jobs": [{"request_id": "an-older-job"}]},
                                 "sha256": hashlib.sha256(archive.canonical({"jobs": [{"request_id": "an-older-job"}]})).hexdigest()},
                                free_floor=0)
        store.acquire_lease("retire:job", "retention:job")
        retention.maintenance(store, tmp_path, max_jobs=10)
        assert (older / "manifest.json").is_file()
        assert store.list_leases() == []


def test_r1_a_row_written_after_the_archive_blocks_the_commit(tmp_path, monkeypatch):
    """R1: the commit re-reads the rows; any change since the archive keeps the job."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        directory = add(store, tmp_path, "job", order=0)
        original = archive.archive

        def archive_then_write(*args, **kwargs):
            final = original(*args, **kwargs)
            store.add_notice("job", "written late", None)         # no session: pins nothing
            return final

        monkeypatch.setattr(archive, "archive", archive_then_write)
        result = retention.maintenance(store, tmp_path, max_jobs=0)
        assert result["pruned"] == [] and store.get_job("job") is not None
        assert directory.exists() and not (tmp_path / "archive" / "job").exists()
        assert store.list_leases() == []
        monkeypatch.undo()
        assert retention.maintenance(store, tmp_path, max_jobs=0)["pruned"] == ["job"]
        rows = json.loads((tmp_path / "archive" / "job" / "rows.json").read_bytes())["rows"]
        assert [notice["text"] for notice in rows["notices"]] == ["written late"]


def test_a_pin_that_appears_mid_pass_keeps_the_job(tmp_path, monkeypatch):
    """P1: pins are asked again in the commit transaction; a late pin leaves everything."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        directory = add(store, tmp_path, "job", order=0)
        original = archive.archive

        def archive_then_pin(*args, **kwargs):
            final = original(*args, **kwargs)
            store.add_notice("job", "unread", "session-1")
            return final

        monkeypatch.setattr(archive, "archive", archive_then_pin)
        result = retention.maintenance(store, tmp_path, max_jobs=0)
        assert result["pruned"] == [] and "job" in result["protected"]
        assert tree(directory) == {"stdout": b"x" * 10}
        assert not (tmp_path / "archive" / "job").exists() and store.list_leases() == []


# ---------------------------------------------------------------------------
# A1, oldest first, minimal, pins held
# ---------------------------------------------------------------------------

JOBS = st.lists(st.tuples(st.sampled_from(["dispatch", "turn"]), st.integers(0, 400),
                          st.sampled_from(["succeeded", "succeeded", "failed", "running", "pinned"])),
                max_size=10)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(jobs=JOBS, max_jobs=st.integers(0, 6), max_bytes=st.integers(0, 2000),
       turn_max_jobs=st.integers(0, 6), turn_max_bytes=st.integers(0, 2000))
def test_a1_accounting_order_minimality_and_pins(tmp_path_factory, jobs, max_jobs, max_bytes,
                                                 turn_max_jobs, turn_max_bytes):
    root = tmp_path_factory.mktemp("pool")
    with Store(root / "state.sqlite3") as store:
        lane(store)
        info = {}
        for order, (kind, size, state) in enumerate(jobs):
            identity = f"j{order:02d}"
            add(store, root, identity, order=order, size=size, kind=kind,
                state="running" if state == "running" else "succeeded")
            if state == "pinned":
                store.add_notice(identity, "unread", "session-1")
            info[identity] = {"pool": "turn" if kind == "turn" else "detached", "size": size, "order": order,
                              "prunable": state in ("succeeded", "failed")}
        budgets = {"detached": (max_jobs, max_bytes), "turn": (turn_max_jobs, turn_max_bytes)}
        result = retention.maintenance(store, root, max_jobs=max_jobs, max_bytes=max_bytes,
                                       turn_max_jobs=turn_max_jobs, turn_max_bytes=turn_max_bytes, turn_keep_s=0)
        pruned = result["pruned"]
        assert "interrupted" not in result
        assert all(info[job]["prunable"] for job in pruned)
        assert result["bytes_after"] == result["bytes_before"] - sum(info[job]["size"] for job in pruned)
        assert result["jobs_after"] == len(jobs) - len(pruned)
        for pool, (limit_jobs, limit_bytes) in budgets.items():
            members = [job for job in info if info[job]["pool"] == pool]
            gone = [job for job in pruned if info[job]["pool"] == pool]
            left = [job for job in members if job not in gone]
            count, size = len(left), sum(info[job]["size"] for job in left)
            assert result["pools"][pool]["jobs_after"] == count and result["pools"][pool]["bytes_after"] == size
            prunable_left = [job for job in left if info[job]["prunable"]]
            if count > limit_jobs or size > limit_bytes:
                assert prunable_left == []                        # over budget only with nothing left to prune
            if gone:
                assert max(info[job]["order"] for job in gone) < min(
                    (info[job]["order"] for job in prunable_left), default=10 ** 9)   # oldest first
                last = max(gone, key=lambda job: info[job]["order"])
                assert count + 1 > limit_jobs or size + info[last]["size"] > limit_bytes   # minimal
        for job in info:
            assert (root / "jobs" / job).exists() == (job not in pruned)
            assert (root / "archive" / job).exists() == (job in pruned)
        assert store.list_leases() == []


# ---------------------------------------------------------------------------
# G1: progress under a deadline
# ---------------------------------------------------------------------------

def test_g1_a_walk_cut_by_the_deadline_resumes_and_each_pass_progresses(tmp_path, monkeypatch):
    """G1, section 5: sizing a big tree takes several passes; each one moves it on and says
    so, none raises, and pruning follows once the size is known."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        files = {f"d{index:03d}/f": b"x" * 10 for index in range(60)}
        add(store, tmp_path, "big", order=0, files=files)
        add(store, tmp_path, "new", order=1, size=1)
        clock = [1000.0]

        def tick():
            clock[0] += 1.0
            return clock[0]
        monkeypatch.setattr(retention.time, "monotonic", tick)
        state = retention.RetentionState(clock=lambda: clock[0])
        passes = []
        for _ in range(40):
            result = retention.maintenance(store, tmp_path, max_jobs=10, max_bytes=100, state=state,
                                           deadline=clock[0] + 40)
            passes.append(result)
            if result["pruned"]:
                break
        assert passes[-1]["pruned"] == ["big"], passes[-1]
        assert len(passes) > 2                                  # the walk really was split
        assert all(p["made_progress"] for p in passes)
        assert all(p.get("interrupted") == "deadline" for p in passes[:-1])
        assert passes[-1]["bytes_before"] == 601


def test_g1_deferred_candidates_let_the_next_one_through(tmp_path, monkeypatch):
    """R4-3 (the livelock): a candidate that cannot be archived is deferred with its
    reason, and the next oldest job is retired in the same pass."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        add(store, tmp_path, "stuck", order=0)
        add(store, tmp_path, "next", order=1)
        original = archive.archive

        def refuse_stuck(job_dir, *args, **kwargs):
            if job_dir.name == "stuck":
                raise archive.Unarchivable("an entry on another device")
            return original(job_dir, *args, **kwargs)

        monkeypatch.setattr(archive, "archive", refuse_stuck)
        state = retention.RetentionState()
        result = retention.maintenance(store, tmp_path, max_jobs=1, state=state)
        assert result["pruned"] == [] or result["pruned"] == ["next"]
        assert "another device" in result["deferred"]["stuck"]
        second = retention.maintenance(store, tmp_path, max_jobs=0, state=state)
        assert "stuck" in second["deferred"] and store.get_job("next") is None


def test_the_cache_sees_a_live_job_grow_and_a_job_gain_a_worktree(tmp_path):
    """Review probes (cache killers) carried over: live sizes are re-measured and a new
    worktree changes what is sized, even with state kept between passes."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        add(store, tmp_path, "old", order=0, size=1024)
        live = add(store, tmp_path, "live", order=1, size=1024, state="running")
        store.add_attempt(attempt_id="live/a1", job_id="live", seq=1, lane_id="codex-1",
                          model_requested="m", state="running")
        state = retention.RetentionState()
        assert retention.maintenance(store, tmp_path, max_bytes=3000, state=state)["pruned"] == []
        (live / "stdout").write_bytes(b"x" * 3000)
        state.sizes["live"] = (state.sizes["live"][0], state.sizes["live"][1], -10 ** 9)   # past its 10 min
        assert retention.maintenance(store, tmp_path, max_bytes=3000, state=state)["pruned"] == ["old"]


# ---------------------------------------------------------------------------
# policy, resume fence, CLI
# ---------------------------------------------------------------------------

def test_cli_lists_and_restores_an_archived_job(tmp_path, monkeypatch, capsys):
    """C-17.1: `retention archives` and `retention restore` work offline from the archive."""
    from subfleet import cli
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path))
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        add(store, tmp_path, "job", order=0, files={"stdout": b"hello", "a1/out.md": b"# result"})
        store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1", model_requested="m",
                          state="succeeded")
        store.add_artifact("job/a1", "salvage", "refs/subfleet-salvage/x-a1", "digest", 0)
        assert retention.maintenance(store, tmp_path, max_jobs=0)["pruned"] == ["job"]
    assert cli.main(["retention", "archives", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)["archives"]
    assert [row["job_id"] for row in listing] == ["job"] and listing[0]["salvage"] == ["refs/subfleet-salvage/x-a1"]
    assert cli.main(["retention", "restore", "job", "--check"]) == 0
    assert cli.main(["retention", "restore", "job"]) == 0
    assert (tmp_path / "jobs" / "job" / "a1" / "out.md").read_bytes() == b"# result"
    assert cli.main(["retention", "restore", "job"]) == int(Exit.INVALID_INPUT)      # exists now
    assert cli.main(["retention", "restore", "../etc"]) == int(Exit.INVALID_INPUT)
    assert cli.main(["retention", "restore", "job", "--to", str(tmp_path / "elsewhere")]) == 0
    assert (tmp_path / "elsewhere" / "job" / "stdout").read_bytes() == b"hello"


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(jobs=JOBS, max_jobs=st.integers(0, 6), max_bytes=st.integers(0, 2000))
def test_a_dry_run_names_exactly_what_the_pass_then_prunes_and_writes_nothing(tmp_path_factory, jobs, max_jobs,
                                                                           max_bytes):
    """Differential: `dry_run` (used by `retention preview`) and a real pass agree, and the
    dry run changes neither the store nor the filesystem."""
    root = tmp_path_factory.mktemp("preview")
    with Store(root / "state.sqlite3") as store:
        lane(store)
        for order, (kind, size, state) in enumerate(jobs):
            add(store, root, f"j{order:02d}", order=order, size=size, kind=kind,
                state="running" if state == "running" else "succeeded")
            if state == "pinned":
                store.add_notice(f"j{order:02d}", "unread", "session-1")
        rows_before = {table: store.query(f"SELECT * FROM {table}") for table in ("jobs", "leases", "events", "notices")}
    before = sorted(str(p) for p in root.rglob("*") if "state.sqlite3" not in p.name)
    with Store(root / "state.sqlite3", read_only=True) as reader:
        preview = retention.maintenance(reader, root, max_jobs=max_jobs, max_bytes=max_bytes, turn_keep_s=0,
                                        dry_run=True)
    assert sorted(str(p) for p in root.rglob("*") if "state.sqlite3" not in p.name) == before
    with Store(root / "state.sqlite3") as store:
        assert {table: store.query(f"SELECT * FROM {table}") for table in rows_before} == rows_before
        result = retention.maintenance(store, root, max_jobs=max_jobs, max_bytes=max_bytes, turn_keep_s=0)
    assert [row["job_id"] for row in preview["would_retire"]] == result["pruned"]



def test_g1_a_pass_whose_candidates_are_all_deferred_still_progresses(tmp_path, monkeypatch):
    """Review of rev 3, finding 1: 200 candidates all deferred (too little free space) are
    progress, not a timeout; the next pass offers the jobs behind them."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        for order in range(230):
            add(store, tmp_path, f"j{order:03d}", order=order)
        monkeypatch.setattr(archive.shutil, "disk_usage", lambda path: type("U", (), {"free": 10 ** 9})())
        state = retention.RetentionState()
        first = retention.maintenance(store, tmp_path, max_jobs=0, state=state)
        assert first["interrupted"] == "capped" and first["made_progress"] and first["pruned"] == []
        assert len(first["newly_deferred"]) == 200
        second = retention.maintenance(store, tmp_path, max_jobs=0, state=state)
        assert second["made_progress"] and len(second["newly_deferred"]) == 30 and "interrupted" not in second
        monkeypatch.undo()
        third = retention.maintenance(store, tmp_path, max_jobs=0, state=state)
        assert third["pruned"] == [] and "interrupted" not in third        # all deferred for a day


def test_g1_classification_is_bounded_by_the_deadline(tmp_path, monkeypatch):
    """Review of rev 3, finding 5: reading repositories stops at its share of the deadline;
    the rest wait for a later pass, kept meanwhile."""
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        root = tmp_path
        (root / "worktrees").mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        for order in range(3):
            identity = f"w{order}"
            store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                          workdir=str(repo), worktree=str(root / "worktrees" / identity), prompt_path="/p",
                          sandbox="workspace-write", state="succeeded", finished_at="2026-01-02T00:00:00Z",
                          created_at=f"2026-01-01T00:00:0{order}Z")
            (root / "jobs" / identity).mkdir(parents=True)
        clock = [100.0]
        monkeypatch.setattr(retention.time, "monotonic", lambda: clock[0])

        def slow(workdir):
            clock[0] += 50.0
            return False
        monkeypatch.setattr(retention, "_common_dir", slow)
        result = retention.maintenance(store, root, max_jobs=0, deadline=clock[0] + 60)
        assert any("later pass" in reason for reason in result["kept"].values())
