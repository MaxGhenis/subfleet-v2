"""Reversible retirement and resumable reclamation of retention directories.

The journal precedes every rename. A surviving job row always owns its trash;
recovery restores that job before checking its pins and preservation proofs.
Only trash without a job row may be unlinked.
"""
from __future__ import annotations

import json
import os
import subprocess
import stat
import shutil
import sys
import uuid
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
            if admin_path.is_symlink():
                raise ValueError("Git admin directory is a symlink")
            admin_path = admin_path.resolve()
            if common is None or admin_path.parent != Path(common).resolve() / "worktrees":
                raise ValueError("Git admin directory is not a direct shared worktree registration")
            if Path((admin_path / "gitdir").read_text().strip()).resolve() != worktree / ".git":
                raise ValueError("Git admin directory points at a different worktree")
            metadata = admin_path.stat()
            admin = {"admin": str(admin_path), "admin_inode": metadata.st_ino,
                     "admin_device": metadata.st_dev,
                     "admin_gitdir": (admin_path / "gitdir").read_bytes().hex()}
        device = target.stat().st_dev
        for source, _ in paths:
            if source.exists() or source.is_symlink():
                if source.lstat().st_dev != device:
                    raise ValueError("retention trash must be on the same filesystem")
        _write_manifest(target, {"job_id": identity,
                                 "worktree": str(worktree) if worktree else None,
                                 "common": common, "bytes": size, **(admin or {})})
    except BaseException:
        (target / "manifest.tmp").unlink(missing_ok=True)
        (target / "manifest.json").unlink(missing_ok=True)
        target.rmdir()
        raise
    return target


def stage(root: Path, identity: str, worktree: Path | None, target: Path) -> None:
    # Renames have no cancellation points; a failed second proof restores both.
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


def _write_manifest(target: Path, data: dict) -> None:
    with (target / "manifest.tmp").open("w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    (target / "manifest.tmp").replace(target / "manifest.json")
    descriptor = os.open(target, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _admin_matches(path: Path, data: dict, *, contents: bool = True) -> bool:
    try:
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) !=
                (data["admin_device"], data["admin_inode"])):
            return False
        pointer = path / "gitdir"
        if not contents and not pointer.exists() and not pointer.is_symlink():
            return True  # Our interrupted rmtree may already have removed it.
        return not pointer.is_symlink() and pointer.read_bytes().hex() == data["admin_gitdir"]
    except FileNotFoundError:
        return False


def validate_registration(target: Path) -> None:
    """Condition 9 is rechecked before the job row can be removed."""
    data = json.loads((target / "manifest.json").read_text())
    if data.get("admin") and not _admin_matches(Path(data["admin"]), data):
        raise ValueError("condition 9: Git admin registration changed during retirement")


def _remove_registration(data, target, checkpoint, progress):
    """Detach exactly the recorded generation; old journals leave registrations."""
    if not all(key in data for key in ("admin", "admin_inode", "admin_device", "admin_gitdir")):
        return None
    admin = Path(data["admin"])
    common = Path(data["common"]).resolve()
    if admin.parent != common / "worktrees":
        raise ValueError("invalid retired Git admin directory")
    if "admin_retired" not in data:
        if not admin.exists() and not admin.is_symlink():
            return None
        if not _admin_matches(admin, data):
            raise ValueError("Git admin registration changed or points at another worktree")
        # A caller repository may be on another filesystem. Detach to a unique
        # sibling there; journal its destination before the atomic rename.
        retired = (target / "admin" if data["admin_device"] == target.stat().st_dev
                   else admin.parent / (".subfleet-retention-" + uuid.uuid4().hex))
        data["admin_retired"] = str(retired)
        _write_manifest(target, data)
    retired = Path(data["admin_retired"])
    if retired.parent not in (target, admin.parent) or (retired.parent == admin.parent
            and not retired.name.startswith(".subfleet-retention-")):
        raise ValueError("invalid detached Git admin directory")
    if data.get("admin_detached"):
        if not retired.exists() and not retired.is_symlink():
            return None  # Never reconsider a replacement at the original path.
        if not _admin_matches(retired, data, contents=False):
            raise ValueError("detached Git admin registration was replaced")
        return retired
    if not retired.exists() and not retired.is_symlink():
        if not admin.exists() and not admin.is_symlink():
            return None
        checkpoint()
        if not _admin_matches(admin, data):
            raise ValueError("Git admin registration changed or points at another worktree")
        admin.rename(retired)
    # Verify again after rename, so a racing replacement is restored, never
    # deleted. A crash here repeats this check before starting any deletion.
    if not _admin_matches(retired, data):
        if not admin.exists() and not admin.is_symlink():
            retired.rename(admin)
        raise ValueError("Git admin registration changed during retirement")
    data["admin_detached"] = True
    _write_manifest(target, data)
    progress["made_progress"] = True
    return retired


def _rmtree(path: Path, *, report_progress: bool = False) -> None:
    """Child-process deletion uses only the standard fd-safe rmtree walker."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("retention needs fd-safe shutil.rmtree")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if not path.is_absolute():
        raise ValueError("retention trash path must be absolute")
    # O_NOFOLLOW on an absolute parent path protects only its final component.
    # Open every ancestor relative to its descriptor so a swapped trash root
    # cannot redirect the child's otherwise fd-safe walker outside owned trash.
    parent = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
    except BaseException:
        os.close(parent)
        raise
    unlink, rmdir = os.unlink, os.rmdir
    reported = False

    def report(function):
        def remove(*args, **kwargs):
            nonlocal reported
            result = function(*args, **kwargs)
            if not reported:
                os.write(sys.stdout.fileno(), b"+")
                reported = True
            return result
        return remove

    # These wrappers live only in the disposable child, retain every dir_fd
    # argument, and report the first successful deletion without filling a pipe.
    if report_progress:
        os.unlink, os.rmdir = report(unlink), report(rmdir)

    class Retry(Exception):
        pass

    def repair(function, failed, error):
        if not isinstance(error, PermissionError):
            raise error
        # Read-only cache directories are repaired only when necessary. Anchor
        # every component to a descriptor and refuse symlinks; path-based chmod
        # could follow an ancestor swapped by another process.
        directory = Path(failed).parent if function in (os.unlink, os.rmdir) else Path(failed)
        descriptor = os.dup(parent)
        try:
            for component in directory.parts:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            if mode | 0o700 == mode:
                raise error  # An ACL or other denial cannot be repaired this way.
            os.fchmod(descriptor, mode | 0o700)
        finally:
            os.close(descriptor)
        raise Retry()

    try:
        while True:
            try:
                shutil.rmtree(path.name, dir_fd=parent, onexc=repair)
                break
            except Retry:
                pass  # Successful deletions remain progress on every retry.
    finally:
        os.unlink, os.rmdir = unlink, rmdir
        os.close(parent)


def _reclaim(path: Path, checkpoint, progress) -> None:
    # rmtree has no cooperative checkpoint hook. A disposable process lets a
    # pass stop even within one huge cache; the next pass resumes what remains.
    checkpoint()
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.set_blocking(child.stdout.fileno(), False)

    def receive_progress():
        if not child.stdout.closed:
            try:
                if os.read(child.stdout.fileno(), 1):
                    progress["made_progress"] = True
            except BlockingIOError:
                pass

    try:
        while child.poll() is None:
            receive_progress()
            checkpoint()
            try:
                child.wait(timeout=0.025)
            except subprocess.TimeoutExpired:
                pass
        output, error = child.communicate()
        if output:
            progress["made_progress"] = True
        if child.returncode:
            raise OSError(error.strip())
        progress["made_progress"] = True
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        receive_progress()
        child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


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
                target.rmdir()
                continue
            data = json.loads(manifest.read_text())
            if data.get("job_id") != identity:
                raise ValueError("invalid retention trash journal")
            admin = _remove_registration(data, target, checkpoint, progress)
            for path in (admin, target / "worktree", target / "job"):
                if path is not None and (path.exists() or path.is_symlink()):
                    _reclaim(path, checkpoint, progress)
            checkpoint()
            manifest.unlink()
            target.rmdir()
            progress["made_progress"] = True
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = {"job_id": identity, "error": str(exc)}
            progress["errors"].append(error)
            store.add_event("retention.trash_error", job_id=identity, data=error)


if __name__ == "__main__":
    _rmtree(Path(sys.argv[1]), report_progress=True)
