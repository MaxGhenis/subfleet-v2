"""Remove the redundant rows a job's `decisions` history accumulated (C-11.5).

Until #16 the admission pass recorded a whole routing decision for a job that
was only waiting for capacity, once per re-check (`daemon.py` `_admit` called
`add_decision` each time a routing verdict left a job waiting), and nothing
reads those rows again: `subfleet why` (the daemon's `why` verb) and offline
`runs show` each serve one row per job, and no foreign key in
`store_schema.sql` points at `decisions`. #16 stopped the repeats — a wait
that reaches the verdict it reached last time adds no row (C-6.10) — and this
module is the one-off operator pass that gives back the space the rows already
written take. The dry run measures how many of them a store holds.

What it guarantees:

* **`decisions` and nothing else.** No other table is written. `events` is the
  append-only audit spine (C-3.2, and `store_schema.sql` says so over the table)
  and gains only the one row per delete batch that C-3.2 requires of a
  transaction that changes state. A store behind this build's schema is refused
  rather than written to, because opening one for writing migrates it
  (`store.py` `_migrate`), which would write `schema_version` and `events` rows
  before this pass could copy or fingerprint anything.
* **No reader loses a row.** The keep set is built from `KEEP_QUERIES`, whose
  first two statements are the readers of `READER_QUERIES`; after the pass those
  two are asked again for every affected job, and the bytes they return must be
  identical to the bytes they returned before.
* **Recoverable.** A real pass writes two backups before the first DELETE: a
  `VACUUM INTO` copy of the whole store, verified with `PRAGMA integrity_check`,
  and every row it is about to delete as gzipped JSON lines, re-read and checked
  against the count and digest recorded while writing.
* **Proven, not asserted.** Every other table is counted and digested before and
  after and must match, `events` is proven append-only, and the store is checked
  with `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. With `--vacuum`
  the same proofs are taken again over the rebuilt file. A mismatch is reported,
  saved in the report, and exits non-zero with how to restore.
* **Refuses rather than guesses.** A daemon holding `daemon.lock`, a volume
  without room for the backups, a schema this build would migrate, or a missing
  store is a refusal (exit 7). Anything else that stops a pass which had begun
  to write still saves the report and, once rows have been deleted, names the
  copy to restore from. A report that cannot be saved is printed in full
  instead, and never replaces the error that stopped the pass or its fix.

The default is a dry run that does the whole job but the writing: it computes
the exact keep and delete sets, the exact bytes the delete frees, the
before-fingerprints and the free-space verdict, and prints the report. It writes
no row, no backup and no file of its own but that report — though SQLite creates
`state.sqlite3-wal` and `state.sqlite3-shm` beside a WAL store whenever one is
opened, read-only included, which is why `importer.py` copies a database it
means to leave alone rather than opening it in place. Writing needs `--apply`
and `--i-understand-this-deletes-decisions` together. A dry run does not
rehearse the deletes against a scratch copy of the store, and does not take
`daemon.lock`, so its numbers are a snapshot: every read is taken inside one
read transaction, so the plan, its check and its fingerprints describe the same
instant, and a daemon that is running keeps writing rows after it.

Invocation (`python -m subfleet.prune_decisions`; the CLI keeps its own verbs):

    python -m subfleet.prune_decisions                       # dry run
    subfleet daemon stop
    python -m subfleet.prune_decisions --apply --i-understand-this-deletes-decisions
    python -m subfleet.prune_decisions --apply --i-understand-this-deletes-decisions --vacuum

A second real pass is a no-op: the keep set is everything that is left, so there
is nothing to delete, no backup is written and no row changes.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guardian import atomic_publish
from .store import SCHEMA_VERSION, SchemaVersionError, Store, utc_now

STORE_NAME = "state.sqlite3"
LOCK_NAME = "daemon.lock"
BACKUPS_NAME = "backups"

#: The operator recipe, also embedded verbatim in docs/migration.md and
#: executed against disposable stores by test_prune_restore.py. Invariants:
#: all old database files stay together, no old WAL reaches the restored copy,
#: and neither the backup nor its rows change during restoration.
RESTORE_RECIPE = """Stop the daemon with `subfleet daemon stop`. If its launchd job is
installed, unload it with
`launchctl unload ~/Library/LaunchAgents/com.subfleet.daemon.plist` so KeepAlive
cannot restart it. Set `state_root` to the store's directory, then run
`lsof -- "$state_root/state.sqlite3" "$state_root/state.sqlite3-wal" "$state_root/state.sqlite3-shm"`
and confirm that no process holds any of these files. Missing sidecars are OK;
resolve any other inspection error before proceeding.

Move `state.sqlite3`, `state.sqlite3-wal` and `state.sqlite3-shm` aside together
before copying the backup. Run this block in the same shell with `state_root`
set; replace the backup placeholder with the copy named in the pass report:

```sh
(
    set -eu
    backup={copy_path}
    aside=$(mktemp -d "$state_root/state-before-restore.XXXXXX")
    for file in state.sqlite3 state.sqlite3-wal state.sqlite3-shm; do
        if [ -e "$state_root/$file" ]; then
            mv "$state_root/$file" "$aside/$file"
        fi
    done
    cp "$backup" "$state_root/state.sqlite3"
    sqlite3 "$state_root/state.sqlite3" 'PRAGMA integrity_check'
)
```

Require `integrity_check` to return exactly `ok`. Compare each table's
`SELECT COUNT(*) FROM "<table>"` with the pre-prune counts in this pass's JSON
report: `decisions.total` for `decisions`, and
`fingerprints.before.<table>.rows` for `events` and every other table.
`fingerprints.before.decisions-kept.rows` is only the retained subset, not the
backup's decision count. An integrity check alone does not prove that the
backup's contents were restored. Only after integrity and all counts match,
restart with `launchctl load -w ~/Library/LaunchAgents/com.subfleet.daemon.plist`
if installed, or `subfleet daemon start`. Keep the displaced files and backup
until the restore is verified."""

#: The `jobs.state` values that mean the job is finished: the CHECK on
#: `jobs.state` in `store_schema.sql` less the three live ones, which is the
#: same set `retention.py` calls terminal. A job in any other state keeps every
#: row it has: the daemon may write another decision for it at any moment, and
#: a waiting job's history is not this pass's business.
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "lost"})

#: Primary keys per DELETE. Each batch is one transaction and therefore one
#: `events` row (C-3.2): a row-at-a-time prune would rebuild in `events` the
#: volume it is taking out of `decisions`.
BATCH_SIZE = 1000

#: Batches between `PRAGMA wal_checkpoint(TRUNCATE)`, so the write-ahead log is
#: bounded by ten batches rather than by the length of the pass.
CHECKPOINT_BATCHES = 10

#: What the pass requires free, and why. The `VACUUM INTO` copy is the size of
#: the database; a quarter of the database is the allowance for the compressed
#: row backup; 512 MiB of margin covers the write-ahead log between checkpoints
#: and keeps the volume off zero. `--vacuum` writes a second whole database
#: beside the first while it rebuilds, which is at most the database less the
#: bytes this pass frees. So one volume must have `database * 1.25 + the rebuild
#: + 512 MiB`. With `--backup-dir` on another volume the halves are checked
#: separately: the backup volume carries the copy and the compressed rows
#: (`database * 1.25`), the state root's volume carries the rebuild and the
#: margin.
NEEDED_FACTOR = 1.25
NEEDED_MARGIN = 512 * 1024 ** 2
#: How the margin is written wherever the formula is quoted. `_bytes` is
#: 1000-based like every other size this module prints, so it renders the
#: constant as "536.87 MB", which reads like a different number from the one
#: this comment and `docs/migration.md` state.
NEEDED_MARGIN_TEXT = "512 MiB"

#: The two statements that serve a job's decision today, their ORDER BY and
#: LIMIT copied from the readers: the daemon's `why` verb (`daemon.py`, `op ==
#: "why"`) and offline `runs show` (`offline.py`, `show_job`). `decision_id` is
#: added to the select list, which chooses no other row. The prune keeps the row
#: each one selects, and proves afterwards that both still return the same bytes.
READER_QUERIES: dict[str, str] = {
    "why": "SELECT decision_id, decision_json FROM decisions WHERE job_id=?"
           " ORDER BY decision_id DESC LIMIT 1",
    "show_job": "SELECT decision_id, decision_json FROM decisions WHERE job_id = ?"
                " ORDER BY evaluated_at DESC, decision_id DESC LIMIT 1",
}

#: What a terminal job keeps, named by the reason it is kept. The first two are
#: the reader statements above. The second two repeat them over the attempt-less
#: rows alone, so a job whose newest row belongs to an attempt still keeps the
#: last decision written while it was waiting. `evaluated_at` and `decision_id`
#: are written in the same statement by both insert paths, so the two orderings
#: agree in practice; they are asked separately rather than assumed equal.
KEEP_QUERIES: dict[str, str] = {
    "why": "SELECT decision_id FROM decisions WHERE job_id=? ORDER BY decision_id DESC LIMIT 1",
    "show_job": "SELECT decision_id FROM decisions WHERE job_id=?"
                " ORDER BY evaluated_at DESC, decision_id DESC LIMIT 1",
    "latest-attemptless": "SELECT decision_id FROM decisions WHERE job_id=? AND attempt_id IS NULL"
                          " ORDER BY decision_id DESC LIMIT 1",
    "latest-attemptless-by-time": "SELECT decision_id FROM decisions WHERE job_id=?"
                                  " AND attempt_id IS NULL"
                                  " ORDER BY evaluated_at DESC, decision_id DESC LIMIT 1",
}

#: The event kind every delete batch commits under.
PRUNE_KIND = "decisions.pruned"


class PruneError(RuntimeError):
    """The shape every stop here has: the message, the fix, and the pass's report.

    `main` prints the first two as `cli.fail` does and the report's path beside
    them, so an operator is never left with a traceback for an answer.
    """

    code = 1

    def __init__(self, message: str, fix: str | None = None,
                 report: "PruneReport | None" = None):
        super().__init__(message)
        self.fix = fix
        self.report = report


class PruneRefused(PruneError):
    """A precondition of a real pass is not met (a live daemon, no room, an old schema)."""

    code = 7


class PruneVerificationError(PruneError):
    """A proof did not hold; the pass stops and names the copy to restore from."""


class PruneStopped(PruneError):
    """The pass stopped on something it does not handle: a full volume, a Ctrl-C.

    `prune` raises it in place of whatever came out of the pass, which stays on
    `__cause__`. The report is the only written record of the backup paths and
    of how many batches were committed, so it is saved and named here rather
    than lost with the traceback.
    """


class PruneReportUnsaved(PruneError):
    """The pass finished, but its report could not be saved.

    Raised only when nothing else went wrong: a pass that failed raises its own
    error whether or not its report was saved. Either way the report stays
    attached, and `main` prints one that is not on disk in full, because it
    names the backups and holds the pre-prune counts a restore is checked
    against.
    """


# --- the report ---------------------------------------------------------------

@dataclass
class PruneReport:
    """What one pass planned, wrote, and proved."""

    state_root: str
    database: str
    backup_dir: str
    dry_run: bool
    vacuum: bool
    started_at: str
    finished_at: str | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, Any] = field(default_factory=dict)
    space: dict[str, Any] = field(default_factory=dict)
    backups: dict[str, Any] = field(default_factory=dict)
    database_bytes: dict[str, Any] = field(default_factory=dict)
    fingerprints: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    batches: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)
    path: str | None = None
    save_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state_root": self.state_root,
            "database": self.database,
            "backup_dir": self.backup_dir,
            "dry_run": self.dry_run,
            "vacuum": self.vacuum,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "schema": self.schema,
            "decisions": self.decisions,
            "space": self.space,
            "backups": self.backups,
            "database_bytes": self.database_bytes,
            "fingerprints": self.fingerprints,
            "verification": self.verification,
            "batches": self.batches,
            "deleted": self.deleted,
            "errors": self.errors,
        }

    def save(self, state_root: str | Path) -> Path:
        """Save to `<state root>/decisions-prune-report-<utc>.json` (C-8.1).

        The stamp is second-precision like every other UTC stamp here, so two
        passes inside one second would publish over each other's evidence — the
        collision the backups refuse. A report is written when its pass is
        already over, so rather than fail one it takes the next free suffix.
        """
        stamp = (self.finished_at or self.started_at).replace("-", "").replace(":", "")
        directory = Path(state_root)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = directory / f"decisions-prune-report-{stamp}.json"
        taken = 1
        while path.exists():
            taken += 1
            path = directory / f"decisions-prune-report-{stamp}-{taken}.json"
        atomic_publish(path, json.dumps(self.as_dict(), indent=1, sort_keys=True).encode() + b"\n")
        self.path = str(path)
        return path


# --- preconditions ------------------------------------------------------------

def _schema_version(database: Path) -> int:
    """The store's schema version, read through a connection that cannot write.

    `Store.__init__` migrates a store behind this build as it opens it for
    writing (`store.py` `_migrate`): it writes a `schema_version` row and a
    `schema.migrated` and `schema.applied` event per step, and it does so before
    this pass could take its `VACUUM INTO` copy or its before-fingerprints. So
    the version is read first, here, and a store this build would migrate is
    refused instead. 0 means the store has no `schema_version` table at all,
    which `Store` refuses on its own terms.
    """
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                              " AND name='schema_version'").fetchone()
        if not exists:
            return 0
        return int(conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version")
                   .fetchone()[0])
    finally:
        conn.close()


def _restore_fix(copy_path: str | None) -> str:
    """How to put the store back from the copy this pass took.

    All three files are named. SQLite replays a `state.sqlite3-wal` it finds
    beside a database over whatever `state.sqlite3` is there, so a restore that
    moves only the database aside is read back as something other than the copy.
    `test_prune_restore.py` shows both ways, on synthetic stores with a hot log
    present: over a copy whose pages line up with the log, the replay is clean
    and `integrity_check` says ok over the wrong rows; over a copy `VACUUM INTO`
    compacted, as this pass takes it, the image is malformed. `subfleet doctor`
    is not the check for this — `doctor.py` opens it read-only to query
    unfinished jobs with unknown lane pins, but does not check integrity or
    restored row counts.
    """
    if not copy_path:
        return "nothing was deleted and no copy was taken; the store is as it was"
    return RESTORE_RECIPE.format(copy_path=shlex.quote(copy_path))


def _hold_daemon_lock(state_root: Path) -> int | None:
    """Take `daemon.lock` for the whole pass, as `importer._hold_daemon_lock` does.

    The daemon is the store's writer (C-3.4), so a pass that deletes rows takes
    the daemon's own lock and keeps it: checking once and letting go would leave
    a window in which a daemon starts and two writers run. A lock file that
    exists and cannot be opened is a refusal, not a pass. A dry run never takes
    this path.
    """
    lock = state_root / LOCK_NAME
    try:
        handle = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise PruneRefused(
            f"prune decisions: cannot open {lock} to check for a daemon: {error}") from None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(handle)
        raise PruneRefused(
            f"prune decisions: a daemon holds {lock}; it is the store's only writer (C-3.4) "
            f"and it writes a decision for every waiting job while this pass runs",
            "subfleet daemon stop — and note that `subfleet daemon install` writes "
            "~/Library/LaunchAgents/com.subfleet.daemon.plist with KeepAlive and RunAtLoad "
            "set, so launchd starts it again; `launchctl unload` that plist for the pass "
            "and load it again afterwards") from None
    except OSError as error:
        os.close(handle)
        raise PruneRefused(f"prune decisions: cannot lock {lock}: {error}") from None
    return handle


def _release_daemon_lock(handle: int | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _existing(path: Path) -> Path:
    """The nearest ancestor that exists, so a volume can be measured before it is used."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or ".")


def _bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1000 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.2f} {unit}"
        count /= 1000.0
    raise AssertionError("unreachable")            # pragma: no cover


def _volume(path: Path) -> int:
    """Which filesystem a path is on, so the two halves can be charged apart."""
    return path.stat().st_dev


def _space(database: Path, state_root: Path, backups: Path, *, reclaimable: int,
           vacuum: bool) -> dict[str, Any]:
    """Measure what each volume has and what this pass needs of it (NEEDED_FACTOR).

    `--vacuum` writes a whole second database beside the first while it rebuilds,
    so its cost is charged to the state root's volume in both branches: sharing
    a volume with the backups makes that volume need more, not less.
    """
    size = database.stat().st_size
    state_at, backups_at = _existing(state_root), _existing(backups)
    same_volume = _volume(state_at) == _volume(backups_at)
    rebuild = max(size - reclaimable, 0) if vacuum else 0
    if same_volume:
        needs = {str(state_at): int(size * NEEDED_FACTOR) + rebuild + NEEDED_MARGIN}
    else:
        needs = {str(backups_at): int(size * NEEDED_FACTOR),
                 str(state_at): rebuild + NEEDED_MARGIN}
    volumes = {}
    for mount, needed in needs.items():
        free = shutil.disk_usage(mount).free
        volumes[mount] = {"free": free, "needed": needed, "sufficient": free >= needed}
    return {"database_bytes": size, "same_volume": same_volume, "rebuild_bytes": rebuild,
            "formula": f"database * {NEEDED_FACTOR}"
                       + (" + the rebuild --vacuum writes (the database less what this pass "
                          "frees)" if vacuum else "")
                       + f" + {NEEDED_MARGIN_TEXT} on one volume; split across two when "
                         f"--backup-dir is on another",
            "volumes": volumes,
            "sufficient": all(volume["sufficient"] for volume in volumes.values())}


def _space_refusal(space: dict[str, Any]) -> PruneRefused:
    short = [f"{mount} has {_bytes(volume['free'])} free and needs {_bytes(volume['needed'])}"
             for mount, volume in sorted(space["volumes"].items()) if not volume["sufficient"]]
    return PruneRefused(
        f"prune decisions: not enough free space for the backups: {'; '.join(short)} "
        f"(the database is {_bytes(space['database_bytes'])}; {space['formula']})",
        "free space on that volume, or pass --backup-dir on another one")


# --- fingerprints --------------------------------------------------------------

def _blob(value: Any) -> str:
    """A BLOB column has no JSON spelling; hex is one, and it is reversible."""
    if isinstance(value, (bytes, bytearray)):
        return "blob:" + bytes(value).hex()
    raise TypeError(f"cannot digest a {type(value).__name__} column")


def _row_digest(row: sqlite3.Row) -> bytes:
    return hashlib.sha256(json.dumps(dict(row), sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=_blob).encode()).digest()


def _combine(digests: list[bytes]) -> dict[str, Any]:
    digests.sort()
    total = hashlib.sha256()
    for digest in digests:
        total.update(digest)
    return {"rows": len(digests), "sha256": total.hexdigest()}


def _digest(rows: Iterator[sqlite3.Row]) -> dict[str, Any]:
    """Count and digest rows in an order no row can change.

    Every row becomes canonical JSON and is hashed; the digests are sorted and
    hashed in turn, so the fingerprint covers the multiset of rows and needs no
    primary key. A table added to the schema after this was written is therefore
    covered without being named here.
    """
    return _combine([_row_digest(row) for row in rows])


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name")]


def _fingerprints(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every table but `decisions` and `events`, which have proofs of their own."""
    return {table: _digest(conn.execute(f'SELECT * FROM "{table}"'))
            for table in _tables(conn) if table not in ("decisions", "events")}


def _digest_ids(conn: sqlite3.Connection, ids: Sequence[int], batch_size: int) -> dict[str, Any]:
    """The same fingerprint over named `decisions` rows, read in batches of keys."""
    digests: list[bytes] = []
    for batch in _batches(ids, batch_size):
        marks = ",".join("?" for _ in batch)
        digests.extend(_row_digest(row) for row in conn.execute(
            f"SELECT * FROM decisions WHERE decision_id IN ({marks})", batch))
    return _combine(digests)


def _reader_bytes(conn: sqlite3.Connection, jobs: Sequence[str]) -> dict[str, dict[str, Any]]:
    """What each reader serves for these jobs, as the bytes it would return."""
    served: dict[str, dict[str, Any]] = {}
    for job_id in jobs:
        for name, sql in READER_QUERIES.items():
            row = conn.execute(sql, (job_id,)).fetchone()
            served.setdefault(job_id, {})[name] = row["decision_json"] if row else None
    return served


# --- the plan ------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    """The exact rows one pass would keep and delete, and why."""

    total: int
    keep: dict[int, list[str]]
    delete: list[int]
    delete_bytes: int
    jobs: list[str]
    affected: list[str]
    live_jobs: list[str]

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for held in self.keep.values():
            for reason in held:
                counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))


def plan(conn: sqlite3.Connection) -> Plan:
    """Decide what every `decisions` row is, by the query that would serve it.

    A row is kept when it carries an attempt id (C-11.5: the decision is stored
    per attempt), when one of `KEEP_QUERIES` selects it for its job, or when its
    job is not terminal. Everything else is redundant: no reader in the package
    reaches it and no foreign key names it.
    """
    states = {row["job_id"]: row["state"] for row in conn.execute("SELECT job_id, state FROM jobs")}
    # `length()` over a TEXT column counts characters, and `store._json` writes
    # with `ensure_ascii=False`, so the cast is what makes the figure the bytes
    # of `decision_json` as stored.
    rows = conn.execute("SELECT decision_id, job_id, attempt_id,"
                        " length(CAST(decision_json AS BLOB)) AS bytes"
                        " FROM decisions ORDER BY decision_id").fetchall()
    keep: dict[int, list[str]] = {}

    def hold(decision_id: int, reason: str) -> None:
        keep.setdefault(int(decision_id), []).append(reason)

    by_job: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_job.setdefault(row["job_id"], []).append(row)
        if row["attempt_id"] is not None:
            hold(row["decision_id"], "attempt")
    live_jobs = []
    for job_id, job_rows in by_job.items():
        # A job the `jobs` table does not know is treated as live: an unexplained
        # row is not evidence that nothing will read it.
        if states.get(job_id) not in TERMINAL_STATES:
            live_jobs.append(job_id)
            for row in job_rows:
                hold(row["decision_id"], "live-job")
            continue
        for reason, sql in KEEP_QUERIES.items():
            selected = conn.execute(sql, (job_id,)).fetchone()
            if selected is not None:
                hold(selected["decision_id"], reason)
    delete: list[int] = []
    delete_bytes = 0
    dropped: set[str] = set()
    for row in rows:
        if row["decision_id"] not in keep:
            delete.append(row["decision_id"])
            delete_bytes += row["bytes"]
            dropped.add(row["job_id"])
    return Plan(len(rows), keep, delete, delete_bytes, sorted(by_job),
                 sorted(dropped), sorted(live_jobs))


def _check_plan(conn: sqlite3.Connection, planned: Plan) -> None:
    """Refuse a plan that would leave a job with nothing for its readers to serve.

    The keep set is built from these same statements, so this is an assertion
    rather than a filter — and an assertion is what the store deserves before a
    delete. It also settles the count: every job keeps the row its readers
    select, so `COUNT(DISTINCT job_id)` over `decisions` cannot move, which
    `_verify` asks the database again once the rows are gone.
    """
    for job_id in planned.jobs:
        for name, sql in READER_QUERIES.items():
            row = conn.execute(sql, (job_id,)).fetchone()
            if row is None or int(row["decision_id"]) not in planned.keep:
                raise PruneVerificationError(
                    f"prune decisions: the plan would delete the row {name} serves for {job_id}",
                    "this is a bug in the keep set; nothing has been deleted")


# --- backups -------------------------------------------------------------------

def _batches(ids: Sequence[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(ids), size):
        yield list(ids[start:start + size])


def _fsync_dir(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _backup_dir(path: Path) -> None:
    """Create the backup directory private (0700); leave an operator's own alone.

    Every component this pass creates gets the mode, not only the last one:
    `mkdir(parents=True, mode=…)` applies `mode` to the leaf and leaves the
    parents at the default, which would put a world-readable directory over a
    copy of the whole store.
    """
    if path.exists():
        return
    missing = [candidate for candidate in (path, *path.parents) if not candidate.exists()]
    for candidate in reversed(missing):
        candidate.mkdir(mode=0o700)


def _copy_store(conn: sqlite3.Connection, destination: Path) -> dict[str, Any]:
    """`VACUUM INTO` a whole copy of the store, then `PRAGMA integrity_check` it.

    The copy is the restore path, and it is taken the way `docs/migration.md`
    prescribes for a store upgrade. `VACUUM INTO` writes a defragmented,
    self-consistent database: it cannot tear the way a file copy of a live WAL
    database can, which is why nothing here copies `state.sqlite3` with `shutil`.
    """
    conn.execute("VACUUM INTO ?", (str(destination),))
    os.chmod(destination, 0o600)
    check = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchall()
    finally:
        check.close()
    integrity = [row[0] for row in result]
    if integrity != ["ok"]:
        raise PruneVerificationError(
            f"prune decisions: the backup copy {destination} fails integrity_check: "
            f"{'; '.join(integrity)}",
            "nothing has been deleted; investigate the store before pruning")
    return {"path": str(destination), "bytes": destination.stat().st_size,
            "integrity_check": "ok"}


def _backup_rows(conn: sqlite3.Connection, ids: Sequence[int], destination: Path,
                 batch_size: int) -> dict[str, Any]:
    """Stream every row about to be deleted to `decisions-pruned-<utc>.jsonl.gz`.

    One JSON object per row with every column, so a row can be put back with an
    INSERT. The digest is over the uncompressed lines as they were written; the
    file is read back and both the digest and the row count are checked before
    the caller deletes anything. `sha256` is the digest of the file itself, so
    an operator can confirm it later with `shasum -a 256`.
    """
    stream = hashlib.sha256()
    rows = 0
    handle = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(handle, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            for batch in _batches(ids, batch_size):
                marks = ",".join("?" for _ in batch)
                for row in conn.execute(
                        f"SELECT * FROM decisions WHERE decision_id IN ({marks})"
                        " ORDER BY decision_id", batch):
                    line = json.dumps(dict(row), sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, default=_blob).encode() + b"\n"
                    compressed.write(line)
                    stream.update(line)
                    rows += 1
        raw.flush()
        os.fsync(raw.fileno())
    _fsync_dir(destination.parent)
    if rows != len(ids):
        raise PruneVerificationError(
            f"prune decisions: {destination} holds {rows} rows for {len(ids)} to delete",
            "nothing has been deleted")
    record = {"path": str(destination), "bytes": destination.stat().st_size, "rows": rows,
              "rows_sha256": stream.hexdigest(), "sha256": _file_sha256(destination)}
    read_back = _read_rows(destination)
    if read_back != (rows, record["rows_sha256"]):
        raise PruneVerificationError(
            f"prune decisions: {destination} reads back as {read_back[0]} rows "
            f"({read_back[1]}), not {rows} ({record['rows_sha256']})",
            "nothing has been deleted; the backup is not trustworthy")
    record["verified"] = True
    return record


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_rows(path: Path) -> tuple[int, str]:
    """Count and digest the rows a `decisions-pruned-*.jsonl.gz` decompresses to."""
    digest = hashlib.sha256()
    rows = 0
    with gzip.open(path, "rb") as handle:
        for line in handle:
            digest.update(line)
            rows += 1
    return rows, digest.hexdigest()


# --- the pass ------------------------------------------------------------------

def prune(state_root: str | Path, *, apply: bool = False, confirm: bool = False,
          backup_dir: str | Path | None = None, vacuum: bool = False,
          write_report: bool = True, batch_size: int = BATCH_SIZE,
          now: str | None = None) -> PruneReport:
    """Plan, and with `apply` and `confirm` both set, perform the prune.

    Returns the `PruneReport` and, unless `write_report=False`, saves it to
    `<state root>/decisions-prune-report-<utc>.json`. Without `apply` no row, no
    backup and no VACUUM is written, and no file of this pass's own but that
    report; SQLite still creates its own `-wal` and `-shm` beside the store when
    it opens one, read-only included. Raises `PruneRefused` (exit 7) for a
    precondition, `PruneVerificationError` (exit 1) for a proof that did not
    hold, and `PruneStopped` (exit 1) for anything else that came out of the
    pass, each with the report attached once there is one to attach. A report
    that cannot be saved never replaces any of those: it is recorded on
    `report.save_error`, and a pass that otherwise finished raises
    `PruneReportUnsaved` (exit 1).
    """
    state_root = Path(state_root).expanduser()
    database = state_root / STORE_NAME
    if not database.is_file():
        raise PruneRefused(f"prune decisions: no store at {database}",
                           "pass --state-root for the state root that holds state.sqlite3")
    if apply and not confirm:
        raise PruneRefused(
            "prune decisions: --apply deletes rows and was not confirmed",
            "re-run with --apply --i-understand-this-deletes-decisions")
    backups = Path(backup_dir).expanduser() if backup_dir else state_root / BACKUPS_NAME
    report = PruneReport(str(state_root), str(database), str(backups), not apply, vacuum,
                         now or utc_now())
    version = _schema_version(database)
    report.schema = {"store": version, "build": SCHEMA_VERSION}
    if apply and 0 < version < SCHEMA_VERSION:
        # A dry run only records the mismatch: it opens the store read-only, and
        # `Store` migrates nothing on that path, so its plan is still the plan.
        raise PruneRefused(
            f"prune decisions: the store is at schema {version} and this build at "
            f"{SCHEMA_VERSION}; opening it for writing would migrate it — writing "
            f"schema_version and events rows — before this pass could copy or fingerprint it, "
            f"so the copy it offers to restore from would already be a migrated store",
            "subfleet daemon start, then subfleet daemon stop: the daemon opens the store for "
            "writing and migrates it (C-3.1), and this pass then finds the schema it expects")
    lock = _hold_daemon_lock(state_root) if apply else None
    try:
        # C-3.4: the writing pass opens the store the daemon's way (WAL,
        # synchronous=FULL, foreign_keys=ON, and the C-3.5 refusal of a schema
        # this build does not know); a dry run opens it read-only like every
        # other reader, so it cannot write even by mistake.
        store = Store(database) if apply else Store(database, read_only=True)
    except BaseException:
        _release_daemon_lock(lock)
        raise
    failure: BaseException | None = None
    try:
        _pass(store, report, database=database, state_root=state_root, backups=backups,
              apply=apply, vacuum=vacuum, batch_size=batch_size)
    except BaseException as error:
        # Anything at all, a Ctrl-C in the delete loop included: the report is
        # the only written record of the backup paths and of how far the pass
        # got, so it is saved before the error leaves this function.
        report.errors.append(f"{type(error).__name__}: {error}")
        failure = error
    # Closing and unlocking come after the last write, so an error in either is
    # recorded rather than allowed to replace the pass's outcome or skip its report.
    for release in (store.close, lambda: _release_daemon_lock(lock)):
        try:
            release()
        except BaseException as error:
            report.errors.append(f"{type(error).__name__}: {error}")
    report.finished_at = utc_now()
    unsaved: BaseException | None = None
    if write_report:
        try:
            report.save(state_root)
        except BaseException as error:
            # A full volume is the likeliest reason a save fails, and the
            # likeliest reason the pass itself failed. The save's error must not
            # replace the pass's own, or the copy it names to restore from: the
            # report stays attached, and `main` prints it in full instead.
            unsaved = error
            report.save_error = f"{type(error).__name__}: {error}"
            report.errors.append(f"report not saved: {report.save_error}")
    if failure is None:
        if unsaved is None:
            return report
        raise PruneReportUnsaved(*_unsaved(report, state_root), report) from unsaved
    if isinstance(failure, PruneError):
        failure.report = report
        raise failure
    raise PruneStopped(
        f"prune decisions: the pass stopped on {type(failure).__name__}: {failure}"
        f" after {report.deleted} rows in {report.batches} committed batches",
        _restore_fix(report.backups.get("copy", {}).get("path")), report) from failure


def _unsaved(report: PruneReport, state_root: Path) -> tuple[str, str]:
    """The message and fix for a pass that finished but could not save its report."""
    unsaved = f"its report could not be saved: {report.save_error}"
    if report.dry_run:
        return (f"prune decisions: the dry run finished, but {unsaved}",
                f"the dry run deleted nothing; free space in {state_root} or make it writable, "
                f"or pass --no-report")
    if not report.deleted:
        return (f"prune decisions: the pass had nothing to delete, but {unsaved}",
                "nothing was deleted and no backup was taken")
    return (f"prune decisions: the pass deleted {report.deleted} rows in {report.batches} "
            f"committed batches and every proof held, but {unsaved}",
            f"keep the report printed below: it names both backups and holds the pre-prune "
            f"counts a restore from {report.backups['copy']['path']} is checked against")


def _pass(store: Store, report: PruneReport, *, database: Path, state_root: Path,
          backups: Path, apply: bool, vacuum: bool, batch_size: int) -> None:
    """One pass. A dry run reads everything from one snapshot of the store.

    A real pass holds `daemon.lock` from before its first read, so nothing else
    writes while it plans. A dry run takes no lock, and a daemon that is running
    writes decision rows while it reads: without one read transaction, a row
    written between `plan` and `_check_plan` for a job still waiting makes the
    dry run refuse its own plan ("the plan would delete the row why serves"),
    which `test_a_dry_run_reads_one_snapshot_while_a_daemon_keeps_writing`
    reproduces. In WAL mode one read transaction sees one instant, so the plan,
    its check and its fingerprints agree with each other; rows written after
    that instant are simply not in the report.
    """
    if apply:
        _pass_body(store, report, database=database, state_root=state_root, backups=backups,
                   apply=apply, vacuum=vacuum, batch_size=batch_size)
        return
    store.connection.execute("BEGIN")
    try:
        _pass_body(store, report, database=database, state_root=state_root, backups=backups,
                   apply=apply, vacuum=vacuum, batch_size=batch_size)
    finally:
        store.connection.execute("ROLLBACK")


def _pass_body(store: Store, report: PruneReport, *, database: Path, state_root: Path,
               backups: Path, apply: bool, vacuum: bool, batch_size: int) -> None:
    conn = store.connection
    planned = plan(conn)
    _check_plan(conn, planned)
    report.decisions = {"total": planned.total, "keep": len(planned.keep),
                        "delete": len(planned.delete), "delete_bytes": planned.delete_bytes,
                        "jobs": len(planned.jobs), "jobs_affected": len(planned.affected),
                        "live_jobs": len(planned.live_jobs), "kept_by": planned.reasons()}
    report.database_bytes = {"before": database.stat().st_size, **_pages(conn, "before")}
    before = _fingerprints(conn)
    events_max = conn.execute("SELECT COALESCE(MAX(event_id),0) AS top FROM events").fetchone()["top"]
    events_before = _digest(conn.execute("SELECT * FROM events WHERE event_id<=?", (events_max,)))
    kept_before = _digest_ids(conn, sorted(planned.keep), batch_size)
    readers_before = _reader_bytes(conn, planned.affected)
    report.fingerprints = {"before": {**before, "events": events_before,
                                      "decisions-kept": kept_before}}
    report.space = _space(database, state_root, backups,
                          reclaimable=planned.delete_bytes, vacuum=vacuum)
    if not planned.delete:
        # Idempotence: a second pass finds the keep set is everything there is.
        # `--vacuum` belongs on the pass that deletes — rebuilding a database
        # this pass has not touched would be a rewrite with no backup behind it.
        report.verification = {"nothing_to_prune": True,
                               **({"vacuum_skipped": True} if apply and vacuum else {})}
        return
    if not apply:
        return
    if not report.space["sufficient"]:
        raise _space_refusal(report.space)

    stamp = report.started_at.replace("-", "").replace(":", "")
    copy_path = backups / f"state-{stamp}.sqlite3"
    rows_path = backups / f"decisions-pruned-{stamp}.jsonl.gz"
    _backup_dir(backups)
    for path in (copy_path, rows_path):
        # Two passes in one second would otherwise overwrite each other's
        # evidence; the stamp is second-precision like every other UTC stamp.
        if path.exists():
            raise PruneRefused(f"prune decisions: {path} already exists",
                               "move it aside, or pass --backup-dir")
    # One at a time, so a failure between them still leaves the report naming
    # the copy that was taken.
    report.backups["copy"] = _copy_store(conn, copy_path)
    report.backups["rows"] = _backup_rows(conn, planned.delete, rows_path, batch_size)

    busy = False
    for index, batch in enumerate(_batches(planned.delete, batch_size), start=1):
        marks = ",".join("?" for _ in batch)
        with store.transaction(PRUNE_KIND, data={"batch": index, "deleted": len(batch),
                                                 "first": batch[0], "last": batch[-1]}) as tx:
            tx.execute(f"DELETE FROM decisions WHERE decision_id IN ({marks})", batch)
        report.batches = index
        report.deleted += len(batch)
        if index % CHECKPOINT_BATCHES == 0:
            busy |= _checkpoint(conn)
    busy |= _checkpoint(conn)

    _verify(conn, report, planned=planned, before=before, events_max=events_max,
            events_before=events_before, kept_before=kept_before, readers_before=readers_before,
            copy_path=report.backups["copy"]["path"])

    if vacuum:
        conn.execute("VACUUM")
        # In WAL mode the rebuilt database is committed through the log, and the
        # file gives its pages back to the filesystem only once that log is
        # checkpointed: measured here, a VACUUM without this leaves the file the
        # size it was.
        busy |= _checkpoint(conn)
        # The rebuild rewrites every page, so the proofs are taken again over
        # the file the pass actually leaves behind, not only over the one the
        # delete left.
        _reverify(conn, report, planned=planned, before=before, events_max=events_max,
                  events_before=events_before, kept_before=kept_before,
                  readers_before=readers_before, copy_path=report.backups["copy"]["path"])
    report.verification["wal_checkpoint_busy"] = busy
    report.database_bytes.update({"after": database.stat().st_size, **_pages(conn, "after")})


def _checkpoint(conn: sqlite3.Connection) -> bool:
    """`PRAGMA wal_checkpoint(TRUNCATE)`; True when it could not truncate the log.

    The pragma returns one row, `(busy, log, checkpointed)`, and reports another
    connection holding the log in `busy` rather than raising: measured on a
    synthetic store, `(0, 0, 0)` idle and `(1, n, 0)` — busy, n frames in the
    log, none checkpointed — with a second connection inside a read
    transaction. A busy checkpoint leaves the log where it was, and after a
    VACUUM leaves the file the size it was, so the pass records it and says so
    rather than reporting a rebuild that never reached the filesystem.
    """
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return bool(row[0]) if row is not None else False


def _pages(conn: sqlite3.Connection, when: str) -> dict[str, Any]:
    return {f"page_size_{when}": conn.execute("PRAGMA page_size").fetchone()[0],
            f"page_count_{when}": conn.execute("PRAGMA page_count").fetchone()[0],
            f"freelist_{when}": conn.execute("PRAGMA freelist_count").fetchone()[0]}


def _invariants(conn: sqlite3.Connection, *, planned: Plan, before: dict[str, dict[str, Any]],
                events_max: int, events_before: dict[str, Any], kept_before: dict[str, Any],
                readers_before: dict[str, dict[str, Any]]
                ) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """What must be true of the store at any point after the delete.

    Every other table counted and digested again and matching byte for byte;
    `events` holding the rows it already had, unchanged; `decisions` being
    exactly the rows the plan kept, unchanged; both readers returning the same
    bytes for every affected job; `PRAGMA integrity_check` and
    `PRAGMA foreign_key_check` clean. Returns what did not hold, what was found,
    and the after-fingerprints. `_verify` takes it once the last batch has
    committed and `_reverify` again once `--vacuum` has rewritten every page.
    """
    mismatches: list[str] = []
    after = _fingerprints(conn)
    changed_tables = [table for table in sorted(set(before) | set(after))
                      if before.get(table) != after.get(table)]
    for table in changed_tables:
        mismatches.append(f"{table} changed: {before.get(table)} -> {after.get(table)}")
    events_after = _digest(conn.execute("SELECT * FROM events WHERE event_id<=?", (events_max,)))
    if events_after != events_before:
        mismatches.append(f"events rewrote history: {events_before} -> {events_after}")
    kept_after = _digest(conn.execute("SELECT * FROM decisions"))
    if kept_after != kept_before:
        mismatches.append(f"the kept decisions changed: {kept_before} -> {kept_after}")
    readers_after = _reader_bytes(conn, planned.affected)
    differing = sorted(job for job in readers_before if readers_before[job] != readers_after.get(job))
    if differing:
        mismatches.append(f"a reader returns different bytes for {', '.join(differing[:5])}"
                          + (f" and {len(differing) - 5} more" if len(differing) > 5 else ""))
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != ["ok"]:
        mismatches.append(f"integrity_check: {'; '.join(integrity)}")
    if foreign_keys:
        mismatches.append(f"foreign_key_check reported {len(foreign_keys)} rows")
    found = {
        "tables_checked": sorted(before),
        "tables_identical": not changed_tables,
        "events_append_only": events_after == events_before,
        "readers_identical": len(readers_before) - len(differing),
        "readers_checked": len(readers_before),
        "decisions_after": kept_after["rows"],
        "decisions_identical": kept_after == kept_before,
        "integrity_check": "ok" if integrity == ["ok"] else "; ".join(integrity),
        "foreign_key_check": "ok" if not foreign_keys else f"{len(foreign_keys)} rows",
    }
    return mismatches, found, {**after, "events": events_after, "decisions-kept": kept_after}


def _verify(conn: sqlite3.Connection, report: PruneReport, *, planned: Plan,
            before: dict[str, dict[str, Any]], events_max: int,
            events_before: dict[str, Any], kept_before: dict[str, Any],
            readers_before: dict[str, dict[str, Any]], copy_path: str) -> None:
    """Prove the pass touched `decisions` and nothing else, and raise if it did not.

    The invariants above, plus the two things that are true only of the delete
    itself: `events` gained exactly one new row per committed batch, each of
    kind `decisions.pruned` (C-3.2), and every job that had a decision still has
    one, so `COUNT(DISTINCT job_id)` over `decisions` has not moved.
    """
    mismatches, found, fingerprints = _invariants(
        conn, planned=planned, before=before, events_max=events_max,
        events_before=events_before, kept_before=kept_before, readers_before=readers_before)
    new_events = conn.execute("SELECT kind, COUNT(*) AS n FROM events WHERE event_id>?"
                              " GROUP BY kind", (events_max,)).fetchall()
    added = {row["kind"]: row["n"] for row in new_events}
    if added != ({PRUNE_KIND: report.batches} if report.batches else {}):
        mismatches.append(f"events gained {added or 'nothing'}, not {report.batches} "
                          f"{PRUNE_KIND} rows")
    jobs_after = conn.execute("SELECT COUNT(DISTINCT job_id) AS n FROM decisions").fetchone()["n"]
    if jobs_after != len(planned.jobs):
        mismatches.append(f"{jobs_after} jobs have decisions, not {len(planned.jobs)}")
    report.fingerprints["after"] = fingerprints
    report.verification = {**found, "events_added": added, "ok": not mismatches}
    if mismatches:
        raise PruneVerificationError(
            "prune decisions: the store is not what the plan promised: " + "; ".join(mismatches),
            _restore_fix(copy_path))


def _reverify(conn: sqlite3.Connection, report: PruneReport, *, planned: Plan,
              before: dict[str, dict[str, Any]], events_max: int,
              events_before: dict[str, Any], kept_before: dict[str, Any],
              readers_before: dict[str, dict[str, Any]], copy_path: str) -> None:
    """Take the same proofs again over the file `--vacuum` left behind.

    `VACUUM` rewrites every page, so `PRAGMA integrity_check` on its own would
    only say the new file is well formed, not that it holds the same rows. The
    kept set is a row or two per job, so the fingerprints, the kept digest and
    both readers are simply asked again and compared against the same
    before-values `_verify` used.
    """
    mismatches, found, fingerprints = _invariants(
        conn, planned=planned, before=before, events_max=events_max,
        events_before=events_before, kept_before=kept_before, readers_before=readers_before)
    report.fingerprints["after_vacuum"] = fingerprints
    report.verification["after_vacuum"] = {**found, "ok": not mismatches}
    report.verification["integrity_check_after_vacuum"] = found["integrity_check"]
    if mismatches:
        raise PruneVerificationError(
            "prune decisions: the rebuilt database is not what the delete left: "
            + "; ".join(mismatches), _restore_fix(copy_path))


# --- module entry point --------------------------------------------------------

def _fail(code: int, message: str, fix: str | None = None,
          report: PruneReport | None = None) -> int:
    """The CLI's refusal shape (`cli.fail`): the message, then the fix (C-17.4).

    A pass that got as far as planning has a report, and it is the record of
    the backups, of how far the pass got and of the pre-prune counts a restore
    is checked against. A saved one is named; one that is not on disk, because
    saving it failed or `--no-report` asked for none, is printed in full. The
    message is kept to one line, since an error's own text can hold a line
    break; the fix is a recipe and keeps its lines.
    """
    print(f"subfleet: {_one_line(message)}", file=sys.stderr)
    if fix:
        print(f"  fix: {fix}", file=sys.stderr)
    if report is not None and report.path:
        print(f"  report: {report.path}", file=sys.stderr)
    elif report is not None:
        why = (f"saving it failed: {_one_line(report.save_error)}" if report.save_error
               else "--no-report")
        print(f"  report: not saved ({why}); here it is in full:", file=sys.stderr)
        print(json.dumps(report.as_dict(), indent=1, sort_keys=True, default=str),
              file=sys.stderr)
    return code


def _print(report: PruneReport) -> None:
    decisions, space = report.decisions, report.space
    rows = [
        ("store", f"{report.database} ({_bytes(report.database_bytes['before'])})"),
        ("mode", ("dry run — no row, no backup, no VACUUM"
                  + (", only this report" if report.path else ""))
                 if report.dry_run else "apply"),
        ("decisions", f"{decisions['total']} rows over {decisions['jobs']} jobs"),
        ("keep", f"{decisions['keep']} (" + ", ".join(
            f"{reason} {count}" for reason, count in decisions["kept_by"].items()) + ")"),
        ("delete", f"{decisions['delete']} rows over {decisions['jobs_affected']} jobs, "
                   f"{_bytes(decisions['delete_bytes'])} of decision_json"),
        ("live jobs", f"{decisions['live_jobs']} kept whole (not terminal)"),
    ]
    if report.schema.get("store") not in (None, report.schema.get("build")):
        # Next to the store it describes, because it decides whether --apply runs.
        rows.insert(1, ("schema", f"the store is at {report.schema['store']} and this build at "
                                  f"{report.schema['build']}; --apply is refused until the "
                                  f"daemon has migrated it"))
    for mount, volume in sorted(space["volumes"].items()):
        rows.append(("free space", f"{mount}: {_bytes(volume['free'])} free, "
                                   f"{_bytes(volume['needed'])} needed"
                                   f"{'' if volume['sufficient'] else '  <- NOT ENOUGH'}"))
    for name, backup in sorted(report.backups.items()):
        rows.append((f"backup ({name})", f"{backup['path']} ({_bytes(backup['bytes'])})"
                                         + (f", {backup['rows']} rows, sha256 {backup['sha256']}"
                                            if "rows" in backup else ", integrity_check ok")))
    if report.batches:
        rows.append(("deleted", f"{report.deleted} rows in {report.batches} "
                                f"batch{'' if report.batches == 1 else 'es'}, "
                                f"one {PRUNE_KIND} event each"))
        verification = report.verification
        rows.append(("verified", f"every other table identical; events append-only; "
                                 f"{verification['readers_identical']}/"
                                 f"{verification['readers_checked']} jobs read identical bytes; "
                                 f"integrity_check {verification['integrity_check']}, "
                                 f"foreign_key_check {verification['foreign_key_check']}"))
        if verification.get("after_vacuum"):
            rebuilt = verification["after_vacuum"]
            rows.append(("verified (rebuilt)",
                         f"--vacuum rewrote every page and the same proofs were taken again: "
                         f"every other table identical; {rebuilt['readers_identical']}/"
                         f"{rebuilt['readers_checked']} jobs read identical bytes; "
                         f"integrity_check {rebuilt['integrity_check']}"))
        rows.append(("database", f"{_bytes(report.database_bytes['after'])}, "
                                 f"{report.database_bytes['page_count_after']} pages, "
                                 f"{report.database_bytes['freelist_after']} free"
                                 + ("" if report.vacuum else
                                    " — the freed pages stay in the file; --vacuum returns them "
                                    "to the filesystem and has to be passed on the pass that "
                                    "deletes, since a later one has nothing to delete")))
    if report.verification.get("vacuum_skipped"):
        rows.append(("vacuum", "skipped: this pass deleted nothing, and --vacuum belongs on the "
                               "pass that deletes and took the backups. To rebuild the file now, "
                               "stop the daemon and run `sqlite3 <store> 'VACUUM'`"))
    if report.verification.get("wal_checkpoint_busy"):
        rows.append(("write-ahead log", "another connection held it open, so "
                                        "wal_checkpoint(TRUNCATE) could not truncate the log"
                     + (" and the rebuilt file did not give its pages back to the filesystem"
                        if report.vacuum else "")))
    rows.extend(("error", _one_line(error)) for error in report.errors)
    for label, value in rows:
        print(f"{label:<16} {value}")
    if report.path:
        print(f"{'report':<16} {report.path}")


def _one_line(text: str) -> str:
    """`text` on one line: a line break in an error must not start a row of its own."""
    return "".join(char if char.isprintable() else char.encode("unicode_escape").decode()
                   for char in text)


def _show(report: PruneReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), sort_keys=True))
    else:
        _print(report)


def main(argv: list[str] | None = None) -> int:
    """`python -m subfleet.prune_decisions`; the CLI keeps its own verbs."""
    parser = argparse.ArgumentParser(
        prog="subfleet.prune_decisions",
        description="delete the redundant decisions rows a waiting job accumulated "
                    "(the daemon must be stopped)")
    parser.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME") or "~/.subfleet")
    parser.add_argument("--backup-dir", default=None,
                        help="where the backups go (default: <state root>/backups), "
                             "for instance a directory on another volume")
    parser.add_argument("--apply", action="store_true",
                        help="delete rows; without it the pass plans and writes nothing")
    parser.add_argument("--i-understand-this-deletes-decisions", dest="confirm",
                        action="store_true", help="required beside --apply")
    parser.add_argument("--vacuum", action="store_true",
                        help="rebuild the database after the delete so the freed pages "
                             "return to the filesystem")
    parser.add_argument("--no-report", dest="report", action="store_false",
                        help="print the report without saving it")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if args.apply and not args.confirm:
        return _fail(PruneRefused.code, "prune decisions: --apply deletes rows and was not "
                                        "confirmed",
                     "re-run with --apply --i-understand-this-deletes-decisions")
    try:
        report = prune(args.state_root, apply=args.apply, confirm=args.confirm,
                       backup_dir=args.backup_dir, vacuum=args.vacuum, write_report=args.report)
    except PruneError as stop:
        # Every refusal, failed proof, unhandled stop and unsaved report
        # arrives in one shape. A pass that finished still prints its report
        # where a finished pass does, after the record on stderr, so a stdout
        # that cannot be written loses nothing; only the file is missing.
        code = _fail(stop.code, str(stop), stop.fix, stop.report)
        if isinstance(stop, PruneReportUnsaved):
            _show(stop.report, args.json)
        return code
    except SchemaVersionError as mismatch:
        # C-3.5: the store is newer than this build knows, and the message
        # carries both versions; `Store` raises it before the pass has a report.
        return _fail(mismatch.code, f"prune decisions: {mismatch}",
                     "run this pass from the build that wrote the store")
    except (sqlite3.Error, OSError) as error:
        # Reading the schema or opening the store: everything after that comes
        # out of `prune` as a `PruneError` carrying its report.
        return _fail(PruneStopped.code, f"prune decisions: {type(error).__name__}: {error}",
                     "the pass stopped before it planned, so it deleted nothing and has "
                     "no report; fix what the error names and run it again")
    _show(report, args.json)
    return 0


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
