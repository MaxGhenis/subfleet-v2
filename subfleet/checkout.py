"""C-6.14: how much of its repository a job's worktree checks out.

A writable job that is not in place gets a worktree of its own (C-6.6), and a
full checkout copies every committed file. On 2026-09-29 that was about 15 GB
per job on the corpus repository, and four finished jobs held 56 GB under
`worktrees/` until the disk floor (d636) closed every lane. So at submit the
daemon measures what a full checkout of the caller's head would write
(`measure_tree`: the committed blobs' sizes, which is exactly the bytes a
checkout writes before filters), and above `caps.sparse_checkout_min_bytes` it
plans a sparse checkout in git's cone mode (`plan_checkout`): the caller's
place in the repository, the paths the brief names (`named_paths`) or
`run --paths` gives, and then the smallest top-level and second-level
directories, all while the checkout stays within `caps.sparse_cone_budget_bytes`.
Explicit `--paths` are always checked out; `--paths .` asks for everything.

The worktree is a linked worktree of the caller's repository, so every other
committed file is still one `git show HEAD:<path>` away, and
`git sparse-checkout add <dir>` materializes a directory when the job needs it:
the objects are in the shared object store, so nothing is fetched. That is the
fallback when a brief names nothing: the checkout holds the top-level files and
whatever small directories fit the budget, and everything else is read on demand.

`create_worktree` cuts it (`git worktree add --no-checkout`, then
`git sparse-checkout set --cone`, then `git read-tree -mu HEAD`), and
`disk_usage` measures what a worktree holds on disk, for `runs show` and
`daemon.log`. A sparse worktree's snapshots (salvage, the baseline, a turn's
diff, retention's check) never read the files outside its cone as deleted:
see `salvage.working_tree`.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The oldest git whose cone-mode sparse checkout, `--no-sparse-index` and
#: `add --sparse` this module relies on (git 2.37, 2022). An older git is given
#: a full checkout, with that reason recorded.
SPARSE_GIT = (2, 37)
#: How deep the budget fill looks: top-level directories, then their children.
FILL_DEPTH = 2
#: At most this many directories are named in a cone, and this many named paths
#: are read from a brief; what is dropped is recorded, never silently.
MAX_CONE_DIRS = 1000
MAX_NAMED = 64
#: The largest directories left out, as the workspace note and `runs show` name them.
LARGEST_SHOWN = 5
#: `disk_usage`'s default wall-clock cap; a walk cut short says so (`complete`).
DISK_USAGE_CAP_S = 10.0


class CheckoutError(RuntimeError):
    """A sparse worktree could not be cut. `transient` when another try may succeed
    (a lock another git holds), as `salvage.SalvageError` means it."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


# --- measuring a tree ---------------------------------------------------------------

@dataclass(frozen=True)
class TreeSizes:
    """What a full checkout of one tree writes, by directory.

    Directories are `/`-joined paths relative to the top, the top itself `""`.
    `total` and `count` are recursive; `direct` and `direct_count` count only the
    files directly in a directory (a submodule's gitlink is a file of 0 bytes).
    """

    tree: str
    bytes: int
    files: int
    total: Mapping[str, int]
    count: Mapping[str, int]
    direct: Mapping[str, int]
    direct_count: Mapping[str, int]
    children: Mapping[str, tuple[str, ...]]

    def is_dir(self, path: str) -> bool:
        return path in self.total


def parse_ls_tree(data: bytes, tree: str = "") -> TreeSizes:
    """`TreeSizes` from `git ls-tree -r -l -z --full-tree` output.

    Each record is `<mode> <type> <object> <size>\\t<path>`, the size padded with
    spaces, `-` for a gitlink. Paths are decoded as the file system names them
    (`os.fsdecode`), so a name that is not UTF-8 is carried, not refused.
    Files are summed into their own directory first, and each directory's sum
    is then carried up its ancestors once, so a million files cost a million
    steps, not a million times their depth.
    """
    direct: dict[str, int] = {"": 0}
    direct_count: dict[str, int] = {"": 0}
    size_sum = files = 0
    for record in data.split(b"\0"):
        if not record:
            continue
        meta, tab, raw = record.partition(b"\t")
        fields = meta.split()
        if not tab or len(fields) != 4:
            raise ValueError(f"unexpected ls-tree record {record[:80]!r}")
        size = 0 if fields[3] == b"-" else int(fields[3])
        parent = os.fsdecode(raw).rpartition("/")[0]
        files += 1
        size_sum += size
        direct[parent] = direct.get(parent, 0) + size
        direct_count[parent] = direct_count.get(parent, 0) + 1
    # A directory that holds only directories has no file of its own; every
    # ancestor of one that does is a directory too.
    for directory in list(direct):
        here = directory
        while here:
            here = here.rpartition("/")[0]
            if here in direct:
                break
            direct[here] = 0
            direct_count[here] = 0
    total = dict(direct)
    count = dict(direct_count)
    children: dict[str, list[str]] = {directory: [] for directory in direct}
    for directory in direct:
        if not directory:
            continue
        children[directory.rpartition("/")[0]].append(directory)
        here = directory
        while here:
            here = here.rpartition("/")[0]
            total[here] += direct[directory]
            count[here] += direct_count[directory]
    return TreeSizes(tree=tree, bytes=size_sum, files=files, total=total, count=count,
                     direct=direct, direct_count=direct_count,
                     children={key: tuple(sorted(value)) for key, value in children.items()})


_CACHE: OrderedDict[tuple[str, str], TreeSizes] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_SIZE = 8


def measure_tree(repo: str | Path, commit: str, *, timeout_s: float) -> TreeSizes:
    """What a full checkout of `commit` in `repo` writes (C-6.14).

    One `ls-tree -r -l` reads every blob's size from the object store without
    reading its contents (0.5 s for the corpus's 60,164 files, 2026-09-29).
    A tree's sizes never change, so the last few are kept, keyed by the
    repository's common directory and the tree. Raises `subprocess.TimeoutExpired`
    or `OSError`, or `ValueError` when git fails or answers in a shape it never
    does; the caller records the checkout as unmeasured.
    """
    tree = _git_out(repo, "rev-parse", "--verify", f"{commit}^{{tree}}", timeout_s=timeout_s).strip()
    common = _git_out(repo, "rev-parse", "--git-common-dir", timeout_s=timeout_s).strip()
    key = (os.path.realpath(os.path.join(repo, common)), tree)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    raw = _git_bytes(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", tree, timeout_s=timeout_s)
    sizes = parse_ls_tree(raw, tree)
    with _CACHE_LOCK:
        _CACHE[key] = sizes
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return sizes


# --- the paths a brief names --------------------------------------------------------

#: A run of characters a path may hold in prose: anything but white space and the
#: punctuation that brackets or separates a path in Markdown and shell text.
_TOKEN = re.compile(r"[^\s`'\"()\[\]{}<>,;|*]+")
#: A line (and column) or an anchor after a file name: `x.py:12`, `x.py:12:3`, `x.md#L4`.
_LOCATION = re.compile(r"(?::\d+)+$|#L?\d+(?:-L?\d+)?$")


def _clean(token: str) -> str | None:
    """A repository-relative spelling of `token`, or None when it cannot be one."""
    token = _LOCATION.sub("", token.rstrip(".:!?"))
    parts = [part for part in token.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def named_paths(prompt: bytes | str, sizes: TreeSizes, *, roots: Mapping[str, str] | None = None,
                prefix: str = ".") -> tuple[list[dict[str, str]], int]:
    """The directories a brief names, in the order it first names them (C-6.14).

    A token names a path only when it holds a `/` (bare words such as `data` or
    `tests` are prose far more often than paths) and is not a URL. An absolute
    path counts only under one of `roots`, which maps a spelling of a directory
    in the checkout to where it is in the repository (`.` for the top, the
    caller's place for the caller's directory); `~` is expanded. A relative one
    is read relative to the caller's place (`prefix`) when something below that
    place matches, else relative to the top. It resolves to itself when it is a
    directory of the tree, else to its longest ancestor that is one, the top
    excepted: a file names its directory (cone mode checks out directories), and
    so does a file the job is to create there.

    Returns `({"path", "named"}, dropped)`: at most `MAX_NAMED` directories, and
    how many more were named.
    """
    text = prompt.decode("utf-8", "replace") if isinstance(prompt, bytes) else prompt
    spellings = sorted(((str(root).rstrip("/"), where) for root, where in (roots or {}).items()
                        if root and str(root) != "/"), key=lambda item: len(item[0]), reverse=True)
    home = os.path.expanduser("~")
    found: dict[str, str] = {}
    dropped = 0
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if "://" in token or token.startswith(("mailto:", "git@")) or "/" not in token:
            continue
        path = home + token[1:] if token.startswith("~/") else token
        base = prefix
        if path.startswith("/"):
            under = next(((path[len(root) + 1:], where) for root, where in spellings
                          if path == root or path.startswith(root + "/")), None)
            if under is None:
                continue
            path, where = under
            path = path if where in ("", ".") else f"{where}/{path}"
            base = "."
        cleaned = _clean(path)
        if cleaned is None:
            continue
        directory = None
        if base not in ("", "."):
            # Relative to the caller's place only when something below that
            # place matches; `prefix` alone is the caller's place, not a name.
            directory = _existing_dir(f"{base}/{cleaned}", sizes)
            if directory is not None and not directory.startswith(base + "/"):
                directory = None
        directory = directory or _existing_dir(cleaned, sizes)
        if directory and directory not in found:
            if len(found) < MAX_NAMED:
                found[directory] = token
            else:
                dropped += 1
    return [{"path": path, "named": token} for path, token in found.items()], dropped


def _existing_dir(path: str, sizes: TreeSizes) -> str | None:
    """`path` when the tree holds it as a directory, else its longest ancestor
    that is one, the top excepted (None)."""
    while path:
        if sizes.is_dir(path):
            return path
        path = path.rpartition("/")[0]
    return None


def explicit_dirs(paths: Iterable[str], kinds: Mapping[str, str | None]) -> list[str] | None:
    """`run --paths` as cone directories, or None when one of them is `.` (everything).

    `kinds` maps each cleaned path to what the commit holds there (`tree`, `blob`,
    `commit` for a submodule, or None), as `git cat-file -t` answers: a directory
    is itself, a file or submodule its directory (the top for a top-level file,
    which every cone holds). Raises `ValueError` naming a path that is absolute,
    leaves the repository, or that the commit does not hold.
    """
    out: list[str] = []
    for raw in paths:
        cleaned = normalize_explicit(raw)
        if cleaned == ".":
            return None
        kind = kinds.get(cleaned)
        if kind == "tree":
            directory = cleaned
        elif kind in ("blob", "commit"):
            directory = cleaned.rpartition("/")[0]
        else:
            raise ValueError(f"paths: {raw!r} is not in the job's commit; name a directory or file it holds")
        if directory and directory not in out:
            out.append(directory)
    return out


def normalize_explicit(raw: str) -> str:
    """One `--paths` entry as a repository-relative path, `.` for the whole tree."""
    if not isinstance(raw, str):
        raise ValueError("paths: each entry must be a string")
    text = raw.strip()
    if any(ch in text for ch in "\n\r\0"):
        raise ValueError(f"paths: {raw!r} holds a line break or NUL; name one path per entry")
    if text in ("", ".", "./", "/"):
        return "."
    if os.path.isabs(text) or text.startswith("~"):
        raise ValueError(f"paths: {raw!r} must be relative to the repository's top")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"paths: {raw!r} leaves the repository")
    return "/".join(parts) or "."


# --- planning a cone ----------------------------------------------------------------

def safe_dir(path: str) -> bool:
    """Whether `git sparse-checkout set --stdin` reads `path` as written: one line,
    no C-style quoting, no backslash (a cone pattern's escape)."""
    return bool(path) and not any(ch in path for ch in "\n\r\\") and not path.startswith('"')


class Cone:
    """A cone-mode sparse checkout being built, and exactly what it writes.

    Git's cone mode checks out a file when its directory is the top, an ancestor
    of a cone directory, or at or under a cone directory. `counted` is that set
    of directories, so the checkout's bytes are the sum of their `direct` bytes;
    `gain` is what adding one more directory would add.
    """

    def __init__(self, sizes: TreeSizes):
        self.sizes = sizes
        self.dirs: set[str] = set()
        self.counted: set[str] = {""}
        self.bytes = sizes.direct.get("", 0)
        self.files = sizes.direct_count.get("", 0)

    def covered(self, path: str) -> bool:
        here = path
        while here:
            if here in self.dirs:
                return True
            here = here.rpartition("/")[0]
        return False

    def _newly_counted(self, path: str) -> list[str]:
        """The directories adding `path` would count that are not counted yet."""
        new: list[str] = []
        stack = [path]
        while stack:
            here = stack.pop()
            if here not in self.counted:
                new.append(here)
            stack.extend(self.sizes.children.get(here, ()))
        here = path.rpartition("/")[0]
        while here and here not in self.counted:
            new.append(here)
            here = here.rpartition("/")[0]
        return new

    def gain(self, path: str, limit: int | None = None) -> int | None:
        """The bytes adding `path` checks out; None when it would exceed `limit`
        more bytes, which is known without walking when even the part of `path`'s
        subtree that could already be counted leaves too much."""
        if self.covered(path):
            return 0
        if limit is not None and self.sizes.total.get(path, 0) - self.bytes > limit:
            return None
        gained = sum(self.sizes.direct[here] for here in self._newly_counted(path))
        return None if limit is not None and gained > limit else gained

    def add(self, path: str) -> None:
        if self.covered(path):
            return
        for here in self._newly_counted(path):
            self.counted.add(here)
            self.bytes += self.sizes.direct[here]
            self.files += self.sizes.direct_count[here]
        # The cone stays minimal: a directory under the new one is implied by it.
        self.dirs = {d for d in self.dirs if not d.startswith(path + "/")}
        self.dirs.add(path)

    def largest_excluded(self, limit: int = LARGEST_SHOWN) -> list[dict[str, Any]]:
        """The largest directories the cone leaves out entirely, largest first."""
        out: list[tuple[int, str]] = []
        stack = [""]
        while stack:
            here = stack.pop()
            for child in self.sizes.children.get(here, ()):
                if self.covered(child):
                    continue
                if child in self.counted:
                    stack.append(child)
                else:
                    out.append((self.sizes.total[child], child))
        out.sort(key=lambda item: (-item[0], item[1]))
        return [{"path": path, "bytes": size, "files": self.sizes.count[path]} for size, path in out[:limit]]


def cone_files(paths: Iterable[str], cone: Iterable[str]) -> set[str]:
    """The files of `paths` a cone of `cone` checks out, by git's cone-mode rule
    written out directly: the reference `Cone` and git itself are tested against."""
    dirs = set(cone)
    counted = {""}
    for d in dirs:
        here = d.rpartition("/")[0]
        while here:
            counted.add(here)
            here = here.rpartition("/")[0]
    chosen = set()
    for path in paths:
        parent = path.rpartition("/")[0]
        here, inside = parent, False
        while here:
            if here in dirs:
                inside = True
                break
            here = here.rpartition("/")[0]
        if inside or parent in counted:
            chosen.add(path)
    return chosen


def plan_checkout(sizes: TreeSizes | None, *, threshold: int | None, budget: int,
                  explicit: list[str] | None | bool = False, named: Iterable[Mapping[str, str]] = (),
                  prefix: str = ".", git_ok: bool = True, unmeasured: str | None = None) -> dict[str, Any]:
    """C-6.14: the checkout a job's worktree gets, as the manifest records it.

    `explicit` is `run --paths` resolved by `explicit_dirs`: False when not given,
    None for `.` (everything), else the directories, all checked out whatever
    they cost. Without it, `named` (from `named_paths`) are checked out in order,
    each only while the checkout stays within `budget` bytes. The caller's place
    (`prefix`) comes first on the same terms. Then the budget is filled from the
    smallest top-level directories, then the smallest of their children. The top's
    own files are always checked out, whatever they cost.
    """
    base: dict[str, Any] = {"threshold_bytes": threshold, "budget_bytes": budget}
    if sizes is not None:
        base.update(tree=sizes.tree, tree_bytes=sizes.bytes, tree_files=sizes.files)
    if explicit is None:
        return {"mode": "full", "reason": "paths-all", **base}
    if sizes is None:
        return {"mode": "full", "reason": "unmeasured", "error": unmeasured, **base}
    if threshold is None or sizes.bytes <= threshold:
        return {"mode": "full", "reason": "under-threshold", **base}
    if not git_ok:
        return {"mode": "full", "reason": "git-too-old", **base}
    cone = Cone(sizes)
    considered: list[dict[str, Any]] = []
    omitted: list[str] = []

    def offer(path: str, source: str, *, forced: bool = False, named_as: str | None = None) -> bool:
        if not safe_dir(path):
            omitted.append(path)
            considered.append({"path": path, "from": source, "included": False, "reason": "name"})
            return False
        if not cone.covered(path) and len(cone.dirs) >= MAX_CONE_DIRS:
            considered.append({"path": path, "from": source, "included": False, "reason": "cone-full"})
            return False
        gain = cone.gain(path, None if forced else budget - cone.bytes)
        entry: dict[str, Any] = {"path": path, "from": source}
        if named_as is not None and named_as != path:
            entry["named"] = named_as
        if gain is None:
            entry.update(included=False, bytes=sizes.total.get(path, 0), reason="budget")
            considered.append(entry)
            return False
        cone.add(path)
        entry.update(included=True, bytes=gain)
        considered.append(entry)
        return True

    place_kept = True
    if prefix not in ("", ".") and sizes.is_dir(prefix):
        place_kept = offer(prefix, "caller")
    if explicit:
        for path in explicit:
            offer(path, "paths", forced=True)
    elif explicit is False:
        for item in named:
            offer(item["path"], "brief", named_as=item.get("named"))
    for depth in range(1, FILL_DEPTH + 1):
        level = [path for path in sizes.total if path and path.count("/") == depth - 1
                 and not cone.covered(path)]
        level.sort(key=lambda path: (sizes.total[path], path))
        for path in level:
            if len(cone.dirs) >= MAX_CONE_DIRS:
                break
            if not safe_dir(path):
                continue
            gain = cone.gain(path, budget - cone.bytes)
            if gain is not None:
                cone.add(path)
    for entry in considered:
        # What the fill added after an offer was refused can still hold it.
        entry["checked_out"] = cone.covered(entry["path"])
    plan = {"mode": "sparse", "reason": "over-threshold" if explicit is False else "paths", **base,
            "cone": sorted(cone.dirs), "cone_bytes": cone.bytes, "cone_files": cone.files,
            "considered": considered, "largest_excluded": cone.largest_excluded(),
            "place_checked_out": place_kept}
    if omitted:
        plan["unsafe_names"] = omitted
    return plan


# --- cutting the worktree -----------------------------------------------------------

def git_version() -> tuple[int, ...] | None:
    """The installed git's version, read once per process; None when unreadable."""
    global _VERSION
    if _VERSION is _UNREAD:
        try:
            shown = subprocess.run(["git", "version"], capture_output=True, text=True, timeout=30)
            match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", shown.stdout)
            _VERSION = tuple(int(part) for part in match.groups() if part is not None) if match else None
        except (OSError, subprocess.SubprocessError):
            _VERSION = None
    return _VERSION


_UNREAD: Any = object()
_VERSION: Any = _UNREAD


def sparse_supported() -> bool:
    version = git_version()
    return version is not None and version[:2] >= SPARSE_GIT


def create_worktree(repository: str, path: str, head: str, plan: Mapping[str, Any] | None, *,
                    timeout_s: float) -> None:
    """Cut `path` from `repository` at `head` as `plan` says, within `timeout_s` in all.

    A full plan (or none, a job submitted before C-6.14) is `git worktree add
    --detach`, as before. A sparse one adds the worktree with no checkout, sets
    its cone (`--no-sparse-index`: every later snapshot reads a plain index),
    checks out HEAD through the cone, and runs the post-checkout hook as `git
    worktree add` runs it. The first sparse worktree of a
    repository turns on `extensions.worktreeConfig` in its shared config, which is
    how git gives a linked worktree settings of its own; nothing else in the
    repository changes. Raises `subprocess.TimeoutExpired` at the deadline and
    `CheckoutError` when git refuses; the caller removes what was made.
    """
    deadline = time.monotonic() + timeout_s
    if not plan or plan.get("mode") != "sparse":
        _run(repository, "worktree", "add", "--detach", path, head, deadline=deadline)
        return
    _run(repository, "worktree", "add", "--no-checkout", "--detach", path, head, deadline=deadline)
    cone = b"".join(os.fsencode(directory) + b"\n" for directory in plan.get("cone") or ())
    _run(path, "sparse-checkout", "set", "--cone", "--no-sparse-index", "--stdin",
         deadline=deadline, stdin=cone)
    _run(path, "read-tree", "-mu", "HEAD", deadline=deadline)
    # `worktree add --no-checkout` skips the repository's post-checkout hook, which
    # a full `worktree add` runs; run it as that would (no previous HEAD, the new
    # one, a branch checkout), so a sparse worktree is set up as a full one is.
    commit = _run(path, "rev-parse", "--verify", "HEAD", deadline=deadline).decode().strip()
    _run(path, "hook", "run", "--ignore-missing", "post-checkout", "--", "0" * len(commit), commit, "1",
         deadline=deadline)


def _run(cwd: str, *args: str, deadline: float, stdin: bytes | None = None) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(["git", *args], 0)
    result = subprocess.run(["git", "-C", cwd, *args], input=stdin, capture_output=True, timeout=remaining)
    if result.returncode:
        stderr = os.fsdecode(result.stderr).strip()
        # Another git holding the config or index lock, or a git killed by a
        # signal, is the machine's moment, not the repository's state (C-6.8's
        # transient class).
        raise CheckoutError(f"git {args[0]} failed: {stderr[-300:] or f'git exited {result.returncode}'}",
                            transient=result.returncode < 0 or bool(_LOCKED.search(stderr)))
    return result.stdout


#: How git says another git holds a lock: `could not lock config file` and
#: `Unable to create '….lock': File exists`. Not `cannot lock ref`, which also
#: covers a ref that exists or a name that conflicts, which no retry changes.
_LOCKED = re.compile(r"could not lock config|unable to create '[^'\n]*\.lock'", re.IGNORECASE)


# --- measuring a worktree on disk ---------------------------------------------------

def disk_usage(path: str | Path, *, cap_s: float = DISK_USAGE_CAP_S) -> dict[str, Any]:
    """What `path` holds on disk: allocated bytes (`st_blocks`), apparent bytes,
    files and directories, never following a symbolic link, never crossing onto
    another file system, and counting a hard-linked file once.

    The walk stops at `cap_s`; `complete` says whether it saw everything, so a
    partial count is never read as the whole.
    """
    started = time.monotonic()
    deadline = started + cap_s
    root = Path(path)
    try:
        top = os.lstat(root)
    except OSError as exc:
        return {"bytes": 0, "apparent_bytes": 0, "files": 0, "dirs": 0, "complete": False,
                "error": f"{type(exc).__name__}: {exc}", "elapsed_s": 0.0}
    allocated = getattr(top, "st_blocks", 0) * 512
    apparent = files = dirs = 0
    seen: set[tuple[int, int]] = set()
    stack = [str(root)] if stat.S_ISDIR(top.st_mode) else []
    complete = True
    error: str | None = None
    while stack:
        if time.monotonic() >= deadline:
            complete = False
            break
        here = stack.pop()
        dirs += 1
        try:
            entries = list(os.scandir(here))
        except OSError as exc:
            complete, error = False, f"{type(exc).__name__}: {exc}"
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                complete = False
                continue
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev == top.st_dev:
                    stack.append(entry.path)
                    allocated += getattr(info, "st_blocks", 0) * 512
                continue
            if info.st_nlink > 1:
                key = (info.st_dev, info.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            files += 1
            apparent += info.st_size
            allocated += getattr(info, "st_blocks", 0) * 512
    usage = {"bytes": allocated, "apparent_bytes": apparent, "files": files, "dirs": dirs,
             "complete": complete, "elapsed_s": round(time.monotonic() - started, 3)}
    if error:
        usage["error"] = error
    return usage


def human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{value} B"


# --- git helpers --------------------------------------------------------------------

def _git_bytes(repo: str | Path, *args: str, timeout_s: float) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout_s)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {os.fsdecode(result.stderr).strip()[-300:]}")
    return result.stdout


def _git_out(repo: str | Path, *args: str, timeout_s: float) -> str:
    return os.fsdecode(_git_bytes(repo, *args, timeout_s=timeout_s))


def object_kinds(repo: str | Path, commit: str, paths: Iterable[str], *, timeout_s: float) -> dict[str, str | None]:
    """What `commit` holds at each path (`tree`, `blob`, `commit`), or None: one
    `cat-file --batch-check` for all of them."""
    cleaned = [path for path in dict.fromkeys(paths) if path != "."]
    if not cleaned:
        return {}
    request = b"".join(os.fsencode(f"{commit}:{path}") + b"\n" for path in cleaned)
    result = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check=%(objecttype)"],
                            input=request, capture_output=True, timeout=timeout_s)
    if result.returncode:
        raise ValueError(f"git cat-file failed: {os.fsdecode(result.stderr).strip()[-300:]}")
    answers = os.fsdecode(result.stdout).splitlines()
    kinds: dict[str, str | None] = {}
    for path, answer in zip(cleaned, answers, strict=False):
        kinds[path] = answer if answer in ("tree", "blob", "commit") else None
    return kinds


def describe(plan: Mapping[str, Any] | None) -> str:
    """One phrase for a plan, for `daemon.log` and the CLI."""
    plan = plan or {}
    if plan.get("mode") == "sparse":
        return (f"sparse ({len(plan.get('cone') or ())} dirs, {human_bytes(plan.get('cone_bytes'))} of "
                f"the {human_bytes(plan.get('tree_bytes'))} tree)")
    tree = f", {human_bytes(plan.get('tree_bytes'))} tree" if plan.get("tree_bytes") is not None else ""
    return f"full ({plan.get('reason', 'no plan')}{tree})"


def summary(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a submit reply and `runs show` say about a job's checkout plan."""
    if not plan:
        return None
    keys = ("mode", "reason", "tree_bytes", "tree_files", "cone", "cone_bytes", "cone_files",
            "threshold_bytes", "budget_bytes", "largest_excluded", "error")
    out = {key: plan[key] for key in keys if key in plan}
    left = [item["path"] for item in plan.get("considered") or () if not item.get("checked_out", item.get("included"))]
    if left:
        out["left_out"] = left
    return out


def report(jobdir: str | Path) -> dict[str, Any] | None:
    """`runs show`'s `worktree` (C-6.14): a job's own worktree, its checkout, and
    its size on disk when admission cut it (`created`) and after its latest
    attempt (`latest`), from the job's manifest and `worktree.json`. Online and
    offline read the same files. None for a job with no worktree of its own."""
    directory = Path(jobdir)
    workspace = (_json(directory / "manifest.json") or {}).get("workspace") or {}
    record = _json(directory / "worktree.json") or {}
    if not workspace.get("worktree") and not record:
        return None
    return {"path": workspace.get("worktree") or record.get("path"),
            "checkout": summary(workspace.get("checkout")),
            **{stage: record[stage] for stage in ("created", "latest") if stage in record}}


def _json(path: Path) -> dict[str, Any] | None:
    """A job's JSON record, read only as a regular file; None when there is none."""
    import json
    from .sessions.transcripts import NotRegularFile, read_regular
    try:
        value = json.loads(read_regular(path))
    except (OSError, NotRegularFile, ValueError):
        return None
    return value if isinstance(value, dict) else None
