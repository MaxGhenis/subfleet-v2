"""C-3.8: an admission pass's event is named for what it did, and a repeated wait records nothing.

Incident (D-RESERVED-NOISE1, 2026-11-11): the newest 20,000 `attempt.reserved`
events in the live store spanned 02:05:33Z-05:00:34Z, every one with a NULL
`attempt_id` and data `{}`, across 111 jobs, while no job ran (230 waiting,
0 running): about 6,900 events an hour into a 9.84 GB store. Every pass that
left a job waiting recorded one, because admission's transaction opened as
`attempt.reserved` and its wait's UPDATE counts in `total_changes` even when it
writes the values the row already holds.
"""

import pytest

from subfleet.adapters.registry import register
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter


@pytest.fixture
def service(tmp_path, monkeypatch, process_inspection_available):
    """A real daemon core on a temp state root with one measured Codex lane; passes are driven by hand."""
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root, desktop_prober=lambda: None)
    monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("nothing here may launch a guardian"))
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                     ReadingLabel.PROVIDER, "fixture", utcnow()))
    try:
        yield daemon, harness
    finally:
        daemon.close()


def events(daemon, job_id):
    return [(row["kind"], row["attempt_id"], row["lane_id"]) for row in daemon.store.list_events(job_id)]


def due(daemon):
    with daemon.store.transaction("test.due") as tx:
        tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting'", (after(-1),))


def close_lane(daemon):
    daemon.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "fixture"))


def test_c3_8_repro_passes_with_no_lane_record_no_empty_reservation(service):
    """C-3.8 repro (D-RESERVED-NOISE1): several looks at a job no lane will take record no
    `attempt.reserved` event without an attempt id."""
    daemon, harness = service
    close_lane(daemon)
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    for _ in range(5):
        daemon._admit()
        due(daemon)
    assert daemon.store.list_attempts(job) == []
    assert daemon.store.get_job(job)["state"] == "waiting"
    empty = [row for row in events(daemon, job) if row[0] == "attempt.reserved" and row[1] is None]
    assert empty == [], f"{len(empty)} empty reservations over 5 looks"
