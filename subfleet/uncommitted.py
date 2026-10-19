"""C-6.6, D-SH1 round 6: a policy review's worktree starts with what its caller sees.

A review whose sandbox comes from a `worktree-write` policy entry runs in a job
worktree cut at the submit HEAD (C-11.1). Before round 6 it then reviewed HEAD,
not the uncommitted or untracked files the caller had in front of them (#221
review r1b, P2). Now, at submit, the caller's uncommitted tracked changes
(`git diff --binary <head>`) and untracked, non-ignored files (`git ls-files
--others --exclude-standard`) are planned, checked to apply to `<head>`, and
kept in the job folder; when the worktree is cut the patch is applied and the
files are copied in. Ignored files are never copied.

A plan that is too large (more than `MAX_FILES` paths, or more than
`MAX_BYTES` of patch and file bytes together), whose patch does not apply to
`<head>`, that changes a submodule, or that names an untracked nested
repository is refused, and the review falls back to read-only in place, where
it sees the caller's checkout as it is. `git ls-files --others` lists only
files, symbolic links and nested repositories (never a FIFO or a socket, which
are simply not copied), so a nested repository is the only untracked path that
reaches the not-a-file fallback (#221 review r2, P3-2). That decision is made at submit, before
the job exists, so no sandbox is ever lowered after submit (d261).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .salvage import SalvageError, _git_bytes

#: The bound on what is copied: changed tracked paths plus untracked files...
MAX_FILES = 1000
#: ...and the patch's bytes plus the untracked files' bytes, together (16 MiB).
MAX_BYTES = 16 * 1024 * 1024
#: Where a job keeps its copy, under its job folder.
DIRECTORY = "uncommitted"
PATCH = "changes.patch"
FILES = "untracked"
#: Written in the copy once it is applied, naming the worktree it was applied
#: to (#221 review r2, P3-1). A worktree whose marker is missing was cut by a
#: daemon that stopped before the copy was in, and is cut again.
APPLIED = "applied"
#: `git diff` options that make the patch the same whatever the caller's config says.
DIFF_OPTIONS = ("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                "--no-relative", "--src-prefix=a/", "--dst-prefix=b/", "--ignore-submodules=none")
APPLY_OPTIONS = ("apply", "--binary", "--whitespace=nowarn")


class CopyRefused(Exception):
    """The plan cannot be copied; the review falls back to read-only in place."""


@dataclass
class Plan:
    top: Path
    head: str
    patch: bytes
    tracked: int
    untracked: list[str] = field(default_factory=list)
    bytes: int = 0

    @property
    def files(self) -> int:
        return self.tracked + len(self.untracked)

    def record(self) -> dict:
        return {"files": self.files, "tracked": self.tracked, "untracked": len(self.untracked),
                "bytes": self.bytes}


def _git(top: Path, *args: str, env: dict | None = None, timeout_s: float | None = None) -> bytes:
    out = _git_bytes(top, *args, env=env, timeout_s=timeout_s)
    if out is None:
        raise CopyRefused(f"git {args[0]} failed")
    return out


def _gitlinks(raw: bytes) -> bool:
    """Whether a `git diff --raw -z` listing changes a submodule (mode 160000)."""
    for entry in raw.split(b"\0"):
        if entry.startswith(b":"):
            modes = entry[1:].split(b" ")[:2]
            if b"160000" in modes:
                return True
    return False


def _size(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MiB" if limit >= 1024 * 1024 else f"{limit} bytes"


def plan(top: str | Path, head: str, *, scratch_root: str | Path, timeout_s: float | None = None,
         max_files: int | None = None, max_bytes: int | None = None) -> Plan | None:
    """What the caller has beyond `head`, or None for a clean checkout.

    Raises `CopyRefused` with the reason the copy cannot be made, and
    `SalvageError` when git did not finish (C-6.8: the submit is retried).
    The applicability check's scratch index lives under `scratch_root`, inside
    the state root (C-2.1).
    """
    top = Path(top)
    max_files = MAX_FILES if max_files is None else max_files
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    names = [name for name in _git(top, "diff", "--name-only", "-z", "--no-renames", "--no-relative",
                                   "--ignore-submodules=none", head, "--", timeout_s=timeout_s).split(b"\0") if name]
    raw = _git(top, "diff", "--raw", "-z", "--no-renames", "--no-relative", "--ignore-submodules=none",
               head, "--", timeout_s=timeout_s)
    if _gitlinks(raw):
        raise CopyRefused("a submodule changed")
    untracked = [os.fsdecode(name) for name in _git(top, "ls-files", "--others", "--exclude-standard", "-z",
                                                     timeout_s=timeout_s).split(b"\0") if name]
    if not names and not untracked:
        return None
    if len(names) + len(untracked) > max_files:
        raise CopyRefused(f"{len(names) + len(untracked)} files, more than {max_files}")
    total = 0
    for name in untracked:
        try:
            info = os.lstat(top / name)
        except OSError as exc:
            raise CopyRefused(f"untracked {name} cannot be read: {exc.strerror}") from None
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            # Git lists only files, symlinks and nested repositories: this is a nested repository.
            raise CopyRefused(f"untracked {name} is not a regular file or symbolic link")
        total += info.st_size
        if total > max_bytes:
            raise CopyRefused(f"more than {_size(max_bytes)}")
    patch = _git(top, *DIFF_OPTIONS, head, "--", timeout_s=timeout_s) if names else b""
    total += len(patch)
    if total > max_bytes:
        raise CopyRefused(f"more than {_size(max_bytes)}")
    if patch:
        _check_applies(top, head, patch, Path(scratch_root), timeout_s)
    return Plan(top, head, patch, len(names), untracked, total)


def _check_applies(top: Path, head: str, patch: bytes, scratch_root: Path, timeout_s: float | None) -> None:
    """The patch applies to `head`'s tree, checked in a scratch index the
    caller's own index never sees."""
    scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="subfleet-uncommitted-", dir=scratch_root) as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(top, "read-tree", head, env=env, timeout_s=timeout_s)
        if _git_bytes(top, *APPLY_OPTIONS, "--cached", "--check", "-", env=env, stdin=patch,
                      timeout_s=timeout_s) is None:
            raise CopyRefused(f"the changes do not apply to {head[:12]}")


def keep(found: Plan, job_dir: str | Path) -> dict:
    """Copy the plan into `<job dir>/uncommitted`, as of now; the record for the job.

    A file that changed since `plan` is copied as it is now, within the bound;
    one that went away fails the submit (`CopyRefused`), which the caller then
    retries, rather than keeping a copy that does not match the plan's count.
    """
    target = Path(job_dir) / DIRECTORY
    files = target / FILES
    files.mkdir(parents=True, mode=0o700)
    (target / PATCH).write_bytes(found.patch)
    total = len(found.patch)
    for name in found.untracked:
        source = found.top / name
        destination = files / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = os.lstat(source)
            if stat.S_ISLNK(info.st_mode):
                os.symlink(os.readlink(source), destination)
            elif stat.S_ISREG(info.st_mode):
                shutil.copy2(source, destination, follow_symlinks=False)
            else:
                raise CopyRefused(f"untracked {name} is no longer a regular file or symbolic link")
        except OSError as exc:
            raise CopyRefused(f"untracked {name} could not be copied: {exc.strerror}") from None
        total += info.st_size
    if total > MAX_BYTES:
        raise CopyRefused(f"more than {_size(MAX_BYTES)}")
    return {**found.record(), "bytes": total}


def applied(job_dir: str | Path, worktree: str | Path) -> bool:
    """Whether the kept copy was applied, in full, to `worktree`."""
    try:
        return (Path(job_dir) / DIRECTORY / APPLIED).read_text() == str(worktree)
    except (OSError, UnicodeDecodeError):
        return False


def apply(job_dir: str | Path, worktree: str | Path, *, timeout_s: float | None = None) -> None:
    """Apply a kept copy to a freshly cut worktree, then mark it applied
    (`applied`). Raises `CopyRefused` or `SalvageError`."""
    source = Path(job_dir) / DIRECTORY
    marker = source / APPLIED
    marker.unlink(missing_ok=True)          # a marker from an earlier cut says nothing of this one
    patch = (source / PATCH).read_bytes()
    if patch and _git_bytes(worktree, *APPLY_OPTIONS, "-", stdin=patch, timeout_s=timeout_s) is None:
        raise CopyRefused("the kept changes did not apply to the new worktree")
    files = source / FILES
    for path in sorted(files.rglob("*")):
        relative = path.relative_to(files)
        destination = Path(worktree) / relative
        if path.is_dir() and not path.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise CopyRefused(f"untracked {relative} already exists in the worktree")
        if path.is_symlink():
            os.symlink(os.readlink(path), destination)
        else:
            shutil.copy2(path, destination, follow_symlinks=False)
    pending = source / (APPLIED + ".tmp")
    pending.write_text(str(worktree))
    os.replace(pending, marker)


__all__ = ["APPLIED", "CopyRefused", "MAX_BYTES", "MAX_FILES", "Plan", "SalvageError", "apply", "applied", "keep",
           "plan"]
