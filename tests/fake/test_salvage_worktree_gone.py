"""D-SALV-MISSING1, C-13.1: a worktree that no longer exists has nothing to salvage.

An attempt released from quarantine days after it ended can find its worktree already
removed (retention, or the operator). Release/217's salvage then ran `git -C <missing>
rev-parse`, recorded `salvage failed: git rev-parse failed (exit 128); ...` and told the
job "the worktree is kept: <path>", about a path that was not there. A missing worktree is
not a failure: salvage records "worktree already gone: nothing to salvage" in its receipt,
the attempt's evidence and the notice, and says nothing is kept.
"""

from __future__ import annotations

import json
import shutil

from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet.procs import Containment
from subfleet.salvage import SalvageError
from tests.fake.test_salvage_finalization import events, finalizing, job_notices
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401 (a fixture)
from tests.fake.test_workspace_contract import repository

#: What salvage says of a worktree that is not there (`daemon.WORKTREE_GONE`).
WORKTREE_GONE = "worktree already gone: nothing to salvage"
GONE_RECEIPT = {"result": None, "checkpoint": None, "error": None, "skipped": [], "gone": True}


def quarantined(daemon, harness):
    workdir = repository(daemon, harness)
    job_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    (workdir / "tracked.txt").write_text("provider progress\n")
    daemon._quarantine(attempt, Containment(marker_pids=frozenset({42099})), "escaped fixture")
    return workdir, job_id, daemon.store.get_attempt(attempt["attempt_id"]), adir


def test_d_salv_missing1_a_release_whose_worktree_is_gone_reports_nothing_to_salvage(state_daemon, monkeypatch):
    """`kill --confirm-dead` after the worktree went: no `git rev-parse` failure, no
    `salvage_error`, and no notice that says the worktree is kept."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = quarantined(daemon, harness)
    shutil.rmtree(workdir)
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, confirm_dead=True))
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "lost"
    assert daemon.store.list_leases() == []
    assert json.loads((adir / "salvage.json").read_text()) == GONE_RECEIPT
    [event] = events(daemon, job_id, "quarantine.confirmed_dead")
    assert event["salvage_gone"] == WORKTREE_GONE == getattr(daemon_module, "WORKTREE_GONE", None)
    assert "salvage_error" not in event
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_gone"] == WORKTREE_GONE and "salvage_error" not in evidence
    quarantine, released = job_notices(daemon, job_id)
    assert released.split("\n")[1:] == ["released from quarantine (attempt a1)", WORKTREE_GONE]
    assert not any("kept" in notice or "rev-parse" in notice for notice in (quarantine, released))
    assert not [row for row in daemon.store.list_artifacts(attempt["attempt_id"]) if row["role"] == "salvage"]


def test_d_salv_missing1_salvage_asks_git_nothing_about_a_worktree_already_gone(state_daemon, monkeypatch):
    """Gone before salvage: neither the snapshot nor its HEAD read runs on the path (release/217
    ran `git rev-parse` there). The check after a failed salvage alone would report the same
    receipt, so only this tells the two apart."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = quarantined(daemon, harness)
    shutil.rmtree(workdir)
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    asked = []
    monkeypatch.setattr(daemon_module, "salvage", lambda *args, **kwargs: asked.append("salvage"))
    monkeypatch.setattr(daemon_module, "git_head", lambda *args, **kwargs: asked.append("git_head"))
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, confirm_dead=True))
    assert asked == []
    assert json.loads((adir / "salvage.json").read_text()) == GONE_RECEIPT


def test_d_salv_missing1_a_finalization_whose_worktree_is_gone_is_no_failure(state_daemon):
    """The same at finalization: the job succeeds, its notice says nothing was there to salvage,
    and the evidence carries no `salvage_error` (which would make admission try to hold a
    start snapshot of a worktree that is not there, C-13.1)."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    shutil.rmtree(workdir)
    daemon._finalize(attempt)
    assert json.loads((adir / "salvage.json").read_text()) == GONE_RECEIPT
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_gone"] == WORKTREE_GONE and "salvage_error" not in evidence
    [notice] = job_notices(daemon, job_id)
    assert notice.endswith(f"\n{WORKTREE_GONE}") and "kept" not in notice


def test_d_salv_missing1_a_worktree_removed_while_salvage_runs_is_gone_not_failed(state_daemon, monkeypatch):
    """Retention can remove the worktree while the snapshot is taken: the failure that follows
    is the worktree's absence, so it is reported the same way, at once, without a retry."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = quarantined(daemon, harness)
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    calls = []

    def removed_meanwhile(*args, **kwargs):
        calls.append(1)
        shutil.rmtree(workdir)
        raise SalvageError("git add failed: fatal: Unable to read current working directory", transient=True)
    monkeypatch.setattr(daemon_module, "salvage", removed_meanwhile)
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, confirm_dead=True))
    assert calls == [1]
    assert json.loads((adir / "salvage.json").read_text()) == GONE_RECEIPT
    _, released = job_notices(daemon, job_id)
    assert released.split("\n")[1:] == ["released from quarantine (attempt a1)", WORKTREE_GONE]


def test_d_salv_missing1_a_worktree_that_is_there_but_broken_still_fails_and_is_kept(state_daemon, monkeypatch):
    """Only absence is "gone": a worktree that exists and cannot be salvaged (here its
    repository was removed from under it) is a failure as before, and is kept."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = quarantined(daemon, harness)
    shutil.rmtree(workdir / ".git")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, confirm_dead=True))
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["error"].startswith("salvage failed: ") and "gone" not in receipt
    _, released = job_notices(daemon, job_id)
    assert released.endswith(f"; the worktree is kept: {workdir}")
    assert WORKTREE_GONE not in released


def test_d_salv_missing1_a_replay_reads_the_gone_receipt(state_daemon, monkeypatch):
    """A replayed finalization reads the receipt: nothing is salvaged again, and the evidence
    says the worktree was gone."""
    daemon, harness = state_daemon
    _, _, attempt, adir = finalizing(daemon, harness)
    (adir / "salvage.json").write_text(json.dumps(GONE_RECEIPT))
    monkeypatch.setattr(daemon_module, "salvage", lambda *a, **k: (_ for _ in ()).throw(AssertionError("again")))
    daemon._finalize(attempt)
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_gone"] == WORKTREE_GONE and "salvage_error" not in evidence
