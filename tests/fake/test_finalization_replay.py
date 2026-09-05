"""Replay checks using durable rows and files, without provider processes."""
import errno

import pytest

from tests.fake.test_state_contract import state_daemon, reserve, receipt_fixture
from tests.fake_adapter import FakeAdapter


def test_c4_2_classification_and_attestation_are_frozen_before_acceptance(state_daemon, monkeypatch):
    """C-4.2 finalizing, C-8.2 replay reuses classification, attestation and captured deliverable."""
    daemon, harness = state_daemon
    job_id, a, adir = reserve(daemon, harness)
    finalizing = receipt_fixture(daemon, a, adir)
    notice = daemon._notice
    def interrupted(*args):
        raise RuntimeError("acceptance interrupted")
    monkeypatch.setattr(daemon, "_notice", interrupted)
    with pytest.raises(RuntimeError, match="acceptance interrupted"):
        daemon._finalize(finalizing)
    assert (adir / "finalization.json").is_file()
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    def reread(*args):
        raise AssertionError("completed adapter step was repeated")
    monkeypatch.setattr(FakeAdapter, "classify", reread)
    monkeypatch.setattr(FakeAdapter, "attest", reread)
    monkeypatch.setattr(FakeAdapter, "deliverable", reread)
    (adir / "stdout").write_bytes(b"later external append\n")
    monkeypatch.setattr(daemon, "_notice", notice)
    daemon._finalize(finalizing)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
    assert (adir / "deliverable.md").read_bytes() == b"fixture result\n"
    assert len(daemon.store.list_notices()) == 1


def test_c4_3_stale_attempt_cannot_publish_or_accept(state_daemon):
    """C-4.3 an older sequence cannot run publication steps or accept over the current attempt."""
    daemon, harness = state_daemon
    job_id, old, old_dir = reserve(daemon, harness)
    old = receipt_fixture(daemon, old, old_dir)
    current_id = job_id + "/a2"
    daemon.store.add_attempt(attempt_id=current_id, job_id=job_id, seq=2,
        lane_id=old["lane_id"], model_requested=old["model_requested"], state="finalizing")
    daemon._finalize(old)
    assert not (old_dir / "deliverable.md").exists()
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    assert daemon.store.list_artifacts(old["attempt_id"]) == []
    assert daemon.store.list_notices() == []


def test_c20_3_finalization_metadata_disk_full_is_recoverable(state_daemon):
    """C-20.3, C-4.2 ENOSPC while freezing adapter evidence cannot commit partial acceptance."""
    daemon, harness = state_daemon
    job_id, a, adir = reserve(daemon, harness)
    a = receipt_fixture(daemon, a, adir)
    def full(role, path):
        if role == "finalization":
            raise OSError(errno.ENOSPC, "injected metadata write failure")
    daemon.publish_hook = full
    with pytest.raises(OSError) as error:
        daemon._finalize(a)
    assert error.value.errno == errno.ENOSPC
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "finalizing"
    assert daemon.store.list_notices() == []
    daemon.publish_hook = None
    daemon._finalize(a)
    assert daemon.store.get_job(job_id)["state"] == "succeeded"
