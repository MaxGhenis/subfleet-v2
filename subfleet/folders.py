"""C-6.5, C-24.5, C-26.3: who holds a folder, as lease rows.

A detached writer holds its write target alone: `worktree:<folder>`, one row,
one holder (C-6.5). Conversation turns share theirs (the owner's ruling of
2026-09-28, "nothing should be queued"): each turn job has its own row, so any
number of conversations in one folder run at once, and every holder-keyed
release site frees a turn's row with the job's other leases.

- `worktree-turn:<folder>:<job id>`: a writable turn. A detached writer waits
  while one exists, and a turn waits while `worktree:<folder>` is held.
- `worktree-read:<folder>:<job id>`: a read-only turn. It excludes no writer;
  it only keeps retention from removing the folder under it (C-8.4, C-13.4).

`<folder>` is a real path and may itself contain `:`; a job id never does
(C-1.1), so a key names a folder exactly when what follows `<prefix><folder>:`
has no colon. Rows are found by a range on the primary key and that check.

Keys compare folders as strings, so one folder must have one spelling:
`canonical` gives it, symlinks resolved and each name in the case the file
system stores it (review of 5e9f2fbd, P3-4). A git checkout's folder is its
top level, which git already spells so; a folder outside git (a scratch folder
a conversation works in) was kept as typed, and APFS is case-insensitive, so
`~/Scratch` and `~/scratch` were two keys for one folder and two conversations
there were never marked as sharing it.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any, Callable, Iterable

EXCLUSIVE = "worktree:"
TURN = "worktree-turn:"
READER = "worktree-read:"
SHARED = (TURN, READER)


def canonical(path: str | os.PathLike[str]) -> str:
    """`path`'s one spelling: `~` expanded, symlinks, `.` and `..` resolved, and each
    name as its directory lists it, so every case (and Unicode normalization) a
    case-insensitive volume accepts for a folder gives the same string. A name its
    directory does not list as given is matched case-insensitively and confirmed to
    be the same file; one that cannot be read or does not exist stays as given, with
    everything after it."""
    real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    names = [name for name in real.split(os.sep) if name]
    out = os.sep
    for index, name in enumerate(names):
        given = os.path.join(out, name)
        try:
            listed = os.listdir(out)
        except OSError:
            return os.path.join(given, *names[index + 1:])
        if name not in listed:
            want = _fold(name)
            given = next((os.path.join(out, entry) for entry in listed
                          if _fold(entry) == want and _same(os.path.join(out, entry), given)), given)
        out = given
    return out


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def exclusive_key(folder: str) -> str:
    return f"{EXCLUSIVE}{folder}"


def turn_key(folder: str, job_id: str, *, writable: bool) -> str:
    if ":" in job_id:
        raise ValueError(f"a job id never contains ':' (C-1.1): {job_id!r}")
    return f"{TURN if writable else READER}{folder}:{job_id}"


def parse(key: str) -> tuple[str, str, str] | None:
    """`(prefix, folder, job id)` of a turn's row, or None for any other key."""
    for prefix in SHARED:
        if key.startswith(prefix):
            folder, sep, job_id = key[len(prefix):].rpartition(":")
            if sep and folder and job_id:
                return prefix, folder, job_id
    return None


def _row(row: Any) -> tuple[str, str]:
    if isinstance(row, dict):
        return row["lease_key"], row["holder"]
    return row[0], row[1]


def turn_holds(read: Callable[[str, tuple], Iterable[Any]], folder: str,
               kinds: tuple[str, ...] = SHARED) -> list[tuple[str, str]]:
    """The turn rows `(lease key, holder)` on exactly `folder`, of the prefixes in
    `kinds`. `read(sql, params)` runs one statement: a store's `query`, or a
    transaction's `execute(...).fetchall()`, so the answer is the transaction's."""
    found = []
    for prefix in kinds:
        low = f"{prefix}{folder}:"
        high = low[:-1] + ";"                 # ':' + 1: every key that starts with `low`
        for row in read("SELECT lease_key, holder FROM leases WHERE lease_key >= ? AND lease_key < ?",
                        (low, high)):
            key, holder = _row(row)
            if ":" not in key[len(low):]:     # not a longer folder that starts `<folder>:`
                found.append((key, holder))
    return found


def turn_folders(read: Callable[[str, tuple], Iterable[Any]]) -> set[str]:
    """Every folder a live turn holds, writable or read-only (retention's pins)."""
    folders = set()
    for prefix in SHARED:
        for row in read("SELECT lease_key, holder FROM leases WHERE lease_key >= ? AND lease_key < ?",
                        (prefix, prefix[:-1] + ";")):
            parsed = parse(_row(row)[0])
            if parsed:
                folders.add(parsed[1])
    return folders
