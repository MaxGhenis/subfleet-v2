"""C-8.4, C-26.12, IR-17: detached and turn jobs are retained under separate budgets;
turn jobs the conversation service still needs are pinned; a notice no session can read
pins nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from subfleet import retention
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store

LONG_AGO = "2026-01-01T00:00:00Z"


@pytest.fixture
def retained(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("claude-1", "claude", "claude:one", Credential("claude", "TOKEN", "env"),
                            None, LaneOwner.V2, False))
        yield store, tmp_path


def job(store, root, identity, *, kind="dispatch", created, finished=LONG_AGO, state="succeeded", size=10):
    fields = dict(job_id=identity, request_id=identity, payload_digest="digest", kind=kind, workdir=str(root),
                  prompt_path="/prompt", sandbox="read-only", state=state, created_at=created, finished_at=finished)
    if kind == "turn":
        fields.update(request_id=f"turn:{identity}:0", name="turn-cv-1", in_place=1, max_attempts=1)
    store.add_job(**fields)
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    (directory / "stdout").write_bytes(b"x" * size)


def stamp(minutes: int) -> str:
    return f"2026-02-01T00:{minutes:02d}:00Z"


def test_a_released_turns_notification_survives_retention_until_delivered(retained):
    """C-5.7: a crash between stores must not lose the outbox or its manifest."""
    store, root = retained
    job(store, root, "released", kind="turn", created=stamp(0), state="lost")
    manifest = root / "jobs" / "released" / "manifest.json"
    manifest.write_text('{"turn":{"conversation_id":"fixture","message_id":"fixture"}}')
    store.add_attempt(attempt_id="released/a1", job_id="released", seq=1, lane_id="claude-1",
                      model_requested="opus", state="lost", quarantine_notice_pending=1)
    def sweep():
        return retention.maintenance(store, root, max_jobs=0, turn_max_jobs=0, turn_max_bytes=0,
                                     turn_keep_s=0, holders=lambda *args, **kwargs: {})
    first = sweep()
    assert "released" in first["protected"] and "released" not in first["pruned"]
    assert first["pin_reasons"]["released"] == "quarantine-notice"
    assert store.get_attempt("released/a1")["quarantine_notice_pending"] == 1
    assert manifest.is_file() and not store.list_leases()
    store.update_attempt("released/a1", quarantine_notice_pending=0)
    second = sweep()
    assert second["pruned"] == ["released"] and store.get_job("released") is None


def test_each_pool_prunes_only_against_its_own_budget(retained):
    """C-8.4, C-26.12: a pool over budget prunes its own oldest jobs; the other pool,
    within its budget, loses nothing, however old its jobs are."""
    store, root = retained
    for n in range(3):
        job(store, root, f"detached-{n}", created=stamp(n))            # the oldest jobs overall
    for n in range(4):
        job(store, root, f"turn-{n}", kind="turn", created=stamp(10 + n))
    result = retention.maintenance(store, root, max_jobs=3, max_bytes=10**6, turn_max_jobs=2,
                                   turn_max_bytes=10**6, turn_keep_s=0)
    assert result["pruned"] == ["turn-0", "turn-1"]
    assert {j["job_id"] for j in store.list_jobs()} == {"detached-0", "detached-1", "detached-2", "turn-2", "turn-3"}
    assert result["pools"]["turn"]["jobs_after"] == 2 and result["pools"]["detached"]["jobs_after"] == 3

    for n in range(3, 5):
        job(store, root, f"detached-{n}", created=stamp(20 + n))
    result = retention.maintenance(store, root, max_jobs=3, max_bytes=10**6, turn_max_jobs=2,
                                   turn_max_bytes=10**6, turn_keep_s=0)
    assert result["pruned"] == ["detached-0", "detached-1"]
    assert store.get_job("turn-2") is not None and store.get_job("turn-3") is not None


def test_byte_budgets_are_separate_too(retained):
    """C-26.12: turn bytes count against the turn budget only."""
    store, root = retained
    job(store, root, "detached", created=stamp(1), size=100)
    job(store, root, "turn-old", kind="turn", created=stamp(2), size=100)
    job(store, root, "turn-new", kind="turn", created=stamp(3), size=100)
    result = retention.maintenance(store, root, max_jobs=10, max_bytes=100, turn_max_jobs=10,
                                   turn_max_bytes=150, turn_keep_s=0)
    assert result["pruned"] == ["turn-old"]
    assert result["bytes_after"] == 200


def test_a_turn_job_is_kept_for_days_after_it_ends(retained):
    """C-26.12: pinned for `turn_keep_days` after it ends, over budget or not; a job with
    no recorded end is not known to have ended long ago and is kept."""
    store, root = retained
    recent = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    job(store, root, "turn-ended-long-ago", kind="turn", created=stamp(1))
    job(store, root, "turn-ended-recently", kind="turn", created=stamp(2), finished=recent)
    job(store, root, "turn-no-end", kind="turn", created=stamp(3), finished=None)
    result = retention.maintenance(store, root, turn_max_jobs=0, turn_max_bytes=0, turn_keep_s=14 * 86400)
    assert result["pruned"] == ["turn-ended-long-ago"]
    assert {"turn-ended-recently", "turn-no-end"} <= set(result["protected"])


def test_the_conversation_services_pins_are_asked_again_inside_the_delete(retained):
    """IR-17: a turn job the service starts needing after selection (its message became
    live again, a runner adopted it) is still not removed."""
    store, root = retained
    job(store, root, "turn-a", kind="turn", created=stamp(1))
    job(store, root, "turn-b", kind="turn", created=stamp(2))
    calls = []

    def pins():
        calls.append(store.connection.in_transaction)
        return {"turn-b"} if len(calls) > 1 else set()

    result = retention.maintenance(store, root, turn_max_jobs=0, turn_max_bytes=0, turn_keep_s=0, pins=pins)
    assert result["pruned"] == ["turn-a"]
    assert "turn-b" in result["protected"] and store.get_job("turn-b") is not None
    assert (root / "jobs" / "turn-b").exists()
    assert True in calls                     # asked inside a delete transaction


def test_pins_keep_what_a_conversation_still_needs(retained):
    """C-26.12: whatever the service names is pinned, over any budget."""
    store, root = retained
    job(store, root, "turn-needed", kind="turn", created=stamp(1))
    job(store, root, "turn-spare", kind="turn", created=stamp(2))
    result = retention.maintenance(store, root, turn_max_jobs=0, turn_max_bytes=0, turn_keep_s=0,
                                   pins=lambda: {"turn-needed"})
    assert result["pruned"] == ["turn-spare"]
    assert store.get_job("turn-needed") is not None


@pytest.mark.parametrize("session", [None, ""])
def test_a_notice_with_no_session_never_pins(retained, session):
    """C-8.4, IR-17: a pending notice addressed to no session can never be read, so it
    does not keep its job; one addressed to a session still does."""
    store, root = retained
    job(store, root, "orphan-notice", created=stamp(1))
    job(store, root, "read-by-session", created=stamp(2))
    store.add_notice("orphan-notice", "unread", session)
    store.add_notice("read-by-session", "unread", "session-1")
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == ["orphan-notice"]
    assert "read-by-session" in result["protected"]
