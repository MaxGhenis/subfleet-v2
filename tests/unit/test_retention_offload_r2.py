"""Regression cases from archive offload review round 2 (C-8.8)."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from pathlib import Path

import pytest

from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_offload as rof
from tests.unit.test_retention_offload import (
    EXT_UUID,
    Crash,
    Env,
    PLAIN,
    RICH,
    archives,
    crash_at,
    env,
    install,
    offload,
    outcomes,
    quick_flushes,
    sha,
    skips,
    tree_state,
    volumes,
)

OTHER_UUID = "0D9C0A44-6E21-4C44-8F5B-4B1C2B0B6E11"
_STAT_EXTRA = ("st_atime", "st_mtime", "st_ctime", "st_atime_ns", "st_mtime_ns", "st_ctime_ns", "st_blksize",
               "st_blocks", "st_rdev", "st_flags", "st_gen", "st_birthtime", "st_birthtime_ns")


def _outside(env: Env, name: str) -> Path:
    stub = rof.read_stub(env.root, name)
    assert stub is not None
    return env.volume.mount / stub["path"]


def _interrupted(env: Env, name: str, monkeypatch, step: str, occurrence: int = 1) -> Path:
    monkeypatch.setattr(rof, "HOOK", crash_at(step, occurrence))
    with pytest.raises(Crash):
        offload(env)
    monkeypatch.setattr(rof, "HOOK", None)
    return _outside(env, name)


def _path_of(fd: int) -> bytes:
    return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0")


def _identity(path: Path) -> tuple[int, int]:
    found = os.lstat(path)
    return found.st_dev, found.st_ino


def remount(monkeypatch, top: Path, shift: int = 1 << 20) -> None:
    """The volume holding `top` comes back with another device number, as
    macOS gives a disk whose disk number changed: from now on every stat of
    an entry that was under `top` reports another `st_dev`, for every caller
    alike, with its inode and birth time unchanged."""
    moved = {_identity(top)}
    for folder, dirs, names in os.walk(top):
        moved.update(_identity(Path(folder) / leaf) for leaf in dirs + names)
    real = {name: getattr(os, name) for name in ("stat", "lstat", "fstat")}

    def shifted(found: os.stat_result) -> os.stat_result:
        if (found.st_dev, found.st_ino) not in moved:
            return found
        fields = list(found[:10])
        fields[2] = found.st_dev + shift
        return os.stat_result(fields, {n: getattr(found, n) for n in _STAT_EXTRA if hasattr(found, n)})

    for name, function in real.items():
        monkeypatch.setattr(os, name, lambda *a, _f=function, **k: shifted(_f(*a, **k)))


# --- P0: a resumed offload keeps the good external bundle ---------------------------------------------

def test_copying_resume_must_not_replace_good_external_bundle(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    outside = _interrupted(env, name, monkeypatch, "copied")
    inside_bundle, outside_bundle = env.archive(name) / found.bundle, outside / found.bundle
    good = sha(inside_bundle)
    assert good is not None and sha(outside_bundle) == good
    data = bytearray(inside_bundle.read_bytes())
    data[-1] ^= 0xFF                       # size and heads unchanged
    inside_bundle.write_bytes(bytes(data))
    folder_fd = os.open(env.archive(name), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert rof._heads_ok(folder_fd, found.bundle, found.bundle_heads)
    finally:
        os.close(folder_fd)
    assert inside_bundle.stat().st_size == found.bundle_bytes and sha(inside_bundle) != good

    report = offload(env)

    assert good in (sha(inside_bundle), sha(outside_bundle)), "no location keeps the good bundle"
    assert sha(outside_bundle) == good
    assert rof.read_stub(env.root, name)["bundle_sha256"] == good
    assert outcomes(report) == {name: "offloaded"}
    assert report["results"][0]["left_internal"] == [found.bundle]
    assert rarch.check_archive(env.root, name)["ok"]


def test_the_copying_stub_records_the_bundle_checksum_before_any_copy(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    good = sha(env.archive(name) / found.bundle)
    seen = {}

    def look(step: str) -> None:
        if step == "stub-copying":
            seen["stub"] = rof.read_stub(env.root, name)
            seen["external"] = (env.target / rof.FOLDER).exists()
    monkeypatch.setattr(rof, "HOOK", look)
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert seen["stub"]["state"] == "copying" and seen["stub"]["bundle_sha256"] == good
    assert seen["external"] is False


def test_a_resumed_copy_from_a_damaged_internal_bundle_stops_naming_it(archives, env, monkeypatch):
    """Crash before the bundle is copied, then the internal bundle changes:
    no good copy exists, and the run stops naming the bundle."""
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    outside = _interrupted(env, name, monkeypatch, "copy-file")
    inside_bundle = env.archive(name) / found.bundle
    data = bytearray(inside_bundle.read_bytes())
    data[-1] ^= 0xFF
    inside_bundle.write_bytes(bytes(data))
    internal_before = tree_state(env.archive(name))

    report = offload(env)

    assert outcomes(report) == {name: "failed"}
    assert found.bundle in report["results"][0]["error"]
    assert not (outside / found.bundle).exists()
    assert tree_state(env.archive(name)) == internal_before
    assert rof.read_stub(env.root, name)["state"] == "copying"


# --- P2: a copying archive whose target is gone --------------------------------------------------------

def test_a_copying_archive_whose_target_is_gone_is_recalled(archives, env, volumes, tmp_path, monkeypatch):
    name = install(archives.build(RICH), env)
    before = tree_state(env.archive(name))
    outside = _interrupted(env, name, monkeypatch, "copy-file")
    partial = tree_state(outside)
    volumes.unmount(EXT_UUID)
    other = tmp_path / "vol-b"
    other.mkdir()
    volumes.mount(other, uuid=OTHER_UUID, name="Other")
    assert "run it with that target" in skips(rof.offload(env.root, other))[name]

    report = rof.recall(env.root, name)

    assert outcomes(report) == {name: "recalled"}, report
    assert rof.read_stub(env.root, name) is None
    assert tree_state(env.archive(name)) == before
    assert tree_state(outside) == partial                      # the unmounted target's copy is untouched
    assert rarch.check_archive(env.root, name)["ok"]
    # Mounted again, a later offload to that target takes over its own partial copy and finishes it.
    volumes.mount(env.volume.mount)
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert rarch.check_archive(env.root, name)["ok"]
    assert outcomes(rof.recall(env.root, name, drop_external=True)) == {name: "recalled"}
    assert not outside.exists()


# --- P2: an error or crash during the first ledger write -----------------------------------------------

def _fail_first_ledger_sync(monkeypatch, error: BaseException) -> dict:
    real = rfs.fullsync
    calls = {"n": 0}

    def failing(fd: int) -> None:
        path = _path_of(fd)
        if b"/.owner." in path and path.endswith(b".tmp") and calls["n"] == 0:
            calls["n"] += 1
            raise error
        real(fd)
    monkeypatch.setattr(rfs, "fullsync", failing)
    return calls


def test_crash_in_first_ledger_write_wedges_offload(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    calls = _fail_first_ledger_sync(monkeypatch, OSError(errno.EIO, "synthetic fullsync failure"))
    report = offload(env)
    assert calls["n"] == 1 and outcomes(report) == {name: "failed"}
    outside = _outside(env, name)
    assert [n for n in os.listdir(outside) if n.startswith(".owner.")] == []
    monkeypatch.setattr(rfs, "fullsync", os.fsync)

    assert outcomes(offload(env)) == {name: "offloaded"}
    assert rarch.check_archive(env.root, name)["ok"]
    assert outcomes(rof.recall(env.root, name, drop_external=True)) == {name: "recalled"}


def test_a_crash_in_the_first_ledger_write_leaves_a_temporary_the_rerun_clears(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    _fail_first_ledger_sync(monkeypatch, Crash("killed during the first ledger write"))
    with pytest.raises(Crash):
        offload(env)
    outside = _outside(env, name)
    left = os.listdir(outside)
    assert len(left) == 1 and left[0].startswith(".owner.") and left[0].endswith(".tmp"), left
    monkeypatch.setattr(rfs, "fullsync", os.fsync)
    (env.root / "archive" / f".lock.{name}").touch()

    assert outcomes(offload(env)) == {name: "offloaded"}
    assert sorted(os.listdir(outside)) == sorted([rof.OWNER, "files", rof.plan(env.root, name).bundle])
    assert outcomes(rof.recall(env.root, name, drop_external=True)) == {name: "recalled"}
    assert not outside.exists()


def test_a_folder_holding_something_besides_ledger_temporaries_is_still_refused(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    _fail_first_ledger_sync(monkeypatch, Crash("killed during the first ledger write"))
    with pytest.raises(Crash):
        offload(env)
    outside = _outside(env, name)
    (outside / "someone-elses").write_bytes(b"not ours")
    monkeypatch.setattr(rfs, "fullsync", os.fsync)
    before = tree_state(outside)

    report = offload(env)

    assert outcomes(report) == {name: "failed"}
    assert "not owned" in report["results"][0]["error"]
    assert tree_state(outside) == before


# --- P2: the program's folders are found by position, not by name --------------------------------------

def test_a_target_below_a_folder_named_subfleet_archive_keeps_its_modes(archives, env):
    name = install(archives.build(PLAIN), env)
    user = env.volume.mount / rof.FOLDER
    inner = user / "inner"
    inner.mkdir(parents=True)
    for folder in (user, inner):
        folder.chmod(0o755)

    report = rof.offload(env.root, inner)

    assert outcomes(report) == {name: "offloaded"}, report
    assert {p.name: oct(stat.S_IMODE(p.stat().st_mode)) for p in (user, inner)} == \
        {rof.FOLDER: "0o755", "inner": "0o755"}
    ours = inner / rof.FOLDER
    assert stat.S_IMODE(ours.stat().st_mode) == 0o700
    assert stat.S_IMODE((ours / rof._source(env.root)).stat().st_mode) == 0o700


# --- P3: every program folder's parent is flushed on resume -------------------------------------------

def test_every_program_folders_parent_is_flushed_on_resume(archives, env, monkeypatch):
    name = install(archives.build(PLAIN), env)
    real = rfs.fullsync
    target = _identity(env.target)

    def crash_on_dir(fd: int) -> None:
        found = os.fstat(fd)
        if (found.st_dev, found.st_ino) == target:
            raise Crash("after mkdir(subfleet-archive), before its parent's flush")
        real(fd)
    monkeypatch.setattr(rfs, "fullsync", crash_on_dir)
    with pytest.raises(Crash):
        offload(env)
    assert (env.target / rof.FOLDER).is_dir()
    flushed: set[tuple[int, int]] = set()

    def record(fd: int) -> None:
        found = os.fstat(fd)
        flushed.add((found.st_dev, found.st_ino))
    monkeypatch.setattr(rfs, "fullsync", record)

    assert outcomes(offload(env)) == {name: "offloaded"}
    folders = [env.target, env.target / rof.FOLDER, env.target / rof.FOLDER / rof._source(env.root)]
    assert [str(p) for p in folders if _identity(p) not in flushed] == [], "DIR or a program folder never flushed"


# --- P1: a remount with another device number --------------------------------------------------------

def test_remount_with_new_device_number(archives, env, monkeypatch, tmp_path):
    name = install(archives.build(RICH), env)
    assert outcomes(offload(env)) == {name: "offloaded"}
    before = tree_state(env.archive(name))
    remount(monkeypatch, env.volume.mount)
    assert os.stat(env.target).st_dev != os.stat(env.root).st_dev

    assert rarch.check_archive(env.root, name)["ok"]
    rarch.restore(env.root, name, to=tmp_path / "out")
    assert (tmp_path / "out" / "job" / "a").read_bytes() == b"alpha\n"
    assert tree_state(env.archive(name)) == before
    report = rof.recall(env.root, name, drop_external=True)
    assert outcomes(report) == {name: "recalled"}, report
    assert report["results"][0]["external"] == "dropped"
    assert rarch.check_archive(env.root, name)["ok"]


@pytest.mark.parametrize("step", ["copied", "verified", "removing"])
def test_a_resumed_offload_after_a_remount_with_new_device_number(archives, env, monkeypatch, step):
    name = install(archives.build(RICH), env)
    _interrupted(env, name, monkeypatch, step)
    remount(monkeypatch, env.volume.mount)
    (env.root / "archive" / f".lock.{name}").touch()

    report = offload(env)

    assert outcomes(report) == {name: "offloaded"}, report
    assert rarch.check_archive(env.root, name)["ok"]


def test_a_state_root_with_a_new_device_number_keeps_its_source(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    other = install(archives.build(PLAIN), env, "second")
    assert outcomes(offload(env, keep_internal=True)) == {name: "verified", other: "verified"}
    source = rof._source(env.root)
    remount(monkeypatch, env.root)
    assert os.stat(env.root / "archive").st_dev != os.stat(env.target).st_dev

    assert rof._source(env.root) == source
    report = rof.offload(env.root, env.target, limit=1)          # resumes, not skipped
    assert outcomes(report) == {name: "offloaded"}, report
    report = rof.recall(env.root, all_on=env.target, drop_external=True)
    assert outcomes(report) == {name: "recalled", other: "recalled"}, report
    assert {r["archive"]: r["external"] for r in report["results"]} == {name: "dropped", other: "dropped"}


# --- P3: no `offloaded_to` for an archive that never left ---------------------------------------------

def test_never_offloaded_archive_through_a_symlinked_root(archives, env, tmp_path):
    name = install(archives.build(PLAIN), env)
    link = env.base / "link-to-state"
    link.symlink_to(env.root, target_is_directory=True)
    roots = [link]
    if str(env.root).startswith("/private/var/"):
        roots.append(Path("/var") / env.root.relative_to("/private/var"))     # /var is a link to /private/var
    for index, root in enumerate(roots):
        checked = rarch.check_archive(root, name)
        assert checked["ok"] and "offloaded_to" not in checked, checked.get("offloaded_to")
        restored = rarch.restore(root, name, to=tmp_path / f"out-{index}")
        assert "offloaded_to" not in restored
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert rarch.check_archive(link, name)["offloaded_to"] == str(env.external(name))


# --- P3: a damaged ownership marker -------------------------------------------------------------------

def test_a_damaged_ownership_marker_fails_naming_the_file(archives, env, monkeypatch, capsys):
    from subfleet import cli
    name = install(archives.build(RICH), env)
    assert outcomes(offload(env)) == {name: "offloaded"}
    outside = _outside(env, name)
    (outside / rof.OWNER).write_text("{not json")
    internal_before, external_before = tree_state(env.archive(name)), tree_state(outside)

    with pytest.raises(rof.Failed, match=r"\.owner\.json"):
        rarch.check_archive(env.root, name)
    report = rof.recall(env.root, name, drop_external=True)
    assert outcomes(report) == {name: "failed"} and rof.OWNER in report["results"][0]["error"]
    monkeypatch.setenv("SUBFLEET_HOME", str(env.root))
    assert cli.main(["retention", "restore", name, "--check", "--json"]) == 1
    assert cli.main(["retention", "recall", name]) == 1
    output = capsys.readouterr()
    assert rof.OWNER in output.out + output.err
    assert tree_state(outside) == external_before
    assert tree_state(env.archive(name)) == internal_before
    assert json.loads(rof.stub_path(env.root, name).read_text())["state"] == "offloaded"
