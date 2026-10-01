"""Git plumbing for retention by archive (C-8.4, C-13.4, design d635).

Retention preserves a retired worktree's Git state in a bundle it owns, so that
nothing depends on a repository it does not control:

- every object the worktree's admin directory names (HEAD, the index, ORIG_HEAD,
  MERGE_HEAD, FETCH_HEAD, per-worktree refs, rebase and bisect state, every
  reflog entry) and the job's salvage commits become the parents and trees of one
  synthetic *anchor* commit;
- the anchor, its only head, is written to ``commits.bundle`` with every commit
  not reachable from a network remote's remote-tracking refs (a salvage ref is
  not a head: `git bundle` drops a head a remote already holds);
- a tracked file is left out of the byte archive only when its raw bytes hash to
  the blob at the same path in a commit those remote-tracking refs reach, in a
  repository that is not a scratch clone, and the object reads back and hashes
  whole (review findings: Opus 1, Astra 1 and 2).

The only writes to the source repository are new objects (the anchor's trees and
commits) and create-only refs ``refs/subfleet-archive/<job>/<commit>``; none of
it removes or moves anything. Hooks, fsmonitor, auto-gc and replace refs are off
for every call.
"""
from __future__ import annotations

import bisect
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import retention_fs as rfs
from . import retention_qos as rqos

GIT_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
              "-c", "maintenance.auto=false", "-c", "core.untrackedCache=false")
ANCHOR_IDENTITY = {"GIT_AUTHOR_NAME": "subfleet retention", "GIT_AUTHOR_EMAIL": "retention@subfleet.invalid",
                   "GIT_COMMITTER_NAME": "subfleet retention", "GIT_COMMITTER_EMAIL": "retention@subfleet.invalid",
                   "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}
ARCHIVE_NAMESPACE = "refs/subfleet-archive"
LOCK_MARKER = "subfleet retention: "
#: Per-worktree namespaces a linked worktree's admin directory can hold.
WORKTREE_REF_PREFIXES = ("refs/worktree/", "refs/bisect/", "refs/rewritten/")
PSEUDO_REFS = ("HEAD", "ORIG_HEAD", "MERGE_HEAD", "FETCH_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
               "REBASE_HEAD", "AUTO_MERGE", "BISECT_HEAD", "BISECT_EXPECTED_REV")
MAX_PARENTS = 64
TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders")


class GitError(Exception):
    pass


class Cancelled(Exception):
    pass


def environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false")
    if extra:
        env.update(extra)
    return env


def run(args: Iterable[str], *, git_dir: Path | None = None, cwd: Path | None = None,
        stdin: bytes | None = None, timeout: float = 120, cancel: threading.Event | None = None,
        env: dict[str, str] | None = None, ok: Iterable[int] = (0,), stdin_file=None) -> subprocess.CompletedProcess:
    """One git command with a wall cap; cancellation kills it and raises `Cancelled`."""
    argv = ["git", *GIT_CONFIG]
    if git_dir is not None:
        argv.append(f"--git-dir={git_dir}")
    argv.extend(args)
    process = subprocess.Popen(rqos.argv(argv), cwd=cwd, env=environment(env),
                               stdin=stdin_file if stdin_file is not None else (subprocess.PIPE if stdin is not None else subprocess.DEVNULL),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    first = True
    while True:
        try:
            out, err = process.communicate(stdin if first else None,
                                           timeout=max(0.05, min(1.0, deadline - time.monotonic())))
            break
        except subprocess.TimeoutExpired:
            first = False
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                process.kill()
                process.communicate()
                if cancel is not None and cancel.is_set():
                    raise Cancelled(f"git {argv[len(GIT_CONFIG) + 1]} cancelled") from None
                raise GitError(f"git {' '.join(list(args)[:2])} timed out after {timeout:g} s") from None
    result = subprocess.CompletedProcess(argv, process.returncode, out, err)
    if result.returncode not in tuple(ok):
        tail = err.decode("utf-8", "replace").strip()[-400:]
        raise GitError(f"git {' '.join(list(args)[:2])} exited {result.returncode}: {tail}")
    return result


# --- the registration ------------------------------------------------------------

@dataclass(frozen=True)
class Registration:
    """A linked worktree's admin directory and the repository it belongs to."""
    admin: Path
    common: Path
    gitfile: bytes


def _resolve(base: Path, text: str) -> Path:
    path = Path(text)
    return Path(os.path.realpath(path if path.is_absolute() else base / path))


def gitfile_admin(tree: Path) -> tuple[Path | None, bytes, str | None]:
    """(the admin directory the tree's gitfile names, the gitfile, None), or
    (None, b"", why) when the tree has no gitfile naming one."""
    try:
        gitfile = rfs.read_regular(tree / ".git", limit=65536)
    except FileNotFoundError:
        return None, b"", "no-gitfile"
    except (IsADirectoryError, OSError) as exc:
        return None, b"", f"gitfile-unreadable: {exc}"
    text = gitfile.decode("utf-8", "surrogateescape").strip()
    if not text.startswith("gitdir:"):
        return None, b"", "gitfile-malformed"
    return _resolve(tree, text[len("gitdir:"):].strip()), gitfile, None


#: What is left of an admin directory whose files a temporary directory's
#: cleaner deleted while it kept the directories: name -> type.
REMNANT = {"index": "f", "logs": "d"}


def is_remnant(admin: Path) -> bool:
    """Whether `admin` is what is left of a registration, not a registration:
    directly under a `worktrees` directory, a real directory holding nothing
    but an `index` file and a `logs` directory (no `gitdir`, `commondir` or
    `HEAD`, so git can neither use it nor say whose it was). Live: the four
    `mstat6-g*` jobs, whose source clones in /tmp lost every file but those
    (final review of e50716e8, N5)."""
    try:
        if not stat.S_ISDIR(os.lstat(admin).st_mode) or admin.parent.name != "worktrees":
            return False
        for name in os.listdir(admin):
            if REMNANT.get(name) != rfs.kind(os.lstat(admin / name).st_mode):
                return False
    except OSError:
        return False
    return True


def registration(tree: Path) -> tuple[Registration | None, str | None]:
    """(registration, None), or (None, why) when the tree has none we may use.

    The tree's ``.git`` must be a gitdir file naming an admin directory directly
    under ``<common>/worktrees/``, whose ``gitdir`` backlink names this tree:
    we never act on someone else's registration (design 5.2 step 2). An admin
    directory that is only a remnant (`is_remnant`) answers ``admin-remnant``.
    """
    admin, gitfile, why = gitfile_admin(tree)
    if admin is None:
        return None, why
    if not admin.is_dir():
        return None, "admin-missing"
    try:
        backlink = rfs.read_regular(admin / "gitdir", limit=65536).decode("utf-8", "surrogateescape").strip()
        commondir = rfs.read_regular(admin / "commondir", limit=65536).decode("utf-8", "surrogateescape").strip()
    except OSError as exc:
        if isinstance(exc, FileNotFoundError) and is_remnant(admin):
            return None, "admin-remnant"
        return None, f"admin-unreadable: {exc}"
    if _resolve(admin, backlink) != Path(os.path.realpath(tree / ".git")):
        return None, "registration-mismatch"
    common = _resolve(admin, commondir)
    if admin.parent != common / "worktrees":
        return None, "registration-not-direct"
    return Registration(admin, common, gitfile), None


def common_dir(path: Path, timeout: float = 60, cancel: threading.Event | None = None) -> Path | None:
    """The common directory of the repository `path` is in, or None."""
    try:
        out = run(["rev-parse", "--git-common-dir"], cwd=path, timeout=timeout, cancel=cancel).stdout
    except (GitError, OSError):
        return None
    return _resolve(path, out.decode("utf-8", "surrogateescape").strip())


def find_registration(repository: Path, tree: Path, timeout: float = 60,
                      cancel: threading.Event | None = None) -> Registration | None:
    """The registration whose backlink names `tree` when the tree itself is gone."""
    common = common_dir(repository, timeout, cancel)
    return None if common is None else registration_in(common, tree)


def registration_in(common: Path, tree: Path, *, named: bool = False) -> Registration | None:
    """The admin directory directly under ``<common>/worktrees`` whose ``gitdir``
    backlink names `tree`, or None. With `named`, only those named after the
    tree: git names a registration by its tree's basename (with digits added
    when that is taken), and `git worktree move` keeps the name, so a search of
    many repositories reads only the few that can be the tree's."""
    wanted = os.path.realpath(tree / ".git")
    pattern = re.compile(re.escape(tree.name) + r"[0-9]*")
    try:
        admins = sorted((common / "worktrees").iterdir())
    except OSError:
        return None
    for admin in admins:
        if named and not pattern.fullmatch(admin.name):
            continue
        try:
            backlink = rfs.read_regular(admin / "gitdir", limit=65536).decode("utf-8", "surrogateescape").strip()
        except OSError:
            continue
        if os.path.realpath(_resolve(admin, backlink)) == wanted:
            return Registration(Path(os.path.realpath(admin)), common, b"")
    return None


def repository_near(path: Path, timeout: float = 60, cancel: threading.Event | None = None) -> Path | None:
    """For a directory that is gone: the common directory of the repository its
    nearest existing ancestor is in, or, when git cannot use that (a linked
    checkout whose admin directory was removed), the one whose
    ``worktrees/<id>`` the first ``.git`` file above it names. None when
    neither is there. Only a hint: a caller confirms it (a registration's
    backlink, the job's own salvage refs) before acting on it."""
    ancestor = path
    while not os.path.isdir(ancestor):
        if ancestor.parent == ancestor:
            return None
        ancestor = ancestor.parent
    common = common_dir(ancestor, timeout, cancel)
    if common is not None:
        return common
    for candidate in (ancestor, *ancestor.parents):
        if not os.path.lexists(candidate / ".git"):
            continue
        admin, _, _ = gitfile_admin(candidate)
        if admin is not None and admin.parent.name == "worktrees" and os.path.isdir(admin.parent.parent / "objects"):
            return admin.parent.parent
        return None
    return None


def refs_under(common: Path, prefix: str, *, cancel: threading.Event | None = None) -> dict[str, str]:
    """ref -> object id of every ref under `prefix` (one `for-each-ref`)."""
    out = run(["for-each-ref", "--format=%(refname) %(objectname)", prefix], git_dir=common,
              cancel=cancel).stdout.decode("utf-8", "surrogateescape")
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def discard_registration(repository: str | os.PathLike, tree: str | os.PathLike, *,
                         timeout: float = 60) -> Path | None:
    """Remove `tree`'s own registration from `repository`, and nothing else.

    Only the admin directory directly under ``<common>/worktrees`` whose
    ``gitdir`` backlink names ``tree/.git`` is deleted, and only if it is not
    locked (a lock is someone's claim: retention's, or a person's `git worktree
    lock`). A repository-wide `git worktree prune` would also drop every other
    registration whose tree is missing at that moment, with the commits only
    its HEAD and reflogs hold (d635: never run a repository-wide prune). Best
    effort, for callers about to re-add the tree: returns the directory
    removed, or None.
    """
    try:
        out = run(["rev-parse", "--git-common-dir"], cwd=Path(repository), timeout=timeout).stdout
        common = _resolve(Path(repository), out.decode("utf-8", "surrogateescape").strip())
        wanted = os.path.realpath(Path(tree) / ".git")
        admins = sorted((common / "worktrees").iterdir())
    except (GitError, OSError):
        return None
    for admin in admins:
        if admin.is_symlink() or not admin.is_dir():
            continue       # never follow a link out of the repository
        try:
            backlink = rfs.read_regular(admin / "gitdir", limit=65536).decode("utf-8", "surrogateescape").strip()
        except OSError:
            continue
        if os.path.realpath(_resolve(admin, backlink)) != wanted:
            continue
        if os.path.lexists(admin / "locked"):
            return None
        shutil.rmtree(admin, ignore_errors=True)
        return admin
    return None


def object_format(common: Path, cancel: threading.Event | None = None) -> str:
    out = run(["rev-parse", "--show-object-format"], git_dir=common, cancel=cancel).stdout.decode().strip()
    if out not in ("sha1", "sha256"):
        raise GitError(f"unknown object format {out!r}")
    return out


# --- what is held elsewhere --------------------------------------------------------

_SCP = re.compile(r"^[A-Za-z0-9._~-]+@([A-Za-z0-9.-]+):(?!/)")
_URL = re.compile(r"^(?:https?|ssh|git|git\+ssh|ssh\+git)://([^/]+)/", re.IGNORECASE)


def network_url(url: str) -> bool:
    """A remote on another machine: a URL with a host, or scp-like
    ``user@host:path``, whose host is not this one (`localhost`, a loopback
    address, a `.local` name: a clone on the same disk holds nothing
    elsewhere; review of the revision-4 build)."""
    match = _URL.match(url) or _SCP.match(url)
    return match is not None and not rfs.this_machine(match.group(1))


def network_remotes(common: Path, cancel: threading.Event | None = None) -> dict[str, str]:
    out = run(["config", "--get-regexp", r"^remote\..+\.url$"], git_dir=common, ok=(0, 1),
              cancel=cancel).stdout.decode("utf-8", "surrogateescape")
    remotes: dict[str, str] = {}
    for line in out.splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote."):-len(".url")]
        if name and not any(c in name for c in "*?[\\") and network_url(url.strip()):
            remotes[name] = url.strip()
    return remotes


def baseline_held(common: Path, baseline: str | None, held: list[str], *, timeout: float = 600,
                  cancel: threading.Event | None = None) -> bool:
    """Whether a network remote's refs reach the commit a job started from.
    Then its bundle carries only what the job itself added; otherwise it
    carries the shared history too, paid again by every job of the
    repository (no network remote, a remote never fetched or fetched only
    for `gh-pages`, or refs from long ago: review of the revision-4 build)."""
    if not baseline or not held or not classify(common, [baseline], timeout=timeout, cancel=cancel)["commit"]:
        return False
    return held_commit(common, baseline, held, timeout=timeout, cancel=cancel)


def held_arguments(remotes: dict[str, str]) -> list[str]:
    return [f"--glob=refs/remotes/{name}/*" for name in sorted(remotes)]


def held_state(common: Path, remotes: dict[str, str], cancel: threading.Event | None = None) -> str:
    """A digest of every remote-tracking ref of `remotes`, names and values: a
    bundle made against one state has prerequisites the next may no longer
    hold (a branch deleted on the server, then fetched with --prune)."""
    if not remotes:
        return ""
    out = run(["for-each-ref", "--format=%(objectname) %(refname)",
               *[f"refs/remotes/{name}/" for name in sorted(remotes)]], git_dir=common, cancel=cancel).stdout
    return hashlib.sha256(out).hexdigest()


def is_ancestor(git_dir: Path, ancestor: str, descendant: str, *, cancel: threading.Event | None = None) -> bool:
    return run(["merge-base", "--is-ancestor", ancestor, descendant], git_dir=git_dir, ok=(0, 1),
               timeout=600, cancel=cancel).returncode == 0


def held_commit(git_dir: Path, commit: str, held: list[str], *, timeout: float = 600,
                cancel: threading.Event | None = None) -> bool:
    """Whether the held remote-tracking refs reach `commit` (then so do they
    every commit it reaches, and a bundle of it would be empty)."""
    if not held:
        return False
    out = run(["rev-list", "-n", "1", commit, "--not", *held], git_dir=git_dir, timeout=timeout,
              cancel=cancel).stdout
    return not out.strip()


def history_bytes(git_dir: Path, heads: Iterable[str], held: list[str], *, timeout: float = 600,
                  cancel: threading.Event | None = None) -> int:
    """What a bundle of `heads` against `held` would carry, measured as the
    bytes the objects take on disk here (`rev-list --objects --disk-usage`):
    every object reachable from the heads that the held refs do not reach.
    With no network remote, `held` is empty and this is the whole history
    (final review of e50716e8, N1). Heads that are not commits here are
    skipped. An estimate: `git bundle` packs the objects again."""
    wanted = sorted({h for h in heads if h})
    if not wanted:
        return 0
    commits = sorted(classify(git_dir, wanted, timeout=timeout, cancel=cancel)["commit"])
    if not commits:
        return 0
    args = ["rev-list", "--objects", "--disk-usage", *commits]
    if held:
        args += ["--not", *held]
    out = run(args, git_dir=git_dir, timeout=timeout, cancel=cancel).stdout.decode().strip()
    return int(out or 0)


def temp_roots() -> set[str]:
    """Directories whose contents the system or a person may delete at any time."""
    return {os.path.realpath(p) for p in (*TEMP_ROOTS, tempfile.gettempdir(), os.environ.get("TMPDIR") or "/tmp")}


def scratch_reason(common: Path, state_root: Path, remotes: dict[str, str],
                   cancel: threading.Event | None = None) -> str | None:
    """Why a repository cannot be trusted to keep a blob, or None.

    Omission (leaving a tracked file out of the byte archive) is allowed only
    in a repository that is not itself scratch: not under a temporary directory
    or the state root, not named as scratch, with its own complete object store
    (no alternates, not shallow, not a partial clone), and with a network remote.
    When in doubt, the bytes are archived.
    """
    top = common.parent if common.name == ".git" else common
    real = os.path.realpath(top)
    for root in temp_roots():
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return "temporary directory"
    state = os.path.realpath(state_root)
    if real == state or real.startswith(state + "/"):
        return "inside the subfleet state root"
    if any("scratch" in part.lower() or part.lower() in ("tmp", "temp", ".tmp") for part in Path(real).parts):
        return "scratch path"
    if (common / "objects" / "info" / "alternates").exists():
        return "borrows objects (alternates)"
    if (common / "shallow").exists():
        return "shallow clone"
    try:
        partial = run(["config", "--get", "extensions.partialclone"], git_dir=common, ok=(0, 1), cancel=cancel)
        promisor = run(["config", "--get-regexp", r"^remote\..+\.promisor$"], git_dir=common, ok=(0, 1), cancel=cancel)
    except GitError as exc:
        return f"config unreadable: {exc}"
    if partial.stdout.strip() or b"true" in promisor.stdout.lower():
        return "partial clone"
    if not remotes:
        return "no network remote"
    return None


def omission_map(common: Path, heads: Iterable[str], held: list[str], *, timeout: float = 300,
                 cancel: threading.Event | None = None) -> tuple[dict[str, dict[str, int]], list[str]]:
    """path -> {blob id: size} over commits the held refs reach, near `heads`.

    For each head: itself when held, else the boundary commits of its unheld
    history (held parents of unpushed commits). A blob at the same path in one
    of their trees is reachable from a remote-tracking ref.
    """
    if not held:
        return {}, []
    candidates: list[str] = []
    for head in heads:
        if not head:
            continue
        out = run(["rev-list", "--boundary", head, "--not", *held], git_dir=common, timeout=timeout,
                  cancel=cancel).stdout.decode()
        lines = out.split()
        if not lines:
            candidates.append(head)
        candidates.extend(line[1:] for line in lines if line.startswith("-"))
    seen: list[str] = []
    for commit in candidates:
        if commit not in seen:
            seen.append(commit)
    blobs: dict[str, dict[str, int]] = {}
    for commit in seen[:8]:
        out = run(["ls-tree", "-r", "-z", "-l", "--full-tree", commit], git_dir=common, timeout=timeout,
                  cancel=cancel).stdout
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, oid, size = meta.split()
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                continue
            blobs.setdefault(os.fsdecode(path), {})[oid.decode()] = int(size)
    return blobs, seen[:8]


def _fold(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


class Ignored:
    """Which directories of a worktree git says hold only ignored, untracked
    files (d635 disk relief).

    Built from one `ls-files --cached --others --exclude-standard` (every
    tracked path, and every untracked file no ignore rule covers; a nested
    repository is listed as its directory). A directory is *clear* when none
    of those paths is it, inside it, or one of its parents.
    """

    def __init__(self, paths: Iterable[str]):
        # Compared folded: git prints the index's case and precomposed (NFC)
        # names, the walk sees the disk's; APFS matches names regardless of
        # case and normalization. Folding only merges names, so it can make a
        # directory less clear, never more (when in doubt, archive the bytes).
        self.paths = sorted({_fold(p.rstrip("/")) for p in paths if p})
        self.members = set(self.paths)

    @classmethod
    def of(cls, admin: Path, tree: Path, *, timeout: float = 600,
           cancel: threading.Event | None = None) -> Ignored:
        out = run([f"--work-tree={tree}", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                  git_dir=admin, cwd=tree, timeout=timeout, cancel=cancel).stdout
        return cls(os.fsdecode(p) for p in out.split(b"\0") if p)

    def clear(self, rel: str) -> bool:
        rel = _fold(rel)
        if not rel or rel in self.members:
            return False
        parts = rel.split("/")
        if any("/".join(parts[:n]) in self.members for n in range(1, len(parts))):
            return False
        prefix = rel + "/"
        at = bisect.bisect_left(self.paths, prefix)
        return not (at < len(self.paths) and self.paths[at].startswith(prefix))


def blob_id(data: bytes, fmt: str) -> str:
    h = hashlib.new(fmt)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


class ObjectReader:
    """``git cat-file --batch``: whole objects, each hashed here (Astra finding 1).

    Git does not check an object's hash when it reads it, so a loose object
    whose zlib stream is intact but whose bytes are wrong would be trusted by
    ``--batch-check`` and by anchoring. Reading the full object and hashing it
    is the only check that counts.
    """

    def __init__(self, common: Path, fmt: str, cancel: threading.Event | None = None):
        self.fmt = fmt
        self.cancel = cancel
        self.argv = ["git", *GIT_CONFIG, f"--git-dir={common}", "cat-file", "--batch"]
        self.process = self._start()

    def _start(self) -> subprocess.Popen:
        return subprocess.Popen(rqos.argv(self.argv), env=environment(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def verify(self, oid: str, expect_type: str = "blob") -> bool:
        """True only if the whole object reads back and hashes to `oid`. A
        damaged object can make git die mid-stream; that answers False, and the
        next object gets a fresh reader."""
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled("object verification cancelled")
        try:
            return self._verify(oid, expect_type)
        except (OSError, ValueError, GitError):
            self.close()
            self.process = self._start()
            return False

    def _verify(self, oid: str, expect_type: str) -> bool:
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(oid.encode() + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline()
        parts = header.split()
        if len(parts) != 3 or parts[0].decode() != oid:
            if not header:
                raise GitError("cat-file stopped")
            return False          # "<oid> missing"
        kind, size = parts[1].decode(), int(parts[2])
        h = hashlib.new(self.fmt)
        h.update(kind.encode() + b" %d\0" % size)
        remaining = size
        while remaining:
            chunk = self.process.stdout.read(min(remaining, rfs.CHUNK))
            if not chunk:
                raise GitError("cat-file ended early")
            h.update(chunk)
            remaining -= len(chunk)
        if self.process.stdout.read(1) != b"\n":   # the newline after the object
            raise GitError("cat-file stream out of step")
        return kind == expect_type and h.hexdigest() == oid

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=30)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()

    def __enter__(self) -> ObjectReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- objects the admin directory names ------------------------------------------

def _token_pattern(fmt: str) -> re.Pattern[bytes]:
    n = 40 if fmt == "sha1" else 64
    return re.compile(rb"(?<![0-9a-f])[0-9a-f]{%d}(?![0-9a-f])" % n)


def admin_objects(admin: Path, fmt: str, *, timeout: float = 300,
                  cancel: threading.Event | None = None) -> dict[str, set[str]]:
    """Every object id the admin directory names, by type (and those missing).

    Three sources, so neither ref backend nor git's internal file names need to
    be enumerated: every hex token in every file of the directory (HEAD, the
    pseudo-refs, reflogs, rebase, sequencer and bisect state), git's own view of
    the per-worktree refs, HEAD and its reflog (for the reftable backend), and
    the index (``ls-files --stage``; a gitlink's commit lives in the submodule).
    """
    tokens: set[str] = set()
    pattern = _token_pattern(fmt)
    fd = os.open(admin, rfs.O_DIR)
    try:
        for rel, st, parent, name in rfs.walk(fd):
            if not rel or rel == "index" or rel.split("/")[0] == "modules" or not stat.S_ISREG(st.st_mode):
                continue
            if st.st_size > 256 * 1024 * 1024:
                continue
            try:
                handle = os.open(name, rfs.O_FILE, dir_fd=parent)
            except OSError:
                continue
            try:
                data = b"".join(iter(lambda: os.read(handle, rfs.CHUNK), b""))
            finally:
                os.close(handle)
            tokens.update(m.decode() for m in pattern.findall(data))
    finally:
        os.close(fd)
    refs = run(["for-each-ref", "--format=%(objectname)", *WORKTREE_REF_PREFIXES], git_dir=admin, ok=(0, 1),
               timeout=timeout, cancel=cancel).stdout.decode().split()
    tokens.update(refs)
    for name in PSEUDO_REFS:
        out = run(["rev-parse", "--verify", "--quiet", name], git_dir=admin, ok=(0, 1), timeout=timeout,
                  cancel=cancel).stdout.decode().strip()
        if out:
            tokens.add(out)
    reflog = run(["reflog", "show", "--format=%H", "HEAD", "--"], git_dir=admin, ok=(0, 1, 128),
                 timeout=timeout, cancel=cancel).stdout.decode().split()
    tokens.update(reflog)
    index_blobs: set[str] = set()
    if (admin / "index").exists():
        out = run(["ls-files", "--stage", "-z"], git_dir=admin, timeout=timeout, cancel=cancel).stdout
        for record in out.split(b"\0"):
            if not record:
                continue
            meta = record.partition(b"\t")[0].split()
            if len(meta) == 3 and meta[0] != b"160000":
                index_blobs.add(meta[1].decode())
    zero = "0" * (40 if fmt == "sha1" else 64)
    tokens.discard(zero)
    typed = classify(admin, tokens | index_blobs, timeout=timeout, cancel=cancel)
    typed["index"] = {oid for oid in index_blobs if oid in typed["blob"]}
    return typed


def no_objects() -> dict[str, set[str]]:
    """What `admin_objects` answers for a job with no registration."""
    return {"commit": set(), "tree": set(), "blob": set(), "tag": set(), "missing": set(), "index": set()}


def classify(git_dir: Path, oids: Iterable[str], *, timeout: float = 300,
             cancel: threading.Event | None = None) -> dict[str, set[str]]:
    typed: dict[str, set[str]] = {"commit": set(), "tree": set(), "blob": set(), "tag": set(), "missing": set()}
    ordered = sorted(set(oids))
    if not ordered:
        return typed
    out = run(["cat-file", "--batch-check=%(objectname) %(objecttype)"], git_dir=git_dir,
              stdin=("\n".join(ordered) + "\n").encode(), timeout=timeout, cancel=cancel).stdout.decode()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] in typed:
            typed[parts[1]].add(parts[0])
        elif parts and parts[-1] == "missing":
            typed["missing"].add(parts[0])
    return typed


def peel_tags(git_dir: Path, tags: Iterable[str], *, cancel: threading.Event | None = None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"commit": set(), "tree": set(), "blob": set()}
    for tag in sorted(tags):
        target = run(["rev-parse", "--verify", "--quiet", f"{tag}^{{}}"], git_dir=git_dir, ok=(0, 1),
                     cancel=cancel).stdout.decode().strip()
        if target:
            kind = run(["cat-file", "-t", target], git_dir=git_dir, cancel=cancel).stdout.decode().strip()
            if kind in out:
                out[kind].add(target)
    return out


# --- the anchor and the bundle --------------------------------------------------------

def _mktree(git_dir: Path, entries: list[tuple[str, str, str, str]], cancel: threading.Event | None) -> str:
    """entries: (mode, type, oid, name)."""
    body = b"".join(f"{mode} {kind} {oid}\t{name}".encode() + b"\0" for mode, kind, oid, name in sorted(entries, key=lambda e: e[3]))
    return run(["mktree", "-z"], git_dir=git_dir, stdin=body, cancel=cancel).stdout.decode().strip()


def _commit(git_dir: Path, tree: str, parents: list[str], message: str, cancel: threading.Event | None) -> str:
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", message]
    return run(args, git_dir=git_dir, env=ANCHOR_IDENTITY, cancel=cancel).stdout.decode().strip()


def make_anchor(git_dir: Path, job_id: str, objects: dict[str, set[str]], extra_commits: Iterable[str], *,
                cancel: threading.Event | None = None) -> str:
    """One commit that reaches every object in `objects` and every extra commit.

    Its tree holds ``index/`` (every staged blob, named by id), ``trees/`` and
    ``blobs/`` (trees and blobs the admin directory names). Its parents are the
    named commits, reduced to independent tips, 64 at a time through part
    commits. Author, committer and dates are fixed, so the same inputs give the
    same commit and anchoring is idempotent (Opus finding 2).
    """
    commits = sorted(set(objects.get("commit", ())) | {c for c in extra_commits if c})
    peeled = peel_tags(git_dir, objects.get("tag", ()), cancel=cancel)
    commits = sorted(set(commits) | peeled["commit"])
    if 1 < len(commits) <= 4000:
        reduced = run(["merge-base", "--independent", *commits], git_dir=git_dir, timeout=600,
                      cancel=cancel).stdout.decode().split()
        commits = sorted(set(reduced))
    root: list[tuple[str, str, str, str]] = []
    index = sorted(objects.get("index", ()))
    if index:
        root.append(("040000", "tree", _mktree(git_dir, [("100644", "blob", b, b) for b in index], cancel), "index"))
    trees = sorted(set(objects.get("tree", ())) | peeled["tree"])
    if trees:
        root.append(("040000", "tree", _mktree(git_dir, [("040000", "tree", t, t) for t in trees], cancel), "trees"))
    blobs = sorted((set(objects.get("blob", ())) - set(index)) | peeled["blob"])
    if blobs:
        root.append(("040000", "tree", _mktree(git_dir, [("100644", "blob", b, b) for b in blobs], cancel), "blobs"))
    tree = _mktree(git_dir, root, cancel)
    parents = commits
    if len(parents) > MAX_PARENTS:
        empty = _mktree(git_dir, [], cancel)
        parents = [_commit(git_dir, empty, parents[i:i + MAX_PARENTS], f"subfleet retention anchor part {i // MAX_PARENTS} for {job_id}\n", cancel)
                   for i in range(0, len(parents), MAX_PARENTS)]
    return _commit(git_dir, tree, parents, f"subfleet retention anchor for {job_id}\n", cancel)


def anchor_ref(job_id: str, commit: str) -> str:
    return f"{ARCHIVE_NAMESPACE}/{job_id}/{commit}"


def create_ref(git_dir: Path, ref: str, commit: str, *, cancel: threading.Event | None = None) -> None:
    """Create-only; an existing ref already equal to `commit` is success (Opus finding 2)."""
    try:
        run(["update-ref", "--no-deref", ref, commit, ""], git_dir=git_dir, cancel=cancel)
        return
    except GitError as error:
        current = run(["rev-parse", "--verify", "--quiet", ref], git_dir=git_dir, ok=(0, 1),
                      cancel=cancel).stdout.decode().strip()
        if current != commit:
            raise GitError(f"{ref} exists and names {current or 'nothing'}, not {commit}") from error


def resolve(git_dir: Path, ref: str, *, cancel: threading.Event | None = None) -> str | None:
    out = run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], git_dir=git_dir, ok=(0, 1),
              cancel=cancel).stdout.decode().strip()
    return out or None


def create_bundle(git_dir: Path, path: Path, refs: list[str], held: list[str], *, timeout: float = 3600,
                  cancel: threading.Event | None = None) -> None:
    args = ["bundle", "create", str(path), *refs]
    if held:
        args += ["--not", *held]
    run(args, git_dir=git_dir, timeout=timeout, cancel=cancel)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        rfs.fullsync(fd)
    finally:
        os.close(fd)


def bundle_heads(path: Path, *, cancel: threading.Event | None = None) -> dict[str, str]:
    out = run(["bundle", "list-heads", str(path)], cwd=path.parent, cancel=cancel).stdout.decode("utf-8", "surrogateescape")
    heads: dict[str, str] = {}
    for line in out.splitlines():
        oid, _, ref = line.partition(" ")
        if ref:
            heads[ref] = oid
    return heads


def _pack_offset(path: Path) -> int:
    with open(path, "rb") as stream:
        first = stream.readline()
        if not first.startswith(b"# v2 git bundle") and not first.startswith(b"# v3 git bundle"):
            raise GitError("not a git bundle")
        while True:
            line = stream.readline()
            if not line:
                raise GitError("bundle header has no end")
            if line == b"\n":
                return stream.tell()


def verify_bundle(git_dir: Path, path: Path, expected: dict[str, str], fmt: str, scratch: Path, *,
                  timeout: float = 3600, cancel: threading.Event | None = None) -> None:
    """Read the bundle back: its header in the source repository (prerequisites
    present), its heads, and every object in its pack, hashed by ``index-pack``
    in a throwaway repository that borrows the source's objects only to resolve
    thin deltas."""
    run(["bundle", "verify", "--quiet", str(path)], git_dir=git_dir, timeout=timeout, cancel=cancel)
    heads = bundle_heads(path, cancel=cancel)
    for ref, oid in expected.items():
        if heads.get(ref) != oid:
            raise GitError(f"bundle lacks {ref} at {oid}")
    rfs.remove_own_tree(scratch)
    run(["init", "--quiet", "--bare", f"--object-format={fmt}", str(scratch)], timeout=120, cancel=cancel)
    try:
        objects = Path(os.path.realpath(git_dir / "objects"))
        common_file = git_dir / "commondir"
        if common_file.exists():
            objects = _resolve(git_dir, common_file.read_text().strip()) / "objects"
        (scratch / "objects" / "info" / "alternates").write_text(str(objects) + "\n")
        offset = _pack_offset(path)
        with open(path, "rb") as stream:
            stream.seek(offset)
            run(["index-pack", "--stdin", "--fix-thin"], git_dir=scratch, stdin_file=stream,
                timeout=timeout, cancel=cancel)
    finally:
        rfs.remove_own_tree(scratch)


def late_anchor(git_dir: Path, job_id: str, data: Iterable[bytes], fmt: str, *,
                cancel: threading.Event | None = None) -> str | None:
    """Anchor the objects named in admin files that changed after the final
    check, so a commit made in the quarantined tree stays reachable."""
    pattern = _token_pattern(fmt)
    tokens: set[str] = set()
    for blob in data:
        tokens.update(m.decode() for m in pattern.findall(blob))
    typed = classify(git_dir, tokens, cancel=cancel)
    if not (typed["commit"] or typed["tree"] or typed["blob"] or typed["tag"]):
        return None
    commit = make_anchor(git_dir, job_id + "-late", typed, (), cancel=cancel)
    create_ref(git_dir, f"{ARCHIVE_NAMESPACE}/{job_id}/late-{commit}", commit, cancel=cancel)
    return commit


def reachable_commits(git_dir: Path, tips: Iterable[str], *, cancel: threading.Event | None = None) -> set[str]:
    tips = sorted({t for t in tips if t})
    if not tips:
        return set()
    out = run(["rev-list", "--stdin"], git_dir=git_dir, stdin=("\n".join(tips) + "\n").encode(),
              timeout=600, cancel=cancel).stdout.decode().split()
    return set(out)


Check = Callable[[], None]
