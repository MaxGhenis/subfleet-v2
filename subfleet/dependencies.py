"""C-8.4: the places a job needs besides its folder, kept while it needs them.

A job works in its folder (C-6.5: a writer's write target, or the folder submit
recorded, `folders.canonical`). Retention keeps a finished job's tree while a job
that has not ended works in it (`worktree-in-use`), and admission starts no job
in a tree retention is retiring (`folders.retiring`). A job can need other places
than its folder:

- an isolated review's root (`-I -D`, `jobs.review_root`), which it reads; `gate
  pr` sets it to the pull request's checkout;
- its `-o` path, which the daemon writes the accepted deliverable to after the
  job ends, while the job still holds `out:<path>`;
- the git storage its folder's checkout uses. `git -C <tree>/vendor/lib worktree
  add <outside>` makes a checkout outside the tree whose repository, and so every
  git command run there, is inside it: a worktree writer submitted from there
  registers its new worktree in that repository (N4).

Submit records them, spelled once (`record`, in `job.submitted` as `depends`).
Admission reads retention's fence on each (`fenced`), before it prepares the
workspace and again in the reserving transaction. Retention keeps a tree while a
job that has not ended needs a place in it, and while an export to a path in it
is pending (`live`, `dependents`, `users`): database reads and string operations
alone, so the selecting and committing transactions read them too. Paths are
compared `folders.fold`ed, so a spelling in another case or Unicode form than
the volume stores, which a path given before it was recorded keeps, is the same
path (the rows' `workdir`, `review_root` and `out_path` are `Path.resolve`d,
never spelled, C-6.5).

A job a daemon queued before it recorded `depends` has them read for it: by
admission when it looks at the job, and by retention once a pass, outside any
transaction (`legacy`), since every such job is older than the pass.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import folders
from .sessions.transcripts import open_regular, read_regular

#: What a reader is given: `read(sql, params)` runs one statement, as for
#: `folders.turn_holds` (a store's `query`, or a transaction's fetchall).
Read = Callable[[str, tuple], Iterable[Any]]

#: The `depends` roles, as a hold names them (C-6.11) and `why` words them.
REVIEW_ROOT, OUTPUT, GIT = "review-root", "output", "git-storage"

#: The most a gitfile or `commondir` file is read: Git writes a single path.
_FILE_LIMIT = 64 * 1024
#: Bound only one alternates line, not the file: 256 times a 4096-byte PATH_MAX.
#: This allows even heavily escaped paths while bounding the streaming read buffer.
_ALTERNATE_LINE_LIMIT = 1024 * 1024
#: How deep alternates are followed; git follows them five levels deep.
_ALTERNATES_DEPTH = 5

_TERMINAL = "('succeeded','failed','cancelled','lost')"
#: The first `job.submitted` of a job, by its events in order (`events_job`;
#: `+kind` keeps SQLite off `events_kind`, which walks every submit ever made).
_SUBMITTED = ("(SELECT e.data_json FROM events e WHERE e.job_id = {job} AND +e.kind = 'job.submitted' "
              "ORDER BY e.event_id LIMIT 1)")
#: Jobs that have not ended, with what submit recorded beside each.
_LIVE = ("SELECT job_id, workdir, worktree, review_root, out_path, "
         + _SUBMITTED.format(job="jobs.job_id") + " AS submitted "
         f"FROM jobs WHERE state NOT IN {_TERMINAL}")
#: Legacy discovery includes terminal jobs for as long as they hold an export.
_DISCOVERING = (_LIVE + " OR job_id IN (SELECT holder FROM leases "
                "WHERE lease_key >= 'out:' AND lease_key < 'out;')")
#: Exports still to be written: a job holds `out:<path>` from its reservation
#: until its export is written or it ends without one (`Daemon._export`).
_EXPORTING = ("SELECT holder AS job_id, substr(lease_key, 5) AS out_path, jobs.out_path AS job_out_path, "
              + _SUBMITTED.format(job="leases.holder") + " AS submitted "
              "FROM leases LEFT JOIN jobs ON jobs.job_id = leases.holder "
              "WHERE lease_key >= 'out:' AND lease_key < 'out;'")
#: A legacy job's folder as admission spelled it (`Daemon._detached_folder`).
_SPELLED = "SELECT data_json FROM events WHERE job_id = ? AND +kind = 'job.folder_spelled' ORDER BY event_id"


def key(path: str) -> str:
    """`path` as dependencies compare it: normalized, then `folders.fold`ed."""
    return folders.fold(os.path.normpath(path))


# --- what a job needs, read at submit -----------------------------------------------

def record(workdir: str, *, review_root: str | None = None, out_path: str | None = None) -> dict[str, Any]:
    """What submit records as `depends` (C-8.4): the review root and the `-o` path,
    spelled once (`folders.canonical`), and the git storage the workdir's checkout
    and the review root's use (`git_storage`). Filesystem work: never inside a
    store transaction."""
    found: dict[str, Any] = {}
    if review_root:
        found["review_root"] = folders.canonical(review_root)
    if out_path:
        found["out_path"] = folders.canonical(out_path)
    incomplete: list[dict[str, str]] = []
    git = git_storage(workdir, incomplete=incomplete) + (
        git_storage(review_root, incomplete=incomplete) if review_root else [])
    if git:
        found["git"] = list(dict.fromkeys(git))
    if incomplete:
        found["incomplete"] = incomplete
    return found


def git_storage(path: str, *, incomplete: list[dict[str, str]] | None = None) -> list[str]:
    """The git storage a checkout at `path`, or a folder in one, uses: for each
    `.git` at or above `path` (git uses the nearest a repository answers for;
    each one above is kept too, which can only keep more), the directory it is
    or names, that directory's common directory, and the object directories its
    alternates name, each spelled once (`folders.canonical`).

    Read from the files git reads, without running git, so it costs a few stats
    and no process, and a `.git` file that names a directory no longer there
    still names it: a linked checkout's `gitdir:` (relative to the folder that
    holds it), its admin directory's `commondir` (relative to that directory;
    with no `commondir`, an admin directory under `worktrees/` belongs to the
    repository two levels up, as `retention_git.repository_near` reads it), and
    `objects/info/alternates` (relative to the objects directory), followed five
    levels deep as git follows them. Empty outside a checkout. What this cannot
    read is left out: environment variables (`GIT_DIR`, `GIT_COMMON_DIR`) and
    `core.worktree`, which a job's folder does not set."""
    aliases = _Aliases()
    try:
        start = aliases.resolve(os.path.expanduser(path))
    except (OSError, ValueError):
        return []
    found: list[str] = []
    for folder in (start, *folders.above(start)):
        dotgit = os.path.join(folder, ".git")
        try:
            resolved = aliases.resolve(dotgit)
            mode = os.stat(dotgit).st_mode
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(mode):
            found.extend(_storage(resolved, named=False, incomplete=incomplete, aliases=aliases))
        elif stat.S_ISREG(mode):
            gitdir = _gitfile(dotgit, folder, aliases=aliases)
            if gitdir is not None:
                found.extend(_storage(gitdir, named=True, incomplete=incomplete, aliases=aliases))
    aliases.check(incomplete)
    return list(dict.fromkeys(folders.canonical(each) for each in found))


def _text(path: str) -> str | None:
    """A small regular file's text, or None when it cannot be read as one."""
    try:
        return os.fsdecode(read_regular(path, limit=_FILE_LIMIT))
    except (OSError, ValueError):
        return None


class _Aliases:
    """Original path and pointer spellings beside the targets discovery used.

    Revalidate only after traversal, outside store transactions. This observes
    a changed final target, not an alias swapped away and back within the read.
    """

    def __init__(self):
        self.paths: dict[str, tuple] = {}
        self.pointers: dict[str, str | None] = {}
        self.errors: list[dict[str, str]] = []

    @staticmethod
    def _target(path: str) -> tuple:
        resolved = os.path.realpath(path)
        try:
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
        except FileNotFoundError:
            identity = None        # a gitdir target may already be absent
        return resolved, identity

    def resolve(self, path: str) -> str:
        try:
            target = self._target(path)
        except (OSError, ValueError) as exc:
            self.errors.append({"file": path, "reason": str(exc)})
            target = (os.path.abspath(path), None)
        self.paths.setdefault(path, target)
        return target[0]

    def text(self, path: str) -> str | None:
        self.resolve(path)
        value = _text(path)
        self.pointers.setdefault(path, value)
        return value

    def check(self, incomplete: list[dict[str, str]] | None) -> None:
        if incomplete is not None:
            incomplete.extend(self.errors)
        for path, before in self.paths.items():
            try:
                if self._target(path) == before:
                    continue
                reason = "repository alias changed or vanished during discovery"
            except (OSError, ValueError) as exc:
                reason = str(exc)
            if incomplete is not None:
                incomplete.append({"file": path, "reason": reason})
        for path, before in self.pointers.items():
            if _text(path) != before and incomplete is not None:
                incomplete.append({"file": path, "reason": "repository pointer changed during discovery"})


def _gitfile(dotgit: str, folder: str, *, aliases: _Aliases) -> str | None:
    """The directory a `.git` file names (`gitdir: <path>`), or None."""
    text = (aliases.text(dotgit) or "").strip()
    if not text.startswith("gitdir:"):
        return None
    named = text[len("gitdir:"):].strip()
    return aliases.resolve(os.path.join(folder, named)) if named else None


def _storage(gitdir: str, *, named: bool, incomplete: list[dict[str, str]] | None = None,
             aliases: _Aliases) -> list[str]:
    """`gitdir`, its common directory, and the alternates of that repository's objects."""
    common = gitdir
    pointer = aliases.text(os.path.join(gitdir, "commondir"))
    if pointer is not None and pointer.strip():
        common = aliases.resolve(os.path.join(gitdir, pointer.strip()))
    elif named and os.path.basename(os.path.dirname(gitdir)) == "worktrees":
        common = os.path.dirname(os.path.dirname(gitdir))
    return list(dict.fromkeys([gitdir, common, *_alternates(os.path.join(common, "objects"), incomplete=incomplete, aliases=aliases)]))


def _unquote(line: bytes) -> tuple[bytes, int]:
    """Git's C quoting: decode bytes, returning the offset after the closing quote."""
    escapes = {ord(k): v for k, v in zip('abtnvfr"\\', (7, 8, 9, 10, 11, 12, 13, 34, 92))}
    decoded = bytearray()
    i = 1
    while i < len(line):
        char = line[i]
        i += 1
        if char == ord('"'):
            return bytes(decoded), i
        if char == ord('\\'):
            if i >= len(line):
                break
            char = line[i]
            i += 1
            if char in escapes:
                char = escapes[char]
            elif ord('0') <= char <= ord('3') and len(line) >= i + 2 and all(
                    ord('0') <= c <= ord('7') for c in line[i:i + 2]):
                char = int(bytes([char]) + line[i:i + 2], 8)
                i += 2
            else:
                raise ValueError("invalid Git C escape")
        decoded.append(char)
    raise ValueError("unterminated Git C quote")


def _alternate_paths(line: bytes) -> Iterable[bytes]:
    """Parse an alternates line without stripping path bytes.

    Git ignores a comment only at the start of an entry. After a C-quoted
    entry it skips one separator byte, then parses any remaining entry too.
    """
    while line and not line.startswith(b"#"):
        if line.startswith(b'"'):
            path, end = _unquote(line)
            yield path
            line = line[end + 1:]
        else:
            yield line
            return


def _alternates(objects: str, *, incomplete: list[dict[str, str]] | None = None,
                aliases: _Aliases | None = None) -> list[str]:
    """Stream complete alternates files, at most one bounded byte line at a time.

    The returned paths and traversal frontier grow with distinct dependencies;
    the input buffer is bounded independently of the size of the file.
    Failures are recorded separately, never interpreted as an empty file.
    """
    own_aliases = aliases is None
    aliases = aliases if aliases is not None else _Aliases()
    found: list[str] = []
    seen = {objects}
    level = [objects]
    for _ in range(_ALTERNATES_DEPTH):
        following: list[str] = []
        for directory in level:
            filename = os.path.join(directory, "info", "alternates")
            opened = False
            try:
                with open_regular(filename) as stream:
                    opened = True
                    before = os.fstat(stream.fileno())
                    while line := stream.readline(_ALTERNATE_LINE_LIMIT + 1):
                        if len(line) > _ALTERNATE_LINE_LIMIT:
                            raise ValueError(f"line exceeds {_ALTERNATE_LINE_LIMIT} bytes")
                        for path in _alternate_paths(line.removesuffix(b"\n")):
                            each = aliases.resolve(os.path.join(directory, os.fsdecode(path)))
                            if each not in seen:
                                seen.add(each)
                                found.append(each)
                                following.append(each)
                    after = os.fstat(stream.fileno())
                    named = os.stat(filename)
                    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                    if (any(getattr(before, field) != getattr(after, field) for field in fields)
                            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)):
                        raise ValueError("alternates changed during discovery")
            except (OSError, ValueError) as exc:
                # A missing file is normal, but a dangling link exists and is unreadable.
                if isinstance(exc, FileNotFoundError) and not opened and not os.path.lexists(filename):
                    continue
                if incomplete is not None:
                    incomplete.append({"file": filename, "reason": str(exc)})
        level = following
    if own_aliases:
        aliases.check(incomplete)
    return found


# --- what admission fences -----------------------------------------------------------

def needed(submitted: Mapping[str, Any]) -> list[tuple[str, str]] | None:
    """`(role, path)` of each place `depends` records, in the order a hold names
    them, or None for a job queued before `depends` was recorded."""
    depends = submitted.get("depends")
    if not isinstance(depends, dict):
        return None
    found = [(REVIEW_ROOT, depends.get("review_root")), (OUTPUT, depends.get("out_path")),
             *((GIT, each) for each in depends.get("git") or ())]
    return [(role, path) for role, path in found if isinstance(path, str) and path]


def fenced(submitted: Mapping[str, Any], job: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The places besides its folder on which admission reads retention's fence
    for `job`: what submit recorded, or, for a job queued before that was
    recorded, the same read now (filesystem work: outside any transaction)."""
    found = needed(submitted)
    if found is None:
        found = needed({"depends": record(job["workdir"], review_root=job.get("review_root"),
                                          out_path=job.get("out_path"))}) or []
    return found


def fences(read: Read, places: Iterable[tuple[str | None, str]]) -> list[tuple[str | None, str, list[str]]]:
    """`(role, path, keys)` for each of `places` retention's fence holds: the
    `worktree:` keys retention holds on that path or a folder above it, nearest
    first (`folders.retiring`), all read in one indexed range statement. Compare
    `key`ed paths as retention's pins do: quarantine can leave recorded dependency
    names unspellable, including the parent of an output not yet written."""
    places = [(role, path) for role, path in places if path]
    if not places:
        return []
    held = [(lease, key(lease[len(folders.EXCLUSIVE):]))
            for lease, holder in map(folders._row, read(
                "SELECT lease_key, holder FROM leases WHERE lease_key >= ? AND lease_key < ?",
                (folders.EXCLUSIVE, folders.EXCLUSIVE[:-1] + ";")))
            if str(holder or "").startswith(folders.RETENTION)]
    found = []
    for role, path in places:
        mine = sorted((lease for lease, tree in held if folders.within(key(path), tree)),
                      key=lambda lease: (-lease.count("/"), lease))
        if mine:
            found.append((role, path, mine))
    return found


# --- what retention keeps -------------------------------------------------------------

def _loads(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class Places(dict):
    """A path census with incomplete discoveries kept beside, never among, paths.

    `legacy` fills this outside transactions; `live` carries forward failures
    only while the relevant job is live or exporting, using database reads alone.
    """

    def __init__(self):
        super().__init__()
        self.incomplete: dict[str, list[dict[str, str]]] = {}


def legacy(read: Read) -> Places:
    """job id -> the places a live or exporting job, queued before submit
    recorded `depends`, needs (`record`, read now). Filesystem work: retention
    calls it once a pass, outside any transaction, spelling the legacy workdir
    too: release no longer caches job.folder_spelled between admission looks.
    No such job is newer than the
    pass, since no daemon that reads this queues one."""
    found = Places()
    for row in read(_DISCOVERING, ()):
        if needed(_loads(row["submitted"])) is not None or not row["workdir"]:
            continue
        depends = record(row["workdir"], review_root=row["review_root"], out_path=row["out_path"])
        if depends.get("incomplete"):
            found.incomplete[row["job_id"]] = depends["incomplete"]
        found[row["job_id"]] = [folders.canonical(row["workdir"]),
                                *(path for _, path in needed({"depends": depends}) or ())]
    return found


def live(read: Read, legacy_places: Mapping[str, Iterable[str]] | None = None) -> Places:
    """job id -> the places (`key`ed) of each job that has not ended: its
    workdir, worktree, review root and `-o` path as its row holds them, its
    folder as submit recorded it (or admission spelled it, for an older job), and
    `depends`; and of each job whose export is pending, its `-o` path. Database
    reads alone (with `legacy_places`, read before the transaction), so a store
    transaction may call it."""
    found = Places()

    def add(job_id: str, path: Any) -> None:
        if isinstance(path, str) and os.path.isabs(path):
            found.setdefault(job_id, set()).add(key(path))

    def failures(job_id: str, submitted: Mapping[str, Any]) -> None:
        depends = submitted.get("depends")
        errors = depends.get("incomplete", ()) if isinstance(depends, dict) else ()
        errors = [error for error in errors if isinstance(error, dict) and isinstance(error.get("file"), str)]
        if errors:
            found.incomplete[job_id] = errors

    for row in read(_LIVE, ()):
        job_id = row["job_id"]
        found.setdefault(job_id, set())
        submitted = _loads(row["submitted"])
        failures(job_id, submitted)
        errors = getattr(legacy_places, "incomplete", {}).get(job_id)
        if errors:
            found.incomplete[job_id] = errors
        for path in (row["workdir"], row["worktree"], row["review_root"], row["out_path"],
                     submitted.get("folder"), submitted.get("write_target")):
            add(job_id, path)
        for _, path in needed(submitted) or ():
            add(job_id, path)
        if not submitted.get("folder") and not submitted.get("write_target"):
            for event in read(_SPELLED, (job_id,)):
                add(job_id, _loads(event["data_json"]).get("folder"))
        for path in (legacy_places or {}).get(job_id) or ():
            add(job_id, path)
    for row in read(_EXPORTING, ()):
        failures(row["job_id"], _loads(row["submitted"]))
        errors = getattr(legacy_places, "incomplete", {}).get(row["job_id"])
        if errors:
            found.incomplete[row["job_id"]] = errors
        add(row["job_id"], row["job_out_path"])
        add(row["job_id"], row["out_path"])
        add(row["job_id"], (_loads(row["submitted"]).get("depends") or {}).get("out_path"))
    for job_id in list(found):
        if not found[job_id]:
            del found[job_id]
    return found


def users(found: Mapping[str, Iterable[str]]) -> dict[str, set[str]]:
    """`key`ed folder -> the jobs of `found` (`live`) that need a place that is it
    or inside it: each place under itself and every folder above it."""
    index: dict[str, set[str]] = {}
    for job_id, paths in found.items():
        for path in paths:
            for each in (path, *folders.above(path)):
                index.setdefault(each, set()).add(job_id)
    return index


def dependents(read: Read, tree: str, *, exclude: str | None = None,
               legacy_places: Mapping[str, Iterable[str]] | None = None) -> list[str]:
    """The jobs other than `exclude` that need `tree` or a place inside it
    (`live`), for the selecting and committing transactions, which name the tree
    in the spelling their fence holds."""
    mine = key(tree)
    found = live(read, legacy_places)
    return sorted(job_id for job_id in found.keys() | found.incomplete.keys()
                  if job_id != exclude and (found.incomplete.get(job_id) or any(
                      folders.within(path, mine) for path in found.get(job_id, ()))))
