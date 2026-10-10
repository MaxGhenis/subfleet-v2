"""C-23.16 end to end: the real daemon's admission judges demand; reset credits follow it.

Replays the two incidents against the daemon's own admission pass, reset timer and
probe cycle. Launches are never run (`_admit` only reserves), providers are the fake
adapter, and the Codex subscription endpoints are the real adapter over a fake HTTP
transport (`FakeWham`), so no real credential, endpoint, or credit is touched.

2026-09-22 (UTC): codex-2 at 20:33 with nothing queued or running; codex-3 at 21:03
while codex-2 ran one job; codex-6, codex-1, codex-4 and codex-5 by 00:53 the next
day while three reset lanes still had room. Each record said `no-dispatchable-lanes`:
a lane running one job at its unmeasured slot cap looked exactly like an exhausted
one. The replays run under those caps (`tests/caps.py`).

2026-09-30: a credit was redeemed on a lane under an operator hold.

The replays only use what release/217 already had (`_admit`, `reset_credits_cycle`,
`probe_cycle`, `_hold_lane`), so they run unchanged against it, and fail there.
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
from tests.caps import capped
from tests.fake_adapter import FakeAdapter


class FakeWham:
    """The Codex usage and reset-credit endpoints, one gift per account, fail closed otherwise.

    An account a consume reset reads open (5%) until `limit_again` marks it used up.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.reset: set[str] = set()
        self.used_up: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, request, timeout):
        account = request.get_header("Chatgpt-account-id")
        assert account and account.startswith("fake-"), "fixture accounts only"
        with self._lock:
            self.calls.append((request.get_method(), request.full_url, account))
        if request.full_url == WHAM_USAGE_URL:
            limited = account not in self.reset or account in self.used_up
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


def _policy(root: Path, *, min_interval_min: float = 1e-6) -> None:
    policy = capped(json.loads(Path(DEFAULT_POLICY_PATH).read_text()))
    policy["reserve"]["models"] = []
    # Automatic redemption on, as the incident's daemon had it, with the floor it
    # still named (checked, no longer read). No minimum interval to hide behind
    # unless a case asks for one: only demand and the one-at-a-time rule may stop a spend.
    policy["reset_credits"] = {"enabled": True, "headroom_floor_pct": 15, "min_interval_min": min_interval_min}
    policy["alerts"]["operator_session"] = "test-operator"
    (root / "policy.json").write_text(json.dumps(policy))


def _open(root: Path, http: FakeWham) -> Daemon:
    service = Daemon(root)
    service.timers.adapter_factory = lambda provider: CodexAdapter(opener=http)
    return service


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})


def _fleet(tmp_path, *, min_interval_min: float = 1e-6, lanes: int = 6):
    root = tmp_path / "state"
    root.mkdir()
    _policy(root, min_interval_min=min_interval_min)
    (root / "work").mkdir()
    http = FakeWham()
    service = _open(root, http)
    for number in range(1, lanes + 1):
        home = root / f"codex-{number}"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"tokens": {"account_id": f"fake-{number}",
                                                               "access_token": "FAKE-ONLY"}}))
        service.store.put_lane(Lane(f"codex-{number}", "codex", f"codex:fake-{number}",
                                    Credential("codex", str(home), "home"), str(home), LaneOwner.V2, False))
    return service, http


@pytest.fixture
def fleet(tmp_path, patched):
    service, http = _fleet(tmp_path)
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


def limit_again(service, http, number):
    """A lane reset earlier is used up again: an attempt came back limited, and its usage says so."""
    lane_id = f"codex-{number}"
    http.used_up.add(f"fake-{number}")
    service.store.put_closure(Closure(lane_id, "account", after(6 * 86400), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "attempt"))
    service.timers.metadata[lane_id] = {"probe_status": "limited", "verdict": "limited", "limit_reached": True,
                                        "checked_at": utcnow(), "account_key": f"codex:fake-{number}",
                                        "reset_credits": {"available": 0, "applicable": 0}}


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


def due(service, *job_ids):
    for job_id in job_ids:
        service.store.update_job(job_id, next_check_at=utcnow())


# --- the incidents -------------------------------------------------------------

def test_incident_2026_09_22_no_work_waiting_spends_and_lists_nothing(fleet):
    """C-23.16 (a), the incident's first redemption: every lane limited, every gift banked, nothing queued."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    for _ in range(3):
        service._admit()
        result = service.timers.reset_credits_cycle()
        assert result["status"] in ("no-demand", "no-eligible-lane"), result
    service.timers.probe_cycle()
    assert http.consumed() == [] and http.listed() == []
    assert not service.store.query("SELECT * FROM actions")


def test_incident_2026_09_22_replay_one_reset_at_a_time_for_waiting_work(fleet):
    """C-23.16 (a)-(d): six limited lanes, six gifts. One waiting job gets one reset, on its route's
    furthest-out lane, and runs there; while that lane is busy at its slot cap, with more jobs queued
    and probe cycles running, no pass spends another. Used up again, the next waiting job gets the
    next lane, and only that one."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    first = submit(service, "first")
    service._admit()
    assert state_of(service, first) == ("waiting", "capacity")

    result = service.timers.reset_credits_cycle()
    assert result["status"] == "confirmed" and result["lane_id"] == "codex-6", result
    assert http.consumed() == ["fake-6"]
    service._admit()
    assert lanes_of(service, first) == ["codex-6"]         # the reset reached the job that caused it

    queued = [submit(service, name) for name in ("second", "third", "fourth")]
    for sweep in range(12):
        service._admit()
        verdict = service.timers.reset_credits_cycle()
        assert verdict["status"] in ("reset-lane-open", "no-demand"), verdict
        if sweep % 4 == 0:
            service.timers.probe_cycle()                   # the path most of the incident's redemptions took
        assert http.consumed() == ["fake-6"], (sweep, verdict)
    assert len(service.store.query("SELECT * FROM actions")) == 1
    # Everything placed went to the lane already reset.
    placed = {lane for job_id in queued for lane in lanes_of(service, job_id)}
    assert placed <= {"codex-6"}

    # The reset lane is used up: now, and only now, the next waiting job may have the next lane.
    limit_again(service, http, 6)
    waiting = [job_id for job_id in queued if not lanes_of(service, job_id)]
    assert waiting
    due(service, *waiting)
    service._admit()
    second = service.timers.reset_credits_cycle()
    assert second["status"] == "confirmed" and second["lane_id"] == "codex-5", second
    assert second["job_id"] in waiting
    for sweep in range(6):
        service._admit()
        service.timers.reset_credits_cycle()
        if sweep % 3 == 0:
            service.timers.probe_cycle()
    assert http.consumed() == ["fake-6", "fake-5"]


@pytest.mark.parametrize("ends", ["before-the-limit", "after-the-limit"])
def test_incident_2026_09_30_replay_a_held_lane_never_takes_a_credit(fleet, ends):
    """C-23.16 (e): codex-6, the furthest-out lane, is under an operator hold. The timer spends on the
    waiting job's next lane instead, and the operator's own `reset codex codex-6` is refused.

    `before-the-limit`: the hold ends before the lane's open account limit, so `Store.put_closure`
    keeps the limit's row and the hold is only its `lane.held` event. `after-the-limit`: the
    hold's row replaces the limit's."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    until = after((2 if ends == "before-the-limit" else 9) * 86400)
    service._hold_lane(protocol.LanesArgs(action="hold", lane_id="codex-6", until=until))
    job_id = submit(service, "waiting")
    service._admit()
    assert state_of(service, job_id) == ("waiting", "capacity")
    named = service.timers.reset_credits_cycle(target="codex-6")
    assert named["status"] in ("lane-held", "no-eligible-lane", "disabled"), named
    assert "fake-6" not in http.consumed() and "fake-6" not in http.listed()
    result = service.timers.reset_credits_cycle()
    assert result["status"] == "confirmed" and result["lane_id"] == "codex-5", result
    for _ in range(3):
        service._admit()
        service.timers.reset_credits_cycle()
    service.timers.probe_cycle()
    assert http.consumed() == ["fake-5"]
    assert "fake-6" not in http.listed()


# --- (a) what is demand --------------------------------------------------------

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
        assert result["status"] in ("no-demand", "no-eligible-lane"), result
        if result["status"] == "no-demand":
            assert result["waiting"][0]["verdict"] == ("placeable" if room == "idle" else "lane-has-capacity")
    assert http.consumed() == [] and http.listed() == []
    if room == "idle":
        due(service, job_id)
        service._admit()
        assert lanes_of(service, job_id) == ["codex-6"]


def test_a_job_no_admission_pass_has_looked_at_is_not_demand(fleet):
    """C-23.16 (a): a wait a restart forgot has had no look."""
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


@pytest.mark.parametrize("door", ["machine-busy", "disk"])
def test_a_job_held_at_the_door_is_not_codex_demand(fleet, monkeypatch, door):
    """C-23.16 (a): a job admission holds for the machine's load or its disk is not waiting on a lane,
    whatever its last route look found; a reset would not let it run."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    job_id = submit(service, "waiting")
    service._admit()
    assert [row["verdict"] for row in service._reset_demand()] == ["codex-demand"]
    if door == "machine-busy":
        monkeypatch.setattr(daemon_module.scheduler, "machine_hold",
                            lambda policy, reading, klass: {"reason": "machine-busy", "class": klass})
    else:
        monkeypatch.setattr(service, "_disk_hold", lambda klass: {"reason": "disk", "class": klass})
    service._admit()
    assert service._holds[job_id]["reason"] == door
    assert service._reset_demand() == []
    for _ in range(3):
        assert service.timers.reset_credits_cycle()["status"] == "no-demand"
    assert http.consumed() == [] and http.listed() == []


def test_the_state_root_no_reset_marker_refuses_the_daemon(fleet):
    """C-23.16 (f): `<state root>/no-reset` refuses the timer and the operator's lane alike, listing included."""
    service, http = fleet
    for number in range(1, 7):
        limit(service, number)
    submit(service, "waiting")
    service._admit()
    (service.root / "no-reset").write_text(json.dumps({"reason": "operator hold"}))
    assert service.timers.reset_credits_cycle()["status"] == "inhibited"
    assert service.timers.reset_credits_cycle(target="codex-1")["status"] == "inhibited"
    preview = operations.dispatch(service, protocol.OperationsArgs("reset", dry_run=True))
    assert preview["status"] == "inhibited"
    service.timers.probe_cycle()
    assert http.consumed() == [] and http.listed() == []
    (service.root / "no-reset").unlink()
    assert service.timers.reset_credits_cycle()["status"] == "confirmed"


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
    named = operations.dispatch(service, protocol.OperationsArgs("reset", dry_run=True, target="codex-2"))
    assert named["status"] == "would-evaluate" and named["candidate_lanes"] == ["codex-2"]
    assert http.calls == [] and not service.store.query("SELECT * FROM actions")


# --- (d) a restart grants nothing ----------------------------------------------

def test_a_restart_grants_no_extra_redemption(tmp_path, patched):
    """C-23.16 (d): the minimum interval and the one-at-a-time rule are read from the store, so a daemon
    restarted a minute after a spend, with the reset lane used up and a job waiting, spends nothing."""
    service, http = _fleet(tmp_path, min_interval_min=30)
    root = service.root
    try:
        for number in range(1, 7):
            limit(service, number)
        first = submit(service, "first")
        service._admit()
        assert service.timers.reset_credits_cycle()["status"] == "confirmed"
        service._admit()
        assert lanes_of(service, first) == ["codex-6"]
        second = submit(service, "second")
        service._admit()
    finally:
        service.close()
    restarted = _open(root, http)
    try:
        restarted._recover_then_start_timers()
        for number in range(1, 6):
            limit(restarted, number)
        limit_again(restarted, http, 6)
        assert restarted.timers.reset_credits_cycle()["status"] in ("no-demand", "interval-blocked")
        due(restarted, second)
        restarted._admit()
        assert state_of(restarted, second) == ("waiting", "capacity")
        for _ in range(3):
            assert restarted.timers.reset_credits_cycle()["status"] == "interval-blocked"
        assert http.consumed() == ["fake-6"]
    finally:
        restarted.close()


@pytest.mark.parametrize("enabled", [True, False])
def test_the_daemon_log_says_what_the_policy_means_for_resets(tmp_path, patched, enabled):
    """C-23.16 (f): the policy is read once at startup; the log says automatic redemption is on or off."""
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
    line = next(line for line in text.splitlines() if "reset credits: automatic redemption" in line)
    assert line.rstrip().endswith("/no-reset") and "absent" not in line
