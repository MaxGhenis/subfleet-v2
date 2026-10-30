"""C-11.10 (D-TASKHOLD1): an operator holds one task at admission, never whole lanes.

Example tests on the real daemon pass, in process, on a virtual clock. A compute
booking that asks for no new builds holds `build`; reviews, lookups and research
on the same provider and the same lane are placed as before, conversation turns
are never held, and a running attempt is never touched. Release ends the hold at
once and its end ends it by the clock. The properties (T1 to T5) are in
`test_task_hold_properties.py` and the mutants in `test_task_hold_mutants.py`.
"""

from __future__ import annotations

import pytest

from subfleet import cli, daemon as daemon_module, protocol, render, task_holds
from subfleet.store import SCHEMA_VERSION, Store
from tests.disk_floor_model import stamp
from tests.fake.test_admission_ramp import fake_clock
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.task_hold_support import (NOTE, attempts, finish, fresh_daemon, hold, held_now, job, refused, release, store_image,
                                     submit, turn)


@pytest.fixture(autouse=True)
def non_git_workspace(monkeypatch):
    monkeypatch.setattr(daemon_module, "git_head", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "git_toplevel", lambda *args, **kwargs: None)


@pytest.fixture
def held(state_daemon, monkeypatch):      # noqa: F811
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    return service, harness, clock


def placed_jobs(service) -> list[str]:
    return [row["job_id"] for row in attempts(service)]


# --- the hold itself ---------------------------------------------------------------------------

def test_a_held_build_waits_while_a_review_on_the_same_lane_is_placed(held):
    """The incident's shape: one provider, one lane; the build waits, the review runs there."""
    service, harness, clock = held
    answer = hold(service, "build", stamp(3600))
    assert answer["held"] == "build"
    assert answer["task_hold"] == {"task": "build", "until": stamp(3600), "note": NOTE, "since": stamp(0)}
    build = submit(service, harness, "build", "build-1")
    review = submit(service, harness, "review", "review-1")
    service._admit()
    assert placed_jobs(service) == [review]
    assert attempts(service, review)[0]["lane_id"] == "codex-1"
    row = job(service, build)
    assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "task-held", stamp(3600))
    hold_record = service._holds[build]
    assert hold_record == {"reason": "task-held", "task": "build", "until": stamp(3600), "note": NOTE,
                           "next_check_at": stamp(3600)}
    for _ in range(3):
        clock[0] += 60
        service._admit()
    assert placed_jobs(service) == [review]
    # The lane itself was never closed: no closure, and its slot took the review.
    assert service.store.query("SELECT * FROM closures") == []


def test_a_job_with_no_task_and_a_job_of_another_task_are_not_held(held):
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    pinned = submit(service, harness, None, "pinned-1")
    sweep = submit(service, harness, "sweep", "sweep-1")
    service._admit()
    assert sorted(placed_jobs(service)) == sorted([pinned, sweep])


def test_a_held_build_retried_after_a_failed_attempt_waits(held):
    service, harness, clock = held
    build = submit(service, harness, "build", "build-1")
    service._admit()
    [first] = attempts(service, build)
    hold(service, "build", stamp(3600))
    finish(service, first, retry=True)                  # transient: the job is queued again
    clock[0] += 61
    service._admit()
    assert len(attempts(service, build)) == 1           # no second attempt while held
    assert job(service, build)["wait_reason"] == "task-held"
    release(service, "build")
    service._admit()
    assert [row["seq"] for row in attempts(service, build)] == [1, 2]


def test_release_wakes_the_held_jobs_at_once(held):
    """A release puts each held job back in the queue in its own transaction, not at the old end."""
    service, harness, clock = held
    hold(service, "build", stamp(86400))
    builds = [submit(service, harness, "build", f"build-{n}") for n in range(3)]
    service._admit()
    assert all(job(service, b)["wait_reason"] == "task-held" for b in builds)
    notified = []
    original = service._notify
    service._notify = lambda: (notified.append(True), original())[1]
    answer = release(service, "build")
    assert answer["released"] == "build" and answer["was_held"] is True and answer["woken"] == 3
    assert notified, "release must notify"
    for b in builds:
        row = job(service, b)
        assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("queued", None, None)
    clock[0] += 1                                        # long before the old end
    service._admit()
    assert sorted(placed_jobs(service)) == sorted(builds)


def test_a_release_mid_pass_lets_the_later_jobs_of_that_pass_start(held, monkeypatch):
    """The pass in flight reads the holds again once a release lands (`_task_hold_epoch`)."""
    service, harness, clock = held
    hold(service, "build", stamp(86400))
    review = submit(service, harness, "review", "review-0")
    clock[0] += 1
    build = submit(service, harness, "build", "build-1")
    route = service._prepare_route
    released = []

    def release_then_route(job_row, *args, **kwargs):
        if job_row["job_id"] == review and not released:
            released.append(release(service, "build"))
        return route(job_row, *args, **kwargs)
    monkeypatch.setattr(service, "_prepare_route", release_then_route)
    service._admit()
    assert released and sorted(placed_jobs(service)) == sorted([review, build])


def test_the_hold_ends_by_the_clock_with_no_reaper(held):
    service, harness, clock = held
    hold(service, "build", stamp(600))
    build = submit(service, harness, "build", "build-1")
    service._admit()
    assert job(service, build)["next_check_at"] == stamp(600)
    clock[0] = 599
    service._admit()
    assert placed_jobs(service) == []
    clock[0] = 600
    service._admit()
    assert placed_jobs(service) == [build]
    # Nothing removed the row: the clock alone ended the hold.
    assert [row["lease_key"] for row in service.store.query(task_holds.ROWS_SQL, task_holds.ROWS_PARAMS)] \
        == ["task-hold:build"]
    assert held_now(service) == []


def test_a_second_hold_replaces_the_first(held):
    service, harness, clock = held
    hold(service, "build", stamp(86400), note="first booking")
    build = submit(service, harness, "build", "build-1")
    service._admit()
    assert job(service, build)["next_check_at"] == stamp(86400)
    answer = hold(service, "build", stamp(300), note="second booking")
    assert answer["replaced"] == {"task": "build", "until": stamp(86400), "note": "first booking",
                                  "since": stamp(0)}
    assert held_now(service) == [{"task": "build", "until": stamp(300), "note": "second booking",
                                  "since": stamp(0)}]
    rows = service.store.query(task_holds.ROWS_SQL, task_holds.ROWS_PARAMS)
    assert [(row["holder"], row["expires_at"]) for row in rows] == [("operator: second booking", stamp(300))]
    assert job(service, build)["next_check_at"] == stamp(300)      # re-clocked to the new end
    service._admit()
    assert service._holds[build]["note"] == "second booking"
    clock[0] = 300
    service._admit()
    assert placed_jobs(service) == [build]


def test_a_turn_of_a_held_task_is_placed(held):
    """C-26.9: a conversation turn is a person waiting; it is never held."""
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    turn_job = turn(service, harness, 1, task="build")
    assert job(service, turn_job)["task"] == "build"
    service._admit()
    assert placed_jobs(service) == [turn_job]


def test_a_running_attempt_is_never_touched(held, monkeypatch):
    service, harness, clock = held
    build = submit(service, harness, "build", "build-1")
    service._admit()
    with service.store.transaction("test.running") as tx:
        tx.execute("UPDATE attempts SET state='running',started_at=? WHERE job_id=?", (stamp(0), build))
    before = [dict(row) for row in attempts(service, build)]
    signals = []
    monkeypatch.setattr(daemon_module.os, "killpg", lambda *args: signals.append(args))
    monkeypatch.setattr(daemon_module.os, "kill", lambda *args: signals.append(args))
    hold(service, "build", stamp(3600))
    for _ in range(3):
        clock[0] += 30
        service._admit()
    assert [dict(row) for row in attempts(service, build)] == before
    assert job(service, build)["state"] == "running" and signals == []
    release(service, "build")
    service._admit()
    assert [dict(row) for row in attempts(service, build)] == before


def test_a_hold_committed_after_the_pass_read_its_holds_still_stops_the_reservation(held, monkeypatch):
    """C-6.3: the reserving transaction reads the hold again at its own clock."""
    service, harness, clock = held
    build = submit(service, harness, "build", "build-1")
    route = service._prepare_route

    def hold_then_route(job_row, *args, **kwargs):
        if job_row["job_id"] == build:
            # The hold lands after this job's door check, as a concurrent operator's would;
            # the epoch the pass compares is put back, so only the transaction can see it.
            epoch_seen = service._task_hold_epoch
            hold(service, "build", stamp(3600))
            service._task_hold_epoch = epoch_seen
        return route(job_row, *args, **kwargs)
    monkeypatch.setattr(service, "_prepare_route", hold_then_route)
    service._admit()
    assert placed_jobs(service) == []
    row = job(service, build)
    assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "task-held", stamp(3600))


# --- refusals write nothing ------------------------------------------------------------------

@pytest.mark.parametrize("args,reason", [
    ({"action": "hold", "task": "builds", "until": stamp(60), "note": NOTE}, "unknown-task"),
    ({"action": "hold", "task": "", "until": stamp(60), "note": NOTE}, "unknown-task"),
    ({"action": "hold", "task": None, "until": stamp(60), "note": NOTE}, "unknown-task"),
    ({"action": "hold", "task": "build", "until": stamp(60)}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "   "}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "two\nlines"}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "x" * 161}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "tab\there"}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "\nbooking\n"}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "\tbooking\t"}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "booking\u2028"}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": "\x85booking"}, "task-hold-note"),
    ({"action": "hold", "task": "build", "until": stamp(60), "note": 7}, "task-hold-note"),
    ({"action": "hold", "task": "build", "note": NOTE}, "task-hold-until"),
    ({"action": "hold", "task": "build", "until": "tomorrow", "note": NOTE}, "task-hold-until"),
    ({"action": "hold", "task": "build", "until": "2026-13-01T00:00:00Z", "note": NOTE}, "task-hold-until"),
    ({"action": "hold", "task": "build", "until": stamp(0), "note": NOTE}, "task-hold-until"),
    ({"action": "hold", "task": "build", "until": stamp(-60), "note": NOTE}, "task-hold-until"),
    ({"action": "release", "task": "builds"}, "unknown-task"),
    ({"action": "pause", "task": "build"}, "tasks-action"),
])
def test_a_refused_hold_or_release_writes_nothing(held, args, reason):
    service, harness, clock = held
    hold(service, "review", stamp(7200), note="kept")
    submit(service, harness, "build", "build-1")
    before = store_image(service)
    error = refused(service, args)
    assert error.reason == reason and error.code == 2
    assert store_image(service) == before


def test_a_note_at_the_limit_is_kept_whole(held):
    service, harness, clock = held
    note = ("uk-data " * 20)[:task_holds.NOTE_MAX - 1] + "!"      # prose: a run of 100 letters reads as a token
    assert len(note) == task_holds.NOTE_MAX
    assert hold(service, "build", stamp(60), note=f"  {note}  ")["task_hold"]["note"] == note


def test_a_note_shaped_like_a_credential_is_refused_and_never_stored(held):
    service, harness, clock = held
    secret = "sk-ant-oat01-" + "A" * 40
    before = store_image(service)
    error = refused(service, {"action": "hold", "task": "build", "until": stamp(60), "note": f"key {secret}"})
    assert error.reason == "task-hold-note"
    assert store_image(service) == before


def test_release_of_a_task_with_no_hold_is_a_no_op(held):
    service, harness, clock = held
    before = store_image(service)
    answer = release(service, "build")
    assert answer["released"] == "build" and answer["was_held"] is False and answer["woken"] == 0
    assert store_image(service) == before


def test_a_hold_on_a_task_the_policy_no_longer_routes_can_still_be_released(held):
    service, harness, clock = held
    hold(service, "build", stamp(600))
    chains = service.policy["chains"]
    service.policy["chains"] = {task: chain for task, chain in chains.items() if task != "build"}
    assert refused(service, {"action": "hold", "task": "build", "until": stamp(60), "note": NOTE}).reason \
        == "unknown-task"
    assert release(service, "build")["was_held"] is True
    assert held_now(service) == []


def test_the_task_names_are_the_policys_chains(held):
    service, harness, clock = held
    for task in service.policy["chains"]:
        assert hold(service, task, stamp(60))["held"] == task
    assert [row["task"] for row in held_now(service)] == sorted(service.policy["chains"])


# --- events, status, why -----------------------------------------------------------------------

def test_hold_and_release_are_events(held):
    service, harness, clock = held
    hold(service, "build", stamp(600))
    hold(service, "build", stamp(900), note="moved")
    release(service, "build")
    rows = service.store.query("SELECT kind,data_json FROM events WHERE kind IN ('task.held','task.released') "
                               "ORDER BY event_id")
    import json
    found = [(row["kind"], json.loads(row["data_json"])) for row in rows]
    assert found == [
        ("task.held", {"task": "build", "until": stamp(600), "note": NOTE, "replaced": None}),
        ("task.held", {"task": "build", "until": stamp(900), "note": "moved",
                       "replaced": {"until": stamp(600), "note": NOTE}}),
        ("task.released", {"task": "build", "until": stamp(900), "note": "moved", "woken": 0}),
    ]


def test_status_shows_the_holds_in_force_and_raises_no_alert(held):
    service, harness, clock = held
    status = service.dispatch("daemon.status", {})
    assert status["task_holds"] == []
    assert "task holds" not in cli.format_status(status)
    hold(service, "build", stamp(3600))
    build = submit(service, harness, "build", "build-1")
    service._admit()
    status = service.dispatch("daemon.status", {})
    assert status["task_holds"] == [{"task": "build", "until": stamp(3600), "note": NOTE, "since": stamp(0),
                                     "waiting": 1}]
    assert status["alerts"] == []
    text = cli.format_status(status)
    assert "task holds" in text
    line = next(line for line in text.splitlines() if line.strip().startswith("build"))
    assert stamp(3600) in line and NOTE in line and "1 waiting" in line
    waiting = next(row for row in status["jobs"] if row["job_id"] == build)
    assert waiting["wait_reason"] == "task-held"
    # The idle-admission record counts no job the operator holds (C-6.11: it is not admission's to place).
    assert service._admission["pending"] == 0 and "task-held" not in service._admission["reasons"]
    # The capacity text a `readings` caller gets names the hold too.
    assert "Task holds" in render.status(status)
    clock[0] = 3600
    assert service.dispatch("daemon.status", {})["task_holds"] == []


def test_why_names_the_note_and_the_end(held):
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    build = submit(service, harness, "build", "build-1")
    service._admit()
    answer = service.dispatch("why", {"job_id": build})
    lines = render.why_queue(answer["job"])
    held_line = next(line for line in lines if line.startswith("Held:"))
    assert "build" in held_line and NOTE in held_line and stamp(3600) in held_line
    assert "`subfleet tasks release -- build`" in held_line


def test_offline_status_lists_the_holds(held):
    from subfleet.offline import Offline
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    # Read with the wall clock: an hour past the virtual one is still far in the past here,
    # so the offline reader (which has no virtual clock) finds no hold in force.
    assert Offline(service.root).status()["task_holds"] == []
    hold(service, "build", "2099-06-01T00:00:00Z")
    assert [row["task"] for row in Offline(service.root).status()["task_holds"]] == ["build"]


@pytest.mark.parametrize("reader", ["daemon", "offline"])
def test_status_counts_every_held_job_past_the_jobs_it_lists(held, reader):
    """Review r1, P3-1: 51 held builds read `(50 waiting)` offline, counted from the 50 jobs listed."""
    from subfleet.offline import Offline
    service, harness, clock = held
    hold(service, "build", "2099-06-01T00:00:00Z")          # in force by the offline reader's wall clock too
    for n in range(51):
        submit(service, harness, "build", f"build-{n}")
    review = submit(service, harness, "review", "review-1")
    service._admit()
    assert [row["job_id"] for row in attempts(service)] == [review]
    status = service.dispatch("daemon.status", {}) if reader == "daemon" else Offline(service.root).status()
    if reader == "offline":
        assert len(status["running"]) == 50                 # the listing is capped; the count is not
    assert [(row["task"], row["waiting"]) for row in status["task_holds"]] == [("build", 51)]
    assert "build until 2099-06-01T00:00:00Z (51 waiting)" in cli.format_status(status)


def test_an_unknown_task_is_refused_by_name_and_holds_nothing(held):
    """Review r1: the unknown-task mutant survived every property, so the refusal is asserted here."""
    service, harness, clock = held
    before = store_image(service)
    for task in ("builds", "Build", "build ", "task-hold:build", "data build"):
        for args in ({"action": "hold", "task": task, "until": stamp(60), "note": NOTE},
                     {"action": "release", "task": task}):
            error = refused(service, args)
            assert (error.reason, error.code) == ("unknown-task", 2), (task, args)
            assert repr(task) in str(error)
    assert store_image(service) == before
    assert held_now(service) == []
    build = submit(service, harness, "build", "build-1")
    service._admit()
    assert placed_jobs(service) == [build]


def test_a_release_signals_the_daemons_waiters_after_it_commits(held):
    """Review r1: a mutant that dropped only the release's notification survived the named
    assertion. Here the daemon's own signals are spied on: a thread waiting on `changed` wakes,
    and the wait hub is poked after the job is queued again in the store."""
    import threading
    service, harness, clock = held
    hold(service, "build", stamp(86400))
    build = submit(service, harness, "build", "build-1")
    service._admit()
    assert job(service, build)["wait_reason"] == "task-held"
    pokes = []
    poke = service.wait_hub.poke
    service.wait_hub.poke = lambda: (pokes.append(job(service, build)["state"]), poke())[1]
    ready, woke = threading.Event(), []

    def waiter():
        with service.changed:
            ready.set()
            woke.append(service.changed.wait(timeout=30))
    thread = threading.Thread(target=waiter)
    thread.start()
    assert ready.wait(30)
    release(service, "build")
    thread.join(30)
    assert woke == [True], "T3-notify: the release woke no waiter on `changed`"
    assert pokes and pokes[0] == "queued", "T3-notify: the release poked no wait hub after its commit"


def test_a_reset_credit_is_never_spent_for_a_held_job(held):
    """C-23.16: a job the operator holds is no demand for capacity."""
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    submit(service, harness, "build", "build-1")
    service._admit()
    assert service._reset_demand() == []


# --- storage: a lease row, no schema change ----------------------------------------------------

def test_a_hold_needs_no_schema_change_and_survives_a_restart(held):
    """The hold is a `leases` row. It was built at schema 7; release/217, merged in at f6612e160,
    moved the store to schema 9 with job and attempt columns only, and a hold adds nothing."""
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    build = submit(service, harness, "build", "build-1")
    service._admit()
    # A second connection, as a restarted daemon's or the offline reader's would be.
    reopened = Store(service.root / "state.sqlite3", read_only=True)
    try:
        assert reopened.one("SELECT max(version) v FROM schema_version")["v"] == SCHEMA_VERSION
        assert reopened.one("SELECT 1 FROM sqlite_master WHERE name LIKE '%task_hold%'") is None
        row = reopened.one(task_holds.ROW_SQL, (task_holds.lease_key("build"),))
        assert (row["holder"], row["expires_at"]) == (f"operator: {NOTE}", stamp(3600))
        assert task_holds.in_force([row], stamp(1)) == {"build": {"task": "build", "until": stamp(3600),
                                                                   "note": NOTE, "since": stamp(0)}}
        assert reopened.get_job(build)["wait_reason"] == "task-held"
    finally:
        reopened.close()


def test_no_lease_reaper_removes_a_hold(held):
    """Each reaper deletes by an attempt's, a job's, a probe's or retention's holder, or its own key."""
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    review = submit(service, harness, "review", "review-1")
    service._admit()
    [attempt] = attempts(service, review)
    finish(service, attempt)
    for holder in (attempt["attempt_id"], review, "probe:timer:x", "retention:x", "resume:x"):
        service.store.release_leases(holder)
    service._recover_probes()
    assert [row["task"] for row in held_now(service)] == ["build"]


def test_a_hold_is_no_lease_a_capacity_wait_counts(held):
    """C-6.10: a pass's lease census leaves the hold's row out, so its release frees nothing early."""
    service, harness, clock = held
    hold(service, "build", stamp(3600))
    service._admit()
    for kind in ("detached", "turn"):
        seen = service._leases_seen.get(kind, frozenset())
        assert not any(key.startswith(task_holds.KEY) for key, _ in seen), kind


def test_the_hold_shows_in_why_text_constants():
    assert "task-held" in render._HOLD_TEXT
    assert "task-held" in daemon_module.NOT_ADMISSIONS_TO_PLACE
    assert protocol.TasksArgs().action == "list"


def test_quarantined_probe_can_recover_without_a_person(monkeypatch):
    from subfleet.procs import Containment, ProcessIdentity
    from tests.fake.test_probe_recovery import reserved_probe, exit_receipts
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "probe-recovery")
        record = reserved_probe(service, build, state="quarantined")
        exit_receipts(record)
        hold(service, "build", stamp(1000))
        assert job(service, build)["wait_reason"] == "task-held"
        census = [Containment(marker_pids=frozenset({900003}), identities={
            900003: ProcessIdentity(900003, "boot", "escaped")})]
        monkeypatch.setattr(service, "_probe_census", lambda record: census[0])
        service._recover_probes()
        assert job(service, build)["wait_reason"] == "uncertain"
        assert service._probe_record(record["holder"])["state"] == "quarantined"
        census[0] = Containment()
        service._recover_probes()
        assert service._probe_record(record["holder"])["state"] == "completed"
        assert job(service, build)["wait_reason"] == "capacity"
        assert service._task_holds_now().get("build")
        service._admit()
        assert job(service, build)["wait_reason"] == "task-held"
        assert attempts(service, build) == []
