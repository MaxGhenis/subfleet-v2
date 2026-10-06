"""Review probe for PR #131: migrate a base-built schema-6 store shaped like the
live one (211 quarantined attempts, old-format reasons, live lease mix), then
drive the paced passes. Not part of the PR. Needs REVIEW_V6_STORE.
"""
from datetime import datetime, timedelta, timezone
import json
import math
import os
import shutil
from pathlib import Path

import pytest

from subfleet import daemon as dm, procs
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon, QUARANTINE_RECHECK_BATCH
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter

V6 = os.environ.get("REVIEW_V6_STORE")
ORIGINAL_CENSUS = procs.containment


@pytest.mark.skipif(not V6, reason="REVIEW_V6_STORE not set")
def test_live_shaped_v6_store_migrates_and_every_gone_attempt_is_released_fairly(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    shutil.copy(V6, root / "state.sqlite3")
    for n in range(211):
        (root / "jobs" / f"20260922-{n:06d}-fixture" / "a1").mkdir(parents=True)
    monkeypatch.setattr(procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(procs, "proc_start", lambda pid: "unit-test-start")
    register("codex", FakeAdapter)
    now = [datetime(2026, 10, 5, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(dm, "quarantine_time", lambda seconds=0: (now[0] + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds").replace("+00:00", "Z"))

    daemon = Daemon(harness.root)
    try:
        store = daemon.store
        assert store.one("SELECT MAX(version) v FROM schema_version")["v"] == 7
        assert [r["version"] for r in store.query("SELECT version FROM schema_version ORDER BY version")] == [6, 7]
        assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"
        indexes = {r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"attempts_quarantine_due", "attempts_quarantine_notice"} <= indexes
        rows = store.query("SELECT * FROM attempts WHERE state='quarantined'")
        assert len(rows) == 211
        assert {r["quarantine_recheck_at"] for r in rows} == {""}
        assert {r["quarantine_notice_pending"] for r in rows} == {0}
        assert store.one("SELECT COUNT(*) n FROM leases")["n"] == 493
        plan = store.query("EXPLAIN QUERY PLAN SELECT * FROM attempts WHERE state='quarantined' AND "
                           "quarantine_recheck_at<=? ORDER BY quarantine_recheck_at,attempt_id LIMIT ?",
                           (dm.quarantine_time(), QUARANTINE_RECHECK_BATCH))
        assert any("attempts_quarantine_due" in r["detail"] for r in plan)

        # Census script: every recorded writer is gone except 7 still-live ones;
        # the old "marker enumeration unavailable" rows stay unverifiable for ever.
        unverifiable = {f"20260922-{n:06d}-fixture/a1" for n in range(211) if n % 4 == 3}
        live = {f"20260922-{n:06d}-fixture/a1": n for n in range(211) if n % 4 != 3 and n % 25 == 1}
        assert len(unverifiable) == 52 and len(live) == 7
        current = {}
        def snapshot():
            n = current.get("live")
            return procs.ProcessTable({23050 + n: (1, 23050 + n, "S", f"Sat Oct  3 23:51:{(23050 + n) % 60:02d} 2026")}
                                      if n is not None else {}, boot_id="6F1C0F2E-1111-4222-8333-944455556666")
        def read(argv, **kwargs):
            if current.get("unverifiable"):
                raise procs.InspectionError("marker enumeration unavailable")
            if current.get("live") is not None:
                return f"{23050 + current['live']} writer SUBFLEET_ATTEMPT={current['attempt']} SUBFLEET_ROOT={root}\n"
            return ""
        censused = []
        def census(pgid, guardian, child, attempt_id, root=None, **kwargs):
            censused.append(attempt_id)
            current.clear()
            current.update(unverifiable=attempt_id in unverifiable, live=live.get(attempt_id), attempt=attempt_id)
            return ORIGINAL_CENSUS(pgid, guardian, child, attempt_id, root=root, **kwargs)
        monkeypatch.setattr(procs, "snapshot", snapshot)
        monkeypatch.setattr(procs, "_read", read)
        monkeypatch.setattr(procs, "containment", census)

        passes = 0
        while store.one("SELECT 1 FROM attempts WHERE state='quarantined' AND quarantine_recheck_at<=?",
                        (dm.quarantine_time(),)):
            daemon._recheck_quarantines()
            passes += 1
            assert passes <= 30
        assert passes == math.ceil(211 / QUARANTINE_RECHECK_BATCH) == 27
        assert len(censused) == 211 and len(set(censused)) == 211          # each once, oldest first
        # The 152 verified-empty quarantines release on the same boot.
        held = {r["attempt_id"] for r in store.query("SELECT attempt_id FROM attempts WHERE state='quarantined'")}
        assert held == unverifiable | set(live)
        released = [r for r in store.query("SELECT * FROM attempts WHERE state!='quarantined'")]
        assert len(released) == 211 - 52 - 7
        holders = {r["holder"] for r in store.query("SELECT holder FROM leases")}
        for r in released:
            assert r["attempt_id"] not in holders and r["job_id"] not in holders
            assert r["quarantine_notice_pending"] == 0                      # turns without manifests skipped
        assert len(store.query("SELECT 1 FROM events WHERE kind='quarantine.self_resolved' AND data_json!='{}'")) == 152
        # Old-format reasons keep their recorded writers and gain the census.
        for aid in live:
            reason = json.loads(store.get_attempt(aid)["quarantine_reason"])
            assert reason["live_pids"] and reason["identities"]

        # Next pace: the held ones only, the unverifiable ones never blocking the others.
        censused.clear()
        now[0] += timedelta(seconds=daemon.policy["quarantine_recheck_s"])
        passes = 0
        while store.one("SELECT 1 FROM attempts WHERE state='quarantined' AND quarantine_recheck_at<=?",
                        (dm.quarantine_time(),)):
            daemon._recheck_quarantines()
            passes += 1
        assert passes == math.ceil(59 / QUARANTINE_RECHECK_BATCH) and sorted(censused) == sorted(held)
        print(f"rotation: 27 passes for 211, then {passes} for the 59 still held")
    finally:
        daemon.close()
