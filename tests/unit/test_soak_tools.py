"""tools/soak_report.py and tools/canary_check.py: the soak and canary evidence rules."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store

REPO = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


soak_report = load("soak_report")
canary_check = load("canary_check")

SINCE = "2026-09-05T22:00:00Z"


def seed(root: Path) -> Store:
    store = Store(root / "state.sqlite3")
    home = root / "lanes" / "codex-3"
    store.put_lane(Lane("codex-3", "codex", "codex:canary", Credential("codex", str(home), "home"),
                        str(home), LaneOwner.V2, False))
    return store


def job(store, job_id, state, *, imported=False, attempt_state=None, reserved_at="2026-09-06T01:00:00Z",
        finished_at="2026-09-06T01:05:00Z", outcome_class=None, accepted=False, rc=None):
    store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind="dispatch", state=state,
                  workdir="/tmp/w", prompt_path="/tmp/p", sandbox="read-only",
                  accepted_attempt_id=f"{job_id}/a1" if accepted else None, created_at=reserved_at)
    store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="codex-3", model_requested="astra",
                      state=attempt_state or state, reserved_at=reserved_at, finished_at=finished_at,
                      outcome_class=outcome_class, rc=rc,
                      evidence_json=json.dumps({"imported": True}) if imported else "{}")


def test_soak_report_ignores_imported_history_and_stops_on_a_fresh_loss(tmp_path):
    """Peer round 2: imported v1 losses must neither stop the soak nor hide a new one."""
    store = seed(tmp_path)
    job(store, "20260901-100000-old", "lost", imported=True, reserved_at="2026-09-01T10:00:00Z",
        finished_at="2026-09-06T00:30:00Z")
    # Importer events are stamped at import time, inside the reported day here by construction.
    for kind in ("job.imported", "import.cursor", "attempt.accepted"):
        store.conn.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                           ("2026-09-06T02:00:00Z", kind, "20260901-100000-old", "{}"))
    store.conn.commit()
    text, clean = soak_report.report(tmp_path, "2026-09-06", "%-canary-%", SINCE)
    assert clean and "CLEAN" in text
    events_section = text.split("## Events today")[1].split("##")[0]
    assert "job.imported" not in events_section and "import.cursor" not in events_section
    assert "| attempt.accepted | 1 |" in events_section      # positive control: same day, not an import kind
    assert "History imported from v1" in text and "- lost: 1" in text.split("History imported")[1]
    job(store, "20260906-010000-canary-001", "lost", finished_at="2026-09-06T01:05:00Z")
    text, clean = soak_report.report(tmp_path, "2026-09-06", "%-canary-%", SINCE)
    assert not clean and "STOP" in text and "20260906-010000-canary-001/a1" in text
    # An attempt before the soak window is history too, imported or not.
    job(store, "20260905-210000-early", "lost", reserved_at="2026-09-05T21:00:00Z", finished_at="2026-09-06T01:06:00Z")
    text, _ = soak_report.report(tmp_path, "2026-09-06", "%-canary-%", SINCE)
    assert "20260905-210000-early" not in text.split("## Quarantined or lost today")[1].split("##")[0]


def test_soak_since_prefers_the_state_root_file(tmp_path):
    (tmp_path / "soak.json").write_text(json.dumps({"since": SINCE}))
    assert soak_report.soak_since(tmp_path, None) == SINCE
    assert soak_report.soak_since(tmp_path, "2026-09-07T00:00:00Z") == "2026-09-07T00:00:00Z"


ISOLATED_ARGV = ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--ignore-user-config",
                 "--ignore-rules", "-c", "features.hooks=false", "-c", "features.plugins=false", "-"]


def canary_world(tmp_path: Path, *, argv=ISOLATED_ARGV, successes=2, failures=0, unknowns=0):
    root = tmp_path / "state"
    root.mkdir()
    store = seed(root)
    canary = tmp_path / "canary"
    (canary / "work").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(canary / "work")], check=True)
    (canary / "work" / "README").write_text("canary\n")
    subprocess.run(["git", "-C", str(canary / "work"), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(canary / "work"), "-c", "user.name=t", "-c", "user.email=t@example.test",
                    "commit", "-q", "-m", "base"], check=True)
    head = subprocess.run(["git", "-C", str(canary / "work"), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (canary / "baseline.json").write_text(json.dumps({"head": head, "count": successes + failures + unknowns, "since": SINCE}))
    cohort = []
    index = 0
    for kind, n in (("ok", successes), ("limited", failures), ("unknown", unknowns)):
        for _ in range(n):
            index += 1
            job_id = f"20260907-0700{index:02d}-canary-{index:03d}"
            cohort.append({"name": f"canary-{index:03d}", "job_id": job_id})
            state = "succeeded" if kind == "ok" else "failed"
            job(store, job_id, state, outcome_class=kind, accepted=(kind == "ok"), rc=0 if kind == "ok" else 4)
            adir = root / "jobs" / job_id / "a1"
            adir.mkdir(parents=True)
            (adir / "launch.json").write_text(json.dumps({"argv": argv}))
            if kind == "ok":
                (adir / "deliverable.md").write_text("done\n")
                store.add_artifact(f"{job_id}/a1", "deliverable", str(adir / "deliverable.md"), "sha", 5)
                store.add_notice(job_id, "done")
    (canary / "jobs.json").write_text(json.dumps(cohort))
    store.close()
    return root, canary


def test_canary_check_passes_a_complete_isolated_cohort(tmp_path):
    root, canary = canary_world(tmp_path, successes=3)
    result = canary_check.check(root, canary, expect=3, min_success=3)
    assert result["violations"] == [] and result["verdict"] == "PASS"


def test_canary_check_fails_without_the_isolation_flags(tmp_path):
    """Peer round 2: `--sandbox read-only` alone does not restrain MCP servers, plugins, or hooks."""
    root, canary = canary_world(tmp_path, argv=["codex", "exec", "--sandbox", "read-only", "-"], successes=2)
    result = canary_check.check(root, canary, expect=2, min_success=2)
    assert any("--ephemeral" in v for v in result["violations"])
    assert any("features.hooks=false" in v for v in result["violations"])
    assert result["verdict"] == "FAIL"


def test_canary_check_fails_a_batch_that_only_fails_cleanly(tmp_path):
    """Peer round 2: classified failures alone exercise no success path."""
    root, canary = canary_world(tmp_path, successes=0, failures=3)
    result = canary_check.check(root, canary, expect=3, min_success=2)
    assert any("0 succeeded, 2 required" in v for v in result["violations"])


def test_canary_check_fails_an_unclassified_failure_and_a_cohort_mismatch(tmp_path):
    root, canary = canary_world(tmp_path, successes=2, unknowns=1)
    result = canary_check.check(root, canary, expect=3, min_success=2)
    assert any("without a classified failure (unknown)" in v for v in result["violations"])
    (canary / "jobs.json").write_text(json.dumps(json.loads((canary / "jobs.json").read_text())[:2]))
    result = canary_check.check(root, canary, expect=2, min_success=2)
    assert any("outside the recorded cohort" in v for v in result["violations"])


def test_canary_check_reports_pending_before_the_cohort_is_terminal(tmp_path):
    root, canary = canary_world(tmp_path, successes=2)
    store = Store(root / "state.sqlite3")
    job(store, "20260907-070099-canary-099", "running", attempt_state="running", finished_at=None)
    store.close()
    (canary / "jobs.json").write_text(json.dumps(json.loads((canary / "jobs.json").read_text())
                                                 + [{"name": "canary-099", "job_id": "20260907-070099-canary-099"}]))
    result = canary_check.check(root, canary, expect=3, min_success=2)
    assert result["verdict"].startswith("PENDING") and result["violations"] == []
