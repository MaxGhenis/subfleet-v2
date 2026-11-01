"""C-8.4: incomplete alternates discovery admits jobs and fails retirement closed."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from subfleet import dependencies, folders, retention
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_admission_liveness import _live
from tests.unit.retention_world import World


def damage(monkeypatch, alternate, defect):
    alternate.parent.mkdir(parents=True, exist_ok=True)
    if defect == "overlong":
        alternate.write_bytes(b"#" + b"x" * dependencies._ALTERNATE_LINE_LIMIT + b"\n")
    elif defect == "quote":
        alternate.write_bytes(b'"bad\\z"\n')
    elif defect == "fifo":
        import os
        os.mkfifo(alternate)
    else:
        alternate.write_bytes(b"# read this first\n")
        real = dependencies.open_regular

        class Partial(io.BytesIO):
            def readline(self, size=-1):
                if self.tell():
                    raise OSError("fixture read interrupted")
                return super().readline(size)

        def opening(path, *args, **kwargs):
            if str(path) == str(alternate):
                if defect == "unreadable":
                    raise PermissionError("fixture cannot read alternates")
                return Partial(alternate.read_bytes())
            return real(path, *args, **kwargs)

        monkeypatch.setattr(dependencies, "open_regular", opening)


@pytest.mark.parametrize("defect", ["overlong", "quote", "unreadable", "partial", "fifo"])
@pytest.mark.parametrize("mode", ["recorded", "legacy", "exporting"])
def test_c8_4_incomplete_discovery_keeps_every_allocated_tree(tmp_path, monkeypatch, defect, mode):
    world = World(tmp_path)
    try:
        tree = world.job("owner")
        borrower = tmp_path / "borrower"
        alternate = borrower / ".git" / "objects" / "info" / "alternates"
        damage(monkeypatch, alternate, defect)
        recorded = dependencies.record(str(borrower))
        assert recorded["incomplete"][0]["file"] == str(alternate)
        world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                            kind="dispatch", state="succeeded" if mode == "exporting" else "queued",
                            sandbox="read-only", workdir=str(borrower), prompt_path="/p")
        world.store.add_event("job.submitted", job_id="borrower",
                              data={} if mode == "legacy" else {"depends": recorded})
        if mode == "exporting":
            assert world.store.acquire_lease("out:/outside/result", "borrower")
        result = retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                       holders=lambda *a, **k: {})
        assert not result["pruned"], result
        reason = result["pin_reasons"]["owner"]
        assert "dependency-incomplete" in reason and "borrower" in reason and str(alternate) in reason
        assert tree.is_dir()
        world.store.update_job("borrower", state="succeeded")
        world.store.release_leases("borrower")
        result = retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                       holders=lambda *a, **k: {})
        assert "owner" in result["pruned"], result
    finally:
        world.close()


def test_c8_4_incomplete_alternates_still_admit(tmp_path):
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        base = harness.workdir / ".git" / "objects" / "info"
        base.mkdir(parents=True, exist_ok=True)
        (base / "alternates").write_bytes(b'"bad\\z"\n')
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        job = submit(daemon, harness)
        daemon._admit()
        assert _live(daemon, job), daemon._holds.get(job)
        assert dependencies._loads(daemon.store.one(
            "SELECT data_json FROM events WHERE job_id=? AND kind='job.submitted'", (job,))["data_json"])["depends"]["incomplete"]


def test_c8_4_missing_alternates_are_complete_but_dangling_link_is_not(tmp_path):
    base = tmp_path / ".git" / "objects" / "info"
    base.mkdir(parents=True)
    assert "incomplete" not in dependencies.record(str(tmp_path))
    (base / "alternates").symlink_to(tmp_path / "gone")
    assert dependencies.record(str(tmp_path))["incomplete"][0]["file"] == str(base / "alternates")


def test_c8_4_incomplete_recheck_uses_database_only(tmp_path, monkeypatch):
    world = World(tmp_path)
    try:
        tree = world.job("owner")
        world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                            kind="dispatch", state="queued", sandbox="read-only", workdir="/outside", prompt_path="/p")
        world.store.add_event("job.submitted", job_id="borrower", data={"depends": {
            "incomplete": [{"file": "/outside/.git/objects/info/alternates", "reason": "fixture error"}]}})
        def forbidden(*a, **k):
            raise AssertionError("filesystem discovery under transaction")
        with world.store.transaction():
            for name in ("record", "git_storage", "_alternates", "open_regular"):
                monkeypatch.setattr(dependencies, name, forbidden)
            monkeypatch.setattr(folders, "canonical", forbidden)
            reason = retention._pin_reasons(world.store, set(), None, root=world.root)["owner"]
            assert "borrower" in reason and "alternates" in reason
    finally:
        world.close()


def test_c8_4_disappearance_during_read_is_incomplete(tmp_path, monkeypatch):
    alternate = tmp_path / ".git" / "objects" / "info" / "alternates"
    alternate.parent.mkdir(parents=True)
    alternate.write_bytes(b"# first line\n")
    original = dependencies.open_regular

    class Vanished(io.BytesIO):
        def readline(self, size=-1):
            if self.tell():
                alternate.unlink()
                raise FileNotFoundError("fixture disappeared during read")
            return super().readline(size)

    monkeypatch.setattr(dependencies, "open_regular", lambda path, *a, **k:
                        Vanished(alternate.read_bytes()) if str(path) == str(alternate) else original(path, *a, **k))
    assert dependencies.record(str(tmp_path))["incomplete"][0]["file"] == str(alternate)
