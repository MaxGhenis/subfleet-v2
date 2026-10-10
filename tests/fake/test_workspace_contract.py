"""Reservation and launch safety with real temporary git repositories, no providers."""

import json

import pytest

from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon
from tests.unit.test_salvage import git


def repository(daemon, harness):
    workdir = harness.workdir
    git(workdir, "init", "-b", "task/example")
    git(workdir, "config", "user.name", "Test User")
    git(workdir, "config", "user.email", "test@example.invalid")
    (workdir / "tracked.txt").write_text("baseline\n")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "baseline")
    # Writable admission requires measured capacity; a deterministic reading
    # keeps this state-only suite from starting even a fake probe guardian.
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .1,
                                    after(3600), ReadingLabel.PROVIDER, "fixture", utcnow()))
    return workdir


@pytest.mark.parametrize("boundary", ["queued", "reserved"])
@pytest.mark.parametrize("branch", ["main", "master"])
def test_c6_5_branch_switch_before_launch_is_refused(state_daemon, boundary, branch):
    """C-6.5, C-13.2 writable jobs recheck protected branches after submission and reservation."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    if boundary == "queued":
        job_id = daemon.dispatch("submit", harness.submit_args(
            sandbox="workspace-write", in_place=True))["job_id"]
    else:
        job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    git(workdir, "branch", "-m", branch)
    if boundary == "queued":
        daemon._admit()
        assert daemon.store.list_attempts(job_id) == []
    else:
        # The fixture forbids scheduled launches. Call the implementation here:
        # a missing branch refusal would try a real guardian and fail this test.
        Daemon._launch(daemon, attempt)
        assert daemon._children == {}
        daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    job = daemon.store.get_job(job_id)
    assert job["state"] == "failed" and job["rc"] == 7
    assert branch in daemon.store.list_notices()[0]["text"]
    assert "fix:" in daemon.store.list_notices()[0]["text"]
    assert daemon.store.list_leases() == []


@pytest.mark.parametrize("provider_changed", [False, True])
def test_c13_1_reservation_records_actual_working_tree(state_daemon, provider_changed):
    """C-13.1 the reservation baseline includes existing dirty files, and salvage preserves new work."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    (workdir / "tracked.txt").write_text("pre-existing dirty work\n")
    (workdir / "untracked.txt").write_text("pre-existing untracked work\n")
    index = (workdir / ".git" / "index").read_bytes()
    job_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    assert attempt["baseline_tree"] != git(workdir, "rev-parse", "HEAD^{tree}")
    assert git(workdir, "show", f"{attempt['baseline_tree']}:tracked.txt") == "pre-existing dirty work"
    if provider_changed:
        (workdir / "tracked.txt").write_text("provider progress\n")
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    artifacts = [row for row in daemon.store.list_artifacts(attempt["attempt_id"])
                 if row["role"] == "salvage"]
    assert len(artifacts) == int(provider_changed)
    if provider_changed:
        assert git(workdir, "show", f"{artifacts[0]['path']}:tracked.txt") == "provider progress"
        assert git(workdir, "show", f"{artifacts[0]['path']}:untracked.txt") == "pre-existing untracked work"
    assert (workdir / ".git" / "index").read_bytes() == index


def sparse_repository(daemon, harness):
    """`repository` as a sparse checkout shaped like chief-of-staff's: code the
    patterns keep on disk, and state files they leave off it."""
    workdir = repository(daemon, harness)
    for name in ("bin/tool", "state/a.json", "state/deep/b.json"):
        (workdir / name).parent.mkdir(parents=True, exist_ok=True)
        (workdir / name).write_text(f"{name}\n")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "code and state")
    git(workdir, "sparse-checkout", "set", "--no-cone", "/bin/", "/tracked.txt")
    assert not (workdir / "state").exists()
    return workdir


@pytest.mark.parametrize("provider_changed", [False, True])
def test_c13_1_a_writable_job_in_a_sparse_checkout_is_prepared_and_salvaged(state_daemon, provider_changed):
    """C-13.1, C-6.8 the 2026-10-09 failure, through admission and finalization. A
    writable in-place job in a sparse checkout with an untracked file outside the
    patterns failed before launch ("workspace preparation failed: SalvageError:
    git add failed: The following paths and/or pathspecs matched paths that exist
    outside of your sparse-checkout definition"). It is reserved; its baseline
    holds that file and every file the patterns leave out; and its salvage, when
    the provider wrote anything, records the work in the patterns and outside them."""
    daemon, harness = state_daemon
    workdir = sparse_repository(daemon, harness)
    (workdir / ".review-scratch" / "opus").mkdir(parents=True)
    (workdir / ".review-scratch" / "opus" / "baseline.txt").write_text("scratch\n")
    index = (workdir / ".git" / "index").read_bytes()
    job_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    assert git(workdir, "diff-tree", "-r", "--name-status", "HEAD^{tree}", attempt["baseline_tree"]) \
        == "A\t.review-scratch/opus/baseline.txt"
    if provider_changed:
        (workdir / "bin" / "tool").write_text("provider progress\n")
        (workdir / "state").mkdir()
        (workdir / "state" / "a.json").write_text("provider wrote outside the patterns\n")
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    artifacts = [row for row in daemon.store.list_artifacts(attempt["attempt_id"])
                 if row["role"] == "salvage"]
    assert len(artifacts) == int(provider_changed)
    if provider_changed:
        ref = artifacts[0]["path"]
        assert sorted(git(workdir, "diff-tree", "-r", "--name-status", attempt["baseline_tree"],
                          ref).splitlines()) == ["M\tbin/tool", "M\tstate/a.json"]
        assert git(workdir, "rev-parse", f"{ref}:state/deep/b.json") \
            == git(workdir, "rev-parse", "HEAD:state/deep/b.json")
    assert (workdir / ".git" / "index").read_bytes() == index


@pytest.mark.parametrize("receipt_has_rc", [False, True])
def test_c4_4_exit_receipt_without_return_code_is_lost(state_daemon, receipt_has_rc):
    """C-4.4 a final attempt with no recorded rc is lost even when a receipt exists."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1)
    finalizing = receipt_fixture(daemon, attempt, adir, rc=None)
    if not receipt_has_rc:
        receipt = json.loads((adir / "exit.json").read_text())
        receipt.pop("rc")
        (adir / "exit.json").write_text(json.dumps(receipt))
        daemon._begin_finalizing(attempt, receipt)
    daemon._finalize(finalizing)
    job = daemon.store.get_job(job_id)
    assert job["state"] == "lost" and job["rc"] == 125
    assert job["accepted_attempt_id"] is None
    assert daemon.store.get_attempt(attempt["attempt_id"])["rc"] is None
    assert daemon.store.list_leases() == []
