"""What retention's delete path (C-8.7, decision d2660) leaves for the rest of retention to read.

`<state>/jobs/<job>/tree-deleted.json` marks a job whose kept worktree an
operator's ruling deleted (`retention_delete`). The normal pass keeps such a job
for good: it never retires, archives or prunes it, and a tree that is no longer
there is not an error. The survey reports the job as deleted.

`<state>/retention-delete/<job>/journal.json` is a deletion in flight. The
normal pass keeps that job as well and leaves its `retention:<job>` leases to
the delete path, which takes them under the same holder the normal pass uses.

This module reads names and small files only, and imports nothing of retention,
so `retention`, `retention_survey` and `retention_delete` can all use it.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import retention_fs as rfs

#: The delete path's folder under the state root, beside `retention/` and
#: `retention-conflicts/`. Never under `retention/`: `retention_archive.journals`
#: reads every entry there as a retirement.
DELETE_DIR = "retention-delete"
#: The marker in the job's own folder.
MARKER = "tree-deleted.json"
#: A deletion's journal, in `<state>/retention-delete/<job>/`.
JOURNAL = "journal.json"
#: Why the normal pass and the survey keep such a job.
DELETED = "tree-deleted"
IN_FLIGHT = "tree-delete in flight"


def marker_path(root: Path, job_id: str) -> Path:
    return Path(root) / "jobs" / job_id / MARKER


def marked(root: Path, job_id: str) -> bool:
    """Whether the job carries the marker. Any entry by that name counts, a
    broken one too: it keeps the job, which is the safe answer."""
    return os.path.lexists(marker_path(root, job_id))


def marker(root: Path, job_id: str) -> dict[str, Any] | None:
    """The marker's record, None without one, and `{"unreadable": why}` for one
    that cannot be read as a small JSON object (it still marks the job)."""
    path = marker_path(root, job_id)
    if not os.path.lexists(path):
        return None
    try:
        data = json.loads(rfs.read_regular(path, limit=1 << 20))
    except (OSError, ValueError) as exc:
        return {"unreadable": str(exc)[:200]}
    return data if isinstance(data, dict) else {"unreadable": "not a JSON object"}


def journal_dir(root: Path, job_id: str) -> Path:
    return Path(root) / DELETE_DIR / job_id


def journal_ids(root: Path) -> set[str]:
    """The jobs with a deletion journal in flight (one entry name each)."""
    try:
        names = os.listdir(Path(root) / DELETE_DIR)
    except FileNotFoundError:
        return set()
    return {name for name in names if Path(name).name == name and not name.startswith(".")
            and os.path.lexists(Path(root) / DELETE_DIR / name / JOURNAL)}


def kept_for_good(root: Path, job_ids: Iterable[str]) -> dict[str, str]:
    """job id -> why retention keeps it (`DELETED` or `IN_FLIGHT`), for the jobs
    of `job_ids` the delete path has marked or is deleting."""
    flying = journal_ids(root)
    found: dict[str, str] = {}
    for job_id in job_ids:
        if Path(job_id).name != job_id or job_id in (".", ".."):
            continue
        if job_id in flying:
            found[job_id] = IN_FLIGHT
        elif marked(root, job_id):
            found[job_id] = DELETED
    return found
