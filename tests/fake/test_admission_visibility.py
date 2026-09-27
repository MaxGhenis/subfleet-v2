"""C-6.11 and C-5.10: a stalled queue says so, and a failing worker does not spin.

Incident, 2026-09-20: fifteen jobs sat queued for hours beside open lanes and
nothing said why. `subfleet why <queued job>` printed `null` (a job admission
skipped has no decision row, and the CLI printed the missing value rather than
the daemon's text). `subfleet status` said `running jobs: 519`, the size of the
whole store. `daemon.log` had not gained a line since the daemon started.
"""

import logging
import re
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


def age(service, seconds):
    """Move the idle stretch `seconds` into the past: no test here waits on a real clock."""
    state = service._admission
    for key in ("idle_since", "checked_at", "logged_at"):
        if state.get(key) is not None:
            state[key] -= seconds


class Inline:
    """A worker pool that runs the work in the caller, so no test waits on a thread or a clock."""

    def __init__(self, real):
        self.real = real

    def submit(self, fn, *args):
        from concurrent.futures import Future
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **options):
        self.real.shutdown(**options)


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
    ({"reason": "route-moved", "tries": 3}, "its route could not be settled in 3 reservations in a row"),
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
    age(service, 61)
    for _ in range(50):
        service._admit()
    lines = log_lines(service)
    assert len(lines) == 1
    assert re.search(r"2 jobs pending, none placed for 6\d s", lines[0])      # 61 s, or 62 on a slow runner
    assert "1 lanes open (codex-1)" in lines[0]
    assert "behind-older-job x1" in lines[0] and "no-lanes x1" in lines[0] and f"first in line {stuck}" in lines[0]
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
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
    age(service, 61)
    service._admit()
    with service.store.transaction("fixture.disable") as tx:
        tx.execute("UPDATE lanes SET enabled=0")
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
    service._admit()
    assert [record.levelno for record in seen] == [logging.WARNING]  # no lane open now: expected, and said within the hour
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_EXPECTED_S + 1)
    service._admit()
    assert [record.levelno for record in seen] == [logging.WARNING, logging.INFO]
    assert "0 lanes open (-)" in seen[1].getMessage()


def test_c6_11_a_wait_that_becomes_a_warning_is_one_within_ten_minutes(fleet):
    """C-6.11 review of cb83e1b: the interval was chosen from the last line's severity, so an expected
    wait that turned into the incident stayed silent for the rest of the hour."""
    service, harness = fleet
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    with service.store.transaction("fixture.disable") as tx:
        tx.execute("UPDATE lanes SET enabled=0")
    submit(service, harness, pinned_model="opus")
    service._admit()
    age(service, 61)
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO]     # no lane open: waiting is expected
    with service.store.transaction("fixture.enable") as tx:
        tx.execute("UPDATE lanes SET enabled=1")
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO, logging.WARNING]


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
    age(service, 61)
    service._admit()
    assert [record.levelno for record in seen] == [logging.INFO] and "fleet-full x2" in seen[0].getMessage()
    assert "1 lanes open (codex-1)" in seen[0].getMessage()          # the second slot of a measured lane
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_S + 1)
    service._admit()
    assert len(seen) == 1                                            # ten minutes on: looked at, still nothing to say
    age(service, daemon_module.ADMISSION_IDLE_REPEAT_EXPECTED_S)
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
    age(service, 3700)
    data = service.dispatch("daemon.status", {})
    assert data["admission"]["pending"] == 1 and data["admission"]["open_lanes"] == ["codex-1"]
    assert data["admission"]["reasons"] == {"no-lanes": 1} and data["admission"]["idle_for_s"] >= 3700
    assert {row["job_id"] for row in data["jobs"]} == {done, stuck}  # the capacity view carries them all
    text = cli.format_status(data)
    assert "running jobs: 1" in text and done not in text and stuck in text
    assert "admission: 1 pending, none placed for 37" in text and "no-lanes x1" in text


def test_c3_7_c15_5_status_reports_the_read_pool_and_the_wait_hub(fleet):
    """An operator can see a starved read pool and a wait hub that keeps dying."""
    service, _ = fleet
    data = service.dispatch("daemon.status", {})
    assert data["read_pool"]["size"] == daemon_module.READ_CONNECTIONS
    assert {"snapshot_share", "in_use", "waits", "own_connections", "longest_wait_s"} <= set(data["read_pool"])
    assert data["wait_hub"] == {"waiters": 0, "running": False, "reads": 0, "deaths": 0, "restarts": 0,
                                "start_failures": 0}


# --- a worker that raises (C-5.10) --------------------------------------------------------------

def test_c5_10_a_worker_that_raises_is_retried_with_backoff_and_logged_sparsely(fleet, monkeypatch):
    """C-5.10 the control loop offers a live key every 50 ms; forty SalvageError lines were one attempt."""
    service, _ = fleet
    # No thread and no wall clock: the work runs inline, and the retry time is an hour off until the test says it has come.
    service.workers = Inline(service.workers)
    monkeypatch.setattr(daemon_module, "worker_retry_delay", lambda failures: 3600.0)
    calls = []

    def boom():
        calls.append(len(calls))
        raise RuntimeError("secret-bearing detail that must not be logged")

    for _ in range(40):
        service._schedule("fixture/a1", boom, paced=True)
    assert len(calls) == 1                                           # not forty
    for expected in (2, 3, 4, 5):
        service._worker_retry_at["fixture/a1"] = 0                   # its retry time arrives
        for _ in range(10):
            service._schedule("fixture/a1", boom, paced=True)
        assert len(calls) == expected
    service._log_handler.flush()
    lines = [line for line in (service.root / "daemon.log").read_text().splitlines() if "fixture/a1" in line]
    assert [line.split("(")[1].split(",")[0] for line in lines] == ["1 in a row", "2 in a row", "4 in a row"]
    assert "secret" not in "\n".join(lines) and all("RuntimeError" in line for line in lines)
    assert service._worker_failures["fixture/a1"] == 5 and "fixture/a1" not in service._busy


@pytest.mark.parametrize("failures,expected", [(1, .5), (2, 1), (3, 2), (4, 4), (7, 32), (8, 60), (500, 60), (0, .5)])
def test_c5_10_retry_delay(failures, expected):
    assert daemon_module.worker_retry_delay(failures) == expected


def test_c5_10_a_success_forgives_the_failures(fleet):
    service, _ = fleet
    service.workers = Inline(service.workers)
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise OSError("transient")

    def run_once():
        service._worker_retry_at.pop("fixture/a2", None)
        service._schedule("fixture/a2", flaky, paced=True)

    run_once(); run_once()
    assert service._worker_failures["fixture/a2"] == 2
    state["fail"] = False
    run_once()
    assert "fixture/a2" not in service._worker_failures and "fixture/a2" not in service._worker_retry_at


def test_c5_10_a_one_shot_request_is_never_held_back(fleet):
    """C-5.10 review of cb83e1b: `kill --confirm-dead` schedules `resolve:<job>` once and answers
    "resolution requested". Pacing it dropped the operator's retry: nothing offers that key again."""
    service, _ = fleet
    service.workers = Inline(service.workers)
    calls = []

    def resolve():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("the first resolution fails")

    for _ in range(2):
        service._schedule("resolve:fixture", resolve)                # as `kill` calls it: not paced
    assert calls == [0, 1]                                           # the retry ran at once
    assert "resolve:fixture" not in service._worker_retry_at


# --- what another thread reads (C-6.11) ---------------------------------------------------------

def test_c6_11_status_reads_one_snapshot_that_admission_never_changes_under_it(fleet):
    """C-6.11 review of cb83e1b: `daemon.status` read `idle_since` twice and admission could clear it
    between the reads (`float - None`). The snapshot is replaced whole and the old one is left alone."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    service._admit()
    before = service._admission
    frozen = dict(before)
    assert before["idle_since"] is not None
    service.kill(protocol.KillArgs(stuck))
    service._admit()                                                 # the stretch ends: idle_since goes to None
    assert service._admission is not before and service._admission["idle_since"] is None
    assert before == frozen                                          # a reader holding the old one saw no half-update
    assert service.dispatch("daemon.status", {})["admission"]["idle_for_s"] is None


def test_c6_11_a_pass_that_does_not_look_keeps_the_whole_hold(fleet):
    """C-6.11 review of cb83e1b: the pass after the look kept only the label, and `why` printed
    `held by another job: -` and `max_active_attempts (?)`."""
    service, harness = fleet
    out = str(harness.root / "report.md")
    waiting = submit(service, harness, pinned_model="terra", out_path=out)
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"out:{out}", "some-other-job", utcnow()))
    service._admit()
    service.store.update_job(waiting, next_check_at=after(3600))
    service._admit()                                                 # not due: nothing is looked at
    assert service._holds[waiting]["leases"] == [f"out:{out}"]
    assert f"held by another job: out:{out}" in service.dispatch("why", {"job_id": waiting})["text"]


def test_c6_11_the_reason_is_the_latest_looks_even_when_the_verdict_repeats(fleet, monkeypatch):
    """C-6.11 review of cb83e1b: a probe counts toward the fleet cap, so one verdict read `fleet-full`
    on one look and the real reason on the next; the first label stuck and hid the warning."""
    service, harness = fleet
    labels = iter(["fleet-full", "reserve:fable:unmeasured"])
    monkeypatch.setattr(daemon_module.scheduler, "dominant_rejection", lambda decision: next(labels))
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service._holds[stuck]["reason"] == "fleet-full"
    service.store.update_job(stuck, next_check_at=utcnow())
    service._admit()
    assert service._capacity_waits[stuck]["rechecks"] == 1           # the same verdict: no new row
    assert len(service.store.list_decisions(stuck)) == 1
    service.store.update_job(stuck, next_check_at=after(3600))
    service._admit()                                                 # a pass that does not look reports the last look
    assert service._holds[stuck]["reason"] == "reserve:fable:unmeasured"
    assert service._admission["reasons"] == {"reserve:fable:unmeasured": 1}



# --- review of be171d5 --------------------------------------------------------------------------

def test_c6_11_the_slot_kept_for_an_older_job_is_not_called_a_full_fleet(fleet):
    """C-6.11, C-6.9: with a cap of 2, one attempt running and an older job waiting, a later job is
    held one short of the cap. `why` said "the fleet is at max_active_attempts (2)" beside a free slot."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 2
    submit(service, harness, pinned_model="terra")
    service._admit()
    older = submit(service, harness, pinned_model="astra")
    passer = submit(service, harness, pinned_model="terra")
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(3600))
    service._admit()
    hold = service._holds[passer]
    assert (hold["reason"], hold["kept_for"], hold["live"], hold["max_active_attempts"]) == ("slot-kept", older, 1, 2)
    text = service.dispatch("why", {"job_id": passer})["text"]
    assert f"1 of 2 attempts are running and the last slot is kept for {older}" in text and "fleet is at" not in text
    assert "slot-kept" in daemon_module.EXPECTED_HOLDS               # ordinary queueing: never a warning


def test_c6_11_a_workspace_retry_is_reported_as_one_even_behind_a_full_fleet(fleet):
    """C-6.11: the full-fleet branch came first, so a C-6.8 retry was counted as `fleet-full`, and as pending."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    submit(service, harness, pinned_model="terra")                   # takes the only slot
    blocked = submit(service, harness, pinned_model="terra")         # looked at, finds the fleet full: the pass ends
    retrying = submit(service, harness, pinned_model="astra")
    service.store.update_job(retrying, state="waiting", wait_reason="workspace", next_check_at=after(3600))
    service._admit()
    assert service._holds[blocked]["reason"] == "fleet-full"
    assert service._holds[retrying] == {"reason": "workspace"}       # reached by the full-fleet branch, reported as itself
    assert service._admission["pending"] == 1                        # `blocked` only: a retry is not admission's to place


def test_c6_11_a_pass_that_raises_publishes_nothing(fleet):
    """C-6.11: half a hold set read as "nothing left pending" and ended the idle stretch, queue untouched."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    age(service, 61)
    service._admit()
    assert len(log_lines(service)) == 1
    holds, snapshot = service._holds, service._admission
    real = service._admit_pass

    def raising(holds, tally, **options):
        raise OSError("no space left on device")

    service._admit_pass = raising
    with pytest.raises(OSError):
        service._admit()
    assert service._holds is holds and service._admission is snapshot
    assert len(log_lines(service)) == 1                              # no "nothing left pending"
    service._admit_pass = real
    service._admit()
    assert service._holds[stuck]["reason"] == "no-lanes"
