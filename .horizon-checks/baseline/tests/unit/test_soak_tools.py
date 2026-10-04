"""tools/soak_report.py and tools/canary_check.py: the soak and canary evidence rules."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
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
canary_submit = load("canary_submit")
release_gates = load("measure_release_gates")

SINCE = "2026-09-05T22:00:00Z"


def seed(root: Path) -> Store:
    store = Store(root / "state.sqlite3")
    home = root / "lanes" / "codex-3"
    store.put_lane(Lane("codex-3", "codex", "codex:canary", Credential("codex", str(home), "home"),
                        str(home), LaneOwner.V2, False))
    store.conn.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                       ("2026-09-06T02:00:00Z", "timer.run", '{"timer":"probe","last_error_type":null}'))
    return store


def job(store, job_id, state, *, imported=False, attempt_state=None, reserved_at="2026-09-06T01:00:00Z",
        finished_at="2026-09-06T01:05:00Z", outcome_class=None, accepted=False, rc=None,
        workdir="/tmp/w", isolated_review=False, review_root=None):
    store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind="dispatch", state=state,
                  workdir=workdir, prompt_path="/tmp/p", sandbox="read-only",
                  isolated_review=isolated_review, review_root=review_root,
                  accepted_attempt_id=f"{job_id}/a1" if accepted else None, created_at=reserved_at)
    store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="codex-3", model_requested="astra",
                      state=attempt_state or state, reserved_at=reserved_at, finished_at=finished_at,
                      outcome_class=outcome_class, rc=rc,
                      evidence_json=json.dumps({"imported": True}) if imported else "{}")


def test_soak_report_ignores_imported_history_and_stops_on_a_fresh_loss(tmp_path):
    """C-20.4: imported v1 losses must neither stop the soak nor hide a new one."""
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
    """C-20.4: soak timing comes from the recorded start or an explicit window."""
    (tmp_path / "soak.json").write_text(json.dumps({"since": SINCE}))
    assert soak_report.soak_since(tmp_path, None) == SINCE
    assert soak_report.soak_since(tmp_path, "2026-09-07T00:00:00Z") == "2026-09-07T00:00:00Z"


ISOLATED_ARGV = ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--ignore-user-config",
                 "--ignore-rules", *(arg for config in canary_check.ISOLATION_CONFIG for arg in ("-c", config)), "-"]


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
            job(store, job_id, state, outcome_class=kind, accepted=(kind == "ok"), rc=0 if kind == "ok" else 4,
                workdir=str(canary / "work"), isolated_review=True, review_root=str(canary / "work"))
            adir = root / "jobs" / job_id / "a1"
            adir.mkdir(parents=True)
            (adir / "launch.json").write_text(json.dumps({"argv": argv, "cwd": str(canary / "work")}))
            if kind == "ok":
                (adir / "deliverable.md").write_text("done\n")
                store.add_artifact(f"{job_id}/a1", "deliverable", str(adir / "deliverable.md"),
                                   hashlib.sha256(b"done\n").hexdigest(), 5)
                store.add_notice(job_id, "done")
    (canary / "jobs.json").write_text(json.dumps(cohort))
    store.close()
    return root, canary


def test_canary_check_passes_a_complete_isolated_cohort(tmp_path):
    """C-20.4: a fully evidenced cohort meets the requested success floor."""
    root, canary = canary_world(tmp_path, successes=3)
    result = canary_check.check(root, canary, expect=3, min_success=3)
    assert result["violations"] == [] and result["verdict"] == "PASS"


def test_canary_check_fails_without_the_isolation_flags(tmp_path):
    """C-20.4, C-23.2: read-only alone does not restrain hosted capabilities."""
    root, canary = canary_world(tmp_path, argv=["codex", "exec", "--sandbox", "read-only", "-"], successes=2)
    result = canary_check.check(root, canary, expect=2, min_success=2)
    assert any("--ephemeral" in v for v in result["violations"])
    assert any("features.hooks=false" in v for v in result["violations"])
    assert result["verdict"] == "FAIL"


def test_canary_check_fails_a_batch_that_only_fails_cleanly(tmp_path):
    """C-20.4: classified failures alone exercise no success path."""
    root, canary = canary_world(tmp_path, successes=0, failures=3)
    result = canary_check.check(root, canary, expect=3, min_success=2)
    assert any("0 succeeded, 2 required" in v for v in result["violations"])


def test_canary_check_fails_an_unclassified_failure_and_a_cohort_mismatch(tmp_path):
    """C-20.4: every recorded job must belong to the cohort and classify its failure."""
    root, canary = canary_world(tmp_path, successes=2, unknowns=1)
    result = canary_check.check(root, canary, expect=3, min_success=2)
    assert any("without a classified failure (unknown)" in v for v in result["violations"])
    (canary / "jobs.json").write_text(json.dumps(json.loads((canary / "jobs.json").read_text())[:2]))
    result = canary_check.check(root, canary, expect=2, min_success=2)
    assert any("outside the recorded cohort" in v for v in result["violations"])


def test_canary_check_reports_pending_before_the_cohort_is_terminal(tmp_path, monkeypatch, capsys):
    """C-20.4: a pending cohort cannot pass a shell release gate."""
    root, canary = canary_world(tmp_path, successes=2)
    store = Store(root / "state.sqlite3")
    job(store, "20260907-070099-canary-099", "running", attempt_state="running", finished_at=None,
        workdir=str(canary / "work"), isolated_review=True, review_root=str(canary / "work"))
    store.close()
    (canary / "jobs.json").write_text(json.dumps(json.loads((canary / "jobs.json").read_text())
                                                 + [{"name": "canary-099", "job_id": "20260907-070099-canary-099"}]))
    baseline = json.loads((canary / "baseline.json").read_text())
    baseline["count"] = 3
    (canary / "baseline.json").write_text(json.dumps(baseline))
    result = canary_check.check(root, canary, expect=3, min_success=2)
    assert result["verdict"].startswith("PENDING") and result["violations"] == []
    monkeypatch.setattr(sys, "argv", ["canary_check", "--state-root", str(root), "--canary-dir", str(canary),
                                      "--expect", "3", "--min-success", "2"])
    assert canary_check.main() == 1
    assert "PENDING" in capsys.readouterr().out


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_canary_requires_verified_deliverable_bytes(tmp_path, damage):
    """C-8.2, C-20.4: artifact metadata is insufficient when bytes are lost or changed."""
    root, canary = canary_world(tmp_path, successes=1)
    path = next(root.glob("jobs/*/a1/deliverable.md"))
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("evil\n")  # Same size: hash verification must catch this.
    result = canary_check.check(root, canary, expect=1, min_success=1)
    assert result["verdict"] == "FAIL"
    assert any("deliverable cannot be verified" in v for v in result["violations"])


@pytest.mark.parametrize("damage", ["missing", "count", "invalid_git"])
def test_canary_requires_baseline_and_verified_clone(tmp_path, damage):
    """C-20.4: missing baseline or failed git commands cannot prove an unchanged clone."""
    root, canary = canary_world(tmp_path, successes=1)
    if damage == "missing":
        (canary / "baseline.json").unlink()
    elif damage == "count":
        baseline = json.loads((canary / "baseline.json").read_text())
        baseline["count"] = 100
        (canary / "baseline.json").write_text(json.dumps(baseline))
    else:
        (canary / "work" / ".git").rename(canary / "parked-git")
    assert canary_check.check(root, canary, expect=1, min_success=1)["verdict"] == "FAIL"


@pytest.mark.parametrize("field,value", [("workdir", "/tmp"), ("review_root", "/tmp"),
                                        ("review_root", None), ("isolated_review", 0),
                                        ("sandbox", "workspace-write"), ("worktree", "/tmp")])
def test_canary_requires_isolated_job_bound_to_clone(tmp_path, field, value):
    """C-20.4, C-23.2: a clean unrelated clone cannot certify the recorded job's execution."""
    root, canary = canary_world(tmp_path, successes=1)
    with Store(root / "state.sqlite3") as store:
        store.conn.execute(f"UPDATE jobs SET {field}=?", (value,))
    assert canary_check.check(root, canary, expect=1, min_success=1)["verdict"] == "FAIL"


@pytest.mark.parametrize("cwd", [None, "/tmp", "."])
def test_canary_requires_launch_cwd_bound_to_clone(tmp_path, cwd):
    """C-20.4: only a recorded launch in the verified canary clone proves its read-only run."""
    root, canary = canary_world(tmp_path, successes=1)
    launch = next(root.glob("jobs/*/a1/launch.json"))
    record = json.loads(launch.read_text())
    if cwd is None:
        record.pop("cwd")
    else:
        record["cwd"] = cwd
    launch.write_text(json.dumps(record))
    result = canary_check.check(root, canary, expect=1, min_success=1)
    assert result["verdict"] == "FAIL"
    assert any("launch cwd" in violation for violation in result["violations"])


def test_canary_rejects_argv_chdir_override(tmp_path):
    """C-20.4: provider directory flags cannot override the checked process cwd."""
    root, canary = canary_world(tmp_path, successes=1, argv=ISOLATED_ARGV[:-1] + ["--cd=/tmp", "-"])
    assert canary_check.check(root, canary, expect=1, min_success=1)["verdict"] == "FAIL"


def test_canary_accepts_resolved_directory_alias(tmp_path):
    """C-20.4: equivalent absolute directory aliases still identify the same clone."""
    root, canary = canary_world(tmp_path, successes=1)
    alias = tmp_path / "clone-alias"
    alias.symlink_to(canary / "work", target_is_directory=True)
    launch = next(root.glob("jobs/*/a1/launch.json"))
    record = json.loads(launch.read_text())
    record["cwd"] = str(alias)
    launch.write_text(json.dumps(record))
    assert canary_check.check(root, canary, expect=1, min_success=1)["verdict"] == "PASS"


@pytest.mark.parametrize("state", ["failed", "cancelled", "lost"])
def test_canary_cannot_count_non_successful_job_as_success(tmp_path, state):
    """C-4.1, C-20.4: a dangling acceptance pointer cannot satisfy the success floor."""
    root, canary = canary_world(tmp_path, successes=1)
    with Store(root / "state.sqlite3") as store:
        store.conn.execute("UPDATE jobs SET state=?", (state,))
    result = canary_check.check(root, canary, expect=1, min_success=1)
    assert result["succeeded"] == 0 and result["verdict"] == "FAIL"


@pytest.mark.parametrize("override", [["-c", "features.hooks=true"], ["--config=features.hooks=true"],
                                     ["-cfeatures.hooks=true"]])
def test_canary_config_override_does_not_prove_isolation(tmp_path, override):
    """C-23.2, C-20.4: a later override cannot re-enable a restricted capability."""
    root, canary = canary_world(tmp_path, successes=1,
                                argv=ISOLATED_ARGV[:-1] + override + ["-"])
    result = canary_check.check(root, canary, expect=1, min_success=1)
    assert any("features.hooks=false" in v for v in result["violations"])


@pytest.mark.parametrize("override", ["--sandbox=workspace-write", "--dangerously-bypass-approvals-and-sandbox"])
def test_canary_sandbox_override_is_not_read_only(tmp_path, override):
    """C-20.4, C-23.2: a later sandbox override invalidates the canary isolation claim."""
    root, canary = canary_world(tmp_path, successes=1, argv=ISOLATED_ARGV[:-1] + [override, "-"])
    assert canary_check.check(root, canary, expect=1, min_success=1)["verdict"] == "FAIL"


def test_canary_does_not_silently_deduplicate_cohort(tmp_path):
    """C-20.4: repeated receipt IDs cannot be counted as distinct submitted jobs."""
    root, canary = canary_world(tmp_path, successes=1)
    cohort = json.loads((canary / "jobs.json").read_text())
    (canary / "jobs.json").write_text(json.dumps(cohort * 2))
    assert canary_check.check(root, canary, expect=1, min_success=1)["verdict"] == "FAIL"


def test_soak_carries_prior_losses_and_unfinished_quarantines_forward(tmp_path):
    """C-20.4: midnight cannot clear an unresolved loss or undated quarantine."""
    store = seed(tmp_path)
    job(store, "yesterday", "lost", reserved_at=SINCE, finished_at="2026-09-05T23:00:00Z")
    job(store, "quarantine", "running", attempt_state="quarantined", finished_at=None)
    text, clean = soak_report.report(tmp_path, "2026-09-06", "%-canary-%", SINCE)
    assert not clean
    section = text.split("## Unresolved quarantined or lost since soak start")[1].split("##")[0]
    assert "yesterday/a1" in section and "quarantine/a1" in section


def test_soak_import_marker_is_json_not_whitespace(tmp_path):
    """C-20.4: imported history remains excluded with compact JSON serialization."""
    store = seed(tmp_path)
    job(store, "imported", "lost")
    store.conn.execute("UPDATE attempts SET evidence_json=?", ('{"imported":true}',))
    text, clean = soak_report.report(tmp_path, "2026-09-06", "%-canary-%", SINCE)
    assert clean and "- lost: 1" in text.split("## History imported")[1]


@pytest.mark.parametrize("missing", ["start", "timers", "successful_timer"])
def test_soak_empty_or_unstarted_evidence_is_not_clean(tmp_path, missing):
    """C-18.1, C-20.4: absence of observed defects alone is not evidence of a running soak."""
    store = seed(tmp_path)
    if missing == "timers":
        store.conn.execute("DELETE FROM events")
    elif missing == "successful_timer":
        store.conn.execute("UPDATE events SET data_json=?", ('{"timer":"probe","last_error_type":"TimeoutError"}',))
    text, clean = soak_report.report(tmp_path, "2026-09-06", "%-canary-%", None if missing == "start" else SINCE)
    assert not clean and "STOP" in text


def test_soak_identity_mismatch_blocks_observation(tmp_path):
    """C-10.6, C-20.4: an unresolved lane identity mismatch stops the soak."""
    store = seed(tmp_path)
    store.conn.execute("UPDATE lanes SET identity_status='mismatch'")
    text, clean = soak_report.report(tmp_path, "2026-09-06", "%-canary-%", SINCE)
    assert not clean and "`codex-3`: mismatch" in text


def test_numeric_release_gates_reject_failed_or_slow_recovery():
    """C-20.4: quick loss is not recovery, and threshold violations fail the command."""
    assert release_gates.violations(99, 249, 29, 1, "succeeded") == []
    assert len(release_gates.violations(100, 250, 30, 2, "lost")) == 4
    assert release_gates.violations(float("nan"), 1, 1, 1, "succeeded")
    assert release_gates.p95(list(range(1, 11))) == 10000


def test_numeric_measurements_refuse_failed_commands():
    """C-20.4: timing a fast CLI error cannot satisfy the status or submission gate."""
    from types import SimpleNamespace
    e2e = SimpleNamespace(cli=lambda *args: SimpleNamespace(rc=69, stderr="daemon unavailable"))
    with pytest.raises(RuntimeError, match="exit 69"):
        release_gates.checked_cli(e2e, "status")


def test_measurement_fleet_has_distinct_verified_claude_identities(tmp_path):
    """C-10.6, C-20.4: synthetic measurement lanes agree with their fixture credentials."""
    from types import SimpleNamespace
    (tmp_path / "lanes.json").write_text("[]")
    e2e = SimpleNamespace(root=tmp_path, env={})
    release_gates.seed_lanes(e2e, total=14)
    lanes = json.loads((tmp_path / "lanes.json").read_text())
    assert len({lane["account_key"] for lane in lanes}) == 14
    for lane in lanes:
        if lane["provider"] == "claude":
            number = int(lane["lane_id"].split("-")[1])
            assert lane["identity"] == release_gates.derived_identity(number)[0]
            assert e2e.env[lane["credential_ref"]] == f"fake-subscription-token-{number}"


def test_canary_submit_preserves_original_baseline_and_rejects_moved_head(tmp_path, monkeypatch):
    """C-20.4: resubmission cannot reset the soak clock or bless a changed clone."""
    _, canary = canary_world(tmp_path, successes=1)
    runs = tmp_path / "v1" / "a"
    runs.mkdir(parents=True)
    (runs / "prompt.md").write_text("Review this repository.")
    (canary / "baseline.json").unlink()
    (canary / "jobs.json").unlink()
    monkeypatch.setattr(sys, "argv", ["canary_submit", "--canary-dir", str(canary), "--v1-runs", str(runs.parent),
                                      "--count", "1", "--dry-run"])
    assert canary_submit.main() == 0
    baseline = (canary / "baseline.json").read_bytes()
    assert canary_submit.main() == 0
    assert (canary / "baseline.json").read_bytes() == baseline
    subprocess.run(["git", "-C", str(canary / "work"), "-c", "user.name=t", "-c", "user.email=t@example.test",
                    "commit", "-q", "--allow-empty", "-m", "moved"], check=True)
    assert canary_submit.main() == 2
    assert (canary / "baseline.json").read_bytes() == baseline


def test_canary_submit_failed_command_is_not_acknowledged(tmp_path, monkeypatch):
    """C-6.2, C-20.4: a rejected CLI submission neither records an ID nor returns success."""
    _, canary = canary_world(tmp_path, successes=1)
    (canary / "jobs.json").unlink()
    (canary / "baseline.json").unlink()
    runs = tmp_path / "v1" / "a"
    runs.mkdir(parents=True)
    (runs / "prompt.md").write_text("Review this repository.")
    real_run = subprocess.run
    def run(cmd, **kwargs):
        if cmd[0] == "fake-sf2":
            return subprocess.CompletedProcess(cmd, 2, "not a job id\n", "rejected")
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["canary_submit", "--canary-dir", str(canary), "--v1-runs", str(runs.parent),
                                      "--count", "1", "--sf2", "fake-sf2"])
    assert canary_submit.main() == 1
    assert json.loads((canary / "jobs.json").read_text())[0]["job_id"] is None


def test_canary_retry_keeps_frozen_payload_after_ledger_changes(tmp_path, monkeypatch):
    """C-6.2, C-20.4: interruption after acknowledgement cannot change the retry payload."""
    _, canary = canary_world(tmp_path, successes=2)
    (canary / "jobs.json").unlink()
    (canary / "baseline.json").unlink()
    runs = tmp_path / "v1"
    for name in ("b", "c"):
        (runs / name).mkdir(parents=True)
        (runs / name / "prompt.md").write_text(f"Original task {name}.")
    submissions = []
    real_run, real_save = subprocess.run, canary_submit.save_json
    def run(cmd, **kwargs):
        if cmd[0] != "fake-sf2":
            return real_run(cmd, **kwargs)
        baseline = json.loads((canary / "baseline.json").read_text())
        assert len(canary_submit.frozen_prompts(baseline, canary, 2)) == 2
        submissions.append((cmd[cmd.index("--request-id") + 1], Path(cmd[cmd.index("-p") + 1]).read_bytes()))
        return subprocess.CompletedProcess(cmd, 0, f"job-{cmd[cmd.index('-n') + 1]}\n", "")
    def interrupted_save(path, value):
        if path.name == "jobs.json":
            raise KeyboardInterrupt("crash after daemon acknowledgement")
        return real_save(path, value)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(canary_submit, "save_json", interrupted_save)
    monkeypatch.setattr(sys, "argv", ["canary_submit", "--canary-dir", str(canary), "--v1-runs", str(runs),
                                      "--count", "2", "--sf2", "fake-sf2"])
    with pytest.raises(KeyboardInterrupt):
        canary_submit.main()
    original_baseline = (canary / "baseline.json").read_bytes()
    (runs / "a").mkdir()
    (runs / "a" / "prompt.md").write_text("New task sorts ahead of the sampled cohort.")
    for name in ("b", "c"):
        (runs / name / "prompt.md").unlink()
    monkeypatch.setattr(canary_submit, "save_json", real_save)
    assert canary_submit.main() == 0
    assert submissions[0] == submissions[1]
    assert b"Original task c." in submissions[2][1]
    assert (canary / "baseline.json").read_bytes() == original_baseline
    assert len(json.loads((canary / "jobs.json").read_text())) == 2


def test_canary_retry_refuses_edited_frozen_prompt(tmp_path, monkeypatch):
    """C-6.2, C-20.4: edited frozen bytes are rejected before any new submission."""
    _, canary = canary_world(tmp_path, successes=1)
    (canary / "jobs.json").unlink()
    (canary / "baseline.json").unlink()
    runs = tmp_path / "v1" / "a"
    runs.mkdir(parents=True)
    (runs / "prompt.md").write_text("Original task.")
    monkeypatch.setattr(sys, "argv", ["canary_submit", "--canary-dir", str(canary), "--v1-runs", str(runs.parent),
                                      "--count", "1", "--dry-run"])
    assert canary_submit.main() == 0
    (canary / "prompts" / "canary-001.md").write_text("Edited task.")
    assert canary_submit.main() == 2


@pytest.mark.parametrize("failure", [None, "credential_missing", "v1_unreadable", "wrong_owner"])
def test_canary_runbook_requires_verified_ownership(tmp_path, failure):
    """C-10.4, C-20.4: transfer verification must not treat absent evidence as success."""
    repository = tmp_path / "repo"
    (repository / "tools").mkdir(parents=True)
    script = repository / "tools" / "canary_runbook.sh"
    script.write_text((REPO / "tools" / "canary_runbook.sh").read_text())
    (repository / ".venv" / "bin").mkdir(parents=True)
    (repository / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (repository / "bin").mkdir()
    home = tmp_path / "home"
    state = home / ".subfleet"
    lane_home = state / "lanes" / "codex-3"
    lane_home.mkdir(parents=True)
    if failure != "credential_missing":
        (lane_home / "auth.json").write_text("{}")
    roster = home / "chief-of-staff" / "subfleet" / "codex-accounts.json"
    roster.parent.mkdir(parents=True)
    roster.write_text('{"transferred_to_v2":["codex-3"]}')
    v1 = repository / "bin" / "subfleet"
    v1.write_text("#!/bin/sh\n" + ("exit 69\n" if failure == "v1_unreadable" else "echo codex-1\n"))
    v1.chmod(0o755)
    v2 = repository / "bin" / "sf2"
    lanes = [{"lane_id": "codex-3", "owner": "v1" if failure == "wrong_owner" else "v2", "home": str(lane_home)}]
    v2.write_text("#!/bin/sh\ncat <<'JSON'\n" + json.dumps(lanes) + "\nJSON\n")
    v2.chmod(0o755)
    result = subprocess.run(["sh", str(script), "verify"], capture_output=True, text=True,
                            env={**os.environ, "HOME": str(home), "SUBFLEET_HOME": str(state),
                                 "LANE": "codex-3", "PATH": f"{repository / 'bin'}:/usr/bin:/bin"})
    assert (result.returncode == 0) is (failure is None), result.stdout + result.stderr
