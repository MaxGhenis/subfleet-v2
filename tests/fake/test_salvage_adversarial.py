"""Adversarial combinations for the incident: a failed finalization must converge."""

import errno
import json
import subprocess

import pytest

from subfleet import salvage as salvage_module
from subfleet.daemon import SALVAGE_TRIES, TURN_TREE_TRIES
from subfleet.salvage import SalvageError
from subfleet.adapters.registry import register
from tests.fake.test_salvage_finalization import TransientAdapter, empty_repository, finalizing, lane_leases
from tests.fake_adapter import FakeAdapter
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401
from tests.fake.test_turn_trees import writable_turn
from tests.unit.test_salvage import git


def ending(daemon, harness, turn):
    if not turn:
        return finalizing(daemon, harness)
    job_id, attempt, adir, _, _, _ = writable_turn(daemon, harness)
    (harness.workdir / "tracked.txt").write_text("provider progress\n")
    (adir / "turn.json").write_text(json.dumps({"state": "complete", "reason": None, "final_text": "done"}))
    return harness.workdir, job_id, receipt_fixture(daemon, attempt, adir), adir


@pytest.mark.parametrize("turn", [False, True], ids=["detached", "turn"])
@pytest.mark.parametrize("full_disk", [False, True], ids=["permission", "full-disk"])
def test_nested_repository_and_non_utf8_failure_end_after_bounded_retries(
        state_daemon, monkeypatch, turn, full_disk):
    """A real first add discovers the unborn repo; the excluded retry hits a second
    failure. A binary path in that failure must not restart the finalization loop.
    APFS forbids binary names, so only the second add's result is supplied."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = ending(daemon, harness, turn)
    empty_repository(workdir / "scratch/empty")
    real_index = (workdir / ".git/index").read_bytes()
    run = salvage_module.subprocess.run
    failures = []
    reason = b"No space left on device" if full_disk else b"Permission denied"

    def second_add_fails(command, **kwargs):
        if "--pathspec-from-file=-" in command:
            assert b":(top,exclude,literal)scratch/empty/\0" in kwargs["input"]
            failures.append(1)
            return subprocess.CompletedProcess(command, 128, b"",
                b'error: open("caf\xe9.txt"): ' + reason + b"\nfatal: adding files failed\n")
        return run(command, **kwargs)

    monkeypatch.setattr(salvage_module.subprocess, "run", second_add_fails)
    tries = (TURN_TREE_TRIES if turn else SALVAGE_TRIES) if full_disk else 1
    for _ in range(tries - 1):
        with pytest.raises(SalvageError) as caught:
            daemon._finalize(attempt)
        assert caught.value.transient
        assert lane_leases(daemon)
    daemon._finalize(attempt)
    assert len(failures) == tries
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    assert lane_leases(daemon) == []
    receipt_path = adir / ("trees.json" if turn else "salvage.json")
    receipt_bytes = receipt_path.read_bytes()
    assert "caf\\xe9.txt" in json.loads(receipt_bytes)["error"]
    receipt_bytes.decode("utf-8", "strict")
    assert (workdir / ".git/index").read_bytes() == real_index
    assert git(workdir, "for-each-ref", "refs/subfleet-salvage") == ""
    assert (workdir / "scratch/empty/inside.txt").read_text() == "never committed\n"
    daemon._finalize(attempt)
    assert len(failures) == tries and receipt_path.read_bytes() == receipt_bytes


@pytest.mark.parametrize("turn", [False, True], ids=["detached", "turn"])
def test_removed_workspace_during_finalization_records_failure_and_replays(state_daemon, turn):
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = ending(daemon, harness, turn)
    moved = workdir.with_name(workdir.name + "-removed")
    workdir.rename(moved)
    try:
        daemon._finalize(attempt)
        assert daemon.store.get_job(job_id)["state"] == "succeeded"
        assert lane_leases(daemon) == []
        receipt_path = adir / ("trees.json" if turn else "salvage.json")
        original = receipt_path.read_bytes()
        assert "No such file or directory" in json.loads(original)["error"]
        daemon._finalize(attempt)
        assert receipt_path.read_bytes() == original
        assert (moved / "tracked.txt").read_text() == "provider progress\n"
    finally:
        moved.rename(workdir)


def test_full_disk_publishing_receipt_reuses_the_existing_salvage_ref(state_daemon, monkeypatch):
    """A disk error can occur after Git has held the work but before the receipt.
    Once writing works, finalization must reuse the held ref and free the lane."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    publish = daemon._publish
    failed = []

    def full_once(role, path, contents):
        if role == "salvage" and not failed:
            failed.append(1)
            raise OSError(errno.ENOSPC, "No space left on device")
        return publish(role, path, contents)

    monkeypatch.setattr(daemon, "_publish", full_once)
    with pytest.raises(OSError) as caught:
        daemon._finalize(attempt)
    assert caught.value.errno == errno.ENOSPC
    assert lane_leases(daemon) and not (adir / "salvage.json").exists()
    [ref] = git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage").splitlines()
    commit = git(workdir, "rev-parse", ref)
    daemon._finalize(attempt)
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    receipt = json.loads((adir / "salvage.json").read_bytes())
    assert receipt["result"]["ref"] == ref and receipt["result"]["commit"] == commit
    assert receipt["error"] is None
    assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage") == ref
    assert git(workdir, "show", f"{ref}:tracked.txt") == "provider progress"


def test_a_tracked_file_replaced_by_an_empty_repository_is_salvaged_and_the_retry_admitted(state_daemon):
    """Adversarial review of the round-3 branch, real git: `rm tracked.txt; git init
    tracked.txt` beside new work. `ls-files -o` does not list `tracked.txt/`, so nothing
    was excluded: the salvage failed for good, held nothing (the new work included), and
    the job's retry failed at admission on the same snapshot. Now the salvage holds the
    new work and the file's removal, leaves the repository out and says so, and the retry
    is admitted."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    register("codex", TransientAdapter)
    (workdir / "unrelated-work.txt").write_text("real work\n")
    (workdir / "tracked.txt").unlink()
    empty_repository(workdir / "tracked.txt")
    daemon._finalize(attempt)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["error"] is None and receipt["skipped"] == ["tracked.txt/"]
    names = git(workdir, "ls-tree", "-r", "--name-only", receipt["result"]["ref"]).splitlines()
    assert "unrelated-work.txt" in names and "tracked.txt" not in names
    assert lane_leases(daemon) == [] and daemon.store.get_job(job_id)["state"] == "waiting"
    daemon.store.update_job(job_id, next_check_at=None)
    register("codex", FakeAdapter)
    daemon._admit()
    a1, a2 = daemon.store.list_attempts(job_id)
    assert a2["state"] == "reserved"
    assert json.loads(a2["evidence_json"])["baseline_skipped"] == {"count": 1, "paths": ["tracked.txt/"]}
    assert (workdir / "tracked.txt" / "inside.txt").read_text() == "never committed\n"
