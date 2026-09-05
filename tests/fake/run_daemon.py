"""Standalone fake daemon; all fault injection stays in the test harness."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import threading
import time


def audit_publication(path: Path) -> None:
    """Record actual fsync/rename calls without changing their behavior."""
    sync = os.fsync
    rename = os.rename
    replace = os.replace
    lock = threading.Lock()

    def record(data: dict) -> None:
        with lock, path.open("a") as stream:
            stream.write(json.dumps(data) + "\n")

    def fsync(fd: int) -> None:
        info = os.fstat(fd)
        sync(fd)
        record({"op": "fsync", "ino": info.st_ino,
                "directory": stat.S_ISDIR(info.st_mode)})

    def publish(original, source, target, *args, **kwargs):
        info = os.stat(source)
        directory = os.stat(Path(target).parent)
        result = original(source, target, *args, **kwargs)
        record({"op": "rename", "ino": info.st_ino,
                "dir_ino": directory.st_ino, "target": str(target)})
        return result

    os.fsync = fsync
    os.rename = lambda src, dst, *a, **kw: publish(rename, src, dst, *a, **kw)
    os.replace = lambda src, dst, *a, **kw: publish(replace, src, dst, *a, **kw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--crash-at")
    parser.add_argument("--hold-at")
    parser.add_argument("--missing-start", action="store_true")
    parser.add_argument("--start-delay", type=float, default=0)
    parser.add_argument("--publication-audit", action="store_true")
    parser.add_argument("--gate-peer", action="store_true")
    args = parser.parse_args()
    root = args.state_root
    if args.publication_audit:
        audit_publication(root / "publication.jsonl")

    from subfleet.adapters.registry import register
    from subfleet.daemon import Daemon, DaemonUnavailable
    from tests.fake_adapter import FakeAdapter

    if args.gate_peer:
        from tests.fake.gate_peer import FakeGateAdapter
        register("codex", FakeGateAdapter)
    else:
        register("codex", FakeAdapter)

    def hook(boundary: str, job_id: str, attempt_id: str) -> None:
        if boundary not in {args.crash_at, args.hold_at}:
            return
        (root / f"hook-{boundary}.json").write_text(json.dumps(
            {"job_id": job_id, "attempt_id": attempt_id}))
        if boundary == args.hold_at:
            while not (root / "release-hook").exists():
                time.sleep(.01)
        if boundary == args.crash_at:
            if args.missing_start:
                with sqlite3.connect(f"file:{root / 'state.sqlite3'}?mode=ro", uri=True) as db:
                    guardian = db.execute(
                        "SELECT guardian_pid FROM attempts WHERE attempt_id=?", (attempt_id,)
                    ).fetchone()[0]
                os.kill(guardian, signal.SIGKILL)
            os.kill(os.getpid(), signal.SIGKILL)

    try:
        daemon = Daemon(root, tick_s=.02, start_grace_s=.65, term_grace_s=.08,
                        guardian_start_delay_s=args.start_delay, crash_hook=hook)
    except DaemonUnavailable:
        return 69
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: daemon.stopping.set())
    try:
        daemon.serve_forever()
    finally:
        daemon.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
