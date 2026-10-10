"""PR watch regression and unchanged-state property for review 127."""
import json
from tests.unit.test_conversation_service import svc  # noqa: F401
from tests.unit.test_conversation_wakes import bound

import subprocess
import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from subfleet.conversations import wakes
from tests.unit.test_conversation_wakes import register, rows, settle_all


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(state=st.sampled_from(["OPEN", "MERGED", "CLOSED"]), checks=st.booleans(), cycles=st.integers(1, 10))
def test_property_unchanged_watched_pr_never_wakes(svc, monkeypatch, state, checks, cycles):
    cid = bound(svc)
    with svc.store.transaction() as tx:
        tx.execute("DELETE FROM wake_meta WHERE key='pr-polled'")
    clock = [1000.0]
    svc.wakes.now = lambda: clock[0]
    snapshot = {"state": state, "checks": [["COMPLETED", "tests", "SUCCESS", "1970-01-01T00:01:00Z", "head"]] if checks else [],
                "reviews": ["old-review"], "review_times": {"old-review": "1970-01-01T00:01:00Z"},
                "merged_at": "1970-01-01T00:01:00Z" if state == "MERGED" else None,
                "closed_at": "1970-01-01T00:01:00Z" if state == "CLOSED" else None}
    monkeypatch.setattr(wakes, "query_prs", lambda _: {"o/r#1": snapshot})
    for _ in range(cycles):
        register(svc, cid, prs=["o/r#1"])
        svc.wakes.tick()
        settle_all(svc, cid)
        clock[0] += 61
    assert rows(svc, cid) == []


@pytest.mark.parametrize("kind", ["checks", "review", "merge", "close"])
def test_first_pr_poll_detects_only_events_after_registration(svc, monkeypatch, kind):
    cid = bound(svc)
    svc.wakes.now = lambda: 1000
    register(svc, cid, prs=["o/r#1"])
    fresh = "1970-01-01T00:17:00Z"
    snapshot = {"state": "OPEN", "checks": [], "reviews": []}
    if kind == "checks":
        snapshot["checks"] = [["COMPLETED", "tests", "SUCCESS", fresh, "head"]]
    elif kind == "review":
        snapshot.update(reviews=["new"], review_times={"new": fresh})
    else:
        snapshot.update(state="MERGED" if kind == "merge" else "CLOSED")
        snapshot["merged_at" if kind == "merge" else "closed_at"] = fresh
    monkeypatch.setattr(wakes, "query_prs", lambda _: {"o/r#1": snapshot})
    svc.wakes.now = lambda: 1061
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1


@pytest.mark.parametrize("exit_code", [0, 1])
def test_partial_graphql_error_does_not_silence_other_conversations(svc, monkeypatch, exit_code):
    good, bad = bound(svc), bound(svc)
    svc.wakes.now = lambda: 1000
    register(svc, good, prs=["o/r#1"])
    register(svc, bad, prs=["o/r#999999"])
    with svc.store.transaction() as tx:
        tx.execute("UPDATE wake_requests SET observed_json=? WHERE conversation_id=?", (json.dumps({"o/r#1": {"state": "OPEN", "checks": [], "reviews": []}}), good))
    body = {"data": {"p0": {"pullRequest": {"state": "MERGED", "headRefOid": "head"}}, "p1": {"pullRequest": None}},
            "errors": [{"path": ["p1", "pullRequest"], "message": "Could not resolve PR"}]}
    monkeypatch.setattr(wakes.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], exit_code, json.dumps(body), "missing PR")))
    svc.wakes.tick()
    assert len(rows(svc, good)) == 1
    assert "PR state changed: o/r#1" in svc.store.message_text(svc.store.message(rows(svc, good)[0]["message_id"]))
    assert len(rows(svc, bad)) == 1
    assert "PR watch refused: o/r#999999" in svc.store.message_text(svc.store.message(rows(svc, bad)[0]["message_id"]))
    settle_all(svc, good)
    settle_all(svc, bad)
    svc.wakes.now = lambda: 1061
    svc.wakes.tick()
    assert len(rows(svc, good)) == len(rows(svc, bad)) == 1
