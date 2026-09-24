"""The legacy cockpit's v1 state, built in code (C-30.4).

`outbox.sqlite3` in the schema `outbox.py:123-136` creates; payloads as
`Outbox.enqueue` stores them; receipts as `native_dispatch.dispatch` returns
them and `Outbox.update_active`, `cancel` and `resolve_handled` rewrite them
(read from the cockpit's source and a private copy of the real outbox,
2026-09-24; `subfleet/conversations/legacy.py` cites the lines). No real prompt,
session or receipt is copied here; the ids are fixtures.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tests import sessions_fixtures as fx

LEGACY_SESSION = "5e551011-0000-4000-8000-00000000000a"
QUEUED_SESSION = "5e551011-0000-4000-8000-00000000000b"
LEGACY = tuple(f"1e9ac700-0000-4000-8000-{n:012d}" for n in range(1, 11))

OUTBOX_SCHEMA = """CREATE TABLE messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    receipt TEXT NOT NULL
);
CREATE INDEX messages_session_status ON messages(session_id, status, sequence);"""


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def outbox_row(message_id: str, session: str, status: str, prompt: str, *, at: float = 60,
               provider: str = "claude", receipt: dict | None = None,
               images: int = 0) -> tuple:
    """One `messages` row as the cockpit wrote it, `at` seconds ago."""
    session_id = f"{provider}:{session}"
    payload = {"image_paths": [f"/v1/outbox-attachments/message-x/image-{n}.png" for n in range(images)],
               "image_sha256": ["0" * 64] * images, "message_id": message_id, "prompt": prompt,
               "service_tier": None, "session_id": session_id}
    dispatched = {"error": None, "kind": "subfleet.session-continuation",
                  "message": "Message submitted through the persistent native provider connection.",
                  "message_id": message_id, "native_message_id": message_id, "ok": True, "pid": 4242,
                  "provider": provider, "run_id": f"20260831-120000-message-{message_id[:8]}",
                  "schema_version": 1, "session_id": session_id, "status": "dispatched"}
    if receipt is None:
        receipt = {
            "finished": {**dispatched, "message": "The provider finished this turn."},
            "error": {**dispatched, "ok": False, "error": "provider-failed",
                      "message": "The provider stopped with exit code 1."},
            "cancelled": {"ok": True, "message": "Cancelled before dispatch."},
            "queued": {"ok": True, "message": "Queued locally. Preparing the provider session."},
            "delivery-unknown": {"ok": False, "error": "delivery-unknown", "message": "The broker stopped "
                                 "during dispatch. Delivery is unknown; this message will not be replayed "
                                 "automatically."},
        }.get(status, dispatched)
    stamp = datetime.now(timezone.utc).timestamp() - at
    digest = hashlib.sha256(_compact(payload).encode()).hexdigest()
    return (message_id, session_id, digest, digest, _compact(payload), status, stamp, stamp + 30,
            _compact(receipt))


def write_outbox(state: Path, rows: list[tuple]) -> Path:
    """`<v1 state>/outbox.sqlite3`, replaced with exactly these rows."""
    path = state / "outbox.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(OUTBOX_SCHEMA)
    connection.executemany(
        "INSERT INTO messages(message_id,session_id,request_digest,payload_digest,payload,"
        "status,created_at,updated_at,receipt) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return path


def write_transcript(claude: Path, session: str, cwd: Path, *, mode: str = "default",
                     model: str = "claude-opus-5-5") -> Path:
    """A Claude transcript under `<claude>/projects`, as `catalog.claude_session` reads one."""
    return fx.transcript(claude, session, [
        fx.user_text("the session's own first prompt", cwd=str(cwd), permissionMode=mode),
        fx.assistant_text("an answer", cwd=str(cwd), model=model),
    ], cwd=str(cwd))
