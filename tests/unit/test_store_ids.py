"""Persistence and identifier contract checks."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import UUID

import pytest

from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from subfleet.ids import attempt_id, canonical_json, job_id, payload_digest, request_id
from subfleet.store import SCHEMA_VERSION, SchemaVersionError, Store


def lane():
    return Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/lane", "home"), "/home/lane", LaneOwner.V2, False)


def add_job(store, identity="job"):
    return store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch", workdir="/work", prompt_path="/prompt", sandbox="read-only")


def test_store_pragmas_schema_and_readonly(tmp_path):
    """C-3.1, C-3.4: SQLite is durable and an offline connection cannot mutate."""
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        assert store.one("PRAGMA journal_mode")["journal_mode"] == "wal"
        assert store.one("PRAGMA synchronous")["synchronous"] == 2
        assert store.one("PRAGMA foreign_keys")["foreign_keys"] == 1
        assert store.one("PRAGMA busy_timeout")["timeout"] == 5000
        assert store.one("SELECT version FROM schema_version")["version"] == SCHEMA_VERSION
        add_job(store)
        with Store(path, read_only=True) as reader:
            assert reader.get_job("job")["state"] == "queued"
            with pytest.raises(sqlite3.OperationalError, match="read-only"):
                reader.update_job("job", state="cancelled")
            with pytest.raises(sqlite3.OperationalError):
                reader.connection.execute("DELETE FROM jobs")


def test_store_newer_schema_refused(tmp_path):
    """C-3.5: the refusal identifies stored and supported schema versions."""
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        with store.transaction() as conn:
            conn.execute("UPDATE schema_version SET version=99")
    with pytest.raises(SchemaVersionError, match="99.*1"):
        Store(path)


def test_atomic_state_notice_event_and_rollback(tmp_path):
    """C-3.2, C-4.3, C-15.1: terminal state, notice, and audit commit or roll back together."""
    with Store(tmp_path / "state.sqlite3") as store:
        add_job(store)
        before = len(store.list_events())
        with pytest.raises(RuntimeError):
            with store.transaction("job.finished", job_id="job"):
                store.update_job("job", state="succeeded")
                store.add_notice("job", "done", "session")
                raise RuntimeError("crash before commit")
        assert store.get_job("job")["state"] == "queued"
        assert store.list_notices() == []
        assert len(store.list_events()) == before
        with store.transaction("job.finished", job_id="job"):
            store.update_job("job", state="succeeded")
            store.add_notice("job", "done", "session")
        assert store.get_job("job")["state"] == "succeeded"
        assert len(store.list_notices()) == 1
        assert store.list_events("job")[-1]["kind"] == "job.finished"


def test_concurrent_slot_lease_is_exclusive(tmp_path):
    """C-6.3: concurrent one-slot reservations have exactly one owner."""
    with Store(tmp_path / "state.sqlite3") as store:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda holder: store.acquire_lease("lane:codex-1:slot:1", holder), ["a", "b"]))
        assert sorted(results) == [False, True]
        assert len(store.list_leases()) == 1


def test_lane_and_attempt_bindings_immutable(tmp_path):
    """C-10.1, C-4.6: identity bindings cannot be replaced in place."""
    with Store(tmp_path / "state.sqlite3") as store:
        original = lane()
        store.put_lane(original)
        assert store.get_lane(original.lane_id) == original
        with pytest.raises(ValueError, match="immutable"):
            store.put_lane(Lane("codex-1", "codex", "codex:other", original.credential, original.home, original.owner, False))
        add_job(store)
        store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1", model_requested="gpt-6-astra")
        with pytest.raises(ValueError, match="immutable"):
            store.update_attempt("job/a1", lane_id="codex-2")


def test_closure_extends_without_shortening(tmp_path):
    """C-9.6: later observations extend a closure but cannot shorten its clock."""
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(lane())
        def closure(until):
            return Closure("codex-1", "account", until, ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "event")
        identity = store.put_closure(closure("2026-09-05T12:00:00Z"))
        assert store.put_closure(closure("2026-09-05T11:00:00Z")) == identity
        assert store.list_closures()[0]["until_at"] == "2026-09-05T12:00:00Z"
        store.put_closure(closure("2026-09-05T13:00:00Z"))
        assert store.list_closures()[0]["until_at"] == "2026-09-05T13:00:00Z"


def test_notice_ack_is_session_scoped(tmp_path):
    """C-15.3: another caller session cannot acknowledge a pending notice."""
    with Store(tmp_path / "state.sqlite3") as store:
        add_job(store)
        notice = store.add_notice("job", "finished", "owner")
        assert store.acknowledge_notices("other", [notice]) == 0
        assert store.acknowledge_notices("owner", [notice]) == 1
        assert store.list_notices(pending=True) == []


def test_identifier_formats_and_collision():
    """C-1.1, C-1.2, C-1.5: ids preserve v1 naming, unique slugs, and UUID4 requests."""
    now = datetime(2026, 9, 5, 10, 1, 2).astimezone()
    identity = job_id("BUILD -- résumé / " + "x" * 50, now=now)
    second = job_id("BUILD -- résumé / " + "x" * 50, now=now, existing={identity})
    assert identity.startswith("20260905-100102-build-r-sum-")
    assert second != identity
    assert len(second[16:]) <= 40
    assert attempt_id(identity, 1) == identity + "/a1"
    with pytest.raises(ValueError):
        attempt_id(identity, 0)
    assert UUID(request_id()).version == 4
    assert request_id("caller-id") == "caller-id"
    with pytest.raises(ValueError):
        request_id("x" * 129)


def test_payload_digest_canonical_and_payload_sensitive(tmp_path):
    """C-6.2: canonical payloads have stable SHA-256 digests sensitive to every pin."""
    kwargs = dict(workdir=tmp_path, workdir_head="head", sandbox="read-only", policy_hash="policy")
    first = payload_digest(b"hello\x00world", exclusions=["codex-2", "codex-1"], **kwargs)
    assert first == payload_digest(b"hello\x00world", exclusions=["codex-1", "codex-2"], **kwargs)
    assert first != payload_digest(b"hello\x00world", pinned_lane="codex-1", exclusions=["codex-1", "codex-2"], **kwargs)
    assert first != payload_digest(b"different", exclusions=["codex-1", "codex-2"], **kwargs)
    assert canonical_json({"b": 1, "a": "é"}) == b'{"a":"\xc3\xa9","b":1}'
    assert payload_digest({"b": 1, "a": 2}) == hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
