"""What a turn, and a whole conversation, changed in its workspace (C-26.13, design D-25).

A writable turn whose workspace is a git checkout with a commit is bracketed
by two snapshots of the working tree, each a tree object written through a
temporary index by the C-6.8 snapshot (`salvage.working_tree`): the start one
is the attempt's `baseline_tree`, taken at admission for every writable job;
the end one is taken at finalization, while the turn's `worktree:` lease is
still held, so no other writer's work lands between the turn and it. Neither
writes a ref, and neither touches HEAD, the real index, or a file in the
checkout. The objects are unreferenced, so git may prune them once they are
older than its prune window (`gc.pruneExpire`, two weeks by default); a diff
whose objects are gone says so.

`build` compares two trees with git's plumbing (`diff-tree`, which runs no
external diff driver or textconv filter here) and returns a bounded result:
the changed files with status and line counts, and a unified diff cut at a
line boundary with a `truncated` flag. The diff text passes the handoff
scrubber (C-23.14) that event text and history pages pass (C-25.5), so a
credential written into a tracked file does not leave the daemon in a result.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

from ..salvage import (
    SalvageError, _git, git_head, git_timeout_s, git_toplevel, transient_os_error, working_tree,
)
from ..sessions.handoff import scrub_secrets

#: The unified diff a result carries, and the changed files it lists.
DIFF_BYTES = 512 * 1024
DIFF_FILES = 1000
#: What one listing call (`--name-status`, `--numstat`) may return before it is cut.
LIST_BYTES = 4 * 1024 * 1024
#: The longest `path` a caller may name.
PATH_MAX = 4096

STATUS = {"A": "added", "D": "deleted", "M": "modified", "T": "type-changed", "R": "renamed", "C": "copied",
          "U": "unmerged", "X": "unknown"}

#: Every diff-tree call: renames found, no external diff driver, no textconv
#: filter, fixed prefixes whatever the repository's `diff.*` settings say.
_COMMON = ("-r", "-M", "--no-ext-diff", "--no-textconv")


class Unavailable(Exception):
    """There is nothing to compare; `reason` is a short token, the message says why."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def pathspec(value) -> str | None:
    """A caller's `path`: one file, relative to the checkout's top level, or None."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > PATH_MAX or "\0" in value:
        raise ValueError("path must be a string of at most 4096 characters")
    parts = value.split("/")
    if value.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must be relative to the checkout's top level, with no '.' or '..' parts")
    return value


def snapshot(workdir: str | Path, *, timeout_s: float | None = None) -> tuple[str, str] | None:
    """`(HEAD, tree)` of the working tree now, or None outside a checkout with a commit."""
    head = git_head(workdir, timeout_s=timeout_s)
    if head is None:
        return None
    return head, working_tree(workdir, head, timeout_s=timeout_s)


def end_snapshot(workdir: str | Path, *, head_before: str | None, start_tree: str | None,
                 timeout_s: float | None = None) -> dict:
    """A turn's end (C-26.10): HEAD after, and the working tree's end snapshot when the
    turn has a start snapshot to compare it with. Raises `SalvageError` when git fails."""
    head_after = git_head(workdir, timeout_s=timeout_s)
    end_tree = None
    if start_tree is not None:
        base = head_after or head_before
        if base is None:
            raise SalvageError("the workspace has no commit to snapshot against")
        end_tree = working_tree(workdir, base, timeout_s=timeout_s)
    return {"head_after": head_after, "end_tree": end_tree}


def have_tree(workdir: str | Path, tree: str, *, timeout_s: float | None = None) -> bool:
    """Whether the repository still holds `tree` (an unreferenced snapshot can be pruned)."""
    return _git(workdir, "cat-file", "-e", f"{tree}^{{tree}}", optional=True, timeout_s=timeout_s) is not None


def build(workdir: str | Path, old: str, new: str, *, path: str | None = None, max_bytes: int = DIFF_BYTES,
          max_files: int = DIFF_FILES, timeout_s: float | None = None) -> dict:
    """The changes from tree `old` to tree `new`, bounded (design D-25)."""
    for tree in (old, new):
        if not have_tree(workdir, tree, timeout_s=timeout_s):
            raise Unavailable("snapshot-pruned", f"the repository no longer holds snapshot {tree[:12]}")
    spec = ("--", f":(top,literal){path}") if path else ()
    names, names_cut = _bounded(workdir, ("-c", "core.quotepath=false", "diff-tree", "-z", *_COMMON,
                                          "--name-status", old, new, *spec), LIST_BYTES, timeout_s)
    counts, counts_cut = _bounded(workdir, ("diff-tree", "-z", *_COMMON, "--numstat", old, new, *spec),
                                  LIST_BYTES, timeout_s)
    files = _files(names, counts)
    patch, patch_cut = _bounded(workdir, ("-c", "core.quotepath=false", "diff-tree", "-p", *_COMMON, "--no-color",
                                          "--src-prefix=a/", "--dst-prefix=b/", old, new, *spec),
                                max_bytes, timeout_s)
    text = patch.decode("utf-8", errors="replace")
    if patch_cut:
        # Cut at a line boundary, so the last line shown is a whole one.
        text = text[: text.rfind("\n") + 1] if "\n" in text else ""
    text, scrubbed = scrub_secrets(text)
    listed_all = not (names_cut or counts_cut)
    stats = {"files": len(files), "additions": sum(f["additions"] or 0 for f in files),
             "deletions": sum(f["deletions"] or 0 for f in files), "complete": listed_all}
    return {"files": files[:max_files], "files_truncated": len(files) > max_files or not listed_all,
            "stats": stats, "diff": text, "truncated": patch_cut, "scrubbed": scrubbed}


def _fields(output: bytes) -> list[bytes]:
    """`-z` output as its NUL-terminated fields. Whatever follows the last NUL is a
    field the bound cut short (complete output ends with a NUL), so it is dropped."""
    return output.split(b"\0")[:-1]


def _files(names: bytes, counts: bytes) -> list[dict]:
    """Join `--name-status -z` and `--numstat -z` by the file's (new) path."""
    numbers: dict[str, tuple[int | None, int | None]] = {}
    fields = _fields(counts)
    i = 0
    while i < len(fields):
        parts = fields[i].split(b"\t", 2)
        if len(parts) != 3:
            break
        if parts[2] == b"":                     # a rename or copy: the two paths follow
            if i + 2 >= len(fields):
                break
            key, i = fields[i + 2], i + 3
        else:
            key, i = parts[2], i + 1
        added, removed = (None if n == b"-" else int(n) for n in parts[:2])
        numbers[_text(key)] = (added, removed)
    out: list[dict] = []
    fields = _fields(names)
    i = 0
    while i + 1 < len(fields):
        letter = fields[i][:1].decode("ascii", errors="replace")
        if letter in ("R", "C"):
            if i + 2 >= len(fields):
                break
            source, target, i = _text(fields[i + 1]), _text(fields[i + 2]), i + 3
        else:
            source, target, i = None, _text(fields[i + 1]), i + 2
        added, removed = numbers.get(target, (None, None))
        entry = {"path": target, "status": STATUS.get(letter, "unknown"), "additions": added, "deletions": removed,
                 "binary": target in numbers and added is None}
        if source is not None:
            entry["from"] = source
        out.append(entry)
    return out


def _text(raw: bytes) -> str:
    # A result is JSON; a path that is not UTF-8 is shown with replacement characters.
    return raw.decode("utf-8", errors="replace")


def _bounded(workdir: str | Path, args: tuple[str, ...], limit: int,
             timeout_s: float | None) -> tuple[bytes, bool]:
    """Run git and read at most `limit` bytes of its output: `(output, cut)`.

    A call that runs past the cap is killed and raises a transient
    `SalvageError`, as every capped git call does (C-6.8); a non-zero exit
    raises with git's stderr. Output past the bound is not read: git is
    stopped and the result says it was cut.
    """
    cap = git_timeout_s(timeout_s)
    verb = next(a for a in args if not a.startswith("-") and "=" not in a)
    expired = threading.Event()
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(["git", "-C", str(workdir), *args], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=errors,
                                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        except OSError as exc:
            raise SalvageError(f"git {verb} could not run: {exc}", transient=transient_os_error(exc)) from exc

        def expire():
            expired.set()
            process.kill()

        timer = threading.Timer(cap, expire)
        timer.daemon = True
        timer.start()
        try:
            data = process.stdout.read(limit + 1)
            cut = len(data) > limit
            if cut:
                process.kill()
            process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
        if expired.is_set():
            raise SalvageError(f"git {verb} timed out after {cap:g} s", transient=True)
        if not cut and process.returncode:
            errors.seek(0)
            message = errors.read(2000).decode("utf-8", errors="replace").strip()
            raise SalvageError(f"git {verb} failed: {message or f'exit {process.returncode}'}")
    return data[:limit], cut


def toplevel(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    """The checkout's top level, which every path in a result is relative to."""
    return git_toplevel(workdir, timeout_s=timeout_s)
