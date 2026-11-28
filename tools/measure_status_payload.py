"""Measure D-ST2 using only a disposable synthetic store.

Run with TMPDIR under `getconf DARWIN_USER_TEMP_DIR` on macOS. No daemon
server, provider, or process inspection is started. `--legacy` reproduces the
old full-history capacity read for comparison after the fix.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
import tempfile
import time
from unittest.mock import patch

from subfleet import protocol
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon, after, utcnow


def seed_store(store, *, finished=8000, evidence_bytes=12 * 1024, live=32):
    """A growing terminal ledger beside live work, turns, and quarantine."""
    lane = store.get_lane("codex-1")
    home = lane.home if lane else str(store.path.parent / "home")
    if lane is None:
        store.put_lane(Lane("codex-1", "codex", "codex:synthetic", Credential("codex", home, "home"),
                            home, LaneOwner.V2, False))
    stamp, evidence = utcnow(), json.dumps({"synthetic": "x" * evidence_bytes})
    job_sql = ("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,"
               "sandbox,created_at,finished_at,wait_reason,next_check_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
    attempt_sql = ("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,"
                   "reserved_at,finished_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)")

    def job(identity, state, kind="dispatch", reason=None):
        return (identity, identity, "synthetic", kind, state, home, home + "/prompt.md", "read-only",
                stamp, stamp if state in ("succeeded", "failed", "cancelled", "lost") else None,
                reason, after(60) if reason else None)

    with store.transaction("fixture.synthetic-status") as tx:
        tx.executemany(job_sql, (job(f"done-{i:06d}", ("succeeded", "failed", "cancelled", "lost")[i % 4])
                                  for i in range(finished)))
        tx.executemany(attempt_sql,
                       ((f"done-{i:06d}/a1", f"done-{i:06d}", 1, "codex-1", "gpt-6-astra",
                         ("succeeded", "failed", "cancelled", "lost")[i % 4], stamp, stamp, evidence)
                        for i in range(finished)))
        for i in range(live):
            identity = f"live-{i:03d}"
            tx.execute(job_sql, job(identity, "running", "turn" if i == 0 else "dispatch"))
            tx.execute(attempt_sql, (identity + "/a1", identity, 1, "codex-1", "gpt-6-astra",
                                    ("reserved", "starting", "running", "finalizing")[i % 4], stamp, None,
                                    evidence))
        # A cancelled turn can still have an uncontained quarantined attempt.
        tx.execute(job_sql, job("quarantine", "cancelled", "turn"))
        tx.execute(attempt_sql, ("quarantine/a1", "quarantine", 1, "codex-1", "gpt-6-astra",
                                "quarantined", stamp, None, evidence))
        tx.execute(job_sql, job("queued", "queued"))
        tx.execute(job_sql, job("waiting", "waiting", reason="no-slot"))


def measure(service, op, args, repeats):
    service.dispatch(op, args)  # warm the read connections
    dispatch_ms, total_ms, sizes = [], [], []
    for _ in range(repeats):
        start = time.perf_counter()
        result = service.dispatch(op, args)
        dispatch_ms.append((time.perf_counter() - start) * 1000)
        wire = protocol.encode(protocol.Response("measure", True, result=result))
        total_ms.append((time.perf_counter() - start) * 1000)
        sizes.append(len(wire))
    return f"| {op} | {max(sizes):,} | {median(dispatch_ms):.2f} | {median(total_ms):.2f} |"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finished", type=int, default=8000)
    parser.add_argument("--evidence-bytes", type=int, default=12 * 1024)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="status-payload-") as temporary, \
            patch("subfleet.daemon.procs.boot_id", return_value="synthetic"), \
            patch("subfleet.daemon.procs.proc_start", return_value="synthetic"), \
            patch.object(Daemon, "_desktop_identity", return_value=None), \
            patch.object(Daemon, "_desktop_in_use", return_value=False), \
            patch.object(Daemon, "_priority_callers_status", return_value=[]):
        service = Daemon(Path(temporary))
        try:
            seed_store(service.store, finished=args.finished, evidence_bytes=args.evidence_bytes)
            if args.legacy:
                original = service._capacity_rows

                def legacy(*, route=False):
                    with service.store.snapshot():
                        rows = original(route=route)
                        if not route:
                            rows["view"]["attempts"] = service.store.list_attempts()
                            rows["view"]["jobs"] = service.store.query("SELECT * FROM jobs ORDER BY created_at,rowid")
                        return rows
                service._capacity_rows = legacy
            print(f"Synthetic store: {args.finished:,} finished attempts, 32 live, 1 quarantined; "
                  f"{args.evidence_bytes:,} evidence bytes/attempt; {args.repeats} measured calls/op.")
            print("| Operation | Response bytes (including wire envelope) | Read/build median ms | With JSON median ms |")
            print("| --- | ---: | ---: | ---: |")
            print(measure(service, "daemon.status", {}, args.repeats))
            print(measure(service, "lanes", {"action": "hold", "lane_id": "codex-1", "until": after(3600)},
                          args.repeats))
        finally:
            service.close()


if __name__ == "__main__":
    main()
