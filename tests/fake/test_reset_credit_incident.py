"""C-23.16 end to end: the real daemon's admission judges demand; reset credits follow it.

Replays the 2026-09-22 incident against the daemon's own admission pass, reset timer
and probe cycle. Launches are never run (`_admit` only reserves), providers are the
fake adapter, and the Codex subscription endpoints are the real adapter over a fake
HTTP transport (`FakeWham`), so no real credential, endpoint, or credit is touched.

On 2026-09-22 (UTC): codex-2 at 20:33 with nothing queued or running; codex-3 at
21:03 while codex-2 ran one job; codex-6, codex-1, codex-4 and codex-5 by 00:53 the
next day while three reset lanes still had room. Each record said
`no-dispatchable-lanes`.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module, operations, protocol
from subfleet.adapters import registry
from subfleet.adapters.codex import (CodexAdapter, WHAM_RESET_CREDITS_CONSUME_URL, WHAM_RESET_CREDITS_URL,
                                     WHAM_USAGE_URL)
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from subfleet.policy import DEFAULT_POLICY_PATH
from tests.fake_adapter import FakeAdapter


class FakeWham:
    """The Codex usage and reset-credit endpoints, one gift per account, fail closed otherwise."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.reset: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, request, timeout):
        account = request.get_header("Chatgpt-account-id")
        assert account and account.startswith("fake-"), "fixture accounts only"
        with self._lock:
            self.calls.append((request.get_method(), request.full_url, account))
        if request.full_url == WHAM_USAGE_URL:
            limited = account not in self.reset
            days = int(account.rsplit("-", 1)[1])
            return 200, json.dumps({"rate_limit": {
                "limit_reached": limited, "allowed": not limited,
                "primary_window": {"window_minutes": 10080, "used_percent": 100 if limited else 5,
                                   "reset_at": int(time.time() + days * 86400)}},
                "rate_limit_reset_credits": {"available_count": 0 if account in self.reset else 1,
                                             "applicable_available_count": 0 if account in self.reset else 1}}).encode()
        if request.full_url == WHAM_RESET_CREDITS_URL:
            gifts = [] if account in self.reset else [{"id": f"gift-{account}", "status": "available",
                                                       "reset_type": "codex_rate_limits"}]
            return 200, json.dumps({"credits": gifts}).encode()
        assert request.full_url == WHAM_RESET_CREDITS_CONSUME_URL and request.get_method() == "POST"
        self.reset.add(account)
        return 200, b'{"code":"reset","windows_reset":2}'

    def consumed(self) -> list[str]:
        return [account for method, _, account in self.calls if method == "POST"]

    def listed(self) -> list[str]:
        return [account for method, url, account in self.calls if url == WHAM_RESET_CREDITS_URL]


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    root = tmp_path / "state"
    root.mkdir()
    policy = json.loads(Path(DEFAULT_POLICY_PATH).read_text())
    policy["reserve"]["models"] = []
    # Automatic redemption on, as the incident's daemon had it, and no minimum
    # interval to hide behind: only demand and the one-at-a-time rule may stop it.
    policy["reset_credits"] = {"enabled": True, "min_interval_min": 1e-6}
    policy["alerts"]["operator_session"] = "test-operator"
    (root / "policy.json").write_text(json.dumps(policy))
    (root / "work").mkdir()
    service = Daemon(root)
    http = FakeWham()
    service.timers.adapter_factory = lambda provider: CodexAdapter(opener=http)
    for number in range(1, 7):
        home = root / f"codex-{number}"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"tokens": {"account_id": f"fake-{number}",
                                                               "access_token": "FAKE-ONLY"}}))
        service.store.put_lane(Lane(f"codex-{number}", "codex", f"codex:fake-{number}",
                                    Credential("codex", str(home), "home"), str(home), LaneOwner.V2, False))
    try:
        yield service, http
    finally:
        service.close()


def limit(service, number, *, days=None):
    """Lane `codex-<number>` at its weekly limit with a gift banked; its reset is `days` out."""
    lane_id, days = f"codex-{number}", days or number
    now = datetime.now(timezone.utc)
    until = (now + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    service.store.put_closure(Closure(lane_id, "account", until, ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    service.store.add_reading(Reading(lane_id, "account", "seven_day", 1., until, ReadingLabel.PROVIDER,
                                      "fixture", utcnow()))
    service.timers.metadata[lane_id] = {"probe_status": "limited", "verdict": "limited", "limit_reached": True,
                                        "checked_at": utcnow(), "account_key": f"codex:fake-{number}",
                                        "reset_credits": {"available": 1, "applicable": 1}}


def open_lane(service, number, *, measured):
    """Lane `codex-<number>` with room: a fresh 40% reading, or no reading at all."""
    lane_id = f"codex-{number}"
    with service.store.transaction("fixture.open") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND released_at IS NULL", (utcnow(), lane_id))
        tx.execute("DELETE FROM readings WHERE lane_id=?", (lane_id,))
    if measured:
        service.store.add_reading(Reading(lane_id, "account", "seven_day", .4, after(3 * 86400),
                                          ReadingLabel.PROVIDER, "fixture", utcnow()))
    service.timers.metadata[lane_id] = {"probe_status": "ok", "verdict": "ok", "limit_reached": False,
                                        "checked_at": utcnow(), "account_key": f"codex:fake-{number}",
                                        "reset_credits": {"available": 1, "applicable": 1}}


def submit(service, name, **changes):
    prompt = service.root / f"{name}.md"
    prompt.write_text("fixture prompt")
    args = {"request_id": name, "kind": "dispatch", "workdir": str(service.root / "work"),
            "prompt_path": str(prompt), "sandbox": "read-only", "pinned_model": "astra",
            "allow_tmp": True, "caller_session": "fake-session", "name": name, **changes}
    return service.dispatch("submit", args)["job_id"]


def busy(service, lane_id, count=1, model="gpt-6-astra"):
    """`count` running attempts of other jobs on `lane_id`."""
    for n in range(count):
        job_id = f"filler-{lane_id}-{n}"
        service.store.add_job(job_id=job_id, request_id=job_id, payload_digest="digest", kind="dispatch",
                              workdir=str(service.root / "work"), prompt_path="/prompt", sandbox="read-only",
                              state="running", started_at=utcnow())
        service.store.add_attempt(attempt_id=job_id + "/a1", job_id=job_id, seq=1, lane_id=lane_id,
                                  model_requested=model, state="running")


def lanes_of(service, job_id):
    return [row["lane_id"] for row in service.store.list_attempts(job_id)]


def state_of(service, job_id):
    row = service.store.get_job(job_id)
    return row["state"], row["wait_reason"]


def test_incident_replay_one_waiting_job_gets_one_reset_on_its_lane_and_no_more(fleet):
    """C-23.16 (a)-(d): six limited lanes, six gifts, one waiting job: one reset, the job runs there,
    and with that lane at its slot cap and more jobs queued no pass spends another."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    first = submit(service, "first")
    service._admit()
    assert state_of(service, first) == ("waiting", "capacity")

    result = service.timers.reset_credits_cycle()
    assert result["status"] == "confirmed" and result["job_id"] == first
    assert result["lane_id"] == "codex-6"                  # furthest weekly reset (Max, 2026-08-22)
    assert http.consumed() == ["fake-6"] and http.listed() == ["fake-6"]
    service._admit()
    assert lanes_of(service, first) == ["codex-6"]         # the reset reached the job that caused it

    queued = [submit(service, name) for name in ("second", "third", "fourth")]
    for sweep in range(12):
        service._admit()
        verdict = service.timers.reset_credits_cycle()
        assert verdict["status"] == "reset-lane-open" and verdict["reset_lanes"] == ["codex-6"], verdict
        if sweep % 4 == 0:
            service.timers.probe_cycle()                   # the path the incident's redemptions took
    assert http.consumed() == ["fake-6"] and len(service.store.query("SELECT * FROM actions")) == 1
    assert not any(lanes_of(service, job_id) for job_id in queued)
    assert state_of(service, queued[0]) == ("waiting", "capacity")

    # The reset lane is used up (its attempt came back limited): now, and only now,
    # the next waiting job may have the next lane.
    service.store.put_closure(Closure("codex-6", "account", after(6 * 86400), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "attempt"))
    service._admit()
    second = service.timers.reset_credits_cycle()
    assert second["status"] == "confirmed" and second["lane_id"] == "codex-5" and second["job_id"] == queued[0]
    service._admit()
    assert lanes_of(service, queued[0]) == ["codex-5"]
    for _ in range(4):
        service._admit()
        assert service.timers.reset_credits_cycle()["status"] == "reset-lane-open"
    assert http.consumed() == ["fake-6", "fake-5"]


def test_no_waiting_job_spends_nothing_and_lists_nothing(fleet):
    """C-23.16 (a), the incident's first redemption: every lane limited, every gift banked, no work."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    for _ in range(3):
        service._admit()
        assert service.timers.reset_credits_cycle()["status"] == "no-demand"
    service.timers.probe_cycle()
    assert http.consumed() == [] and http.listed() == []
    assert not service.store.query("SELECT * FROM actions")


@pytest.mark.parametrize("measured", [True, False], ids=["measured", "unmeasured"])
@pytest.mark.parametrize("room", ["idle", "busy"])
def test_a_waiting_job_beside_a_lane_with_room_spends_nothing(fleet, measured, room):
    """C-23.16 (b): five lanes limited and one with room, busy or idle, measured or not: no reset."""
    service, http = fleet
    for number in range(1, 6):
        limit(service, number)
    if room == "busy":
        open_lane(service, 6, measured=measured)
        busy(service, "codex-6", 2 if measured else 1)     # the lane's whole slot cap
    else:
        limit(service, 6)                                  # the job is held while the lane is closed...
    job_id = submit(service, "waiting")
    service._admit()
    assert state_of(service, job_id) == ("waiting", "capacity")
    if room == "idle":
        open_lane(service, 6, measured=measured)           # ...and the lane opens before its next look
    for _ in range(4):
        result = service.timers.reset_credits_cycle()
        assert result["status"] == "no-demand"
        assert result["waiting"][0]["verdict"] == ("placeable" if room == "idle" else "lane-has-capacity")
    assert http.consumed() == [] and http.listed() == []
    if room == "idle":
        service.store.update_job(job_id, next_check_at=utcnow())
        service._admit()
        assert lanes_of(service, job_id) == ["codex-6"]


def test_a_freshly_reset_lane_with_a_guessed_clock_counts_as_available(fleet):
    """C-23.16 (b), (d), (e): after an operator's reset, that lane is capacity until measured limited."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    assert service.timers.reset_credits_cycle(target="codex-3")["status"] == "confirmed"
    override = service.timers.actions.confirmed_override("codex-3")
    assert override["clock_source"] == "guessed"
    assert not [row for row in service.timers.snapshot()["readings"]
                if row["lane_id"] == "codex-3" and row["label"] == "provider"]
    busy(service, "codex-3")                               # unmeasured: one slot, taken
    job_id = submit(service, "waiting")
    service._admit()
    assert state_of(service, job_id) == ("waiting", "capacity")
    for _ in range(4):
        result = service.timers.reset_credits_cycle()
        assert result["status"] == "reset-lane-open" and result["reset_lanes"] == ["codex-3"]
    preview = operations.dispatch(service, protocol.OperationsArgs("reset", dry_run=True))
    assert preview["status"] == "reset-lane-open" and preview["dry_run"] is True
    assert http.consumed() == ["fake-3"]


@pytest.mark.parametrize("kept", [True, False], ids=["kept", "not-kept"])
def test_the_reset_lane_goes_to_the_job_it_was_spent_for(fleet, monkeypatch, kept):
    """C-23.16 (c): an older job waiting for a busy Claude lane never takes the lane reset for another."""
    service, http = fleet
    service.store.put_lane(Lane("claude-1", "claude", "claude:fake:org", Credential("claude", "fake-token", "env"),
                                None, LaneOwner.V2, False))
    busy(service, "claude-1", model="claude-sonnet-5")
    for number in range(1, 7):
        limit(service, number)
    older = submit(service, "older", pinned_model=None, task="review", tier="easy")   # sonnet, opus, astra
    younger = submit(service, "younger", tier="standard")                             # astra only
    service._admit()
    assert state_of(service, older) == state_of(service, younger) == ("waiting", "capacity")
    demand = {row["job_id"]: row["verdict"] for row in service._reset_demand()}
    assert demand == {older: "lane-has-capacity", younger: "codex-demand"}
    result = service.timers.reset_credits_cycle()
    assert result["status"] == "confirmed" and result["job_id"] == younger and result["lane_id"] == "codex-6"
    if not kept:
        # Without the reservation the older job, first in admission order, would take it.
        monkeypatch.setattr(daemon_module, "reset_reservations", lambda store, **kwargs: {})
    service.store.update_job(older, next_check_at=utcnow())
    service._admit()
    if kept:
        assert lanes_of(service, younger) == ["codex-6"] and lanes_of(service, older) == []
    else:
        assert lanes_of(service, older) == ["codex-6"] and lanes_of(service, younger) == []
    assert http.consumed() == ["fake-6"]


def test_a_cancelled_job_releases_the_lane_reset_for_it(fleet):
    """C-23.16 (c): the lane is the job's only while it still waits."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    job_id = submit(service, "leaves")
    service._admit()
    assert service.timers.reset_credits_cycle()["status"] == "confirmed"
    assert service.timers.actions.reservations() == {"codex-6": job_id}
    service.kill(protocol.KillArgs(job_id))            # before admission looks again
    assert service.timers.actions.reservations() == {}
    other = submit(service, "other")
    service._admit()
    assert lanes_of(service, other) == ["codex-6"] and lanes_of(service, job_id) == []
    assert http.consumed() == ["fake-6"]


def test_the_state_root_no_reset_marker_refuses_the_daemon(fleet):
    """C-23.16 (f): `<state root>/no-reset` refuses the timer and the operator's lane alike."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    submit(service, "waiting")
    service._admit()
    (service.root / "no-reset").write_text(json.dumps({"reason": "operator hold"}))
    assert service.timers.reset_credits_cycle()["status"] == "inhibited"
    assert service.timers.reset_credits_cycle(target="codex-1")["status"] == "inhibited"
    service.timers.probe_cycle()
    assert http.consumed() == [] and http.listed() == []
    (service.root / "no-reset").unlink()
    assert service.timers.reset_credits_cycle()["status"] == "confirmed"


def test_a_job_no_admission_pass_has_looked_at_is_not_demand(fleet):
    """C-23.16 (a): a wait a restart forgot, or a job held behind an older one, has had no look."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    job_id = submit(service, "waiting")
    service._admit()
    assert [row["job_id"] for row in service._reset_demand()] == [job_id]
    service._capacity_waits.clear()                        # what a restart forgets
    assert service._reset_demand() == []
    assert service.timers.reset_credits_cycle()["status"] == "no-demand"
    assert http.consumed() == [] and http.listed() == []


def test_the_preview_judges_the_same_demand_without_listing_a_credit(fleet):
    """C-23.16 (e), C-19.1: `reset codex --policy --dry-run` reads the queue as the timer would, and spends nothing."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    empty = operations.dispatch(service, protocol.OperationsArgs("reset", dry_run=True))
    assert empty["status"] == "no-demand" and empty["dry_run"] is True
    job_id = submit(service, "waiting")
    service._admit()
    preview = operations.dispatch(service, protocol.OperationsArgs("reset", dry_run=True))
    assert preview["status"] == "would-evaluate" and preview["job_id"] == job_id
    assert preview["candidate_lanes"][0] == "codex-6"
    assert http.calls == [] and not service.store.query("SELECT * FROM actions")


def test_a_reset_confirmed_in_the_middle_of_an_admission_pass_is_kept_for_its_job(fleet, monkeypatch):
    """C-23.16 (c): reservations are read with the capacity they guard, not once per pass."""
    service, http = fleet
    service.store.put_lane(Lane("claude-1", "claude", "claude:fake:org", Credential("claude", "fake-token", "env"),
                                None, LaneOwner.V2, False))
    busy(service, "claude-1", model="claude-sonnet-5")
    for number in range(1, 7):
        limit(service, number)
    older = submit(service, "older", pinned_model=None, task="review", tier="easy")   # sonnet, opus, astra
    younger = submit(service, "younger", tier="standard")                             # astra only
    service._admit()
    service.store.update_job(older, next_check_at=utcnow())
    workspace = service._workspace
    def confirm_mid_pass(job):
        if job["job_id"] == older and not http.consumed():
            assert service.timers.reset_credits_cycle()["job_id"] == younger    # lands after the pass began
        return workspace(job)
    monkeypatch.setattr(service, "_workspace", confirm_mid_pass)
    service._admit()
    assert http.consumed() == ["fake-6"]
    assert lanes_of(service, older) == []           # evaluated after the confirmation, kept off codex-6
    service._admit()                                # the next pass sees the job due, as the reset left it
    assert lanes_of(service, younger) == ["codex-6"] and lanes_of(service, older) == []


def _same_tier_pair(service):
    """A younger Astra job judged first, then an older job of its tier that waits on a busy Claude lane."""
    service.store.put_lane(Lane("claude-1", "claude", "claude:fake:org", Credential("claude", "fake-token", "env"),
                                None, LaneOwner.V2, False))
    busy(service, "claude-1", model="claude-opus-5-5")
    for number in range(1, 7):
        limit(service, number)
    younger = submit(service, "younger", tier="standard")                    # astra only
    service._admit()
    older = submit(service, "older", pinned_model=None, task="review", tier="standard")  # opus, then astra
    with service.store.transaction("fixture.reorder") as tx:
        # Older in FIFO terms: it was in some other wait when the younger one was looked at.
        tx.execute("UPDATE jobs SET created_at='2026-01-01T00:00:00Z' WHERE job_id=?", (older,))
    service._admit()
    assert state_of(service, older) == state_of(service, younger) == ("waiting", "capacity")
    return older, younger


def test_the_job_a_reset_was_spent_for_passes_an_older_job_for_that_lane(fleet):
    """C-6.9, C-23.16 (c): no older job can take the reserved lane, so FIFO does not hold its holder back."""
    service, http = fleet
    older, younger = _same_tier_pair(service)
    demand = {row["job_id"]: row["verdict"] for row in service._reset_demand()}
    assert demand == {older: "lane-has-capacity", younger: "codex-demand"}
    assert service.timers.reset_credits_cycle()["job_id"] == younger
    service.store.update_job(older, next_check_at=utcnow())
    service._admit()
    assert lanes_of(service, younger) == ["codex-6"] and lanes_of(service, older) == []


def test_the_job_a_reset_was_spent_for_keeps_its_place_for_any_other_lane(fleet):
    """C-6.9, C-23.16 (c): routed to a lane other than its reserved one, the holder waits behind the older job."""
    service, http = fleet
    older, younger = _same_tier_pair(service)
    with service.store.transaction("fixture.exclude") as tx:
        tx.execute("UPDATE jobs SET exclusions=? WHERE job_id=?", (json.dumps(["codex-5"]), older))
    assert service.timers.reset_credits_cycle()["job_id"] == younger          # codex-6 kept for it
    open_lane(service, 5, measured=False)                                       # codex-5 opens, sorts first
    for job_id in (older, younger):
        service.store.update_job(job_id, next_check_at=utcnow())
    service._admit()
    assert lanes_of(service, younger) == [] and lanes_of(service, older) == []
    assert service._holds[younger]["reason"] == "behind-older-job" and service._holds[younger]["behind"] == older


@pytest.mark.parametrize("enabled", [True, False])
def test_the_daemon_log_says_what_the_policy_means_for_resets(tmp_path, monkeypatch, enabled):
    """C-23.16 (f): the policy is read once at startup; the log says automatic redemption is on or off."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    root = tmp_path / "state"
    root.mkdir()
    policy = json.loads(Path(DEFAULT_POLICY_PATH).read_text())
    policy["reset_credits"]["enabled"] = enabled
    (root / "policy.json").write_text(json.dumps(policy))
    (root / "no-reset").write_text("{}")
    service = Daemon(root)
    try:
        text = (root / "daemon.log").read_text()
    finally:
        service.close()
    assert ("automatic redemption on" if enabled else "automatic redemption off") in text
    assert f"no-reset marker {root / 'no-reset'}" in text
