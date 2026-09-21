"""C-6.11 and C-5.10: a stalled queue says so, and a failing worker does not spin.

Incident, 2026-09-20: fifteen jobs sat queued for hours beside open lanes and
nothing said why. `subfleet why <queued job>` printed `null` (a job admission
skipped has no decision row, and the CLI printed the missing value rather than
the daemon's text). `subfleet status` said `running jobs: 519`, the size of the
whole store. `daemon.log` had not gained a line since the daemon started.
"""

import logging
import time

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol, render
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)


@pytest.fixture
def fleet(routing_state):  # noqa: F811
    service, harness = routing_state
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    return service, harness


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def log_lines(service):
    service._log_handler.flush()
    return [line for line in (service.root / "daemon.log").read_text().splitlines() if line.startswith("admission:")]


# --- why (C-6.11) -------------------------------------------------------------------------------

def test_c6_11_the_incident_why_names_the_job_a_queued_job_is_held_behind(fleet):
    """C-6.11 a job admission skipped has no decision row and is answered anyway."""
    service, harness = fleet
    older = submit(service, harness, pinned_model="astra")
    held = submit(service, harness, pinned_model="astra")
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(30))
    service._admit()
    assert not service.store.list_decisions(held)                    # the precondition of the `null`
    answer = service.dispatch("why", {"job_id": held})
    assert answer["decision"] is not None and answer["decision_source"] == "evaluated-now"
    assert answer["job"]["hold"] == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
    assert older in answer["text"] and "C-6.9" in answer["text"] and "null" not in answer["text"]
    assert not service.store.list_decisions(held)                    # answering is not admission: nothing is recorded


def test_c6_11_why_for_a_job_nothing_admits_names_the_reason_and_the_rechecks(fleet):
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    for _ in range(3):
        service._admit()
        service.store.update_job(stuck, next_check_at=utcnow())
    answer = service.dispatch("why", {"job_id": stuck})
    assert answer["decision_source"] == "recorded"
    assert answer["job"]["recheck"]["rechecks"] == 2 and "signature" not in answer["job"]["recheck"]
    assert any(line.startswith("Rechecks: same verdict 3 times") for line in answer["queue"])
    assert any(line.startswith("Held: no lane admits it") for line in answer["queue"])


def test_c6_11_why_before_any_pass_and_after_the_job_ended(fleet):
    service, harness = fleet
    fresh = submit(service, harness, pinned_model="astra")
    answer = service.dispatch("why", {"job_id": fresh})
    assert answer["decision"]["chosen_lane"] == "codex-1"
    assert "no admission pass has reached this job yet" in answer["text"]
    service.kill(protocol.KillArgs(fresh))
    ended = service.dispatch("why", {"job_id": fresh})
    assert ended["decision"] is None and ended["job"]["hold"] is None
    assert ended["text"].splitlines() == [f"Job: {fresh} is cancelled", "No decision recorded."]


def test_c6_11_the_full_fleet_is_named(fleet):
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    third = submit(service, harness, pinned_model="astra")
    service._admit()
    assert service._holds[second]["reason"] == "fleet-full"          # evaluated: every lane `no-slot`, the cap the cause
    assert service._holds[third] == {"reason": "fleet-full", "max_active_attempts": 1}     # not evaluated: the pass ended
    assert "max_active_attempts (1)" in service.dispatch("why", {"job_id": third})["text"]
    assert "max_active_attempts" in service.dispatch("why", {"job_id": second})["text"]


@pytest.mark.parametrize("hold,expected", [
    ({"reason": "lease-held", "leases": ["out:/r.md"]}, "a lease this job needs is held by another job: out:/r.md"),
    ({"reason": "probe-pending"}, "its lane is being probed"),
    ({"reason": "reserve:fable:unmeasured"}, "no lane admits it (reserve:fable:unmeasured)"),
    ({"reason": "behind-older-job"}, "held behind ?"),                # a hold missing its fields still renders
    (None, "no admission pass has reached this job yet"),
])
def test_c6_11_every_hold_renders_as_a_sentence(hold, expected):
    lines = render.why_queue({"job_id": "j", "state": "queued", "hold": hold})
    assert expected in "\n".join(lines)


# --- daemon.log and status (C-6.11) -------------------------------------------------------------

def test_c6_11_the_log_says_when_jobs_are_pending_and_nothing_is_placed(fleet):
    """C-6.11 one line after a minute, not one a pass; the open lanes and the reasons on it."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    behind = submit(service, harness, pinned_model="opus")
    service._admit()
    assert log_lines(service) == []                                  # under a minute: nothing yet
    service._admission["idle_since"] = time.monotonic() - 61
    for _ in range(50):
        service._admit()
    lines = log_lines(service)
    assert len(lines) == 1
    assert "2 jobs pending, none placed for 61 s" in lines[0]
    assert "1 lanes open (codex-1)" in lines[0]
    assert "behind-older-job x1" in lines[0] and "no-lanes x1" in lines[0] and f"oldest {stuck}" in lines[0]
    service._admission["logged_at"] = time.monotonic() - daemon_module.ADMISSION_IDLE_REPEAT_S - 1
    service._admit()
    assert len(log_lines(service)) == 2                              # and again every ten minutes while it lasts
    for job_id in (stuck, behind):
        service.kill(protocol.KillArgs(job_id))
    service._admit()
    assert log_lines(service)[-1].startswith("admission: nothing left pending after")
    service._admit()
    assert len(log_lines(service)) == 3


def test_c6_11_open_lanes_make_it_a_warning_and_none_make_it_information(fleet):
    service, harness = fleet
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    submit(service, harness, pinned_model="opus")
    service._admit()
    service._admission["idle_since"] = time.monotonic() - 61
    service._admit()
    with service.store.transaction("fixture.disable") as tx:
        tx.execute("UPDATE lanes SET enabled=0")
    service._admission["logged_at"] = time.monotonic() - daemon_module.ADMISSION_IDLE_REPEAT_S - 1
    service._admit()
    assert [record.levelno for record in seen] == [logging.WARNING, logging.INFO]
    assert "0 lanes open (-)" in seen[1].getMessage()


def test_c6_11_a_fleet_at_its_cap_is_ordinary_queueing_not_a_warning(fleet):
    """C-6.11 four jobs running and ten behind them, lanes to spare: information, hourly, never a warning."""
    service, harness = fleet
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    service.policy["caps"]["max_active_attempts"] = 1
    submit(service, harness, pinned_model="terra")
    submit(service, harness, pinned_model="terra")
    submit(service, harness, pinned_model="astra")
    service._admit()
    service._admit()                                                 # a pass that places nothing starts the idle stretch
    assert service._admission["reasons"] == {"fleet-full": 2}
    service._admission["idle_since"] = time.monotonic() - 61
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO] and "fleet-full x2" in seen[0].getMessage()
    assert "1 lanes open (codex-1)" in seen[0].getMessage()          # the second slot of a measured lane
    service._admission["logged_at"] = time.monotonic() - daemon_module.ADMISSION_IDLE_REPEAT_S - 1
    service._admit()
    assert len(seen) == 1                                            # ten minutes on: still nothing to say
    service._admission["logged_at"] = time.monotonic() - daemon_module.ADMISSION_IDLE_REPEAT_EXPECTED_S - 1
    service._admit()
    assert len(seen) == 2


def test_c6_11_a_job_whose_earlier_attempt_is_still_live_says_so_and_is_not_counted(fleet):
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    service._admit()
    service.store.update_job(job_id, state="queued")                 # between attempts: the first is still live
    service._admit()
    assert service._holds[job_id] == {"reason": "attempt-live"} and service._admission["pending"] == 0
    assert "an earlier attempt of this job is still live" in service.dispatch("why", {"job_id": job_id})["text"]


def test_c6_11_a_placement_ends_the_idle_stretch_and_waits_a_person_must_end_do_not_start_one(fleet):
    service, harness = fleet
    approval = submit(service, harness, pinned_model="astra")
    service.store.update_job(approval, state="waiting", wait_reason="approval")
    service._admit()
    assert service._admission["pending"] == 0 and service._admission["idle_since"] is None
    submit(service, harness, pinned_model="astra")
    service._admit()
    assert service._admission["placed_at"] and service._admission["idle_since"] is None


def test_c6_11_status_reports_admission_and_counts_only_live_jobs(fleet):
    """C-6.11, C-17.1 `running jobs: 519` was every job in the store."""
    from subfleet import cli
    service, harness = fleet
    done = submit(service, harness, pinned_model="astra")
    service.kill(protocol.KillArgs(done))
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    service._admission["idle_since"] = time.monotonic() - 3700
    data = service.dispatch("daemon.status", {})
    assert data["admission"]["pending"] == 1 and data["admission"]["open_lanes"] == ["codex-1"]
    assert data["admission"]["reasons"] == {"no-lanes": 1} and data["admission"]["idle_for_s"] >= 3700
    assert {row["job_id"] for row in data["jobs"]} == {done, stuck}  # the capacity view carries them all
    text = cli.format_status(data)
    assert "running jobs: 1" in text and done not in text and stuck in text
    assert "admission: 1 pending, none placed for 37" in text and "no-lanes x1" in text


# --- a worker that raises (C-5.10) --------------------------------------------------------------

def test_c5_10_a_worker_that_raises_is_retried_with_backoff_and_logged_sparsely(fleet):
    """C-5.10 the control loop offers a live key every 50 ms; forty SalvageError lines were one attempt."""
    service, _ = fleet
    calls = []

    def boom():
        calls.append(time.monotonic())
        raise RuntimeError("secret-bearing detail that must not be logged")

    def offer(times):
        for _ in range(times):
            service._schedule("fixture/a1", boom)
            time.sleep(.002)
        deadline = time.monotonic() + 2
        while "fixture/a1" in service._busy and time.monotonic() < deadline:
            time.sleep(.005)

    offer(40)
    assert len(calls) == 1                                           # not forty
    for expected in (2, 3, 4):
        service._worker_retry_at["fixture/a1"] = 0                   # its retry time arrives
        offer(10)
        assert len(calls) == expected
    service._log_handler.flush()
    lines = [line for line in (service.root / "daemon.log").read_text().splitlines() if "fixture/a1" in line]
    assert [line.split("(")[1].split(",")[0] for line in lines] == ["1 in a row", "2 in a row", "4 in a row"]
    assert "secret" not in "\n".join(lines)
    assert service._worker_retry_at["fixture/a1"] - time.monotonic() > 3    # .5, 1, 2, then 4 s


def test_c5_10_a_success_forgives_the_failures(fleet):
    service, _ = fleet
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise OSError("transient")

    def run_once():
        service._worker_retry_at.pop("fixture/a2", None)
        service._schedule("fixture/a2", flaky)
        deadline = time.monotonic() + 2
        while "fixture/a2" in service._busy and time.monotonic() < deadline:
            time.sleep(.005)

    run_once(); run_once()
    assert service._worker_failures["fixture/a2"] == 2
    state["fail"] = False
    run_once()
    assert "fixture/a2" not in service._worker_failures and "fixture/a2" not in service._worker_retry_at
