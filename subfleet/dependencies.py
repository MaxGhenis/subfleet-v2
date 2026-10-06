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
from .sessions.transcripts import read_regular

#: What a reader is given: `read(sql, params)` runs one statement, as for
#: `folders.turn_holds` (a store's `query`, or a transaction's fetchall).
Read = Callable[[str, tuple], Iterable[Any]]

#: The `depends` roles, as a hold names them (C-6.11) and `why` words them.
REVIEW_ROOT, OUTPUT, GIT = "review-root", "output", "git-storage"

#: The most a gitfile, `commondir` or `alternates` file is read: git's own are a line or a few.
_FILE_LIMIT = 64 * 1024
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
#: Exports still to be written: a job holds `out:<path>` from its reservation
#: until its export is written or it ends without one (`Daemon._export`).
_EXPORTING = ("SELECT holder AS job_id, substr(lease_key, 5) AS out_path, "
              + _SUBMITTED.format(job="leases.holder") + " AS submitted "
              "FROM leases WHERE lease_key >= 'out:' AND lease_key < 'out;'")
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
    git = git_storage(workdir) + (git_storage(review_root) if review_root else [])
    if git:
        found["git"] = list(dict.fromkeys(git))
    return found


def git_storage(path: str) -> list[str]:
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
    try:
        start = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        return []
    found: list[str] = []
    for folder in (start, *folders.above(start)):
        dotgit = os.path.join(folder, ".git")
        try:
            mode = os.stat(dotgit).st_mode
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(mode):
            found.extend(_storage(os.path.realpath(dotgit), named=False))
        elif stat.S_ISREG(mode):
            gitdir = _gitfile(dotgit, folder)
            if gitdir is not None:
                found.extend(_storage(gitdir, named=True))
    return list(dict.fromkeys(folders.canonical(each) for each in found))


def _text(path: str) -> str | None:
    """A small regular file's text, or None when it cannot be read as one."""
    try:
        return os.fsdecode(read_regular(path, limit=_FILE_LIMIT))
    except (OSError, ValueError):
        return None


def _gitfile(dotgit: str, folder: str) -> str | None:
    """The directory a `.git` file names (`gitdir: <path>`), or None."""
    text = (_text(dotgit) or "").strip()
    if not text.startswith("gitdir:"):
        return None
    named = text[len("gitdir:"):].strip()
    return os.path.realpath(os.path.join(folder, named)) if named else None


def _storage(gitdir: str, *, named: bool) -> list[str]:
    """`gitdir`, its common directory, and the alternates of that repository's objects."""
    common = gitdir
    pointer = _text(os.path.join(gitdir, "commondir"))
    if pointer is not None and pointer.strip():
        common = os.path.realpath(os.path.join(gitdir, pointer.strip()))
    elif named and os.path.basename(os.path.dirname(gitdir)) == "worktrees":
        common = os.path.dirname(os.path.dirname(gitdir))
    return list(dict.fromkeys([gitdir, common, *_alternates(os.path.join(common, "objects"))]))


def _alternates(objects: str) -> list[str]:
    """The object directories `objects` borrows from (`info/alternates`), followed
    `_ALTERNATES_DEPTH` levels deep; a line may be relative to `objects`."""
    found: list[str] = []
    level = [objects]
    for _ in range(_ALTERNATES_DEPTH):
        following: list[str] = []
        for directory in level:
            for line in (_text(os.path.join(directory, "info", "alternates")) or "").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if len(line) > 1 and line[0] == line[-1] == '"':
                    line = line[1:-1]          # git's C-style quoting: a path with an odd character
                each = os.path.realpath(os.path.join(directory, line))
                if each not in found and each != objects:
                    found.append(each)
                    following.append(each)
        level = following
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
    first (`folders.retiring`), all read in one statement."""
    places = [(role, path) for role, path in places if path]
    wanted = {path: [folders.exclusive_key(each) for each in (path, *folders.above(path))] for _, path in places}
    keys = sorted({each for found in wanted.values() for each in found})
    if not keys:
        return []
    held = {}
    for start in range(0, len(keys), 500):
        part = keys[start:start + 500]
        held.update(folders._row(row) for row in read(
            f"SELECT lease_key, holder FROM leases WHERE lease_key IN ({','.join('?' * len(part))})", tuple(part)))
    found = []
    for role, path in places:
        mine = [each for each in wanted[path] if str(held.get(each) or "").startswith(folders.RETENTION)]
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


def legacy(read: Read) -> dict[str, list[str]]:
    """job id -> the places a job that has not ended, queued before submit
    recorded `depends`, needs (`record`, read now). Filesystem work: retention
    calls it once a pass, outside any transaction; no such job is newer than the
    pass, since no daemon that reads this queues one."""
    found: dict[str, list[str]] = {}
    for row in read(_LIVE, ()):
        if needed(_loads(row["submitted"])) is not None or not row["workdir"]:
            continue
        depends = record(row["workdir"], review_root=row["review_root"], out_path=row["out_path"])
        found[row["job_id"]] = [path for _, path in needed({"depends": depends}) or ()]
    return found


def live(read: Read, legacy_places: Mapping[str, Iterable[str]] | None = None) -> dict[str, set[str]]:
    """job id -> the places (`key`ed) of each job that has not ended: its
    workdir, worktree, review root and `-o` path as its row holds them, its
    folder as submit recorded it (or admission spelled it, for an older job), and
    `depends`; and of each job whose export is pending, its `-o` path. Database
    reads alone (with `legacy_places`, read before the transaction), so a store
    transaction may call it."""
    found: dict[str, set[str]] = {}

    def add(job_id: str, path: Any) -> None:
        if isinstance(path, str) and os.path.isabs(path):
            found.setdefault(job_id, set()).add(key(path))

    for row in read(_LIVE, ()):
        job_id = row["job_id"]
        found.setdefault(job_id, set())
        submitted = _loads(row["submitted"])
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
        add(row["job_id"], row["out_path"])
        add(row["job_id"], (_loads(row["submitted"]).get("depends") or {}).get("out_path"))
    return {job_id: paths for job_id, paths in found.items() if paths}


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
    return sorted(job_id for job_id, paths in live(read, legacy_places).items()
                  if job_id != exclude and any(folders.within(path, mine) for path in paths))
