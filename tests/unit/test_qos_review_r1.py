"""D-QOS-BOOKED1 review: exact operator values and every offline job reader."""

import pytest

from subfleet import guardian
from subfleet.offline import Offline
from subfleet.store import Store
from test_store_qos_migration import old_store


@pytest.mark.parametrize("value", ["Inherit", " inherit", "inherit ", "INHERIT"])
def test_operator_qos_requires_the_exact_inherit_value(monkeypatch, value):
    monkeypatch.setattr(guardian.os, "access", lambda *args: True)
    monkeypatch.setenv(guardian.PROVIDER_QOS_ENV, value)
    assert guardian.provider_qos("utility") == "utility"
    assert guardian.provider_qos("inherit") is None


@pytest.mark.parametrize("migrated", [False, True], ids=["schema9", "schema10-null"])
def test_offline_list_and_show_agree_on_old_jobs(tmp_path, migrated):
    path = old_store(tmp_path / "state.sqlite3")
    if migrated:
        with Store(path) as store:
            assert {job["provider_qos"] for job in store.list_jobs()} == {"utility"}
    offline = Offline(tmp_path)
    rows = offline.list_jobs(include_turns=True)
    assert {row["job_id"] for row in rows} == {"old-dispatch", "old-turn"}
    for row in rows:
        assert row["provider_qos"] == offline.show_job(row["job_id"])["provider_qos"] == "utility"


@pytest.mark.parametrize("reader", ["get_job", "get_job_by_request", "list_jobs"])
def test_read_only_store_defaults_a_missing_qos_column(tmp_path, reader):
    path = old_store(tmp_path / "state.sqlite3")
    with Store(path, read_only=True) as store:
        rows = store.list_jobs() if reader == "list_jobs" else [getattr(store, reader)("old-dispatch")]
        assert {row["provider_qos"] for row in rows} == {"utility"}
