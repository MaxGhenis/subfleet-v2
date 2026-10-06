"""C-8.4, C-6.5, C-6.11: a detached job that takes no lease on its folder waits while
retention retires a tree that folder is in.

A read-only detached job takes no folder lease, and a writer that is not in place
takes one only on the worktree the daemon cuts for it, so reservation checked no
fence for either: submitted after retention's selection with its workdir in a
finished job's tree (the tree itself, a folder in it, or a repository nested in
it), such a job was reserved while retention held `worktree:<tree>` (probe in the
evidence of the turn and writer fixes: `'live': True`, `'hold': None`). The
quarantine then moved the tree from under it. The commit rolled the retirement
back while the job was active (`worktree-in-use`, #76), but only after the move.
A writer in a nested repository had worse: preparing its workspace ran
`git worktree add` there, which registered its worktree inside the tree being
archived (N4, `nested-host`).

Submit now records the folder such a job works in, spelled once (`folder`,
`folders.canonical`), and admission reads retention's fence on it and on every
folder above it (`folders.retiring`): before preparing the workspace, so no git
runs in the tree, and again in the reserving transaction, by string operations
alone. The hold names the fence as retention's (`retiring`) and the job's folder,
and `why` says retention is removing that tree (C-6.11). These run the daemon's
own admission in-process against the archive driver's own retirement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import folders, render, retention
from subfleet import retention_archive as rarch
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_admission_liveness import _end, _live
from tests.fake.test_writer_retention_fence import checkout, lease, look, measured
from tests.unit.retention_world import snapshot
from tests.unit.test_retention_shared_folders import nested_repository, retiring_tree

#: The two shapes: a read-only job, and a writer whose worktree the daemon cuts (C-6.6).
SHAPES = {"read-only": {"sandbox": "read-only"}, "worktree": {"sandbox": "workspace-write"}}


def detached_in(daemon, harness, workdir, shape: str, **changes) -> str:
    """A detached job of `shape` submitted with -C `workdir`, as `subfleet run` submits it."""
    return submit(daemon, harness, workdir=str(workdir), **SHAPES[shape], **changes)


def where_in(tree: str, nested: str, where: str) -> str:
    """The workdir: the tree itself, a folder inside it, or a repository nested in it."""
    if where == "sub":
        sub = Path(tree) / "pkg"
        sub.mkdir(exist_ok=True)
        return str(sub)
    return tree if where == "tree" else nested


def registrations(nested: str) -> list[str]:
    """The worktrees registered in the nested repository's own `.git` (N4)."""
    admin = Path(nested) / ".git" / "worktrees"
    return sorted(os.listdir(admin)) if admin.is_dir() else []


def recorded(daemon, job_id: str) -> dict:
    return daemon._submitted(job_id)


class Workspaces:
    """`Daemon._workspace`, counted per job: the real one, or a stand-in that touches nothing."""

    def __init__(self, daemon, patch, *, real: bool):
        self.calls: list[str] = []
        self.real = daemon._workspace
        self.daemon = daemon
        self.use_real = real
        patch.setattr(daemon, "_workspace", self)

    def __call__(self, job):
        self.calls.append(job["job_id"])
        if self.use_real:
            return self.real(job)
        if job["sandbox"] == "workspace-write" and not job["in_place"]:
            return str(self.daemon.root / "worktrees" / job["job_id"]), None, None, []
        return job["workdir"], None, None, []


@pytest.mark.parametrize("where", ["tree", "sub", "nested"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c8_4_a_detached_job_submitted_under_retentions_fence_waits_for_it(tmp_path, shape, where):
    """The probe, made a test. While retention holds `worktree:<tree>` (after the
    selecting transaction, before `begin` reads or moves the tree), a detached job is
    submitted with its workdir in the tree. Submission accepts it and records its
    folder. Admission holds it `lease-held` on the fence before preparing its
    workspace: no git runs for it, and a writer's worktree is not registered in the
    nested repository. The hold names the fence as retention's and the job's folder,
    and `why` says retention is removing that tree. Once the retirement lets go, the
    next detached pass places it without waiting out its clock (C-6.10), and only
    then is a writer's worktree cut.
    Failed on 5253faa2: reserved under the fence (`'live': True`, no hold)."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        workdir = where_in(tree, nested, where)
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=True)
        seen = {}

        def begin(retirement, job, pool):
            seen["fence"] = lease(daemon, fence)
            seen["job"] = job_id = detached_in(daemon, harness, workdir, shape)
            daemon._admit()
            seen.update(live=_live(daemon, job_id), hold=dict(daemon._holds.get(job_id) or {}),
                        prepared=list(workspaces.calls), registered=registrations(nested),
                        row=dict(daemon._job(job_id)), why=daemon._why_job(daemon._job(job_id))["text"])
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
        job_id = seen["job"]
        folder = folders.canonical(workdir)
        assert seen["fence"] == "retention:retired", seen
        assert not seen["live"] and seen["prepared"] == [] and seen["registered"] == [], seen
        assert recorded(daemon, job_id)["folder"] == folder
        assert seen["hold"] == {"reason": "lease-held", "leases": [fence], "retiring": [fence], "folder": folder,
                                "next_check_at": seen["row"]["next_check_at"]}, seen
        assert seen["row"]["state"] == "waiting" and seen["row"]["wait_reason"] == "capacity", seen["row"]
        said = (f", its folder ({tree})" if folder == tree else f" that its folder {folder} is in ({tree})")
        assert f"Held: retention is removing a finished job's worktree{said}; it waits until retention lets go " \
               "of it (C-8.4)" in seen["why"], seen["why"]
        assert "held by another job" not in seen["why"], seen["why"]
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        daemon._admit()                                 # the fence went: freed capacity, looked at now
        assert _live(daemon, job_id), daemon._holds.get(job_id)
        assert workspaces.calls == [job_id]
        if shape == "worktree":
            assert registrations(nested) == ([job_id] if where == "nested" else [])
            assert lease(daemon, folders.exclusive_key(str(daemon.root / "worktrees" / job_id))) == job_id
        else:
            assert daemon.store.one("SELECT 1 FROM leases WHERE lease_key LIKE 'worktree%' AND holder=?",
                                    (job_id,)) is None      # a read-only job takes no folder lease


@pytest.mark.parametrize("where", ["tree", "nested"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c8_4_a_detached_job_waits_out_a_retirement_and_runs_once_the_fence_goes(tmp_path, shape, where):
    """Liveness, end to end, with the daemon's own workspace preparation: a detached job
    submitted in a job's tree after selection is looked at again at each step of the
    retirement (begin, lock, quarantine, archive, the final check), its tree moved away
    from the quarantine on, and is never reserved and never prepared. Its queued job
    keeps the tree at the commit (`worktree-in-use`, #76), so the retirement is rolled
    back, the tree comes back exactly as it was (no byte or time in it moved, and a
    writer's worktree was not registered there; that the workspace was never prepared
    is `Workspaces.calls`, since a git that only reads moves nothing), the fence goes,
    and the next detached pass runs the job.
    Failed on 5253faa2: reserved at the first look, live while its tree was in quarantine."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        workdir = where_in(tree, nested, where)
        fence = folders.exclusive_key(tree)
        before = snapshot(Path(tree))
        workspaces = Workspaces(daemon, patch, real=True)
        state = {"looked": []}

        def step(at):
            if "job" not in state:
                state["job"] = detached_in(daemon, harness, workdir, shape)
            hold = look(daemon, state["job"])
            assert not _live(daemon, state["job"]) and workspaces.calls == [], (at, hold)
            assert hold.get("reason") == "lease-held" and hold["leases"] == hold["retiring"] == [fence], (at, hold)
            state["looked"].append((at, Path(workdir).is_dir()))

        for method in ("begin", "lock", "quarantine", "archive", "final_check", "reclaim"):
            def wrapped(self, *args, _real=getattr(rarch.Retirement, method), _at=method, **kwargs):
                step(_at)
                return _real(self, *args, **kwargs)
            patch.setattr(rarch.Retirement, method, wrapped)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert list(dict.fromkeys(state["looked"])) == [
            ("begin", True), ("lock", True), ("quarantine", True), ("archive", False), ("final_check", False)], state
        assert "retired" in result["protected"] and "retired" not in result["pruned"], result
        assert snapshot(Path(tree)) == before
        assert daemon.store.get_job("retired")
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        daemon._admit()
        job_id = state["job"]
        assert _live(daemon, job_id), daemon._holds.get(job_id)
        assert workspaces.calls == [job_id]
        _end(daemon, job_id)
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder=?", (job_id,)) is None


def test_c8_4_no_git_runs_for_an_in_place_writer_held_on_a_fence_above(tmp_path):
    """A detached writer in place in a repository nested in a tree being retired is held
    on the fence above its write target (5253faa2's transaction check), and now before
    its workspace is prepared: its start snapshot (`working_tree`, which refreshes the
    nested repository's index and freshens its objects) does not run in the tree while
    the fence is held. Its hold names the fence as retention's and its write target as
    its folder. It runs once the fence goes.
    Failed on 5253faa2: held, but only after `_workspace` ran git in the nested repository."""
    from tests.fake.test_writer_retention_fence import writer_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=True)
        writer = writer_in(daemon, harness, nested)
        before = snapshot(Path(tree))
        assert daemon.store.acquire_lease(fence, "retention:retired")
        hold = look(daemon, writer)
        assert not _live(daemon, writer) and workspaces.calls == [], hold
        assert {key: hold[key] for key in ("reason", "leases", "retiring", "folder")} == {
            "reason": "lease-held", "leases": [fence], "retiring": [fence], "folder": nested}, hold
        assert snapshot(Path(tree)) == before
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)
        assert workspaces.calls == [writer] and lease(daemon, folders.exclusive_key(nested)) == writer


def test_c8_4_while_the_fence_is_held_it_is_the_whole_hold_and_the_next_look_finds_the_rest(tmp_path):
    """The intended difference from the transaction's hold, as for turns: while
    retention's fence covers a detached job's folder, the look before its workspace is
    prepared holds it on the fence alone, whatever else it would also wait for. A
    writer in place in a nested repository where a writable turn is working is held on
    the fence above (`retiring`), not on the turn's row; once the fence goes, the next
    look finds the row, a lease another job holds (no `retiring`, and `why` says so);
    once the turn ends, the writer runs."""
    from tests.fake.test_admission_liveness import _turn_in
    from tests.fake.test_writer_retention_fence import writer_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        Workspaces(daemon, patch, real=False)
        writer = writer_in(daemon, harness, nested)        # queued: no lease yet, so the turn may start
        turn = _turn_in(daemon, harness, 0, workdir=nested)
        daemon._admit_turns()
        assert _live(daemon, turn)
        row = folders.turn_key(nested, turn, writable=True)
        fence = folders.exclusive_key(tree)
        assert daemon.store.acquire_lease(fence, "retention:retired")
        fenced = look(daemon, writer)
        assert not _live(daemon, writer)
        assert {key: fenced.get(key) for key in ("leases", "retiring", "folder")} == {
            "leases": [fence], "retiring": [fence], "folder": nested}, fenced
        daemon.store.release_leases("retention:retired")
        rest = look(daemon, writer)
        assert not _live(daemon, writer) and rest["leases"] == [row] and "retiring" not in rest, rest
        assert f"a lease this job needs is held by another job: {row}" in daemon._why_job(daemon._job(writer))["text"]
        _end(daemon, turn)
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)


def test_c6_11_a_worktree_lease_another_job_holds_is_never_called_retentions(tmp_path):
    """`retiring` names only retention's fences: a writable turn waiting for a detached
    writer's `worktree:` on its folder is held on that key with no `retiring`, and `why`
    says another job holds it, never that retention is removing a worktree. Passes on
    5253faa2 (no `retiring` at all); fails a fix that counts any `worktree:` holder."""
    from tests.fake.test_admission_liveness import _turn_in
    from tests.fake.test_writer_retention_fence import writer_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        Workspaces(daemon, patch, real=False)
        writer = writer_in(daemon, harness, harness.workdir)
        daemon._admit()
        assert _live(daemon, writer)
        target = folders.exclusive_key(folders.canonical(harness.workdir))
        turn = _turn_in(daemon, harness, 0, workdir=harness.workdir)
        daemon._admit_turns()
        hold = dict(daemon._holds.get(turn) or {})
        assert not _live(daemon, turn) and hold["leases"] == [target] and "retiring" not in hold, hold
        text = daemon._why_job(daemon._job(turn))["text"]
        assert f"a lease this job needs is held by another job: {target}" in text and "retention" not in text, text


def test_c8_4_a_fence_on_an_in_place_writers_own_folder_after_the_early_look_is_named_once(tmp_path):
    """The transaction's own read for an in-place writer is the writer fix's: its own
    `worktree:` key, contested, and the fences above it. A fence on its own folder that
    lands after the look before its workspace is named once in its hold, as retention's,
    with its write target as its folder; the transaction's check for a job that takes no
    folder lease is not also applied to it."""
    from tests.fake.test_writer_retention_fence import writer_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        own = folders.exclusive_key(nested)
        workspaces = Workspaces(daemon, patch, real=False)

        def lands(job):
            assert daemon.store.acquire_lease(own, "retention:other")
            return workspaces(job)

        writer = writer_in(daemon, harness, nested)
        patch.setattr(daemon, "_workspace", lands)
        hold = look(daemon, writer)
        assert not _live(daemon, writer)
        assert {key: hold.get(key) for key in ("leases", "retiring", "folder")} == {
            "leases": [own], "retiring": [own], "folder": nested}, hold


@pytest.mark.parametrize("where", ["tree", "nested"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c8_4_a_fence_taken_after_the_early_look_holds_the_job_in_its_transaction(tmp_path, shape, where):
    """The reserving transaction's own read decides. A fence that lands after the look
    before workspace preparation (retention's selection saw no queued job in the tree:
    `worktree-in-use` compares recorded spellings) holds the job in the transaction,
    on the folder submit recorded, with the same hold the early look records: the
    same keys, `retiring`, and the job's folder. The next look holds it before its
    workspace is prepared.
    Failed on 5253faa2: reserved under the fence."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        workdir = where_in(tree, nested, where)
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=False)
        fake = workspaces.__call__

        def selection_lands(job):
            assert daemon.store.acquire_lease(fence, "retention:retired")    # between the two reads
            return fake(job)

        patch.setattr(daemon, "_workspace", selection_lands)
        job_id = detached_in(daemon, harness, workdir, shape)
        late = look(daemon, job_id)
        assert not _live(daemon, job_id), late
        expected = {"reason": "lease-held", "leases": [fence], "retiring": [fence], "folder": folders.canonical(workdir)}
        assert {key: late.get(key) for key in expected} == expected, late
        assert set(late) <= {*expected, "next_check_at"}, late
        patch.setattr(daemon, "_workspace", workspaces)
        assert workspaces.calls == [job_id]                 # prepared once, before the fence landed
        del workspaces.calls[:]
        early = look(daemon, job_id)
        assert {key: early.get(key) for key in expected} == expected and workspaces.calls == [], early
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, job_id), daemon._holds.get(job_id)


@pytest.mark.parametrize("read", ["early", "transaction"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c6_10_a_fence_found_after_the_census_brings_the_next_look_forward_when_it_goes(tmp_path, shape, read):
    """A pass reads its lease census when it begins (`_leases_seen`), and a backed-off
    capacity wait is looked at at once on the next pass only when a lease it saw has
    gone. Retention's fence can be taken after that read and found by the look before
    the workspace (`early`) or by the reserving transaction (`transaction`); either
    counts it as seen, so once retention lets go the next detached pass places the
    job, however far its clock is set.
    Failed on 23a7f1b1 (review P3): the fence was never seen, nothing came free, and
    the job waited out its clock."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=False)
        job_id = detached_in(daemon, harness, nested, shape)
        taken = []

        def take():
            if not taken:
                assert daemon.store.acquire_lease(fence, "retention:retired")
                taken.append(fence)

        if read == "early":
            real = daemon._detached_folder
            patch.setattr(daemon, "_detached_folder", lambda job: (take(), real(job))[1])
        else:
            patch.setattr(daemon, "_workspace", lambda job: (take(), workspaces(job))[1])
        hold = look(daemon, job_id)                       # the census was read before the fence was taken
        assert taken and not _live(daemon, job_id) and hold["retiring"] == [fence], hold
        assert (fence, "retention:retired") in daemon._leases_seen["detached"]
        daemon.store.update_job(job_id, next_check_at="2099-01-01T00:00:00Z")   # backed off as far as it goes
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, job_id), daemon._holds.get(job_id)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c6_11_a_fenced_job_on_its_clock_never_queues_the_fence(tmp_path, shape):
    """C-6.9, C-6.11: the look before the workspace records retention's fences as keys
    the job needs free but does not take (`blocked`), as the transaction does since the
    writer fix's review (#140, P3), so a look on the job's clock never queues for them.
    An older detached job in `<tree>/vendor/lib` rests on its clock, held on the fence on
    `<tree>`; retention lets go mid-pass; a newer writer in place on `<tree>`, whose own
    key the fence was, takes it on that pass instead of waiting `queued_behind` a job
    that never takes it. Rows are written directly: submission refuses a writer on a
    fenced tree, and the queue is what is under test.
    Failed with `_fence_hold` passing no `blocked` (the merge of #139 and #140 alone)."""
    from subfleet.daemon import after
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        Workspaces(daemon, patch, real=False)
        tree = str(harness.root / "worktrees" / "retired")
        nested, fence = tree + "/vendor/lib", folders.exclusive_key(tree)

        def add(job_id, folder, **values):
            daemon.store.add_job(job_id=job_id, request_id=job_id, payload_digest=job_id, kind="dispatch",
                                 workdir=folder, prompt_path=str(harness.root / "prompt.md"), pinned_model="astra",
                                 max_attempts=1, max_wall_s=3600, **values)
            return job_id

        older = add("older", nested, **SHAPES[shape])
        assert daemon.store.acquire_lease(fence, "retention:retired")
        daemon._admit()
        assert not _live(daemon, older) and daemon._holds[older]["leases"] == [fence]
        assert daemon._capacity_waits[older]["blocked"] == (fence,)
        daemon.store.update_job(older, next_check_at=after(3600))
        newer = add("newer", tree, sandbox="workspace-write", in_place=True)
        real = daemon._detached_folder

        def finishes(job):          # retention lets go after the older's look, before the newer's
            if job["job_id"] == newer:
                daemon.store.release_leases("retention:retired")
            return real(job)

        patch.setattr(daemon, "_detached_folder", finishes)
        daemon._admit()
        assert lease(daemon, fence) == newer, daemon._holds.get(newer)
        assert "queued_behind" not in (daemon._holds.get(newer) or {})


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c8_4_a_job_queued_before_folders_were_recorded_keeps_its_spelling_through_the_quarantine(tmp_path, shape):
    """A job queued by a daemon that recorded no `folder`, its workdir typed in another
    case than the volume stores (`…/rETIRED/vendor/lib`), is spelled at its first look,
    while the tree is there, and that spelling is kept (`job.folder_spelled`). Looked at
    again while retention has the tree in quarantine, it is still held on the fence and
    its workspace is not prepared; spelled afresh then, the missing names would keep
    their typed case and miss the fence. The retirement is rolled back (`worktree-in-use`
    reads the queued job's workdir) and the job runs once the fence goes.
    Failed on 23a7f1b1 (review P2): reserved while the tree was away."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        typed = nested.replace("/worktrees/retired/", "/worktrees/rETIRED/")
        if not os.path.isdir(typed) or not os.path.samefile(typed, nested):
            pytest.skip("this volume does not fold case")
        fence = folders.exclusive_key(tree)
        workspaces = Workspaces(daemon, patch, real=True)
        state = {"looked": []}

        def step(at):
            if "job" not in state:
                state["job"] = job_id = detached_in(daemon, harness, typed, shape)
                with daemon.store.transaction("test.legacy") as tx:     # as a daemon before `folder` wrote it
                    row = tx.execute("SELECT event_id, data_json FROM events WHERE job_id=? AND kind='job.submitted'",
                                     (job_id,)).fetchone()
                    data = {k: v for k, v in json.loads(row[1] or "{}").items() if k != "folder"}
                    tx.execute("UPDATE events SET data_json=? WHERE event_id=?", (json.dumps(data), row[0]))
                    # It also kept the workdir as typed (`resolve`d only; #140 spells it now).
                    tx.execute("UPDATE jobs SET workdir=? WHERE job_id=?", (typed, job_id))
                assert daemon._job(job_id)["workdir"] == typed and "folder" not in recorded(daemon, job_id)
            hold = look(daemon, state["job"])
            assert not _live(daemon, state["job"]) and workspaces.calls == [], (at, hold)
            assert hold.get("retiring") == [fence] and hold.get("folder") == nested, (at, hold)
            state["looked"].append((at, Path(tree).is_dir()))

        for method in ("begin", "archive"):
            def wrapped(self, *args, _real=getattr(rarch.Retirement, method), _at=method, **kwargs):
                step(_at)
                return _real(self, *args, **kwargs)
            patch.setattr(rarch.Retirement, method, wrapped)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert state["looked"] == [("begin", True), ("archive", False)], state
        assert "retired" in result["protected"], result
        spelled = [json.loads(row["data_json"]).get("folder") for row in daemon.store.query(
            "SELECT data_json FROM events WHERE job_id=? AND kind='job.folder_spelled'", (state["job"],))]
        assert [folder for folder in spelled if folder] == [nested], spelled     # spelled once, while there
        daemon._admit()
        assert _live(daemon, state["job"]), daemon._holds.get(state["job"])


@pytest.mark.parametrize("spelling", ["subdirectory", "symlink", "case"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c8_4_the_folder_is_recorded_once_at_submit_and_read_for_the_fence(tmp_path, shape, spelling):
    """The folder whose ancestors are read is the one submit recorded, spelled once
    (`folders.canonical`), never the workdir as typed: a job submitted in a folder of
    the nested repository, through a symlink to it, or in another case (on a volume
    that folds case), records the canonical folder and is held on the tree's fence
    all the same; and placed once the fence goes."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        Workspaces(daemon, patch, real=False)
        if spelling == "subdirectory":
            workdir = Path(nested) / "pkg"
            workdir.mkdir()
        elif spelling == "symlink":
            workdir = harness.root / "link-to-nested"
            workdir.symlink_to(nested, target_is_directory=True)
        else:
            workdir = Path(nested.replace("/vendor/lib", "/VENDOR/Lib"))
            if not workdir.is_dir() or not os.path.samefile(workdir, nested):
                pytest.skip("this volume does not fold case")
        job_id = detached_in(daemon, harness, workdir, shape)
        expected = folders.canonical(workdir)
        assert recorded(daemon, job_id)["folder"] == expected
        assert expected == (str(Path(nested) / "pkg") if spelling == "subdirectory" else nested)
        assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
        hold = look(daemon, job_id)
        assert not _live(daemon, job_id)
        assert hold["leases"] == hold["retiring"] == [folders.exclusive_key(tree)] and hold["folder"] == expected, hold
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, job_id), daemon._holds.get(job_id)


@pytest.mark.parametrize("spelt", ["as-recorded", "other-case"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c8_4_a_job_queued_before_folders_were_recorded_is_spelt_at_admission(tmp_path, shape, spelt):
    """A job a daemon queued before submit recorded `folder` (its `job.submitted` event
    has none) is spelt at admission from its workdir, outside any transaction
    (`folders.canonical`), and held on the fence above it all the same: also when the
    row's workdir is in another case than the volume stores (as typed, resolved but not
    spelt)."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        Workspaces(daemon, patch, real=False)
        job_id = detached_in(daemon, harness, nested, shape)
        with daemon.store.transaction("test.legacy") as tx:
            row = tx.execute("SELECT event_id, data_json FROM events WHERE job_id=? AND kind='job.submitted'",
                             (job_id,)).fetchone()
            data = {key: value for key, value in json.loads(row[1] or "{}").items() if key != "folder"}
            tx.execute("UPDATE events SET data_json=? WHERE event_id=?", (json.dumps(data), row[0]))
            if spelt == "other-case":
                typed = nested.replace("/vendor/lib", "/VENDOR/LIB")
                if not os.path.isdir(typed) or not os.path.samefile(typed, nested):
                    pytest.skip("this volume does not fold case")
                tx.execute("UPDATE jobs SET workdir=? WHERE job_id=?", (typed, job_id))
        assert "folder" not in recorded(daemon, job_id)
        assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
        hold = look(daemon, job_id)
        assert not _live(daemon, job_id)
        assert hold["retiring"] == [folders.exclusive_key(tree)] and hold["folder"] == nested, hold


@pytest.mark.parametrize("name", ["retired2", "retired:x"])
@pytest.mark.parametrize("fenced", ["tree", "sibling"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c8_4_a_fence_on_a_sibling_whose_name_extends_the_tree_holds_no_detached_job(tmp_path, shape, name, fenced):
    """`<tree>2` and `<tree>:x` start with the tree's spelling and are not inside it
    (`folders.within`, `above`: whole names only). With the fence on the tree, a
    detached job in such a sibling checkout is placed at once; with the fence on the
    sibling, so is one in the repository nested in the tree. A fix that compared
    spellings by prefix holds both."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        sibling = checkout(daemon.root / "worktrees" / name)
        assert sibling.startswith(tree) and not folders.within(sibling, tree)
        Workspaces(daemon, patch, real=False)
        held, folder = (tree, sibling) if fenced == "tree" else (sibling, nested)
        assert daemon.store.acquire_lease(folders.exclusive_key(held), "retention:other")
        job_id = detached_in(daemon, harness, folder, shape)
        hold = look(daemon, job_id)
        assert _live(daemon, job_id), hold
        assert lease(daemon, folders.exclusive_key(held)) == "retention:other"


@pytest.mark.parametrize("above", ["in-place-writer", "turn-row", "gate-round", "own-folder-writer"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_c6_5_only_retentions_fence_holds_a_detached_job_that_takes_no_folder_lease(tmp_path, shape, above):
    """C-6.5: a read-only job excludes no writer, and a writer that is not in place
    writes in its own worktree, never in the checkout it was cut from. So of the leases
    on a detached job's folder or a folder above it, only retention's fence holds it
    (`folders.RETENTION`): never a detached writer in place in the checkout around a
    nested repository or in that repository itself, a writable turn's row there, or
    any other holder of `worktree:`; and the lease stays its holder's.
    Passed on 5253faa2 (it fenced none of these jobs); fails a fix that fences on any
    holder."""
    from tests.fake.test_admission_liveness import _turn_in
    from tests.fake.test_writer_retention_fence import writer_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        Workspaces(daemon, patch, real=False)
        nested = nested_repository(harness.workdir, branch="task/nested")
        outer = folders.canonical(harness.workdir)
        place = nested if above == "own-folder-writer" else outer
        if above in ("in-place-writer", "own-folder-writer"):
            holder = writer_in(daemon, harness, place, caller_session="writer-session")
            daemon._admit()
            assert _live(daemon, holder) and lease(daemon, folders.exclusive_key(place)) == holder
        elif above == "turn-row":
            holder = _turn_in(daemon, harness, 0, workdir=harness.workdir)
            daemon._admit_turns()
            assert _live(daemon, holder)
        else:
            holder = "gate-round:other"
            assert daemon.store.acquire_lease(folders.exclusive_key(place), holder)
        job_id = detached_in(daemon, harness, nested, shape, caller_session="other-session")
        hold = look(daemon, job_id)
        assert _live(daemon, job_id), hold
        if above == "turn-row":
            assert folders.turn_holds(daemon.store.query, outer, (folders.TURN,))[0][1] == holder
        else:
            assert lease(daemon, folders.exclusive_key(place)) == holder


def test_c8_4_c6_5_detached_reservation_agrees_with_an_oracle_on_any_leases(tmp_path):
    """Differential: the daemon's own admission of a detached read-only job or worktree
    writer against an oracle written apart from `folders.retiring`. For a job in any
    of six checkouts (a job's tree, a folder in it, a repository nested in it, siblings
    whose names extend the tree's, the harness's checkout and a repository nested in
    that) and any `worktree:` leases on those folders and on the folders above them up
    to `/`, held by retentions, a gate round or a job:
    - the job is held before its workspace is prepared exactly when retention holds a
      lease on its folder or a folder it is inside (`folders.within`, whole names); its
      hold names exactly those keys, once each, all `retiring`, and its folder;
    - a worktree writer not so held is held in its reserving transaction exactly when
      retention holds a lease above the worktree cut for it (the writer's check on its
      write target), naming those;
    - otherwise it is placed, holding no lease on its folder (C-8.4, C-6.5)."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        workspaces = Workspaces(daemon, patch, real=False)
        tree, nested = retiring_tree(daemon, harness)
        jobs_dir = folders.canonical(daemon.root / "worktrees")
        sub = Path(tree) / "pkg"
        sub.mkdir()
        places_of = {"tree": tree, "sub": folders.canonical(sub), "nested": nested,
                     "sibling2": checkout(daemon.root / "worktrees" / "retired2"),
                     "sibling:x": checkout(daemon.root / "worktrees" / "retired:x"),
                     "outer": folders.canonical(harness.workdir),
                     "outer-nested": nested_repository(harness.workdir, branch="task/nested")}
        places = sorted({folder for each in places_of.values() for folder in (each, *folders.above(each))})
        holders = ["retention:a", "retention:b", "gate-round:g", "20261005-000000-astra"]
        count = [0]

        @settings(max_examples=80, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
        @example(shape="read-only", where="nested", held=[(tree, "retention:a")])
        @example(shape="worktree", where="sub", held=[(tree, "retention:a"), (nested, "retention:b")])
        @example(shape="worktree", where="outer", held=[(jobs_dir, "retention:a")])
        @example(shape="read-only", where="tree", held=[(tree, "20261005-000000-astra")])
        @given(shape=st.sampled_from(sorted(SHAPES)), where=st.sampled_from(sorted(places_of)),
               held=st.lists(st.tuples(st.sampled_from(places), st.sampled_from(holders)),
                             max_size=4, unique_by=lambda pair: pair[0]))
        def agrees(shape, where, held):
            count[0] += 1
            folder = places_of[where]
            job_id = detached_in(daemon, harness, folder, shape, caller_session=f"session-{count[0]}")
            allocated = f"{jobs_dir}/{job_id}"
            del workspaces.calls[:]
            try:
                with daemon.store.transaction("test.leases") as tx:
                    for place, holder in held:
                        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                                   (folders.exclusive_key(place), holder, "now"))
                fenced = {folders.exclusive_key(place) for place, holder in held
                          if holder.startswith("retention:") and folders.within(folder, place)}
                above_tree = {folders.exclusive_key(place) for place, holder in held
                              if holder.startswith("retention:") and folders.within(allocated, place)
                              } if shape == "worktree" else set()
                hold = look(daemon, job_id)
                if fenced:
                    assert not _live(daemon, job_id) and workspaces.calls == [], (shape, where, held, hold)
                    assert hold["reason"] == "lease-held" and hold["folder"] == folder, (shape, where, held, hold)
                    assert sorted(hold["leases"]) == sorted(hold["retiring"]) == sorted(fenced), (shape, where, held, hold)
                    assert len(hold["leases"]) == len(set(hold["leases"])), hold
                elif above_tree:
                    assert not _live(daemon, job_id) and workspaces.calls == [job_id], (shape, where, held, hold)
                    assert sorted(hold["retiring"]) == sorted(hold["leases"]) == sorted(above_tree), hold
                    assert hold["folder"] == str(daemon.root / "worktrees" / job_id), hold
                else:
                    assert _live(daemon, job_id), (shape, where, held, hold)
                    assert daemon.store.one("SELECT 1 FROM leases WHERE lease_key=? AND holder=?",
                                            (folders.exclusive_key(folder), job_id)) is None
            finally:
                with daemon.store.transaction("test.clear") as tx:
                    for place, holder in held:
                        tx.execute("DELETE FROM leases WHERE lease_key=? AND holder=?",
                                   (folders.exclusive_key(place), holder))
                _end(daemon, job_id)

        agrees()
        assert count[0] >= 80


# --- the interleavings ----------------------------------------------------------------------

STEPS = ("select", "begin", "lock", "quarantine", "archive", "final_check", "reclaim")
action = st.one_of(
    st.tuples(st.just("submit"), st.sampled_from(sorted(SHAPES)), st.sampled_from(["tree", "sub", "nested"])),
    st.tuples(st.just("look"), st.just(""), st.just("")),
    st.tuples(st.just("end"), st.integers(0, 3).map(str), st.just("")))
schedule = st.lists(st.tuples(st.sampled_from(STEPS), action), max_size=10)


@settings(max_examples=12, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@example([("begin", ("submit", "read-only", "nested"))])
@example([("lock", ("submit", "worktree", "nested")), ("archive", ("look", "", ""))])
@example([("select", ("submit", "worktree", "tree")), ("begin", ("look", "", ""))])
@example([("begin", ("submit", "read-only", "tree")), ("lock", ("end", "0", "")), ("final_check", ("look", "", ""))])
@example([("begin", ("submit", "worktree", "sub")), ("quarantine", ("end", "0", ""))])
@given(steps=schedule)
def test_interleavings_never_run_a_detached_job_in_a_tree_being_retired(tmp_path_factory, steps):
    """Detached jobs submitted, looked at and ended at each real archive-driver boundary
    (the selection, then begin, lock, quarantine, archive, the final check and the
    reclaim), with the daemon's own admission and workspace preparation. A job is
    submitted only while its folder exists (submit refuses one that does not).

    Safety, after every look: no detached job is live, or was prepared, while
    retention's fence is on its folder or a folder above it; and before the quarantine
    and the reclaim, no live job's folder is in the tree. At the end: a job not ended
    whose folder is in the tree kept the tree (the retirement was rolled back, the
    fence released); liveness: the next pass places every such job."""
    root = tmp_path_factory.mktemp("interleave")
    with fleet_daemon(root / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        workspaces = Workspaces(daemon, patch, real=True)
        jobs: list[tuple[str, str]] = []                    # (job id, folder)
        prepared_under_fence: list = []
        real_workspace = workspaces.real

        def guarded(job):
            folder = dict(jobs)[job["job_id"]]
            if folders.retiring(daemon.store.query, folder):
                prepared_under_fence.append((job["job_id"], folder))
            return real_workspace(job)

        workspaces.real = guarded

        def safe():
            for job_id, folder in jobs:
                if _live(daemon, job_id):
                    assert not folders.retiring(daemon.store.query, folder), (steps, job_id)
            assert prepared_under_fence == [], steps

        def no_live_job_in_tree():
            for job_id, folder in jobs:
                assert not (_live(daemon, job_id) and folders.within(folder, tree)), (steps, job_id)

        def step(at):
            for when, (verb, a, b) in steps:
                if when != at:
                    continue
                if verb == "submit":
                    workdir = where_in(tree, nested, b) if Path(tree).is_dir() else None
                    if workdir and Path(workdir).is_dir():
                        job_id = detached_in(daemon, harness, workdir, a, caller_session=f"s-{len(jobs)}")
                        jobs.append((job_id, folders.canonical(workdir)))
                elif verb == "look":
                    for job_id, _ in jobs:
                        daemon.store.update_job(job_id, next_check_at=None)
                    daemon._admit()
                elif int(a) < len(jobs):
                    _end(daemon, jobs[int(a)][0])
                safe()
            if at in ("quarantine", "reclaim"):
                no_live_job_in_tree()

        for method in STEPS[1:]:
            def wrapped(self, *args, _real=getattr(rarch.Retirement, method), _at=method, **kwargs):
                step(_at)
                return _real(self, *args, **kwargs)
            patch.setattr(rarch.Retirement, method, wrapped)
        step("select")
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        open_in_tree = [job_id for job_id, folder in jobs if folders.within(folder, tree)
                        and daemon._job(job_id)["state"] not in ("succeeded", "failed", "cancelled", "lost")]
        if open_in_tree:
            assert "retired" in result["protected"] and Path(tree).is_dir(), (steps, result)
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        for job_id, _ in jobs:
            daemon.store.update_job(job_id, next_check_at=None)
        daemon._admit()
        for job_id in open_in_tree:
            assert _live(daemon, job_id), (steps, job_id, daemon._holds.get(job_id))
        safe()


# --- `why` (C-6.11) ---------------------------------------------------------------------------

@pytest.mark.parametrize("hold, expected", [
    ({"reason": "lease-held", "leases": ["worktree:/s/worktrees/j"], "retiring": ["worktree:/s/worktrees/j"],
      "folder": "/s/worktrees/j/vendor/lib"},
     "Held: retention is removing a finished job's worktree that its folder /s/worktrees/j/vendor/lib is in "
     "(/s/worktrees/j); it waits until retention lets go of it (C-8.4)"),
    ({"reason": "lease-held", "leases": ["worktree:/s/worktrees/j"], "retiring": ["worktree:/s/worktrees/j"],
      "folder": "/s/worktrees/j"},
     "Held: retention is removing a finished job's worktree, its folder (/s/worktrees/j); it waits until "
     "retention lets go of it (C-8.4)"),
    ({"reason": "lease-held", "leases": ["worktree:/s/w/{j}"], "retiring": ["worktree:/s/w/{j}"]},
     "Held: retention is removing a finished job's worktree it works in (/s/w/{j}); it waits until retention "
     "lets go of it (C-8.4)"),
    ({"reason": "lease-held", "leases": ["out:/r.md", "worktree:/s/worktrees/j"],
      "retiring": ["worktree:/s/worktrees/j"], "folder": "/s/worktrees/j/a"},
     "(/s/worktrees/j); it waits until retention lets go of it (C-8.4); a lease it needs is also held by another "
     "job: out:/r.md"),
])
def test_c6_11_a_wait_on_retentions_fence_says_retention_is_removing_the_tree(hold, expected):
    """`why` names retention's fence as such (`retiring`), never as a lease another job
    holds, with the job's folder in the tree; a folder spelt with braces is printed as
    spelt; and any other key the job waits for is named after it. A hold without
    `retiring` reads as before."""
    text = "\n".join(render.why_queue({"job_id": "j", "state": "waiting", "hold": hold}))
    assert expected in text, text
    assert ("held by another job: worktree:" not in text), text
    plain = "\n".join(render.why_queue({"job_id": "j", "state": "waiting", "hold": {
        "reason": "lease-held", "leases": ["worktree:/w"]}}))
    assert "a lease this job needs is held by another job: worktree:/w" in plain
