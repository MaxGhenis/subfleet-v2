"""Archive bundle pack contents, compatibility, and command configuration."""
from __future__ import annotations

import json
import struct

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from subfleet import retention_qos as rqos
from tests.unit.retention_delete_world import delete_path, nested_job, request
from tests.unit.retention_delete_world import run as run_delete
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories

pytestmark = pytest.mark.retention_catch_up

FILE_COUNT = 300
JOB = "job-shape"
CHANGED = "pkg/file-0.txt"
HELD = "--glob=refs/remotes/origin/*"
SIDE = "refs/heads/side"
SPARSE_SETTING = "pack.useSparse=false"
EXTENSION_KEY = "extensions.partialclone"
PROMISOR_PATTERN = r"^remote\..+\.promisor$"


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


@pytest.fixture
def shape(world):
    w = world
    (w.repo / "pkg" / "sub").mkdir(parents=True)
    for number in range(FILE_COUNT):
        folder = w.repo / ("pkg/sub" if number % 2 else "pkg")
        (folder / f"file-{number}.txt").write_text(f"base content {number}\n")
    git(w.repo, "add", "pkg")
    git(w.repo, "commit", "--quiet", "-m", "nested tracked files")
    w.push()
    worktree = w.job(JOB)
    (worktree / CHANGED).write_text("new staged content\n")
    git(worktree, "add", CHANGED)
    return worktree


def archive_shape(w):
    result = run(w, slice_s=480)
    assert result["pruned"] == [JOB], result
    return w.root / "archive" / JOB


def test_anchor_bundle_omits_prerequisite_tree_objects(world, shape):
    w = world
    archive = archive_shape(w)
    bundle = archive / "commits.bundle"
    info = json.loads((archive / "manifest.json").read_text())["git"]
    anchor = info["anchor"]
    assert git(w.repo, "ls-tree", "--name-only", anchor) == "index"
    offset = rgit._pack_offset(bundle)
    with bundle.open("rb") as stream:
        stream.seek(offset)
        magic, version, count = struct.unpack(">4sII", stream.read(12))
    assert magic == b"PACK"
    assert version in (2, 3)
    unseen = git(w.repo, "rev-list", "--objects", anchor, "--not", HELD).splitlines()
    print(f"Anchor pack object count: {count}; rev-list count: {len(unseen)}")
    assert count <= min(len(unseen) + 1, 8), f"packed {count} objects, rev-list found {len(unseen)}"

    prerequisite = git(w.repo, "rev-parse", "refs/remotes/origin/main")
    with bundle.open("rb") as stream:
        header = stream.read(offset).splitlines()
    assert [line.split()[0] for line in header if line.startswith(b"-")] == [f"-{prerequisite}".encode()]
    prerequisite_objects = {
        line.split()[0] for line in git(w.repo, "rev-list", "--objects", f"{prerequisite}^{{tree}}").splitlines()
    }
    fmt = git(w.repo, "rev-parse", "--show-object-format")
    probe = w.base / "pack.git"
    git(w.base, "init", "--quiet", "--bare", f"--object-format={fmt}", str(probe))
    (probe / "objects" / "info" / "alternates").write_text(str(w.repo / ".git" / "objects") + "\n")
    with bundle.open("rb") as stream:
        stream.seek(offset)
        rgit.run(["index-pack", "--stdin", "--fix-thin"], git_dir=probe, stdin_file=stream)
    (index,) = (probe / "objects" / "pack").glob("*.idx")
    indexed = rgit.run(["show-index", f"--object-format={fmt}"], git_dir=probe,
                       stdin=index.read_bytes()).stdout.decode().splitlines()
    # --fix-thin can append delta bases. Only offsets before the original
    # pack's checksum belong to objects carried in the bundle.
    pack_end = bundle.stat().st_size - offset - len(anchor) // 2
    packed = {oid for start, oid, _ in (line.split() for line in indexed) if int(start) < pack_end}
    assert len(packed) == count
    assert packed.isdisjoint(prerequisite_objects)


def test_anchor_bundle_restores_the_worktree_and_every_staged_blob(world, shape):
    w = world
    before = snapshot(shape)
    index = git(shape, "ls-files", "-s")
    staged_blobs = sorted({line.split()[1] for line in index.splitlines()})
    staged_change = git(shape, "rev-parse", f":{CHANGED}")
    archive = archive_shape(w)
    assert not shape.exists()

    checked = rarch.check_archive(w.root, JOB)
    assert checked["ok"], checked["problems"]
    rarch.restore(w.root, JOB)
    assert snapshot(shape) == before
    assert git(shape, "ls-files", "-s") == index
    assert git(shape, "rev-parse", f":{CHANGED}") == staged_change
    assert CHANGED in git(shape, "diff", "--cached", "--stat")
    source_objects = rgit.run(["cat-file", "--batch-check=%(objectname) %(objecttype)"], git_dir=w.repo / ".git",
                              stdin=("\n".join(staged_blobs) + "\n").encode()).stdout.decode().splitlines()
    assert source_objects == [f"{oid} blob" for oid in staged_blobs]

    fresh = w.base / "fresh"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(archive / "commits.bundle"), "+refs/*:refs/r/*")
    for oid in staged_blobs:
        git(fresh, "cat-file", "-e", oid)


def add_side_commit(repo):
    parent = "refs/remotes/origin/main"
    tree = git(repo, "rev-parse", f"{parent}^{{tree}}")
    side = git(repo, "commit-tree", tree, "-p", parent, "-m", "unheld side commit")
    git(repo, "update-ref", SIDE, side)


@pytest.fixture(params=("extension", "promisor", "filtered"))
def partial_repo(world, request):
    w = world
    marker = request.param
    repo = w.repo
    if marker == "filtered":
        repo = w.base / "filtered"
        git(w.remote, "config", "uploadpack.allowFilter", "true")
        git(w.base, "clone", "--quiet", "--filter=blob:none", "--no-checkout", w.remote.as_uri(), str(repo))
        missing = git(repo, "rev-list", "--objects", "--missing=print", "refs/remotes/origin/main",
                      env={"GIT_NO_LAZY_FETCH": "1"}).splitlines()
        assert any(line.startswith("?") for line in missing)
    if marker in ("extension", "filtered"):
        # Modern Git can create a filtered clone with only the remote markers.
        # Record the legacy extension as well to exercise a clone with both.
        git(repo, "config", "extensions.partialClone", "origin")
    if marker == "promisor":
        git(repo, "config", "remote.origin.promisor", "true")
    assert git(repo, "config", "--get", EXTENSION_KEY, check=False) == ("" if marker == "promisor" else "origin")
    assert git(repo, "config", "--get", "remote.origin.promisor", check=False) == ("" if marker == "extension" else "true")
    git(repo, "remote", "set-url", "origin", "https://git.example.invalid/x.git")
    add_side_commit(repo)
    return repo / ".git"


@pytest.fixture
def git_commands(monkeypatch):
    calls = []
    original = rqos.argv

    def record(command):
        calls.append(tuple(command))
        return original(command)

    monkeypatch.setattr(rqos, "argv", record)
    return calls


def bundle_creates(calls):
    return [call for call in calls if ("bundle", "create") in zip(call, call[1:])]


def has_sparse_setting(call):
    return any(arg.lower().startswith("pack.usesparse=") for arg in call)


def test_partial_clone_keeps_default_pack_walk(partial_repo, tmp_path, git_commands):
    rgit.create_bundle(partial_repo, tmp_path / "partial.bundle", [SIDE], [HELD], timeout=120)
    assert len(bundle_creates(git_commands)) == 1
    assert not any(has_sparse_setting(call) for call in git_commands)


def assert_only_bundle_create_sets_non_sparse(calls):
    (create,) = bundle_creates(calls)
    assert not any(has_sparse_setting(call) for call in calls if call != create)
    assert create.count(SPARSE_SETTING) == 1, create
    setting = create.index(SPARSE_SETTING)
    assert create[setting - 1] == "-c"
    assert setting < create.index("bundle")


def test_only_bundle_create_sets_non_sparse(world, git_commands):
    w = world
    add_side_commit(w.repo)
    rgit.create_bundle(w.repo / ".git", w.base / "normal.bundle", [SIDE], [HELD], timeout=120)
    assert_only_bundle_create_sets_non_sparse(git_commands)


def test_archive_only_sets_non_sparse_for_bundle_create(world, git_commands):
    w = world
    w.job("job-normal")
    result = run(w, slice_s=480)
    assert result["pruned"] == ["job-normal"], result
    assert_only_bundle_create_sets_non_sparse(git_commands)


def test_bundle_has_same_header_as_git_default(world, shape):
    w = world
    archive = archive_shape(w)
    manifest_git = json.loads((archive / "manifest.json").read_text())["git"]
    ref = manifest_git["anchor_ref"]
    default, actual = w.base / "default.bundle", w.base / "actual.bundle"
    git(w.repo, "bundle", "create", str(default), ref, "--not", HELD)
    rgit.create_bundle(w.repo / ".git", actual, [ref], [HELD], timeout=120)
    assert actual.read_bytes()[:rgit._pack_offset(actual)] == default.read_bytes()[:rgit._pack_offset(default)]


def test_repository_reason_reports_partial_clone(partial_repo, world):
    remotes = rgit.network_remotes(partial_repo)
    assert remotes == {"origin": "https://git.example.invalid/x.git"}
    assert rgit.scratch_reason(partial_repo, world.root, remotes) == "partial clone"


def test_repository_reason_reports_unreadable_config(world):
    git_dir = world.repo / ".git"
    remotes = rgit.network_remotes(git_dir)
    with (git_dir / "config").open("a") as stream:
        stream.write("\n[broken\n")
    reason = rgit.scratch_reason(git_dir, world.root, remotes)
    assert reason.startswith("config unreadable: ")


def test_delete_bundle_skips_pack_setting_and_clone_detection(world, git_commands, monkeypatch):
    w = world
    job = "job-delete"
    tree = nested_job(w, job)
    inner = tree / "inner"
    git(tree, "init", "--quiet", "-b", "main", str(inner))
    git(inner, "commit", "--quiet", "--allow-empty", "-m", "only in the job")

    class BundleVerified(BaseException):
        """Stop the test before the delete path starts removing files."""

    path = delete_path(w)
    verify_bundle = path._verify_bundle

    def stop_after_verification(*args, **kwargs):
        verify_bundle(*args, **kwargs)
        raise BundleVerified

    monkeypatch.setattr(path, "_verify_bundle", stop_after_verification)
    with pytest.raises(BundleVerified):
        run_delete(path, request(job))
    creates = bundle_creates(git_commands)
    assert creates
    assert all("--not" not in create for create in creates)
    assert not any(has_sparse_setting(call) for call in git_commands)
    builders = {arg for create in creates for arg in create if arg.startswith("--git-dir=")}
    assert builders
    # Selection already checks the source repository. Bundling with an
    # empty held list must skip these additional reads in the builder.
    assert not any(
        EXTENSION_KEY in call or PROMISOR_PATTERN in call
        for call in git_commands if builders.intersection(call)
    )
