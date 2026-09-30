"""C-10.3 through the daemon's admission: the desktop login's lane, behind its reserve.

The scheduler's rules are tested in `tests/unit/test_desktop_reserve.py`; these
cases run them through `Daemon._admit`: the probe that takes a fresh reading before
detached work lands on the login, the bound checked again inside the reservation,
and what `why` says. Claude Code is active on the desktop login in every case
(`claude_code_active`), which since 2026-09-30 refuses nothing.
"""

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.contracts import (DESKTOP_EXCLUSION, ClockSource, Closure, ClosureReason, Credential, Lane,
                                LaneOwner, Outcome, OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import Daemon, after, utcnow
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter

DESK_EMAIL = "max@thesisinstitute.org"


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """codex-1 (the harness's) closed for astra, and one Claude lane, claude-9, the desktop login."""
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: DESK_EMAIL)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    claude_code_active(monkeypatch, tmp_path / "claude")
    service = Daemon(root)
    service.store.put_lane(Lane("claude-9", "claude", f"claude:{DESK_EMAIL}",
                                Credential("claude", "claude-quota-desk", "keychain-token"), None,
                                LaneOwner.V2, False, label=DESK_EMAIL))
    service.store.add_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    try:
        yield service, harness
    finally:
        service.close()


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(pinned_model="opus", **changes))["job_id"]


def lane_of(service, job_id):
    return [row["lane_id"] for row in service.store.list_attempts(job_id)]


def fresh(service, window, utilization):
    service.store.add_reading(Reading("claude-9", "account", window, utilization, after(3600),
                                      ReadingLabel.PROVIDER, "rate_limit_event", utcnow()))


def fresh_windows(service, utilization=.2):
    for window in ("five_hour", "seven_day"):
        fresh(service, window, utilization)


def test_c10_3_c11_4_the_first_job_on_the_login_is_probed_and_the_probes_reading_decides(desk, monkeypatch):
    """C-10.3, C-11.4: no fresh reading, so a read-only standard job is probed first on the desktop lane;
    the probe's reading (0.2) admits it. With a probe reading at 0.8 the next job is refused, not placed."""
    service, harness = desk
    readings = [.2]
    probes = []

    def probe(job, lane, model, holder):
        probes.append(lane.lane_id)
        value = readings[0]
        return Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None}, readings=tuple(
            Reading(lane.lane_id, "account", window, value, after(3600), ReadingLabel.PROVIDER,
                    "rate_limit_event", utcnow()) for window in ("five_hour", "seven_day")))
    monkeypatch.setattr(service, "_execute_probe", probe)
    first = submit(service, harness)
    service._admit()
    assert probes == ["claude-9"] and lane_of(service, first) == ["claude-9"]
    service.store.query("SELECT 1")
    with service.store.transaction("fixture.age") as tx:            # the probe's reading is no longer fresh
        tx.execute("UPDATE readings SET observed_at=? WHERE lane_id='claude-9'", ("2026-09-30T00:00:00Z",))
    readings[0] = .8
    second = submit(service, harness)
    service._admit()
    assert probes == ["claude-9", "claude-9"] and not service.store.list_attempts(second)
    assert "desktop-reserve:five_hour" in service.dispatch("why", {"job_id": second})["text"]


def test_c10_3_with_fresh_readings_no_probe_is_run_and_the_bound_holds_the_third(desk, monkeypatch):
    """C-10.3: fresh readings below the ceiling need no probe; two detached attempts run on the login and the
    third waits `desktop-reserve:in-flight`, ordinary queueing (C-6.11)."""
    service, harness = desk
    monkeypatch.setattr(service, "_execute_probe", lambda *args: pytest.fail("no probe was needed"))
    fresh_windows(service)
    jobs = [submit(service, harness) for _ in range(3)]
    service._admit()
    assert [lane_of(service, job) for job in jobs] == [["claude-9"], ["claude-9"], []]
    assert service._holds[jobs[2]]["reason"] == "desktop-reserve:in-flight"
    assert "desktop-reserve:in-flight" in daemon_module.EXPECTED_HOLDS


def test_c10_3_c6_3_the_reservation_sees_the_bound_reached_since_the_evaluation(desk, monkeypatch):
    """C-6.3, C-10.3: the job was evaluated with one attempt on the login; another reached it before the
    reservation, so the check inside judges the lane again and the job is not placed past the bound."""
    service, harness = desk
    fresh_windows(service)
    first = submit(service, harness)
    service._admit()
    assert lane_of(service, first) == ["claude-9"]
    pick = service._pick

    def then_another(job, **options):
        decision = pick(job, **options)
        service.store.add_attempt(attempt_id="elsewhere/a1", job_id=first, seq=9, lane_id="claude-9",
                                  model_requested="claude-opus-5-5", state="running")
        return decision
    monkeypatch.setattr(service, "_pick", then_another)
    second = submit(service, harness)
    service._admit()
    assert not service.store.list_attempts(second)


def test_c10_3_a_no_desktop_job_waits_while_a_default_one_runs_there(desk, monkeypatch):
    """C-10.3: `--no-desktop` keeps its job off the login whatever else is open or closed."""
    service, harness = desk
    fresh_windows(service)
    kept_off = submit(service, harness, exclusions=[DESKTOP_EXCLUSION])
    default = submit(service, harness)
    service._admit()
    assert not service.store.list_attempts(kept_off) and lane_of(service, default) == ["claude-9"]
    assert "claude-9: excluded" in service.dispatch("why", {"job_id": kept_off})["text"]
