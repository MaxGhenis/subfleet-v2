"""Build a schema-6 store shaped like the live one, with the BASE code (08d6a09a).

Run from an export of the base so `import subfleet` is the base tree:
  git archive 08d6a09a | tar -x -C <base>
  cd <base> && PYTHONPATH=. <venv>/bin/python <worktree>/tests/fake/review_pr131_build_v6.py <out.sqlite3>
then: REVIEW_V6_STORE=<out.sqlite3> pytest tests/fake/test_review_pr131_migration.py
Never touches ~/.subfleet. 211 quarantined attempts with old-format reasons.
"""
import json
import sys
from pathlib import Path

import subfleet.store as base_store
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store

assert base_store.SCHEMA_VERSION == 6, base_store.SCHEMA_VERSION
out = Path(sys.argv[1])
assert "/.subfleet/" not in str(out.resolve()).replace("/.subfleet/worktrees/", "/"), out
BOOT = "6F1C0F2E-1111-4222-8333-944455556666"


def census(pids, *, unverifiable=False, errors=(), shapes=True):
    value = {"group_pids": [], "descendant_pids": [], "marker_pids": sorted(pids), "live_pids": sorted(pids),
             "unverifiable": unverifiable,
             "identities": {str(p): {"pid": p, "boot_id": BOOT, "proc_start": f"Sat Oct  3 23:51:{p % 60:02d} 2026"}
                            for p in pids},
             "errors": list(errors)}
    if shapes:
        value["shapes"] = {str(p): {"ppid": 1, "pgid": p, "stat": "S"} for p in pids}
    return value


with Store(out) as store:
    store.put_lane(Lane("codex-1", "codex", "codex:fake", Credential("codex", "/fixture/home", "home", 1),
                        "/fixture/home", LaneOwner("v2"), False, True, None, None))
    with store.transaction("fixture.live_shape") as tx:
        for n in range(211):
            job, aid = f"20260922-{n:06d}-fixture", f"20260922-{n:06d}-fixture/a1"
            kind = "turn" if n % 7 == 0 else "dispatch"
            tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                       "out_path,created_at,finished_at,rc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job, job, "digest", kind, "lost", f"/fixture/wt-{n}", "/fixture/prompt.md",
                        "read-only", f"/fixture/out-{n}.md", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z", 125))
            fmt = n % 4
            if fmt == 0:      # pre-ef1240a5 (2026-09-05): `_quarantine` with reason, no shapes
                reason = {"reason": "writers remain after exit receipt", **census([23050 + n], shapes=False)}
            elif fmt == 1:    # base `quarantine.still_live`: the census alone, no reason
                reason = census([23050 + n])
            elif fmt == 2:    # base `_quarantine` today
                reason = {"reason": "writers remain after exit receipt", **census([23050 + n])}
            else:             # unverifiable: marker enumeration unavailable
                reason = {"reason": "writers remain after exit receipt",
                          **census([], unverifiable=True, errors=["marker enumeration unavailable"])}
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,quarantine_reason,"
                       "reserved_at,finished_at,guardian_pid,pgid,boot_id,proc_start,child_pid,evidence_json) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, job, 1, "codex-1", "astra", "quarantined", json.dumps(reason, sort_keys=fmt != 1),
                        f"2026-09-{22 + n % 8:02d}T00:00:00Z", f"2026-09-{22 + n % 8:02d}T00:10:00Z",
                        30000 + n, 30000 + n, BOOT, "Mon Sep 22 00:00:00 2026", None,
                        json.dumps({"owned_identities": {}})))
            leases = []
            if n < 167:
                leases.append((f"worktree:/fixture/wt-{n}", job))
            if n < 209:
                leases.append((f"out:/fixture/out-{n}.md", job))
            if n % 5 == 0:
                leases.append((f"native:codex-1:sess-{n}", job))
                leases.append((f"native-session:codex-1:sess-{n}", aid))
            if kind == "turn":
                leases.append((f"conversation:cv-{n}", job))
            for key, holder in leases:
                tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                           (key, holder, "2026-09-22T00:00:00Z"))
    counts = {k: store.one(q)["n"] for k, q in {
        "attempts_quarantined": "SELECT COUNT(*) n FROM attempts WHERE state='quarantined'",
        "worktree": "SELECT COUNT(*) n FROM leases WHERE lease_key LIKE 'worktree:%'",
        "out": "SELECT COUNT(*) n FROM leases WHERE lease_key LIKE 'out:%'",
        "leases": "SELECT COUNT(*) n FROM leases",
        "schema": "SELECT MAX(version) n FROM schema_version"}.items()}
print(json.dumps(counts))
