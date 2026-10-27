"""Review regressions through real submit, admission and export, offline."""
from pathlib import Path
import errno
import os
import tempfile

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import folders, resource_leases
from subfleet.conversations.store import ConversationError, canonical_native
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_canonical_identity import (
    MINIMAL, accept_for_export, configure as configure_base, require_case_aliases, revive,
)


def configure(service, patch):
    configure_base(service, patch)
    patch.setattr(service, "_desktop_in_use", lambda: False)
    patch.setattr(service, "_record_desktop_use", lambda: None)


@pytest.mark.parametrize("failure", ["permission", "loop"])
def test_unreachable_stored_output_does_not_stop_submit_admit_or_export(tmp_path, failure):
    directory = tmp_path / "inaccessible"
    directory.mkdir()
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        first = submit(service, harness, out_path=str(directory / "x.md"), caller_session=None)
        service._admit()
        accept_for_export(service, first, b"first\n")
        if failure == "permission":
            directory.chmod(0)
        else:
            directory.rmdir()
            directory.symlink_to(directory)
        try:
            second = submit(service, harness, out_path=str(tmp_path / "healthy.md"), caller_session=None)
            service._admit()
            assert service.store.list_attempts(second)
            accept_for_export(service, second, b"second\n")
            service._export(second)
            service._export(first)
            assert (tmp_path / "healthy.md").read_bytes() == b"second\n"
            assert service.store.get_job(first)["export_error"]
            assert not service.store.list_leases(first)
        finally:
            if failure == "permission":
                directory.chmod(0o700)
            else:
                directory.unlink()


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "fixture"),
                                   OSError(errno.EIO, "fixture"), TimeoutError("fixture")])
def test_identity_failure_falls_back_to_exact_string_through_all_operations(tmp_path, error, caplog):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        def broken(_path):
            raise error
        patch.setattr(folders, "_case_sensitive", broken)
        path = str(tmp_path / "Result.md")
        try:
            answer = folders.identity(path)
        except OSError as exc:
            pytest.fail(f"identity raised {type(exc).__name__} instead of falling back")
        assert answer == path
        first = submit(service, harness, out_path=path, caller_session=None)
        from subfleet.adapters.base import AdapterError
        with pytest.raises(AdapterError, match="out_path"):
            submit(service, harness, out_path=path, caller_session=None)
        second = submit(service, harness, out_path=str(tmp_path / "other.md"), caller_session=None)
        service._admit()
        for job in (first, second):
            assert service.store.list_attempts(job)
            accept_for_export(service, job, b"done\n")
            service._export(job)
            assert not service.store.list_leases(job)
        warnings = [r for r in caplog.records if path in r.getMessage()]
        assert len(warnings) == 1


def legacy_pair(service, harness, directory):
    left, right = directory / "Result.md", directory / "result.md"
    first = submit(service, harness, out_path=str(left), caller_session=None)
    second = submit(service, harness, out_path=str(directory / "different.md"), caller_session=None)
    service._admit()
    service.store.update_job(second, out_path=str(right))
    service.store.release_leases(second, prefix="out:")
    service.store.release_leases(first, prefix="out:")
    for key, job, stamp in ((left, first, "2000-01-01T00:00:00Z"),
                            (right, second, "2000-01-02T00:00:00Z")):
        service.store.acquire_lease(f"out:{key}", job)
        with service.store.transaction("fixture.age") as tx:
            tx.execute("UPDATE leases SET acquired_at=? WHERE holder=? AND lease_key LIKE 'out:%'", (stamp, job))
    return first, second


@pytest.mark.parametrize("newer_first", [True, False])
def test_oldest_legacy_alias_exports_and_superseded_releases_all_leases(tmp_path, newer_first):
    require_case_aliases(tmp_path)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        first, second = legacy_pair(service, harness, tmp_path)
        # A superseded output must not retain a conversation or attempt slot.
        service.store.acquire_lease(f"native:claude:{MINIMAL}", second)
        accept_for_export(service, first, b"oldest\n")
        accept_for_export(service, second, b"newest\n")
        for job in ((second, first) if newer_first else (first, second)):
            if job == second:
                service.store.add_notice(second, "finished", "fixture")
            service._export(job)
            assert not service.store.list_leases(job)
            assert not service.store.list_leases(service.store.list_attempts(job)[0]["attempt_id"])
        assert (tmp_path / "Result.md").read_bytes() == b"oldest\n"
        assert service.store.get_job(second)["export_error"] == f"superseded by {first}"
        assert f"superseded by {first}" in service.store.one("SELECT text FROM notices WHERE job_id=?", (second,))["text"]
        service._export(second)
        assert service.store.one("SELECT text FROM notices WHERE job_id=?", (second,))["text"].count(f"superseded by {first}") == 1
        assert not service.store.list_leases(second)
        third = submit(service, harness, out_path=str(tmp_path / "result.md"), caller_session=None)
        service._admit()
        assert service.store.list_attempts(third)


def test_public_open_between_reservation_and_launch_refuses_live_native_lease(tmp_path):
    from subfleet.conversations import catalog
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = revive(service, harness, MINIMAL)
        patch.setattr(catalog, "native_session", lambda *_args, **_kw: {
            "continuable": True, "model_value": "haiku", "permission": "read-only",
            "cwd": str(harness.workdir), "title": "Native", "lane_id": None})
        refused = []
        def launch(attempt):
            with pytest.raises(ConversationError, match="live job"):
                service.conversations.handle("conversation.open", {
                    "native": {"provider": "claude", "session_id": MINIMAL.upper()}}, None)
            refused.append(attempt)
        patch.setattr(service, "_launch", launch)
        service._admit()
        service._process_attempt(service.store.list_attempts(job)[0]["attempt_id"])
        assert refused and service.store.list_attempts(job)
        assert service.conversations.store.by_native("claude", MINIMAL) is None


def test_public_open_refuses_a_quarantined_native_owner_even_after_job_is_lost(tmp_path):
    from subfleet.conversations import catalog
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = revive(service, harness, MINIMAL)
        service._admit()
        attempt = service.store.list_attempts(job)[0]
        service.store.update_job(job, state="lost")
        service.store.update_attempt(attempt["attempt_id"], state="quarantined")
        patch.setattr(catalog, "native_session", lambda *_args, **_kw: {
            "continuable": True, "model_value": "haiku", "permission": "read-only",
            "cwd": str(harness.workdir), "title": "Native", "lane_id": None})
        with pytest.raises(ConversationError, match="live job"):
            service.conversations.handle("conversation.open", {
                "native": {"provider": "claude", "session_id": MINIMAL}}, None)
        assert service.store.list_leases(job)
        service.store.release_leases(job)
        opened = service.conversations.handle("conversation.open", {
            "native": {"provider": "claude", "session_id": MINIMAL}}, None)
        assert opened["conversation"]["native_session_id"] == MINIMAL


@pytest.mark.parametrize("alias", ["{" + MINIMAL.upper() + "}", "urn:uuid:" + MINIMAL, MINIMAL.replace("-", "")])
def test_uuid_parseable_aliases_collide_through_submit_and_admit(tmp_path, alias):
    from subfleet.adapters.base import AdapterError
    assert canonical_native(alias) == MINIMAL
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        first = revive(service, harness, alias)
        with pytest.raises(AdapterError, match="live revive"):
            revive(service, harness, MINIMAL, "claude-2")
        service._admit()
        assert service.store.list_attempts(first)
        assert resource_leases.native_key("claude", MINIMAL) in {r["lease_key"] for r in service.store.list_leases(first)}


def test_identity_work_is_linear_per_admission_pass_and_fresh_next_pass(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        jobs = [submit(service, harness, out_path=str(tmp_path / f"result-{i}.md"), caller_session=None)
                for i in range(12)]
        identity, calls = folders.identity, []
        def counted(path):
            calls.append(str(path))
            return identity(path)
        patch.setattr(folders, "identity", counted)
        service._admit()
        assert len(calls) <= 3 * len(jobs), len(calls)
        assert len(calls) == len(set(calls))
        # Retrying after filesystem change must establish identities again.
        for job in jobs:
            for attempt in service.store.list_attempts(job):
                service.store.update_attempt(attempt["attempt_id"], state="failed")
                service.store.release_leases(attempt["attempt_id"])
            service.store.update_job(job, state="queued")
        calls.clear()
        service._admit()
        assert calls and len(calls) == len(set(calls))


@pytest.mark.parametrize("missing", ["accepted-attempt", "deliverable", "output-lease"])
def test_export_failure_records_notice_and_releases_leases(tmp_path, missing):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        job = submit(service, harness, out_path=str(tmp_path / "result.md"), caller_session=None)
        service._admit()
        attempt = accept_for_export(service, job, b"done\n")
        service.store.add_notice(job, "finished", "fixture")
        if missing == "accepted-attempt":
            service.store.update_job(job, accepted_attempt_id=None)
        elif missing == "deliverable":
            with service.store.transaction("fixture.missing") as tx:
                tx.execute("DELETE FROM artifacts WHERE role='deliverable'")
        else:
            service.store.release_leases(job, prefix="out:")
        service._export(job)
        error = service.store.get_job(job)["export_error"]
        assert error
        assert error in service.store.one("SELECT text FROM notices WHERE job_id=?", (job,))["text"]
        assert not service.store.list_leases(job)
        assert not service.store.list_leases(attempt["attempt_id"])


@settings(max_examples=16, deadline=None, derandomize=True, database=None)
@given(pair=st.sampled_from([("Result", "result"), ("café", "cafe\u0301"), ("ß", "ss"),
                            ("ﬁ", "fi"), ("ς", "σ"), ("İ", "i\u0307"), ("ı", "I"),
                            ("re\u200Bsult", "result"), ("ᾼ\u0342", "α\u0345\u0342"),
                            ("ΐ", "Ϊ\u0301")]), parent=st.booleans(), newer_first=st.booleans())
def test_one_owner_per_real_object_and_every_finished_output_settles(pair, parent, newer_first):
    from subfleet.adapters.base import AdapterError
    with tempfile.TemporaryDirectory(prefix="identity-invariants-") as directory:
        base = Path(directory)
        left, right = (base / name for name in pair)
        if parent:
            left.mkdir()
            if not right.exists():
                right.mkdir()
            left, right = left / "result.md", right / "result.md"
        left.write_bytes(b"x")
        if not right.exists():
            right.write_bytes(b"y")
        same = os.path.samefile(left, right)
        # Submit when the filenames are absent, the domain kernel spelling
        # alone cannot cover. The real-file comparison is an independent oracle.
        left.unlink()
        if right.exists():
            right.unlink()
        with fleet_daemon(base / "state") as (service, harness, patch):
            configure(service, patch)
            jobs = [submit(service, harness, out_path=str(left), caller_session=None)]
            try:
                jobs.append(submit(service, harness, out_path=str(right), caller_session=None))
            except AdapterError as exc:
                assert exc.code == 7
            service._admit()
            owners = [job for job in jobs if service.store.list_attempts(job)]
            assert not same or len(owners) <= 1
            assert owners
            for job in (list(reversed(owners)) if newer_first else owners):
                attempt = accept_for_export(service, job, b"finished\n")
                service.store.add_notice(job, "finished", "fixture")
                service._export(job)
                row = service.store.get_job(job)
                exported = service.store.one("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                             (attempt["attempt_id"],))
                assert exported or row["export_error"]
                assert not service.store.list_leases(job)
                assert not service.store.list_leases(attempt["attempt_id"])
                assert folders.identity(str(left)) == folders.identity(str(left))


@pytest.mark.parametrize("existing", [False, True], ids=["new-binding", "existing-binding"])
@pytest.mark.parametrize("transactional", [False, True], ids=["read-interface", "writer-transaction"])
def test_native_open_keeps_ownership_checks_through_the_job_store_read_interface(tmp_path, existing, transactional):
    """The read interface never bypasses a lease; the daemon also holds its writer lock."""
    from types import SimpleNamespace
    from subfleet.conversations import catalog
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        configure(service, patch)
        jobs = service.store
        patch.setattr(catalog, "native_session", lambda *_args, **_kw: {
            "continuable": True, "model_value": "haiku", "permission": "read-only",
            "cwd": str(harness.workdir), "title": "Native", "lane_id": None})
        native = {"provider": "claude", "session_id": MINIMAL.upper()}
        if existing:
            bound, _ = service.conversations.store.create_conversation(
                provider="claude", workspace=str(harness.workdir), workspace_kind="in-place",
                settings={"model": "haiku", "effort": None, "fast": False,
                          "permission": "read-only", "auto_continue": True},
                origin="native", native_session_id=MINIMAL)
        job = submit(service, harness)
        jobs.acquire_lease(f"native:claude:{MINIMAL.upper()}", job)
        reads = []
        def query(sql, params=()):
            reads.append(jobs._holds_writer())
            return jobs.query(sql, params)
        def one(sql, params=()):
            reads.append(jobs._holds_writer())
            return jobs.one(sql, params)
        adapter = SimpleNamespace(query=query, one=one, lane_rows=jobs.lane_rows)
        if transactional:
            adapter.transaction = jobs.transaction
        # Both adapters run the real ownership SQL. The transactional adapter
        # must read on the writer connection for the complete guarded open.
        create = service.conversations.store.create_conversation
        def guarded_create(**kwargs):
            assert jobs._holds_writer() == transactional
            return create(**kwargs)
        with patch.context() as opened_patch:
            opened_patch.setattr(service, "store", adapter)
            opened_patch.setattr(service.conversations.store, "create_conversation", guarded_create)
            if existing:
                opened = service.conversations._open_native(native)
                assert opened["conversation_id"] == bound["conversation_id"]
                assert not reads
                return
            with pytest.raises(ConversationError, match="live job") as refused:
                service.conversations._open_native(native)
            assert refused.value.code == 7
            assert reads and all(held == transactional for held in reads)
            assert bool(service.conversations.store.by_native("claude", MINIMAL)) == existing
            jobs.release_leases(job)
            opened = service.conversations._open_native(native)
            assert opened["native_session_id"] == MINIMAL
            if existing:
                assert opened["conversation_id"] == bound["conversation_id"]
