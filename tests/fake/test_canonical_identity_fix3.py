"""Round-three lease regressions through the public operations, offline."""
from pathlib import Path
import errno
import time

import pytest

from subfleet import folders
from subfleet.adapters.base import AdapterError
from subfleet.conversations.store import ConversationError
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_canonical_identity import MINIMAL, SETTINGS, accept_for_export, revive
from tests.fake.test_canonical_identity_fix2 import configure


def seed_outputs(service, harness, base, count, state="queued"):
    first = submit(service, harness, out_path=str(base / "Result-0.md"), caller_session=None)
    template = service.store.get_job(first)
    columns = tuple(template)
    manifest = (service.root / "jobs" / first / "manifest.json").read_bytes()
    jobs = [first]
    with service.store.transaction("fixture.seed") as tx:
        for i in range(1, count):
            row = dict(template, job_id=f"fixture-{i}", request_id=f"fixture-{i}",
                       state=state, out_path=str(base / f"Other-{i}.md"))
            tx.execute(f"INSERT INTO jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       tuple(row[c] for c in columns))
            directory = service.root / "jobs" / row["job_id"]
            directory.mkdir()
            (directory / "manifest.json").write_bytes(manifest)
            jobs.append(row["job_id"])
    return jobs


@pytest.mark.parametrize("live", [10, 40, 160])
def test_admission_and_export_do_not_resolve_unleased_other_outputs(tmp_path, live):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        jobs = seed_outputs(service, harness, tmp_path, live, state="running")
        target = jobs[0]
        path = service.store.get_job(target)["out_path"]
        real, calls = folders.identity, []
        def counted(value):
            calls.append(value)
            assert value in (path, real(path)), "resolved another live job's output"
            return real(value)
        patch.setattr(folders, "identity", counted)
        service._admit()
        assert service.store.list_attempts(target)
        assert len(calls) == 1
        accept_for_export(service, target, b"done\n")
        calls.clear()
        service._export(target)
        assert len(calls) <= 2
        assert Path(path).read_bytes() == b"done\n"
        assert not service.store.list_leases(target)


@pytest.mark.parametrize("live", [10, 40, 160])
def test_identity_calls_stay_linear_without_admission_job_census(tmp_path, live):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        jobs = seed_outputs(service, harness, tmp_path, live)
        real, calls = folders.identity, []
        query = service.store.query
        def without_census(sql, params=()):
            assert "SELECT job_id,out_path FROM jobs" not in sql
            return query(sql, params)
        def counted(path):
            calls.append(path)
            return real(path)
        patch.setattr(service.store, "query", without_census)
        patch.setattr(folders, "identity", counted)
        service._admit()
        assert len(service.store.list_attempts()) == len(jobs)
        assert len(calls) == len(set(calls))
        assert len(calls) <= 2 * live, len(calls)
        print(f"live={live}: admission identity calls={len(calls)}")


@pytest.mark.parametrize("failure", ["identity-only", "permission", "loop"])
def test_export_uses_own_canonical_lease_when_identity_fails(tmp_path, failure):
    directory = tmp_path / "Output"
    directory.mkdir()
    path = directory / "Result.md"
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = submit(service, harness, out_path=str(path), caller_session=None)
        service._admit()
        accept_for_export(service, job, b"done\n")
        if failure == "identity-only":
            def broken(_):
                raise OSError(errno.EIO, "fixture")
            patch.setattr(folders, "_case_sensitive", broken)
        elif failure == "permission":
            directory.chmod(0)
        else:
            directory.rmdir()
            directory.symlink_to(directory)
        try:
            service._export(job)
            error = service.store.get_job(job)["export_error"]
            if failure == "identity-only":
                assert not error
                assert path.read_bytes() == b"done\n"
            else:
                assert "output lease not held" not in error
                expected = errno.EACCES if failure == "permission" else errno.ELOOP
                assert f"errno={expected}" in error
            assert not service.store.list_leases(job)
        finally:
            if failure == "permission":
                directory.chmod(0o700)
            elif failure == "loop":
                directory.unlink()


def test_exact_duplicate_refused_when_quarantine_holds_canonical_key_and_identity_fails(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        path = str(tmp_path / "Result.md")
        job = submit(service, harness, out_path=path, caller_session=None)
        service._admit()
        attempt = service.store.list_attempts(job)[0]
        service.store.update_attempt(attempt["attempt_id"], state="quarantined")
        service.store.update_job(job, state="lost")
        def broken(_):
            raise OSError(errno.EIO, "fixture")
        patch.setattr(folders, "_case_sensitive", broken)
        with pytest.raises(AdapterError) as refused:
            submit(service, harness, out_path=path, caller_session=None)
        assert refused.value.code == 7
        assert len(service.store.list_jobs()) == 1


@pytest.mark.parametrize("lost", [False, True])
def test_public_open_refuses_native_lease_held_by_attempt_id(tmp_path, lost):
    from subfleet.conversations import catalog
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = revive(service, harness, MINIMAL)
        service._admit()
        attempt = service.store.list_attempts(job)[0]
        key = f"native:claude:{MINIMAL}"
        service.store.release_leases(job, prefix="native:")
        service.store.acquire_lease(key, attempt["attempt_id"])
        if lost:
            service.store.update_attempt(attempt["attempt_id"], state="quarantined")
            service.store.update_job(job, state="lost")
        patch.setattr(catalog, "native_session", lambda *_a, **_k: {
            "continuable": True, "model_value": "haiku", "permission": "read-only",
            "cwd": str(harness.workdir), "title": "Native", "lane_id": None})
        with pytest.raises(ConversationError) as refused:
            service.conversations.handle("conversation.open", {
                "native": {"provider": "claude", "session_id": MINIMAL.upper()}}, None)
        assert refused.value.code == 7
        assert service.conversations.store.by_native("claude", MINIMAL) is None
        service.store.release_leases(attempt["attempt_id"], prefix="native:")
        opened = service.conversations._open_native({"provider": "claude", "session_id": MINIMAL})
        assert opened["native_session_id"] == MINIMAL


def test_reopening_bound_native_returns_conversation_during_its_own_turn(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        conversation, _ = service.conversations.store.create_conversation(
            provider="claude", workspace=str(harness.workdir), workspace_kind="in-place",
            settings=SETTINGS, origin="native", native_session_id=MINIMAL)
        job = submit(service, harness)
        service.store.acquire_lease(f"native:claude:{MINIMAL}", job)
        patch.setattr(service.conversations, "_view_live", service.conversations._view)
        opened = service.conversations.handle("conversation.open", {
            "native": {"provider": "claude", "session_id": MINIMAL.upper()}}, None)
        assert opened["conversation"]["conversation_id"] == conversation["conversation_id"]


def test_submit_refuses_output_symlink_loop_with_exit_7_and_fix(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        with pytest.raises(AdapterError) as refused:
            submit(service, harness, out_path=str(loop / "result.md"), caller_session=None)
        assert refused.value.code == 7
        assert "symlink loop" in refused.value.fix
        assert not service.store.list_jobs()


@pytest.mark.xfail(strict=True, reason="P3-6: filesystem identity has no wall-clock bound for a hung mount")
def test_hung_mount_identity_work_has_a_real_deadline(tmp_path, monkeypatch):
    """Finite stand-in for a blocked syscall; never leaves a worker behind."""
    real = folders._case_sensitive
    def slow(path):
        time.sleep(0.05)
        return real(path)
    monkeypatch.setattr(folders, "_case_sensitive", slow)
    started = time.monotonic()
    folders.identity(str(tmp_path / "Result.md"))
    assert time.monotonic() - started < 0.025
