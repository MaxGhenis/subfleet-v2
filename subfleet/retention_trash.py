"""Reversible retirement and resumable reclamation of retention directories.

The journal precedes every rename. A surviving job row always owns its trash;
recovery restores that job before checking its pins and preservation proofs.
Only trash without a job row may be unlinked.
"""
from __future__ import annotations

import json
import errno
import os
import subprocess
import stat
import uuid
from contextlib import closing
from pathlib import Path


def location(root: Path, identity: str) -> Path:
    if Path(identity).name != identity or identity in (".", ".."):
        raise ValueError("invalid retention trash job name")
    trash = root / "trash"
    if trash.is_symlink():
        raise ValueError("retention trash root is a symlink")
    target = trash / identity
    if target.is_symlink():
        raise ValueError("retention trash job is a symlink")
    return target


def prepare(root: Path, identity: str, worktree: Path | None, common: str | None,
            size: int) -> Path:
    target = location(root, identity)
    target.mkdir(parents=True, exist_ok=False)
    # Validate both devices before moving either directory. EXDEV cannot leave
    # one directory retired while the other remains at its original path.
    paths = [(root / "jobs" / identity, "job")]
    if worktree is not None:
        paths.insert(0, (worktree, "worktree"))
    try:
        admin = None
        if worktree is not None and worktree.exists():
            pointer = (worktree / ".git").read_text().strip()
            if not pointer.startswith("gitdir: "):
                raise ValueError("owned worktree has no linked Git admin directory")
            admin_path = Path(pointer[8:])
            if not admin_path.is_absolute():
                admin_path = worktree / admin_path
            admin_path = admin_path.resolve()
            if common is None or admin_path.parent != Path(common).resolve() / "worktrees":
                raise ValueError("Git admin directory is not a direct shared worktree registration")
            if Path((admin_path / "gitdir").read_text().strip()).resolve() != worktree / ".git":
                raise ValueError("Git admin directory points at a different worktree")
            admin = str(admin_path)
        device = target.stat().st_dev
        for source, _ in paths:
            if source.exists() or source.is_symlink():
                if source.lstat().st_dev != device:
                    raise ValueError("retention trash must be on the same filesystem")
        with (target / "manifest.tmp").open("x") as stream:
            json.dump({"job_id": identity, "worktree": str(worktree) if worktree else None,
                       "common": common, "admin": admin, "bytes": size}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        (target / "manifest.tmp").replace(target / "manifest.json")
        descriptor = os.open(target, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        (target / "manifest.tmp").unlink(missing_ok=True)
        (target / "manifest.json").unlink(missing_ok=True)
        target.rmdir()
        raise
    return target


def stage(root: Path, identity: str, worktree: Path | None, target: Path) -> None:
    # No cancellation/deadline checks from here through row commit or restore.
    for source, name in ((worktree, "worktree"), (root / "jobs" / identity, "job")):
        if source is not None and (source.exists() or source.is_symlink()):
            source.rename(target / name)


def restore(root: Path, identity: str, worktree: Path | None) -> dict | None:
    target = location(root, identity)
    if not target.exists():
        return None
    manifest = target / "manifest.json"
    if not manifest.exists() and any((target / name).exists() or (target / name).is_symlink()
                                     for name in ("worktree", "job")):
        raise ValueError("retention trash has moved files without a valid journal")
    data = json.loads(manifest.read_text()) if manifest.exists() else {}
    if data and (data.get("job_id") != identity or data.get("worktree") != (
            str(worktree) if worktree else None)):
        raise ValueError("retention journal does not match job paths")
    for destination, name in ((worktree, "worktree"), (root / "jobs" / identity, "job")):
        source = target / name
        if source.exists() or source.is_symlink():
            if destination is None:
                raise ValueError("retention trash has no restoration destination")
            if destination.exists() or destination.is_symlink():
                # Preserve concurrent output outside automatically cleaned trash.
                # A unique sibling makes the move restartable: no overwrites and
                # the original directory can always be restored on a later pass.
                conflicts = root / "retention-conflicts" / identity
                if (root / "retention-conflicts").is_symlink() or conflicts.is_symlink():
                    raise ValueError("retention conflict directory is a symlink")
                conflicts.mkdir(parents=True, exist_ok=True)
                preserved = conflicts / (name + "-" + uuid.uuid4().hex)
                destination.rename(preserved)
                data.setdefault("conflicts", []).append(str(preserved))
            source.rename(destination)
    manifest.unlink(missing_ok=True)
    (target / "manifest.tmp").unlink(missing_ok=True)
    target.rmdir()
    return data


def _entries(path: Path):
    """Iterative traversal with one open scandir, never following symlinks.

    None is a checkpoint between directories, including empty/deep trees.
    Owner permissions are repaired only on directories already in owned trash.
    """
    pending = [(path, False)]
    while pending:
        yield None, False
        current, visited = pending.pop()
        if visited:
            yield current, True
            continue
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            yield current, False
            continue
        os.chmod(current, stat.S_IMODE(metadata.st_mode) | 0o700, follow_symlinks=False)
        pending.append((current, True))
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), False))
                    yield None, False
                else:
                    yield Path(entry.path), False


def _remove_registration(data, checkpoint, progress):
    """Remove only a manifest-identified registration; old manifests leave it."""
    if not data.get("admin"):
        return
    retired = Path(data["trash"]) / "admin"
    if retired.exists():
        return  # our registration was already detached; never touch a replacement
    admin = Path(data["admin"])
    if not admin.exists():
        return
    common = Path(data["common"]).resolve()
    if admin.is_symlink() or admin.parent != common / "worktrees":
        raise ValueError("invalid retired Git admin directory")
    # An interrupted cross-device cleanup may have removed its backlink and
    # stopped before rmdir. An empty directory holds no registration evidence.
    with os.scandir(admin) as entries:
        empty = next(entries, None) is None
    if empty:
        admin.rmdir()
        return
    pointer = admin / "gitdir"
    expected = Path(data["worktree"]) / ".git"
    if not pointer.is_file() or Path(pointer.read_text().strip()).resolve() != expected:
        raise ValueError("retired Git admin directory now points at another worktree")
    # Atomically detach it before deadline-checked deletion. A crash cannot
    # leave a partially deleted registration that fails its pointer check.
    retired = Path(data["trash"]) / "admin"
    if retired.exists():
        raise ValueError("retired admin directory already exists")
    try:
        admin.rename(retired)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # The repository may be on a different filesystem from its linked
        # worktree. Keep the verified backlink until the last, uninterruptible
        # unlink/rmdir pair so interrupted targeted cleanup remains provable.
        with closing(_entries(admin)) as entries:
            for entry, directory in entries:
                checkpoint()
                if entry is None or entry in (admin, pointer):
                    continue
                entry.rmdir() if directory else entry.unlink()
                progress["made_progress"] = True
        pointer.unlink()
        admin.rmdir()
    progress["made_progress"] = True


def clean(store, root: Path, *, checkpoint, git, progress) -> None:
    trash = root / "trash"
    if not trash.exists():
        return
    if trash.is_symlink():
        raise ValueError("retention trash root is a symlink")
    for target in sorted(trash.iterdir()):
        checkpoint()
        identity = target.name
        try:
            location(root, identity)
            if store.get_job(identity) is not None:
                continue
            manifest = target / "manifest.json"
            if not manifest.is_file() or manifest.is_symlink():
                # Empty journal directories can survive a crash before prepare
                # writes its manifest or after cleanup removes it.
                target.rmdir()
                continue
            data = json.loads(manifest.read_text())
            if data.get("job_id") != identity:
                raise ValueError("invalid retention trash journal")
            _remove_registration({**data, "trash": str(target)}, checkpoint, progress)
            for name in ("admin", "worktree", "job"):
                path = target / name
                if path.exists() or path.is_symlink():
                    with closing(_entries(path)) as entries:
                        for entry, directory in entries:
                            checkpoint()
                            if entry is None:
                                continue
                            try:
                                entry.rmdir() if directory else entry.unlink()
                                progress["made_progress"] = True
                            except FileNotFoundError:
                                pass
            checkpoint()
            manifest.unlink()
            target.rmdir()
            progress["made_progress"] = True
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = {"job_id": identity, "error": str(exc)}
            progress["errors"].append(error)
            store.add_event("retention.trash_error", job_id=identity, data=error)
