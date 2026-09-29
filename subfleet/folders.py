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
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

EXCLUSIVE = "worktree:"
TURN = "worktree-turn:"
READER = "worktree-read:"
SHARED = (TURN, READER)


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
