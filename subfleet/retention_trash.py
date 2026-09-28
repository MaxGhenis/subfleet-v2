"""Reversible retirement and resumable reclamation of retention directories.

The journal precedes every rename. A surviving job row always owns its trash;
recovery restores that job before checking its pins and preservation proofs.
Only trash without a job row may be unlinked.
"""
from __future__ import annotations

import json
import os
import subprocess
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
        device = target.stat().st_dev
        for source, _ in paths:
            if source.exists() or source.is_symlink():
                if source.lstat().st_dev != device:
                    raise ValueError("retention trash must be on the same filesystem")
        with (target / "manifest.tmp").open("x") as stream:
            json.dump({"job_id": identity, "worktree": str(worktree) if worktree else None,
                       "common": common, "bytes": size}, stream)
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
            if destination is None or destination.exists() or destination.is_symlink():
                raise ValueError("cannot restore retention trash over existing job files")
            source.rename(destination)
    manifest.unlink(missing_ok=True)
    (target / "manifest.tmp").unlink(missing_ok=True)
    target.rmdir()
    return data


def _entries(path: Path):
    """Stream entries without following links; the caller closes on interruption."""
    if path.is_symlink() or not path.is_dir():
        yield path, False
        return
    with os.scandir(path) as scan:
        for entry in scan:
            if entry.is_dir(follow_symlinks=False):
                yield from _entries(Path(entry.path))
            else:
                yield Path(entry.path), False
    yield path, True


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
            if data.get("common"):
                common = Path(data["common"])
                if common.exists():
                    git(common, "--git-dir=" + str(common), "worktree", "prune", "--expire=now")
            for name in ("worktree", "job"):
                path = target / name
                if path.exists() or path.is_symlink():
                    with closing(_entries(path)) as entries:
                        for entry, directory in entries:
                            checkpoint()
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
