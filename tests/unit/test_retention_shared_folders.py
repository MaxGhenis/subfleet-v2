"""I5 on the archive driver: shared turn rows, atomic fences and interleavings.

All filesystem work is in real temporary Git repositories; no provider or
live daemon is launched. Readers and writers are checked independently.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, event, example, given, settings, strategies as st

from subfleet import folders, retention, scheduler
from subfleet import retention_archive as rarch
from subfleet.daemon import after
from subfleet.store import Store
from tests.unit.retention_world import Clock, World, git, snapshot


def run(w):
    return retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {}, clock=Clock())


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


@pytest.fixture
def begun(monkeypatch):
    """The jobs whose retirement began (`Retirement.begin`). The selecting
    transaction keeps a job before its tree is quarantined; the commit's own
    checks also keep it, but only by rolling back a tree that was moved from
    under the live turn meanwhile. So a test of the selection layer asserts
    this stays empty, not just that the tree survived (8a112986's commit check
    otherwise hides the selection check's removal)."""
    started = []
    real = rarch.Retirement.begin

    def begin(retirement, job, pool):
        started.append(retirement.job_id)
        return real(retirement, job, pool)

    monkeypatch.setattr(rarch.Retirement, "begin", begin)
    return started


def nested_repository(wt: Path, branch: str = "main") -> str:
    """The folder a conversation opened in `<wt>/vendor/lib`, its own repository,
    keys its rows on: that repository's top level, canonical, inside `wt`. A
    writable turn there needs a branch other than `main` (C-13.2)."""
    from subfleet.salvage import git_toplevel
    nested = wt / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "--quiet", "-b", branch)
    (nested / "f.txt").write_text("nested\n")
    git(nested, "add", ".")
    git(nested, "commit", "--quiet", "-m", "nested")
    folder = folders.canonical(git_toplevel(str(nested)))
    assert folder != folders.canonical(wt) and folders.within(folder, folders.canonical(wt))
    return folder


def turn_folder(wt: Path, where: str) -> str:
    return folders.canonical(wt) if where == "tree" else nested_repository(wt)


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_live_turn_folder_protects_job_through_full_pass(world, writable):
    wt = world.job("job")
    before = snapshot(wt)
    key = folders.turn_key(folders.canonical(wt), "live-turn", writable=writable)
    assert world.store.acquire_lease(key, "live-turn")
    reasons = retention._pin_reasons(world.store, set(), None)
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert reasons["job"] == "turn-folder"
    assert world.store.get_job("job") and snapshot(wt) == before
    assert world.admin("job").is_dir()
    assert not (world.root / "retention" / "job").exists()
    world.store.release_leases("live-turn")
    result = run(world)
    assert result["pruned"] == ["job"], result
    assert not wt.exists() and not world.admin("job").exists()


@pytest.mark.parametrize("where", ["tree", "nested"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_turn_between_selection_and_transaction_stops_retirement(world, monkeypatch, begun, writable, where):
    """`nested`: the selecting transaction also reads rows on a folder inside
    the tree (review of 599af189, P3-1); the census is blinded below. The
    retirement never begins, so the tree is never moved from under the turn."""
    wt = world.job("job")
    folder = turn_folder(wt, where)
    before = snapshot(wt)
    start = retention._Pass._start

    def race(driver, job, protected):
        assert not driver.store._holds_writer()
        assert world.store.acquire_lease(folders.turn_key(folder, "late", writable=writable), "late")
        return start(driver, job, protected)

    monkeypatch.setattr(retention._Pass, "_start", race)
    # Isolate the atomic turn_holds guard from the pin census.
    monkeypatch.setattr(folders, "turn_folders", lambda read: set())
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.admin("job").is_dir()
    assert world.store.get_job("job")
    assert {r["holder"] for r in world.store.list_leases()} == {"late"}
    assert begun == [], "the selecting transaction let the retirement begin under a live turn"


@pytest.mark.parametrize("where", ["tree", "nested"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_archive_commit_rechecks_turn_rows_and_rolls_back(world, monkeypatch, writable, where):
    """An out-of-band row after archive verification must still stop row
    deletion, also on a folder inside the tree (the pins, P3-1)."""
    wt = world.job("job")
    folder = str(wt) if where == "tree" else nested_repository(wt)
    before = snapshot(wt)
    check = rarch.Retirement.final_check

    def late(retirement):
        check(retirement)
        world.store.acquire_lease(folders.turn_key(folder, "late", writable=writable), "late")

    monkeypatch.setattr(rarch.Retirement, "final_check", late)
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert world.store.get_job("job") and snapshot(wt) == before
    assert world.admin("job").is_dir()


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_retirement_fence_blocks_actual_turn_reservation(tmp_path, writable):
    """Admission and retirement agree on APFS case aliases of a recorded folder.

    Start admission after retention has acquired its fence but before it moves
    the worktree. No live turn row exists at selection, so only the canonical
    fence, not a turn pin, can prevent this reservation.
    """
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt = daemon.root / "worktrees" / "Retired"
        git(harness.workdir, "worktree", "add", "--quiet", "--detach", str(wt), "HEAD")
        alias = wt.with_name("rETIRED")
        if not alias.exists() or not alias.samefile(wt):
            pytest.skip("case-insensitive filesystem required")
        daemon.store.add_job(job_id="retired", request_id="retired", payload_digest="d", kind="dispatch",
                             workdir=str(harness.workdir), worktree=str(alias), prompt_path="/prompt",
                             sandbox="workspace-write", state="succeeded", workdir_head=git(wt, "rev-parse", "HEAD"))
        directory = daemon.root / "jobs" / "retired"
        directory.mkdir(parents=True)
        (directory / "stdout").write_text("retired output")
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        _, _, turn = message_in(daemon, harness, "During retirement", workspace=wt, settings=options)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        observed = []

        def begin(retirement, job, pool):
            daemon._admit_turns()
            observed.append(turn)
            assert not _live(daemon, turn), "turn reserved while retirement held its folder"
            assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                    (folders.exclusive_key(folders.canonical(wt)),)), "canonical retirement fence missing"
            assert folders.turn_holds(daemon.store.query, folders.canonical(wt)) == []
            assert daemon._holds[turn]["reason"] == "lease-held"
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert observed == [turn], result
        daemon.store.update_job(turn, next_check_at=None)
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds


def retiring_tree(daemon, harness) -> tuple[str, str]:
    """A finished job, `retired`, with its own worktree under the state root, as
    retention retires one, and a repository nested in that tree on a task branch,
    where a conversation may write: `(tree, nested)`, both canonical."""
    wt = daemon.root / "worktrees" / "retired"
    git(harness.workdir, "worktree", "add", "--quiet", "--detach", str(wt), "HEAD")
    tree, nested = folders.canonical(wt), nested_repository(wt, branch="task/nested")
    daemon.store.add_job(job_id="retired", request_id="retired", payload_digest="d", kind="dispatch",
                         workdir=str(harness.workdir), worktree=tree, prompt_path="/prompt",
                         sandbox="workspace-write", state="succeeded", workdir_head=git(wt, "rev-parse", "HEAD"))
    directory = daemon.root / "jobs" / "retired"
    directory.mkdir(parents=True)
    (directory / "stdout").write_text("retired output")
    return tree, nested


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_the_fence_holds_a_turn_in_a_repository_nested_in_the_tree(tmp_path, writable):
    """C-8.4, C-24.5: a conversation in a repository nested in a job's worktree keys
    its turn's row on that repository, not on the tree. Submitted after selection,
    while retention held the tree's fence and before it moved the tree, such a turn
    was reserved, writable and read-only alike (the known limit #134 left; its probe,
    made a test): the commit then rolled the retirement back, but only after the
    tree had been moved from under a live turn. Admission now reads the fence on
    every folder above the turn's own too (`folders.retiring`), in the reserving
    transaction. The turn waits `lease-held` on it, its message says retention is
    removing the tree its folder is in (C-6.11, C-24.4), and the first turn pass
    after the retirement lets go of the fence places it, without waiting out its
    clock (C-6.10). Failed on 8a112986: reserved under the fence."""
    from subfleet.conversations import waits
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in, reason, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        seen = {}

        def begin(retirement, job, pool):
            fence = daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (folders.exclusive_key(tree),))
            assert fence and fence["holder"] == "retention:retired"
            _, mid, turn = message_in(daemon, harness, "Nested", workspace=nested, settings=options)
            daemon._admit_turns()
            seen.update(mid=mid, turn=turn, live=_live(daemon, turn), hold=dict(daemon._holds.get(turn) or {}),
                        rows=folders.turn_holds(daemon.store.query, tree, inside=True), reason=reason(daemon, mid))
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
        assert not seen["live"] and seen["rows"] == [], seen
        assert seen["hold"]["reason"] == "lease-held", seen
        assert seen["hold"]["leases"] == [folders.exclusive_key(tree)] and seen["hold"]["folder"] == nested, seen
        assert seen["reason"] == ("lease: retention is removing a finished job's worktree that this folder is in "
                                  f"({tree})")
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        daemon._admit_turns()
        assert _live(daemon, seen["turn"]), daemon._holds.get(seen["turn"])
        assert reason(daemon, seen["mid"]) == waits.PLACED


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_detached_writer_above_a_nested_repository_holds_none_of_its_turns(tmp_path, writable):
    """C-6.5: a detached writer's hold is its checkout's top level, and a repository
    nested in that checkout is a hold of its own, so of the leases on a folder above
    a turn's only retention's fence holds the turn (`folders.retiring`), never a
    detached writer's. Beside a detached writer in place in a checkout, a turn in
    `<checkout>/vendor/lib` is placed at once, while a writable turn in the checkout
    itself waits for the writer. The base fenced no folder above, so this passed
    there; it fails a fix that fences on any holder above."""
    from subfleet.conversations import waits
    from tests.fake.test_admission_latency import fleet_daemon, measure, submit
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in, reason, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        nested = nested_repository(harness.workdir, branch="task/nested")
        writer = submit(daemon, harness, sandbox="workspace-write", in_place=True)
        daemon._admit()
        assert _live(daemon, writer), daemon._holds.get(writer)
        target = daemon._write_target(daemon._job(writer), str(harness.workdir))
        assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                (folders.exclusive_key(target),))["holder"] == writer
        assert nested != target and folders.within(nested, target)
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        _, mid, turn = message_in(daemon, harness, "Nested", workspace=nested, settings=options)
        _, _, beside = message_in(daemon, harness, "Checkout")
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds.get(turn)
        assert reason(daemon, mid) == waits.PLACED
        assert not _live(daemon, beside) and daemon._holds[beside]["leases"] == [folders.exclusive_key(target)]


def ctimes(root: Path) -> dict[str, int]:
    """rel path -> ctime_ns, without following links: what `snapshot` leaves out of an
    entry's times, and part of the signature retention deletes by (C-8.4)."""
    return {str(path.relative_to(root)): path.lstat().st_ctime_ns for path in sorted(root.rglob("*"))}


def git_spawns(patch) -> list[tuple[list[str], str]]:
    """Every git process started from now on, as its argv and the folder it ran in
    (`-C`, else its working directory): `subprocess.run` and the rest start theirs
    through `subprocess.Popen`."""
    import os
    import subprocess
    spawned: list[tuple[list[str], str]] = []
    real = subprocess.Popen

    class Recording(real):
        def __init__(self, args, *rest, **kwargs):
            argv = [os.fsdecode(arg) for arg in args] if isinstance(args, (list, tuple)) else [str(args)]
            if argv and os.path.basename(argv[0]) == "git":
                at = argv[argv.index("-C") + 1] if "-C" in argv else os.fsdecode(kwargs.get("cwd") or os.getcwd())
                spawned.append((argv, at))
            super().__init__(args, *rest, **kwargs)

    patch.setattr(subprocess, "Popen", Recording)
    return spawned


def ran_in(spawn: tuple[list[str], str], tree: str) -> bool:
    """Whether a git process ran in `tree` or a folder inside it, as spelled: from the
    quarantine on there is nothing at that path."""
    return folders.within(folders.canonical(spawn[1]), tree)


@pytest.mark.parametrize("where", ["nested", "tree"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_nested_turn_waits_out_a_retirement_and_starts_once_the_fence_goes(tmp_path, writable, where):
    """C-8.4 end to end, with the daemon's own workspace preparation: a turn
    submitted in a repository nested in a job's tree after selection is looked at
    again at each step of the retirement (begin, lock, quarantine, archive, the
    final check), its tree moved away from the quarantine on, and is never
    reserved; no turn row is in the tree when it is moved. Its queued job keeps
    the tree at the commit (`worktree-in-use`, #76), so the retirement is rolled
    back, the tree comes back as it was, the fence goes, and the next turn pass
    places the turn. Failed on 8a112986: reserved at the first look, and live
    while its tree was in quarantine. `tree`: the same for a turn on the tree
    itself.

    No look prepares the turn's workspace while the fence is held (`_fence_hold`,
    before `_workspace`), so no git runs in the tree and nothing in it or in its
    admin directory moves, times included. On 9e159ec9 each look ran git there
    (nine processes for a writable turn, its start snapshot; two for a read-only
    one) and moved the times of the nested repository's `.git`, which fails this
    test."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        folder = nested if where == "nested" else tree
        admin = Path(git(Path(tree), "rev-parse", "--absolute-git-dir"))
        before, before_ctimes, before_admin = snapshot(Path(tree)), ctimes(Path(tree)), snapshot(admin)
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        state = {"looked": [], "prepared": 0}
        prepare = daemon._workspace

        def counted(job):
            state["prepared"] += job["job_id"] == state.get("turn")
            return prepare(job)

        patch.setattr(daemon, "_workspace", counted)
        spawned = git_spawns(patch)

        def look(at):
            if "turn" not in state:
                _, state["mid"], state["turn"] = message_in(daemon, harness, "Nested", workspace=folder,
                                                            settings=options)
            daemon.store.update_job(state["turn"], next_check_at=None)       # looked at now, whatever its clock
            spawned.clear()
            daemon._admit_turns()
            hold = daemon._holds.get(state["turn"]) or {}
            assert not _live(daemon, state["turn"]), (at, hold)
            assert folders.turn_holds(daemon.store.query, tree, inside=True) == [], at
            assert hold.get("reason") == "lease-held" and hold["leases"] == [folders.exclusive_key(tree)], (at, hold)
            assert hold["folder"] == folder, (at, hold)
            assert [spawn for spawn in spawned if ran_in(spawn, tree)] == [], (at, spawned)
            state["looked"].append((at, Path(folder).is_dir()))

        for method in ("begin", "lock", "quarantine", "archive", "final_check", "reclaim"):
            def wrapped(self, *args, _real=getattr(rarch.Retirement, method), _at=method, **kwargs):
                look(_at)
                return _real(self, *args, **kwargs)
            patch.setattr(rarch.Retirement, method, wrapped)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert list(dict.fromkeys(state["looked"])) == [
            ("begin", True), ("lock", True), ("quarantine", True), ("archive", False), ("final_check", False)], state
        assert "retired" in result["protected"] and "retired" not in result["pruned"], result
        assert state["prepared"] == 0, state
        assert snapshot(Path(tree)) == before and ctimes(Path(tree)) == before_ctimes
        assert snapshot(admin) == before_admin
        assert daemon.store.get_job("retired")
        assert daemon.store.one("SELECT 1 FROM leases WHERE holder='retention:retired'") is None
        spawned.clear()
        daemon._admit_turns()
        assert _live(daemon, state["turn"]), daemon._holds.get(state["turn"])
        # The look that places it prepares its workspace, and the recorder sees that git.
        assert state["prepared"] == 1 and any(ran_in(spawn, tree) for spawn in spawned), (state, spawned)


def admitted(daemon, turn, mid, prepared) -> dict:
    """What one turn pass left for `turn`: everything a hold reaches (C-6.10, C-6.11)."""
    from tests.fake.test_admission_liveness import _live
    from tests.fake.test_turn_wait_reasons import reason
    job, hold = daemon._job(turn), dict(daemon._holds[turn])
    wait = daemon._capacity_waits[turn]
    return {"live": _live(daemon, turn), "clock": hold.pop("next_check_at") == job["next_check_at"], "hold": hold,
            "row": (job["state"], job["wait_reason"], job["next_check_at"] is not None),
            "wait": {key: wait[key] for key in ("signature", "rechecks", "label", "hold", "expedite")},
            "reason": reason(daemon, mid), "prepared": prepared.count(turn)}


@pytest.mark.parametrize("where", ["nested", "tree", "subdirectory", "external"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_fenced_turn_is_held_before_its_workspace_as_the_transaction_holds_it(tmp_path, writable, where):
    """Differential (C-8.4, C-6.10, C-6.11): a turn whose folder is in a tree retention
    is retiring is held by the look before its workspace (`_fence_hold`) exactly as
    the admitting transaction holds it when that look is left out: the same hold but
    its clock, the same capacity wait (its signature, so its backoff, and what `why`
    reads), the same `waiting` row and the same message reason. Only the start
    snapshot is spared. `subdirectory`: its Git row stays on the tree, while its
    hold names its actual cwd. `external`: persisted core.worktree puts its Git
    folder outside the tree; both folders' fences still spare its start snapshot."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout
    from tests.fake.test_turn_wait_reasons import message_in, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        folder = nested if where in ("nested", "external") else tree
        workspace = Path(tree) / "pkg" if where == "subdirectory" else folder
        Path(workspace).mkdir(exist_ok=True)
        fence = [folders.exclusive_key(tree)]
        if where == "external":
            external = tmp_path / "external"
            external.mkdir()
            (external / "f.txt").write_text("nested\n")
            git(Path(nested), "config", "core.worktree", str(external))
            key = folders.exclusive_key(folders.canonical(external))
            assert daemon.store.acquire_lease(key, "retention:external")
            fence.insert(0, key)
        assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        prepare, fence_hold, prepared = daemon._workspace, daemon._fence_hold, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or prepare(job))
        seen = {}
        for path in ("transaction", "look"):
            patch.setattr(daemon, "_fence_hold", fence_hold if path == "look" else lambda *args: False)
            _, mid, turn = message_in(daemon, harness, path, workspace=workspace, settings=options)
            daemon._admit_turns()
            seen[path] = admitted(daemon, turn, mid, prepared)
        assert seen["transaction"]["hold"] == {"reason": "lease-held", "leases": fence,
                                                "folder": folders.canonical(workspace)}, seen
        assert seen["transaction"]["prepared"] == 1 and seen["transaction"]["row"] == ("waiting", "capacity", True)
        assert seen["look"] == {**seen["transaction"], "prepared": 0}, seen


def test_a_fenced_turn_names_the_fence_whatever_else_holds_it(tmp_path):
    """Intended difference (C-8.4, C-6.11): with every lane closed as well, the
    transaction held a fenced turn on the lanes, the first thing it met, and its
    message spoke of capacity. The look before the workspace meets the fence first,
    so while retention holds it the turn waits `lease-held` on it and says so; the
    first look after the fence goes finds the lanes."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout
    from tests.fake.test_turn_wait_reasons import message_in
    from subfleet.contracts import ClockSource, Closure, ClosureReason

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
            daemon.store.put_closure(Closure(lane, "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                             ClockSource.REPORTED, "test"))
        tree, nested = retiring_tree(daemon, harness)
        assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
        prepare, fence_hold, prepared = daemon._workspace, daemon._fence_hold, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or prepare(job))
        seen = {}
        for path in ("transaction", "look"):
            patch.setattr(daemon, "_fence_hold", fence_hold if path == "look" else lambda *args: False)
            _, mid, turn = message_in(daemon, harness, path, workspace=nested)
            daemon._admit_turns()
            seen[path] = admitted(daemon, turn, mid, prepared) | {"turn": turn, "mid": mid}
        assert seen["transaction"]["hold"]["reason"].startswith("closed"), seen
        assert seen["transaction"]["reason"].startswith("closed:") and seen["transaction"]["prepared"] == 1, seen
        assert seen["look"]["hold"] == {"reason": "lease-held", "leases": [folders.exclusive_key(tree)],
                                        "folder": nested}, seen
        assert seen["look"]["reason"] == ("lease: retention is removing a finished job's worktree that this "
                                          f"folder is in ({tree})") and seen["look"]["prepared"] == 0
        daemon.store.release_leases("retention:retired")
        daemon.store.update_job(seen["look"]["turn"], next_check_at=None)
        daemon._admit_turns()
        then = admitted(daemon, seen["look"]["turn"], seen["look"]["mid"], prepared)
        assert then["hold"]["reason"].startswith("closed") and then["prepared"] == 1, then


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_the_look_before_the_workspace_reads_the_folder_the_transaction_records(tmp_path, writable):
    """Differential (C-8.4, C-24.5, C-26.14): the folder the look before the workspace
    checks for a fence (`_turn_folder`, no git) is the one the admitting transaction
    keys the turn's row on and records in its attempt's evidence: a checkout's top, a
    subdirectory of it (its top), a repository nested in it, and a folder outside git."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in, SETTINGS
    from subfleet.salvage import git_toplevel

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        (harness.workdir / "pkg").mkdir()
        plain = tmp_path / "Plain"
        plain.mkdir()
        places = {"top": harness.workdir, "subdirectory": harness.workdir / "pkg",
                  "nested": Path(nested_repository(harness.workdir, branch="task/nested")), "outside": plain}
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        for name, place in places.items():
            _, _, turn = message_in(daemon, harness, name, workspace=place, settings=options)
            looked = daemon._turn_folder(daemon._job(turn))
            daemon._admit_turns()
            assert _live(daemon, turn), (name, daemon._holds.get(turn))
            evidence = json.loads(daemon.store.list_attempts(turn)[-1]["evidence_json"])
            assert looked == evidence["folder"] == folders.canonical(git_toplevel(str(place)) or place), name
            row = folders.turn_key(looked, turn, writable=writable)
            assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (row,))["holder"] == turn, name


PLACES = ("folder", "parent", "tree", "root", "child", "sibling", "prefix")
HOLDERS = ("retention:retired", "retention:other", "detached-writer", "turn-x")
JOB_STATES = {"queued": ("queued", None), "waiting": ("waiting", None),
              "cancel-requested": ("queued", "2026-10-05T00:00:00Z"), "succeeded": ("succeeded", None)}


def test_fence_hold_against_a_model_of_the_fence(tmp_path):
    """Property (C-8.4, C-6.10), `_fence_hold` against a model written with
    `pathlib` rather than `folders.above`: for any lease table over folders at,
    above, below and beside a turn's, and any state of its job, it holds the job
    exactly when retention holds `worktree:` on the folder or a folder above it,
    never for another holder there, a turn's row, or a fence below or beside it
    (`/a/bc` is not in `/a/b`); its hold lists those keys nearest first and names
    the folder; its clock is its backed-off delay away and its signature the
    transaction's. A job ended or cancelled meanwhile is held by nobody, and a look
    that finds no fence writes nothing. Looked at again on the same table, the
    wait counts one more recheck and its clock backs off (C-6.10). One daemon and
    one read-only turn in a repository nested in a finished job's tree serve every
    example."""
    from tests.fake.test_admission_latency import fleet_daemon
    from tests.fake.test_admission_liveness import _checkout
    from tests.fake.test_turn_wait_reasons import message_in, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        tree, folder = retiring_tree(daemon, harness)
        _, _, turn = message_in(daemon, harness, "Model", workspace=folder,
                                settings={**SETTINGS, "permission": "read-only"})
        job = daemon._job(turn)
        assert daemon._turn_folder(job) == folder
        parent = str(Path(folder).parent)
        spelled = {"folder": folder, "parent": parent, "tree": tree, "root": "/", "child": folder + "/sub",
                   "sibling": parent + "/other", "prefix": folder + "x"}

        @settings(max_examples=150, deadline=None, derandomize=True)
        @example(rows={("folder", "exclusive"): "retention:retired", ("parent", "exclusive"): "retention:other",
                       ("tree", "exclusive"): "retention:retired"}, job_state="waiting")
        @example(rows={("root", "exclusive"): "retention:retired"}, job_state="queued")
        @example(rows={("prefix", "exclusive"): "retention:retired", ("child", "exclusive"): "retention:retired",
                       ("sibling", "exclusive"): "retention:retired"}, job_state="queued")
        @example(rows={("parent", "exclusive"): "detached-writer", ("folder", "turn"): "retention:retired",
                       ("tree", "reader"): "retention:retired"}, job_state="queued")
        @example(rows={("tree", "exclusive"): "retention:retired"}, job_state="cancel-requested")
        @given(rows=st.dictionaries(st.tuples(st.sampled_from(PLACES),
                                              st.sampled_from(["exclusive", "turn", "reader"])),
                                    st.sampled_from(HOLDERS), max_size=6),
               job_state=st.sampled_from(list(JOB_STATES)))
        def check(rows, job_state):
            keys = {(place, kind): (folders.exclusive_key(spelled[place]) if kind == "exclusive" else
                                    folders.turn_key(spelled[place], "turn-x", writable=kind == "turn"))
                    for place, kind in rows}
            with daemon.store.transaction("test.reset") as tx:
                tx.execute("DELETE FROM leases")
                tx.executemany("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                               [(keys[row], holder, "now") for row, holder in rows.items()])
            state, cancelled = JOB_STATES[job_state]
            daemon.store.update_job(turn, state=state, wait_reason=None, next_check_at=None,
                                    cancel_requested_at=cancelled)
            daemon._capacity_waits.pop(turn, None)
            held = {row["lease_key"]: row["holder"]
                    for row in daemon.store.query("SELECT lease_key, holder FROM leases")}
            expected = [f"worktree:{each}" for each in (folder, *map(str, Path(folder).parents))
                        if str(held.get(f"worktree:{each}", "")).startswith("retention:")]
            gone = job_state in ("cancel-requested", "succeeded")
            event(f"fenced {len(expected)} deep" + (", job gone" if gone else ""))
            for look in (1, 2):
                was = (daemon._job(turn), daemon.store.one("SELECT max(event_id) AS n FROM events")["n"])
                holds, waiters = {}, {}
                backed_off = after(scheduler.capacity_recheck_delay(look - 1))     # read before: a lower bound
                answer = daemon._fence_hold(job, folder, holds, waiters, "standard", None, None)
                at_most = after(scheduler.capacity_recheck_delay(look - 1))        # read after: an upper bound
                now = (daemon._job(turn), daemon.store.one("SELECT max(event_id) AS n FROM events")["n"])
                assert answer == bool(expected), (rows, job_state)
                if not expected or gone:
                    assert (holds, waiters, now) == ({}, {}, was) and turn not in daemon._capacity_waits
                    continue
                row = now[0]
                # A bound read before the look, not the time after it: both clocks are
                # whole seconds, so a delay of 1 s can read as now.
                assert (row["state"], row["wait_reason"]) == ("waiting", "capacity")
                assert backed_off <= row["next_check_at"] <= at_most, (look, row["next_check_at"], backed_off, at_most)
                assert holds == {turn: {"reason": "lease-held", "leases": expected, "folder": folder,
                                        "next_check_at": row["next_check_at"]}}, rows
                assert waiters == {"standard": [(turn, None, None, frozenset(expected))]}
                wait = daemon._capacity_waits[turn]
                assert wait["signature"] == "lease-held:" + ",".join(sorted(expected)) and wait["rechecks"] == look - 1
                assert wait["hold"] == {"reason": "lease-held", "leases": expected, "folder": folder}

        check()


def test_a_fence_let_go_after_the_look_read_it_starts_the_turn_on_that_pass(tmp_path):
    """C-8.4, C-6.10: the look before the workspace reads the fence again in the
    transaction that would hold the turn. Let go between the two reads, nothing is
    held and the turn is placed by that same pass, not after its clock."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
        _, _, turn = message_in(daemon, harness, "Raced", workspace=nested)
        real, reads = folders.retiring, []

        def raced(read, folder):
            found = real(read, folder)
            if not reads:
                daemon.store.release_leases("retention:retired")
            reads.append(found)
            return found

        patch.setattr(folders, "retiring", raced)
        daemon._admit_turns()
        assert reads[:2] == [[folders.exclusive_key(tree)], []], reads
        assert _live(daemon, turn), daemon._holds.get(turn)
        assert turn not in daemon._capacity_waits


def test_a_fence_taken_after_the_look_read_none_is_the_transactions_to_hold(tmp_path):
    """C-8.4: the other order. Retention takes the fence just after the look's plain
    read found none: the look prepares the turn's workspace, git and all, in the tree
    now being retired, and the admitting transaction's own read holds the turn with
    the fence's hold. The look narrows the window; the transaction closes it."""
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        _, _, turn = message_in(daemon, harness, "Taken", workspace=nested)
        prepare, prepared = daemon._workspace, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or prepare(job))
        real, reads = folders.retiring, []

        def taken(read, folder):
            found = real(read, folder)
            if not reads:
                assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
            reads.append(found)
            return found

        patch.setattr(folders, "retiring", taken)
        spawned = git_spawns(patch)
        daemon._admit_turns()
        assert reads[:2] == [[], [folders.exclusive_key(tree)]], reads
        assert not _live(daemon, turn)
        hold = dict(daemon._holds[turn])
        hold.pop("next_check_at")
        assert hold == {"reason": "lease-held", "leases": [folders.exclusive_key(tree)], "folder": nested}, hold
        assert prepared == [turn] and any(ran_in(spawn, tree) for spawn in spawned), (prepared, spawned)


def test_a_fenced_turn_holds_no_later_turn_back_and_keeps_its_slot_as_its_transaction_did(tmp_path):
    """C-6.9, C-26.9, differential with 9e159ec9: with a turn cap set
    (`conversations.max_active_turns` 2), an older turn held on the fence is a waiter of
    its turns' class (`<tier>#turn`) on the fence's key, as when the transaction held it.
    A waiter on a lease waits for no slot, so it holds no later turn back: a later turn
    on its lane and model is placed (on 0812d5c5 it was held `behind-older-job` for the
    whole retirement, fenced-turn-precheck review P3-4). And the last turn slot is kept
    for it, so a turn on another lane after that one is held `slot-kept` for it. Pins the
    class the call site hands `_fence_hold` (`tier`): in another class the waiter would
    keep no turn slot. Its demand (`models`, `lanes`) decides nothing, now that a lease
    waiter holds no job back."""
    import tests.fake.test_turn_wait_reasons as reasons
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _end, _live
    from tests.fake.test_turn_wait_reasons import message_in, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        patch.setitem(daemon.policy, "conversations", {**(daemon.policy.get("conversations") or {}),
                                                       "max_active_turns": 2})
        tree, nested = retiring_tree(daemon, harness)
        assert daemon.store.acquire_lease(folders.exclusive_key(tree), "retention:retired")
        elsewhere = tmp_path / "Elsewhere"
        elsewhere.mkdir()
        seen = {}
        fence_hold = daemon._fence_hold
        for path in ("transaction", "look"):
            patch.setattr(daemon, "_fence_hold", fence_hold if path == "look" else lambda *args: False)
            _, _, older = message_in(daemon, harness, f"older-{path}", workspace=nested)
            reader = {**SETTINGS, "permission": "read-only"}
            _, _, later = message_in(daemon, harness, f"later-{path}", workspace=elsewhere, settings=reader)
            patch.setattr(reasons, "CODEX", (CODEX[1],))             # a conversation on codex-2
            _, _, beside = message_in(daemon, harness, f"beside-{path}", workspace=elsewhere, settings=reader)
            patch.setattr(reasons, "CODEX", CODEX)
            daemon._admit_turns()
            assert daemon._holds[older]["reason"] == "lease-held", \
                (path, {job: daemon._holds.get(job) for job in (older, later, beside)})
            held = dict(daemon._holds.get(beside) or {})
            seen[path] = {"later": _live(daemon, later), "beside": (held.get("reason"), held.get("kept_for") == older)}
            _end(daemon, later)
            with daemon.store.transaction("test.settle") as tx:      # out of the next path's way
                tx.execute("UPDATE jobs SET state='cancelled',cancel_requested_at=? WHERE job_id IN (?,?)",
                           ("2026-10-05T00:00:00Z", older, beside))
        assert seen["transaction"] == {"later": True, "beside": ("slot-kept", True)}, seen
        assert seen["look"] == seen["transaction"], seen


phase = st.sampled_from(["select", "begin", "lock", "archive", "verify", "delete"])
operation = st.tuples(phase, st.sampled_from(["start", "end"]), st.booleans(), st.sampled_from(["tree", "nested"]))


@settings(max_examples=40, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
@example([("select", "start", True, "tree")])
@example([("select", "start", False, "tree")])
@example([("begin", "start", True, "tree"), ("archive", "end", True, "tree"), ("delete", "start", False, "tree")])
@example([("begin", "start", False, "tree")])
@example([("lock", "start", True, "tree"), ("lock", "start", False, "tree")])
@example([("begin", "start", True, "nested")])
@example([("begin", "start", False, "nested")])
@example([("lock", "start", True, "nested"), ("lock", "start", False, "tree")])
@example([("select", "start", False, "nested"), ("begin", "end", False, "nested")])
@given(st.lists(operation, min_size=0, max_size=25))
def test_interleavings_never_delete_a_folder_named_by_a_turn(schedule):
    """Generate starts/ends at each real archive-driver boundary, of turns on the
    tree and in a repository nested in it.

    The independent turn actor reserves its SQL row only while its folder exists
    and admission's own fence check (`folders.retiring`: retention's fence on the
    folder or on one above it) finds none, atomically. The actual daemon's
    reservation is tested above. Before both quarantine and verified reclaim, the
    oracle reads the real lease table and forbids a move/delete with any live
    writer or reader naming the original folder or a folder inside it.
    """
    with tempfile.TemporaryDirectory(prefix="retention-i5-") as temporary, pytest.MonkeyPatch.context() as patch:
        w = World(Path(temporary))
        try:
            wt = w.job("job")
            folder = folders.canonical(wt)
            places = {"tree": folder}
            if any(where == "nested" for *_, where in schedule):
                places["nested"] = nested_repository(wt)
            executed = set()

            def step(at):
                if at in executed:
                    return
                executed.add(at)
                for when, action, writable, where in schedule:
                    if when != at:
                        continue
                    holder = f"{'writer' if writable else 'reader'}-{where}"
                    if action == "end":
                        w.store.release_leases(holder)
                    elif Path(places[where]).is_dir():
                        with w.store.transaction() as conn:
                            if not folders.retiring(lambda sql, params: conn.execute(sql, params).fetchall(),
                                                    places[where]):
                                conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                                             (folders.turn_key(places[where], holder, writable=writable), holder, "now"))

            def oracle():
                assert folders.turn_holds(w.store.query, folder, inside=True) == [], schedule

            def wrap(cls, method, at, check=False):
                real = getattr(cls, method)
                def wrapped(self, *args, **kwargs):
                    step(at)
                    if check:
                        oracle()
                    return real(self, *args, **kwargs)
                patch.setattr(cls, method, wrapped)

            wrap(retention._Pass, "_start", "begin")
            wrap(rarch.Retirement, "lock", "lock")
            wrap(rarch.Retirement, "archive", "archive")
            wrap(rarch.Retirement, "final_check", "verify")
            wrap(rarch.Retirement, "quarantine", "quarantine", check=True)
            wrap(rarch.Retirement, "reclaim", "delete", check=True)
            step("select")
            result = run(w)
            live = folders.turn_holds(w.store.query, folder, inside=True)
            if live:
                assert wt.is_dir() and w.store.get_job("job"), (schedule, result)
                assert "job" in result["protected"], (schedule, result)
            elif "job" in result["pruned"]:
                assert not wt.exists() and not w.admin("job").exists()
        finally:
            w.close()


def test_archive_journal_and_fence_use_the_same_folder_spelling(world):
    wt = world.job("MixedCase")
    alias = wt.with_name("mIXEDcASE")
    if not alias.exists() or not alias.samefile(wt):
        pytest.skip("case-insensitive filesystem required")
    world.store.update_job("MixedCase", worktree=str(alias))
    result = run(world)
    assert result["pruned"] == ["MixedCase"], result
    assert not wt.exists() and not world.admin("MixedCase").exists()


@pytest.mark.parametrize("recorded", ["alias", "unrecorded"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_the_commit_recheck_reads_rows_on_the_journals_spelling(world, monkeypatch, writable, recorded):
    """A row inside the tree after verification (written here directly: admission
    reserves none there while the fence is held) stops the commit also when
    `jobs.worktree` is spelled in another case or was never recorded. The pins
    compare the recorded spelling, so the commit also reads the rows on the
    journal's canonical one (review of 31048e67, F1: both were pruned, the tree
    deleted under the live row)."""
    wt = world.job("Job")
    folder = nested_repository(wt)
    if recorded == "alias":
        alias = wt.with_name(wt.name.swapcase())
        if not alias.exists() or not alias.samefile(wt):
            pytest.skip("case-insensitive filesystem required")
        world.store.update_job("Job", worktree=str(alias))
    else:
        world.store.update_job("Job", worktree=None)       # allocated, never recorded (C-6.12)
    before = snapshot(wt)
    check = rarch.Retirement.final_check

    def late(retirement):
        check(retirement)
        world.store.acquire_lease(folders.turn_key(folder, "late", writable=writable), "late")

    monkeypatch.setattr(rarch.Retirement, "final_check", late)
    result = run(world)
    assert result["pruned"] == [] and "Job" in result["protected"], result
    assert world.store.get_job("Job") and snapshot(wt) == before and world.admin("Job").is_dir()
    world.store.release_leases("late")
    monkeypatch.setattr(rarch.Retirement, "final_check", check)
    assert "Job" in run(world)["pruned"] and not wt.exists()


@pytest.mark.parametrize("where", ["tree", "nested"])
def test_turn_pin_recheck_does_no_filesystem_work_in_transaction(world, monkeypatch, where):
    wt = world.job("job")
    folder = str(wt) if where == "tree" else nested_repository(wt)
    world.store.acquire_lease(folders.turn_key(folder, "live", writable=False), "live")

    def unexpected(*args, **kwargs):
        pytest.fail("folder spelling must be established outside retention transactions")

    monkeypatch.setattr(folders, "canonical", unexpected)
    monkeypatch.setattr(folders, "_kernel_path", unexpected)
    with world.store.transaction():
        assert retention._pin_reasons(world.store, set(), None, only="job")["job"] == "turn-folder"


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_differently_spelt_recording_is_kept_by_the_selection_check(world, begun, writable):
    """The census compares recorded spellings, so only the selecting
    transaction's canonical turn_holds keeps a job whose jobs.worktree is spelt
    in another case while a turn row (canonical, as admission keys it) is live.
    It keeps it before the retirement begins (the commit's check would keep it
    too, but only after quarantining the tree)."""
    wt = world.job("Job")
    alias = wt.with_name(wt.name.swapcase())
    if not alias.exists() or not alias.samefile(wt):
        pytest.skip("case-insensitive filesystem required")
    world.store.update_job("Job", worktree=str(alias))
    before = snapshot(wt)
    world.store.acquire_lease(folders.turn_key(folders.canonical(alias), "live", writable=writable), "live")
    result = run(world)
    assert result["pruned"] == [] and "Job" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("Job") and world.admin("Job").is_dir()
    assert begun == [], "the selecting transaction let the retirement begin under a live turn"


def test_a_restart_mid_retirement_keeps_the_canonical_fence(world):
    """A pass stopped while archiving leaves its fence: admission keeps turns
    and readers out of the quarantined folder until a later pass finishes."""
    import threading
    wt = world.job("job")
    folder = folders.canonical(wt)
    cancel = threading.Event()
    archive = rarch.Retirement.archive

    def stop(self, *args, **kwargs):
        cancel.set()
        return archive(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rarch.Retirement, "archive", stop)
        try:
            retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                  holders=lambda watches, **_: {}, clock=Clock(), cancel=cancel)
        except rarch.Interrupted:
            pass
    fence = world.store.one("SELECT holder FROM leases WHERE lease_key=?", (folders.exclusive_key(folder),))
    assert fence is not None and fence["holder"] == "retention:job"
    assert not wt.exists() and rarch.load_journal(world.root, "job") is not None
    result = run(world)
    assert result["pruned"] == ["job"] or "job" in result.get("reclaimed", []), result
    assert world.store.one("SELECT 1 FROM leases WHERE holder='retention:job'") is None


@pytest.mark.parametrize("turn_state", ["running", "lost"])
def test_a_turn_in_a_repository_nested_in_the_job_worktree_keeps_it(world, turn_state):
    """I5 for a folder inside the job's worktree (review of 599af189, P3-1): a
    conversation opened in a repository nested there keys its row on that
    repository's top level. While its job runs, #76's worktree-in-use pin keeps
    the host; a quarantined turn's job is `lost` and keeps its TURN row (daemon
    `_quarantine`), and then only the row says the folder is in use. Failed on
    ceb1f15f for `lost`: the job was pruned and its tree deleted."""
    wt = world.job("job")
    folder = nested_repository(wt)
    world.store.add_job(job_id="turnjob", request_id="req-turnjob", payload_digest="d", kind="turn",
                        workdir=folder, worktree=folder, in_place=1, prompt_path="/prompt",
                        sandbox="workspace-write", state=turn_state)
    world.attempt("turnjob", state="running" if turn_state == "running" else "quarantined")
    world.store.acquire_lease(folders.turn_key(folder, "turnjob", writable=True), "turnjob")
    before = snapshot(wt)
    assert retention._pin_reasons(world.store, set(), None)["job"] == (
        "worktree-in-use" if turn_state == "running" else "turn-folder")
    result = run(world)
    assert "job" not in result["pruned"] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("job") and world.admin("job").is_dir()
    if turn_state == "lost":
        world.store.release_leases("turnjob")         # the quarantine is resolved: the row goes
        result = run(world)
        assert "job" in result["pruned"], result
        assert not wt.exists() and not world.admin("job").exists()


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_the_census_sees_a_turn_in_a_folder_inside_the_worktree(world, monkeypatch, writable):
    """P3-1, the pins alone: the selecting transaction's check is blinded, so
    only `_pin_reasons` can keep the job (each layer is tested alone, as in
    test_retention_worktrees.py)."""
    wt = world.job("job")
    folder = nested_repository(wt)
    world.store.acquire_lease(folders.turn_key(folder, "live", writable=writable), "live")
    before = snapshot(wt)
    real = folders.turn_holds
    monkeypatch.setattr(folders, "turn_holds", lambda read, folder, kinds=folders.SHARED, **_: [])
    assert retention._pin_reasons(world.store, set(), None)["job"] == "turn-folder"
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("job")
    monkeypatch.setattr(folders, "turn_holds", real)
    world.store.release_leases("live")
    assert run(world)["pruned"] == ["job"]


@pytest.mark.parametrize("beside", ["2", "-copy", ":x", "parent"])
def test_a_turn_beside_the_worktree_keeps_nothing(world, beside):
    """`inside` is a folder below the tree, not one whose name extends it
    (`<wt>2`, `<wt>-copy`, `<wt>:x`) and not one above it (the folder holding
    every tree): such a row keeps nothing, in the census or at selection."""
    wt = world.job("job")
    folder = folders.canonical(wt)
    other = str(Path(folder).parent) if beside == "parent" else folder + beside
    world.store.acquire_lease(folders.turn_key(other, "live", writable=True), "live")
    assert "job" not in retention._pin_reasons(world.store, set(), None)
    assert folders.turn_holds(world.store.query, folder, inside=True) == []
    assert run(world)["pruned"] == ["job"]
    assert not wt.exists()


def in_place(world, job_id: str, folder: str, *, kind: str = "turn", state: str = "succeeded") -> None:
    """A job recorded in place in `folder`, as admission records a writable turn
    (`jobs.worktree` is its write target, daemon.py `_admit`)."""
    world.store.add_job(job_id=job_id, request_id="req-" + job_id, payload_digest="d", kind=kind,
                        workdir=folder, worktree=folder, in_place=1, prompt_path="/prompt",
                        sandbox="workspace-write", state=state, created_at="2026-01-01T00:00:00.000+00:00",
                        finished_at=None if state == "running" else "2026-01-01T00:01:00.000+00:00")
    directory = world.root / "jobs" / job_id
    directory.mkdir()
    (directory / "stdout").write_text("output of " + job_id)


@pytest.mark.parametrize("where", ["folder", "inside"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_live_turn_keeps_no_in_place_job_in_its_folder(world, writable, where):
    """P3-2 (review of 599af189): retention never removes an in-place job's
    folder, so a live turn in a shared folder keeps none of the older jobs
    recorded there, turns or detached writers: they retire with their rows and
    job directories while the folder stays as it is. Failed on ceb1f15f: all
    four were kept as `turn-folder` until the conversation ended."""
    scratch = world.base / "home" / "scratch"
    scratch.mkdir()
    (scratch / "notes.txt").write_text("a person's notes\n")
    folder = folders.canonical(scratch)
    live = folder
    if where == "inside":
        (scratch / "sub").mkdir()
        live = folders.canonical(scratch / "sub")
    for n in range(3):
        in_place(world, f"turn-{n}", folder)
    in_place(world, "detached", folder, kind="dispatch")
    in_place(world, "live", live, state="running")
    world.attempt("live", state="running")
    world.store.acquire_lease(folders.turn_key(live, "live", writable=writable), "live")
    before = snapshot(scratch)
    reasons = retention._pin_reasons(world.store, set(), None)
    assert not {job: why for job, why in reasons.items() if why == "turn-folder"}, reasons
    result = retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0, turn_max_jobs=0,
                                   turn_max_bytes=0, turn_keep_s=0, holders=lambda watches, **_: {}, clock=Clock())
    assert sorted(result["pruned"]) == ["detached", "turn-0", "turn-1", "turn-2"], result
    assert "live" in result["protected"] and world.store.get_job("live")
    assert snapshot(scratch) == before


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_a_turn_row_keeps_no_job_without_its_own_tree(world, sandbox):
    """P3-2's rule is `owned` (_pin_reasons): a writable tree retention
    allocated. A read-only job recorded with a worktree retires as well."""
    wt = world.job("job")
    world.store.update_job("job", sandbox=sandbox, in_place=0 if sandbox == "read-only" else 1)
    world.store.acquire_lease(folders.turn_key(folders.canonical(wt), "live", writable=True), "live")
    before = snapshot(wt)
    assert "job" not in retention._pin_reasons(world.store, set(), None)
    assert run(world)["pruned"] == ["job"]
    assert snapshot(wt) == before                    # not its tree: retention leaves it


# --- the pin as a property --------------------------------------------------------

NAME = st.text(alphabet="ab:;-0./", min_size=1, max_size=4).filter(lambda name: name not in (".", ".."))
TREE = st.lists(NAME.filter(lambda name: "/" not in name), min_size=1, max_size=2).map(
    lambda parts: "/t/" + "/".join(parts))


@st.composite
def census(draw):
    """Jobs with and without their own tree, and turn rows on their trees, on
    folders inside, beside, above and elsewhere, every spelling the key's
    separators (':', '/') and their neighbours (';', '0', '-') can confuse."""
    jobs = []
    for n in range(draw(st.integers(1, 5))):
        tree = draw(st.one_of(TREE, st.none()))
        jobs.append(dict(job_id=f"job-{n}", worktree=tree, in_place=draw(st.booleans()),
                         sandbox=draw(st.sampled_from(["workspace-write", "read-only"]))))
    trees = [job["worktree"] for job in jobs if job["worktree"]] or ["/t/a"]
    near = st.sampled_from(trees).flatmap(lambda tree: st.one_of(
        st.just(tree), NAME.map(lambda name: f"{tree}/{name}"), NAME.map(lambda name: tree + name),
        st.just(tree.rsplit("/", 1)[0] or "/"), TREE))
    rows = draw(st.lists(st.tuples(near, st.booleans()), max_size=6))
    return jobs, rows


@settings(max_examples=150, deadline=None, derandomize=True)
@given(census())
def test_the_turn_folder_pin_is_exactly_an_owned_tree_a_turn_is_in(case):
    """For every job and turn row: `turn-folder` keeps a job iff it has its own
    allocated tree (writable, not in place: P3-2) and some row's folder is that
    tree or inside it (P3-1), checked against `folder == wt or
    folder.startswith(wt + "/")`, and the per-job reading (the selecting and
    committing transactions' `only=`) agrees with the census."""
    jobs, rows = case
    with tempfile.TemporaryDirectory(prefix="retention-pin-") as temporary:
        store = Store(Path(temporary) / "state.sqlite3")
        try:
            for job in jobs:
                store.add_job(request_id="req-" + job["job_id"], payload_digest="d", kind="dispatch",
                              workdir="/t", prompt_path="/prompt", state="succeeded", **job)
            for n, (folder, writable) in enumerate(rows):
                store.acquire_lease(folders.turn_key(folder, f"turn-{n}", writable=writable), f"turn-{n}")
            census_reasons = retention._pin_reasons(store, set(), None)
            for job in jobs:
                tree = job["worktree"]
                want = bool(tree and job["sandbox"] == "workspace-write" and not job["in_place"]
                            and any(folder == tree or folder.startswith(tree + "/") for folder, _ in rows))
                assert (census_reasons.get(job["job_id"]) == "turn-folder") == want, (job, rows, census_reasons)
                alone = retention._pin_reasons(store, set(), None, only=job["job_id"])
                assert alone.get(job["job_id"]) == census_reasons.get(job["job_id"]), (job, rows)
        finally:
            store.close()
