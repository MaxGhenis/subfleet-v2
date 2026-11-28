"""Hook receipts when the daemon is unavailable (C-15.9).

The hook never writes SQLite. A per-session file lock fences its read/receipt
against a waking daemon; a receipt records only notice ids and fingerprints.
Both paths reserve delivery before writing to their external recipient.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import time
from pathlib import Path
from time import monotonic as _monotonic, sleep as _sleep, time_ns as _time_ns
from uuid import uuid4

from .sessions.nudge import notice_source_allows
from .store import notice_fingerprint


def receipt_path(root: Path, session: str) -> Path:
    key = hashlib.sha256(session.encode()).hexdigest()
    return Path(root) / "hooks" / "notice-receipts" / (key + ".json")


#: How often a waiting `delivery_lock` or `activity_lock` tries the lock again.
LOCK_POLL_S = 0.01
#: The polling budget per acquisition of a caller's activity lock. A hook holds
#: it to publish one record or, under the delivery lock, to fold a snapshot of
#: activity. A wake claim holds it through its transaction and shares a
#: pass-wide wait budget with the other claims. A hook publishes even when it
#: misses the lock. A claim that misses the lock leaves the notices pending.
ACTIVITY_LOCK_WAIT_S = 0.25


@contextmanager
def _file_lock(path: Path, *, wait_s: float, gone=None):
    """An exclusive flock on `path`, tried for at most `wait_s`; yields whether held.

    An error opening the lock file is reported as not held, never raised.
    """
    fd = None
    held = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        until = _monotonic() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                left = until - _monotonic()
                if left <= 0 or (gone is not None and gone()):
                    break
                _sleep(min(LOCK_POLL_S, left))
    except OSError:
        pass
    try:
        yield held
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def delivery_lock(root: Path, session: str, *, wait_s: float = 0.0, gone=None):
    """Nonblocking by default: neither a prompt nor one busy caller stalls the pass.

    `wait_s` tries a taken lock again for up to that long, and `gone`, when it
    turns true, ends the wait early: a PostToolUse claim waits out a pass's or a
    hook's brief hold rather than leave its result to a later wake (C-15.9).
    """
    with _file_lock(receipt_path(root, session).with_suffix(".lock"), wait_s=wait_s, gone=gone) as held:
        yield held


def read_receipts(root: Path, session: str) -> list[dict]:
    try:
        value = json.loads(receipt_path(root, session).read_text())
    except FileNotFoundError:
        return []
    # An unreadable/corrupt receipt must fail closed, never repeat a delivery.
    if not isinstance(value, list) or not all(
            isinstance(x, dict) and isinstance(x.get("notice_id"), int)
            and not isinstance(x["notice_id"], bool) and x["notice_id"] != 0
            and isinstance(x.get("fingerprint"), str) for x in value):
        raise ValueError("invalid notice receipts")
    return value



def activity_path(root: Path, session: str) -> Path:
    """The aggregate activity file: what older hooks wrote, and what a fold writes
    (`fold_activity`). Replayed with the published records beside it."""
    return receipt_path(root, session).with_suffix(".activity.json")


@contextmanager
def activity_lock(root: Path, session: str, *, wait_s: float):
    """Order activity publication against the final wake claim, never rendering.

    Yields whether the lock was acquired within the caller's required `wait_s`
    polling budget. Hooks release the publication lock before attempting
    delivery_lock. The daemon and a hook's fold take delivery_lock first, so
    the two locks cannot form a wait cycle.
    """
    with _file_lock(activity_path(root, session).with_suffix(".lock"), wait_s=wait_s) as held:
        yield held


def _read_activity(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None

    def valid_time(stamp):
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return False
        try:
            return math.isfinite(stamp) and stamp >= 0
        except OverflowError:
            return False

    if (not isinstance(value, dict) or set(value) != {"at", "source", "human_at"}
            or not valid_time(value.get("at"))
            or not (value.get("source") is None or isinstance(value["source"], str))
            or not (value.get("human_at") is None or valid_time(value["human_at"]))):
        raise ValueError("invalid notice activity")
    return value


def _activity_records(root: Path, session: str) -> list[tuple[Path, dict]]:
    """Snapshot published, immutable records, including the legacy aggregate."""
    legacy = activity_path(root, session)
    try:
        paths = sorted(legacy.with_suffix(".d").iterdir())
    except FileNotFoundError:
        paths = []
    records = []
    for path in [legacy, *(path for path in paths if path.suffix == ".json")]:
        value = _read_activity(path)
        if value is not None:
            records.append((path, value))
    return records


def _activity_order(at: float, source: str | None) -> tuple[float, bool, bool, str]:
    """Prefer newer activity, then a holding source, then lexical order with None first."""
    return at, not notice_source_allows(source), source is not None, source or ""


def _merge_activity(records: list[tuple[Path, dict]]) -> dict | None:
    latest = max((value for _path, value in records),
                 key=lambda value: _activity_order(value["at"], value["source"]),
                 default=None)
    if latest is None:
        return None
    human_at = max((value["human_at"] for _path, value in records if value["human_at"] is not None),
                   default=None)
    return {**latest, "human_at": human_at}


def read_activity(root: Path, session: str) -> dict | None:
    return _merge_activity(_activity_records(root, session))


def record_offline_activity(root: Path, session: str, *, event: str, source: str | None,
                            personal: bool = True) -> None:
    """Keep the quiet clock even when this hook has no new notices to print.

    Only a personal prompt is personal input: Subfleet's own inbox envelope
    moves the quiet clock but not `human_at` (review r1 of #307, finding 2).
    """
    if event not in ("SessionStart", "UserPromptSubmit"):
        raise ValueError("invalid notice activity event")
    # Preserve the fail-closed behavior for corrupt activity evidence.
    read_activity(root, session)
    now = time.time()
    value = {"at": now,
             "source": source if event == "SessionStart" and isinstance(source, str) else None,
             "human_at": now if event == "UserPromptSubmit" and personal else None}
    directory = activity_path(root, session).with_suffix(".d")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Each hook publishes its own record. Neither another hook nor cleanup of
    # an earlier reconciliation snapshot can overwrite or unlink this write.
    path = directory / f"{_time_ns():020d}-{uuid4().hex}.json"
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # Published whether or not the lock came in time: activity is never
        # lost. Without it, this hook races a claim already in progress.
        with activity_lock(root, session, wait_s=ACTIVITY_LOCK_WAIT_S):
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fold_activity(root: Path, session: str) -> int:
    """Under delivery_lock, merge published records and clean stopped folds.

    Under the activity lock, it snapshots activity and replaces the aggregate
    with `_merge_activity` of that snapshot, preserving the latest activity,
    the source tie rule and the latest human input. It then unlinks the
    published records it folded. Late publications and duplicates left after
    replacement still merge with the aggregate. Holding both locks lets it
    remove abandoned aggregate temporaries even with no records to fold.

    A corrupt record raises ValueError before any files are written or removed
    and fences delivery. The hook can still deliver after an OSError from this
    housekeeping. Returns the number of published records folded, or zero if
    the activity lock stays taken past `ACTIVITY_LOCK_WAIT_S`.
    """
    aggregate = activity_path(root, session)
    with activity_lock(root, session, wait_s=ACTIVITY_LOCK_WAIT_S) as held:
        if not held:
            return 0
        records = _activity_records(root, session)
        # Aggregate temporaries are written only while holding both locks.
        # A temporary still present here belongs to a fold that has stopped.
        temporary_pattern = f"{aggregate.name}." + "[0-9a-f]" * 32 + ".tmp"
        for abandoned in aggregate.parent.glob(temporary_pattern):
            abandoned.unlink(missing_ok=True)
        folded = [path for path, _value in records if path != aggregate]
        if not folded:
            return 0
        temporary = aggregate.with_name(f"{aggregate.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x") as stream:
                os.chmod(temporary, 0o600)
                json.dump(_merge_activity(records), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, aggregate)
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_directory(aggregate.parent)      # the replace is durable before any unlink
        for path in folded:
            path.unlink(missing_ok=True)
    return len(folded)


def reserve_offline(root: Path, session: str, rows: list[dict]) -> list[dict]:
    """Called under delivery_lock, before an offline hook prints anything."""
    receipts = read_receipts(root, session)
    seen = {(item["notice_id"], item["fingerprint"]) for item in receipts}
    fresh = [row for row in rows if
             (row["notice_id"], notice_fingerprint(row)) not in seen]
    if not fresh:
        return []
    receipts.extend({"notice_id": row["notice_id"], "fingerprint": notice_fingerprint(row)}
                    for row in fresh)
    path = receipt_path(root, session)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump(receipts, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return fresh


def apply_activity(tx, session: str, activity: dict | None) -> None:
    """Apply a snapshot without consuming files, also inside a final wake claim."""
    if activity is None:
        return
    clock = tx.execute("SELECT active_at,source,last_wake_at FROM caller_wake_sessions WHERE session_id=?",
                       (session,)).fetchone()
    last_wake = clock["last_wake_at"] if clock else None
    human_at = activity["human_at"]
    reset = human_at is not None and (last_wake is None or human_at >= last_wake)
    at, source = activity["at"], activity["source"]
    if (clock is not None
            and _activity_order(clock["active_at"], clock["source"]) > _activity_order(at, source)):
        at, source = clock["active_at"], clock["source"]
    tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source) VALUES(?,?,?) "
               "ON CONFLICT(session_id) DO UPDATE SET active_at=excluded.active_at,source=excluded.source,"
               "wake_streak=CASE WHEN ? THEN 0 ELSE wake_streak END,"
               "last_wake_at=CASE WHEN ? THEN NULL ELSE last_wake_at END",
               (session, at, source, reset, reset))


def reconcile(daemon, session: str) -> None:
    """Called under delivery_lock; replay receipts into the daemon's store."""
    from .protocol import notice_row
    receipts = read_receipts(daemon.root, session)
    records = _activity_records(daemon.root, session)
    activity = _merge_activity(records)
    if not receipts and activity is None:
        return
    with daemon.store.transaction("notice.offline_surfaced", data={"session_id": session}) as tx:
        apply_activity(tx, session, activity)
        for item in receipts:
            table, row_id = notice_row(item["notice_id"])
            row = tx.execute(f"SELECT text,created_at FROM {table} WHERE notice_id=? AND session_id=?",
                             (row_id, session)).fetchone()
            if row is not None and notice_fingerprint(dict(row)) == item["fingerprint"]:
                tx.execute(f"UPDATE {table} SET state='surfaced',transport='hook:offline' "
                           "WHERE notice_id=? AND session_id=? AND state IN ('pending','offered')",
                           (row_id, session))
    receipt_path(daemon.root, session).unlink(missing_ok=True)
    for path, _activity in records:
        path.unlink(missing_ok=True)
