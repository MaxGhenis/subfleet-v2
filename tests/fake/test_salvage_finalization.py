"""C-13.1, C-13.4: a salvage that cannot succeed ends the attempt and frees its lane.

Before, a finalizing attempt whose salvage failed raised to the worker, which
tried it again every 60 s for as long as the daemon ran: the attempt never left
`finalizing` and held its lane slot (2026-09-27, codex-3 and codex-5, for 2.5 h
and 5 h). Now a transient failure is tried `SALVAGE_TRIES` times in all and any
other failure is recorded at once; finalization then goes on without a salvage
ref and the worktree is kept.
"""
from __future__ import annotations

import json

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import SALVAGE_TRIES
from subfleet.salvage import SalvageError
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401 (a fixture)
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git


def lane_leases(daemon):
    return [row for row in daemon.store.list_leases() if row["lease_key"].startswith("lane:")]


def finalizing(daemon, harness, *, change=True):
    workdir = repository(daemon, harness)
    job_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    if change:
        (workdir / "tracked.txt").write_text("provider progress\n")
    return workdir, job_id, receipt_fixture(daemon, attempt, adir), adir


def test_c13_1_a_salvage_that_cannot_succeed_is_recorded_and_the_attempt_ends(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    calls = []

    def refuse(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add failed: fatal: adding files failed")
    monkeypatch.setattr(daemon_module, "salvage", refuse)
    daemon._finalize(attempt)
    assert calls == [1]                                       # not transient: recorded at once
    job = daemon.store.get_job(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0     # the provider's result stands
    assert lane_leases(daemon) == []                          # the lane is free
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert row["state"] == "succeeded"
    evidence = json.loads(row["evidence_json"])
    assert evidence["salvage_error"] == "salvage failed: git add failed: fatal: adding files failed"
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt == {"result": None, "checkpoint": None, "error": evidence["salvage_error"]}
    assert not [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    notice = daemon.store.list_notices()[-1]["text"]
    assert "salvage failed" in notice and f"the worktree is kept: {workdir}" in notice
    assert (workdir / "tracked.txt").read_text() == "provider progress\n"   # nothing was touched


def test_c13_1_a_transient_failure_is_tried_again_then_recorded(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, job_id, attempt, _ = finalizing(daemon, harness)
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add timed out after 60 s", transient=True)
    monkeypatch.setattr(daemon_module, "salvage", slow)
    for tries in range(1, SALVAGE_TRIES):
        with pytest.raises(SalvageError):
            daemon._finalize(attempt)                         # the worker's backoff tries it again
        assert len(calls) == tries
        assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    daemon._finalize(attempt)
    assert len(calls) == SALVAGE_TRIES
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert "timed out" in evidence["salvage_error"]
    assert attempt["attempt_id"] not in daemon._salvage_failures


def test_c13_1_a_transient_failure_that_clears_salvages_normally(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    workdir, job_id, attempt, _ = finalizing(daemon, harness)
    real, failures = daemon_module.salvage, [SalvageError("git add timed out after 60 s", transient=True)]

    def once(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)
    monkeypatch.setattr(daemon_module, "salvage", once)
    with pytest.raises(SalvageError):
        daemon._finalize(attempt)
    daemon._finalize(attempt)
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert "salvage_error" not in json.loads(row["evidence_json"])
    [artifact] = [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    assert git(workdir, "show", f"{artifact['path']}:tracked.txt") == "provider progress"
    assert attempt["attempt_id"] not in daemon._salvage_failures


def test_c13_1_the_case_that_held_the_lanes_now_finalizes_with_a_snapshot(state_daemon):
    """No stand-in: a real empty repository under untracked scratch, as the reviewers left it."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    nested = workdir / ".review-scratch" / "pytest" / "repo "
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "inside.txt").write_text("never committed\n")
    daemon._finalize(attempt)
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["error"] is None and receipt["result"]["skipped"] == [".review-scratch/pytest/repo /"]
    assert git(workdir, "show", f"{receipt['result']['ref']}:tracked.txt") == "provider progress"
    assert "salvage_error" not in json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])


def test_c13_1_a_replayed_finalization_reads_the_receipt_and_does_not_salvage_again(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, _, attempt, adir = finalizing(daemon, harness)
    (adir / "salvage.json").write_text(json.dumps({"result": None, "checkpoint": None,
                                                   "error": "salvage failed: recorded earlier"}))
    monkeypatch.setattr(daemon_module, "salvage", lambda *a, **k: pytest.fail("salvaged twice"))
    daemon._finalize(attempt)
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_error"] == "salvage failed: recorded earlier"

def test_c13_1_a_snapshot_written_before_head_could_be_read_stands(state_daemon, monkeypatch):
    """Only the checkpoint read failed: the ref is recorded, the error names the checkpoint,
    and the notice does not claim the worktree was left unsaved."""
    daemon, harness = state_daemon
    workdir, _, attempt, adir = finalizing(daemon, harness)

    def no_head(*args, **kwargs):
        raise SalvageError("git rev-parse timed out after 60 s", transient=True)
    monkeypatch.setattr(daemon_module, "git_head", no_head)
    for _ in range(1, SALVAGE_TRIES):
        with pytest.raises(SalvageError):
            daemon._finalize(attempt)
    daemon._finalize(attempt)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["result"]["ref"].startswith("refs/subfleet-salvage/")
    assert receipt["checkpoint"] is None and receipt["error"].startswith("checkpoint failed: ")
    [artifact] = [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    assert git(workdir, "show", f"{artifact['path']}:tracked.txt") == "provider progress"
    notice = daemon.store.list_notices()[-1]["text"]
    assert "checkpoint failed" in notice and "the worktree is kept" not in notice

def test_c13_1_a_quarantine_release_records_a_failed_salvage_at_once(state_daemon, monkeypatch):
    """An operator's one-shot request is never offered again, so even a transient
    failure is recorded, in the release's audit event, and the attempt is released."""
    from subfleet import protocol
    from subfleet.procs import Containment
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    daemon._quarantine(attempt, Containment(marker_pids=frozenset({42099})), "escaped fixture")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add timed out after 60 s", transient=True)
    monkeypatch.setattr(daemon_module, "salvage", slow)
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                               protocol.KillArgs(job_id, confirm_dead=True))
    assert calls == [1]
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "lost"
    assert daemon.store.list_leases() == []
    event = json.loads(daemon.store.one("SELECT * FROM events WHERE kind='quarantine.confirmed_dead'")["data_json"])
    assert event["salvage_error"] == "salvage failed: git add timed out after 60 s"
