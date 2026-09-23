"""Private git snapshots which leave HEAD, the index and worktree alone."""

from __future__ import annotations

import errno
import os
import re
import stat
import subprocess
import unicodedata
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from .adapters.base import AdapterError

#: One git call's wall-clock cap when the caller names none. The 15 s this
#: replaced failed jobs whose repository was healthy: on 2026-09-20, under a
#: load average near 10, a snapshot that takes 0.6 s on an idle machine ran past
#: it. `caps.workspace_git_timeout_s` is the daemon's setting; this variable is
#: for callers that have no policy in hand.
DEFAULT_GIT_TIMEOUT_S = 60
GIT_TIMEOUT_ENV = "SUBFLEET_GIT_TIMEOUT_S"

#: `OSError`s that describe the machine at this moment, not the repository: the
#: same call is expected to succeed once the pressure passes.
TRANSIENT_ERRNOS = frozenset({
    errno.EAGAIN, errno.EINTR, errno.ENOMEM, errno.EMFILE, errno.ENFILE,
    errno.EBUSY, errno.ETIMEDOUT, errno.ENOBUFS,
})


class SalvageError(RuntimeError):
    """A snapshot failed; callers must retain the workspace for reconciliation.

    ``transient`` is true when the failure says nothing about the repository (a
    timeout, or an `OSError` in `TRANSIENT_ERRNOS`), so the caller may retry.
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def git_timeout_s(timeout_s: float | None = None) -> float:
    """The explicit cap, else `SUBFLEET_GIT_TIMEOUT_S`, else the default."""
    if timeout_s is not None:
        return timeout_s
    try:
        value = float(os.environ.get(GIT_TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_GIT_TIMEOUT_S
    return value if value > 0 else DEFAULT_GIT_TIMEOUT_S


def transient_os_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS


def _git(workdir: str | Path, *args: str, env: dict[str, str] | None = None,
         optional: bool = False, timeout_s: float | None = None, raw: bool = False) -> str | None:
    cap = git_timeout_s(timeout_s)
    try:
        result = subprocess.run(["git", "-C", str(workdir), *args], env=env,
                                capture_output=True, text=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        # Never `optional`: a call that did not finish has not said "no HEAD" or
        # "no branch", and reading it that way admits a writable job with no
        # baseline or lets one past the main/master refusal.
        raise SalvageError(f"git {args[0]} timed out after {cap:g} s", transient=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        if transient_os_error(exc):
            raise SalvageError(f"git {args[0]} could not run: {exc}", transient=True) from exc
        if optional:
            return None
        raise SalvageError(f"git {args[0]} unavailable: {type(exc).__name__}: {exc}") from exc
    if result.returncode:
        if optional:
            return None
        raise SalvageError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout if raw else result.stdout.strip()


def git_head(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    return _git(workdir, "rev-parse", "--verify", "HEAD", optional=True, timeout_s=timeout_s)


def git_branch(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    return _git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True,
                timeout_s=timeout_s)


def git_toplevel(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    """C-6.5: the real path of the worktree that holds ``workdir``, or None outside one.

    Two directories of one checkout (`/repo` and `/repo/sub`) are one place to
    write; two linked worktrees of one repository are two.
    """
    top = _git(workdir, "rev-parse", "--show-toplevel", optional=True, timeout_s=timeout_s)
    return os.path.realpath(top) if top else None


def git_tree(workdir: str | Path, commit: str, *, timeout_s: float | None = None) -> str | None:
    """The tree of ``commit``, or None when the repository cannot name it."""
    return _git(workdir, "rev-parse", "--verify", f"{commit}^{{tree}}", optional=True,
                timeout_s=timeout_s)


class WorktreeCheck(NamedTuple):
    """What `check_worktree` found at a path the daemon allocates (C-6.8)."""
    #: The path's entry in its repository's `git worktree list --porcelain`,
    #: {"head": sha, "locked": reason}, each when shown; None when not listed.
    registration: dict[str, str] | None
    #: Why the path is not a finished checkout of the commit; None when it is.
    unfinished: str | None


def _lexical(path: str | Path) -> str:
    """``path`` with its directory resolved and its last component kept: a link there is not what it points to.

    `..` is resolved physically, as the directory's real path, never by text.
    """
    path = os.fspath(path).rstrip(os.sep) or os.sep
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    directory, name = os.path.split(path)
    return os.path.join(os.path.realpath(directory), name)


def worktree_registrations(listing: str) -> list[tuple[str, dict[str, str]]]:
    """Every entry of a `git worktree list --porcelain -z` listing: (path, {"head": sha, "locked": reason})."""
    entries: list[tuple[str, dict[str, str]]] = []
    path, entry = None, {}
    for field in listing.split("\0"):
        if not field:
            if path is not None:
                entries.append((path, entry))
            path, entry = None, {}
        elif field.startswith("worktree "):
            path = field[len("worktree "):]
        elif field.startswith("HEAD "):
            entry["head"] = field[len("HEAD "):]
        elif field == "locked" or field.startswith("locked "):
            entry["locked"] = field[len("locked "):]
    if path is not None:
        entries.append((path, entry))
    return entries


def _same_path(listed: str | Path, target: str | Path) -> bool:
    """Whether two paths name one place, each with its last component kept, as the filesystem compares names.

    Beyond equal text, a last component equal but for case or Unicode
    normalization in one directory counts too: APFS compares names that way,
    and git, with `core.ignorecase`, finds a worktree by its path that way.
    """
    listed, target = _lexical(listed), _lexical(target)
    if listed == target:
        return True
    def folded(name: str) -> str:
        return unicodedata.normalize("NFD", name).casefold()
    if folded(os.path.basename(listed)) != folded(os.path.basename(target)):
        return False
    try:
        return os.path.samefile(os.path.dirname(listed), os.path.dirname(target))
    except OSError:
        return False


def worktree_registration(listing: str, worktree: str | Path) -> dict[str, str] | None:
    """``worktree``'s entry in a `git worktree list --porcelain -z` listing, or None.

    Both sides keep their last component, so a registration whose path is now
    a link is still the registration of that path, lock and all; names are
    compared as the filesystem compares them (`_same_path`). Two entries for
    one path are not an answer: that raises.
    """
    found = [entry for path, entry in worktree_registrations(listing) if _same_path(path, worktree)]
    if len(found) > 1:
        raise SalvageError(f"the repository lists {len(found)} worktrees at {worktree}")
    return found[0] if found else None


def check_worktree(repository: str | Path, worktree: str | Path, commit: str, *,
                   timeout_s: float | None = None) -> WorktreeCheck:
    """C-6.8: whether ``worktree`` is a finished checkout of ``commit``, and its registration.

    Finished means a directory, not a link, with a `.git` link (git run in a
    directory without one answers for whatever repository encloses it),
    listed by ``repository`` as one of its worktrees, unlocked (git locks a
    worktree `initializing` until its checkout is written, so an add that was
    killed leaves the lock), with HEAD at ``commit`` and an index holding that
    commit's tree (a checkout stopped before it wrote its index has none,
    however many files it wrote). The registration is read whether or not the
    directory exists: an add stopped partway can leave one with no directory.
    The listing is never optional: a repository that cannot be read raises,
    because "not listed" is what lets a directory be removed. A call that did
    not finish raises, transient.
    """
    path = Path(worktree)
    entry = worktree_registration(_git(repository, "worktree", "list", "--porcelain", "-z",
                                       timeout_s=timeout_s, raw=True), path)
    if not os.path.lexists(path):
        return WorktreeCheck(entry, "no directory")
    link = path / ".git"
    if path.is_symlink() or not path.is_dir() or link.is_symlink() or not link.is_file():
        return WorktreeCheck(entry, "no .git link")
    if entry is None:
        return WorktreeCheck(entry, "the job's repository does not list it")
    if "locked" in entry:
        return WorktreeCheck(entry, f"locked ({entry['locked'] or 'no reason given'})")
    if entry.get("head") != commit:
        return WorktreeCheck(entry, f"HEAD is {entry.get('head') or 'unreadable'}")
    if _git(path, "diff-index", "--cached", "--quiet", commit, "--", optional=True, timeout_s=timeout_s) is None:
        return WorktreeCheck(entry, "its index is not a checkout of the commit")
    return WorktreeCheck(entry, None)


def _link_target(file: Path, prefix: str) -> str | None:
    """The path a `.git` link (``prefix`` "gitdir: ") or an admin `gitdir` file names, relative ones resolved from the file's directory."""
    try:
        text = file.read_text(encoding="utf-8").rstrip("\n")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith(prefix) or not text[len(prefix):]:
        return None
    return _lexical(os.path.join(file.parent, text[len(prefix):]))


def foreign_registration(registration: dict[str, str] | None, commit: str) -> str | None:
    """C-6.8: why a registration is not as an add of ``commit`` leaves it, or None when it is (or there is none).

    An add locks the path `initializing` and writes HEAD as zeros and then as
    the commit; git unlocks it once the checkout is written. So it is unlocked
    or under that lock, at the commit or, under the lock only, at zeros (a
    HEAD git cannot read reads as zeros too). Any other lock is someone's, and
    another commit was put there after.
    """
    if registration is None:
        return None
    lock, shown = registration.get("locked"), registration.get("head")
    if lock not in (None, "initializing"):
        return f"it is locked ({lock or 'no reason given'})"
    if shown != commit and not (lock == "initializing" and shown and set(shown) == {"0"}):
        return f"its HEAD is {shown or 'unreadable'}, not {commit}"
    return None


def _kind(mode: int) -> str:
    return "link" if stat.S_ISLNK(mode) else "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "special file"


def _admin_directory(common: str, worktree: Path, link: Path) -> str | None:
    """The administrative directory of ``worktree``'s registration in the repository whose common dir is ``common``.

    Through the `.git` link when there is one, and only if it names a
    directory under `<common>/worktrees/` whose `gitdir` names this link back;
    without a link, the one directory there whose `gitdir` names this path.
    None when no such directory can be told.
    """
    registrations = _lexical(os.path.join(common, "worktrees"))
    if os.path.lexists(link):
        admin = _link_target(link, "gitdir: ")
        if admin is None or os.path.dirname(admin) != registrations:
            return None
        back = _link_target(Path(admin) / "gitdir", "")
        return admin if back is not None and _same_path(back, link) else None
    try:
        candidates = [entry.path for entry in os.scandir(registrations) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    found = [admin for admin in candidates
             if (back := _link_target(Path(admin) / "gitdir", "")) is not None and _same_path(back, link)]
    return _lexical(found[0]) if len(found) == 1 else None


def add_leftover(repository: str | Path, worktree: str | Path, commit: str,
                 registration: dict[str, str] | None, *, timeout_s: float | None = None) -> str | None:
    """C-6.8: None when all there is at ``worktree`` is what a `git worktree add` of ``commit`` that never finished left; otherwise why it is kept.

    An add registers the path, locks it `initializing`, writes HEAD as zeros
    and then as the commit, creates the directory and its `.git` link, and
    checks the commit out; git removes the lock once the checkout is written
    and before any post-checkout hook. So the registration, if any, must be as
    an add leaves it (`foreign_registration`). A link in the path's place is
    unlinked, never followed, and an empty directory holds nothing. Anything
    else must be under the lock: a `.git` that is a link back to this
    registration in ``repository``, or none (a removal raced by the checkout,
    as before this fix); nothing but paths of the commit's tree, each of the
    kind the tree has there (a file, a link, a directory); and an index, found
    through the link or, without one, through the registration, that stages
    nothing but deletions. Contents are not compared: the add may have stopped
    mid-file. A checkout that is not locked finished, and whatever makes it
    unfinished now was done after it.
    """
    foreign = foreign_registration(registration, commit)
    if foreign is not None:
        return foreign
    path = Path(worktree)
    if not os.path.lexists(path) or path.is_symlink():
        return None
    if not path.is_dir():
        return "it is not a directory"
    try:
        empty = next(os.scandir(path), None) is None
    except OSError as exc:
        return f"it cannot be read ({exc.strerror or exc})"
    if empty:
        return None
    if registration is None or registration.get("locked") != "initializing":
        return "it holds files, and no add that never finished (git's `initializing` lock) names it"
    link = path / ".git"
    if os.path.lexists(link) and (link.is_symlink() or not link.is_file()):
        return "its .git is not a worktree link"
    common = _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout_s=timeout_s)
    admin = _admin_directory(common, path, link)
    if admin is None:
        return ("its .git names another repository or worktree" if os.path.lexists(link)
                else "no registration of the job's repository can be told apart as its own")
    kinds: dict[str, str] = {}
    for record in _git(repository, "ls-tree", "-r", "-t", "-z", "--full-tree", commit,
                       timeout_s=timeout_s, raw=True).split("\0"):
        if record:
            meta, _, name = record.partition("\t")
            mode, kind = meta.split(" ")[:2]
            kinds[name] = "file" if kind == "blob" and mode != "120000" else "link" if kind == "blob" else "directory"
    unreadable: list[OSError] = []
    for directory, dirnames, filenames in os.walk(path, onerror=unreadable.append):
        base = os.path.relpath(directory, path)
        for name in [*dirnames, *filenames]:
            relative = name if base == "." else os.path.join(base, name)
            if relative == ".git":
                continue
            if relative not in kinds:
                return f"it holds {relative}, which {commit} does not"
            try:
                there = _kind(os.lstat(os.path.join(directory, name)).st_mode)
            except OSError as exc:
                return f"part of it cannot be read ({exc.filename})"
            if there != kinds[relative]:
                return f"it holds {relative} as a {there}, where {commit} has a {kinds[relative]}"
        dirnames[:] = [name for name in dirnames if not (base == "." and name == ".git")
                       and not os.path.islink(os.path.join(directory, name))]
    if unreadable:
        return f"part of it cannot be read ({unreadable[0].filename})"
    if os.path.exists(os.path.join(admin, "index")):
        staged = _git(admin, "diff-index", "--cached", "-z", "--name-status", "--no-renames", commit, "--",
                      env={**os.environ, "GIT_DIR": admin}, optional=True, timeout_s=timeout_s, raw=True)
        if staged is None:
            return "its index cannot be read"
        fields = staged.split("\0")
        for status, name in zip(fields[::2], fields[1::2]):
            if status != "D":
                return f"its index stages {name}"
    return None


def working_tree(workdir: str | Path, baseline_commit: str, *,
                 timeout_s: float | None = None) -> str:
    """Snapshot tracked and untracked files without changing the real index."""
    gitdir = _git(workdir, "rev-parse", "--absolute-git-dir", timeout_s=timeout_s)
    with tempfile.TemporaryDirectory(prefix="subfleet-salvage-", dir=gitdir) as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(workdir, "read-tree", baseline_commit, env=env, timeout_s=timeout_s)
        _git(workdir, "add", "-A", env=env, timeout_s=timeout_s)
        return _git(workdir, "write-tree", env=env, timeout_s=timeout_s)


def validate_writable_workdir(workdir: str | Path, *, timeout_s: float | None = None) -> None:
    """Refuse writable admission on main/master while permitting private refs."""
    branch = git_branch(workdir, timeout_s=timeout_s)
    if branch in {"main", "master"}:
        raise AdapterError(f"writable job refused on {branch}", code=7,
                           fix="Check out a task branch before submitting a writable job.")


@dataclass(frozen=True)
class SalvageResult:
    ref: str
    commit: str
    tree: str
    baseline: str


def _stamp(timestamp: str | datetime | None) -> str:
    if timestamp is None:
        value = datetime.now(UTC)
    elif isinstance(timestamp, str):
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    else:
        value = timestamp
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def salvage(workdir: str | Path, baseline_commit: str, seq: int, *, writable: bool = True,
            state: str = "finalizing", timestamp: str | datetime | None = None,
            baseline_tree: str | None = None,
            timeout_s: float | None = None) -> SalvageResult | None:
    """C-13.1: commit a differing working tree beneath a private salvage ref.

    ``baseline_commit`` is the HEAD recorded at reservation and always the
    snapshot's parent. ``baseline_tree`` is the reservation's working tree,
    including any pre-existing dirty files; older callers default to HEAD. Supply
    the recorded attempt timestamp to make a finalization replay idempotent.
    Private refs are permitted even when the current branch is main (C-13.2).
    ``timeout_s`` caps each git call (`git_timeout_s`).
    """
    if not writable:
        return None
    if state not in {"finalizing", "lost", "kill"}:
        raise ValueError("salvage requires finalizing, lost or kill state")
    if seq < 1:
        raise ValueError("attempt sequence must be positive")
    baseline = _git(workdir, "rev-parse", "--verify", f"{baseline_commit}^{{commit}}", timeout_s=timeout_s)
    baseline_tree = _git(workdir, "rev-parse", "--verify", f"{baseline_tree or baseline}^{{tree}}", timeout_s=timeout_s)
    branch = re.sub(r"[^A-Za-z0-9_-]+", "-", git_branch(workdir, timeout_s=timeout_s) or "detached").strip("-") or "detached"
    ref = f"refs/subfleet-salvage/{branch}-{_stamp(timestamp)}-a{seq}"
    # A private temporary directory avoids index-name races and never points
    # git at the user's real index, including in linked worktrees.
    tree = working_tree(workdir, baseline, timeout_s=timeout_s)
    if tree == baseline_tree:
        return None
    previous = _git(workdir, "rev-parse", "--verify", ref, optional=True, timeout_s=timeout_s)
    if previous:
        previous_tree = _git(workdir, "rev-parse", f"{previous}^{{tree}}", timeout_s=timeout_s)
        previous_parent = _git(workdir, "rev-parse", f"{previous}^", optional=True, timeout_s=timeout_s)
        if previous_tree == tree and previous_parent == baseline:
            return SalvageResult(ref, previous, tree, baseline)
        # Never overwrite a previous snapshot with different bytes.
        ref = f"{ref}-{tree[:12]}"
    commit = _git(workdir, "-c", "user.name=subfleet", "-c", "user.email=subfleet@localhost",
                  "commit-tree", tree, "-p", baseline, "-m", f"subfleet salvage attempt a{seq}", timeout_s=timeout_s)
    existing = _git(workdir, "rev-parse", "--verify", ref, optional=True, timeout_s=timeout_s)
    if existing:
        if (_git(workdir, "rev-parse", f"{existing}^{{tree}}", timeout_s=timeout_s) == tree
                and _git(workdir, "rev-parse", f"{existing}^", timeout_s=timeout_s) == baseline):
            return SalvageResult(ref, existing, tree, baseline)
        raise SalvageError("salvage reference already names a different snapshot")
    _git(workdir, "update-ref", ref, commit, "0" * 40, timeout_s=timeout_s)
    return SalvageResult(ref, commit, tree, baseline)
