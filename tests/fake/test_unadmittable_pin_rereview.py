"""C-11.8, C-15.2, C-26.13: the re-review of fc6c146d (2026-09-30, in-session Opus review)
found these holes with these probes; each failed on fc6c146d and is kept as the
regression test for its fix.

- a withdrawn pin notice's id, reused by a later ping, labelled the ping with the
  job (a notice now matches its event by id, session and creation time);
- an unread pin notice rode along with a job that ended some other way (it is now
  withdrawn in every terminal notice's transaction, and when the lane recovers);
- a Codex expired token whose one heal was spent was treated as capacity (the
  timers now mark the verdict `heal_spent`, and a latch so marked is standing).
"""

from __future__ import annotations

import io
import json

from subfleet import hooks
from subfleet.daemon import utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake.test_state_contract import receipt_fixture
from tests.fake.test_unadmittable_pin_admission import (events, expire, fleet, service_notices,  # noqa: F401
                                                         settle, submit)


class _Client:
    """What the hooks call, answered in process by the daemon under test."""

    def __init__(self, service):
        self.service, self.root = service, service.root

    def call(self, op, args, timeout=None):
        return self.service.dispatch(op, args)


# --- P3: a withdrawn pin notice's id is reused, and the ping that reuses it names the failed job ------

def test_rr_a_ping_that_reuses_a_withdrawn_pin_notice_id_names_no_job(fleet):
    """C-11.8: "a ping, a nudge or an alert still names none". `_withdraw_pin_notice`
    DELETEs the row, and `service_notices.notice_id` is `INTEGER PRIMARY KEY` without
    AUTOINCREMENT, so when it was the newest row the next service notice gets the same
    id; `_pin_notice_jobs` maps ids to jobs through old `job.pin_noticed` events."""
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    [notice] = service_notices(service)
    expire(service, stuck)
    service._admit()
    assert service.store.get_job(stuck)["rc"] == 3
    assert service_notices(service) == []                                 # withdrawn (deleted)

    pinged = service.dispatch("ping", {"session_id": "lane-session", "text": "subfleet: a nudge"})
    assert pinged["notice_id"] == -notice["notice_id"]                    # the same id, reused
    rows = service.dispatch("notice.pending", {"session_id": "lane-session"})["notices"]
    out = io.StringIO()
    hooks.session_event("UserPromptSubmit", {"session_id": "lane-session"}, service.root,
                        client=_Client(service), stdout=out, env={"SUBFLEET_JOB": "some-lane-run"})
    assert ([row["job_id"] for row in rows], out.getvalue()) == ([None], ""), (
        [row["job_id"] for row in rows], out.getvalue()[:160])


# --- P3: a pending pin notice rides with a job's end that is not the pin's failure -------------------

def test_rr_a_pending_pin_notice_is_not_delivered_with_a_successful_end(fleet):
    """C-11.8 withdraws a still-pending pin notice only when the pin fails the job
    ("since the terminal notice supersedes it"). A job whose lane came back and that
    then succeeded keeps its unseen "can never admit it ... Fix: resubmit ..., then
    `subfleet kill`" notice, which now names the job, so layer 2 (`hooks._deliver`)
    prints it beside the success."""
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    assert len(service_notices(service)) == 1
    service.store.update_lane("claude-9", enabled=1)                      # the quick fix lands
    service._admit()
    [attempt] = service.store.list_attempts(stuck)
    service._pending_launches.discard(attempt["attempt_id"])
    adir = service.root / "jobs" / stuck / "a1"
    adir.mkdir(mode=0o700, parents=True, exist_ok=True)
    service._finalize(receipt_fixture(service, attempt, adir))
    job = service.store.get_job(stuck)
    assert job["state"] == "succeeded", job["state"]
    err = io.StringIO()
    hooks._deliver(_Client(service), "caller-session", job, stderr=err)
    assert "can never admit it" not in err.getvalue(), err.getvalue()[:400]


# --- P2: a Codex expired token whose one heal is spent is standing, and is not called so -------------

def test_rr_a_codex_expired_token_after_its_one_heal_is_a_pin_no_lane_can_admit(fleet):
    """C-23.47 gives a Codex lane "exactly one automatic heal" per credential epoch
    (timers.py:421 requires a new epoch AND 20 minutes; tests/unit/test_timers_probe.py::
    test_expired_token_has_one_heal_and_publishes_only_reprobe_verdict asserts no second
    heal). After it, `expired-token` stays until a person logs in again, which is
    C-11.8's own definition of standing. `capacity.credential_gone` excludes every
    expired token, so a job pinned to that lane is never held, told or failed."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_lane="codex-1", pinned_model="astra")
    # As the timers' probe leaves it once the epoch's one heal has run and missed
    # (tests/unit/test_timers_probe.py::test_c11_8_a_codex_heal_that_misses_marks_the_verdict_heal_spent).
    service.timers.metadata["codex-1"] = {"probe_status": "expired-token", "heal_spent": True, "probed_at": utcnow()}
    service._admit()
    hold = service._holds.get(stuck) or {}
    assert hold.get("reason") == "pin-unadmittable", hold
    assert events(service, stuck, "job.pin_unadmittable")


def test_rr_a_codex_heal_that_misses_is_never_retried_in_its_epoch(tmp_path, monkeypatch):
    """The premise of the probe above, executed: a Codex lane whose one heal turn did
    not refresh the token (no "revoked" in its detail) stays `expired-token`, and a
    later cycle, 20 minutes on, runs no second heal. Passes on fc6c146d: it is the
    mechanism, not the defect."""
    from pathlib import Path

    from subfleet.contracts import Outcome, OutcomeClass
    from subfleet.store import Store
    from subfleet.timers import Timers
    from tests.unit.test_timers_probe import Clock, Probe

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from subfleet.contracts import Credential, Lane, LaneOwner
    clock = Clock()
    adapter = Probe(clock)
    policy = {"models": {"haiku": {"id": "claude-haiku-4-5-20251001"}},
              "timers": {"probe_interval_s": 300, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}
    with Store(tmp_path / "state.sqlite3") as store:
        timer = Timers(store, tmp_path, policy, adapter_factory=lambda _: adapter, now=clock)
        try:
            home = tmp_path / "codex-1"
            home.mkdir()
            (home / "auth.json").write_text(json.dumps({"last_refresh": "first"}))
            store.put_lane(Lane("codex-1", "codex", "codex:codex-1", Credential("codex", str(home), "home"),
                                str(home), LaneOwner.V2, False, True))
            turns = []
            timer.turn = lambda lane, purpose, holder, *, cancel, deadline: (
                turns.append(purpose) or Outcome(OutcomeClass.TRANSIENT, "codex exited 1"))
            expired = {"status": "expired-token", "readings": ()}
            adapter.responses["codex-1"] = [dict(expired), dict(expired)]
            timer.probe_cycle()
            assert turns == ["heal"] and timer.metadata["codex-1"]["probe_status"] == "expired-token"
            clock.advance(1200)
            adapter.responses["codex-1"] = [dict(expired)]
            timer.probe_cycle()
            assert turns == ["heal"]                                            # never again this epoch
            assert timer.metadata["codex-1"]["probe_status"] == "expired-token"
        finally:
            timer.stop()


def test_c11_8_an_expired_token_whose_heal_may_still_land_is_capacity(fleet):
    """The other side: before its heal (no `heal_spent`), an expired token may still be
    renewed, so a job pinned there waits as for capacity, and nobody is told "never"."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_lane="codex-1", pinned_model="astra")
    service.timers.metadata["codex-1"] = {"probe_status": "expired-token", "probed_at": utcnow()}
    service._admit()
    assert (service._holds.get(stuck) or {}).get("reason") != "pin-unadmittable"
    assert not events(service, stuck, "job.pin_unadmittable")


def test_c11_8_a_notice_withdrawn_unread_when_the_lane_recovers_is_not_the_jobs_one(fleet):
    """A notice that stopped being true before anyone read it is withdrawn, and does not
    count: if the lane turns again, the caller is told then."""
    service, harness = fleet
    stuck = submit(service, harness)
    service.store.update_lane("claude-7", enabled=0)                      # nowhere else to go
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    assert len(service_notices(service)) == 1
    from subfleet.contracts import ClockSource, Closure, ClosureReason
    from subfleet.daemon import after
    service.store.update_lane("claude-9", enabled=1)
    service.store.add_closure(Closure("claude-9", "account", after(600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))  # admittable, but it waits
    service._admit()
    assert service_notices(service) == []                                  # withdrawn, unread
    assert [row["data"]["why"] for row in events(service, stuck, "job.pin_notice_withdrawn")] == ["admittable"]
    service.store.update_lane("claude-9", enabled=0)                      # it turns again
    service._admit()
    settle(service, stuck)
    notices = service_notices(service)
    assert len(notices) == 1 and "can never admit it" in notices[0]["text"]
    # One the caller has seen is kept and counts: no third notice after another flip.
    [shown] = service.dispatch("notice.pending", {"session_id": "caller-session"})["notices"]
    service.dispatch("notice.mark", {"session_id": "caller-session", "notice_ids": [shown["notice_id"]],
                                     "state": "surfaced", "transport": "hook:UserPromptSubmit"})
    service.store.update_lane("claude-9", enabled=1)
    service._admit()
    service.store.update_lane("claude-9", enabled=0)
    service._admit()
    settle(service, stuck)
    assert [row["state"] for row in service_notices(service)] == ["surfaced"]
