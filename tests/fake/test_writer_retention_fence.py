"""C-8.4, C-6.5: a detached writer in a repository nested in a job's worktree waits
while retention retires that tree.

A detached writer in place holds `worktree:<its checkout's top level>`. In a
repository nested in a finished job's allocated worktree (`<tree>/vendor/lib`)
that key is on the nested repository, not on the tree, so reservation, which read
retention's fence only on that exact key, reserved the writer while retention held
`worktree:<tree>` for the retirement (probe in #137's evidence: `'live': True`
under `'fence': 'retention:retired'`). The quarantine then moved the tree from
under it. The commit rolled the retirement back (the writer's job is active, so
`worktree-in-use` kept the tree; once its attempt is quarantined, #137's
`worktree-lease` on a folder inside the tree does), but only after the move.

Reservation now reads retention's fence on every folder above the writer's too
(`folders.retiring`, the turns' check), in the reserving transaction and by
string operations only. These run the daemon's own admission in-process, as
`test_admission_liveness` does, against the archive driver's own retirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from tests.fake.test_admission_latency import fleet_daemon, measure, submit
from tests.fake.test_admission_liveness import CODEX, _checkout, _end, _live
from tests.unit.retention_world import git, snapshot
from tests.unit.test_retention_shared_folders import nested_repository, retiring_tree


def measured(daemon, harness):
    """Codex lanes measured (no admission probe) and the harness's workdir a checkout."""
    _checkout(harness)
    for lane in CODEX:
        measure(daemon, lane)


def writer_in(daemon, harness, folder: str, **changes) -> str:
    """A detached writer in place in `folder`, as `subfleet run -s workspace-write --in-place` submits it."""
    return submit(daemon, harness, sandbox="workspace-write", in_place=True, workdir=str(folder), **changes)


def look(daemon, job_id: str) -> dict:
    """One detached admission pass that looks at `job_id` now, whatever its clock (C-6.10)."""
    daemon.store.update_job(job_id, next_check_at=None)
    daemon._admit()
    return dict(daemon._holds.get(job_id) or {})


def lease(daemon, key: str) -> str | None:
    row = daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))
    return row["holder"] if row else None


def checkout(path: Path, branch: str = "task/sibling") -> str:
    """A repository of its own at `path`, on a task branch (C-13.2): its canonical top level."""
    path.mkdir(parents=True)
    git(path, "init", "--quiet", "-b", branch)
    (path / "f.txt").write_text("sibling\n")
    git(path, "add", ".")
    git(path, "commit", "--quiet", "-m", "sibling")
    return folders.canonical(path)


def test_c8_4_a_detached_writer_nested_in_a_tree_being_retired_waits_for_its_fence(tmp_path):
    """The probe in #137's evidence, made a test. While retention holds `worktree:<tree>`
    (after the selecting transaction, before `begin` reads or moves the tree), a detached
    writer is submitted in place in `<tree>/vendor/lib`, a repository of its own. Submission
    accepts it: the exact key is free. Admission holds it `lease-held` on the tree's fence,
    named in its hold and in `why`. It takes no `worktree:` lease on its folder, and nothing
    of it is reserved. Once the retirement lets go of the fence, the next detached pass
    places it on its own lease, without waiting out its clock (C-6.10).
    Failed on 9e159ec9: reserved under the fence (`'live': True`)."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        fence = folders.exclusive_key(tree)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        seen = {}

        def begin(retirement, job, pool):
            seen["fence"] = lease(daemon, fence)
            seen["writer"] = writer = writer_in(daemon, harness, nested)
            daemon._admit()
            seen.update(live=_live(daemon, writer), hold=dict(daemon._holds.get(writer) or {}),
                        own=lease(daemon, folders.exclusive_key(nested)),
                        why=daemon._why_job(daemon._job(writer))["text"])
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
        writer = seen["writer"]
        assert seen["fence"] == "retention:retired", seen
        assert not seen["live"] and seen["own"] is None, seen
        assert seen["hold"]["reason"] == "lease-held" and seen["hold"]["leases"] == [fence], seen
        assert (f"Held: retention is removing a finished job's worktree that its folder {nested} is in ({tree})"
                in seen["why"]), seen["why"]
        assert daemon._job(writer)["state"] == "waiting"
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)
        assert lease(daemon, folders.exclusive_key(nested)) == writer


def test_c8_4_a_nested_writer_waits_out_a_retirement_and_runs_once_the_fence_goes(tmp_path):
    """Liveness, end to end, with the daemon's own workspace preparation: a detached
    writer submitted in place in a repository nested in a job's tree after selection
    is looked at again at each step of the retirement (begin, lock, quarantine,
    archive, the final check), its tree moved away from the quarantine on, and is
    never reserved; no lease of its own is in the tree when the tree is moved. Its
    queued job keeps the tree at the commit (`worktree-in-use`, #76), so the
    retirement is rolled back, the tree comes back as it was, the fence goes, and
    the next detached pass runs the writer on the tree's own nested repository.
    Failed on 9e159ec9: reserved at the first look, live while its tree was in
    quarantine.

    The writer's look takes its start snapshot before the reserving transaction
    (`_workspace`), and git freshens the nested repository's objects doing so: their
    times move, never their bytes. That is all that moves."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        fence = folders.exclusive_key(tree)
        before = snapshot(Path(tree))
        state = {"looked": []}

        def step(at):
            if "writer" not in state:
                state["writer"] = writer_in(daemon, harness, nested)
            hold = look(daemon, state["writer"])
            assert not _live(daemon, state["writer"]), (at, hold)
            assert lease(daemon, folders.exclusive_key(nested)) is None, at
            assert hold.get("reason") == "lease-held" and hold["leases"] == [fence], (at, hold)
            state["looked"].append((at, Path(nested).is_dir()))

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
        after = snapshot(Path(tree))
        assert {path: entry[:3] for path, entry in after.items()} == {path: entry[:3] for path, entry in before.items()}
        moved = {path for path in after if after[path] != before[path]}
        assert all(path == "vendor/lib/.git" or path.startswith("vendor/lib/.git/objects/") for path in moved), moved
        assert daemon.store.get_job("retired")
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        daemon._admit()
        writer = state["writer"]
        assert _live(daemon, writer), daemon._holds.get(writer)
        assert lease(daemon, folders.exclusive_key(nested)) == writer
        _end(daemon, writer)
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder=?", (writer,)) is None


@pytest.mark.parametrize("name", ["retired2", "retired:x"])
@pytest.mark.parametrize("fenced", ["tree", "sibling"])
def test_c8_4_a_fence_on_a_sibling_whose_name_extends_the_tree_holds_no_writer(tmp_path, name, fenced):
    """`<tree>2` and `<tree>:x` start with the tree's spelling and are not inside it
    (`folders.within`, `above`: whole names only). With retention's fence on the tree, a
    detached writer in place in such a sibling checkout is placed at once; with the fence
    on the sibling, so is one in the repository nested in the tree. A fix that compared
    spellings by prefix holds both."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        sibling = checkout(daemon.root / "worktrees" / name)
        assert sibling.startswith(tree) and not folders.within(sibling, tree)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        held, folder = (tree, sibling) if fenced == "tree" else (sibling, nested)
        assert daemon.store.acquire_lease(folders.exclusive_key(held), "retention:other")
        writer = writer_in(daemon, harness, folder)
        hold = look(daemon, writer)
        assert _live(daemon, writer), hold
        assert lease(daemon, folders.exclusive_key(folder)) == writer
        assert lease(daemon, folders.exclusive_key(held)) == "retention:other"


@pytest.mark.parametrize("above", ["writer", "turn-row", "other"])
def test_c6_5_a_lease_above_held_by_anything_but_retention_holds_no_writer(tmp_path, above):
    """C-6.5: a detached writer's hold is its checkout's top level, and a repository
    nested in that checkout is a hold of its own. So of the leases on a folder above a
    detached writer's, only retention's fence holds it (`folders.RETENTION`), never a
    detached writer in place in the checkout around it, a writable turn's row there, or
    any other holder of `worktree:` above; and the lease above stays its holder's.
    Passed on 9e159ec9 (it fenced no folder above); fails a fix that fences on any
    holder above."""
    from tests.fake.test_admission_liveness import _turn_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        nested = nested_repository(harness.workdir, branch="task/nested")
        outer = folders.canonical(harness.workdir)
        assert nested != outer and folders.within(nested, outer)
        if above == "writer":
            # Another session's: one instance of a session with a live writer is a twin (C-6.5).
            holder = writer_in(daemon, harness, harness.workdir, caller_session="outer-session")
            daemon._admit()
            assert _live(daemon, holder) and lease(daemon, folders.exclusive_key(outer)) == holder
        elif above == "turn-row":
            holder = _turn_in(daemon, harness, 0, workdir=harness.workdir)
            daemon._admit_turns()
            assert _live(daemon, holder)
            assert folders.turn_holds(daemon.store.query, outer, (folders.TURN,))
        else:
            holder = "gate-round:other"
            assert daemon.store.acquire_lease(folders.exclusive_key(outer), holder)
        writer = writer_in(daemon, harness, nested)
        hold = look(daemon, writer)
        assert _live(daemon, writer), hold
        assert lease(daemon, folders.exclusive_key(nested)) == writer
        if above == "turn-row":
            assert folders.turn_holds(daemon.store.query, outer, (folders.TURN,))[0][1] == holder
        else:
            assert lease(daemon, folders.exclusive_key(outer)) == holder


def test_c8_4_retentions_fence_on_a_writers_own_folder_is_named_once(tmp_path):
    """A detached writer queued in place in a checkout waits while retention's fence holds
    the checkout itself (its own `worktree:<target>`, contested, as before), and its hold
    names that key once: the fence on its own folder is not also counted as one above
    (`folders.retiring` reads the folder itself too). It runs once the fence goes."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        writer = writer_in(daemon, harness, harness.workdir)
        target = daemon._write_target(daemon._job(writer), str(harness.workdir))
        assert daemon.store.acquire_lease(folders.exclusive_key(target), "retention:old-job")
        hold = look(daemon, writer)
        assert not _live(daemon, writer)
        assert hold["reason"] == "lease-held" and hold["leases"] == [folders.exclusive_key(target)], hold
        daemon.store.release_leases("retention:old-job")
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)
        assert lease(daemon, folders.exclusive_key(target)) == writer


@pytest.mark.parametrize("spelling", ["subdirectory", "symlink", "case"])
def test_c8_4_the_fence_above_is_read_on_the_writers_write_target(tmp_path, spelling):
    """The folder whose ancestors are read is the writer's write target, its checkout's
    top level spelled once (`folders.canonical`, C-6.5), never the workdir as submitted
    (symlinks resolved, case as typed): a writer submitted in a subdirectory of the
    nested repository, through a symlink to it, or with the tree's name typed in another
    case on a case-insensitive volume, is held on the tree's fence all the same, and
    placed on the nested repository's own lease once the fence goes."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        if spelling == "subdirectory":
            workdir = Path(nested) / "pkg"
            workdir.mkdir()
        elif spelling == "symlink":
            workdir = harness.root / "link-to-nested"
            workdir.symlink_to(nested, target_is_directory=True)
        else:
            workdir = Path(tree).with_name(Path(tree).name.upper()) / "vendor" / "lib"
            if not workdir.is_dir():
                pytest.skip("the volume is case-sensitive")
        assert not folders.within(str(workdir), tree) or spelling == "subdirectory"
        assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
        writer = writer_in(daemon, harness, workdir)
        assert daemon._write_target(daemon._job(writer), str(workdir)) == nested
        hold = look(daemon, writer)
        assert not _live(daemon, writer)
        assert hold["reason"] == "lease-held" and hold["leases"] == [folders.exclusive_key(tree)], hold
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)
        assert lease(daemon, folders.exclusive_key(nested)) == writer


def test_c8_4_c6_5_writer_reservation_agrees_with_an_oracle_on_any_leases(tmp_path):
    """Differential: the daemon's own reservation of a detached writer in place against
    an oracle written apart from `folders.retiring`. For a writer in any of six checkouts
    (a job's tree, a repository nested in it, siblings whose names extend the tree's, the
    harness's checkout and a repository nested in that) and any `worktree:` leases on
    those folders and on the folders above them up to `/`, held by retentions, a gate
    round or a job: the writer is held exactly when a lease is on its own folder (any
    holder, contested as before) or retention holds one on a folder it is inside
    (`folders.within`, whole names). While retention's fence covers its folder, its hold
    names exactly the fences, once each, all `retiring`: the look before its workspace
    is prepared holds it there whatever else it would also wait for (C-8.4). Otherwise
    its hold names exactly its own key, not `retiring`; and with neither it is placed on
    its own lease (C-8.4, C-6.5)."""
    from hypothesis import HealthCheck, example, given, settings, strategies as st

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        tree, nested = retiring_tree(daemon, harness)
        writers = {"tree": tree, "nested": nested,
                   "sibling2": checkout(daemon.root / "worktrees" / "retired2"),
                   "sibling:x": checkout(daemon.root / "worktrees" / "retired:x"),
                   "outer": folders.canonical(harness.workdir),
                   "outer-nested": nested_repository(harness.workdir, branch="task/nested")}
        # Built apart from `folders.above` (pathlib's parents), so the domain does not share its helper.
        places = sorted({folder for each in writers.values() for folder in (each, *map(str, Path(each).parents))})
        assert "/" in places
        holders = ["retention:a", "retention:b", "gate-round:g", "20261005-000000-astra"]
        count = [0]

        @settings(max_examples=60, deadline=None, derandomize=True,
                  suppress_health_check=[HealthCheck.too_slow])
        @example(where="outer-nested", held=[("/", "retention:a"), (writers["outer-nested"], "gate-round:g")])
        @example(where="nested", held=[(nested, "gate-round:g")])
        @example(where="nested", held=[(nested, "retention:b"), (tree, "retention:a")])
        @given(where=st.sampled_from(sorted(writers)),
               held=st.lists(st.tuples(st.sampled_from(places), st.sampled_from(holders)),
                             max_size=4, unique_by=lambda pair: pair[0]))
        def agrees(where, held):
            count[0] += 1
            folder = writers[where]
            writer = writer_in(daemon, harness, folder, caller_session=f"session-{count[0]}")
            try:
                with daemon.store.transaction("test.leases") as tx:
                    for place, holder in held:
                        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                                   (folders.exclusive_key(place), holder, "now"))
                fenced = {folders.exclusive_key(place) for place, holder in held
                          if holder.startswith("retention:") and folders.within(folder, place)}
                expected = fenced or {folders.exclusive_key(place) for place, holder in held if place == folder}
                hold = look(daemon, writer)
                if expected:
                    assert not _live(daemon, writer), (where, held, hold)
                    assert hold["reason"] == "lease-held", (where, held, hold)
                    assert sorted(hold["leases"]) == sorted(expected), (where, held, hold)
                    assert len(hold["leases"]) == len(set(hold["leases"])), (where, held, hold)
                    assert sorted(hold.get("retiring") or ()) == sorted(fenced), (where, held, hold)
                else:
                    assert _live(daemon, writer), (where, held, hold)
                    assert lease(daemon, folders.exclusive_key(folder)) == writer
            finally:
                with daemon.store.transaction("test.clear") as tx:
                    for place, holder in held:
                        tx.execute("DELETE FROM leases WHERE lease_key=? AND holder=?",
                                   (folders.exclusive_key(place), holder))
                _end(daemon, writer)

        agrees()
        assert count[0] >= 60


def alias(spelling: str, tree: str, nested: str) -> str:
    """A spelling of the writer's folder its volume accepts and does not store: the
    tree's name in capitals, the non-ASCII state root in capitals or in NFD."""
    import unicodedata
    if spelling == "top-case":
        return str(Path(tree).with_name(Path(tree).name.upper()))
    if spelling == "nested-case":
        return nested.replace("/worktrees/retired/", "/worktrees/RETIRED/")
    if spelling == "nested-unicode-case":
        return nested.replace("stäte", "STÄTE")
    if spelling == "nested-nfd":
        return nested.replace("stäte", unicodedata.normalize("NFD", "stäte"))
    raise AssertionError(spelling)


@pytest.mark.parametrize("spelling", ["nested-case", "nested-unicode-case", "nested-nfd"])
def test_c8_4_a_writer_submitted_under_an_alias_keeps_the_tree_through_a_retirement(tmp_path, spelling):
    """The review of 5253faa2, P2, made a test. A writer submitted during a retirement in
    the repository nested in the tree, its folder typed in a spelling the volume accepts
    and does not store, waits on the fence; its queued job must still keep the tree at the
    commit (`worktree-in-use`), which compares recorded strings in SQL. Submit kept the
    workdir as typed (only `resolve`d), so in NFD, or with the non-ASCII state root in
    capitals (SQLite's LIKE folds ASCII only), the pin missed it and the retirement deleted
    the tree and the writer's repository while it waited. Submit now records the workdir
    in its one spelling (`folders.canonical`). With submit's spelling put back, `nested-nfd`
    and `nested-unicode-case` failed with `pruned: ['retired']` and the tree gone;
    `nested-case` kept the tree (LIKE folds ASCII) and failed only on the recorded
    spelling."""
    with fleet_daemon(tmp_path / "stäte") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        fence = folders.exclusive_key(tree)
        workdir = alias(spelling, tree, nested)
        if not Path(workdir).is_dir():
            pytest.skip("the volume tells case or Unicode forms apart")
        assert workdir != nested
        state = {}
        real_begin = rarch.Retirement.begin

        def begin(self, job, pool):
            if "writer" not in state:
                state["writer"] = writer = writer_in(daemon, harness, workdir)
                state["hold"] = look(daemon, writer)
                state["live"] = _live(daemon, writer)
            return real_begin(self, job, pool)

        patch.setattr(rarch.Retirement, "begin", begin)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        writer = state["writer"]
        assert not state["live"] and state["hold"]["leases"] == [fence], state
        assert "retired" in result["protected"] and "retired" not in result["pruned"], result
        assert Path(nested, "f.txt").is_file() and daemon.store.get_job("retired")
        assert daemon._job(writer)["workdir"] == nested
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)
        assert lease(daemon, folders.exclusive_key(nested)) == writer


def test_c8_4_a_writer_queued_on_the_tree_under_a_case_alias_keeps_it_from_selection(tmp_path):
    """A writer queued in place on a finished job's tree itself before retention's pass,
    its folder typed in other capitals. `worktree-in-use` matched the tree itself with `=`,
    which compares case, so the queued writer kept nothing: retention fenced the tree, the
    writer waited on its own key, and the commit deleted the tree it was queued to write
    in. With the workdir recorded in its one spelling the pin keeps the tree from
    selection on, and the writer runs on the tree. With submit's spelling put back the
    pin found nothing and the retirement deleted the tree (`pruned: ['retired']`)."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        workdir = alias("top-case", tree, nested)
        if not Path(workdir).is_dir():
            pytest.skip("the volume tells case apart")
        writer = writer_in(daemon, harness, workdir)
        pins = retention._pin_reasons(daemon.store, set(), None, only="retired", root=daemon.root)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert "retired" in result["protected"] and "retired" not in result["pruned"], (pins, result)
        assert pins == {"retired": "worktree-in-use"} and daemon._job(writer)["workdir"] == tree
        assert Path(tree).is_dir() and lease(daemon, folders.exclusive_key(tree)) is None
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)
        assert lease(daemon, folders.exclusive_key(tree)) == writer


def test_c6_11_a_writer_on_its_clock_never_queues_the_fence_above_it(tmp_path):
    """C-6.9, C-6.11: a key a job needs free but does not take is never queued for, on a
    look on its clock as at a full one (review of 5253faa2, P3; its probe, made a test). An
    older writer in `<tree>/vendor/lib` waits for retention's fence on `<tree>`, then rests
    on its clock. A look on the clock keeps the job's place in the queues of the keys it
    takes. It used to queue every key of its hold, the fence above included, so when the
    fence went mid-pass a newer writer whose own key that is waited `queued_behind` a job
    that never takes the key. Rows are written directly: submission refuses a writer on a
    fenced tree, and the queue mechanism is what is under test."""
    from subfleet.daemon import after
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        tree = str(harness.root / "worktrees" / "retired")
        nested, fence = tree + "/vendor/lib", folders.exclusive_key(tree)

        def add(job_id, folder):
            daemon.store.add_job(job_id=job_id, request_id=job_id, payload_digest=job_id, kind="dispatch",
                                 sandbox="workspace-write", in_place=True, workdir=folder,
                                 prompt_path=str(harness.root / "prompt.md"), pinned_model="astra",
                                 max_attempts=1, max_wall_s=3600)
            return job_id

        older = add("older", nested)
        assert daemon.store.acquire_lease(fence, "retention:retired")
        daemon._admit()
        assert not _live(daemon, older) and daemon._holds[older]["leases"] == [fence]
        daemon.store.update_job(older, next_check_at=after(3600))
        newer = add("newer", tree)

        real = daemon._detached_folder

        def finishes(job):          # retention lets go after the older's look, before the newer's
            if job["job_id"] == newer:
                daemon.store.release_leases("retention:retired")
            return real(job)

        # The newer's first look at the fence is the one before its workspace (C-8.4).
        patch.setattr(daemon, "_detached_folder", finishes)
        daemon._admit()
        assert lease(daemon, fence) == newer, daemon._holds.get(newer)
        assert not _live(daemon, older) and daemon._holds[older]["leases"] == [fence]
        daemon.store.update_job(older, next_check_at=None)
        daemon._admit()
        assert _live(daemon, older) and lease(daemon, folders.exclusive_key(nested)) == older


@pytest.mark.parametrize("who", ["writer", "TURN", "READER"])
def test_c8_4_a_job_submitted_through_the_data_firmlink_keeps_the_tree(tmp_path, who):
    """`/System/Volumes/Data/…` names the same folder as `/…` through a firmlink, which
    `resolve` keeps (it is not a symlink) and the kernel's spelling does not. A detached
    writer or a conversation's turn, writable or read-only, submitted during a retirement
    with its folder in the repository nested in the tree typed that way, waits on the
    fence; its queued job keeps the tree at the commit only if the workdir submit
    recorded names the tree as the tree is recorded. Submit records the workdir in its one
    spelling (review of 5253faa2, P2; the case-alias review's P3, chip task_4972080c, for
    turns). Skipped where the state root is not reached through that firmlink."""
    from tests.fake.test_turn_wait_reasons import SETTINGS, message_in
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        firm = Path("/System/Volumes/Data" + nested)
        if not firm.is_dir() or not firm.samefile(nested):
            pytest.skip("the state root is not reached through the Data volume's firmlink")
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        state = {}
        real_begin = rarch.Retirement.begin

        def begin(self, job, pool):
            if "job" not in state:
                if who == "writer":
                    state["job"] = job_id = writer_in(daemon, harness, firm)
                    state["hold"] = look(daemon, job_id)
                else:
                    options = {**SETTINGS, "permission": "accept-edits" if who == "TURN" else "read-only"}
                    _, _, job_id = message_in(daemon, harness, "Firm", workspace=firm, settings=options)
                    state["job"] = job_id
                    daemon.store.update_job(job_id, next_check_at=None)
                    daemon._admit_turns()
                    state["hold"] = dict(daemon._holds.get(job_id) or {})
                state["live"] = _live(daemon, state["job"])
            return real_begin(self, job, pool)

        patch.setattr(rarch.Retirement, "begin", begin)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        job_id = state["job"]
        assert not state["live"] and state["hold"].get("reason") == "lease-held", state
        assert "retired" in result["protected"] and "retired" not in result["pruned"], result
        assert Path(nested, "f.txt").is_file() and daemon._job(job_id)["workdir"] == nested
        daemon._admit()
        assert _live(daemon, job_id), daemon._holds.get(job_id)
