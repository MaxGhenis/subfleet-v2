"""R5: initial object storage through each public entry and task/fence merging."""
from contextlib import contextmanager
import json
import os

import pytest

from subfleet import dependencies, folders, retention_archive as rarch
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.unit.retention_world import git
from tests.unit.test_retention_dependency_units import repo


LAYOUTS = ["directory", "gitfile", "commondir", "linked"]


def checkout(world, layout):
    outside = world.base / "outside"
    if layout in ("directory", "gitfile"):
        repo(outside)
        common = outside / ".git"
        if layout == "gitfile":
            common = world.base / "metadata.git"
            (outside / ".git").rename(common)
            (outside / ".git").write_text("gitdir: ../metadata.git\n")
    else:
        seed = repo(world.base / "seed")
        common = seed / ".git"
        git(seed, "worktree", "add", "--quiet", "--detach", str(outside), "HEAD")
        if layout == "commondir":
            admin = world.base / "relocated" / "slot"
            admin.parent.mkdir()
            (common / "worktrees" / "outside").rename(admin)
            (outside / ".git").write_text(f"gitdir: {admin}\n")
            (admin / "commondir").write_text(os.path.relpath(common, admin) + "\n")
    return outside, common


def source(world, tree):
    owner = repo(tree / "source")
    (owner / "owner-only.txt").write_text("R5 owner-only storage\n")
    git(owner, "add", "owner-only.txt")
    git(owner, "commit", "--quiet", "-m", "R5 owner-only commit")
    return owner, git(owner, "rev-parse", "HEAD")


def off_transaction(world, monkeypatch):
    target, canonical = dependencies._Aliases._target, folders.canonical
    checks = []

    def observing(path):
        assert not world.store.connection.in_transaction
        checks.append(str(path))
        return target(path)

    def spelling(path):
        assert not world.store.connection.in_transaction
        return canonical(path)

    monkeypatch.setattr(dependencies._Aliases, "_target", staticmethod(observing))
    monkeypatch.setattr(folders, "canonical", spelling)
    return checks


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("state", ["running", "succeeded"], ids=["live", "exporting-control"])
def test_initial_objects_stable_through_each_entry(world, monkeypatch, layout, state):
    tree = world.job("owner")
    owner, commit = source(world, tree)
    outside, common = checkout(world, layout)
    objects = common / "objects"
    objects.rename(world.base / "old-objects")
    objects.symlink_to(owner / ".git" / "objects", target_is_directory=True)
    checks = off_transaction(world, monkeypatch)
    recorded = dependencies.record(str(outside))
    assert checks.count(str(objects)) >= 2, checks
    assert folders.canonical(owner / ".git" / "objects") in recorded["git"]
    assert not recorded.get("incomplete"), recorded
    assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    if state == "running":
        assert tree.is_dir() and "owner" not in result["pruned"], result
        assert "worktree-in-use" in result["pin_reasons"]["owner"]
        assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    else:
        assert "owner" in result["pruned"] and not tree.exists(), result


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("state", ["running", "succeeded"], ids=["live", "exporting"])
@pytest.mark.parametrize("change", ["parent-absent", "parent-hardlink", "remove", "remove-open"])
def test_initial_objects_changes_through_each_entry(world, monkeypatch, layout, state, change):
    trees = [world.job("owner-a"), world.job("owner-b")]
    world.store.update_job("owner-b", worktree=None)
    owner, commit = source(world, trees[0])
    outside, common = checkout(world, layout)
    objects = common / "objects"
    old = world.base / "old-storage"
    old.mkdir()
    objects.rename(old / "objects")
    parent = world.base / "storage-parent"
    parent.symlink_to(old, target_is_directory=True)
    objects.symlink_to(parent / "objects", target_is_directory=True)
    filename = objects / "info" / "alternates"
    if change in ("parent-hardlink", "remove-open"):
        filename.write_bytes(b"# first\n# second\n")
        if change == "parent-hardlink":
            os.link(filename, owner / ".git" / "objects" / "info" / "alternates")
        before = filename.stat()
    checks = off_transaction(world, monkeypatch)
    original = dependencies.open_regular
    changes = []

    def mutate():
        assert not changes
        changes.append(True)
        if change.startswith("remove"):
            objects.unlink()
        else:
            parent.unlink()
            parent.symlink_to(owner / ".git", target_is_directory=True)

    @contextmanager
    def opening(path, *args, **kwargs):
        if str(path) == str(filename) and change not in ("parent-hardlink", "remove-open"):
            try:
                with original(path, *args, **kwargs) as stream:
                    yield stream
            except FileNotFoundError:
                mutate()
                raise
            return
        with original(path, *args, **kwargs) as stream:
            if str(path) != str(filename):
                yield stream
                return

            class Changing:
                def readline(self, size=-1):
                    value = stream.readline(size)
                    if not changes:
                        mutate()
                    return value

                def __getattr__(self, name):
                    return getattr(stream, name)

            yield Changing()

    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "open_regular", opening)
        recorded = dependencies.record(str(outside))
    assert changes and checks.count(str(objects)) >= 2, checks
    assert any(error["file"] == str(objects) for error in recorded.get("incomplete", [])), recorded
    if change == "parent-hardlink":
        after = filename.stat()
        assert all(getattr(before, field) == getattr(after, field) for field in
                   ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))
    if not change.startswith("remove"):
        assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    assert not result["pruned"] and all(tree.is_dir() for tree in trees), (recorded, result)
    assert all("dependency-incomplete" in result["pin_reasons"][job]
               for job in ("owner-a", "owner-b"))


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("stage", ["legacy-census", "fence", "final"])
def test_initial_objects_dependency_survives_each_retention_recheck(world, monkeypatch, layout, stage):
    tree = world.job("owner")
    owner, _ = source(world, tree)
    outside, common = checkout(world, layout)
    objects = common / "objects"
    objects.rename(world.base / "old-objects")
    objects.symlink_to(owner / ".git" / "objects", target_is_directory=True)
    off_transaction(world, monkeypatch)
    recorded = dependencies.record(str(outside))
    assert not recorded.get("incomplete")
    inserted = []

    def insert(*, legacy=False):
        assert not inserted
        inserted.append(True)
        reader(world, {} if legacy else recorded)
        if legacy:
            with world.store.transaction() as tx:
                tx.execute("UPDATE events SET data_json='{}' WHERE kind='job.submitted' AND job_id='borrower'")

    if stage == "legacy-census":
        insert(legacy=True)
    elif stage == "fence":
        transaction = world.store.transaction

        @contextmanager
        def selecting(kind="state.changed", *args, **kwargs):
            if kind == "retention.selected" and not inserted:
                insert()
            with transaction(kind, *args, **kwargs) as tx:
                yield tx

        monkeypatch.setattr(world.store, "transaction", selecting)
    else:
        commit = rarch.Retirement.commit

        def committing(self, pool_bytes):
            if self.job_id == "owner" and not inserted:
                insert()
            return commit(self, pool_bytes)

        monkeypatch.setattr(rarch.Retirement, "commit", committing)
    result = maintain(world)
    assert inserted and tree.is_dir() and "owner" not in result["pruned"], result
    if stage == "legacy-census":
        assert "worktree-in-use" in result["pin_reasons"]["owner"]
    elif stage == "final":
        assert "worktree-in-use" in result["deferred"]["owner"]


@pytest.mark.parametrize("when", ["before-look", "after-look"])
def test_task_hold_takes_precedence_then_dependency_fence_keeps_attribution(state_daemon, monkeypatch, when):
    from tests.disk_floor_model import stamp
    from tests.fake.test_admission_ramp import fake_clock
    from tests.task_hold_support import hold, release, submit, attempts

    service, harness = state_daemon
    fake_clock(monkeypatch)
    job_id = submit(service, harness, "build", "r5-both-held")
    job = service.store.get_job(job_id)
    tree = folders.canonical(service.root / "worktrees" / "storage-owner")
    place = tree + "/source/.git/objects"
    fence = folders.exclusive_key(tree)
    assert service.store.acquire_lease(fence, "retention:storage-owner")
    with service.store.transaction() as tx:
        event = tx.execute("SELECT event_id,data_json FROM events WHERE kind='job.submitted' AND job_id=?",
                           (job_id,)).fetchone()
        data = json.loads(event[1])
        data["depends"] = {"git": [place]}
        tx.execute("UPDATE events SET data_json=? WHERE event_id=?", (json.dumps(data), event[0]))
    needs = service._needs(job)
    assert needs == [("git-storage", place)]
    original = dependencies.fences
    reads = []

    def fences(read, places):
        found = original(read, places)
        if not service.store.connection.in_transaction and not reads:
            reads.append(True)
            if when == "after-look":
                hold(service, "build", stamp(3600))
        return found

    if when == "before-look":
        hold(service, "build", stamp(3600))
    monkeypatch.setattr(dependencies, "fences", fences)
    holds, waiters, priorities = {}, {}, []
    assert service._fence_hold(job, folders.canonical(job["workdir"]), holds, waiters,
                               "standard", None, None, needs=needs,
                               priority_waiters=priorities)
    assert reads and holds[job_id]["reason"] == "task-held", holds
    assert service.store.get_job(job_id)["wait_reason"] == "task-held"
    assert not waiters and not priorities and not attempts(service, job_id)
    release(service, "build")
    holds.clear()
    assert service._fence_hold(service.store.get_job(job_id), folders.canonical(job["workdir"]),
                               holds, waiters, "standard", None, None,
                               needs=needs, priority_waiters=priorities)
    assert holds[job_id]["reason"] == "lease-held"
    assert {key: holds[job_id][key] for key in ("needs", "folder", "retiring")} == {
        "needs": "git-storage", "folder": place, "retiring": [fence]}
    assert waiters and priorities and not attempts(service, job_id)


@pytest.mark.parametrize("pointer", ["gitfile-leading", "gitfile-trailing", "commondir-trailing"])
@pytest.mark.parametrize("state", [
    pytest.param("running", marks=pytest.mark.xfail(
        strict=True, reason="R5-F1: stripping pointer path whitespace omits Git's actual storage"), id="live"),
    pytest.param("succeeded", id="exporting-control"),
])
def test_pointer_path_whitespace_keeps_the_storage_git_reads(world, monkeypatch, pointer, state):
    tree = world.job("owner")
    owner, commit = source(world, tree)
    outside = world.base / "outside"
    if pointer.startswith("gitfile"):
        outside.mkdir()
    else:
        repo(outside)
    # A relative gitdir path can start in a space. Absolute gitdir and common
    # paths can end in one. Git preserves those bytes before resolving the link.
    alias = outside / " metadata" if pointer == "gitfile-leading" else world.base / "metadata "
    alias.symlink_to(owner / ".git", target_is_directory=True)
    if pointer.startswith("gitfile"):
        named = alias.name if pointer == "gitfile-leading" else str(alias)
        (outside / ".git").write_text(f"gitdir: {named}\n")
    else:
        (outside / ".git" / "commondir").write_text(str(alias) + "\n")
    assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    off_transaction(world, monkeypatch)
    recorded = dependencies.record(str(outside))
    assert not recorded.get("incomplete"), recorded
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    if state == "running":
        assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
        assert folders.canonical(owner / ".git" / "objects") in recorded["git"]
        assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    else:
        assert "owner" in result["pruned"] and not tree.exists(), result


# The fixture reserves only fake attempts; it launches no daemon process/provider.
from tests.fake.test_state_contract import state_daemon  # noqa: E402,F401
