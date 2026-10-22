"""D-TMP1: temp allocations count as job bytes and pin census evidence."""

import json
import os
from pathlib import Path

from subfleet import attempt_tmp, retention
from subfleet.disk import DiskAdmission
from subfleet.store import Store
from tests.conftest import make_lane
from subfleet.home_paths import PYTEST_SESSION


def test_retention_counts_private_scratch_and_keeps_its_attempt_evidence(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    scratch = attempt_tmp.AttemptTemp(root)
    record = scratch.create("job/a1")
    payload = Path(record["path"]) / "venv-file"
    payload.write_bytes(b"x" * 65536)
    with Store(root / "state.sqlite3") as store:
        store.put_lane(make_lane("fake"))
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(tmp_path), prompt_path="/prompt", sandbox="read-only", state="succeeded")
        store.conn.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at,evidence_json) "
                           "VALUES('job/a1','job',1,'fake','fake','succeeded','2026-10-22T00:00:00Z',?)",
                           (json.dumps({"tmpdir": record}),))
        found = list(attempt_tmp.job_tmpdirs(store, root, job_id="job"))
        assert found == [("job", record)]
        assert scratch.size(record) >= 65536
        pins = retention._pin_reasons(store, set(), None, root=root)
        assert pins["job"] == "attempt-tmpdir"
        state = retention.RetentionState()
        retention.maintenance(store, root, max_jobs=10, max_bytes=10**9, state=state)
        assert state.sizes["job"][0] >= scratch.size(record)
        assert scratch.remove(record, {"attempt_id": "job/a1", "state": "succeeded"}, verified_empty=True)
        assert list(attempt_tmp.job_tmpdirs(store, root)) == []
        # Physical deletion alone cannot erase authority inside an archive
        # transaction. The daemon commits a cleanup receipt before unpinning.
        assert retention._pin_reasons(store, set(), None, root=root)["job"] == "attempt-tmpdir"
        store.conn.execute("UPDATE attempts SET evidence_json=json_set(evidence_json,'$.tmpdir.removed_at','now')")
        assert "job" not in retention._pin_reasons(store, set(), None, root=root)


def test_probe_leftovers_keep_their_job_and_count_in_its_bytes(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    scratch = attempt_tmp.AttemptTemp(root)
    record = scratch.create("probe:dummy")
    (Path(record["path"]) / "cache").write_bytes(b"x" * 8192)
    probe = {"holder": "probe:dummy", "job_id": "job", "state": "quarantined", "tmpdir": record}
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(tmp_path), prompt_path="/prompt", sandbox="read-only", state="cancelled")
        store.add_event("probe.state", job_id="job", data=probe)
        assert retention._pin_reasons(store, set(), None, root=root)["job"] == "probe-tmpdir"
        assert list(attempt_tmp.job_tmpdirs(store, root, job_id="job")) == [("job", record)]
        state = retention.RetentionState()
        retention.maintenance(store, root, max_jobs=10, max_bytes=10**9, state=state)
        assert state.sizes["job"][0] >= 8192
        record["removed_at"] = "operator-resolved"
        store.add_event("probe.state", job_id="job", data=probe)
        assert "job" not in retention._pin_reasons(store, set(), None, root=root)


def test_worktree_and_tmpdir_share_one_provisional_disk_budget(tmp_path):
    gate = DiskAdmission(tmp_path)
    gate.begin_pass({"admission": {"disk": {"enabled": True}}}, [], "2026-10-22T00:00:00Z")
    gate.reserve("job/a1", "2026-10-22T00:00:00Z")
    assert gate.reserved_bytes == gate.evidence()["bytes"]
    # Like worktree contents, scratch already allocated is in statvfs free
    # space. The placement budget reserves anticipated growth once per job.
    assert gate.reserved_bytes > 0


def test_production_darwin_root_comes_from_confstr_not_tmpdir(tmp_path, monkeypatch, private_attempt_temp_root):
    monkeypatch.setattr(attempt_tmp, "user_temp_root", private_attempt_temp_root)
    monkeypatch.setattr(attempt_tmp.sys, "platform", "darwin")
    monkeypatch.setattr(attempt_tmp, "under_pytest", lambda: False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "daemon-shared"))
    calls = []
    monkeypatch.setattr(attempt_tmp.os, "confstr", lambda key: calls.append(key) or str(tmp_path))
    assert attempt_tmp.user_temp_root() == tmp_path.resolve()
    assert calls == [attempt_tmp.os.confstr_names.get("CS_DARWIN_USER_TEMP_DIR", 65537)]


def test_session_scratch_baseline_survives_fixture_undo_and_environment_replacement(tmp_path, monkeypatch):
    session = Path(os.environ[PYTEST_SESSION]).resolve()
    monkeypatch.undo()
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", {"PATH": os.defpath})
        scratch = attempt_tmp.AttemptTemp(tmp_path / "store")
        # Check before any mkdir: the failing version must create no folder
        # outside this test session's scratch.
        assert scratch.root.is_relative_to(session)
        record = scratch.create("job/a1")
        assert scratch.remove(record, {"attempt_id": "job/a1", "state": "succeeded"}, verified_empty=True)
