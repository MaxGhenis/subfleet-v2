"""C-6.8, C-6.20: what a `git worktree add` leaves at `worktrees/<job id>/`, read before any of it is
reused or removed.

git 2.55 makes a linked worktree in this order (measured 2026-11-12 by holding an add in a
smudge filter, and by killing 360 adds at instants from 0 to 20 ms; `docs/reports/
2026-11-15-worktree-add-c68.md`): the registration's admin directory under
`<common>/worktrees/`, its lock `initializing` (written as `initializing\\n` under the C locale,
and translated under any other), the tree's directory, the `gitdir` backlink, the tree's `.git`,
HEAD, the checkout (by a child, `git reset --hard`, which runs filters as its own children), the
index, and only then the lock's removal, before any `post-checkout` hook. So a registration
whose backlink names a tree under git's `initializing` lock is an add that has not finished, and
one that a live add no longer holds was killed; a tree that passes `finished` was written whole.

Nothing here decides alone: the daemon takes a census of the add first (`Daemon._add_census`),
reads the lock (`lock_state`), and removes only what `prove` accepts. Every OS error read here
that C-6.8 calls transient is raised as `SalvageError(transient=True)`, its cause chained
(`transient`); any other leaves the tree and its registration where they are.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
from pathlib import Path

from . import retention_fs as rfs
from .retention_git import Registration, gitfile_admin, registration, registration_of, same_entry
from .salvage import SalvageError, _failure, _git_env, transient_os_error

#: git's own lock on a registration whose add has not finished, as the C locale writes it.
ADD_LOCK = b"initializing"
#: C-5.5's marker for the processes of a job's add (`SUBFLEET_ATTEMPT`).
MARKER_PREFIX = "worktree-add:"
#: What a lock on a registration is: none, git's own while an add runs, or anyone else's.
NONE, ADD, OTHER = "none", "add", "other"
UNREADABLE = "unreadable"


def marker(job_id: str) -> str:
    return MARKER_PREFIX + job_id


#: Config every add pins in its environment (C-6.8). With `core.fsmonitor=true`, git 2.55 starts
#: `git fsmonitor--daemon run --detach` from the add: a helper in a group of its own, ppid 1, that
#: keeps the add's marker and so holds the job until it exits (review r1 of #329, P2).
PINNED_CONFIG = (("core.fsmonitor", "false"),)


def _config_count(value: str) -> int | None:
    """What git reads from `GIT_CONFIG_COUNT`: C's `strtoul` over the whole value (empty is 0).
    None when git refuses the value ("bogus count", "too many entries") and reads no config."""
    digits = value.lstrip(" \t\n\v\f\r")
    sign = digits[:1] if digits[:1] in ("+", "-") else ""
    digits = digits[len(sign):]
    if value == "":
        return 0
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    count = int(digits)
    if count > 2**31 - 1 or (sign == "-" and count):
        return None                         # "too many entries" (strtoul wraps a negative)
    return count


def pinned(env: dict[str, str]) -> dict[str, str]:
    """C-6.8: `env` with `PINNED_CONFIG` appended after any `GIT_CONFIG_COUNT` entries it
    already has. Git reads these as `git -c` (command-line scope, over every config file), and
    every git the add starts inherits them. A count git refuses is left as it is: git then reads
    no config at all and runs nothing."""
    env = dict(env)
    count = _config_count(env.get("GIT_CONFIG_COUNT", ""))
    if count is None:
        return env
    for offset, (key, value) in enumerate(PINNED_CONFIG):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + len(PINNED_CONFIG))
    return env


def transient(exc: OSError, what: str) -> SalvageError:
    """C-6.8: an OS error in an ownership read, transient by its errno, with the cause chained."""
    error = SalvageError(f"{what}: {os.strerror(exc.errno) if exc.errno else type(exc).__name__}",
                         transient=True)
    error.__cause__ = exc
    return error


def check_transient(exc: OSError, what: str) -> None:
    """Raise `exc` as C-6.8's transient failure when its errno is one (F7); else return."""
    if transient_os_error(exc):
        raise transient(exc, what) from exc


def container_ok(root: str | os.PathLike) -> bool:
    """C-13.4: `<root>/worktrees` resolves to itself, so nothing removed below it is elsewhere."""
    allocated = Path(root) / "worktrees"
    try:
        if allocated.parent.resolve(strict=True) != allocated.parent:
            return False
        try:
            info = os.lstat(allocated)
        except FileNotFoundError:
            return True
        return stat.S_ISDIR(info.st_mode) and allocated.resolve(strict=True) == allocated
    except OSError as exc:
        check_transient(exc, "the worktrees folder could not be read")
        return False


def lock_state(admin: Path | None, *, unreadable: str = OTHER) -> str:
    """`NONE`, `ADD` (git's `initializing`, exactly as the C locale writes it) or `OTHER` (a
    person's, retention's, an empty one, one that is not a regular file, one that cannot be read)."""
    if admin is None:
        return NONE
    try:
        data = rfs.read_regular(admin / "locked", limit=4096)
    except FileNotFoundError:
        return NONE
    except OSError as exc:
        check_transient(exc, "a worktree lock could not be read")
        return unreadable
    return ADD if data in (ADD_LOCK, ADD_LOCK + b"\n") else OTHER


def _git(git_dir: Path, *args: str, timeout_s: float) -> subprocess.CompletedProcess:
    """One capped git call against `git_dir` (an admin directory or a common directory) that
    reads no work tree. A call that did not finish raises, transient."""
    # A submodule's common config may point core.worktree at a checkout that
    # has gone. These object/index reads must not enter that unrelated path.
    command = ["git", "-c", "core.fsmonitor=false", f"--git-dir={git_dir}",
               f"--work-tree={git_dir}", *args]
    try:
        return subprocess.run(command, cwd=git_dir, env=_git_env(None), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise SalvageError(f"git {args[0]} timed out after {timeout_s:g} s", timed_out=True) from exc
    except OSError as exc:
        check_transient(exc, f"git {args[0]} could not run")
        raise SalvageError(f"git {args[0]} could not run: {type(exc).__name__}") from exc


def head(admin: Path, *, timeout_s: float) -> str | None:
    """HEAD as the registration's own admin directory names it (F2: never through a gitfile)."""
    result = _git(admin, "rev-parse", "--verify", "--quiet", "HEAD", timeout_s=timeout_s)
    failure = _failure("rev-parse", result)
    if failure is not None and failure.transient:
        raise failure
    return os.fsdecode(result.stdout).strip() or None if result.returncode == 0 else None


def staged(admin: Path, commit: str, *, timeout_s: float) -> str | None:
    """None when the registration's index stages nothing beyond `commit` (no index, or one that
    stages only deletions, as a checkout stopped before its index leaves); else why not.
    Read whenever a registration would go, whatever is at its tree's path (F1)."""
    try:
        info = os.lstat(admin / "index")
    except FileNotFoundError:
        return None
    except OSError as exc:
        check_transient(exc, "a worktree index could not be read")
        return "its registration's index cannot be read"
    if not stat.S_ISREG(info.st_mode):
        return "its registration's index is not a regular file"
    result = _git(admin, "diff-index", "--cached", "-z", "--name-status", "--no-renames", commit, "--",
                  timeout_s=timeout_s)
    if result.returncode:
        failure = _failure("diff-index", result)
        if failure is not None and failure.transient:
            raise failure
        return "its registration's index cannot be read"
    fields = result.stdout.split(b"\0")
    for status, name in zip(fields[::2], fields[1::2]):
        if status != b"D":
            return f"its registration's index stages {os.fsdecode(name)!r}"
    return None


def removal_head(admin: Path, commit: str, *, timeout_s: float) -> str | None:
    """An add's registration cannot be the sole holder of a later commit.

    Before checkout, git may not have written HEAD yet, or may have left its
    all-zero placeholder. Only its initializing lock establishes that shape.
    A readable, different HEAD stays even when its tree and index are identical.
    """
    try:
        raw = rfs.read_regular(admin / "HEAD", limit=65536).strip()
    except FileNotFoundError:
        return None if lock_state(admin) == ADD else "its registration has no HEAD"
    except OSError as exc:
        check_transient(exc, "the registration's HEAD could not be read")
        return "its registration's HEAD cannot be read"
    if raw in (b"0" * 40, b"0" * 64) and lock_state(admin) == ADD:
        return None
    if head(admin, timeout_s=timeout_s) != commit:
        return "its registration's HEAD is not the job's commit"
    return None


def commit_kinds(common: str | os.PathLike, commit: str, *, timeout_s: float) -> dict[str, str]:
    """path -> kind (`file`, `link` or `directory`) for every path of `commit`; a submodule is a
    directory, as a checkout leaves it. Raises SalvageError when git cannot list it."""
    result = _git(Path(common), "ls-tree", "-r", "-t", "-z", "--full-tree", commit, timeout_s=timeout_s)
    failure = _failure("ls-tree", result)
    if failure is not None:
        raise failure
    kinds: dict[str, str] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, kind = meta.split(b" ")[:2]
        kinds[os.fsdecode(name)] = ("link" if mode == b"120000" else "file") if kind == b"blob" else "directory"
    return kinds


def _patch_kinds(patch: bytes) -> list[str | None]:
    """Destination kinds in a kept, no-renames patch, in `git apply --numstat` order.

    Git's mode headers describe type changes as well as content-only edits. A
    deletion adds no destination kind: the commit already authorizes its old
    kind halfway through applying the copy. Filenames come from git's
    NUL-delimited output below.
    """
    modes: list[bytes | None] = []
    in_header = False
    for line in patch.splitlines():
        if line.startswith(b"diff --git "):
            modes.append(None)
            in_header = True
        elif in_header:
            if line.startswith(b"deleted file mode "):
                modes[-1] = b"deleted"
            elif line.startswith((b"new mode ", b"new file mode ")):
                modes[-1] = line.rsplit(b" ", 1)[-1]
            elif line.startswith(b"index ") and modes[-1] is None:
                fields = line.split()
                if len(fields) == 3:
                    modes[-1] = fields[-1]
            elif line.startswith((b"--- ", b"GIT binary patch")):
                in_header = False
    kinds = {b"100644": "file", b"100755": "file", b"120000": "link", b"deleted": None}
    if not modes or any(mode not in kinds for mode in modes):
        raise SalvageError("the job's kept patch has no readable destination kinds")
    return [kinds[mode] for mode in modes]


def kept_paths(job_dir: str | os.PathLike, *, timeout_s: float) -> dict[str, str]:
    """Step 0, item 6: the paths a job's own kept copy (`jobs/<id>/uncommitted/`) puts in its
    tree, which a tree cut and stopped mid-copy holds beyond its commit: path -> its actual
    destination kind, and `directory` for each folder above one."""
    from . import uncommitted
    source = Path(job_dir) / uncommitted.DIRECTORY
    named: dict[str, str] = {}
    files = source / uncommitted.FILES
    pending = [files]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
            for entry in entries:
                path = Path(entry.path)
                kind = _kind(entry.stat(follow_symlinks=False).st_mode)
                if kind not in ("file", "link", "directory"):
                    raise SalvageError("the job's kept copy holds a special file")
                named[path.relative_to(files).as_posix()] = kind
                if kind == "directory":
                    pending.append(path)
        except FileNotFoundError:
            if directory == files:
                continue
            raise SalvageError("the job's kept copy changed while it was read") from None
        except OSError as exc:
            check_transient(exc, "the job's kept copy could not be read")
            raise SalvageError("the job's kept copy could not be read") from exc
    try:
        patch = (source / uncommitted.PATCH).read_bytes()
    except FileNotFoundError:
        patch = b""
    except OSError as exc:
        check_transient(exc, "the job's kept copy could not be read")
        raise SalvageError("the job's kept copy could not be read") from exc
    if patch:
        try:
            result = subprocess.run(["git", "apply", "--numstat", "-z", "-"], input=patch, capture_output=True,
                                    env=_git_env(None), cwd=source, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise SalvageError(f"git apply timed out after {timeout_s:g} s", timed_out=True) from exc
        except OSError as exc:
            check_transient(exc, "git apply could not run")
            raise SalvageError("git apply could not run") from exc
        failure = _failure("apply", result)
        if failure is not None:
            raise failure
        names = [record.split(b"\t", 2)[2] for record in result.stdout.split(b"\0")
                 if record.count(b"\t") >= 2]
        kinds = _patch_kinds(patch)
        if len(names) != len(kinds):
            raise SalvageError("the job's kept patch has ambiguous destination kinds")
        for name, kind in zip(names, kinds):
            relative = os.fsdecode(name)
            if not relative or relative.startswith("/") or any(part in ("..", ".git") for part in relative.split("/")):
                raise SalvageError("the job's kept copy names an unsafe path")
            if kind is not None:
                named[relative] = kind
    for name in list(named):
        parent = os.path.dirname(name)
        while parent:
            named.setdefault(parent, "directory")
            parent = os.path.dirname(parent)
    return named


def _kind(mode: int) -> str:
    return ("link" if stat.S_ISLNK(mode) else "directory" if stat.S_ISDIR(mode)
            else "file" if stat.S_ISREG(mode) else "special file")


def prove(tree: str | os.PathLike, admin: Path | None, common: str | os.PathLike | None, commit: str, *,
          timeout_s: float, kept: dict[str, str] | None = None) -> str | None:
    """C-6.8 (D): None when everything at `tree` and in its registration `admin` is what an add of
    `commit` can leave, so removing it loses nothing; else what was found (our words and the
    tree's own names, for daemon.log only).

    The admin index is read first, whatever is at the path (F1). Then the path: missing is
    nothing; a link or anything but a directory is not an add's. In a directory, every path
    must be one `commit` has, of the kind it has there, or one the job's kept copy names
    (`kept`); a `.git` must be a gitfile naming this registration, or one under the job's
    repository's `worktrees/` that no longer exists (a removal cut short). Nothing may be
    unreadable. Contents are not compared: an add stopped mid-file leaves one half written."""
    if admin is not None:
        why = staged(admin, commit, timeout_s=timeout_s)
        if why is not None:
            return why
        why = removal_head(admin, commit, timeout_s=timeout_s)
        if why is not None:
            return why
        try:
            container = Path(common) / "worktrees" if common is not None else None
            if (container is None or not stat.S_ISDIR(os.lstat(container).st_mode)
                    or not same_entry(admin.parent, container)):
                return "its registration is not directly under its repository's worktrees folder"
        except OSError as exc:
            check_transient(exc, "the registration container could not be read")
            return "its registration container cannot be read"
    path = Path(tree)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        check_transient(exc, "the worktree could not be read")
        return "the path cannot be read"
    if stat.S_ISLNK(info.st_mode):
        return "a link stands at the path"
    if not stat.S_ISDIR(info.st_mode):
        return "the path is not a directory"
    if common is None:
        return "no repository can be named to compare it with"
    kinds = commit_kinds(common, commit, timeout_s=timeout_s)
    kept = kept or {}
    gitlink = path / ".git"
    try:
        gitinfo = os.lstat(gitlink)
    except FileNotFoundError:
        gitinfo = None
    except OSError as exc:
        check_transient(exc, "the worktree's .git could not be read")
        return "its .git cannot be read"
    if gitinfo is not None:
        if not stat.S_ISREG(gitinfo.st_mode):
            return "its .git is not a gitfile"
        try:
            named, _, _ = gitfile_admin(path, strict=True)
            worktrees = Path(common) / "worktrees"
            if named is None or not same_entry(named.parent, worktrees):
                return "its .git names another repository"
            try:
                os.lstat(named)
            except FileNotFoundError:
                exists = False
            else:
                exists = True
            if exists and (admin is None or not same_entry(named, admin)):
                return "its .git names another worktree"
        except OSError as exc:
            check_transient(exc, "the worktree's .git could not be read")
            return "its .git cannot be read"
    pending = [""]
    while pending:
        relative_dir = pending.pop()
        directory = path / relative_dir if relative_dir else path
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError as exc:
            check_transient(exc, "the worktree could not be read")
            return f"part of it cannot be read ({relative_dir or '.'})"
        for entry in entries:
            relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
            if relative == ".git":
                continue
            try:
                there = _kind(entry.stat(follow_symlinks=False).st_mode)
            except OSError as exc:
                check_transient(exc, "the worktree could not be read")
                return f"part of it cannot be read ({relative})"
            if relative in kinds and kinds[relative] == there:
                pass
            elif relative in kept and kept[relative] == there:
                pass
            elif relative not in kinds and relative not in kept:
                return f"it holds {relative!r}, which the commit does not"
            else:
                return f"it holds {relative!r} as a {there}, unlike the commit or the job's kept copy"
            if there == "directory":
                pending.append(relative)
            else:
                try:
                    if there == "link":
                        os.readlink(entry.path)
                    else:
                        # Open without following a replaced final link. Reading all bytes
                        # also detects an I/O failure partway through an owned file.
                        descriptor = os.open(entry.path, rfs.O_FILE)
                        try:
                            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                                return f"part of it changed kind ({relative})"
                            while os.read(descriptor, rfs.CHUNK):
                                pass
                        finally:
                            os.close(descriptor)
                except OSError as exc:
                    check_transient(exc, "the worktree could not be read")
                    return f"part of it cannot be read ({relative})"
    return None


def finished(tree: str | os.PathLike, common: str | os.PathLike | None, commit: str, *,
             timeout_s: float) -> tuple[Registration | None, str | None]:
    """C-6.8 (B): (its registration, None) when `tree` is a checkout of `commit` that git finished;
    else (what was read, why not). A directory, not a link; its `.git` a gitfile naming an admin
    directory directly under the job's repository's `worktrees/` whose backlink names this tree
    (`retention_git.registration`, F2); no `initializing` lock there (a person's lock is not one:
    C-6.20's locked tree still runs); HEAD, read through that admin directory, at `commit`; and
    an index there that stages nothing against it. The census and a copy's marker are the
    caller's."""
    path = Path(tree)
    try:
        info = os.lstat(path)
    except OSError as exc:
        check_transient(exc, "the worktree could not be read")
        return None, "the path cannot be read"
    if not stat.S_ISDIR(info.st_mode):
        return None, "the path is not a directory"
    try:
        found, why = registration(path, strict=True)
    except OSError as exc:
        check_transient(exc, "the worktree's registration could not be read")
        return None, "its registration cannot be read"
    if found is None:
        return None, f"its .git names no registration of it ({why})"
    try:
        if common is None or not same_entry(common, found.common):
            return found, "its .git names another repository"
    except OSError as exc:
        check_transient(exc, "the repository could not be read")
        return found, "the repository cannot be read"
    state = lock_state(found.admin, unreadable=UNREADABLE)
    if state == ADD:
        return found, "git's initializing lock stands on its registration"
    if state == UNREADABLE:
        return found, "its registration's lock cannot be read"
    if head(found.admin, timeout_s=timeout_s) != commit:
        return found, "its HEAD is not the job's commit"
    try:
        info = os.lstat(found.admin / "index")
    except FileNotFoundError:
        return found, "its registration has no index"
    except OSError as exc:
        check_transient(exc, "a worktree index could not be read")
        return found, "its registration's index cannot be read"
    if not stat.S_ISREG(info.st_mode):
        return found, "its registration's index is not a regular file"
    result = _git(found.admin, "diff-index", "--cached", "--quiet", commit, "--", timeout_s=timeout_s)
    if result.returncode:
        failure = _failure("diff-index", result) if result.returncode != 1 else None
        if failure is not None and failure.transient:
            raise failure
        return found, "its index stages changes against the job's commit"
    return found, None


def drop_registration(common: str | os.PathLike, tree: str | os.PathLike, commit: str, *,
                      timeout_s: float) -> tuple[Path | None, str | None]:
    """C-6.20: remove `tree`'s own registration from `common`, and nothing else (d635), read
    again here: (the admin directory removed, None), (None, None) when there is none, or
    (None, why it stays). It stays under any lock but git's `initializing` one (C-6.20: a
    person's or retention's is a claim; `lock_state`), and when its index stages anything
    beyond `commit` (F1). A dead add's lock goes with it: the caller has read the census."""
    try:
        admin = registration_of(common, tree, strict=True)
    except OSError as exc:
        check_transient(exc, "the repository's worktrees could not be read")
        return None, "the repository's worktrees cannot be read"
    if admin is None:
        return None, None
    if lock_state(admin) == OTHER:
        return None, "a lock stands on its registration"
    why = staged(admin, commit, timeout_s=timeout_s)
    if why is not None:
        return None, why
    why = removal_head(admin, commit, timeout_s=timeout_s)
    if why is not None:
        return None, why
    if lock_state(admin) == OTHER:                   # read again: one may have come since
        return None, "a lock stands on its registration"
    try:
        shutil.rmtree(admin)
    except FileNotFoundError:
        pass
    except OSError as exc:
        check_transient(exc, "the worktree's registration could not be removed")
        return None, "its registration could not be removed"
    return admin, None


def remove_tree(tree: str | os.PathLike) -> OSError | None:
    """`.git` first, never through a link, then the files: a removal cut short leaves no tree a
    later look reuses (its check reads `.git` first). The first error, or None."""
    path = Path(tree)
    try:
        descriptor = os.open(path, rfs.O_DIR)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    try:
        identity = os.fstat(descriptor)
        try:
            os.unlink(".git", dir_fd=descriptor)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return exc
    finally:
        os.close(descriptor)
    try:
        if not os.path.samestat(identity, os.lstat(path)):
            return OSError(errno.ESTALE, "the worktree changed before removal")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    failures: list[OSError] = []
    shutil.rmtree(path, onexc=lambda _function, _path, exc: failures.append(exc))
    if failures:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return exc
        return failures[0]
    return None
