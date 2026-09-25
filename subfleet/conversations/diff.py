"""What a turn, and a whole conversation, changed in its workspace (C-26.14, design D-25).

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

A diff shows windows of a file, so it can hold part of a private key without
the armour line the handoff scrubber needs at each end (its `_PEM_RE` matches
BEGIN to END): a hunk whose context reaches into a key, the 512 KiB cut
landing inside one, or git's hunk header, which repeats the nearest line
above the hunk that starts with a letter (a key's body line, in a key file).
`scrub_diff` removes those first, hunk by hunk, replacing each line's key
material and keeping the line, so every hunk keeps its line counts. It then
runs the handoff scrubber on each line without its diff prefix, since a `+` or
`-` in front of a line hides a value from the rules that read a line's start or
the character before a value.
"""

from __future__ import annotations

import os
import re
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

#: What replaces key material, and a run of encoded lines, on each line it held.
KEY_REDACTED = "[PRIVATE KEY REDACTED]"
ENCODED_OMITTED = "[BASE64 OMITTED]"
#: A private key's armour: the two halves of the handoff scrubber's `_PEM_RE` (C-23.14).
_KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----")
_KEY_END = re.compile(r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----")
_KEY_WHOLE = re.compile(_KEY_BEGIN.pattern + ".*?" + _KEY_END.pattern)
#: A line that is one run of base64 and nothing else but indentation, a quote, an
#: escaped newline, or the comma, plus, semicolon or backslash that continues a
#: string: the shape of a key's body line in a key file, a YAML block or source code.
_ENCODED_LINE = re.compile(r"""[ \t]*["'`]?([A-Za-z0-9+/]{4,}={0,2})(?:\\r)?(?:\\n)?["'`]?[ \t]*[+,;\\]?[ \t\r]*""")
#: A body line of a key: long, and random enough to hold both cases and a digit,
#: which a hex digest (one case) or a word does not.
_ENCODED_STRONG = 40
_HUNK_HEADER = re.compile(r"(@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@)(.*)")

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
    """A caller's `path`, relative to the checkout's top level, or None for every file.

    It names a file or a directory: git matches a pathspec against a path and
    each of its leading directories, so `sub` selects every changed file under
    `sub/` (and `su` selects none). It is taken literally, never as a glob."""
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


def checkout(workdir: str | Path, *, timeout_s: float | None = None) -> str:
    """The checkout's top level, which every path in a result is relative to.

    Raises `Unavailable("workspace-gone")` when git cannot open the workspace as a
    checkout any more (it was moved or removed, a removed linked worktree, or git
    refuses it), so that is never mistaken for a pruned snapshot: `cat-file` fails
    the same way in a directory that is not a checkout as for a missing object."""
    root = git_toplevel(workdir, timeout_s=timeout_s)
    if root is None:
        raise Unavailable("workspace-gone", "the workspace is no longer a git checkout git can open")
    return root


def build(workdir: str | Path, old: str, new: str, *, path: str | None = None, max_bytes: int = DIFF_BYTES,
          max_files: int = DIFF_FILES, list_bytes: int = LIST_BYTES, timeout_s: float | None = None) -> dict:
    """The changes from tree `old` to tree `new`, bounded (design D-25)."""
    root = checkout(workdir, timeout_s=timeout_s)
    for tree in (old, new):
        if not have_tree(workdir, tree, timeout_s=timeout_s):
            raise Unavailable("snapshot-pruned", f"the repository no longer holds snapshot {tree[:12]}")
    spec = ("--", f":(top,literal){path}") if path else ()
    names, names_cut = _bounded(workdir, ("-c", "core.quotepath=false", "diff-tree", "-z", *_COMMON,
                                          "--name-status", old, new, *spec), list_bytes, timeout_s)
    counts, counts_cut = _bounded(workdir, ("diff-tree", "-z", *_COMMON, "--numstat", old, new, *spec),
                                  list_bytes, timeout_s)
    files = _files(names, counts)
    patch, patch_cut = _bounded(workdir, ("-c", "core.quotepath=false", "diff-tree", "-p", *_COMMON, "--no-color",
                                          "--src-prefix=a/", "--dst-prefix=b/", old, new, *spec),
                                max_bytes, timeout_s)
    if patch_cut:
        # Cut at a line boundary, so the last line shown is a whole one.
        patch = patch[: patch.rfind(b"\n") + 1]
    text, scrubbed = scrub_diff(patch.decode("utf-8", errors="replace"))
    # Scrubbed text holds nothing to hide, so cutting it again is safe; a marker
    # longer than the line it replaced, or a replacement character for a byte
    # that is not UTF-8, can take it past the bound.
    text, refit = _fit(text, max_bytes)
    listed_all = not (names_cut or counts_cut)
    stats = {"files": len(files), "additions": sum(f["additions"] or 0 for f in files),
             "deletions": sum(f["deletions"] or 0 for f in files), "complete": listed_all}
    return {"root": root, "files": files[:max_files], "files_truncated": len(files) > max_files or not listed_all,
            "stats": stats, "diff": text, "truncated": patch_cut or refit, "scrubbed": scrubbed}


def scrub_diff(text: str) -> tuple[str, int]:
    """C-25.5: a unified diff with its credentials removed, and how many were.

    Key material is removed hunk by hunk first, line by line, so each hunk keeps
    its line counts: a key with both armour lines in the hunk; from a BEGIN with
    no END after it in the hunk to the hunk's end (the key runs on below the
    hunk, or past the cut); from the hunk's start to an END with no BEGIN before
    it (the key began above the hunk); a run of encoded lines shaped like a key's
    body (two or more long ones, or one at the hunk's edge, where a key can be
    cut off from its armour); and a hunk header's function context when it is
    such a line or holds armour.

    Then the handoff scrubber runs on each line by itself, a hunk's line without
    its one-character `+`, `-` or space prefix, which is put back after: several
    of its rules read the start of a line or the character before a value (the
    `Authorization:` and `Cookie:` rule starts at a line's beginning; the token,
    JWT and base64 rules look behind for a character a value may hold, which `-`
    and `+` are), so on a prefixed line they miss the value or take the prefix
    with it. One line at a time, a rule never joins two lines or removes one."""
    lines = text.split("\n")
    count = 0
    hunk: list[int] | None = None
    body: set[int] = set()
    headers: set[int] = set()
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            count += _scrub_hunk(lines, hunk)
            hunk = None
        elif line.startswith("@@"):
            count += _scrub_hunk(lines, hunk)
            hunk = []
            lines[i], found = _scrub_header(line)
            count += found
            headers.add(i)
        elif hunk is not None and line[:1] in ("+", "-", " "):
            hunk.append(i)
            body.add(i)
    count += _scrub_hunk(lines, hunk)
    for i, line in enumerate(lines):
        if i in headers:
            continue
        cut = 1 if i in body else 0
        rest, found = _scrub_line(line[cut:])
        lines[i] = line[:cut] + rest
        count += found
    return "\n".join(lines), count


def _scrub_line(line: str) -> tuple[str, int]:
    """The handoff scrubber on one line; a CRLF file's carriage return is kept."""
    ending = "\r" if line.endswith("\r") else ""
    text, found = scrub_secrets(line[: len(line) - len(ending)])
    return text + ending, found


def _scrub_header(line: str) -> tuple[str, int]:
    """A hunk header's function context is a line of the file (git repeats the
    nearest one above the hunk that starts with a letter): in a key file, a key's
    body line; elsewhere it is scrubbed as the line it repeats would be."""
    match = _HUNK_HEADER.fullmatch(line)
    if not match:
        return _scrub_line(line)
    context = match.group(2)
    if _KEY_BEGIN.search(context) or _KEY_END.search(context):
        return f"{match.group(1)} {KEY_REDACTED}", 1
    if _encoded(context, 16) or _short_encoded(context):
        # A key's body line, or its short last one (the nearest line above a hunk
        # that starts just below the key's END). Losing an ordinary context that
        # looks like one costs only the hint the header gives.
        return f"{match.group(1)} {ENCODED_OMITTED}", 1
    context, found = _scrub_line(context)
    return match.group(1) + context, found


def _encoded(content: str, length: int) -> bool:
    """Whether a line is one run of base64 at least `length` long holding both cases
    and a digit, as a key's body line does."""
    match = _ENCODED_LINE.fullmatch(content)
    if match is None:
        return False
    run = match.group(1).rstrip("=")
    return (len(run) >= length and any(c.isupper() for c in run) and any(c.islower() for c in run)
            and any(c.isdigit() for c in run))


def _indent(content: str) -> str:
    return content[: len(content) - len(content.lstrip(" \t"))]


def _scrub_hunk(lines: list[str], hunk: list[int] | None) -> int:
    """Remove the key material in one hunk's content lines, in place (see `scrub_diff`)."""
    if not hunk:
        return 0
    count = 0
    touched: set[int] = set()
    inside = False      # within a key whose BEGIN this hunk showed
    floor = 0           # the first line an END with no BEGIN before it reaches back to
    for k, i in enumerate(hunk):
        prefix, content = lines[i][:1], lines[i][1:]
        content, whole = _KEY_WHOLE.subn(KEY_REDACTED, content)
        count += whole
        out: list[str] = [_indent(content), KEY_REDACTED] if inside else []
        pos, marked = 0, inside or bool(whole)
        while True:
            if inside:
                end = _KEY_END.search(content, pos)
                if end is None:
                    break
                inside, pos, floor = False, end.end(), k + 1
                continue
            begin, end = _KEY_BEGIN.search(content, pos), _KEY_END.search(content, pos)
            if end is not None and (begin is None or end.start() < begin.start()):
                # The key began above this hunk: everything back to the floor is its.
                for j in hunk[floor:k]:
                    lines[j] = lines[j][:1] + _indent(lines[j][1:]) + KEY_REDACTED
                    touched.add(j)
                out, pos, floor, marked = [KEY_REDACTED], end.end(), k + 1, True
                count += 1
                continue
            if begin is not None:
                out += [content[pos:begin.start()], KEY_REDACTED]
                inside, pos, marked = True, begin.end(), True
                count += 1
                continue
            out.append(content[pos:])
            break
        if marked:
            lines[i] = prefix + "".join(out)
            touched.add(i)
    # Runs of key-shaped lines outside any armour this hunk shows: two or more, or
    # one at the hunk's edge, where the hunk or the cut can separate a key's body
    # from the rest of it. A short line next to such a run (a key's last line is
    # short) goes with it.
    strong = [i not in touched and _encoded(lines[i][1:], _ENCODED_STRONG) for i in hunk]
    k = 0
    while k < len(hunk):
        if not strong[k]:
            k += 1
            continue
        first = k
        while k < len(hunk) and strong[k]:
            k += 1
        last = k - 1
        if last == first and 0 < first and last < len(hunk) - 1:
            continue
        for n in (first - 1, last + 1):
            if 0 <= n < len(hunk) and hunk[n] not in touched and _short_encoded(lines[hunk[n]][1:]):
                first, last = min(first, n), max(last, n)
        for j in hunk[first:last + 1]:
            lines[j] = lines[j][:1] + _indent(lines[j][1:]) + ENCODED_OMITTED
            touched.add(j)
        count += 1
    return count


def _short_encoded(content: str) -> bool:
    """A key's short last line: base64 with a digit, `+`, `/` or padding (a word has none)."""
    match = _ENCODED_LINE.fullmatch(content)
    return match is not None and any(c.isdigit() or c in "+/=" for c in match.group(1))


def _fit(text: str, limit: int) -> tuple[str, bool]:
    """`text` in at most `limit` UTF-8 bytes, cut after a whole line: `(text, cut)`."""
    raw = text.encode()
    if len(raw) <= limit:
        return text, False
    raw = raw[:limit]
    return raw[: raw.rfind(b"\n") + 1].decode(), True


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
