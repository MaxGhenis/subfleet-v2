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

from .store import notice_fingerprint


def receipt_path(root: Path, session: str) -> Path:
    key = hashlib.sha256(session.encode()).hexdigest()
    return Path(root) / "hooks" / "notice-receipts" / (key + ".json")


#: How often a waiting `delivery_lock` or `activity_lock` tries the lock again.
LOCK_POLL_S = 0.01
#: The longest anyone waits for a caller's activity lock (review r4 of #307).
#: A hook holds it to publish one record (a rename) or, under the delivery lock,
#: to fold a session's few records; a wake's claim holds it through its
#: transaction. A hook that cannot take it in time publishes all the same, and a
#: claim that cannot leaves its caller's notices for a later pass, so neither a
#: stopped hook nor a slow claim stalls the other side.
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

    Yields whether the lock is held; it is tried for at most `wait_s`, so no
    holder stalls a hook or the pass (review r4 of #307). `wait_s` has no
    default: a caller that forgot it would run its body unlocked. Hooks release this
    short lock before attempting delivery_lock. The daemon, and a hook's fold,
    take delivery_lock first, so the two locks cannot form a wait cycle.
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


def _merge_activity(records: list[tuple[Path, dict]]) -> dict | None:
    latest = None
    human_at = None
    for _path, value in records:
        if latest is None or value["at"] >= latest["at"]:
            latest = value
        if value["human_at"] is not None:
            human_at = max(value["human_at"], human_at or 0)
    return {**latest, "human_at": human_at} if latest is not None else None


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
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fold_activity(root: Path, session: str) -> int:
    """Called under delivery_lock: keep a session's activity files bounded.

    Each offline hook publishes one record. A hook that holds the delivery lock
    then folds every record it reads into the aggregate file with one atomic
    replace and unlinks exactly the records it folded, as `reconcile` does
    (review r4 of #307). The fold equals `_merge_activity` over those records:
    the latest `at` with its `source`, and the latest `human_at`. The delivery
    lock orders it against `reconcile` and the wake claim; it takes the activity
    lock second, the daemon's order. A record published after its snapshot
    survives. A crash between the replace and the unlinks leaves the folded
    records beside the aggregate, which merge to the same value. A corrupt
    record raises ValueError before anything is written or removed, so it still
    fences its caller. Returns how many published records it folded: none when
    the activity lock stays taken past `ACTIVITY_LOCK_WAIT_S`.
    """
    aggregate = activity_path(root, session)
    with activity_lock(root, session, wait_s=ACTIVITY_LOCK_WAIT_S) as held:
        if not held:
            return 0
        records = _activity_records(root, session)
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
    clock = tx.execute("SELECT last_wake_at FROM caller_wake_sessions WHERE session_id=?",
                       (session,)).fetchone()
    last_wake = clock["last_wake_at"] if clock else None
    human_at = activity["human_at"]
    reset = human_at is not None and (last_wake is None or human_at >= last_wake)
    tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source) VALUES(?,?,?) "
               "ON CONFLICT(session_id) DO UPDATE SET active_at=max(active_at,excluded.active_at),"
               "source=CASE WHEN excluded.active_at>=active_at THEN excluded.source ELSE source END,"
               "wake_streak=CASE WHEN ? THEN 0 ELSE wake_streak END,"
               "last_wake_at=CASE WHEN ? THEN NULL ELSE last_wake_at END",
               (session, activity["at"], activity["source"], reset, reset))


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
