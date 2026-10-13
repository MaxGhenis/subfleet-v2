"""C-5.3, C-5.5: the three recycled-PID incidents of 2026-10-11."""

from dataclasses import asdict
import json
import subprocess

import pytest

from subfleet import procs, protocol
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.unit.test_procs import census
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import script_table


# Save the real census before state_daemon replaces the process seam.
CONTAINMENT = procs.containment
SNAPSHOT = procs.snapshot
CASES = [
    ("qyp16-r1c", 4000, 4203, "Sun Oct 11 01:04:12 2026",
     "Sun Oct 11 07:52:43 2026", "2026-10-11T01:04:12Z", "2026-10-11T03:45:13Z"),
    ("pb-kit-u4-docs", 243, None, "Fri Oct  2 11:12:44 2026",
     "Tue Oct  6 18:29:36 2026", "2026-10-02T11:12:44Z", "2026-10-02T12:00:00Z"),
    ("pe-uk-2192-r2-a2", 16000, 16379, "Fri Oct  9 00:04:09 2026",
     "Sun Oct 11 09:57:21 2026", "2026-10-09T00:04:09Z", "2026-10-09T01:00:00Z"),
]


def recycled_attempt(daemon, harness, monkeypatch, case, **options):
    _, guardian, child, recorded, now, started_at, finished_at = case
    job_id, attempt, adir = reserve(daemon, harness, **options)
    pid = child or guardian
    owned = {str(pid): asdict(procs.ProcessIdentity(pid, "unit-test-boot", recorded))}
    evidence = json.loads(attempt["evidence_json"] or "{}")
    evidence["owned_identities"] = owned
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=guardian,
                                pgid=guardian, child_pid=child, boot_id="unit-test-boot",
                                proc_start=recorded, started_at=started_at,
                                evidence_json=json.dumps(evidence))
    # The stranger also has a child: excluding the root must prune its walk.
    census(monkeypatch, parents=f"{pid} 1 {pid} S {now}\n"
                                f"55000 {pid} {pid} S {now}\n")
    monkeypatch.setattr(procs, "containment", CONTAINMENT)
    monkeypatch.setattr(procs, "snapshot", SNAPSHOT)
    (adir / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "child_pid": child,
                                               "finished_at": finished_at, "wall_s": 60}))
    return job_id, daemon.store.get_attempt(attempt["attempt_id"]), adir


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_live_incident_census_excludes_recycled_pid(state_daemon, monkeypatch, case):
    """C-5.3, C-5.5: each live incident's stranger and children prove empty."""
    daemon, harness = state_daemon
    _, attempt, _ = recycled_attempt(daemon, harness, monkeypatch, case)
    result = daemon._contain(attempt)
    assert result.verified_empty, result.to_dict()
    pid = case[2] or case[1]
    evidence = result.to_dict()["recycled"][str(pid)]
    assert evidence["recorded"]["proc_start"] == case[3]
    assert evidence["now"]["proc_start"] == case[4]


def test_exit_zero_recycled_child_finalizes_succeeded(state_daemon, monkeypatch):
    """C-4.2, C-5.5, C-5.9: rc 0 plus a recycled child succeeds and exports."""
    daemon, harness = state_daemon
    out = harness.root / "export.md"
    job_id, attempt, adir = recycled_attempt(daemon, harness, monkeypatch, CASES[0], out_path=str(out))
    (adir / "stdout").write_bytes(b"fixture result\n")
    (adir / "stderr").write_bytes(b"")
    daemon.exit_settle_s = 0
    daemon._process_attempt(attempt["attempt_id"])
    daemon._process_attempt(attempt["attempt_id"])
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("succeeded", 0)
    assert job["accepted_attempt_id"] == attempt["attempt_id"]
    assert out.read_bytes() == b"fixture result\n"
    assert not daemon.store.list_leases(attempt["attempt_id"])
    shown = daemon.dispatch("show", {"job_id": job_id})
    assert "different start identity" in json.dumps(shown)
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["recycled"]["4203"]["recorded"]["proc_start"] == CASES[0][3]


def test_confirm_dead_recycled_child_releases_with_salvage(state_daemon, monkeypatch):
    """C-5.7, C-13.1: qyp16's shape confirms empty, releases leases and salvages."""
    daemon, harness = state_daemon
    def git(*args):
        return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=harness.workdir,
                              capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    git("init", "-b", "feature/recycled-fixture")
    tracked = harness.workdir / "tracked.txt"
    tracked.write_text("baseline\n")
    git("add", "tracked.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "baseline")
    # As in test_workspace_contract.repository: writable admission needs a
    # measured lane, so this fixture never tries to launch a probe guardian.
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .1, after(3600),
                                    ReadingLabel.PROVIDER, "fixture", utcnow()))
    job_id, attempt, _ = recycled_attempt(daemon, harness, monkeypatch, CASES[0],
                                         sandbox="workspace-write", in_place=True,
                                         no_preamble=True, max_attempts=1)
    tracked.write_text("completed deliverable\n")
    # Recreate the old census verdict without inspecting or signalling anything.
    daemon._quarantine(attempt, procs.Containment(descendant_pids=frozenset({4203})),
                       "writers remain after exit receipt")
    assert daemon.store.list_leases(attempt["attempt_id"]) or daemon.store.list_leases(job_id)
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                               protocol.KillArgs(job_id, confirm_dead=True))
    assert not daemon.store.list_leases(attempt["attempt_id"])
    assert not daemon.store.list_leases(job_id)
    salvage = [item for item in daemon.store.list_artifacts(attempt["attempt_id"])
               if item["role"] == "salvage"]
    assert len(salvage) == 1
    ref = salvage[0]["path"]
    assert git("show", f"{ref}:tracked.txt") == "completed deliverable"
    event = daemon.store.one("SELECT data_json FROM events WHERE kind='quarantine.confirmed_dead'")
    evidence = json.loads(event["data_json"])
    assert evidence["containment"]["live_pids"] == []
    assert "4203" in evidence["containment"]["recycled"]
    assert evidence["override"] is False
    shown = daemon.dispatch("show", {"job_id": job_id})
    assert "different start identity" in json.dumps(shown)


def test_probe_census_uses_the_same_identity_path(state_daemon, monkeypatch):
    """C-5.5: probes also exclude a recycled child through the common _contain."""
    daemon, harness = state_daemon
    _, attempt, adir = recycled_attempt(daemon, harness, monkeypatch, CASES[0])
    record = {**attempt, "holder": attempt["attempt_id"], "directory": str(adir),
              "owned_identities": json.loads(attempt["evidence_json"])["owned_identities"]}
    record.pop("attempt_id")
    record.pop("evidence_json")
    assert daemon._probe_census(record).verified_empty


@pytest.mark.parametrize("recorded", [False, True])
def test_exit_receipt_supplies_child_before_row_refresh(state_daemon, monkeypatch, recorded):
    """C-5.5: exit supplies the child PID; only a recorded identity can prove reuse."""
    daemon, harness = state_daemon
    _, attempt, _ = recycled_attempt(daemon, harness, monkeypatch, CASES[0])
    # The claim-aware deciding census refreshes the actual attempt row. Keep
    # this publication gap in that row, rather than only in a stale dict.
    updates = {"child_pid": None}
    if not recorded:
        updates["evidence_json"] = "{}"
    daemon.store.update_attempt(attempt["attempt_id"], **updates)
    attempt = daemon.store.get_attempt(attempt["attempt_id"])
    result = daemon._contain(attempt)
    if recorded:
        assert result.verified_empty
        assert result.recycled[4203]["reason"] == "different start identity"
    else:
        assert result.descendant_pids == {4203, 55000}
        assert result.unverifiable and not result.verified_empty
        assert not result.recycled


def test_exit_publication_during_census_keeps_the_unseen_child_hold(state_daemon, monkeypatch):
    """One receipt snapshot governs both the child root and publication hold."""
    daemon, harness = state_daemon
    attempt, directory = running(daemon, harness, 200, 201)
    daemon.store.update_attempt(attempt["attempt_id"], child_pid=None, evidence_json="{}")
    (directory / "start.json").write_text(json.dumps({
        "guardian_pid": 200, "pgid": 200, "boot_id": attempt["boot_id"],
        "proc_start": attempt["proc_start"]}))
    script_table(monkeypatch, {201: (1, 201, "S", "unpublished-child"),
                              202: (201, 201, "S", "unpublished-descendant")})
    read = daemon._read_json
    reads = []

    def publication(path):
        if path == directory / "exit.json":
            reads.append(path)
            return None if len(reads) == 1 else {"rc": 0, "child_pid": 201}
        return read(path)

    monkeypatch.setattr(daemon, "_read_json", publication)
    first = daemon._contain(daemon.store.get_attempt(attempt["attempt_id"]))
    assert not first.verified_empty
    assert "guardian child publication unavailable" in first.errors
    assert len(reads) == 1
    second = daemon._contain(daemon.store.get_attempt(attempt["attempt_id"]))
    assert not second.verified_empty and {201, 202} <= second.live_pids
    assert len(reads) == 2
