"""Archive bundle pack contents, compatibility, and command configuration."""
from __future__ import annotations

import json
import struct

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from subfleet import retention_qos as rqos
from subfleet.retention_delete_guard import ISOLATED_CONFIG
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
FILTER_PATTERN = r"^remote\..+\.partialclonefilter$"
OFFLINE = {"GIT_NO_LAZY_FETCH": "1"}
NETWORK_ORIGIN = "https://git.example.invalid/x.git"


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
    packed = packed_objects(w.repo, bundle, w.base / "pack.git", anchor)
    assert len(packed) == count
    assert packed.isdisjoint(prerequisite_objects)


def packed_objects(repo, bundle, probe, anchor):
    fmt = git(repo, "rev-parse", "--show-object-format")
    git(probe.parent, "init", "--quiet", "--bare", f"--object-format={fmt}", str(probe))
    (probe / "objects" / "info" / "alternates").write_text(str(repo / ".git" / "objects") + "\n")
    offset = rgit._pack_offset(bundle)
    with bundle.open("rb") as stream:
        stream.seek(offset)
        rgit.run(["index-pack", "--stdin", "--fix-thin"], git_dir=probe, stdin_file=stream)
    (index,) = (probe / "objects" / "pack").glob("*.idx")
    indexed = rgit.run(["show-index", f"--object-format={fmt}"], git_dir=probe,
                       stdin=index.read_bytes()).stdout.decode().splitlines()
    # --fix-thin can append delta bases. Only offsets before the original
    # pack's checksum belong to objects carried in the bundle.
    pack_end = bundle.stat().st_size - offset - len(anchor) // 2
    return {oid for start, oid, _ in (line.split() for line in indexed) if int(start) < pack_end}


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


@pytest.fixture
def prerequisite_repo(tmp_path):
    repo = tmp_path / "project"
    git(tmp_path, "init", "--quiet", "-b", "main", str(repo))
    folder = repo / "pkg" / "sub"
    folder.mkdir(parents=True)
    for number in range(5):
        (folder / f"f{number}").write_text(f"base content {number}\n")
    git(repo, "add", "pkg")
    git(repo, "commit", "--quiet", "-m", "prerequisite with nested blobs")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "remote", "add", "origin", NETWORK_ORIGIN)
    return repo


def write_config(repo, section, *settings):
    with (repo / ".git" / "config").open("a") as stream:
        stream.write(f"\n[{section}]\n")
        for setting in settings:
            stream.write(f"\t{setting}\n")


def blobless_clone(source, repo):
    git(source, "config", "uploadpack.allowFilter", "true")
    git(repo.parent, "clone", "--quiet", "--filter=blob:none", "--no-checkout",
        source.as_uri(), str(repo))
    assert git(repo, "config", "--get", EXTENSION_KEY, check=False) == ""
    assert git(repo, "config", "--get", "remote.origin.promisor") == "true"
    assert git(repo, "config", "--get", "remote.origin.partialclonefilter") == "blob:none"
    missing = git(repo, "rev-list", "--objects", "--missing=print", "HEAD", env=OFFLINE).splitlines()
    assert any(line.startswith("?") for line in missing)
    return repo


@pytest.fixture(params=[
    pytest.param((), id="extension"),
    pytest.param(("promisor = true",), id="promisor"),
    pytest.param(("promisor = yes",), id="promisor-yes"),
    pytest.param(("promisor = 1",), id="promisor-1"),
    pytest.param(("promisor = on",), id="promisor-on"),
    pytest.param(("promisor",), id="promisor-bare"),
    pytest.param(("partialclonefilter = blob:none",), id="filter-only"),
    pytest.param(("promisor = false", "partialclonefilter = blob:none"), id="false-with-filter"),
    pytest.param(None, id="filtered"),
])
def partial_repo(prerequisite_repo, request, tmp_path):
    repo = prerequisite_repo
    settings = request.param
    if settings is None:
        repo = blobless_clone(repo, tmp_path / "filtered")
        git(repo, "remote", "set-url", "origin", NETWORK_ORIGIN)
    elif not settings:
        write_config(repo, "extensions", "partialClone = origin")
    else:
        write_config(repo, 'remote "origin"', *settings)
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
    assert rgit.partial_clone(partial_repo)


def test_false_promisor_with_true_in_remote_name_uses_non_sparse_walk(prerequisite_repo, tmp_path,
                                                                     git_commands, monkeypatch):
    repo = prerequisite_repo
    write_config(repo, 'remote "untrue"', "promisor = false")
    add_side_commit(repo)
    git_dir = repo / ".git"
    rgit.create_bundle(git_dir, tmp_path / "complete.bundle", [SIDE], [HELD])
    assert_only_bundle_create_sets_non_sparse(git_commands)
    trust_temporary_directories(monkeypatch)
    assert rgit.scratch_reason(git_dir, tmp_path / "state", {"origin": NETWORK_ORIGIN}) is None


def test_invalid_promisor_keeps_default_pack_walk(prerequisite_repo, tmp_path, git_commands):
    repo = prerequisite_repo
    add_side_commit(repo)
    write_config(repo, 'remote "origin"', "promisor = maybe")
    git_dir = repo / ".git"
    with pytest.raises(rgit.GitError, match=r"config .* exited 128"):
        rgit.partial_clone(git_dir)
    rgit.create_bundle(git_dir, tmp_path / "invalid-promisor.bundle", [SIDE], [HELD])
    (create,) = bundle_creates(git_commands)
    assert not has_sparse_setting(create)


def test_promisor_yes_bundle_supplies_unchanged_staged_blob_offline(prerequisite_repo, tmp_path):
    sender = blobless_clone(prerequisite_repo, tmp_path / "sender")
    receiver = blobless_clone(prerequisite_repo, tmp_path / "receiver")
    git(sender, "checkout", "--quiet", "--force", "HEAD")
    git(sender, "config", "remote.origin.promisor", "yes")
    git(sender, "config", "--unset", "remote.origin.partialclonefilter")
    changed_path = "pkg/sub/f0"
    unchanged_path = "pkg/sub/f1"
    (sender / changed_path).write_text("new staged content\n")
    git(sender, "add", changed_path)
    unchanged = git(sender, "rev-parse", f":{unchanged_path}")
    assert unchanged == git(sender, "rev-parse", f"HEAD:{unchanged_path}")
    staged = {line.split()[1] for line in git(sender, "ls-files", "-s").splitlines()}
    git_dir = sender / ".git"
    parent = git(sender, "rev-parse", "refs/remotes/origin/main")
    anchor = rgit.make_anchor(git_dir, JOB, {"index": staged}, [parent])
    ref = rgit.anchor_ref(JOB, anchor)
    rgit.create_ref(git_dir, ref, anchor)
    bundle = tmp_path / "offline.bundle"
    rgit.create_bundle(git_dir, bundle, [ref], [HELD])

    receiver_git = receiver / ".git"
    probe = ["cat-file", "--batch-check=%(objectname) %(objecttype)"]
    assert rgit.run(probe, git_dir=receiver_git, env=OFFLINE,
                    stdin=f"{unchanged}\n".encode()).stdout == f"{unchanged} missing\n".encode()
    git(receiver, "fetch", "--quiet", str(bundle), f"{ref}:{ref}", env=OFFLINE)
    assert git(receiver, "rev-parse", ref) == anchor
    ordered = sorted(staged)
    received = rgit.run(probe, git_dir=receiver_git, env=OFFLINE,
                        stdin=("\n".join(ordered) + "\n").encode()).stdout.decode().splitlines()
    assert received == [f"{oid} blob" for oid in ordered]


def test_anchor_bundle_omits_admin_objects_already_in_prerequisite(prerequisite_repo, tmp_path):
    repo = prerequisite_repo
    receiver = tmp_path / "receiver"
    git(tmp_path, "clone", "--quiet", "--no-local", str(repo), str(receiver))
    parent = git(repo, "rev-parse", "HEAD")
    assert git(receiver, "rev-parse", "HEAD") == parent
    tree = git(repo, "rev-parse", "HEAD:pkg/sub")
    blob = git(repo, "rev-parse", "HEAD:pkg/sub/f0")
    git_dir = repo / ".git"
    anchor = rgit.make_anchor(git_dir, JOB, {"tree": {tree}, "blob": {blob}}, [parent])
    ref = rgit.anchor_ref(JOB, anchor)
    rgit.create_ref(git_dir, ref, anchor)
    bundle = tmp_path / "admin.bundle"
    rgit.create_bundle(git_dir, bundle, [ref], [HELD])
    packed = packed_objects(repo, bundle, tmp_path / "admin-pack.git", anchor)
    assert packed.isdisjoint({tree, blob})

    git(receiver, "fetch", "--quiet", str(bundle), f"{ref}:{ref}", env=OFFLINE)
    assert git(receiver, "rev-parse", ref) == anchor
    assert git(receiver, "rev-parse", f"{ref}:trees/{tree}") == tree
    assert git(receiver, "rev-parse", f"{ref}:blobs/{blob}") == blob
    assert git(receiver, "cat-file", "-t", tree, env=OFFLINE) == "tree"
    assert git(receiver, "cat-file", "-t", blob, env=OFFLINE) == "blob"


def test_bundle_creation_propagates_probe_cancellation(prerequisite_repo, tmp_path, monkeypatch):
    repo = prerequisite_repo
    add_side_commit(repo)
    bundle = tmp_path / "cancelled.bundle"

    def cancelled(*args, **kwargs):
        raise rgit.Cancelled("probe cancelled")

    monkeypatch.setattr(rgit, "partial_clone", cancelled)
    with pytest.raises(rgit.Cancelled, match="probe cancelled"):
        rgit.create_bundle(repo / ".git", bundle, [SIDE], [HELD])
    assert not bundle.exists()


def test_bundle_creation_keeps_default_when_probe_raises_git_error(prerequisite_repo, tmp_path, git_commands,
                                                                 monkeypatch):
    repo = prerequisite_repo
    add_side_commit(repo)
    bundle = tmp_path / "probe-error.bundle"

    def timed_out(*args, **kwargs):
        raise rgit.GitError("clone config probe timed out")

    monkeypatch.setattr(rgit, "partial_clone", timed_out)
    rgit.create_bundle(repo / ".git", bundle, [SIDE], [HELD])
    (create,) = bundle_creates(git_commands)
    assert not has_sparse_setting(create)
    assert rgit.bundle_heads(bundle).keys() == {SIDE}


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


def test_guarded_bundle_with_held_refs_uses_non_sparse_walk(prerequisite_repo, tmp_path, git_commands):
    """C-8.4: under a command guard with the delete path's isolated config, a
    bundle that excludes held refs from a complete clone is still built, with
    `pack.useSparse=false` on its `bundle create` only, and the guard's check
    sees that command. The side commit shares the prerequisite's tree, so the
    pack holds that one commit under either walk."""
    repo = prerequisite_repo
    add_side_commit(repo)
    git_dir = repo / ".git"
    bundle = tmp_path / "guarded.bundle"
    checked = []
    with rgit.command_guard(lambda args, git_dir, cwd: checked.append(args), env=ISOLATED_CONFIG):
        rgit.create_bundle(git_dir, bundle, [SIDE], [HELD])
    assert bundle.exists()
    assert_only_bundle_create_sets_non_sparse(git_commands)
    assert ("bundle", "create", str(bundle), SIDE, "--not", HELD) in checked
    assert rgit.bundle_heads(bundle).keys() == {SIDE}
    side = git(repo, "rev-parse", SIDE)
    assert packed_objects(repo, bundle, tmp_path / "guarded-pack.git", side) == {side}


def test_guarded_anchor_bundle_omits_admin_objects_in_prerequisite(prerequisite_repo, tmp_path):
    """C-8.4: the non-sparse walk takes effect under a command guard with the
    delete path's isolated config. An anchor naming a prerequisite subtree and
    blob packs neither (Git's default sparse walk packs both)."""
    repo = prerequisite_repo
    parent = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD:pkg/sub")
    blob = git(repo, "rev-parse", "HEAD:pkg/sub/f0")
    git_dir = repo / ".git"
    anchor = rgit.make_anchor(git_dir, JOB, {"tree": {tree}, "blob": {blob}}, [parent])
    ref = rgit.anchor_ref(JOB, anchor)
    rgit.create_ref(git_dir, ref, anchor)
    bundle = tmp_path / "guarded-admin.bundle"
    with rgit.command_guard(lambda args, git_dir, cwd: None, env=ISOLATED_CONFIG):
        rgit.create_bundle(git_dir, bundle, [ref], [HELD])
    packed = packed_objects(repo, bundle, tmp_path / "guarded-admin-pack.git", anchor)
    assert anchor in packed
    assert packed.isdisjoint({tree, blob})


def test_bundle_has_same_header_as_git_default(world, shape):
    w = world
    archive = archive_shape(w)
    manifest_git = json.loads((archive / "manifest.json").read_text())["git"]
    ref = manifest_git["anchor_ref"]
    default, actual = w.base / "default.bundle", w.base / "actual.bundle"
    git(w.repo, "bundle", "create", str(default), ref, "--not", HELD)
    rgit.create_bundle(w.repo / ".git", actual, [ref], [HELD], timeout=120)
    assert actual.read_bytes()[:rgit._pack_offset(actual)] == default.read_bytes()[:rgit._pack_offset(default)]


def test_repository_reason_reports_partial_clone(partial_repo, tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    remotes = rgit.network_remotes(partial_repo)
    assert remotes == {"origin": NETWORK_ORIGIN}
    assert rgit.scratch_reason(partial_repo, tmp_path / "state", remotes) == "partial clone"


def test_repository_reason_reports_unreadable_config(prerequisite_repo, tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    git_dir = prerequisite_repo / ".git"
    remotes = rgit.network_remotes(git_dir)
    with (git_dir / "config").open("a") as stream:
        stream.write("\n[broken\n")
    reason = rgit.scratch_reason(git_dir, tmp_path / "state", remotes)
    assert reason.startswith("config unreadable: ")


def test_bundle_creation_fails_with_unparseable_config(prerequisite_repo, tmp_path, git_commands):
    repo = prerequisite_repo
    add_side_commit(repo)
    git_dir = repo / ".git"
    with (git_dir / "config").open("a") as stream:
        stream.write("\n[broken\n")
    bundle = tmp_path / "broken.bundle"
    with pytest.raises(rgit.GitError, match="git bundle create exited 128"):
        rgit.create_bundle(git_dir, bundle, [SIDE], [HELD])
    (create,) = bundle_creates(git_commands)
    assert not has_sparse_setting(create)
    assert not bundle.exists()


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
        {EXTENSION_KEY, PROMISOR_PATTERN, FILTER_PATTERN}.intersection(call)
        for call in git_commands if builders.intersection(call)
    )
