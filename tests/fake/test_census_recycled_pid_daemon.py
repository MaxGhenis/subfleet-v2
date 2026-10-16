"""C-5.3, C-5.5: the three recycled-PID incidents of 2026-10-11."""

from dataclasses import asdict
import json
import subprocess

import pytest

from subfleet import procs, protocol
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.unit.test_procs import BOOT_A, START, census
from tests.unit.test_census_responsibility_races import changing_root


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
    # As in test_state_contract.receipt_fixture: an exited generic fake's success
    # carries the daemon's detached Codex completion proof (C-9.2).
    (adir / "stream.jsonl").write_text('{"type":"turn.completed"}\n')
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
    attempt["child_pid"] = None
    if not recorded:
        attempt["evidence_json"] = "{}"
    result = daemon._contain(attempt)
    if recorded:
        assert result.verified_empty
        assert result.recycled[4203]["reason"] == "different start identity"
    else:
        assert result.descendant_pids == {4203, 55000}
        assert result.unverifiable and not result.verified_empty
        assert not result.recycled


@pytest.mark.parametrize("path", ["kill", "probe", "finalize", "quarantine", "reap"])
@pytest.mark.parametrize("persisted", [False, True], ids=["just-observed", "persisted"])
def test_responsibility_evidence_and_its_walk_never_authorize_signals(
        state_daemon, monkeypatch, path, persisted):
    """C-5.4–7: even uncertain root-death evidence only holds the attempt.

    Exercise the actual deciding paths, with both an evidence-only seed and a
    child found solely by walking from it. Signals and waitpid are intercepted
    before any host pid can be touched.
    """
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness)
    monkeypatch.setattr(procs, "containment", CONTAINMENT)
    _, _, state = changing_root(monkeypatch, timing="after-members", fate="reaped")
    attempt.update(guardian_pid=42, pgid=42, child_pid=None,
                   boot_id=BOOT_A, proc_start=START, state="running",
                   evidence_json=json.dumps({"responsible_root": {"pid": 42, "unique_id": None}}))
    if persisted:
        state.scanning = False
        state.fake.processes.pop(42)
        state.starts.pop(42)
        state.fake.boss[61] = 61
        evidence = json.loads(attempt["evidence_json"])
        evidence["responsible_identities"] = {"61": asdict(procs.ProcessIdentity(61, BOOT_A, START))}
        attempt["evidence_json"] = json.dumps(evidence)
    daemon.store.update_attempt(attempt["attempt_id"], **{key: attempt[key] for key in (
        "guardian_pid", "pgid", "child_pid", "boot_id", "proc_start", "state", "evidence_json")})
    first = daemon._contain(attempt)
    assert 61 in (first.descendant_pids if persisted else first.responsible_pids)
    assert "61" in json.loads(attempt["evidence_json"])["responsible_identities"]
    state.scanning = False
    snapshot = procs.snapshot

    def with_child():
        table = snapshot()
        return procs.ProcessTable({**table.rows, 62: (61, 62, "S", attempt["proc_start"])}, table.boot_id)

    monkeypatch.setattr(procs, "snapshot", with_child)
    assert daemon._contain(attempt).descendant_pids >= {61, 62}
    signalled, waited = [], []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(procs.os, "killpg", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(procs.os, "waitpid", lambda pid, flags: waited.append(pid) or (pid, 0))
    daemon.term_grace_s = daemon.kill_settle_s = daemon.exit_settle_s = 0
    monkeypatch.setattr(daemon, "_salvage", lambda *a, **k: pytest.fail("salvage with live evidence"))
    if path == "kill":
        daemon._kill_attempt(attempt)
    elif path == "probe":
        record = {**attempt, "holder": attempt["attempt_id"], "directory": str(harness.root),
                  **json.loads(attempt["evidence_json"]), "owned_identities": {}}
        record.pop("attempt_id")
        record.pop("evidence_json")
        assert not daemon._contain_probe(record)
        assert not record["owned_identities"]
    elif path == "finalize":
        daemon._finalize(attempt)
    elif path == "quarantine":
        daemon._quarantine(attempt, daemon._contain(attempt), "evidence only")
        daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                                   protocol.KillArgs(job_id, confirm_dead=True))
        assert daemon.store.list_leases(attempt["attempt_id"]) or daemon.store.list_leases(job_id)
        daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                                   protocol.KillArgs(job_id, force_release=True, operator_note="fixture"))
    else:
        monkeypatch.setattr(procs, "_abandoned", [])
        daemon._children[attempt["attempt_id"]] = procs.Spawned(["fixture guardian"], 42)
        daemon._quarantine(attempt, daemon._contain(attempt), "evidence only")
        monkeypatch.setattr(procs._spawn, "available", False)
        monkeypatch.setattr(procs.subprocess, "Popen", lambda *a, **k: None)
        procs.spawn_disclaimed(["next fixture"], env={}, cwd=str(harness.root))
        assert waited == [42]
    assert not signalled
    assert not ({61, 62} & set(waited))
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert not ({"61", "62"} & evidence.get("owned_identities", {}).keys())
