"""Read-only proof that deleting a job cannot delete its salvage (C-8.4)."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
import stat
import tempfile
import threading
from typing import Any

from .contracts import attempt_dir
from .retention import _Interrupted, _checkpoint, _git
from .sessions.transcripts import read_regular
from .store import Store


# C-8.4: only these durable, shared namespaces can authorize destruction.
ALLOWED_REF_PREFIXES = ("refs/heads/", "refs/tags/", "refs/subfleet-salvage/")
REGENERABLE_CACHE_DIRECTORIES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis",
})


def _regenerable_ignored(path: str) -> bool:
    """Allow only the ignored directory's own name, or a .DS_Store file."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return ((path.endswith("/") and name in REGENERABLE_CACHE_DIRECTORIES)
            or (not path.endswith("/") and name == ".DS_Store"))


def _directory_signature(path: Path) -> tuple[int, int, int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns


@dataclass
class _NestedGitScan:
    identity: tuple[int, int]
    pending: list[Path]
    inspected: list[tuple[Path, tuple[int, int, int, int]]] = field(default_factory=list)


# A partial traversal is work saved, never cached permission. Every directory
# already inspected is revalidated before success; successful/failed proofs
# discard their traversal. The bound also prevents abandoned jobs accumulating.
_NESTED_GIT_SCANS: OrderedDict[Path, _NestedGitScan] = OrderedDict()


def _prove_no_nested_git(worktree: Path, cancel, deadline, *,
                         on_progress: Callable[[], None] = lambda: None) -> list[tuple[Path, tuple]]:
    worktree = worktree.resolve()
    identity = _directory_signature(worktree)[:2]
    scan = _NESTED_GIT_SCANS.get(worktree)
    if scan is None or scan.identity != identity:
        scan = _NestedGitScan(identity, [worktree])
        _NESTED_GIT_SCANS[worktree] = scan
    _NESTED_GIT_SCANS.move_to_end(worktree)
    while len(_NESTED_GIT_SCANS) > 32:
        _NESTED_GIT_SCANS.popitem(last=False)
    try:
        while scan.pending:
            _checkpoint(cancel, deadline)
            directory = scan.pending[-1]
            before = _directory_signature(directory)
            children = []
            names = set()
            # Close the descriptor before a checkpoint can suspend the scan.
            with os.scandir(directory) as entries:
                for entry in entries:
                    names.add(entry.name.casefold())
                    if entry.name == ".gitattributes":
                        _attributes_safe(Path(entry.path))
                    if entry.name.casefold() == ".git":
                        if directory != worktree or entry.name != ".git":
                            nested = Path(entry.path).relative_to(worktree)
                            raise ValueError(f"condition 6: nested Git metadata is not preserved by salvage: {str(nested)!r}")
                    elif entry.is_dir(follow_symlinks=False):
                        children.append(Path(entry.path))
            if directory != worktree and (directory.name.casefold().endswith(".git")
                                          or {"head", "objects"} <= names):
                raise ValueError(f"condition 6: nested bare Git repository is not preserved: {directory.relative_to(worktree)!s}")
            if _directory_signature(directory) != before:
                raise ValueError("worktree directory changed during nested Git proof")
            scan.pending.pop()
            scan.pending.extend(children)
            scan.inspected.append((directory, before))
            on_progress()
        for directory, signature in scan.inspected:
            _checkpoint(cancel, deadline)
            if _directory_signature(directory) != signature:
                raise ValueError("worktree directory changed during nested Git proof")
    except _Interrupted:
        raise
    except BaseException:
        _NESTED_GIT_SCANS.pop(worktree, None)
        raise
    else:
        _NESTED_GIT_SCANS.pop(worktree, None)
    return scan.inspected


def _attributes_safe(path: Path) -> None:
    """Reject conversion definitions, including macros and unused patterns."""
    if path == Path("/dev/null") or (not path.exists() and not path.is_symlink()):
        return
    _attributes_content_safe(read_regular(path, 16 * 1024 * 1024), path)


def _attributes_content_safe(data: bytes, path: Path) -> None:
    for line in data.splitlines():
        if line.lstrip().startswith(b"#"):
            continue
        if re.search(rb"(?:^|\s)[!+-]?(?:filter|text|eol|crlf|working-tree-encoding|ident)(?:=|\s|$)", line):
            raise ValueError(f"condition 4: conversion attributes in {path}")


def _raw_blob(path: Path, mode: str, algorithm: str, cancel, deadline) -> str:
    """Hash Git's blob header and raw bytes, never invoking attribute filters."""
    from .sessions.transcripts import open_regular
    metadata = path.lstat()
    digest = hashlib.new(algorithm)
    if mode == "120000" and stat.S_ISLNK(metadata.st_mode):
        content = os.fsencode(os.readlink(path))
        digest.update(f"blob {len(content)}\0".encode())
        digest.update(content)
    elif mode in ("100644", "100755") and stat.S_ISREG(metadata.st_mode):
        actual_mode = "100755" if metadata.st_mode & 0o111 else "100644"
        if actual_mode != mode:
            raise ValueError(f"condition 4: tracked file mode differs from HEAD: {path}")
        with open_regular(path) as stream:
            before = os.fstat(stream.fileno())
            digest.update(f"blob {before.st_size}\0".encode())
            while chunk := stream.read(1024 * 1024):
                _checkpoint(cancel, deadline)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("condition 4: tracked file changed while hashing")
    else:
        raise ValueError(f"condition 4: unsupported tracked file type: {path}")
    return digest.hexdigest()


def prove_worktree_preserved(job: dict[str, Any], state_root: Path,
                             salvage_artifacts: list[dict[str, Any]], *,
                             cancel: threading.Event | None = None,
                             deadline: float | None = None,
                             on_progress: Callable[[], None] = lambda: None,
                             worktree: Path | None = None, recovering: bool = False) -> str | None:
    """Conditions 2–6, checked independently before and after atomic retirement.

    Only an unmarked legacy lease permits missing tracked files. It never
    permits changed surviving bytes, a changed index, or an unrecorded HEAD.
    Temporary indexes live in the system temp directory; repositories stay
    read-only, including when this validator is used by the live survey.
    """
    worktree = Path(worktree if worktree is not None else job["worktree"])
    _checkpoint(cancel, deadline)
    if worktree.is_symlink():
        raise ValueError("condition 6: worktree is a symlink")
    if not worktree.exists():
        return None
    worktree = worktree.resolve()
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_ATTR_NOSYSTEM": "1"}
    # Inherited Git overrides must not redirect a proof away from this tree.
    for name in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(name, None)

    def git(repository: Path, *args: str, **kwargs) -> str:
        explicit_tree = ("--work-tree=" + str(worktree),) if repository == worktree else ()
        return _git(repository, "--no-replace-objects", "-c", "core.attributesFile=/dev/null",
                    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                    "-c", "core.hooksPath=/dev/null", "-c", "core.splitIndex=false",
                    "-c", "core.sparseCheckout=false", *explicit_tree,
                    *args, cancel=cancel, deadline=deadline, env=kwargs.get("env", env))

    common = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    removed = (worktree, (Path(state_root) / "jobs" / job["job_id"]).resolve())
    if any(common.is_relative_to(path) for path in removed):
        raise ValueError("condition 2: worktree Git object database is inside a directory being removed")
    try:
        caller = Path(git(Path(job["workdir"]), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    except OSError as exc:
        raise ValueError("condition 2: caller repository is unavailable") from exc
    if caller != common:
        raise ValueError("condition 2: worktree is not in the caller repository")
    head = git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
    baseline = head == job.get("workdir_head")
    recorded_salvage = any(a.get("path", "").startswith("refs/subfleet-salvage/")
                           and SalvageReachability._matches(head, a) for a in salvage_artifacts)
    if not baseline and not recorded_salvage:
        raise ValueError("condition 2: HEAD is neither the recorded baseline nor recorded salvage commit")
    prefixes = ("refs/heads/", "refs/tags/") + (("refs/subfleet-salvage/",) if recorded_salvage else ())
    refs = git(common, "--git-dir=" + str(common), "for-each-ref", "--format=%(refname)", "--contains=" + head)
    if not any(ref.startswith(prefixes) for ref in refs.splitlines()):
        raise ValueError("condition 2: worktree HEAD is not preserved by an allowed named ref (baseline included)")

    admin = Path(git(worktree, "rev-parse", "--absolute-git-dir"))
    if admin.is_symlink() or admin.parent.resolve() != common / "worktrees":
        raise ValueError("condition 6: not a linked worktree registration")
    def prove_admin_clean():
        for entry in admin.iterdir():
            name = entry.name.casefold()
            if (name.startswith(("bisect", "rebase", "merge", "stash"))
                    or name in {"sequencer", "cherry_pick_head", "revert_head", "auto_merge", "squash_msg", "locked", "index.lock"}):
                raise ValueError(f"condition 6: Git operation state in admin directory: {entry.name}")
        if git(worktree, "for-each-ref", "--format=%(refname)", "refs/worktree/", "refs/bisect/", "refs/rewritten/"):
            raise ValueError("condition 6: private worktree refs are not preserved")
        for refs_path in (admin / "refs", admin / "logs" / "refs"):
            if refs_path.exists() and any(refs_path.rglob("*")):
                raise ValueError("condition 6: private refs or stash state in admin directory")

    prove_admin_clean()

    # Compare the actual index's full entries with a fresh HEAD index. Neither
    # skip-worktree flags nor stat caches take part in this comparison.
    actual = git(worktree, "ls-files", "--stage", "-z")
    with tempfile.TemporaryDirectory(prefix="retention-index-") as temporary:
        fresh_env = {**env, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(worktree, "-c", "core.sparseCheckout=false", "read-tree", head, env=fresh_env)
        expected = git(worktree, "ls-files", "--stage", "-z", env=fresh_env)
    if actual != expected:
        raise ValueError("condition 3: staged or unmerged index content differs from HEAD")

    # Read attribute sources before disabling them for all Git inspection.
    config = _git(worktree, "--no-replace-objects", "config", "--null", "--list",
                  cancel=cancel, deadline=deadline, env=env)
    for record in config.split("\0"):
        key, _, value = record.partition("\n")
        if key == "core.autocrlf" and value.lower() not in ("false", "no", "off", "0", ""):
            raise ValueError("condition 4: repository enables eol conversion (core.autocrlf)")
        if key == "core.attributesfile":
            attr_path = Path(os.path.expanduser(value))
            _attributes_safe(attr_path if attr_path.is_absolute() else worktree / attr_path)
    for attr_path in (common / "info/attributes", admin / "info/attributes"):
        _attributes_safe(attr_path)

    directories = _prove_no_nested_git(worktree, cancel, deadline, on_progress=on_progress)
    ignored = git(worktree, "ls-files", "-t", "-z", "-o", "-i", "--exclude-standard", "--directory")
    for entry in ignored.split("\0"):
        _checkpoint(cancel, deadline)
        if entry and (not entry.startswith("? ") or not _regenerable_ignored(entry[2:])):
            raise ValueError(f"condition 5: ignored worktree entry is not a regenerable cache: {entry[2:]!r}")
        if entry:
            path = worktree / entry[2:].rstrip("/")
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) if entry.endswith("/") else stat.S_ISREG(mode)):
                raise ValueError(f"condition 5: ignored cache has an unsafe file type: {entry[2:]!r}")
    if git(worktree, "ls-files", "-t", "-z", "-o", "--exclude-standard"):
        raise ValueError("condition 5: untracked files are not preserved")
    hashed_files = []
    algorithm = git(worktree, "rev-parse", "--show-object-format")
    for entry in expected.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        path = worktree / name
        _checkpoint(cancel, deadline)
        if mode == "160000":
            raise ValueError("condition 6: nested repository gitlink is not reproducible")
        if Path(name).name == ".gitattributes":
            # Legacy recovery may waive a missing tracked file, never a
            # conversion definition that still exists in the recorded tree.
            _attributes_content_safe(git(worktree, "show", head + ":" + name).encode("utf-8", "surrogateescape"), path)
        try:
            before = path.lstat()
            current = _raw_blob(path, mode, algorithm, cancel, deadline)
        except FileNotFoundError:
            if recovering:
                continue
            raise ValueError(f"condition 4: tracked file missing: {name!r}") from None
        if current != blob:
            raise ValueError(f"condition 4: raw tracked bytes are not preserved by HEAD: {name!r}")
        hashed_files.append((path, (before.st_dev, before.st_ino, before.st_mode,
                                    before.st_size, before.st_mtime_ns, before.st_ctime_ns)))
        on_progress()
    # Catch writes to early files and new output added while later files hash.
    for path, before in hashed_files:
        _checkpoint(cancel, deadline)
        after = path.lstat()
        if before != (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                      after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("condition 4: tracked file changed during proof")
    for directory, signature in directories:
        if _directory_signature(directory) != signature:
            raise ValueError("condition 5: directory contents changed during proof")
    if git(worktree, "ls-files", "--stage", "-z") != actual:
        raise ValueError("condition 3: index changed during proof")
    if git(worktree, "rev-parse", "--verify", "HEAD^{commit}") != head:
        raise ValueError("condition 2: HEAD changed during proof")
    prove_admin_clean()
    return str(common)


class SalvageReachability:
    """Prove the recorded commit is held by a shared, named repository ref.

    A shared salvage ref is sufficient: removing a linked worktree removes
    neither the common object database nor its refs. Detached HEAD, reflogs,
    and objects with no named ref are insufficient. Every call makes a fresh
    proof outside store transactions; failures leave the artifact pinned.
    """

    def __init__(self, store: Store, state_root: str | Path, *,
                 cancel: threading.Event | None = None, deadline: float | None = None):
        self.store = store
        self.root = Path(state_root)
        self.cancel = cancel
        self.deadline = deadline

    def _git(self, repository: Path, *args: str) -> str:
        return _git(repository, "--no-replace-objects", *args,
                    cancel=self.cancel, deadline=self.deadline)

    @staticmethod
    def _matches(commit: Any, artifact: dict[str, Any]) -> bool:
        return (isinstance(commit, str)
                and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is not None
                and hashlib.sha256(commit.encode()).hexdigest() == artifact.get("sha256"))

    def _receipt_commit(self, job: dict[str, Any], artifact: dict[str, Any]) -> str | None:
        attempt = self.store.one("SELECT seq FROM attempts WHERE attempt_id=? AND job_id=?",
                                 (artifact["attempt_id"], job["job_id"]))
        if attempt is None:
            return None
        directory = self.root / "jobs" / job["job_id"]
        receipt = attempt_dir(self.root, job["job_id"], attempt["seq"]) / "salvage.json"
        # Do not follow a swapped receipt or attempt directory outside this job.
        if directory.resolve() not in receipt.resolve().parents:
            return None
        _checkpoint(self.cancel, self.deadline)
        value = json.loads(read_regular(receipt, 64 * 1024))
        _checkpoint(self.cancel, self.deadline)
        result = value.get("result") if isinstance(value, dict) else None
        if not isinstance(result, dict):
            return None
        if (result.get("ref") or result.get("ref_name")) != artifact["path"]:
            return None
        commit = result.get("commit") or result.get("commit_sha")
        return commit if self._matches(commit, artifact) else None

    def __call__(self, artifact: dict[str, Any]) -> bool:
        _checkpoint(self.cancel, self.deadline)
        if artifact.get("role") != "salvage" or not artifact.get("path", "").startswith("refs/subfleet-salvage/"):
            return False
        job = self.store.one("SELECT j.* FROM jobs j JOIN attempts a USING(job_id) WHERE a.attempt_id=?",
                             (artifact["attempt_id"],))
        if job is None or Path(job["job_id"]).name != job["job_id"] or job["job_id"] in (".", ".."):
            return False
        try:
            # Prefer the caller's repository; a removed caller worktree can
            # still be proved through the job's linked worktree when it exists.
            common = None
            for source in dict.fromkeys((job.get("workdir"), job.get("worktree"))):
                if not source:
                    continue
                try:
                    common = Path(self._git(Path(source), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
                    break
                except (OSError, subprocess.SubprocessError):
                    _checkpoint(self.cancel, self.deadline)
            if common is None:
                return False
            removed = [self.root / "jobs" / job["job_id"]]
            if not job.get("in_place") and job.get("worktree"):
                removed.append(Path(job["worktree"]))
            if any(common.is_relative_to(path.resolve()) for path in removed):
                return False

            # Resolve in the common Git directory, never in a per-worktree
            # namespace that would disappear with the allocated directory.
            gitdir = "--git-dir=" + str(common)
            try:
                commit = self._git(common, gitdir, "rev-parse", "--verify", artifact["path"] + "^{commit}")
                if self._matches(commit, artifact):
                    return True
            except (OSError, subprocess.SubprocessError):
                _checkpoint(self.cancel, self.deadline)

            # A renamed/moved salvage ref can still be held by a branch or
            # another shared ref. The receipt recovers the ID, but only a
            # digest match plus named reachability authorizes retention.
            commit = self._receipt_commit(job, artifact)
            if commit is None:
                return False
            refs = self._git(common, gitdir, "for-each-ref", "--format=%(refname)", "--contains=" + commit)
            return any(ref.startswith(ALLOWED_REF_PREFIXES) for ref in refs.splitlines())
        except (OSError, ValueError, subprocess.SubprocessError):
            _checkpoint(self.cancel, self.deadline)
            return False
