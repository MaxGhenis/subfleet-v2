"""Restore exact lease spellings before rolling back canonical identity.

Stop the daemon first. Pass an explicit state root; no default/live home is read.
Preview: python tools/prepare_identity_rollback.py --root /path/to/state
Apply:   python tools/prepare_identity_rollback.py --root /path/to/state --apply
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subfleet.conversations.store import canonical_native  # noqa: E402


def exact_key(conn, root: Path, lease) -> str:
    key, holder = lease["lease_key"], lease["holder"]
    if not key.startswith(("out:", "native:", "native-session:", "session:")):
        return key
    job = conn.execute("SELECT j.* FROM jobs j WHERE j.job_id=? UNION "
                       "SELECT j.* FROM jobs j JOIN attempts a USING(job_id) WHERE a.attempt_id=?",
                       (holder, holder)).fetchone()
    if job is None:
        raise ValueError(f"cannot recover exact spelling for lease {key}: unknown holder {holder}")
    if key.startswith("out:"):
        if not job["out_path"]:
            raise ValueError(f"output lease {key} has no stored output path")
        return "out:" + job["out_path"]
    if key.startswith("session:") and not key.endswith(":revive"):
        return key
    manifest_path = root / "jobs" / job["job_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    turn = manifest.get("turn") or {}
    candidates = [
        (manifest.get("resume") or {}).get("native_session_id"),
        turn.get("native_session_id"), turn.get("new_session_id"),
        job["caller_session"] if job["kind"] == "revive" else None,
    ]
    candidates.extend(row[0] for row in conn.execute(
        "SELECT native_session_id FROM attempts WHERE job_id=? ORDER BY seq DESC", (job["job_id"],)))
    subject = key[8:-7] if key.startswith("session:") else key.split(":", 2)[2]
    raw = next((value for value in candidates if value and canonical_native(value) == canonical_native(subject)), None)
    if raw is None:
        raise ValueError(f"cannot recover exact native spelling for lease {key}")
    if key.startswith("session:"):
        return f"session:{raw}:revive"
    prefix, scope, _ = key.split(":", 2)
    return f"{prefix}:{scope}:{raw}"


def prepare(root: Path, *, apply: bool = False) -> list[tuple[str, str]]:
    """One atomic rewrite, preserving holders/timestamps and refusing collisions."""
    root = root.resolve(strict=True)
    database = root / "state.sqlite3"
    if not database.is_file():
        raise ValueError(f"no state.sqlite3 at {root}")
    # Same flock as the daemon; an active writer makes this repair refuse.
    with (root / "daemon.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("daemon is running; stop it before preparing rollback") from None
        with contextlib.closing(sqlite3.connect(f"file:{database}?mode=rw", uri=True)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute("SELECT * FROM leases ORDER BY acquired_at,lease_key"))
            rewritten, changes = {}, []
            for row in rows:
                exact = exact_key(conn, root, row)
                existing = rewritten.get(exact)
                if existing is not None and existing["holder"] != row["holder"]:
                    raise ValueError(f"rollback would give lease {exact} to multiple holders; drain those jobs first")
                if existing is not None and existing["expires_at"] != row["expires_at"]:
                    raise ValueError(f"rollback would merge different lease deadlines for {exact}; drain first")
                rewritten.setdefault(exact, row)  # retain the oldest guard when the same holder has two aliases
                if exact != row["lease_key"]:
                    changes.append((row["lease_key"], exact))
            if apply and changes:
                conn.execute("DELETE FROM leases")
                conn.executemany("INSERT INTO leases(lease_key,holder,acquired_at,expires_at) VALUES(?,?,?,?)",
                                 [(key, row["holder"], row["acquired_at"], row["expires_at"])
                                  for key, row in rewritten.items()])
                conn.execute("INSERT INTO events(ts,kind,data_json) VALUES(strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,?)",
                             ("leases.rollback_spelling", json.dumps({"rewritten": len(changes)})))
                conn.commit()
            else:
                conn.rollback()
            return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        changes = prepare(args.root, apply=args.apply)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"rollback preparation refused: {exc}\n")
    for before, after in changes:
        print(f"{before} -> {after}")
    print(f"{'rewrote' if args.apply else 'would rewrite'} {len(changes)} lease keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
