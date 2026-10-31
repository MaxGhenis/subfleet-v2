"""C-8.4: a repository alias retargeted while its alternates are read."""
from contextlib import contextmanager

import pytest

from subfleet import dependencies, retention_archive as rarch
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.unit.retention_world import git
from tests.unit.test_retention_dependency_units import repo


@pytest.mark.parametrize("state", ["running", "succeeded"], ids=["live", "exporting"])
def test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree(world, monkeypatch, state):
    tree = world.job("owner")
    source = repo(tree / "source")
    (source / "owner-only.txt").write_text("R3-F1 owner-only commit\n")
    git(source, "add", "owner-only.txt")
    git(source, "commit", "--quiet", "-m", "owner-only r3 evidence")
    commit = git(source, "rev-parse", "HEAD")
    old = repo(world.base / "old")
    assert git(old, "rev-parse", "HEAD") != commit
    outside = world.base / "outside"
    outside.mkdir()
    link = outside / ".git"
    link.symlink_to(old / ".git")
    alternate = old / ".git" / "objects" / "info" / "alternates"
    alternate.write_bytes(b"# first\n# second\n")
    before = alternate.stat()
    real = dependencies.open_regular
    swapped = []

    @contextmanager
    def opening(path, *args, **kwargs):
        with real(path, *args, **kwargs) as stream:
            if str(path) != str(alternate):
                yield stream
                return

            class Changing:
                def readline(self, size=-1):
                    line = stream.readline(size)
                    if not swapped:
                        swapped.append(True)
                        link.unlink()
                        link.symlink_to(source / ".git")
                    return line

                def __getattr__(self, name):
                    return getattr(stream, name)

            yield Changing()

    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "open_regular", opening)
        recorded = dependencies.record(str(outside))
    assert swapped
    after = alternate.stat()
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
        assert getattr(before, field) == getattr(after, field)
    assert git(outside, "cat-file", "-t", commit) == "commit"
    assert git(outside, "show", commit + ":owner-only.txt") == "R3-F1 owner-only commit"
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    assert "owner" not in result["pruned"], (recorded, result)
    assert tree.is_dir()
    assert recorded.get("incomplete"), recorded
    assert "dependency-incomplete" in result["pin_reasons"]["owner"]


@pytest.mark.parametrize("recorded", [
    pytest.param(False, id="legacy"),
    pytest.param(True, id="recorded"),
])
@pytest.mark.parametrize("state", ["running", "succeeded"], ids=["live", "exporting"])
def test_c8_4_recovery_discovers_incomplete_before_quarantine(world, monkeypatch, recorded, state):
    tree = world.job("owner")
    outside = world.base / "outside"
    alternate = outside / ".git" / "objects" / "info" / "alternates"
    alternate.parent.mkdir(parents=True)
    alternate.write_bytes(b"# valid\n")

    def insert(data):
        world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                            kind="dispatch", state=state, sandbox="read-only",
                            workdir=str(outside), prompt_path="/p")
        world.store.add_event("job.submitted", job_id="borrower", data=data)
        if state == "succeeded":
            world.store.acquire_lease("out:/outside/result", "borrower")

    # A legacy job predates both passes. Its alternates become malformed
    # between them, so recovery must use the resumed pass's fresh census.
    if not recorded:
        insert({})
    original = rarch.Retirement.quarantine

    class Crash(BaseException):
        pass

    def crash(self):
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(rarch.Retirement, "quarantine", crash)
        with pytest.raises(Crash):
            maintain(world)
    assert tree.is_dir()
    alternate.write_bytes(b'"bad\\z"\n')
    if recorded:
        insert({"depends": {"incomplete": [{"file": str(alternate), "reason": "invalid Git C escape"}]}})
    observations = []

    def observing(self):
        try:
            return original(self)
        finally:
            observations.append(tree.is_dir())

    monkeypatch.setattr(rarch.Retirement, "quarantine", observing)
    result = maintain(world)
    # The final rollback is insufficient: a live job must not lose its path
    # during recovery, before the incomplete evidence is finally discovered.
    assert tree.is_dir() and "owner" not in result["pruned"], result
    assert observations == [True], (observations, result)
