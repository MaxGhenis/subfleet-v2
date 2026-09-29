"""Property tests for the job-directory archive (C-8.4; design revision 2, invariants J1-J4, R1).

Trees are generated: nested directories (some 0555), files with arbitrary bytes and
modes, symlinks inside, outside, absolute and dangling, hard links, FIFOs, and names
with spaces, newlines and non-ASCII characters.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from subfleet import retention_archive as archive

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                                         HealthCheck.too_slow])
NAME = st.text(st.characters(min_codepoint=32, max_codepoint=0x2FF, blacklist_characters="/\x7f") | st.sampled_from(["\n", "é", " "]),
               min_size=1, max_size=8).filter(lambda n: n not in (".", ".."))
FILE_MODES = st.sampled_from([0o644, 0o600, 0o755, 0o444, 0o400])
DIR_MODES = st.sampled_from([0o755, 0o700, 0o555])


def trees(depth=3):
    leaf = st.one_of(
        st.tuples(st.just("file"), st.binary(max_size=3000), FILE_MODES),
        st.tuples(st.just("link"), st.sampled_from(["inside", "outside", "absolute", "dangling"])),
        st.tuples(st.just("fifo")),
        st.tuples(st.just("hard")),
    )
    if depth == 0:
        return st.dictionaries(NAME, leaf, max_size=4)
    return st.dictionaries(NAME, st.one_of(leaf, st.tuples(st.just("dir"), st.deferred(lambda: trees(depth - 1)),
                                                           DIR_MODES)), max_size=5)


def build(root: Path, spec: dict, outside: Path) -> None:
    files: list[Path] = []
    later_modes: list[tuple[Path, int]] = []

    def make(directory: Path, node: dict) -> None:
        seen = set()
        for name, item in node.items():
            if name.casefold() in seen:           # APFS is case-insensitive: one of them only
                continue
            seen.add(name.casefold())
            path = directory / name
            if item[0] == "file":
                path.write_bytes(item[1])
                os.chmod(path, item[2])
                files.append(path)
            elif item[0] == "dir":
                path.mkdir()
                make(path, item[1])
                later_modes.append((path, item[2]))
            elif item[0] == "link":
                target = {"inside": ".", "outside": str(outside), "absolute": "/usr/bin/env",
                          "dangling": "missing-target"}[item[1]]
                path.symlink_to(target)
            elif item[0] == "fifo":
                os.mkfifo(path)
            elif item[0] == "hard" and files:
                os.link(files[0], path)

    make(root, spec)
    for path, mode in reversed(later_modes):
        os.chmod(path, mode)


def snapshot(root: Path) -> dict:
    """Every entry: type, permission bits, bytes or link target, mtime, and hard-link groups."""
    result, inodes = {}, {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            path = Path(directory) / name
            st_ = path.lstat()
            rel = str(path.relative_to(root))
            if stat.S_ISLNK(st_.st_mode):
                result[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(st_.st_mode):
                result[rel] = ("dir", stat.S_IMODE(st_.st_mode), st_.st_mtime_ns)
            elif stat.S_ISREG(st_.st_mode):
                os.chmod(path, stat.S_IMODE(st_.st_mode) | stat.S_IRUSR) if not st_.st_mode & stat.S_IRUSR else None
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[rel] = ("file", stat.S_IMODE(st_.st_mode), digest, st_.st_mtime_ns)
                inodes.setdefault(st_.st_ino, []).append(rel)
            else:
                result[rel] = ("fifo", stat.S_IMODE(st_.st_mode))
    groups = sorted(sorted(group) for group in inodes.values() if len(group) > 1)
    return {"entries": result, "hardlinks": groups}


def rows(job_id="job", note="x"):
    content = {"jobs": [{"job_id": job_id, "request_id": job_id, "note": note}], "attempts": []}
    return {"job_id": job_id, "rows": content, "sha256": hashlib.sha256(archive.canonical(content)).hexdigest()}


def make_all_writable(root: Path) -> None:
    for directory, dirnames, _ in os.walk(root):
        for name in dirnames:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, 0o755)


@pytest.fixture
def space(tmp_path):
    yield tmp_path
    make_all_writable(tmp_path)


def fresh(space: Path) -> tuple[Path, Path, Path]:
    base = Path(tempfile.mkdtemp(dir=space))
    job_dir, archive_root, outside = base / "jobs" / "job", base / "archive", base / "outside"
    job_dir.parent.mkdir()
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"must survive")
    job_dir.mkdir()
    return job_dir, archive_root, outside


@SETTINGS
@given(spec=trees())
def test_j4_restore_reproduces_every_archived_entry(space, spec):
    """J4: archive, verify and restore give back every entry: type, bits, bytes, link, mtime, hard links."""
    job_dir, archive_root, outside = fresh(space)
    build(job_dir, spec, outside)
    before = snapshot(job_dir)
    final = archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    restored = job_dir.parent / "restored"
    archive.restore(final, restored)
    assert snapshot(restored) == before
    make_all_writable(restored)


@SETTINGS
@given(spec=trees(), mutations=st.lists(st.tuples(st.sampled_from(["add", "modify", "chmod", "touch", "replace"]),
                                                  st.integers(0, 1000)), max_size=4))
def test_j3_deletion_removes_only_unchanged_archived_entries(space, spec, mutations):
    """J2, J3: after archiving, anything added or changed survives deletion; everything
    unchanged goes; nothing outside the directory is touched."""
    job_dir, archive_root, outside = fresh(space)
    build(job_dir, spec, outside)
    final = archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    manifest = archive.load(final)
    make_all_writable(job_dir)
    entries = sorted(p for p in job_dir.rglob("*") if not p.is_symlink())
    regular = [p for p in entries if p.is_file() and not p.is_symlink() and os.lstat(p).st_nlink == 1]
    dirs = [job_dir] + [p for p in entries if p.is_dir()]
    changed: dict[Path, bytes | None] = {}
    for kind, pick in mutations:
        if kind == "add":
            where = dirs[pick % len(dirs)] / f"late-{pick}"
            if not where.exists():
                where.write_bytes(b"late work")
                changed[where] = b"late work"
        elif regular:
            target = regular[pick % len(regular)]
            os.chmod(target, stat.S_IMODE(target.stat().st_mode) | stat.S_IWUSR | stat.S_IRUSR)
            if kind == "modify":
                target.write_bytes(target.read_bytes() + b"+")
            elif kind == "chmod":
                os.chmod(target, stat.S_IMODE(target.stat().st_mode) ^ 0o010)
            elif kind == "touch":
                os.utime(target, ns=(1, target.stat().st_mtime_ns + 1_000_000_000))
            elif kind == "replace":
                target.unlink()
                target.write_bytes(b"replacement")
            changed[target] = target.read_bytes()
    kept = archive.delete_archived(job_dir, manifest)
    for path, data in changed.items():
        assert path.read_bytes() == data, path
        assert str(path.relative_to(job_dir)) in kept
    allowed = set()
    for path in changed:
        relative = path.relative_to(job_dir)
        allowed.update(str(parent) for parent in relative.parents if str(parent) != ".")
        allowed.add(str(relative))
    survivors = {str(p.relative_to(job_dir)) for p in job_dir.rglob("*")} if job_dir.exists() else set()
    assert survivors <= allowed, survivors - allowed
    assert (outside / "sentinel").read_bytes() == b"must survive"
    if not changed:
        assert kept == [] and not job_dir.exists()


@SETTINGS
@given(spec=trees(depth=2), offset=st.integers(0, 10 ** 6), flip=st.integers(1, 255), truncate=st.booleans())
def test_j1_a_damaged_archive_never_verifies_into_a_wrong_restore(space, spec, offset, flip, truncate):
    """J1: after any one-byte flip or truncation of the tar, verification either fails, or the
    restore still reproduces the original exactly (the damage fell in padding)."""
    job_dir, archive_root, outside = fresh(space)
    build(job_dir, spec, outside)
    before = snapshot(job_dir)
    final = archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    tree = archive.load(final)["tree"]
    assume("tar" in tree)
    tar = final / tree["tar"]
    data = bytearray(tar.read_bytes())
    position = offset % len(data)
    if truncate:
        del data[position:]
    else:
        data[position] ^= flip
    os.chmod(tar, 0o600)
    tar.write_bytes(bytes(data))
    try:
        archive.verify(final)
    except archive.ArchiveCorrupt:
        return
    restored = job_dir.parent / "restored"
    archive.restore(final, restored)
    assert snapshot(restored) == before
    make_all_writable(restored)


def test_r1_rows_travel_with_their_digest(space):
    """R1: rows.json is verified against the digest the manifest recorded."""
    job_dir, archive_root, outside = fresh(space)
    final = archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    archive.verify(final)
    stored = json.loads((final / "rows.json").read_bytes())
    stored["rows"]["jobs"][0]["note"] = "edited"
    os.chmod(final / "rows.json", 0o600)
    (final / "rows.json").write_bytes(archive.canonical(stored))
    with pytest.raises(archive.ArchiveCorrupt):
        archive.verify(final)


def test_an_archive_is_never_left_half_written(space, monkeypatch):
    """J1: a failure while writing leaves no archive at the final name and no partial one."""
    job_dir, archive_root, outside = fresh(space)
    (job_dir / "a").write_bytes(b"a" * 100)

    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(archive, "verify", fail)
    with pytest.raises(OSError):
        archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    assert os.listdir(archive_root) == []
    assert (job_dir / "a").read_bytes() == b"a" * 100


def test_a_file_that_changes_while_it_is_read_defers_the_archive(space, monkeypatch):
    """A writer active during archiving makes the attempt fail as not quiet, never a bad archive."""
    job_dir, archive_root, outside = fresh(space)
    (job_dir / "log").write_bytes(b"x" * 100)
    original = archive._Reader.readinto

    def grow(self, buffer):
        count = original(self, buffer)
        with open(job_dir / "log", "ab") as stream:
            stream.write(b"more")
        return count
    monkeypatch.setattr(archive._Reader, "readinto", grow)
    with pytest.raises(archive.NotQuiet):
        archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    assert os.listdir(archive_root) == []


def test_no_archive_without_room(space):
    """Review Opus 7: an archive that would leave less than the floor free is refused."""
    job_dir, archive_root, outside = fresh(space)
    with pytest.raises(archive.Unarchivable):
        archive.archive(job_dir, archive_root, "job", rows(), free_floor=10 ** 18)


def test_a_symlinked_or_missing_job_directory(space):
    """The old code unlinked a symlinked job directory; the archive records its target instead."""
    job_dir, archive_root, outside = fresh(space)
    job_dir.rmdir()
    job_dir.symlink_to(outside)
    final = archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    assert archive.delete_archived(job_dir, archive.load(final)) == []
    assert not os.path.lexists(job_dir) and (outside / "sentinel").exists()
    missing = archive.archive(job_dir, archive_root, "other", rows("other"), free_floor=0)
    assert archive.load(missing)["tree"] is None


def test_deletion_is_idempotent_after_an_interruption(space, monkeypatch):
    """Opus 8: a deletion stopped halfway resumes without turning emptied directories or
    remaining hard links into conflicts."""
    job_dir, archive_root, outside = fresh(space)
    (job_dir / "d").mkdir()
    for index in range(5):
        (job_dir / "d" / f"f{index}").write_bytes(b"x")
    os.link(job_dir / "d" / "f0", job_dir / "h1")
    os.link(job_dir / "d" / "f0", job_dir / "h2")
    final = archive.archive(job_dir, archive_root, "job", rows(), free_floor=0)
    manifest = archive.load(final)
    calls = []
    original = os.unlink

    def stop_after_two(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 3:
            raise KeyboardInterrupt("shutdown")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", stop_after_two)
    with pytest.raises(KeyboardInterrupt):
        archive.delete_archived(job_dir, manifest)
    monkeypatch.setattr(os, "unlink", original)
    kept = archive.delete_archived(job_dir, manifest)
    assert not job_dir.exists(), kept
