"""Retention's delete path: remove the kept trees of an operator-approved list of finished jobs (C-8.7, d2660).

Retention's normal path archives a finished job before it deletes anything
(`retention_archive`), and keeps a job it cannot archive whole: one whose tree
holds a nested linked worktree (`nested-linked-worktree`), or whose bundle
would carry more history than `remote_less_history_bytes`
(`remote-less-history`). An operator's ruling can give such trees up (decision
d2660). This path removes them, one job at a time on the daemon's retention
worker, after twelve gates, and only once every commit the tree alone holds is
kept: under a ref in the repository, outside the tree, that a linked worktree
belongs to, or in a verified bundle in the job's own folder for a repository
inside the tree. The job's rows, its folder and its accepted outputs stay.
`<state>/jobs/<job>/tree-deleted.json` marks the job, and the normal pass keeps
a marked job for good (`retention_marks`).

The gates, in order (`GATES`), each read just before that job's turn:

1. listed: the job is in the request;
2. finished: a detached job (not a turn) in a terminal state, with no live
   attempt, no retirement journal in flight (an idle, rolled-back one is not in
   flight), no deletion journal and no marker;
3. age: created at least `min_age_days` ago, or named in `age_exempt`;
4. owned path: an in-place job, a sandbox other than `workspace-write`, or a
   `ValueError` from `retention._owned_worktree` fail it; the path must be a
   real directory strictly below `<state>/worktrees`, on the state root's volume;
5. excluded repositories, top level: the job's recorded workdir and the tree's
   top-level `.git` alone are read, and one whose registration, repository or
   borrowed objects lie under an `exclude_repositories` path puts the job out
   with nothing else in the tree read;
6. class: the survey's own detection (`retention_survey.job_info`) still says
   `nested-linked-worktree` or `remote-less-history`;
7. excluded repositories, nested: no git root in the tree names one;
8. pins: `retention._pin_reasons(only=job)` with `hosted` from
   `retention.nested_hosts`, then the reads `retention._Pass._start` makes in
   its selecting transaction (turn rows on or inside the tree, jobs that need a
   place in it, `worktree:` leases inside it); retention's own fence for the
   job is no pin;
9. holders: no process has its working directory in the tree (libproc) or a
   file open there (`lsof_holders`); a scan that fails skips the job this run;
10. registration locks: no registration the tree holds carries git's `locked`;
11. nothing outside depends on it: no repository inside the tree registers a
    linked worktree outside it, and no repository outside it that this path
    reads borrows objects from inside it;
12. free space: `statvfs` free on the state volume is at least `min_free_gb`.

Gates 1 to 4 read only the store and the tree's path. They are read once
before the job's `retire:` and `worktree:` leases are taken, so a job that
fails them names its gate, and again once the leases are held. The rest, the
keeping of commits and the removal run with the leases held under
`retention:<job>`, the holder the normal path uses, so admission starts nothing
in the tree meanwhile (`folders.retiring`). Pins are asked again just before
the rename and just before the removal.

`Excluder` resolves every path this module reads component by component and
refuses a component at or below an excluded root before it stats it, so
nothing there is read or statted (invariant 7). Git itself still reads what a
repository's own config includes; this module does not parse config.

Every step from the leases to the end is journalled in
`<state>/retention-delete/<job>/journal.json`, never under `<state>/retention/`,
which `retention_archive.journals` reads as retirements. The journal is
written before the leases are taken, so the normal pass never mistakes them
for a dead selection's. Up to `removing`, the first destructive state, the
tree is whole: a daemon killed there puts it back on restart, takes away the
refs and bundles that attempt made, and records the job `interrupted`. From
`removing` on, the restart finishes the deletion. A dry run takes no lease and
writes nothing: no journal, ref, bundle, event or file; its record lives in
memory only.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import dependencies, folders, procs
from . import retention as ret
from . import retention_archive as rarch
from . import retention_fs as rfs
from . import retention_git as rgit
from . import retention_marks as marks
from . import retention_survey as rsurvey
from .contracts import Exit
from .protocol import ProtocolError
from .retention_holders import ScanFailed, Watch, lsof_holders

SCHEMA = 1
#: A request names at most this many jobs (the CLI and the daemon refuse more).
MAX_JOBS = 100
#: The persisted apply request, beside the per-job journal folders. Its leading
#: dot keeps it out of `retention_marks.journal_ids`.
RECORD = ".request.json"
#: Where the refs that keep a linked worktree's commits go, in its repository.
KEPT_NAMESPACE = "refs/subfleet-kept"
#: The tombstone's name in the job's journal folder.
TOMBSTONE = "tree"
#: The one event a deleted tree's job gets; its data is the marker.
EVENT = "retention.tree_deleted"
#: Decimal gigabytes, as the request's `min_free_gb` counts them.
GB = 10 ** 9
#: Symlinks followed while resolving one path before it counts as unresolvable.
LINK_LIMIT = 40
#: Git follows alternates at most this deep; so does the exclusion check.
ALTERNATES_DEPTH = 5
GITFILE_LIMIT = 65536
#: Commits handed to one `git merge-base --independent`.
INDEPENDENT_CHUNK = 200
#: A journal that could not be finished or rolled back is tried again after this long.
RETRY_S = 3600.0
GATES = ("listed", "finished", "age", "owned-path", "exclude-top", "class", "exclude-nested", "pins",
         "holders", "locks", "outside", "free-space")
#: Where else a job can stop: a lease another holder has, or a git root whose
#: commits this path cannot keep (the tree stays whole and the job fails).
STOPS = (*GATES, "leases", "keep")
#: What a job comes to: a dry run's verdict, or what an apply did.
DISPOSITIONS = ("would-delete", "deleted", "kept", "skipped", "failed", "interrupted")
#: A request record's states; the first two are a run in progress.
STATUSES = ("running", "stopping", "done", "done-limit", "stopped", "failed")
#: Journal states in order; up to `DESTRUCTIVE` the tree is whole.
STATES = ("gating", "keeping", "kept", "renaming", "renamed", "checked", "removing", "removed", "admins",
          "marking", "marked")
DESTRUCTIVE = "removing"
REQUEST_KEYS = frozenset({"ruling", "jobs", "age_exempt", "min_age_days", "exclude_repositories", "min_free_gb"})
REQUIRED_KEYS = ("ruling", "jobs", "min_age_days", "min_free_gb")
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "lost"})
LIVE_ATTEMPT = ("reserved", "starting", "running", "finalizing")
CLASSES = ("nested-linked-worktree", "remote-less-history")
_KINDS = {"f": "regular file", "d": "directory", "l": "symlink", "p": "fifo", "s": "socket",
          "c": "character device", "b": "block device"}


# --- the request -----------------------------------------------------------------------

class RequestError(ValueError):
    """A request this path refuses; the message names the field."""


class Busy(RuntimeError):
    """A request is already running."""


@dataclass(frozen=True)
class Request:
    ruling: str
    jobs: tuple[str, ...]
    age_exempt: frozenset[str]
    min_age_days: float
    exclude_repositories: tuple[str, ...]
    min_free_gb: float

    def as_json(self) -> dict[str, Any]:
        return {"ruling": self.ruling, "jobs": list(self.jobs), "age_exempt": sorted(self.age_exempt),
                "min_age_days": self.min_age_days, "exclude_repositories": list(self.exclude_repositories),
                "min_free_gb": self.min_free_gb}

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.as_json())).hexdigest()


def _job_id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or value.startswith(".") \
            or any(c in value for c in "/:\\\0") or value != value.strip():
        raise RequestError(f"{where}: {value!r} is not a job id")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise RequestError(f"{where}: must be a finite number of at least 0")
    return float(value)


def parse_request(data: Any) -> Request:
    """The request file's JSON, checked (C-8.7). The CLI and the daemon both
    call this, so neither acts on a request the other would refuse."""
    if not isinstance(data, dict):
        raise RequestError("the request must be a JSON object")
    unknown = sorted(set(data) - REQUEST_KEYS)
    if unknown:
        raise RequestError(f"unknown field {unknown[0]!r}")
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise RequestError(f"missing field {missing[0]!r}")
    ruling = data["ruling"]
    if not isinstance(ruling, str) or not ruling.strip():
        raise RequestError("ruling: must name the ruling")
    if len(ruling) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in ruling):
        raise RequestError("ruling: at most 200 printable characters")
    jobs = data["jobs"]
    if not isinstance(jobs, list) or not jobs:
        raise RequestError("jobs: must list at least one job")
    if len(jobs) > MAX_JOBS:
        raise RequestError(f"jobs: at most {MAX_JOBS} jobs, and {len(jobs)} are listed")
    for job in jobs:
        _job_id(job, "jobs")
    if len(set(jobs)) != len(jobs):
        raise RequestError("jobs: a job is listed twice")
    exempt = data.get("age_exempt", [])
    if not isinstance(exempt, list):
        raise RequestError("age_exempt: must be a list")
    for job in exempt:
        _job_id(job, "age_exempt")
        if job not in jobs:
            raise RequestError(f"age_exempt: {job} is not in jobs")
    if len(set(exempt)) != len(exempt):
        raise RequestError("age_exempt: a job is listed twice")
    excludes = data.get("exclude_repositories", [])
    if not isinstance(excludes, list):
        raise RequestError("exclude_repositories: must be a list")
    spelled: list[str] = []
    for path in excludes:
        if not isinstance(path, str) or not path or "\0" in path or not os.path.isabs(path):
            raise RequestError(f"exclude_repositories: {path!r} is not an absolute path")
        normal = os.path.normpath(path)
        if normal in ("/", "//"):
            raise RequestError("exclude_repositories: / would exclude every repository")
        if normal not in spelled:
            spelled.append(normal)
    return Request(ruling=ruling, jobs=tuple(jobs), age_exempt=frozenset(exempt),
                   min_age_days=_number(data["min_age_days"], "min_age_days"),
                   exclude_repositories=tuple(spelled), min_free_gb=_number(data["min_free_gb"], "min_free_gb"))


def read_request(path: Path) -> Request:
    try:
        text = rfs.read_regular(Path(path), limit=1 << 20)
    except OSError as exc:
        raise RequestError(f"{path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RequestError(f"{path}: not JSON ({exc})") from exc
    return parse_request(data)


# --- excluded repositories ------------------------------------------------------------

class Excluder:
    """The request's `exclude_repositories`, compared as the volume compares
    names (`folders.fold`), each in its own spelling and resolved.

    `resolve` follows a path component by component and asks, before it
    stats each one, whether it is an excluded root or below one: nothing at or
    below an excluded root is read or statted (C-8.7, gates 5 and 7, and
    invariant 7). Resolving a root's own spelling, once, reads its ancestors
    and the root's own entry, never what is in it."""

    def __init__(self, roots: Iterable[str]):
        spellings: set[str] = set()
        for root in roots:
            spellings.add(folders.fold(os.path.normpath(root)))
            try:
                spellings.add(folders.fold(os.path.realpath(root)))
            except (OSError, ValueError):
                pass
        self.roots = tuple(sorted(spellings))

    def covers(self, path: str) -> bool:
        folded = folders.fold(os.path.normpath(path))
        return any(folders.within(folded, root) for root in self.roots)

    def resolve(self, text: str, base: str) -> tuple[str | None, str | None]:
        """(`text` resolved, None), or (None, why): `excluded`, or why it could
        not be resolved without reading an excluded place. `text` is absolute
        or relative to `base`; parts that do not exist are spelled as given."""
        joined = text if os.path.isabs(text) else os.path.join(base, text)
        if "\0" in joined:
            return None, "unresolvable: a NUL in the path"
        if self.covers(joined):
            return None, "excluded"
        pending = [part for part in joined.split("/") if part not in ("", ".")]
        pending.reverse()
        current = "/"
        links = 0
        while pending:
            part = pending.pop()
            if part == "..":
                current = os.path.dirname(current)
                continue
            candidate = os.path.join(current, part)
            if self.covers(candidate):
                return None, "excluded"
            try:
                st = os.lstat(candidate)
            except FileNotFoundError:
                # As `os.path.realpath` does: a missing part is no link, and
                # what follows it is resolved part by part, so a `..` back out
                # of it and on through a link is still checked before it is read.
                current = candidate
                continue
            except OSError as exc:
                return None, f"unresolvable: {candidate}: {exc.strerror or exc}"
            if stat.S_ISLNK(st.st_mode):
                links += 1
                if links > LINK_LIMIT:
                    return None, f"unresolvable: more than {LINK_LIMIT} links"
                try:
                    target = os.readlink(candidate)
                except OSError as exc:
                    return None, f"unresolvable: {candidate}: {exc.strerror or exc}"
                pending.extend(reversed([p for p in target.split("/") if p not in ("", ".")]))
                if os.path.isabs(target):
                    current = "/"
                continue
            current = candidate
        return current, None


# --- small helpers ---------------------------------------------------------------------

def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _utc(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _free(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _key(path: str) -> str:
    return folders.fold(os.path.normpath(path))


def _inside(path: str, top: str) -> bool:
    """Whether `path` is `top` or below it, as the volume compares names."""
    return folders.within(_key(path), _key(top))


def _same(a: str, b: str) -> bool:
    if _key(a) == _key(b):
        return True
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _read_small(path: Path | str, limit: int = GITFILE_LIMIT) -> bytes:
    return rfs.read_regular(Path(path), limit=limit)


def _text(path: Path | str) -> str | None:
    """A small file's text, None when it is not there."""
    try:
        return _read_small(path).decode("utf-8", "surrogateescape").strip()
    except FileNotFoundError:
        return None


def _gitdir_text(data: bytes) -> str | None:
    text = data.decode("utf-8", "surrogateescape").strip()
    if not text.startswith("gitdir:") or "\0" in text:
        return None
    target = text[len("gitdir:"):].strip()
    return target or None


def _is_git_dir(path: str) -> bool:
    """Whether `path` holds what git looks for in a git directory: a `HEAD`,
    and `objects` and `refs` directories (lstat only, nothing followed)."""
    try:
        head = os.lstat(os.path.join(path, "HEAD"))
        objects = os.lstat(os.path.join(path, "objects"))
        refs = os.lstat(os.path.join(path, "refs"))
    except OSError:
        return False
    return (stat.S_ISREG(head.st_mode) or stat.S_ISLNK(head.st_mode)) and stat.S_ISDIR(objects.st_mode) \
        and stat.S_ISDIR(refs.st_mode)


def _why(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or type(exc).__name__


def _digest(commits: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(commits)).encode()).hexdigest()


class _Gate(Exception):
    """A gate (or other stop, `STOPS`) the job did not pass: `gate` names it,
    `reason` says why, `disposition` is what the job comes to."""

    def __init__(self, gate: str, reason: str, disposition: str = "kept"):
        super().__init__(f"{gate}: {reason}")
        self.gate = gate
        self.reason = reason[:500]
        self.disposition = disposition


# --- what the tree holds ------------------------------------------------------------------

@dataclass
class GitRoot:
    """One git root in the tree: a `.git` entry (a gitfile, a directory or
    something else), or a git directory by another name (a bare repository, a
    submodule's under `.git/modules`)."""
    rel: str
    kind: str
    data: bytes = b""


@dataclass
class TreeScan:
    identity: tuple[int, int]
    roots: list[GitRoot]
    logical: int
    allocated: int
    entries: int


def scan_tree(tree: Path, check: Callable[[], None] | None = None) -> TreeScan:
    """Walk the tree without following a link or crossing a device (`rfs.walk`).
    Every `.git` entry, its name compared case-insensitively, is a root (a
    gitfile with its bytes), and so is every other directory holding a `HEAD`
    and `objects` and `refs` directories. Also apparent bytes (files and
    links) and allocated bytes (every entry's blocks, a hard-linked inode once);
    roots are found on every entry, a second link to an inode included."""
    roots: dict[str, GitRoot] = {}
    parts: dict[str, set[str]] = {}
    logical = allocated = entries = 0
    linked: set[tuple[int, int]] = set()
    identity: tuple[int, int] | None = None
    with rfs._cleanup([None]) as owner:
        owner.resources[0] = rfs.open_dir(tree)
        iterator = rfs.walk(owner.resources[0], check)
        with rfs._cleanup((iterator,), close=lambda it: it.close()):
            for rel, st, parent, name in iterator:
                entries += 1
                if not rel:
                    identity = (st.st_dev, st.st_ino)
                    allocated += st.st_blocks * 512
                    continue
                counted = True
                if st.st_nlink > 1 and not stat.S_ISDIR(st.st_mode):
                    counted = (st.st_dev, st.st_ino) not in linked
                    linked.add((st.st_dev, st.st_ino))
                if counted:
                    allocated += st.st_blocks * 512
                    if not stat.S_ISDIR(st.st_mode):
                        logical += st.st_size
                if (name in ("objects", "refs") and stat.S_ISDIR(st.st_mode)) \
                        or (name == "HEAD" and (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode))):
                    parts.setdefault(os.path.dirname(rel), set()).add(name)
                if name is None or name.lower() != ".git":
                    continue
                if stat.S_ISREG(st.st_mode):
                    roots[rel] = GitRoot(rel, "gitfile", _read_entry(parent, name, st))
                elif stat.S_ISDIR(st.st_mode):
                    roots[rel] = GitRoot(rel, "gitdir")
                else:
                    roots[rel] = GitRoot(rel, _KINDS.get(rfs.kind(st.st_mode), "special file"))
    if identity is None:
        raise rfs.TreeError("changed", "the walk did not start at the tree")
    for rel, names in parts.items():
        if rel and rel not in roots and {"HEAD", "objects", "refs"} <= names:
            roots[rel] = GitRoot(rel, "gitdir")
    ordered = sorted(roots.values(), key=lambda root: os.fsencode(root.rel))
    return TreeScan(identity, ordered, logical, allocated, entries)


def _read_entry(parent: int, name: str, st: os.stat_result) -> bytes:
    """A gitfile's bytes, opened by name under its directory's descriptor and
    checked to be the entry the walk saw."""
    handle = os.open(name, rfs.O_FILE, dir_fd=parent)
    try:
        now = os.fstat(handle)
        if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            raise rfs.TreeError("changed", f"{name} was replaced while it was read")
        data = b""
        while len(data) <= GITFILE_LIMIT:
            chunk = os.read(handle, GITFILE_LIMIT + 1 - len(data))
            if not chunk:
                break
            data += chunk
        return data
    finally:
        os.close(handle)


@dataclass
class Outside:
    """A root whose git directory is outside the tree. For a linked worktree
    (`linked`), refs in its repository keep what its registration names; a
    git directory a gitfile names directly keeps its own records."""
    n: int
    rel: str
    git_dir: str          # the registration's admin directory, or the git directory itself
    common: str
    linked: bool


@dataclass
class Inside:
    """A repository inside the tree: a bundle keeps what no network remote holds."""
    n: int
    common: str
    rels: list[str] = field(default_factory=list)


@dataclass
class Layout:
    outside: list[Outside] = field(default_factory=list)
    inside: list[Inside] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Roots whose commits this path cannot keep: the tree stays whole.
    problems: list[str] = field(default_factory=list)
    #: Every registration's `locked` file (gate 10).
    locks: list[str] = field(default_factory=list)
    #: (admin, its gitdir file) of every registration in a repository inside the tree (gate 11).
    backlinks: list[tuple[str, str]] = field(default_factory=list)
    #: Repositories outside the tree that borrow objects from inside it (gate 11).
    borrowers: list[str] = field(default_factory=list)

    def roots(self) -> list[dict[str, Any]]:
        out = [{"n": o.n, "rel": o.rel, "where": "outside", "repository": o.common, "linked": o.linked}
               for o in self.outside]
        for repo in self.inside:
            out += [{"n": repo.n, "rel": rel, "where": "inside", "repository": repo.common} for rel in repo.rels]
        return sorted(out, key=lambda r: (r["n"], os.fsencode(r["rel"])))


def borrowed(git_dir: str, excluder: Excluder, gate: str, problems: list[str]) -> list[str]:
    """The object directories the repository in `git_dir` borrows from
    (`objects/info/alternates`, chained as git chains them), resolved without
    entering an excluded place. One under an excluded root raises `_Gate(gate)`;
    one that cannot be read or resolved is a problem."""
    objects, why = excluder.resolve(os.path.join(git_dir, "objects"), "/")
    if objects is None:
        if why == "excluded":
            raise _Gate(gate, f"{git_dir}'s objects lie in an excluded repository")
        problems.append(f"{git_dir}/objects: {why}")
        return []
    return _alternates(objects, excluder, gate, problems, 0, {_key(objects)})


def _alternates(objects: str, excluder: Excluder, gate: str, problems: list[str], depth: int,
                seen: set[str]) -> list[str]:
    path, why = excluder.resolve(os.path.join(objects, "info", "alternates"), "/")
    if path is None:
        if why == "excluded":
            raise _Gate(gate, f"{objects}'s alternates lie in an excluded repository")
        problems.append(f"{objects}/info/alternates: {why}")
        return []
    try:
        data = _read_small(path, 1 << 20)
    except FileNotFoundError:
        return []
    except OSError as exc:
        problems.append(f"{path}: unreadable ({_why(exc)})")
        return []
    found: list[str] = []
    for line in data.decode("utf-8", "surrogateescape").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith('"'):
            problems.append(f"{path}: a quoted alternate is not read")
            continue
        where, why = excluder.resolve(line, objects)
        if where is None:
            if why == "excluded":
                raise _Gate(gate, f"{objects} borrows objects from {line}, in an excluded repository")
            problems.append(f"{objects} borrows objects from {line} ({why})")
            continue
        if _key(where) in seen:
            continue
        seen.add(_key(where))
        found.append(where)
        if depth + 1 < ALTERNATES_DEPTH:
            found += _alternates(where, excluder, gate, problems, depth + 1, seen)
    return found


def layout(tree: Path, scan: TreeScan, excluder: Excluder) -> Layout:
    """What each root is and where its commits live, read without reading
    anything an excluded path names: a root naming one raises `_Gate` for gate
    5 (the top-level `.git`) or 7. A root whose commits this path cannot keep
    is a problem. A gitfile naming a registration its repository has dropped,
    or another tree's registration, holds nothing of this tree's: a note."""
    tree_s = str(tree)
    out = Layout()
    inside: dict[str, Inside] = {}

    def add_inside(n: int, common: str, rel: str) -> None:
        repo = inside.setdefault(_key(common), Inside(n, os.path.normpath(common)))
        repo.rels.append(rel)

    for n, root in enumerate(scan.roots):
        entry = os.path.join(tree_s, root.rel)
        holder = os.path.dirname(entry)
        gate = "exclude-top" if root.rel.lower() == ".git" else "exclude-nested"
        if root.kind == "gitdir":
            if os.path.lexists(os.path.join(entry, "commondir")):
                out.problems.append(f"{root.rel} is a git directory with a commondir")
                continue
            if not _is_git_dir(entry):
                out.problems.append(f"{root.rel} is a directory git would not read as a repository")
                continue
            borrowed(entry, excluder, gate, out.problems)
            add_inside(n, entry, root.rel)
            continue
        if root.kind != "gitfile":
            out.problems.append(f"{root.rel} is a {root.kind}, neither a gitfile nor a directory")
            continue
        target = _gitdir_text(root.data) if len(root.data) <= GITFILE_LIMIT else None
        if target is None:
            out.problems.append(f"{root.rel} is not a gitfile git can read")
            continue
        admin, why = excluder.resolve(target, holder)
        if admin is None:
            if why == "excluded":
                raise _Gate(gate, f"{root.rel} names {target}, in an excluded repository")
            out.problems.append(f"{root.rel} names {target} ({why})")
            continue
        try:
            st = os.lstat(admin)
        except FileNotFoundError:
            registrations = os.path.dirname(admin)
            common = os.path.dirname(registrations)
            if os.path.basename(registrations) == "worktrees" and _is_git_dir(common):
                out.notes.append(f"{root.rel} names {admin}, a registration its repository no longer has: "
                                 "nothing to keep")
            else:
                out.problems.append(f"{root.rel} names {admin}, which is gone, and no repository holds its commits")
            continue
        except OSError as exc:
            out.problems.append(f"{root.rel} names {admin} ({_why(exc)})")
            continue
        if not stat.S_ISDIR(st.st_mode):
            out.problems.append(f"{root.rel} names {admin}, which is not a directory")
            continue
        try:
            text = _text(os.path.join(admin, "commondir"))
        except OSError as exc:
            out.problems.append(f"{admin}/commondir: unreadable ({_why(exc)})")
            continue
        if text is None:
            if not _is_git_dir(admin):
                remnant = " (the remnant of a registration)" if rgit.is_remnant(Path(admin)) else ""
                out.problems.append(f"{root.rel} names {admin}, which is not a git directory{remnant}")
                continue
            common, linked = admin, False
        else:
            common, why = excluder.resolve(text, admin)
            if common is None:
                if why == "excluded":
                    raise _Gate(gate, f"{admin}/commondir names {text}, in an excluded repository")
                out.problems.append(f"{admin}/commondir names {text} ({why})")
                continue
            if not _is_git_dir(common):
                out.problems.append(f"{root.rel}: its repository {common} is missing or not a git directory")
                continue
            try:
                back = _text(os.path.join(admin, "gitdir"))
            except OSError as exc:
                out.problems.append(f"{admin}/gitdir: unreadable ({_why(exc)})")
                continue
            if not back:
                out.problems.append(f"{admin} has no gitdir naming its tree")
                continue
            backlink, why = excluder.resolve(back, admin)
            if backlink is None:
                if why == "excluded":
                    raise _Gate(gate, f"{admin}/gitdir names {back}, in an excluded repository")
                out.problems.append(f"{admin}/gitdir names {back} ({why})")
                continue
            if not _same(backlink, entry):
                out.notes.append(f"{root.rel} names {admin}, which registers {backlink}: that tree's, not this one's")
                continue
            if _key(os.path.dirname(admin)) != _key(os.path.join(common, "worktrees")):
                out.problems.append(f"{admin} is not directly under {common}/worktrees")
                continue
            out.locks.append(os.path.join(admin, "locked"))
            linked = True
        lent = borrowed(common, excluder, gate, out.problems)
        if _inside(common, tree_s):
            add_inside(n, common, root.rel)
        else:
            out.outside.append(Outside(n, root.rel, admin, common, linked))
            if not linked:
                out.notes.append(f"{root.rel} uses the git directory {admin}, outside the tree, which keeps "
                                 "its own records")
            if any(_inside(where, tree_s) for where in lent):
                out.borrowers.append(common)
    for repo in sorted(inside.values(), key=lambda r: r.n):
        worktrees = os.path.join(repo.common, "worktrees")
        try:
            names = sorted(os.listdir(worktrees), key=os.fsencode)
        except (FileNotFoundError, NotADirectoryError):
            names = []
        except OSError as exc:
            out.problems.append(f"{worktrees}: unreadable ({_why(exc)})")
            names = []
        for name in names:
            admin = os.path.join(worktrees, name)
            try:
                if not stat.S_ISDIR(os.lstat(admin).st_mode):
                    continue
            except OSError:
                continue
            out.locks.append(os.path.join(admin, "locked"))
            out.backlinks.append((admin, os.path.join(admin, "gitdir")))
        out.inside.append(repo)
    return out


# --- commits -------------------------------------------------------------------------------

def _split(data: bytes) -> set[str]:
    return {token for token in data.decode("utf-8", "surrogateescape").split() if token}


def _typed_commits(git_dir: Path, tokens: set[str], cancel: threading.Event | None) -> set[str]:
    typed = rgit.classify(git_dir, tokens, cancel=cancel)
    commits = set(typed["commit"])
    if typed["tag"]:
        commits |= rgit.peel_tags(git_dir, typed["tag"], cancel=cancel)["commit"]
    return commits


def _ref_folders(common: Path, ref: str) -> list[Path]:
    """The folders a loose `ref` sits in below `refs/`, shallowest first: for
    `refs/subfleet-kept/<job>/<n>/<m>`, the namespace, the job's and the root's."""
    parts = ref.split("/")[1:-1]
    return [common.joinpath("refs", *parts[:i]) for i in range(1, len(parts) + 1)]


def admin_commits(admin: Path, fmt: str, cancel: threading.Event | None = None) -> set[str]:
    """The commits a linked worktree's admin directory names: its HEAD and the
    other pseudo-refs, its per-worktree refs (`refs/worktree/*`, `refs/bisect/*`,
    `refs/rewritten/*`), its HEAD reflog and every id in its files
    (`rgit.admin_objects`), tags peeled."""
    objects = rgit.admin_objects(admin, fmt, cancel=cancel)
    commits = set(objects["commit"])
    if objects["tag"]:
        commits |= rgit.peel_tags(admin, objects["tag"], cancel=cancel)["commit"]
    return commits


def _pseudo_refs(git_dir: Path, cancel: threading.Event | None) -> set[str]:
    found: set[str] = set()
    for name in rgit.PSEUDO_REFS:
        out = rgit.run(["rev-parse", "--verify", "--quiet", name], git_dir=git_dir, ok=(0, 1),
                       cancel=cancel).stdout.decode().strip()
        if out:
            found.add(out)
    return found


def repository_commits(common: Path, fmt: str, cancel: threading.Event | None = None) -> set[str]:
    """Every commit a repository's own records name: every ref (branches,
    tags, `refs/stash`, notes, remote-tracking refs), the main worktree's HEAD,
    pseudo-refs and per-worktree refs, every reflog of every worktree
    (`rev-list --reflog`), and each linked worktree's admin directory
    (`admin_commits`), tags peeled."""
    tokens = _split(rgit.run(["for-each-ref", "--format=%(objectname)"], git_dir=common, cancel=cancel).stdout)
    tokens |= _split(rgit.run(["for-each-ref", "--format=%(objectname)", *rgit.WORKTREE_REF_PREFIXES],
                              git_dir=common, ok=(0, 1), cancel=cancel).stdout)
    tokens |= _pseudo_refs(common, cancel)
    # With no reflog at all rev-list has no revision to list: it prints its usage and exits 129.
    reflog = rgit.run(["rev-list", "--no-walk", "--ignore-missing", "--reflog"], git_dir=common, ok=(0, 129),
                      timeout=600, cancel=cancel)
    if reflog.returncode == 0:
        tokens |= _split(reflog.stdout)
    elif reflog.stdout.strip():
        raise rgit.GitError("git rev-list --reflog failed")
    commits = _typed_commits(common, tokens, cancel)
    worktrees = common / "worktrees"
    try:
        names = sorted(os.listdir(worktrees), key=os.fsencode)
    except (FileNotFoundError, NotADirectoryError):
        names = []
    for name in names:
        admin = worktrees / name
        if stat.S_ISDIR(os.lstat(admin).st_mode) and not rgit.is_remnant(admin):
            commits |= admin_commits(admin, fmt, cancel=cancel)
    return commits


def independent(git_dir: Path, commits: Iterable[str], cancel: threading.Event | None = None) -> list[str]:
    """The tips of `commits` (`git merge-base --independent`), in chunks. A
    chunked answer may keep a commit another chunk's tip reaches: a ref more
    than needed, never one fewer."""
    tips = sorted(set(commits))
    for _ in range(4):
        if len(tips) <= 1:
            return tips
        reduced: set[str] = set()
        for i in range(0, len(tips), INDEPENDENT_CHUNK):
            part = tips[i:i + INDEPENDENT_CHUNK]
            if len(part) == 1:
                reduced.update(part)
                continue
            reduced |= _split(rgit.run(["merge-base", "--independent", *part], git_dir=git_dir, timeout=600,
                                       cancel=cancel).stdout)
        done = len(reduced) == len(tips) or len(tips) <= INDEPENDENT_CHUNK
        tips = sorted(reduced)
        if done:
            return tips
    return tips


def held_objects(common: Path, remotes: dict[str, str], cancel: threading.Event | None) -> list[str]:
    """The commits and tags the remote-tracking refs of network remotes name
    (`rgit.network_remotes`, retention's notion of what a remote holds), those
    the repository has."""
    if not remotes:
        return []
    out = rgit.run(["for-each-ref", "--format=%(objectname)", *[f"refs/remotes/{name}/" for name in sorted(remotes)]],
                   git_dir=common, cancel=cancel).stdout
    typed = rgit.classify(common, _split(out), cancel=cancel)
    return sorted(typed["commit"] | typed["tag"])


def kept_ref(job_id: str, n: int, m: int | str) -> str:
    return f"{KEPT_NAMESPACE}/{job_id}/{n}/{m}"


# --- the delete path ------------------------------------------------------------------------

Holders = Callable[..., dict[str, list[str]]]
CwdPids = Callable[[str], frozenset[int]]


class DeletePath:
    """The daemon's delete path. `step` resumes any journal that is due, then
    does one job of the running request, on the retention worker; `start`,
    `stop` and `status` answer the `retention.delete` op. Every argument after
    `root` is a seam for tests: `on_step(job_id, point)` runs after each
    journal write (point = the state) and at a few points between them, and may
    raise a `BaseException` that is not an `Exception` to stand for the daemon
    being killed there."""

    def __init__(self, store: Any, root: Path, *, holders: Holders | None = None, cwd_pids: CwdPids | None = None,
                 now: Callable[[], datetime] | None = None, clock: Callable[[], float] | None = None,
                 on_step: Callable[[str, str], None] | None = None, log: Any = None):
        self.store = store
        self.root = Path(root).resolve()
        self.base = self.root / marks.DELETE_DIR
        self.holders = holders or lsof_holders
        self.cwd_pids = cwd_pids or procs.worktree_cwd_pids
        self.now = now or (lambda: datetime.now(UTC))
        self.clock = clock or time.time
        self.on_step = on_step
        self.log = log
        self.lock = threading.RLock()
        #: The persisted apply record; the latest dry run's lives in memory only.
        self.apply: dict[str, Any] | None = self._load()
        self.dry: dict[str, Any] | None = None
        #: Jobs with a deletion journal on disk (this path alone writes them).
        self.journals: set[str] = set(marks.journal_ids(self.root))
        #: job id -> when its journal is next tried, for one that could not be finished or rolled back.
        self.retry_at: dict[str, float] = {}
        # Per step, set by `step`.
        self.pins: Callable[[], Iterable[str]] | None = None
        self.explicit: set[str] = set()
        self.turn_keep_s = 0.0
        self.remote_less: int | None = None
        self.cancel: threading.Event | None = None

    # --- the record ------------------------------------------------------------------

    def _load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(rfs.read_regular(self.base / RECORD, limit=16 << 20))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            if self.log is not None:
                self.log.warning("retention delete: the request record is unreadable: %s", type(exc).__name__)
            return None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("status") not in STATUSES:
            return None
        return data

    def _save(self) -> None:
        assert self.apply is not None
        self.apply["updated_at"] = _utc(self.now())
        self._ensure_base()
        rfs.write_atomic(self.base / RECORD, _canonical(self.apply))

    def _ensure_base(self) -> None:
        self.base.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(self.base)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(errno.ENOTDIR, "the delete path's folder is not a directory", str(self.base))
        if st.st_dev != os.lstat(self.root).st_dev:
            raise OSError(errno.EXDEV, "the delete path's folder is on another volume", str(self.base))

    def _active(self) -> dict[str, Any] | None:
        with self.lock:
            for record in (self.apply, self.dry):
                if record is not None and record.get("status") in ("running", "stopping"):
                    return record
            return None

    def start(self, request: Request, *, mode: str, limit: int | None = None) -> dict[str, Any]:
        """Begin a dry run or an apply (C-8.7). Another one while either runs is
        refused (`Busy`). An apply of the request the last apply ran continues
        it: deleted jobs stay deleted, the others are gated again."""
        if mode not in ("dry-run", "apply"):
            raise ValueError(mode)
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("limit")
        with self.lock:
            if self._active() is not None:
                raise Busy("a retention delete request is running")
            moment = _utc(self.now())
            fresh = {"schema": SCHEMA, "mode": mode, "request": request.as_json(), "digest": request.digest,
                     "run": 1, "results": {job: None for job in request.jobs}, "runs": []}
            if mode == "dry-run":
                record = fresh
            else:
                previous = self.apply
                if previous is not None and previous.get("digest") == request.digest:
                    record = previous
                    record["run"] = int(record.get("run") or 0) + 1
                    for job, result in list(record["results"].items()):
                        if not (isinstance(result, dict) and result.get("disposition") == "deleted"):
                            record["results"][job] = None
                else:
                    if previous is not None:
                        self._archive_record(previous)
                    record = fresh
            record.update(status="running", limit=limit if mode == "apply" else None, started_at=moment,
                          ended_at=None, deleted_this_run=0, stop=False, error=None)
            if mode == "dry-run":
                self.dry = record
            else:
                self.apply = record
                self._save()
            return self._view(record)

    def _archive_record(self, record: dict[str, Any]) -> None:
        name = f".request-{str(record.get('digest') or 'unknown')[:12]}-{int(self.clock())}.json"
        try:
            self._ensure_base()
            rfs.write_atomic(self.base / name, _canonical(record))
        except OSError as exc:
            if self.log is not None:
                self.log.warning("retention delete: the previous request record was not kept: %s",
                                 type(exc).__name__)

    def stop(self) -> dict[str, Any]:
        """End the running request at the next job boundary."""
        with self.lock:
            record = self._active()
            if record is None:
                return {"stopped": False, **self.status()}
            record["stop"] = True
            record["status"] = "stopping"
            if record is self.apply:
                self._save()
            return {"stopped": True, **self.status()}

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"apply": self._view(self.apply) if self.apply is not None else None,
                    "dry_run": self._view(self.dry) if self.dry is not None else None,
                    "journals": sorted(self.journals)}

    def _view(self, record: dict[str, Any]) -> dict[str, Any]:
        request = record.get("request") or {}
        results = record.get("results") or {}
        totals: dict[str, int] = {d: 0 for d in DISPOSITIONS}
        totals.update(pending=0, logical_bytes=0, allocated_bytes=0, refs=0, bundles=0, bundle_bytes=0)
        lines = []
        for job in request.get("jobs") or ():
            result = results.get(job)
            if not isinstance(result, dict):
                totals["pending"] += 1
                continue
            lines.append(result)
            totals[result["disposition"]] = totals.get(result["disposition"], 0) + 1
            if result["disposition"] in ("deleted", "would-delete"):
                for key in ("logical_bytes", "allocated_bytes"):
                    totals[key] += int((result.get("bytes") or {}).get(key) or 0)
                keep = result.get("keep") or {}
                totals["refs"] += len(keep.get("refs") or ())
                totals["bundles"] += len(keep.get("bundles") or ())
                totals["bundle_bytes"] += sum(int(b.get("bytes") or 0) for b in keep.get("bundles") or ())
        view = {key: record.get(key) for key in ("mode", "status", "digest", "run", "limit", "started_at",
                                                 "ended_at", "deleted_this_run", "error", "runs")}
        return {**view, "request": request, "results": json.loads(_canonical(lines)), "totals": totals}

    def pending(self) -> bool:
        """Whether `step` has work: a running request, or a journal due. Memory only."""
        with self.lock:
            if self._active() is not None:
                return True
            now = self.clock()
            return any(self.retry_at.get(job, 0.0) <= now for job in self.journals)

    def fail(self, error: str) -> None:
        """End the running request as failed, and leave every journal for an
        hour (the daemon calls this when a step raises what it did not expect,
        so it does not raise again at every tick)."""
        with self.lock:
            self._end_run(error[:300])
            later = self.clock() + RETRY_S
            for job_id in self.journals:
                self.retry_at[job_id] = max(self.retry_at.get(job_id, 0.0), later)

    # --- one step ----------------------------------------------------------------------

    def step(self, *, pins: Callable[[], Iterable[str]] | None = None, explicit: Iterable[str] = (),
             turn_keep_s: float = 0, remote_less_history_bytes: int | None = None,
             cancel: threading.Event | None = None) -> dict[str, Any]:
        """Resume what a killed daemon left, then do one job of the running request."""
        self.pins, self.explicit, self.turn_keep_s = pins, set(explicit), turn_keep_s
        self.remote_less, self.cancel = remote_less_history_bytes, cancel
        report: dict[str, Any] = {"resumed": []}
        now = self.clock()
        for job_id in sorted(self.journals):
            if self.retry_at.get(job_id, 0.0) > now:
                continue
            if cancel is not None and cancel.is_set():
                return report
            try:
                report["resumed"].append({"job_id": job_id, **self._resume(job_id)})
            except (rarch.Interrupted, rgit.Cancelled):
                return report
            except sqlite3.Error as exc:
                self._end_run(f"store: {type(exc).__name__}")
                report["error"] = "store"
                return report
        record = self._active()
        if record is None:
            return report
        if record.get("stop"):
            self._finish(record, "stopped")
            return report
        if self._limit_reached(record):
            self._finish(record, "done-limit")
            return report
        waiting = [job for job in record["request"]["jobs"] if record["results"].get(job) is None]
        if not waiting:
            self._finish(record, "done")
            return report
        if cancel is not None and cancel.is_set():
            return report
        job_id = waiting[0]
        request = parse_request(record["request"])
        try:
            result = self._one(request, job_id, dry=record["mode"] == "dry-run", run=int(record.get("run") or 1))
        except sqlite3.Error as exc:
            # A store write failure ends the run (C-8.7); a journal the job
            # left is resumed by a later step.
            self._end_run(f"store: {type(exc).__name__}")
            report["error"] = "store"
            return report
        if result is None:                      # cancelled, or finishing from its journal
            return report
        with self.lock:
            record["results"][job_id] = result
            if result["disposition"] == "deleted":
                record["deleted_this_run"] = int(record.get("deleted_this_run") or 0) + 1
            if record is self.apply:
                self._save()
        report["job"] = result
        if self.log is not None:
            self.log.info("retention delete: %s %s%s", job_id, result["disposition"],
                          f" ({result['gate']})" if result.get("gate") else "")
        if all(record["results"].get(job) is not None for job in record["request"]["jobs"]):
            self._finish(record, "done")
        elif self._limit_reached(record):
            self._finish(record, "done-limit")
        return report

    @staticmethod
    def _limit_reached(record: dict[str, Any]) -> bool:
        return record["mode"] == "apply" and record.get("limit") is not None \
            and int(record.get("deleted_this_run") or 0) >= int(record["limit"])

    def _finish(self, record: dict[str, Any], status: str) -> None:
        with self.lock:
            record["status"] = status
            record["ended_at"] = _utc(self.now())
            record.setdefault("runs", []).append({"run": record.get("run"), "status": status,
                                                  "started_at": record.get("started_at"),
                                                  "ended_at": record["ended_at"], "limit": record.get("limit"),
                                                  "deleted": record.get("deleted_this_run")})
            if record is self.apply:
                self._save()

    def _end_run(self, error: str) -> None:
        record = self._active()
        if record is not None:
            record["error"] = error
            try:
                self._finish(record, "failed")
            except OSError:
                pass

    # --- one job --------------------------------------------------------------------

    def _one(self, request: Request, job_id: str, *, dry: bool, run: int) -> dict[str, Any] | None:
        result: dict[str, Any] = {"job_id": job_id, "run": run, "started_at": _utc(self.now()), "gate": None,
                                  "reason": None, "class": None, "roots": [], "keep": {"refs": [], "bundles": []}}
        excluder = Excluder(request.exclude_repositories)
        holder = f"retention:{job_id}"
        journal: dict[str, Any] | None = None
        try:
            job, tree, folder = self._first_gates(request, job_id)
            keys = [f"retire:{job_id}", folders.exclusive_key(folder)]
            if dry:
                self._leases_free(keys, holder)
            else:
                journal = self._begin(job_id, request, run, tree, folder, keys)
                self._take(keys, holder, job_id)
                # Read again with the fence held: nothing can start in the tree now.
                job, again, folder_again = self._first_gates(request, job_id, own_journal=True)
                if again != tree or folder_again != folder:
                    raise _Gate("owned-path", "the job's tree changed while its leases were taken")
            plan = self._gates(request, job, tree, folder, excluder, result)
            if dry:
                result.update(disposition="would-delete", ended_at=_utc(self.now()))
                return result
            assert journal is not None
            return self._delete(journal, job, tree, folder, plan, result, excluder)
        except _Gate as gate:
            result.update(disposition=gate.disposition, gate=gate.gate, reason=gate.reason,
                          ended_at=_utc(self.now()))
            if journal is not None:
                self._settle(journal, result)
            return result
        except (rarch.Interrupted, rgit.Cancelled):
            if journal is not None:
                self._settle(journal, None)
            return None
        except sqlite3.Error:
            if journal is not None:
                try:
                    self._settle(journal, None)
                except (sqlite3.Error, OSError):
                    pass
            raise
        except Exception as exc:        # one job's failure never ends the run (C-8.7)
            result.update(disposition="failed", reason=f"{type(exc).__name__}: {exc}"[:500],
                          ended_at=_utc(self.now()))
            if journal is not None:
                self._settle(journal, result)
            return result

    def _settle(self, journal: dict[str, Any], result: dict[str, Any] | None) -> None:
        """After a job stopped: before the destructive step, put the tree back and
        drop the journal; from it on, leave the journal for a later step to finish."""
        if _before_destructive(journal):
            try:
                rolled = self._rollback(journal)
            except (OSError, rgit.GitError) as exc:
                self.retry_at[journal["job_id"]] = self.clock() + RETRY_S
                if result is not None:
                    result["reason"] = (str(result.get("reason") or "")
                                        + f"; the rollback did not finish ({type(exc).__name__}: {exc}); "
                                          "its journal is tried again later")[:500]
                return
            if result is not None and rolled.get("notes"):
                result["rollback_notes"] = rolled["notes"]
        else:
            self.retry_at[journal["job_id"]] = self.clock() if result is None else self.clock() + RETRY_S
            if result is not None:
                result["reason"] = (str(result.get("reason") or "") + "; the deletion resumes from its journal")[:500]

    def _check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise rarch.Interrupted("cancelled")

    def _hook(self, job_id: str, point: str) -> None:
        if self.on_step is not None:
            self.on_step(job_id, point)

    # gates 1 to 4 -------------------------------------------------------------------

    def _first_gates(self, request: Request, job_id: str, *,
                     own_journal: bool = False) -> tuple[dict[str, Any], Path, str]:
        if job_id not in request.jobs:
            raise _Gate("listed", "not in the request")
        row = self.store.get_job(job_id)
        if row is None:
            raise _Gate("finished", "no job row")
        job = dict(row)
        if job.get("kind") == "turn":
            raise _Gate("finished", "a conversation turn, not a detached job")
        if job.get("state") not in TERMINAL:
            raise _Gate("finished", f"state {job.get('state')}")
        live = self.store.query("SELECT attempt_id,state FROM attempts WHERE job_id=? AND state IN "
                                f"({','.join('?' * len(LIVE_ATTEMPT))}) ORDER BY attempt_id", (job_id, *LIVE_ATTEMPT))
        if live:
            raise _Gate("finished", f"attempt {live[0]['attempt_id']} is {live[0]['state']}")
        try:
            retirement = rarch.load_journal(self.root, job_id)
        except (OSError, ValueError) as exc:
            raise _Gate("finished", f"its retirement journal is unreadable ({type(exc).__name__})")
        if retirement is not None and retirement.get("state") != "idle":
            raise _Gate("finished", f"a retirement is in flight ({retirement.get('state')})")
        if not own_journal and job_id in self.journals:
            raise _Gate("finished", "a deletion of its tree is in flight")
        if marks.marked(self.root, job_id):
            raise _Gate("finished", "its tree is already deleted (tree-deleted.json)")
        if job_id not in request.age_exempt:
            created = _parse_time(job.get("created_at"))
            if created is None:
                raise _Gate("age", f"created_at {job.get('created_at')!r} is unreadable")
            days = (self.now() - created).total_seconds() / 86400
            if days < request.min_age_days:
                raise _Gate("age", f"{days:.2f} days old, under {request.min_age_days:g}")
        if job.get("in_place"):
            raise _Gate("owned-path", "an in-place job: its folder is not the daemon's")
        if job.get("sandbox") != "workspace-write":
            raise _Gate("owned-path", f"sandbox {job.get('sandbox')}: no allocated worktree")
        try:
            tree = ret._owned_worktree(job, self.root)
        except ValueError as exc:
            raise _Gate("owned-path", str(exc))
        if tree is None:
            raise _Gate("owned-path", "no allocated worktree")
        allocated = self.root / "worktrees"
        if tree == allocated or allocated not in tree.parents:
            raise _Gate("owned-path", f"{tree} is not strictly below {allocated}")
        try:
            st = os.lstat(tree)
        except FileNotFoundError:
            raise _Gate("owned-path", f"{tree} is not there")
        if not stat.S_ISDIR(st.st_mode):
            raise _Gate("owned-path", f"{tree} is not a directory")
        if st.st_dev != os.lstat(self.root).st_dev:
            raise _Gate("owned-path", f"{tree} is on another volume than the state root")
        try:
            folder = folders.canonical(tree)
        except OSError as exc:
            raise _Gate("owned-path", f"{tree}: {type(exc).__name__}")
        if not _inside(folder, str(allocated)) or _key(folder) == _key(str(allocated)):
            raise _Gate("owned-path", f"{folder} is not strictly below {allocated}")
        return job, tree, folder

    # leases ------------------------------------------------------------------------

    def _leases_free(self, keys: list[str], holder: str) -> None:
        for key in keys:
            row = self.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))
            if row is not None and row["holder"] != holder:
                raise _Gate("leases", f"{key} is held by {row['holder']}", disposition="skipped")

    def _take(self, keys: list[str], holder: str, job_id: str) -> None:
        with self.store.transaction("retention.delete_selected", job_id=job_id, data={"keys": keys}) as conn:
            for key in keys:
                current = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone()
                if current is not None and current["holder"] != holder:
                    raise _Gate("leases", f"{key} is held by {current['holder']}", disposition="skipped")
            for key in keys:
                conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                             (key, holder, _utc(self.now())))

    def _release(self, keys: list[str], holder: str, job_id: str) -> None:
        """Release this path's own rows, by key (`release_leases` takes every row of the holder)."""
        if not keys:
            return
        with self.store.transaction("retention.delete_released", job_id=job_id, data={"keys": keys}) as conn:
            conn.execute(f"DELETE FROM leases WHERE holder=? AND lease_key IN ({','.join('?' * len(keys))})",
                         (holder, *keys))

    # gates 5 to 12 -----------------------------------------------------------------

    def _gates(self, request: Request, job: dict[str, Any], tree: Path, folder: str, excluder: Excluder,
               result: dict[str, Any]) -> dict[str, Any]:
        job_id = job["job_id"]
        self._check()
        top_digest = self._top(job, tree, excluder)
        self._check()
        # 6. Still the class the survey found, by its own detection.
        info = rsurvey.job_info(job, self.root, pin=None, kept=None, journal=None,
                                salvage_refs=self._salvage_refs(job_id), cache={},
                                remote_less=self.remote_less, live=self._live_trees(excluder),
                                known=lambda: [], sizes=True)
        issue = info.get("issue")
        result["class"] = issue
        if not issue or not str(issue).startswith(CLASSES):
            raise _Gate("class", "retention would retire it normally" if not issue
                        else f"retention keeps it for another reason: {issue}")
        # 7. Nothing nested names an excluded repository.
        self._check()
        try:
            scan = scan_tree(tree, self._check)
        except rfs.TreeError as exc:
            raise _Gate("exclude-nested", f"the tree could not be read whole: {exc}")
        top = next((root for root in scan.roots if root.rel.lower() == ".git"), None)
        seen = hashlib.sha256(top.data).hexdigest() if top is not None and top.kind == "gitfile" else None
        if seen != top_digest:
            raise _Gate("exclude-top", "the tree's .git changed after it was read")
        plan = layout(tree, scan, excluder)
        result["roots"] = plan.roots()
        result["notes"] = plan.notes
        result["bytes"] = {"logical_bytes": scan.logical, "allocated_bytes": scan.allocated, "entries": scan.entries}
        # 8. Pins, asked as the normal path asks them when it selects a job.
        self._check()
        hosted = self._hosted(job_id, tree, excluder)
        why = self._pinned(job_id, folder, hosted)
        if why is not None:
            raise _Gate("pins", why)
        # 9. Holders.
        self._check()
        self._holders(job_id, [folder, str(tree)], scan.identity)
        # 10. Registration locks: git's `locked`, whoever wrote it.
        for lock in plan.locks:
            if os.path.lexists(lock):
                try:
                    text = _text(lock) or "locked"
                except OSError:
                    text = "locked (unreadable)"
                raise _Gate("locks", f"{lock}: {text[:200]}")
        # 11. Nothing outside depends on the tree.
        for admin, gitdir in plan.backlinks:
            try:
                back = _text(gitdir)
            except OSError as exc:
                raise _Gate("outside", f"{gitdir}: unreadable ({_why(exc)})")
            if not back:
                continue                    # git's own prune would remove it: it registers nothing
            where, _ = excluder.resolve(back, admin)
            if where is None or not _inside(where, str(tree)):
                raise _Gate("outside", f"{admin} registers {back}, outside the tree")
        if plan.borrowers:
            raise _Gate("outside", f"{plan.borrowers[0]} borrows objects from inside the tree")
        # 12. Free space.
        free = _free(self.root)
        if free < request.min_free_gb * GB:
            raise _Gate("free-space", f"{free / GB:.2f} GB free, under {request.min_free_gb:g} GB")
        if plan.problems:
            more = f" (and {len(plan.problems) - 1} more)" if len(plan.problems) > 1 else ""
            raise _Gate("keep", plan.problems[0] + more, disposition="failed")
        keep = self._plan_keep(plan)
        result["keep"] = {"refs": [{"repository": r["common"], "n": r["n"], "tips": len(r["tips"])}
                                   for r in keep["outside"] if r["tips"]],
                          "bundles": [{"repository": b["common"], "n": b["n"], "tips": len(b["tips"]),
                                       "unheld_tips": len(b["unheld"])} for b in keep["inside"] if b["unheld"]]}
        result["free_before"] = free
        return {"layout": plan, "scan": scan, "keep": keep, "free_before": free, "top_digest": top_digest,
                "class": issue, "hosted": hosted}

    def _top(self, job: dict[str, Any], tree: Path, excluder: Excluder) -> str | None:
        """Gate 5: the job's recorded workdir and the tree's top-level `.git`
        alone; the gitfile's digest, or None for no gitfile."""
        workdir = job.get("workdir")
        if isinstance(workdir, str) and workdir:
            where, why = excluder.resolve(workdir, "/")
            if where is None:
                raise _Gate("exclude-top", f"its workdir {workdir}" + (" lies in an excluded repository"
                                                                       if why == "excluded" else f": {why}"))
        top = tree / ".git"
        try:
            st = os.lstat(top)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            raise _Gate("exclude-top", ".git is a symlink, which this path does not follow")
        if stat.S_ISDIR(st.st_mode):
            borrowed(str(top), excluder, "exclude-top", [])
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        try:
            data = _read_small(top)
        except OSError as exc:
            raise _Gate("exclude-top", f".git is unreadable ({_why(exc)})")
        digest = hashlib.sha256(data).hexdigest()
        target = _gitdir_text(data)
        if target is None:
            return digest
        admin, why = excluder.resolve(target, str(tree))
        if admin is None:
            if why == "excluded":
                raise _Gate("exclude-top", f".git names {target}, in an excluded repository")
            return digest
        try:
            if not stat.S_ISDIR(os.lstat(admin).st_mode):
                return digest
            text = _text(os.path.join(admin, "commondir"))
        except OSError:
            return digest
        common = admin
        if text is not None:
            common, why = excluder.resolve(text, admin)
            if common is None:
                if why == "excluded":
                    raise _Gate("exclude-top", f"its registration's repository {text} is in an excluded repository")
                return digest
        borrowed(common, excluder, "exclude-top", [])
        return digest

    def _salvage_refs(self, job_id: str) -> dict[str, str]:
        return {row["path"]: row["sha256"] for row in self.store.query(
            "SELECT r.path,r.sha256 FROM artifacts r JOIN attempts a USING(attempt_id) "
            "WHERE a.job_id=? AND r.role='salvage' ORDER BY r.artifact_id", (job_id,))}

    def _live_trees(self, excluder: Excluder) -> Callable[[Path], bool]:
        """As the survey's `live`: whether a tree is one some job's row or a
        retirement journal names, read once and only when asked, resolved
        without entering an excluded place."""
        found: list[set[str]] = []

        def live(host: Path) -> bool:
            if not found:
                trees = [row["worktree"] for row in self.store.query(
                    "SELECT worktree FROM jobs WHERE worktree IS NOT NULL")]
                trees += [j.get("worktree") for j in rarch.journals(self.root).values() if isinstance(j, dict)]
                spelled: set[str] = set()
                for tree in trees:
                    if isinstance(tree, str) and tree:
                        where, _ = excluder.resolve(tree, "/")
                        if where is not None:
                            spelled.add(where)
                found.append(spelled)
            return str(host) in found[0]
        return live

    def _registration(self, job: dict[str, Any], excluder: Excluder) -> tuple[str | None, bool]:
        """Where `nested_hosts` looks for the job's registration, and whether
        reading it there would enter an excluded place (or could not be
        resolved without risking it); the place is then spelled as recorded."""
        workdir = job.get("workdir")
        if isinstance(workdir, str) and workdir and excluder.covers(workdir):
            return os.path.normpath(workdir), True
        try:
            tree = ret._owned_worktree(job, self.root)
        except ValueError:
            return None, False
        if tree is None:
            return None, False
        if os.path.lexists(tree):
            try:
                data = _read_small(tree / ".git")
            except OSError:
                return None, False
            target = _gitdir_text(data)
            if target is None:
                return None, False
            where, why = excluder.resolve(target, str(tree))
            if where is None:
                return os.path.normpath(target if os.path.isabs(target) else os.path.join(str(tree), target)), True
            return where, False
        if isinstance(workdir, str) and workdir:
            where, why = excluder.resolve(workdir, "/")
            return (where, False) if where is not None else (os.path.normpath(workdir), True)
        return None, False

    def _hosted(self, job_id: str, tree: Path, excluder: Excluder) -> dict[str, set[str]]:
        """`nested_hosts` as the normal pass reads it, over every job but those
        whose registration or workdir lies in an excluded repository, whose
        gitfiles `nested_hosts` would resolve there. One of those counts as
        hosted by this tree when the place it names lies inside it."""
        passed: list[dict[str, Any]] = []
        guests: set[str] = set()
        for row in self.store.list_jobs():
            job = dict(row)
            place, excluded = self._registration(job, excluder)
            if not excluded:
                passed.append(job)
            elif place is not None and job["job_id"] != job_id and _inside(place, str(tree)):
                guests.add(job["job_id"])
        guests |= set(ret.nested_hosts(passed, self.root).get(job_id) or ())
        return {job_id: guests} if guests else {}

    def _pinned(self, job_id: str, folder: str, hosted: dict[str, set[str]]) -> str | None:
        legacy = dependencies.legacy(self.store.query)
        reason = ret._pin_reasons(self.store, self.explicit, None, pins=self.pins, turn_keep_s=self.turn_keep_s,
                                  only=job_id, hosted=hosted, root=self.root, legacy=legacy).get(job_id)
        if reason is not None:
            return reason
        read = self.store.query
        if folders.turn_holds(read, folder, inside=True):
            return "turn-folder"
        if dependencies.dependents(read, folder, exclude=job_id, legacy_places=legacy):
            return "worktree-in-use"
        holder = f"retention:{job_id}"
        if any(other != holder for _, other in folders.exclusive_inside(read, folder)):
            return "worktree-lease"
        return None

    def _holders(self, job_id: str, paths: list[str], identity: tuple[int, int] | None) -> None:
        prefixes = sorted({p for p in paths if p})
        try:
            pids: set[int] = set()
            for path in prefixes:
                if os.path.lexists(path):
                    pids |= set(self.cwd_pids(path))
            if pids:
                raise _Gate("holders", "; ".join(f"pid {pid}: cwd in the tree" for pid in sorted(pids)[:3]))
            busy = self.holders({job_id: Watch(prefixes=prefixes, inodes={identity} if identity else set())},
                                cancel=self.cancel)
        except (ScanFailed, procs.InspectionError, OSError) as exc:
            if self.cancel is not None and self.cancel.is_set():
                raise rarch.Interrupted("cancelled") from exc
            raise _Gate("holders", f"the holder scan failed: {exc}", disposition="skipped")
        if busy.get(job_id):
            raise _Gate("holders", "; ".join(busy[job_id][:3]))

    # what is kept -------------------------------------------------------------------

    def _plan_keep(self, plan: Layout) -> dict[str, Any]:
        outside = []
        for root in plan.outside:
            if not root.linked:
                continue                        # its git directory stays, and with it every record
            self._check()
            common = Path(root.common)
            fmt = rgit.object_format(common, cancel=self.cancel)
            commits = admin_commits(Path(root.git_dir), fmt, cancel=self.cancel)
            outside.append({"n": root.n, "rel": root.rel, "common": root.common, "git_dir": root.git_dir,
                            "fmt": fmt, "commits": sorted(commits),
                            "tips": independent(common, commits, cancel=self.cancel)})
        inside = []
        for repo in plan.inside:
            self._check()
            common = Path(repo.common)
            fmt = rgit.object_format(common, cancel=self.cancel)
            commits = repository_commits(common, fmt, cancel=self.cancel)
            tips = independent(common, commits, cancel=self.cancel)
            remotes = rgit.network_remotes(common, cancel=self.cancel)
            held = rgit.held_arguments(remotes)
            unheld = [tip for tip in tips if not rgit.held_commit(common, tip, held, cancel=self.cancel)]
            inside.append({"n": repo.n, "rels": repo.rels, "common": repo.common, "fmt": fmt,
                           "commits_digest": _digest(commits), "tips": tips, "unheld": unheld,
                           "held": held_objects(common, remotes, self.cancel) if unheld else [],
                           "remotes": sorted(remotes)})
        return {"outside": outside, "inside": inside}

    # the deletion --------------------------------------------------------------------

    def _journal_dir(self, job_id: str) -> Path:
        return self.base / job_id

    def _begin(self, job_id: str, request: Request, run: int, tree: Path, folder: str,
               keys: list[str]) -> dict[str, Any]:
        """The job's journal, written (`gating`) before any lease is taken."""
        self._ensure_base()
        work = self._journal_dir(job_id)
        work.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(os.lstat(work).st_mode):
            raise _Gate("finished", f"{work} is not a directory", disposition="failed")
        if os.path.lexists(work / TOMBSTONE):
            raise _Gate("finished", f"{work / TOMBSTONE} is there with no journal", disposition="failed")
        for name in os.listdir(work):           # what a `_drop` cut short left
            path = work / name
            if stat.S_ISDIR(os.lstat(path).st_mode):
                rfs.remove_own_tree(path)
            else:
                path.unlink()
        journal = {"schema": SCHEMA, "job_id": job_id, "state": None, "ruling": request.ruling,
                   "digest": request.digest, "run": run, "worktree": str(tree), "folder": folder, "keys": keys,
                   "holder": f"retention:{job_id}", "tombstone": str(work / TOMBSTONE),
                   "exclude_repositories": list(request.exclude_repositories), "started_at": _utc(self.now())}
        self._saved(journal, "gating")
        return journal

    def _saved(self, journal: dict[str, Any], state: str, **changes: Any) -> None:
        journal.update(changes, state=state, updated_at=_utc(self.now()))
        rfs.write_atomic(self._journal_dir(journal["job_id"]) / marks.JOURNAL, _canonical(journal))
        with self.lock:
            self.journals.add(journal["job_id"])
        self._hook(journal["job_id"], state)

    def _drop(self, journal: dict[str, Any]) -> None:
        job_id = journal["job_id"]
        work = self._journal_dir(job_id)
        if os.path.lexists(work / TOMBSTONE):
            raise OSError(errno.ENOTEMPTY, "the tombstone is still there", str(work / TOMBSTONE))
        try:
            names = os.listdir(work)
        except FileNotFoundError:
            names = []
        for name in names:
            if name == marks.JOURNAL:
                continue
            path = work / name
            if stat.S_ISDIR(os.lstat(path).st_mode):
                rfs.remove_own_tree(path)
            else:
                path.unlink()
        try:
            (work / marks.JOURNAL).unlink()
        except FileNotFoundError:
            pass
        try:
            os.rmdir(work)
        except FileNotFoundError:
            pass
        rfs.sync_path(self.base)
        with self.lock:
            self.journals.discard(job_id)
            self.retry_at.pop(job_id, None)

    def _rollback(self, journal: dict[str, Any]) -> dict[str, Any]:
        """Before `removing`: put the tree back whole, take away what this
        attempt added (its bundles, the refs it created), release its leases
        and drop the journal. Only putting the tree back must succeed."""
        job_id = journal["job_id"]
        tombstone, original = Path(journal["tombstone"]), Path(journal["worktree"])
        if os.path.lexists(tombstone):
            if os.path.lexists(original):
                raise OSError(errno.EEXIST, "the tree's place is taken; the tombstone stays", str(original))
            os.rename(tombstone, original)
            rfs.sync_path(original.parent)
            rfs.sync_path(tombstone.parent)
        notes: list[str] = []
        for path in [*(journal.get("tmp_bundles") or ()), *(journal.get("bundle_paths") or ())]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                notes.append(f"{path} stays ({_why(exc)})")
        for ref in journal.get("refs_written") or ():
            repository = Path(ref["repository"])
            try:
                current = rgit.run(["rev-parse", "--verify", "--quiet", ref["ref"]], git_dir=repository,
                                   ok=(0, 1)).stdout.decode().strip()
                if current == ref["commit"]:
                    rgit.run(["update-ref", "--no-deref", "-d", ref["ref"], ref["commit"]], git_dir=repository)
                elif current:
                    notes.append(f"{ref['ref']} in {ref['repository']} names {current}, not {ref['commit']}: "
                                 "it stays")
            except (rgit.GitError, OSError) as exc:
                notes.append(f"{ref['ref']} in {ref['repository']} stays ({type(exc).__name__})")
        # After the refs and bundles in them, deepest first: git prunes a ref's
        # empty folders when it deletes the ref, but never `refs/<namespace>`.
        for folder in reversed(journal.get("made_folders") or ()):
            try:
                os.rmdir(folder)
            except OSError:
                pass
        self._release(list(journal.get("keys") or ()), str(journal.get("holder") or f"retention:{job_id}"), job_id)
        self._drop(journal)
        return {"notes": notes}

    def _delete(self, journal: dict[str, Any], job: dict[str, Any], tree: Path, folder: str, plan: dict[str, Any],
                result: dict[str, Any], excluder: Excluder) -> dict[str, Any]:
        job_id = job["job_id"]
        scan: TreeScan = plan["scan"]
        self._saved(journal, "keeping", identity=list(scan.identity), gitfile=plan["top_digest"],
                    bytes={"logical_bytes": scan.logical, "allocated_bytes": scan.allocated, "entries": scan.entries},
                    free_before=plan["free_before"], **{"class": plan["class"]})
        kept = self._keep(journal, job_id, plan["keep"])
        admins = [{"admin": o["git_dir"], "common": o["common"], "n": o["n"], "rel": o["rel"]}
                  for o in plan["keep"]["outside"]]
        self._saved(journal, "kept", kept=kept, admins=admins,
                    plan={"outside": [{k: o[k] for k in ("n", "rel", "common", "git_dir", "fmt", "tips")}
                                      for o in plan["keep"]["outside"]],
                          "inside": [{k: b[k] for k in ("n", "rels", "common", "fmt", "commits_digest")}
                                     for b in plan["keep"]["inside"]]})
        result["keep"] = kept
        self._check()
        why = self._pinned(job_id, folder, plan["hosted"])
        if why is not None:
            raise _Gate("pins", f"{why} (asked again before the rename)")
        tombstone = Path(journal["tombstone"])
        self._saved(journal, "renaming")
        try:
            os.rename(tree, tombstone)
        except FileNotFoundError:
            raise _Gate("owned-path", f"{tree} went before it was renamed")
        rfs.sync_path(tombstone.parent)
        rfs.sync_path(tree.parent)
        self._hook(job_id, "renamed-unsaved")
        self._saved(journal, "renamed")
        self._after_rename(journal, job_id, tree, tombstone, plan)
        self._check()
        why = self._pinned(job_id, folder, plan["hosted"])
        if why is not None:
            raise _Gate("pins", f"{why} (asked again before the removal)")
        self._saved(journal, "checked")
        self._saved(journal, DESTRUCTIVE)
        return self._finish_deletion(journal, result)

    def _keep(self, journal: dict[str, Any], job_id: str, keep: dict[str, Any]) -> dict[str, Any]:
        """Write and read back the refs, then make and verify the bundles; all of
        it finishes before the journal records `kept`."""
        refs: list[dict[str, Any]] = []
        created = list(journal.get("refs_written") or ())
        for root in keep["outside"]:
            self._check()
            if not root["tips"]:
                continue                        # an unborn HEAD with no reflog: nothing to keep
            written = self._write_refs(Path(root["common"]), job_id, root["n"], root["tips"], journal=journal,
                                       created=created)
            refs += written
        bundles: list[dict[str, Any]] = []
        for repo in keep["inside"]:
            self._check()
            if repo["unheld"]:
                bundles.append(self._bundle(journal, job_id, repo))
        return {"refs": refs, "bundles": bundles}

    def _write_refs(self, common: Path, job_id: str, n: int, tips: list[str], *, journal: dict[str, Any],
                    created: list[dict[str, Any]], late: bool = False) -> list[dict[str, Any]]:
        """A ref under `refs/subfleet-kept/<job>/<n>/` for each tip, each read
        back. Each ref is recorded in the journal (`refs_written`) before it is
        made, so a rollback finds it; `update-ref -d` with the commit removes
        only a ref that still names it."""
        prefix = f"{KEPT_NAMESPACE}/{job_id}/{n}/"
        existing = rgit.refs_under(common, prefix, cancel=self.cancel)
        have = {commit: ref for ref, commit in sorted(existing.items())}
        used = [int(ref[len(prefix):]) for ref in existing if ref[len(prefix):].isdigit()]
        m = max(used) + 1 if used else 0
        written: list[dict[str, Any]] = []
        for tip in tips:
            ref = have.get(tip)
            if ref is None:
                ref = prefix + str(m)
                m += 1
                if not late:
                    created.append({"repository": str(common), "ref": ref, "commit": tip})
                    # The folders the loose ref will make, so a rollback takes them away too.
                    made = journal.setdefault("made_folders", [])
                    made += [str(f) for f in _ref_folders(common, ref) if str(f) not in made and not os.path.lexists(f)]
                    self._saved(journal, journal["state"], refs_written=created)
                rgit.create_ref(common, ref, tip, cancel=self.cancel)
            if rgit.resolve(common, ref, cancel=self.cancel) != tip:
                raise rgit.GitError(f"{ref} in {common} does not read back as {tip}")
            written.append({"repository": str(common), "n": n, "ref": ref, "commit": tip,
                            **({"late": True} if late else {})})
        return written

    def _bundle(self, journal: dict[str, Any], job_id: str, repo: dict[str, Any]) -> dict[str, Any]:
        """A bundle of every commit the repository's tips reach that no network
        remote holds, made from a scratch repository that borrows the
        repository's objects (nothing is written into the tree), verified by
        `rgit.verify_bundle`, then moved into `<state>/jobs/<job>/kept-commits/`."""
        work = self._journal_dir(job_id)
        n = repo["n"]
        scratch = work / f"bundle-{n}.git"
        verify = work / f"verify-{n}.git"
        rfs.remove_own_tree(scratch)
        rgit.run(["init", "--quiet", "--bare", f"--object-format={repo['fmt']}", str(scratch)], timeout=120,
                 cancel=self.cancel)
        try:
            objects = os.path.realpath(os.path.join(repo["common"], "objects"))
            (scratch / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (scratch / "objects" / "info" / "alternates").write_text(objects + "\n")
            expected = {f"refs/kept/{i}": tip for i, tip in enumerate(repo["unheld"])}
            updates = [f"create {ref} {tip}\n" for ref, tip in expected.items()]
            updates += [f"create refs/held/{j} {oid}\n" for j, oid in enumerate(repo["held"])]
            rgit.run(["update-ref", "--stdin"], git_dir=scratch, stdin="".join(updates).encode(), timeout=600,
                     cancel=self.cancel)
            folder = self.root / "jobs" / job_id / "kept-commits"
            if not folder.exists():
                journal.setdefault("made_folders", []).append(str(folder))
                self._saved(journal, journal["state"])
                folder.mkdir(mode=0o700)
            final = folder / f"{n}.bundle"
            temporary = folder / f".{n}.bundle.tmp"
            journal.setdefault("tmp_bundles", []).append(str(temporary))
            journal.setdefault("bundle_paths", []).append(str(final))
            self._saved(journal, journal["state"])
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            rgit.create_bundle(scratch, temporary, sorted(expected), ["--glob=refs/held/*"] if repo["held"] else [],
                               cancel=self.cancel)
            rgit.verify_bundle(scratch, temporary, expected, repo["fmt"], verify, cancel=self.cancel)
            os.replace(temporary, final)
            rfs.sync_path(folder)
            journal["tmp_bundles"].remove(str(temporary))
            digest = hashlib.sha256()
            with open(final, "rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            return {"n": n, "repository": repo["common"], "path": str(final), "bytes": os.lstat(final).st_size,
                    "sha256": digest.hexdigest(), "heads": expected, "prerequisites_from": repo["remotes"]}
        finally:
            rfs.remove_own_tree(scratch)
            rfs.remove_own_tree(verify)

    def _after_rename(self, journal: dict[str, Any], job_id: str, tree: Path, tombstone: Path,
                      plan: dict[str, Any]) -> None:
        """The tree in the tombstone must be the one gated and kept: the same
        directory, the same top-level gitfile, no holder, and every root's
        commits the ones kept."""
        st = os.lstat(tombstone)
        if (st.st_dev, st.st_ino) != tuple(plan["scan"].identity):
            raise _Gate("owned-path", "the tree renamed is not the directory that was gated")
        top = tombstone / ".git"
        try:
            top_st = os.lstat(top)
        except FileNotFoundError:
            top_st = None
        digest = hashlib.sha256(_read_small(top)).hexdigest() \
            if top_st is not None and stat.S_ISREG(top_st.st_mode) else None
        if digest != plan["top_digest"]:
            raise _Gate("exclude-top", "the tree's .git changed after it was read")
        self._holders(job_id, [str(tombstone), str(tree), journal["folder"]], plan["scan"].identity)
        for root in plan["keep"]["outside"]:
            self._check()
            now = admin_commits(Path(root["git_dir"]), root["fmt"], cancel=self.cancel)
            if now - set(root["commits"]):
                raise _Gate("keep", f"{root['rel']}'s registration names commits made after they were kept",
                            disposition="failed")
        for repo in plan["keep"]["inside"]:
            self._check()
            moved = Path(str(tombstone) + repo["common"][len(str(tree)):])
            if _digest(repository_commits(moved, repo["fmt"], cancel=self.cancel)) != repo["commits_digest"]:
                raise _Gate("keep", f"{repo['common']} changed after its commits were kept", disposition="failed")

    def _finish_deletion(self, journal: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
        """From `removing` on: remove the tombstone and the admin directories,
        write the marker and the event, release the leases, drop the journal."""
        job_id = journal["job_id"]
        state = journal["state"]
        tombstone = Path(journal["tombstone"])
        if state == DESTRUCTIVE:
            removed = remove_tree(tombstone, check=lambda: self._hook(job_id, "removing-entry"))
            rfs.sync_path(tombstone.parent)
            self._saved(journal, "removed", removed_entries=removed)
            state = "removed"
        if state == "removed":
            self._saved(journal, "admins", admin_report=self._remove_admins(journal))
            state = "admins"
        if state == "admins":
            self._saved(journal, "marking")
            state = "marking"
        if state == "marking":
            marker = self._marker(journal)
            rfs.write_atomic(marks.marker_path(self.root, job_id), _canonical(marker))
            self._hook(job_id, "marker-written")
            self._record_event(job_id, marker)
            self._saved(journal, "marked", ended_at=marker["ended_at"], free_after=marker["free_after"])
            state = "marked"
        if state == "marked":
            self._release(list(journal.get("keys") or ()), str(journal.get("holder") or f"retention:{job_id}"),
                          job_id)
            self._drop(journal)
        out = result if result is not None else {"job_id": job_id, "run": journal.get("run"), "gate": None,
                                                 "reason": None}
        out.update(disposition="deleted", ended_at=journal.get("ended_at") or _utc(self.now()),
                   free_before=journal.get("free_before"), free_after=journal.get("free_after"),
                   keep=journal.get("kept") or out.get("keep"), bytes=journal.get("bytes") or out.get("bytes"),
                   admins=journal.get("admin_report") or {})
        out.setdefault("class", journal.get("class"))
        return out

    def _record_event(self, job_id: str, marker: dict[str, Any]) -> None:
        """The job's one `retention.tree_deleted` event, carrying the marker.
        The look for an earlier one and the write share a transaction, so a
        restart that finishes the deletion again never writes a second. The
        block writes its own row and turns the transaction's audit row off, as
        the daemon's hold writes do: `Store.add_event` would add an empty audit
        row of the same kind after it."""
        with self.store.transaction(EVENT, job_id=job_id) as tx:
            self.store.audit_as(None)
            if tx.execute("SELECT 1 FROM events WHERE kind=? AND job_id=? LIMIT 1", (EVENT, job_id)).fetchone():
                return
            tx.execute("INSERT INTO events(ts,kind,job_id,attempt_id,lane_id,data_json) VALUES (?,?,?,?,?,?)",
                       (_utc(self.now()), EVENT, job_id, None, None,
                        json.dumps(marker, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))

    def _marker(self, journal: dict[str, Any]) -> dict[str, Any]:
        kept = journal.get("kept") or {}
        report = journal.get("admin_report") or {}
        refs = [*(kept.get("refs") or ()), *(report.get("late_refs") or ())]
        return {"schema": SCHEMA, "job_id": journal["job_id"], "ruling": journal["ruling"],
                "request_digest": journal.get("digest"), "worktree": journal["worktree"],
                "class": journal.get("class"), "started_at": journal["started_at"], "ended_at": _utc(self.now()),
                **(journal.get("bytes") or {}), "free_before": journal.get("free_before"),
                "free_after": _free(self.root),
                "refs": [{k: r[k] for k in ("repository", "n", "ref", "commit")} | ({"late": True} if r.get("late") else {})
                         for r in refs],
                "bundles": [{k: b[k] for k in ("n", "repository", "path", "bytes", "sha256", "heads",
                                                "prerequisites_from")}
                            for b in kept.get("bundles") or ()],
                "admins_removed": report.get("removed") or [], "admins_kept": report.get("kept") or []}

    def _remove_admins(self, journal: dict[str, Any]) -> dict[str, Any]:
        """Each linked-worktree registration the tree held, as `_reclaim` and
        `_late_admin` treat a retired job's: one whose `gitdir` no longer names
        a place in the removed tree, that a tree names again, or that someone
        locked stays; one naming commits made since they were kept gets refs
        for them and stays; the rest are removed."""
        job_id = journal["job_id"]
        original = journal["worktree"]
        excluder = Excluder(journal.get("exclude_repositories") or ())
        plan = {o["n"]: o for o in (journal.get("plan") or {}).get("outside") or ()}
        kept_refs = (journal.get("kept") or {}).get("refs") or ()
        removed: list[str] = []
        kept: list[dict[str, str]] = []
        late: list[dict[str, Any]] = []
        for item in journal.get("admins") or ():
            admin = item["admin"]
            try:
                if not stat.S_ISDIR(os.lstat(admin).st_mode):
                    kept.append({"admin": admin, "why": "not a directory"})
                    continue
            except FileNotFoundError:
                continue
            try:
                back = _text(os.path.join(admin, "gitdir"))
            except OSError as exc:
                kept.append({"admin": admin, "why": f"its gitdir is unreadable ({_why(exc)})"})
                continue
            where, _ = excluder.resolve(back, admin) if back else (None, None)
            if where is None or not _inside(where, original):
                kept.append({"admin": admin, "why": f"its gitdir names {back}, not a place in the removed tree"})
                continue
            claimed = self._claimed(admin, where, original, excluder)
            if claimed is not None:
                kept.append({"admin": admin, "why": f"{claimed} names it again"})
                continue
            if os.path.lexists(os.path.join(admin, "locked")):
                kept.append({"admin": admin, "why": "locked"})
                continue
            common = Path(item["common"])
            root = plan.get(item["n"]) or {}
            # Only what a written ref holds counts as kept, never what the plan
            # meant to keep: a commit the keep step missed gets a late ref here,
            # and the registration stays.
            tips = {r["commit"] for r in kept_refs if r["repository"] == item["common"] and r.get("n") == item["n"]}
            try:
                fmt = root.get("fmt") or rgit.object_format(common, cancel=self.cancel)
                fresh = admin_commits(Path(admin), fmt, cancel=self.cancel) - tips
                new = sorted(set(independent(common, fresh | tips, cancel=self.cancel)) - tips) if fresh else []
                if new:
                    late += self._write_refs(common, job_id, item["n"], new, journal=journal, created=[], late=True)
            except (rgit.GitError, OSError) as exc:
                kept.append({"admin": admin, "why": f"its commits could not be read ({type(exc).__name__})"})
                continue
            if new:
                kept.append({"admin": admin, "why": "it names commits made after they were kept: refs keep them"})
                continue
            remove_tree(Path(admin))
            rfs.sync_path(Path(admin).parent)
            removed.append(admin)
        return {"removed": removed, "kept": kept, "late_refs": late}

    def _claimed(self, admin: str, where: str, original: str, excluder: Excluder) -> str | None:
        """A tree that names `admin` again (`Retirement._claimed`): at the tree's
        original path, moved aside beside it (`tree_away`), or where the
        registration's backlink says it is."""
        away = rarch.tree_away(Path(original))
        for place in (original, str(away) if away is not None else None, os.path.dirname(where)):
            if place is None:
                continue
            try:
                data = _read_small(os.path.join(place, ".git"))
            except OSError:
                continue
            target = _gitdir_text(data)
            if target is None:
                continue
            named, _ = excluder.resolve(target, place)
            if named is not None and _same(named, admin):
                return place
        return None

    # resume ----------------------------------------------------------------------------

    def _resume(self, job_id: str) -> dict[str, Any]:
        """What a killed daemon left of one job: before `removing`, the tree goes
        back whole and the job is recorded `interrupted`; from `removing` on,
        the deletion is finished."""
        path = self._journal_dir(job_id) / marks.JOURNAL
        try:
            journal = json.loads(rfs.read_regular(path, limit=16 << 20))
        except FileNotFoundError:
            with self.lock:
                self.journals.discard(job_id)
            return {"gone": True}
        except (OSError, ValueError) as exc:
            self.retry_at[job_id] = self.clock() + RETRY_S
            return {"error": f"journal unreadable: {type(exc).__name__}"}
        if not isinstance(journal, dict) or journal.get("job_id") != job_id or journal.get("schema") != SCHEMA \
                or journal.get("state") not in STATES:
            self.retry_at[job_id] = self.clock() + RETRY_S
            return {"error": "journal is not one this path wrote"}
        state = journal["state"]
        if _before_destructive(journal):
            try:
                rolled = self._rollback(journal)
            except (OSError, rgit.GitError) as exc:
                self.retry_at[job_id] = self.clock() + RETRY_S
                return {"error": f"rollback: {type(exc).__name__}: {exc}"[:300], "state": state}
            self._interrupted(job_id, state, rolled.get("notes") or [])
            return {"rolled_back": state, **({"notes": rolled["notes"]} if rolled.get("notes") else {})}
        try:
            result = self._finish_deletion(journal)
        except (OSError, rgit.GitError) as exc:
            self.retry_at[job_id] = self.clock() + RETRY_S
            return {"error": f"finish: {type(exc).__name__}: {exc}"[:300], "state": journal.get("state")}
        self._resumed(job_id, result)
        return {"finished": state}

    def _interrupted(self, job_id: str, state: str, notes: list[str]) -> None:
        """A job a restart interrupted before its destructive step: its tree is
        whole again, and it is not tried again in this run."""
        with self.lock:
            record = self.apply
            if record is None or record.get("status") not in ("running", "stopping") \
                    or job_id not in (record.get("results") or {}) or record["results"].get(job_id) is not None:
                return
            record["results"][job_id] = {"job_id": job_id, "run": record.get("run"), "disposition": "interrupted",
                                         "gate": None, "reason": f"the daemon stopped at {state}; the tree is whole",
                                         "ended_at": _utc(self.now()),
                                         **({"rollback_notes": notes} if notes else {})}
            self._save()

    def _resumed(self, job_id: str, result: dict[str, Any]) -> None:
        with self.lock:
            record = self.apply
            if record is None or job_id not in (record.get("results") or {}):
                return
            previous = record["results"].get(job_id)
            if isinstance(previous, dict) and previous.get("disposition") == "deleted":
                return
            record["results"][job_id] = {**(previous if isinstance(previous, dict) else {}), **result,
                                         "run": record.get("run"), "resumed": True}
            if record.get("status") in ("running", "stopping"):
                record["deleted_this_run"] = int(record.get("deleted_this_run") or 0) + 1
            self._save()


ACTIONS = ("dry-run", "apply", "status", "stop")


def handle(path: DeletePath, args: Any, *, dormant: bool) -> dict[str, Any]:
    """The daemon's `retention.delete` op (C-8.7): `dry-run` and `apply` start a
    request, `status` reads the records, `stop` ends the running one at its next
    job boundary. The request is checked again here (`parse_request`); its
    words go to the daemon log, and the wire shows the reason's template."""
    action = getattr(args, "action", None)
    if action not in ACTIONS:
        raise ProtocolError("retention delete: action must be dry-run, apply, status or stop", Exit.INVALID_INPUT,
                            reason="retention-delete-action")
    if action == "status":
        return path.status()
    if action == "stop":
        return path.stop()
    if dormant:
        raise ProtocolError("retention delete: retention is dormant (SUBFLEET_RETENTION_DORMANT=1)", Exit.REFUSED,
                            reason="retention-delete-dormant")
    limit = getattr(args, "limit", None)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ProtocolError("retention delete: --limit must be a positive integer", Exit.INVALID_INPUT,
                            reason="retention-delete-limit")
    try:
        request = parse_request(getattr(args, "request", None))
    except RequestError as exc:
        raise ProtocolError(f"retention delete: {exc}", Exit.INVALID_INPUT,
                            "check it with subfleet retention delete --request FILE --dry-run, which names the field",
                            reason="retention-delete-request") from None
    try:
        return path.start(request, mode=action, limit=limit if action == "apply" else None)
    except Busy:
        raise ProtocolError("retention delete: a request is already running", Exit.REFUSED,
                            "wait for it, or stop it with subfleet retention delete --stop",
                            reason="retention-delete-busy") from None


def _before_destructive(journal: dict[str, Any]) -> bool:
    state = journal.get("state")
    return state is None or STATES.index(state) < STATES.index(DESTRUCTIVE)


def remove_tree(path: Path, check: Callable[[], None] | None = None) -> int:
    """Remove `path` and everything below it by descriptor, as
    `rfs.remove_own_tree` does (each directory gets owner read, write and
    search first, so odd modes are handled), and never crossing a device: a
    mount point inside raises `EXDEV`. `check()` runs before each entry. The
    entries removed; an absent path removes nothing."""
    with rfs._cleanup([None]) as owner:
        try:
            owner.resources[0] = rfs.open_dir(path)
        except FileNotFoundError:
            return 0
        fd = owner.resources[0]
        count = _remove_children(fd, os.fstat(fd).st_dev, check)
    os.rmdir(path)
    return count + 1


def _remove_children(fd: int, device: int, check: Callable[[], None] | None) -> int:
    os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) | 0o700)
    count = 0
    for name in os.listdir(fd):
        if check is not None:
            check()
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if st.st_dev != device:
            raise OSError(errno.EXDEV, "a mount point inside the tree", name)
        if stat.S_ISDIR(st.st_mode):
            with rfs._cleanup([None]) as owner:
                owner.resources[0] = rfs.open_dir(name, dir_fd=fd, expect=st)
                count += _remove_children(owner.resources[0], device, check)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
        count += 1
    return count
