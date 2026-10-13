"""Private git snapshots which leave HEAD, the index and worktree alone."""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
         optional: bool = False, timeout_s: float | None = None) -> str | None:
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
    return result.stdout.strip()


def _git_bytes(workdir: str | Path, *args: str, env: dict[str, str] | None = None,
               stdin: bytes | None = None, timeout_s: float | None = None) -> bytes | None:
    """``_git`` for byte streams (paths need not be UTF-8); None on a non-zero
    exit. Timeouts and transient OS errors raise exactly as in ``_git``."""
    cap = git_timeout_s(timeout_s)
    try:
        result = subprocess.run(["git", "-C", str(workdir), *args], env=env, input=stdin,
                                capture_output=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        raise SalvageError(f"git {args[0]} timed out after {cap:g} s", transient=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        if transient_os_error(exc):
            raise SalvageError(f"git {args[0]} could not run: {exc}", transient=True) from exc
        return None
    return None if result.returncode else result.stdout


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


def working_tree(workdir: str | Path, baseline_commit: str, *,
                 timeout_s: float | None = None) -> str:
    """Snapshot tracked and untracked files without changing the real index.

    The temporary index holds ``baseline_commit`` before ``add -A`` records
    the worktree over it. It is seeded from a copy of the real index and
    read with ``read-tree -m``: the entries are the baseline's either way,
    but git keeps the real index's stat data for every path whose content
    matches, so ``add -A`` hashes only the files that changed. Read into an
    empty index, the baseline has no stat data and every tracked file is
    hashed again, which on a large checkout outlasts the preparation cap
    (C-6.8). The copy's assume-unchanged and skip-worktree bits, which
    ``read-tree -m`` keeps, are cleared before ``add -A``: it skips such paths, and the empty-index read has no such
    bits, so the two reads give the same tree. When the real index cannot
    seed it (there is none yet, it has unmerged entries, git cannot read the
    copy or clear its bits), the baseline is read into an empty index as
    before.

    Trusting stat data is git's own model (``git status`` does the same): a
    file rewritten with the same size, mtime and inode is read as unchanged,
    where the empty-index read would hash it.

    A sparse checkout (``core.sparseCheckout`` is on for this worktree) keeps
    the files its patterns leave out off disk, their index entries marked
    skip-worktree. git's ``add`` will not touch a path outside the patterns:
    ``add -A`` exits 1 on an untracked file there, which failed the snapshot
    and with it the job's preparation (2026-10-09, a review's scratch file in
    a sparse worktree of chief-of-staff, whose 330,000 state files a full
    checkout writes as 15 GB), and it passes over a tracked file the job wrote
    there without a word, so the snapshot lost that work. In a sparse checkout
    the worktree is therefore recorded with ``add -A --sparse``, which reads
    every path. On its own that is worse: with the bits cleared, a file the
    patterns keep off disk is a deleted one, and the snapshot of a worktree
    nobody edited dropped every such file. So each real index entry that is
    skip-worktree and absent from disk (`_off_disk`) is not a deletion: git
    reports it unchanged, with the entry's content, and so does the snapshot.
    The temporary index takes those entries as the real index holds them
    (`_sparse_base`), with their bits set, which ``add -A --sparse`` leaves
    alone; every other bit is cleared as above, so a file written outside the
    patterns is read from disk. Both reads, seeded and empty, take the same
    entries, so they still agree, and a sparse worktree nobody changed
    snapshots to its baseline.

    A file the patterns keep off disk is recorded as the real index holds it
    and is not read, so two things a full checkout records are not recorded
    here, and ``git status`` reports neither: its removal (written and then
    removed, or gone with its directory, as when a repository is cloned where
    the directory was; a file or link written there does replace it, and
    ``git rm --sparse`` removes the entry, which is recorded), and a change of
    attributes or filters that a full checkout would apply to it on disk
    (review of the first version).

    The temporary index of a sparse checkout is always a full one
    (`_full_index`), whatever the real index is. A checkout that is not sparse
    is read exactly as before. Observed with git 2.55; ``add --sparse`` is
    git 2.34's.
    """
    gitdir = _git(workdir, "rev-parse", "--absolute-git-dir", timeout_s=timeout_s)
    sparse = sparse_checkout(workdir, timeout_s=timeout_s)
    with tempfile.TemporaryDirectory(prefix="subfleet-salvage-", dir=gitdir) as temporary:
        index = Path(temporary) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        base, keep = baseline_commit, {}
        if sparse:
            env = _full_index(env)
            keep = _off_disk(workdir, Path(temporary) / "listed", timeout_s=timeout_s)
            base = _sparse_base(workdir, env, Path(temporary) / "base", baseline_commit, keep,
                                timeout_s=timeout_s)
        seeded = (_seed_index(workdir, index, timeout_s=timeout_s)
                  and _git(workdir, "read-tree", "-m", base, env=env,
                           optional=True, timeout_s=timeout_s) is not None
                  and _clear_skip_bits(workdir, env, keep=keep, timeout_s=timeout_s))
        if not seeded:
            index.unlink(missing_ok=True)
            _git(workdir, "read-tree", base, env=env, timeout_s=timeout_s)
            if keep and not _clear_skip_bits(workdir, env, keep=keep, timeout_s=timeout_s):
                # Without their bits `add -A --sparse` records each as deleted.
                raise SalvageError("could not mark the files the sparse checkout keeps off disk")
        _git(workdir, "add", "-A", *(("--sparse",) if sparse else ()), env=env, timeout_s=timeout_s)
        return _git(workdir, "write-tree", env=env, timeout_s=timeout_s)


def sparse_checkout(workdir: str | Path, *, timeout_s: float | None = None) -> bool:
    """Whether ``core.sparseCheckout`` is on for this worktree (cone patterns or not).

    An unset key is ``false`` (``--default``), so any other failure to read it
    raises: read as "not sparse", it took the snapshot that loses a file a job
    wrote outside the patterns (review of the first version).
    """
    return _git(workdir, "config", "--type=bool", "--default=false", "--get",
                "core.sparseCheckout", timeout_s=timeout_s) == "true"


def _full_index(env: dict[str, str]) -> dict[str, str]:
    """``env`` with ``index.sparse`` off for the git it runs, so the index that
    git writes names every file.

    A sparse index (``git sparse-checkout set --sparse-index``) holds one
    directory entry in place of the files under a directory outside the cone,
    and the temporary index, a copy of it, did too. Writing a file entry
    under such a directory left the directory entry beside it, and
    ``write-tree`` wrote a tree naming that directory twice, which ``git
    fsck`` calls ``duplicateEntries`` (git 2.55; found by the property that a
    sparse worktree nobody edited snapshots to its baseline, in a repository
    whose one file is outside the cone). With the setting off, git expands
    such an index as it reads it and writes a full one. The real index is
    only read, and stays as it is.

    The setting goes last in ``GIT_CONFIG_PARAMETERS``, where ``git -c`` puts
    its own: git takes the last value given there over any other source. A
    value inherited there, or in ``GIT_CONFIG_COUNT``, cannot turn it back on
    (review of the first version: ``GIT_CONFIG_PARAMETERS="'index.sparse=true'"``
    did, and the snapshot was the malformed tree again).
    """
    inherited = env.get("GIT_CONFIG_PARAMETERS", "").strip()
    return {**env, "GIT_CONFIG_PARAMETERS": f"{inherited} 'index.sparse=false'".lstrip()}


def _records(data: bytes):
    """The NUL-terminated records of a git listing, one at a time: chief-of-staff's
    index lists 332,000, and a list of them all doubles what the listing holds."""
    start = 0
    while start < len(data):
        end = data.find(b"\0", start)
        if end == -1:
            end = len(data)
        yield data[start:end]
        start = end + 1


def _off_disk(workdir: str | Path, copy: Path, *,
              timeout_s: float | None = None) -> dict[bytes, bytes]:
    """The real index entries a sparse checkout keeps off disk, each path (from
    the top level) to its ``<mode> <object>``, in index order: marked
    skip-worktree (``S``, or ``s`` when also assume-unchanged), at stage 0, the
    file absent. git 2.55 lists an unmerged entry as ``M`` and will not mark
    one skip-worktree, so the stage only guards against an index git did not
    write.

    The entries are listed from ``copy``, a private copy of the real index
    (`_copy_index`), so one read of the real index decides them: an index
    removed or replaced while they were listed was read as one with no entries,
    and every file the patterns leave out was recorded as deleted (review of
    the first version). Raises when there is no index to copy, or the copy
    cannot be listed, for the same reason.
    """
    top = _git_bytes(workdir, "rev-parse", "--show-toplevel", timeout_s=timeout_s)
    top = top[:-1] if top and top.endswith(b"\n") else top
    if not top:
        raise SalvageError("could not find the sparse checkout's top level")
    if not _copy_index(workdir, copy, timeout_s=timeout_s):
        # With no index, git itself reads every file off disk as deleted; the
        # snapshot refuses rather than record that for the files left out.
        raise SalvageError("the sparse checkout has no index to say which files it keeps off disk")
    try:
        # From the top level, as in `_clear_skip_bits`. Without `--sparse`, ls-files
        # lists a sparse index's directory entries as the files they stand for.
        listed = _git_bytes(os.fsdecode(top), "ls-files", "-s", "-v", "-z",
                            env={**os.environ, "GIT_INDEX_FILE": str(copy)}, timeout_s=timeout_s)
    finally:
        copy.unlink(missing_ok=True)
    if listed is None:
        raise SalvageError("could not read the sparse checkout's index")
    off_disk: dict[bytes, bytes] = {}
    for record in _records(listed):
        if record[:2] not in (b"S ", b"s "):
            continue
        meta, _, path = record[2:].partition(b"\t")
        mode, _, rest = meta.partition(b" ")
        oid, _, stage = rest.partition(b" ")
        if (mode and oid and stage == b"0" and path
                and not os.path.lexists(os.path.join(top, path))):
            off_disk[path] = mode + b" " + oid
    return off_disk


def _sparse_base(workdir: str | Path, env: dict[str, str], scratch: Path, baseline: str,
                 off_disk: dict[bytes, bytes], *, timeout_s: float | None = None) -> str:
    """What the temporary index of a sparse checkout reads before ``add -A``:
    ``baseline``, with `_off_disk`'s entries as the real index holds them.

    When every such entry is the baseline's (no job moved the index past it),
    that is ``baseline`` itself. Otherwise it is a tree written from
    ``scratch``, an index built from one stream in index order: the entries,
    and the baseline's other entries, less any whose path is one of theirs,
    lies under one of them, or holds one of them (a file where the real index
    has a directory, or the reverse: the real index is the job's state).

    One stream in index order appends every entry. The first version wrote
    the entries into an index already holding the baseline, and git moved the
    rest of the index for each one it inserted: 21 s for 332,176 paths the
    baseline did not have (review of the first version). Raises when git
    cannot list the baseline or write the tree.
    """
    raw = _git_bytes(workdir, "rev-parse", "--show-toplevel", timeout_s=timeout_s)
    top = os.fsdecode(raw[:-1] if raw and raw.endswith(b"\n") else raw or b"")
    if not top:
        raise SalvageError("could not find the sparse checkout's top level")
    listed = _git_bytes(top, "ls-tree", "-r", "-z", "--full-tree", baseline, timeout_s=timeout_s)
    if listed is None:
        raise SalvageError(f"could not list the baseline {baseline}")

    def entries():
        """The baseline's ``(path, <mode> <object>)``, from ``mode type object\tpath``."""
        for record in _records(listed):
            meta, _, path = record.partition(b"\t")
            mode, _, rest = meta.partition(b" ")
            oid = rest.partition(b" ")[2]
            if mode and oid and path:
                yield path, mode + b" " + oid
    if sum(off_disk.get(path) == meta for path, meta in entries()) == len(off_disk):
        return baseline
    held = {parent for path in off_disk for parent in _parents(path)}

    def kept(path: bytes) -> bool:
        return (path not in off_disk and path not in held
                and not any(parent in off_disk for parent in _parents(path)))
    stream = sorted([entry for entry in entries() if kept(entry[0])] + list(off_disk.items()))
    scratch_env = {**env, "GIT_INDEX_FILE": str(scratch)}
    try:
        if _git_bytes(top, "update-index", "-z", "--index-info", env=scratch_env, timeout_s=timeout_s,
                      stdin=b"".join(meta + b"\t" + path + b"\0" for path, meta in stream)) is None:
            raise SalvageError("could not build the index of the files the sparse checkout keeps off disk")
        return _git(top, "write-tree", env=scratch_env, timeout_s=timeout_s)
    finally:
        scratch.unlink(missing_ok=True)


def _parents(path: bytes):
    """Each directory above ``path``, nearest the top first: ``a``, ``a/b`` for ``a/b/c``."""
    at = path.find(b"/")
    while at != -1:
        yield path[:at]
        at = path.find(b"/", at + 1)


def _seed_index(workdir: str | Path, index: Path, *,
                timeout_s: float | None = None) -> bool:
    """The seed of the temporary index (`working_tree`): `_copy_index`."""
    return _copy_index(workdir, index, timeout_s=timeout_s)


def _copy_index(workdir: str | Path, index: Path, *,
                timeout_s: float | None = None) -> bool:
    """Copy the real index to ``index``; False when there is none to copy.

    The copy keeps the file's mtime: git treats entries at or after the
    index's own mtime as possibly modified and hashes them, and a copy
    stamped now would hide exactly those edits. The bytes and the mtime come
    from one open file, so a concurrent rewrite of the index (git replaces
    it by rename) cannot pair one index's entries with another's mtime.
    """
    real = _git(workdir, "rev-parse", "--git-path", "index", optional=True,
                timeout_s=timeout_s)
    if not real:
        return False
    source = Path(real) if os.path.isabs(real) else Path(workdir) / real
    try:
        with open(source, "rb") as reader, open(index, "wb") as writer:
            stat = os.fstat(reader.fileno())
            shutil.copyfileobj(reader, writer)
        os.utime(index, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        return False
    return True


def _clear_skip_bits(workdir: str | Path, env: dict[str, str], *,
                     keep: Collection[bytes] = frozenset(),
                     timeout_s: float | None = None) -> bool:
    """Clear assume-unchanged and skip-worktree bits in the temporary index,
    except the skip-worktree bits of ``keep``, the entries a sparse checkout
    keeps off disk (`_off_disk`), which it sets where they are not set.

    Flipping a bit hashes nothing. ``update-index`` applies only the first
    of the options it is given, so each gets its own call. False, as for any
    failure, when the index does not hold every entry of ``keep``.
    """
    # From the toplevel: from a subdirectory, ls-files lists only that
    # subdirectory's entries, and add -A still records the whole tree.
    top = _git(workdir, "rev-parse", "--show-toplevel", optional=True, timeout_s=timeout_s)
    if not top:
        return False
    listed = _git_bytes(top, "ls-files", "-v", "-z", env=env, timeout_s=timeout_s)
    if listed is None:
        return False
    assumed: list[bytes] = []
    skipped: list[bytes] = []
    unmarked: list[bytes] = []
    kept = 0
    for record in _records(listed):
        if len(record) < 3:
            continue
        tag, path = record[:1], record[2:]
        if tag.islower():
            assumed.append(path)
        if path in keep:
            kept += 1
            if tag not in (b"S", b"s"):
                unmarked.append(path)
        elif tag in (b"S", b"s"):
            skipped.append(path)
    if kept != len(keep):
        # An entry of `keep` the index does not hold would be read as deleted.
        return False
    for option, paths in (("--no-assume-unchanged", assumed),
                          ("--no-skip-worktree", skipped),
                          ("--skip-worktree", unmarked)):
        if paths and _git_bytes(top, "update-index", option, "-z", "--stdin",
                                stdin=b"\0".join(paths) + b"\0", env=env,
                                timeout_s=timeout_s) is None:
            return False
    return True


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
