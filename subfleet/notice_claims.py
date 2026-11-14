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

from .store import notice_fingerprint


def receipt_path(root: Path, session: str) -> Path:
    key = hashlib.sha256(session.encode()).hexdigest()
    return Path(root) / "hooks" / "notice-receipts" / (key + ".json")


@contextmanager
def delivery_lock(root: Path, session: str):
    """Nonblocking: neither a prompt nor one busy caller stalls the pass."""
    path = receipt_path(root, session).with_suffix(".lock")
    fd = None
    held = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError:
        pass
    try:
        yield held
    finally:
        if fd is not None:
            os.close(fd)


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
    return receipt_path(root, session).with_suffix(".activity.json")


def read_activity(root: Path, session: str) -> dict | None:
    try:
        value = json.loads(activity_path(root, session).read_text())
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


def record_offline_activity(root: Path, session: str, *, event: str, source: str | None) -> None:
    """Keep the quiet clock even when this hook has no new notices to print."""
    if event not in ("SessionStart", "UserPromptSubmit"):
        raise ValueError("invalid notice activity event")
    previous = read_activity(root, session) or {}
    now = time.time()
    human_at = previous.get("human_at")
    if event == "UserPromptSubmit":
        human_at = max(now, human_at or 0)
    value = {"at": max(now, previous.get("at") or 0),
             "source": source if event == "SessionStart" and isinstance(source, str) else None,
             "human_at": human_at}
    path = activity_path(root, session)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reserve_offline(root: Path, session: str, rows: list[dict], *,
                    event: str, source: str | None = None) -> list[dict]:
    """Called under delivery_lock, before an offline hook prints anything."""
    record_offline_activity(root, session, event=event, source=source)
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


def reconcile(daemon, session: str) -> None:
    """Called under delivery_lock; replay receipts into the daemon's store."""
    from .protocol import notice_row
    receipts = read_receipts(daemon.root, session)
    activity = read_activity(daemon.root, session)
    if not receipts and activity is None:
        return
    with daemon.store.transaction("notice.offline_surfaced", data={"session_id": session}) as tx:
        if activity is not None:
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
        for item in receipts:
            table, row_id = notice_row(item["notice_id"])
            row = tx.execute(f"SELECT text,created_at FROM {table} WHERE notice_id=? AND session_id=?",
                             (row_id, session)).fetchone()
            if row is not None and notice_fingerprint(dict(row)) == item["fingerprint"]:
                tx.execute(f"UPDATE {table} SET state='surfaced',transport='hook:offline' "
                           "WHERE notice_id=? AND session_id=? AND state IN ('pending','offered')",
                           (row_id, session))
    receipt_path(daemon.root, session).unlink(missing_ok=True)
    activity_path(daemon.root, session).unlink(missing_ok=True)
