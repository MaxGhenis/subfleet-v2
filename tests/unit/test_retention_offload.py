"""Settled retention archives on another volume and back (C-8.8, D-ARCHIVE-OFFLOAD1).

Every archive here is a real one: a `retention_world.World` job retired by
`retention.maintenance`, so manifests, summaries, stored-file names and bundles
are the retention builder's own. Stand-in volumes are folders in the test's
temporary directory, behind the one seam for volume identity,
`retention_volume.volumes` (`Volumes` below). Crashes are injected through
`retention_offload.HOOK`, which the code calls at each named step.

The brief's invariants, each with what holds it:

1. At every step of an offload or a recall, each stored file and the bundle
   exist in at least one place whose bytes match the manifest, and the archive
   checks clean through the resolver: `test_every_step_keeps_one_good_copy`.
2. Never an archive whose job has a retention folder; no write outside
   `<state>/archive/<name>/`, its lock and log, and `DIR/subfleet-archive/<name>/`:
   `test_skip_*retention_folder*`, `test_offload_and_recall_write_only_their_own_paths`.
3. Offload then recall leaves `<state>/archive/<name>/` with the same files,
   bytes and modes: `test_offload_then_recall_gives_back_the_archive`.
4. `restore --check` and `restore --to` give the same verdict and trees from
   an offloaded archive (volume mounted) as from the internal one:
   `test_restore_from_an_offloaded_archive_matches_the_internal_one`,
   `test_check_verdicts_match_for_damage_inside_and_outside`.
5. A rerun after a crash at any step ends where an uninterrupted run ends:
   `test_a_crash_at_any_offload_step_then_a_rerun_ends_the_same`,
   `test_a_crash_at_any_recall_step_then_a_rerun_ends_the_same`.
6. `--dry-run` writes nothing: `test_dry_run_writes_nothing`.

`OFFLOAD_PROP_EXAMPLES` raises the property example count.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_offload as rof
from subfleet import retention_volume as rvol
from tests.unit.retention_world import Clock, World, git, snapshot

EXAMPLES = int(os.environ.get("OFFLOAD_PROP_EXAMPLES", "8"))
PROPERTY = settings(max_examples=EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
EXT_UUID = "6A1E0F7C-0FF1-4AD5-9C3B-5D2B7E0A1B2C"
INTERNAL = rvol.Volume(uuid="B863062A-EC68-4C4A-8B8D-CE3815995D30", name="Macintosh HD", mount=Path("/"),
                       fstype="apfs", local=True, owners=True, readonly=False, free=10 ** 15, encrypted=True)
VOLATILE = ("started_at", "updated_at", "verified_at", "offloaded_at")


class Crash(BaseException):
    """A crash injected at a step; the code under test catches no BaseException."""


class Volumes:
    """The stand-in for `retention_volume.volumes`: folders mounted by UUID."""

    def __init__(self) -> None:
        self.mounted: list[rvol.Volume] = [INTERNAL]

    def reset(self) -> None:
        self.mounted = [INTERNAL]

    def mount(self, folder: Path, **overrides) -> rvol.Volume:
        fields = dict(uuid=EXT_UUID, name="Backup ☃", mount=Path(os.path.realpath(folder)), fstype="apfs",
                      local=True, owners=True, readonly=False, free=10 ** 12, encrypted=True)
        fields.update(overrides)
        volume = rvol.Volume(**fields)
        self.mounted = [v for v in self.mounted if v.uuid != volume.uuid or v is INTERNAL] + [volume]
        return volume

    def unmount(self, uuid: str) -> None:
        self.mounted = [v for v in self.mounted if v.uuid != uuid]

    def __call__(self, path: Path | None = None) -> list[rvol.Volume]:
        if path is None:
            return list(self.mounted)
        real = os.path.realpath(path)
        holding = [v for v in self.mounted
                   if real == str(v.mount) or real.startswith(str(v.mount).rstrip("/") + "/")]
        return [max(holding, key=lambda v: len(str(v.mount)))]


# --- real archives -------------------------------------------------------------------------------

DIRS = ["", "d", "d/e", "ö", "日本"]
NAMES = ["a", "b.txt", "é", "sp ace", "empty"]
rel_path = st.tuples(st.sampled_from(DIRS), st.sampled_from(NAMES)).map(lambda t: f"{t[0]}/{t[1]}" if t[0] else t[1])
content = st.binary(max_size=3000) | st.just(b"")
files = st.lists(st.tuples(rel_path, content), max_size=5, unique_by=lambda f: f[0])
SHAPES = st.fixed_dictionaries({
    "job": files,                     # the job directory's files
    "worktree": st.booleans(),        # a registration, so a bundle; or no worktree, no bundle
    "tree": files,                    # untracked files in the worktree
    "commit": st.booleans(),          # an unpushed commit in the worktree
    "hardlink": st.booleans(),        # two names of one stored file
})


class Archives:
    """Real archives built by retention's own pass, once per shape, in one World."""

    def __init__(self, base: Path):
        self.world = World(base)
        self.count = 0
        self.cache: dict[str, Path] = {}

    def build(self, shape: dict) -> Path:
        key = repr(shape)
        if key in self.cache:
            return self.cache[key]
        self.count += 1
        job_id = f"job-{self.count}"
        w = self.world
        job_files = {p: data for p, data in shape["job"]} or None
        wt = w.job(job_id, worktree=shape["worktree"], files=job_files)
        if wt is not None:
            for p, data in shape["tree"]:
                (wt / p).parent.mkdir(parents=True, exist_ok=True)
                (wt / p).write_bytes(data)
            if shape["commit"]:
                (wt / "committed.txt").write_text(f"unpushed work of {job_id}\n")
                git(wt, "add", "committed.txt")
                git(wt, "commit", "--quiet", "-m", "unpushed")
        if shape["hardlink"]:
            first = sorted((w.root / "jobs" / job_id).rglob("*"))
            first = [p for p in first if p.is_file()]
            if first:
                os.link(first[0], w.root / "jobs" / job_id / "hardlink-copy")
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {}, clock=Clock())
        assert result["pruned"] == [job_id], result
        archive = w.root / "archive" / job_id
        self.cache[key] = archive
        return archive

    def close(self) -> None:
        self.world.close()


@pytest.fixture(scope="module", autouse=True)
def quick_flushes():
    """`F_FULLFSYNC` flushes the whole drive's cache; on a loaded host
    (2026-11-13: load average near 100) one took over 45 s, in retention's
    builder and in git's object writes alike. Durability on the medium is not
    what these properties test (a crash here is an exception at a step), so
    flushes are plain `fsync` and git's are off; that each external file and
    folder is flushed is `test_every_external_file_and_folder_is_flushed`."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rfs, "fullsync", os.fsync)
        mp.setenv("GIT_CONFIG_COUNT", "1")
        mp.setenv("GIT_CONFIG_KEY_0", "core.fsync")
        mp.setenv("GIT_CONFIG_VALUE_0", "none")
        yield


@pytest.fixture(scope="module")
def archives(tmp_path_factory, quick_flushes):
    built = Archives(tmp_path_factory.mktemp("offload-world"))
    yield built
    built.close()


@pytest.fixture
def volumes(monkeypatch):
    stand_in = Volumes()
    monkeypatch.setattr(rvol, "volumes", stand_in)
    monkeypatch.setattr(rof, "HOOK", None)
    return stand_in


@dataclass
class Env:
    base: Path
    root: Path
    target: Path            # DIR, on the stand-in volume
    volume: rvol.Volume

    def archive(self, name: str) -> Path:
        return self.root / "archive" / name

    def external(self, name: str) -> Path:
        return self.target / rof.FOLDER / name


def fresh(parent: Path, volumes: Volumes, **volume) -> Env:
    base = Path(tempfile.mkdtemp(dir=parent, prefix="env-"))
    root = base / "state"
    (root / "archive").mkdir(parents=True, mode=0o700)
    mount = base / "vol-a"
    (mount / "backups").mkdir(parents=True)
    volumes.reset()
    return Env(base, root, mount / "backups", volumes.mount(mount, **volume))


def install(src: Path, env: Env, name: str | None = None) -> str:
    name = name or src.name
    shutil.copytree(src, env.archive(name), symlinks=True)
    return name


def sha(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def tree_state(path: Path) -> dict | None:
    """rel -> (type, mode, bytes, mtime for files); directories without their mtimes."""
    if not path.exists():
        return None
    out = {}
    for rel, (kind, mode, data, mtime) in snapshot(path).items():
        out[rel] = (kind, mode, data, mtime if kind == "f" else None)
    return out


def stub_state(env: Env, name: str) -> dict | None:
    stub = rof.read_stub(env.root, name)
    return None if stub is None else {k: v for k, v in stub.items() if k not in VOLATILE}


def final_state(env: Env, name: str) -> dict:
    internal = tree_state(env.archive(name)) or {}
    internal.pop(rof.STUB, None)
    leftovers = sorted(n for n in os.listdir(env.root / "archive") if n.startswith(".lock."))
    return {"internal": internal, "stub": stub_state(env, name), "external": tree_state(env.external(name)),
            "locks": leftovers}


def offload(env: Env, **kwargs) -> dict:
    return rof.offload(env.root, env.target, **kwargs)


def outcomes(report: dict) -> dict[str, str]:
    return {r["archive"]: r["outcome"] for r in report["results"]}


def skips(report: dict) -> dict[str, str]:
    return {s["archive"]: s["reason"] for s in report["skipped"]}


# --- invariant 1: one good copy at every step ------------------------------------------------------

def checker(env: Env, name: str, seen: list[str]):
    """At each step: every stored file and the bundle have a copy whose bytes
    match, and the archive checks clean through the resolver."""
    expected = rof.plan(env.root, name)
    bundle_sha = sha(env.archive(name) / expected.bundle) if expected.bundle else None

    def hook(step: str) -> None:
        seen.append(step)
        inside, outside = env.archive(name), env.external(name)
        for store, (_, digest) in expected.stores.items():
            assert digest in (sha(inside / "files" / store), sha(outside / "files" / store)), (step, store)
        if expected.bundle:
            assert bundle_sha in (sha(inside / expected.bundle), sha(outside / expected.bundle)), step
        checked = rarch._check_archive(env.root, name)
        assert checked["ok"], (step, checked["problems"])
    return hook


@PROPERTY
@given(shape=SHAPES, keep=st.booleans(), drop=st.booleans())
def test_every_step_keeps_one_good_copy(archives, volumes, tmp_path, monkeypatch, shape, keep, drop):
    env = fresh(tmp_path, volumes)
    name = install(archives.build(shape), env)
    seen: list[str] = []
    monkeypatch.setattr(rof, "HOOK", checker(env, name, seen))
    report = offload(env, keep_internal=keep)
    assert outcomes(report) == {name: "verified" if keep else "offloaded"}, report
    assert "verified" in seen and ("offloaded" in seen) is (not keep)
    report = rof.recall(env.root, name, drop_external=drop)
    assert outcomes(report) == {name: "recalled"}, report
    assert "stub-dropped" in seen
    assert rof.read_stub(env.root, name) is None


# --- invariant 3: offload then recall gives the archive back ----------------------------------------

@PROPERTY
@given(shape=SHAPES, drop=st.booleans())
def test_offload_then_recall_gives_back_the_archive(archives, volumes, tmp_path, shape, drop):
    env = fresh(tmp_path, volumes)
    name = install(archives.build(shape), env)
    before = tree_state(env.archive(name))
    report = offload(env)
    assert outcomes(report) == {name: "offloaded"}, report
    found = rof.plan(env.root, name)
    inside = set(os.listdir(env.archive(name)))
    assert inside == {"manifest.json", "summary.json", "rows.json", rof.STUB}, inside
    assert rof.read_stub(env.root, name)["state"] == "offloaded"
    for store, (_, digest) in found.stores.items():
        assert sha(env.external(name) / "files" / store) == digest
        assert stat.S_IMODE(os.lstat(env.external(name) / "files" / store).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(env.external(name)).st_mode) == 0o700
    assert outcomes(rof.recall(env.root, name, drop_external=drop)) == {name: "recalled"}
    assert tree_state(env.archive(name)) == before
    assert env.external(name).exists() is (not drop)


# --- invariant 5: crash at any step, rerun, same end ------------------------------------------------

def crash_at(step: str, occurrence: int):
    count = {"n": 0}

    def hook(name: str) -> None:
        if name == step:
            count["n"] += 1
            if count["n"] == occurrence:
                raise Crash(f"{step} #{occurrence}")
    return hook


def points(steps: list[str]) -> list[tuple[str, int]]:
    seen: dict[str, int] = {}
    out = []
    for s in steps:
        seen[s] = seen.get(s, 0) + 1
        out.append((s, seen[s]))
    return out


def tear(env: Env, name: str, step: str, occurrence: int, found: rof.Plan) -> None:
    """What a power cut does to the file just written before its sync: half of it."""
    victim = None
    if step == "copy-file":
        victim = env.external(name) / "files" / sorted(found.stores)[occurrence - 1]
    elif step == "copy-bundle":
        victim = env.external(name) / found.bundle
    if victim is not None and victim.stat().st_size:
        with open(victim, "r+b") as stream:
            stream.truncate(victim.stat().st_size // 2)


def stale_lock(env: Env, name: str) -> None:
    """A killed process leaves its lock file (the kernel drops the lock itself)."""
    (env.root / "archive" / f".lock.{name}").touch()


@PROPERTY
@given(shape=SHAPES, keep=st.booleans())
def test_a_crash_at_any_offload_step_then_a_rerun_ends_the_same(archives, volumes, tmp_path, monkeypatch,
                                                               shape, keep):
    src = archives.build(shape)
    reference = fresh(tmp_path, volumes)
    name = install(src, reference)
    steps: list[str] = []
    monkeypatch.setattr(rof, "HOOK", steps.append)
    offload(reference, keep_internal=keep)
    monkeypatch.setattr(rof, "HOOK", None)
    expected = final_state(reference, name)
    found = rof.plan(reference.root, name)
    assert expected["stub"]["state"] == ("verified" if keep else "offloaded")
    for step, occurrence in points(steps):
        env = fresh(tmp_path, volumes)
        install(src, env)
        monkeypatch.setattr(rof, "HOOK", crash_at(step, occurrence))
        with pytest.raises(Crash):
            offload(env, keep_internal=keep)
        monkeypatch.setattr(rof, "HOOK", None)
        tear(env, name, step, occurrence, found)
        stale_lock(env, name)
        report = offload(env, keep_internal=keep)
        assert not report["refused"], report
        assert final_state(env, name) == expected, (step, occurrence)


@PROPERTY
@given(shape=SHAPES, drop=st.booleans())
def test_a_crash_at_any_recall_step_then_a_rerun_ends_the_same(archives, volumes, tmp_path, monkeypatch,
                                                              shape, drop):
    src = archives.build(shape)
    reference = fresh(tmp_path, volumes)
    name = install(src, reference)
    original = tree_state(reference.archive(name))
    offload(reference)
    steps: list[str] = []
    monkeypatch.setattr(rof, "HOOK", steps.append)
    rof.recall(reference.root, name, drop_external=drop)
    monkeypatch.setattr(rof, "HOOK", None)
    expected = final_state(reference, name)
    assert expected["internal"] == original and expected["stub"] is None
    for step, occurrence in points(steps):
        env = fresh(tmp_path, volumes)
        install(src, env)
        offload(env)
        monkeypatch.setattr(rof, "HOOK", crash_at(step, occurrence))
        with pytest.raises(Crash):
            rof.recall(env.root, name, drop_external=drop)
        monkeypatch.setattr(rof, "HOOK", None)
        stale_lock(env, name)
        if rof.read_stub(env.root, name) is not None:
            assert outcomes(rof.recall(env.root, name, drop_external=drop)) == {name: "recalled"}
        assert final_state(env, name) == expected, (step, occurrence)


# --- invariant 6: --dry-run writes nothing ----------------------------------------------------------

@PROPERTY
@given(shapes=st.lists(SHAPES, min_size=1, max_size=3), keep=st.booleans(),
       limit=st.none() | st.integers(1, 3), min_bytes=st.sampled_from([0, 1, 5000]))
def test_dry_run_writes_nothing(archives, volumes, tmp_path, shapes, keep, limit, min_bytes):
    env = fresh(tmp_path, volumes)
    for i, shape in enumerate(shapes):
        install(archives.build(shape), env, f"arch-{i}")
    before = snapshot(env.base)
    report = offload(env, dry_run=True, keep_internal=keep, limit=limit, min_bytes=min_bytes)
    assert snapshot(env.base) == before
    assert report["results"] == [] and not report["refused"]
    real = offload(env, keep_internal=keep, limit=limit, min_bytes=min_bytes)
    assert [b["archive"] for b in report["batch"]] == [r["archive"] for r in real["results"]]
    assert report["batch_bytes"] == real["batch_bytes"]
    assert sorted(report["batch"], key=lambda b: (-b["bytes"], b["archive"])) == report["batch"]


# --- example tests ------------------------------------------------------------------------------------

RICH = {"job": [("a", b"alpha\n"), ("d/e/b.txt", b"nested\n"), ("ö/é", "ünïcödé".encode()), ("empty", b""),
                ("d/sp ace", os.urandom(5000))],
        "worktree": True, "tree": [("b.txt", b"untracked\n"), ("日本/a", b"\x00\x01binary")],
        "commit": True, "hardlink": True}
PLAIN = {"job": [("a", b"only the job directory\n")], "worktree": False, "tree": [], "commit": False,
         "hardlink": False}


@pytest.fixture
def env(volumes, tmp_path):
    return fresh(tmp_path, volumes)


def test_the_stub_moves_copying_verified_offloaded(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    states: list[tuple[str, str | None, bool]] = []

    def hook(step: str) -> None:
        stub = rof.read_stub(env.root, name)
        states.append((step, stub["state"], bool(stub.get("removing"))))
    monkeypatch.setattr(rof, "HOOK", hook)
    offload(env)
    order = []
    for _, state, removing in states:
        if not order or order[-1] != (state, removing):
            order.append((state, removing))
    assert order == [("copying", False), ("verified", False), ("verified", True), ("offloaded", True)]
    assert dict((s, (st_, r)) for s, st_, r in states)["remove-file"] == ("verified", True)
    stub = rof.read_stub(env.root, name)
    found = rof.plan(env.root, name)
    assert stub["volume"] == {"uuid": EXT_UUID, "name": "Backup ☃", "fstype": "apfs", "encrypted": True}
    assert stub["path"] == f"backups/{rof.FOLDER}/{name}"
    assert stub["manifest_sha256"] == found.manifest_sha256
    assert (stub["files"], stub["bytes"], stub["bundle_bytes"]) == (len(found.stores), found.files_bytes,
                                                                    found.bundle_bytes)
    assert stub["format"] == rof.FORMAT and stub["subfleet_version"] and stub["bundle_sha256"]
    log = [json.loads(line) for line in (env.root / "archive" / rof.LOG).read_text().splitlines()]
    assert [(r["op"], r["archive"], r["outcome"]) for r in log] == [("offload", name, "offloaded")]


def test_every_external_file_and_folder_is_flushed(archives, env, monkeypatch):
    """Each file and folder offload creates on the target gets `rfs.fullsync`
    (`F_FULLFSYNC`), as does the stub's folder."""
    name = install(archives.build(RICH), env)
    flushed: set[tuple[int, int]] = set()

    def record(fd: int) -> None:
        st = os.fstat(fd)
        flushed.add((st.st_dev, st.st_ino))
    monkeypatch.setattr(rfs, "fullsync", record)
    assert outcomes(offload(env)) == {name: "offloaded"}
    made = [env.external(name), env.target / rof.FOLDER, env.archive(name)]
    made += [Path(d) / n for d, dirs, names in os.walk(env.external(name)) for n in dirs + names]
    missing = [str(p) for p in made if (p.lstat().st_dev, p.lstat().st_ino) not in flushed]
    assert missing == []


def test_keep_internal_copies_and_verifies_and_deletes_nothing(archives, env):
    name = install(archives.build(RICH), env)
    before = tree_state(env.archive(name))
    assert outcomes(offload(env, keep_internal=True)) == {name: "verified"}
    after = tree_state(env.archive(name))
    after.pop(rof.STUB)
    assert after == before
    stub = rof.read_stub(env.root, name)
    assert stub["state"] == "verified" and stub["keep_internal"] and not stub["removing"]
    assert skips(offload(env, keep_internal=True))[name].startswith("already copied")
    # A later run without --keep-internal turns the backup into an offload.
    assert outcomes(offload(env)) == {name: "offloaded"}


def test_a_copy_that_does_not_read_back_stops_with_the_internal_copy_untouched(archives, env, monkeypatch):
    """Mutant 1 (skip the read-back) dies here."""
    name = install(archives.build(RICH), env)
    before = tree_state(env.archive(name))
    original = rof._copy_into

    def spoiled(src_dir_fd, n, dst_dir_fd, dst_name, mode):
        original(src_dir_fd, n, dst_dir_fd, dst_name, mode)
        fd = os.open(dst_name, os.O_WRONLY | os.O_APPEND, dir_fd=dst_dir_fd)
        try:
            os.write(fd, b"!")        # a bad drive: one byte too many
        finally:
            os.close(fd)
    monkeypatch.setattr(rof, "_copy_into", spoiled)
    report = offload(env)
    assert outcomes(report) == {name: "failed"} and "did not read back" in report["results"][0]["error"]
    after = tree_state(env.archive(name))
    after.pop(rof.STUB)
    assert after == before
    assert rof.read_stub(env.root, name)["state"] == "copying"
    assert rarch.check_archive(env.root, name)["ok"]
    monkeypatch.setattr(rof, "_copy_into", original)
    assert outcomes(offload(env)) == {name: "offloaded"}      # the next run replaces the bad copies
    assert rarch.check_archive(env.root, name)["ok"]


def test_a_resumed_offload_reads_the_external_copy_back_before_removing(archives, env, monkeypatch):
    """`verified` re-verifies: a copy damaged since is repaired from the
    internal files before any of them goes."""
    name = install(archives.build(RICH), env)
    monkeypatch.setattr(rof, "HOOK", crash_at("verified", 1))
    with pytest.raises(Crash):
        offload(env)
    victim = sorted((env.external(name) / "files").iterdir())[0]
    victim.write_bytes(b"damaged after verification")
    seen: list[str] = []

    def record(step: str) -> None:
        if step == "remove-file":
            assert sha(victim) is not None and "repaired" in seen    # repaired before the first removal
        seen.append(step)
    monkeypatch.setattr(rof, "HOOK", record)
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert "repaired" in seen and "reverified" not in seen
    monkeypatch.setattr(rof, "HOOK", None)
    assert rarch.check_archive(env.root, name)["ok"]


def test_a_damaged_external_copy_with_nothing_left_inside_fails_loudly(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    monkeypatch.setattr(rof, "HOOK", crash_at("removed", 1))
    with pytest.raises(Crash):
        offload(env)
    monkeypatch.setattr(rof, "HOOK", None)
    victim = sorted((env.external(name) / "files").iterdir())[0]
    victim.write_bytes(b"damaged after the internal copy went")
    report = offload(env)
    assert outcomes(report) == {name: "failed"}
    assert "no internal copy is left" in report["results"][0]["error"]
    stub = rof.read_stub(env.root, name)
    assert stub["state"] == "verified" and stub["removing"]
    assert not rarch.check_archive(env.root, name)["ok"]


def test_skip_an_archive_whose_job_has_a_retention_folder(archives, env):
    """Mutant 3 (ignore the retention-folder skip) dies here."""
    name = install(archives.build(PLAIN), env)
    (env.root / "retention" / name).mkdir(parents=True)
    before = snapshot(env.base)
    report = offload(env)
    assert "retention folder" in skips(report)[name] and report["results"] == []
    assert snapshot(env.base) == before


def test_skip_a_numbered_archive_whose_job_has_a_retention_folder(archives, env):
    """Mutant 4 (look the folder up without stripping `.N`) dies here."""
    src = archives.build(PLAIN)
    name = install(src, env, f"{src.name}.2")
    (env.root / "retention" / src.name).mkdir(parents=True)
    report = offload(env)
    assert "retention folder" in skips(report)[name] and report["results"] == []
    assert rof.job_of("job-7.12") == "job-7" and rof.job_of("job-7") == "job-7"


def test_a_job_id_ending_in_a_number_is_looked_up_whole_too(archives, env):
    src = archives.build(PLAIN)
    name = install(src, env, "job.3")
    (env.root / "retention" / "job.3").mkdir(parents=True)
    assert "retention folder" in skips(offload(env))[name]


def test_skip_reasons(archives, env):
    src = archives.build(PLAIN)
    install(src, env, "done")
    assert outcomes(offload(env)) == {"done": "offloaded"}
    install(src, env, "bad-summary")
    (env.archive("bad-summary") / "summary.json").write_text("{not json")
    install(src, env, "bad-manifest")
    (env.archive("bad-manifest") / "manifest.json").write_text("{not json")
    install(src, env, "changed-manifest")
    manifest = json.loads((env.archive("changed-manifest") / "manifest.json").read_text())
    manifest["created_at"] = "edited"
    (env.archive("changed-manifest") / "manifest.json").write_text(json.dumps(manifest))
    install(src, env, "bad-stub")
    (env.archive("bad-stub") / rof.STUB).write_text("[]")
    install(src, env, "small")
    report = offload(env, min_bytes=10 ** 9)
    reasons = skips(report)
    assert reasons["done"].startswith("already offloaded to 'Backup ☃' (" + EXT_UUID)
    assert reasons["bad-summary"].startswith("summary.json unreadable")
    assert reasons["bad-stub"].startswith("offload.json unreadable")
    assert "under --min-bytes" in reasons["small"]
    assert report["results"] == []
    reasons = skips(offload(env))
    assert reasons["bad-manifest"].startswith("manifest.json unreadable")
    assert "is not the one summary.json recorded" in reasons["changed-manifest"]


def test_skip_an_archive_with_nothing_to_move(archives, env):
    src = archives.build(PLAIN)
    name = install(src, env)
    manifest_path = env.archive(name) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for tree in manifest["trees"].values():
        for entry in tree["entries"]:
            entry.pop("store", None)
    manifest_path.write_text(json.dumps(manifest))
    summary_path = env.archive(name) / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary.pop("manifest_sha256")
    summary_path.write_text(json.dumps(summary))
    assert "nothing to move" in skips(offload(env))[name]


def test_an_offload_in_progress_to_another_target_is_left_for_that_target(archives, env, volumes, tmp_path):
    name = install(archives.build(PLAIN), env)
    assert outcomes(offload(env, keep_internal=True)) == {name: "verified"}
    other = tmp_path / "vol-b"
    other.mkdir()
    volumes.mount(other, uuid="0D9C0A44-6E21-4C44-8F5B-4B1C2B0B6E11", name="Other")
    report = rof.offload(env.root, other)
    assert "run it with that target" in skips(report)[name] and report["results"] == []


def test_the_lock_keeps_a_second_run_and_a_restore_out(archives, env):
    name = install(archives.build(PLAIN), env)
    with rof.ArchiveLock(env.root, name):
        report = offload(env)
        assert outcomes(report) == {name: "skipped"} and "busy" in report["results"][0]["reason"]
        with pytest.raises(rof.Busy):
            rarch.check_archive(env.root, name)
        with pytest.raises(rof.Busy):
            rarch.restore(env.root, name, to=env.base / "out")
        assert outcomes(rof.recall(env.root, name)) == {name: "skipped"}
    assert not (env.root / "archive" / f".lock.{name}").exists()
    assert rof.read_stub(env.root, name) is None
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert not (env.root / "archive" / f".lock.{name}").exists()


def test_a_lock_file_removed_while_waiting_is_taken_again(env):
    (env.root / "archive" / "x").mkdir()
    lock = rof.ArchiveLock(env.root, "x")
    real_stat = os.stat
    calls = {"n": 0}

    def swap(path, *args, **kwargs):
        if str(path) == str(lock.path) and calls["n"] == 0:
            calls["n"] += 1
            os.unlink(lock.path)                   # another holder finished and removed it
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "stat", swap)
        with lock:
            assert lock.path.exists()
    assert calls["n"] == 1 and not lock.path.exists()


# --- refused targets ------------------------------------------------------------------------------------

@pytest.mark.parametrize("overrides, phrase", [
    ({"local": False, "fstype": "smbfs"}, "not a local volume"),
    ({"fstype": "exfat"}, "does not keep owners"),
    ({"fstype": "msdos"}, "does not keep owners"),
    ({"owners": False}, "does not keep owners"),
    ({"encrypted": False}, "not encrypted"),
    ({"encrypted": None}, "unknown encryption"),
    ({"readonly": True}, "read-only"),
    ({"uuid": None}, "no UUID"),
    ({"free": 1000}, "plus 10%"),
])
def test_refused_targets_write_nothing(archives, volumes, tmp_path, overrides, phrase):
    """Mutant 5 (accept an exFAT target) dies here."""
    env = fresh(tmp_path, volumes, **overrides)
    install(archives.build(RICH), env)
    before = snapshot(env.base)
    report = offload(env)
    assert any(phrase in r for r in report["refused"]), report["refused"]
    assert report["results"] == [] and snapshot(env.base) == before


def test_free_space_must_cover_the_batch_plus_ten_percent(archives, volumes, tmp_path):
    src = archives.build(RICH)
    probe = fresh(tmp_path, volumes)
    install(src, probe)
    need = offload(probe, dry_run=True)["batch_bytes"]
    env = fresh(tmp_path, volumes, free=need + need // 10 - 1)
    name = install(src, env)
    assert any("plus 10%" in r for r in offload(env)["refused"])
    env = fresh(tmp_path, volumes, free=need + need // 10)
    install(src, env)
    assert outcomes(offload(env)) == {name: "offloaded"}


def test_an_unencrypted_volume_is_accepted_only_when_asked(archives, volumes, tmp_path):
    env = fresh(tmp_path, volumes, encrypted=False)
    name = install(archives.build(PLAIN), env)
    report = offload(env, allow_unencrypted=True)
    assert outcomes(report) == {name: "offloaded"} and report["target"]["encrypted"] is False


def test_the_state_roots_own_volume_and_a_missing_folder_are_refused(archives, env, volumes):
    install(archives.build(PLAIN), env)
    volumes.reset()                  # the target folder is now on the state root's volume
    report = offload(env)
    assert any("state root's own volume" in r for r in report["refused"])
    report = rof.offload(env.root, env.base / "missing")
    assert report["refused"] == [f"{env.base / 'missing'} is not an existing folder"]


def test_volume_classification_from_statfs(tmp_path):
    """The real reads' classification: exFAT and FAT keep no owners, `noowners`
    neither, a volume without MNT_LOCAL is a network one."""
    def statfs(fstype: bytes, flags: int) -> rvol._Statfs:
        fs = rvol._Statfs()
        fs.f_fstypename = fstype
        fs.f_mntonname = os.fsencode(str(tmp_path))
        fs.f_flags = flags
        fs.f_bavail, fs.f_bsize = 10, 4096
        return fs
    local = rvol.MNT_LOCAL
    assert rvol._volume(statfs(b"apfs", local), encryption=False).owners is True
    assert rvol._volume(statfs(b"exfat", local), encryption=False).owners is False
    assert rvol._volume(statfs(b"msdos", local), encryption=False).owners is False
    assert rvol._volume(statfs(b"apfs", local | rvol.MNT_IGNORE_OWNERSHIP), encryption=False).owners is False
    assert rvol._volume(statfs(b"smbfs", 0), encryption=False).local is False
    assert rvol._volume(statfs(b"apfs", local | rvol.MNT_RDONLY), encryption=False).readonly is True
    assert rvol._volume(statfs(b"apfs", local), encryption=False).free == 40960
    exfat = rvol._volume(statfs(b"exfat", local), encryption=False)
    assert any("does not keep owners" in r for r in rvol.refusals(exfat, allow_unencrypted=True))


def test_the_real_volume_reads_identify_the_temporary_folders_volume(tmp_path):
    """Read-only: statfs, getattrlist and getfsstat on this host (no stand-in)."""
    if rvol._statfs is None or rvol._getattrlist is None:
        pytest.skip("not macOS")
    volume = rvol.volume_of(tmp_path)
    assert volume.uuid and volume.local and volume.mount.is_dir()
    assert any(v.uuid == volume.uuid and v.mount == volume.mount for v in rvol.volumes())
    assert rvol.mounted_with(volume.uuid.lower()).mount == volume.mount
    assert ctypes.sizeof(rvol._Statfs) == 2168


# --- recall --------------------------------------------------------------------------------------------

def test_recall_that_does_not_read_back_keeps_the_stub_and_the_external_copy(archives, env):
    """Mutant 6 (recall without verifying) dies here."""
    name = install(archives.build(RICH), env)
    offload(env)
    found = rof.plan(env.root, name)
    store = sorted(found.stores)[0]
    (env.external(name) / "files" / store).write_bytes(b"rotted on the drive")
    report = rof.recall(env.root, name, drop_external=True)
    assert outcomes(report) == {name: "failed"} and "did not read back" in report["results"][0]["error"]
    assert rof.read_stub(env.root, name)["state"] == "offloaded"
    assert env.external(name).exists()
    assert not (env.archive(name) / "files" / store).exists()
    assert not [n for n in os.listdir(env.archive(name) / "files") if n.startswith(".recall.")]


def test_recall_of_a_damaged_bundle_fails(archives, env):
    name = install(archives.build(RICH), env)
    offload(env)
    bundle = env.external(name) / "commits.bundle"
    data = bytearray(bundle.read_bytes())
    data[-1] ^= 0xFF
    bundle.write_bytes(bytes(data))
    report = rof.recall(env.root, name)
    assert outcomes(report) == {name: "failed"} and "commits.bundle did not read back" in report["results"][0]["error"]
    assert rof.read_stub(env.root, name) is not None


def test_recall_all_on_a_folder_and_keep_or_drop_the_external_copy(archives, env):
    rich, plain = archives.build(RICH), archives.build(PLAIN)
    install(rich, env, "r")
    install(plain, env, "p")
    install(plain, env, "zz-kept-here")
    assert outcomes(offload(env, limit=2)) == {"r": "offloaded", "p": "offloaded"}
    report = rof.recall(env.root, all_on=env.target)
    assert outcomes(report) == {"r": "recalled", "p": "recalled"}
    assert env.external("r").exists() and env.external("p").exists()
    assert outcomes(offload(env)) == {"r": "offloaded", "p": "offloaded", "zz-kept-here": "offloaded"}
    report = rof.recall(env.root, "r", drop_external=True)
    assert report["results"][0]["external"] == "dropped" and not env.external("r").exists()
    assert outcomes(rof.recall(env.root, "r")) == {"r": "skipped"}


def test_a_recall_needs_the_volume_and_says_which(archives, env, volumes):
    name = install(archives.build(PLAIN), env)
    offload(env)
    volumes.unmount(EXT_UUID)
    report = rof.recall(env.root, name)
    assert outcomes(report) == {name: "failed"}
    assert f"volume 'Backup ☃' ({EXT_UUID}), which is not mounted" in report["results"][0]["error"]


# --- restore, check and list follow the stub -----------------------------------------------------------

def test_restore_from_an_offloaded_archive_matches_the_internal_one(archives, volumes, tmp_path):
    """Differential (invariant 4): the same archive restored kept internal and
    restored offloaded (volume mounted) writes the same trees, byte for byte.
    Mutant 7 (restore reads the internal path) dies here."""
    src = archives.build(RICH)
    kept = fresh(tmp_path, volumes)
    name = install(src, kept)
    verdict_kept = rarch.check_archive(kept.root, name)
    rarch.restore(kept.root, name, to=kept.base / "out")
    moved = fresh(tmp_path, volumes)
    install(src, moved)
    assert outcomes(offload(moved)) == {name: "offloaded"}
    assert not (moved.archive(name) / "files").exists()
    verdict_moved = rarch.check_archive(moved.root, name)
    assert (verdict_moved["ok"], verdict_moved["problems"]) == (verdict_kept["ok"], verdict_kept["problems"])
    assert verdict_moved["offloaded_to"] == str(moved.external(name))
    report = rarch.restore(moved.root, name, to=moved.base / "out")
    assert report["offloaded_to"] == str(moved.external(name))
    trees = sorted(os.listdir(kept.base / "out"))
    assert trees == sorted(os.listdir(moved.base / "out")) and "job" in trees and "worktree" in trees
    for tree in trees:
        assert snapshot(moved.base / "out" / tree) == snapshot(kept.base / "out" / tree), tree


def test_check_verdicts_match_for_damage_inside_and_outside(archives, volumes, tmp_path):
    src = archives.build(RICH)
    kept = fresh(tmp_path, volumes)
    name = install(src, kept)
    found = rof.plan(kept.root, name)
    store = sorted(found.stores)[-1]
    (kept.archive(name) / "files" / store).write_bytes(b"damaged")
    moved = fresh(tmp_path, volumes)
    install(src, moved)
    offload(moved)
    (moved.external(name) / "files" / store).write_bytes(b"damaged")
    a, b = rarch.check_archive(kept.root, name), rarch.check_archive(moved.root, name)
    assert not a["ok"] and (a["ok"], a["problems"]) == (b["ok"], b["problems"])
    with pytest.raises(rarch.RestoreError, match="failed verification"):
        rarch.restore(moved.root, name, to=moved.base / "out")


def test_an_unmounted_or_remounted_volume(archives, env, volumes, tmp_path):
    name = install(archives.build(RICH), env)
    offload(env)
    volumes.unmount(EXT_UUID)
    with pytest.raises(rof.VolumeMissing, match=f"volume 'Backup ☃' \\({EXT_UUID}\\), which is not mounted"):
        rarch.check_archive(env.root, name)
    with pytest.raises(rof.VolumeMissing):
        rarch.restore(env.root, name, to=env.base / "out")
    (listed,) = rarch.list_archives(env.root)
    assert listed["offload"]["describe"] == f"offloaded to Backup ☃ ({EXT_UUID})"
    # The same volume mounted somewhere else is found by its UUID.
    elsewhere = tmp_path / "remounted"
    os.rename(env.volume.mount, elsewhere)
    volumes.mount(elsewhere)
    assert rarch.check_archive(env.root, name)["ok"]
    rarch.restore(env.root, name, to=env.base / "out")
    assert (env.base / "out" / "job" / "a").read_bytes() == b"alpha\n"


def test_a_link_in_the_external_path_is_not_followed(archives, env, tmp_path):
    name = install(archives.build(PLAIN), env)
    offload(env)
    elsewhere = tmp_path / "elsewhere"
    os.rename(env.target / rof.FOLDER, elsewhere)
    os.symlink(elsewhere, env.target / rof.FOLDER)
    with pytest.raises(OSError):
        rarch.check_archive(env.root, name)


def test_cli_offload_list_check_recall(archives, env, monkeypatch, capsys):
    from subfleet import cli
    name = install(archives.build(RICH), env)
    monkeypatch.setenv("SUBFLEET_HOME", str(env.root))
    assert cli.main(["retention", "offload", "--to", str(env.target), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [b["archive"] for b in report["batch"]] == [name] and report["dry_run"]
    assert cli.main(["retention", "offload", "--to", str(env.target)]) == 0
    text = capsys.readouterr().out
    assert f"target {os.path.realpath(env.target)} on volume Backup ☃ ({EXT_UUID})" in text
    assert f"{name}: offloaded" in text
    assert cli.main(["retention", "archives"]) == 0
    assert f"offloaded to Backup ☃ ({EXT_UUID})" in capsys.readouterr().out
    assert cli.main(["retention", "archives", "--json"]) == 0
    (listed,) = json.loads(capsys.readouterr().out)["archives"]
    assert listed["offload"]["state"] == "offloaded" and listed["offload"]["volume_uuid"] == EXT_UUID
    assert cli.main(["retention", "restore", name, "--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main(["retention", "recall", name, "--json"]) == 0
    assert outcomes(json.loads(capsys.readouterr().out)) == {name: "recalled"}
    assert cli.main(["retention", "recall"]) == 2
    capsys.readouterr()
    monkeypatch.setattr(rvol, "volumes", Volumes())     # the target is now the state root's own volume
    assert cli.main(["retention", "offload", "--to", str(env.target)]) == 7
    assert "refused:" in capsys.readouterr().out


# --- invariant 2: what the commands write ----------------------------------------------------------------

def test_offload_and_recall_write_only_their_own_paths(archives, env):
    """Everything outside `<state>/archive/<name>/`, its lock and the log, and
    `DIR/subfleet-archive/<name>/` is byte for byte as it was, other archives
    and other files on the volume included."""
    src = archives.build(RICH)
    name = install(src, env, "moved")
    install(archives.build(PLAIN), env, "other")
    (env.root / "retention").mkdir()
    (env.root / "retention" / "other").mkdir()            # not settled: left alone
    (env.target / "someone-elses.txt").write_text("not ours\n")
    (env.volume.mount / "top.txt").write_text("not ours either\n")
    sentinel = env.base / "outside.txt"
    sentinel.write_text("outside\n")

    def outside() -> dict:
        everything = snapshot(env.base)
        own = (f"state/archive/{name}", f"vol-a/backups/{rof.FOLDER}")
        skipped = {"state/archive", f"vol-a/backups"}       # parents whose mtime the own entries move
        return {rel: v for rel, v in everything.items()
                if not rel.startswith(own) and rel not in skipped and rel != f"state/archive/{rof.LOG}"}
    before = outside()
    report = offload(env)
    assert outcomes(report) == {name: "offloaded"} and "retention folder" in skips(report)["other"]
    assert outside() == before
    assert sorted(os.listdir(env.target / rof.FOLDER)) == [name]
    rof.recall(env.root, name, drop_external=True)
    assert outside() == before
    assert sorted(n for n in os.listdir(env.root / "archive") if n.startswith(".")) == [rof.LOG]
