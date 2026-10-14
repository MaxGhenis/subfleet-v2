"""C-9.6 (D-HOLD1): an operator hold outlives every limit on its lane, and a limit outlives the hold's release.

`Store.put_closure` kept one open closure per lane and scope. A weekly limit
that an attempt reported on a held lane rewrote the hold's row as its own; the
usage read that later saw the window reset released that row, and the lane was
admitted hours before the hold's end. The reverse merge kept a longer hold's
row and dropped a limit that ended sooner, so `lanes release` opened a lane
that was still limited. Holds and limits now keep separate rows.

Everything here runs the code that does it: the operator's verbs are the
daemon's own handlers (`Daemon._hold_lane`, `Daemon._release_lane_limits`) on
a stand-in that carries only the store; the usage read is `Timers.probe_cycle`
over a fake provider (and `Timers._persist`, to publish a reset as if a read had
slipped past the hold); a reset credit's release is `ResetCredits._release`; an
attempt's limit goes through `Store.add_closure` as finalization records it; and
"admission candidate" is `scheduler.evaluate` choosing the lane from
`capacity.from_store`.

The invariant, for any interleaving of hold, limit (ending before or after the
hold), reset, reset credit, `lanes release --limits`, `lanes release` and clock
advance: the lane is an admission candidate only if no operator hold and no
limit is in force; and its account-scope `closed:` refusals name exactly the
hold and the limit in force. The model judging it is C-9.6's: a hold lasts to
its `until` or `lanes release`; the limits share one row, which a later report
extends (taking that report's reason and clock) and which a reset (a
`provider-limit` row), a reset credit, or `lanes release --limits` (a guessed
clock) releases. A held lane is never read (C-18.1).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, example, find, given, settings, strategies as st

import subfleet.store as store_module
from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet.capacity import from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import Daemon
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate
from subfleet.store import Store
from subfleet.timers import Timers, iso
from tests.caps import capped

START = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
LANE = "codex-1"
JOB = {"job_id": "job", "task": None, "tier": "hard", "pinned_model": "astra", "sandbox": "read-only",
       "exclusions": []}


def _route_policy() -> dict:
    """The shipped policy with no reserve (as `test_reset_credit_properties`)."""
    loaded = capped(load_policy(DEFAULT_POLICY_PATH))
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}
    return loaded


ROUTE_POLICY = _route_policy()
TIMER_POLICY = {"models": {"haiku": {"id": "claude-haiku-4-5-20251001"}},
                "timers": {"probe_interval_s": 300, "keepalive_interval_s": 18300},
                "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}


class Clock:
    def __init__(self):
        self.at = START

    def __call__(self):
        return self.at


class Provider:
    """Codex's usage read for the lane: the window open, or used up until `limited_until`."""

    def __init__(self, clock: Clock):
        self.clock, self.calls, self.limited_until = clock, [], None

    def probe_status(self, lane, env):
        self.calls.append(lane.lane_id)
        return self.answer(lane)

    def answer(self, lane) -> dict:
        now = self.clock()
        if self.limited_until is not None:
            return {"status": "limited", "limit_reached": True, "readings": (
                Reading(lane.lane_id, "account", "seven_day", 1., iso(self.limited_until), ReadingLabel.PROVIDER,
                        "wham", iso(now)),)}
        return {"status": "ok", "limit_reached": False, "readings": (
            Reading(lane.lane_id, "account", "seven_day", .2, iso(now + timedelta(days=5)), ReadingLabel.PROVIDER,
                    "wham", iso(now)),)}


class World:
    """One Codex lane, its store and timers, and the operator's verbs as the daemon runs them."""

    def __init__(self, root: Path, patch: pytest.MonkeyPatch):
        self.clock = Clock()
        self.provider = Provider(self.clock)
        patch.setattr(Path, "home", lambda: root)                       # `Timers._identities` reads ~/.codex
        patch.setattr(daemon_module, "utcnow", lambda: iso(self.clock()))
        self.store = Store(root / "state.sqlite3")
        self.store.__enter__()
        self.timer = Timers(self.store, root, TIMER_POLICY, adapter_factory=lambda _: self.provider, now=self.clock)
        home = root / LANE
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"last_refresh": "first"}))
        self.lane = Lane(LANE, "codex", "codex:" + LANE, Credential("codex", str(home), "home"), str(home),
                         LaneOwner.V2, False, True)
        self.store.put_lane(self.lane)
        self.operator = SimpleNamespace(store=self.store, _notify=lambda: None, _desktop_identity=lambda: None,
                                        _capacity_view=lambda _identity: {"lanes": [], "closures": []})

    def close(self) -> None:
        self.timer.stop()
        self.store.__exit__(None, None, None)

    # -- the verbs ------------------------------------------------------------

    def hold(self, until: datetime) -> None:
        Daemon._hold_lane(self.operator, protocol.LanesArgs(action="hold", lane_id=LANE, until=iso(until)))

    def release(self) -> None:
        Daemon._hold_lane(self.operator, protocol.LanesArgs(action="release", lane_id=LANE))

    def release_limits(self) -> bool:
        try:
            Daemon._release_lane_limits(self.operator, protocol.LanesArgs(action="release-limits", lane_id=LANE,
                                                                          note="operator: a false match"))
        except protocol.ProtocolError:
            return False
        return True

    def attempt_limit(self, until: datetime, reason=ClosureReason.PROVIDER_LIMIT, clock=ClockSource.REPORTED) -> None:
        """A limit an attempt already running on the lane reports, as finalization records it."""
        self.store.add_closure(Closure(LANE, "account", iso(until), reason, clock, "attempt"))

    def read(self, limited_until: datetime | None = None) -> None:
        """The timer's usage read: the window used up until `limited_until`, or open (a reset)."""
        self.provider.limited_until = limited_until
        self.timer.probe_cycle()

    def publish_reset(self) -> None:
        """An open-window read published whatever holds the lane, as no timer would."""
        self.provider.limited_until = None
        self.timer._persist(self.lane, self.provider.answer(self.lane))

    def credit_release(self) -> None:
        """What a confirmed reset credit releases (C-23.17)."""
        self.timer.actions._release(LANE, iso(self.clock()), action_id="test-credit")

    # -- what admission sees --------------------------------------------------

    def decision(self, at: datetime | None = None):
        return evaluate(ROUTE_POLICY, from_store(self.store, now=at or self.clock()), JOB)

    def candidate(self, at: datetime | None = None) -> bool:
        return self.decision(at).chosen_lane == LANE

    def closed(self, at: datetime | None = None) -> set[str]:
        """The `until`s of the account-scope `closed:` refusals admission gives the lane."""
        return {reason.removeprefix("closed:account:") for evaluation in self.decision(at).evaluations
                for rejection in evaluation["rejections"] if rejection["lane_id"] == LANE
                for reason in rejection["reasons"] if reason.startswith("closed:account:")}

    def open_rows(self) -> list[dict]:
        """The lane's closures in force now: not released and not yet ended."""
        return self.store.query("SELECT reason,until_at FROM closures WHERE lane_id=? AND released_at IS NULL "
                                "AND until_at>? ORDER BY closure_id", (LANE, iso(self.clock())))


@pytest.fixture
def world(tmp_path, monkeypatch):
    made = World(tmp_path, monkeypatch)
    try:
        yield made
    finally:
        made.close()


def hours(value: float) -> timedelta:
    return timedelta(hours=value)


# --- the store ---------------------------------------------------------------

@pytest.mark.parametrize("hold_first", [True, False])
def test_a_hold_and_a_limit_on_one_scope_keep_two_rows_each_extended_by_its_own_kind(world, hold_first):
    """C-9.6: a hold and a limit never share a row, in either order; each kind still extends and never
    shortens its own row, and a limit's later report takes that report's reason and clock."""
    store = world.store

    def put(reason, end, clock=ClockSource.REPORTED):
        return store.put_closure(Closure(LANE, "account", iso(START + hours(end)), reason, clock, "test"))

    first, second = ((ClosureReason.OPERATOR_HOLD, 6), (ClosureReason.PROVIDER_LIMIT, 72))
    if not hold_first:
        first, second = second, first
    ids = {first[0]: put(*first), second[0]: put(*second)}
    assert len(set(ids.values())) == 2
    assert put(ClosureReason.OPERATOR_HOLD, 2) == ids[ClosureReason.OPERATOR_HOLD]               # never shortens
    assert put(ClosureReason.OPERATOR_HOLD, 8) == ids[ClosureReason.OPERATOR_HOLD]               # extends
    assert put(ClosureReason.CREDITS, 1) == ids[ClosureReason.PROVIDER_LIMIT]                    # never shortens
    assert put(ClosureReason.CREDITS, 96, ClockSource.GUESSED) == ids[ClosureReason.PROVIDER_LIMIT]
    assert put(ClosureReason.AUTH_DEAD, 1) not in ids.values()                                   # its own row too
    assert sorted(world.open_rows(), key=lambda row: row["reason"]) == [
        {"reason": "auth-dead", "until_at": iso(START + hours(1))},
        {"reason": "credits", "until_at": iso(START + hours(96))},
        {"reason": "operator-hold", "until_at": iso(START + hours(8))}]
    [limit] = store.query("SELECT clock_source FROM closures WHERE reason='credits'")
    assert limit["clock_source"] == "guessed"


# --- the defect's two shapes ---------------------------------------------------

@pytest.mark.parametrize("release", ["usage-read reset", "reset credit", "lanes release --limits"])
def test_a_hold_outlives_a_later_limit_that_a_reset_releases(world, release):
    """D-HOLD1: hold to +6 h, an attempt then reports a weekly limit to +3 d, and the limit is released
    (a reset published as if the held lane had been read, a reset credit, or `lanes release --limits` on
    a guessed clock). Before the fix the limit had taken over the hold's row, so the lane was admitted at
    once; now it stays held to +6 h and is admitted just after."""
    world.read()
    assert world.candidate()
    hold_end = START + hours(6)
    world.hold(hold_end)
    assert not world.candidate()
    clock = ClockSource.GUESSED if release == "lanes release --limits" else ClockSource.REPORTED
    world.attempt_limit(START + hours(72), clock=clock)
    assert [row["reason"] for row in world.open_rows()] == ["operator-hold", "provider-limit"]
    if release == "usage-read reset":
        world.publish_reset()
    elif release == "reset credit":
        world.credit_release()
    else:
        assert world.release_limits()
    assert world.open_rows() == [{"reason": "operator-hold", "until_at": iso(hold_end)}]
    assert world.closed() == {iso(hold_end)}
    assert not world.candidate()
    assert not world.candidate(hold_end - timedelta(seconds=1))
    assert world.candidate(hold_end + timedelta(seconds=1))


def test_the_timer_never_reads_a_held_lane_and_resets_the_limit_once_the_hold_ends(world):
    """D-HOLD1, end to end through the timer: hold to +1 h, a weekly limit to +3 d from an attempt. Before
    the fix the hold's row was gone, so the next cycle read the lane, saw the window reset and opened it
    under the hold. Now no cycle reads it while held (C-18.1); the first read after the hold's end sees the
    reset, releases the limit, and the lane is a candidate again."""
    world.read()
    world.hold(START + hours(1))
    world.attempt_limit(START + hours(72))
    calls = len(world.provider.calls)
    world.read()
    assert len(world.provider.calls) == calls
    assert not world.candidate()
    world.clock.at = START + hours(2)
    assert world.closed() == {iso(START + hours(72))}                     # the hold is over, the limit is not
    world.read()
    assert len(world.provider.calls) == calls + 1
    assert world.open_rows() == []
    assert world.candidate()


@pytest.mark.parametrize("limit_end", [2, 72], ids=["limit-ends-before-the-hold", "limit-ends-after-the-hold"])
def test_lanes_release_ends_the_hold_and_leaves_a_later_limit_in_force(world, limit_end):
    """D-HOLD1: hold to +6 h, then an attempt reports a limit; `lanes release` lifts the hold alone. The lane
    opens once the limit ends, not before. A limit ending before the hold (+2 h) used to be swallowed by
    the hold's row, so the release opened a limited lane at once."""
    world.read()
    world.hold(START + hours(6))
    world.attempt_limit(START + hours(limit_end))
    world.release()
    limit = START + hours(limit_end)
    assert world.open_rows() == [{"reason": "provider-limit", "until_at": iso(limit)}]
    assert world.closed() == {iso(limit)}
    assert not world.candidate()
    assert not world.candidate(limit - timedelta(seconds=1))
    assert world.candidate(limit + timedelta(seconds=1))


def test_a_hold_placed_on_a_limited_lane_keeps_its_own_end_after_the_limit_resets(world):
    """C-9.6: the order reversed. A lane limited to +3 d is held to +6 h: the hold gets its own row, so
    the limit's release by a reset leaves the lane held to +6 h; it was once only the hold's event."""
    world.read(limited_until=START + hours(72))
    world.hold(START + hours(6))
    assert [row["reason"] for row in world.open_rows()] == ["provider-limit", "operator-hold"]
    world.publish_reset()
    assert world.closed() == {iso(START + hours(6))}
    assert world.candidate(START + hours(6) + timedelta(seconds=1))


# --- the property ------------------------------------------------------------

@dataclass
class Model:
    """C-9.6 and C-18.1 by construction: the oracle the store is judged against."""

    hold: datetime | None = None
    limit: tuple[datetime, str, str] | None = None        # the one limit row: until, reason, clock

    def held(self, now: datetime) -> bool:
        return self.hold is not None and self.hold > now

    def limited(self, now: datetime) -> bool:
        return self.limit is not None and self.limit[0] > now

    def report(self, until: datetime, reason: str, clock: str) -> None:
        if self.limit is None or until > self.limit[0]:
            self.limit = (until, reason, clock)

    def closed(self, now: datetime) -> set[str]:
        return ({iso(self.hold)} if self.held(now) else set()) | ({iso(self.limit[0])} if self.limited(now) else set())


HOLD_ENDS = (-1, 1, 2, 6, 48)                 # hours from now; -1 is a hold already over
LIMIT_ENDS = (1, 3, 7, 72)
ADVANCES = (1, 30, 90, 240, 600, 3000)         # minutes
REASONS = ("provider-limit", "credits")
CLOCKS = ("reported", "guessed")

STEPS = st.one_of(
    st.tuples(st.just("hold"), st.sampled_from(HOLD_ENDS)),
    st.tuples(st.just("attempt-limit"), st.sampled_from(LIMIT_ENDS), st.sampled_from(REASONS),
              st.sampled_from(CLOCKS)),
    st.tuples(st.just("read-limited"), st.sampled_from(LIMIT_ENDS)),
    st.tuples(st.just("read-reset")),
    st.tuples(st.just("publish-reset")),
    st.tuples(st.just("reset-credit")),
    st.tuples(st.just("release-limits")),
    st.tuples(st.just("release")),
    st.tuples(st.just("advance"), st.sampled_from(ADVANCES)),
)


def play(steps) -> list[str]:
    """Run `steps` on a fresh world; the invariant violations, each naming its step."""
    violations: list[str] = []
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        world = World(Path(directory), patch)
        model = Model()
        try:
            for index, step in enumerate(steps):
                now = world.clock()
                kind = step[0]
                if kind == "hold":
                    until = now + hours(step[1])
                    world.hold(until)
                    model.hold = until if model.hold is None else max(model.hold, until)
                elif kind == "attempt-limit":
                    until = now + hours(step[1])
                    world.attempt_limit(until, ClosureReason(step[2]), ClockSource(step[3]))
                    model.report(until, step[2], step[3])
                elif kind == "read-limited":
                    until = now + hours(step[1])
                    world.read(limited_until=until)
                    if not model.held(now):
                        model.report(until, "provider-limit", "reported")
                elif kind in ("read-reset", "publish-reset"):
                    if kind == "read-reset":
                        world.read()
                    else:
                        world.publish_reset()
                    if (kind == "publish-reset" or not model.held(now)) and model.limit \
                            and model.limit[1] == "provider-limit":
                        model.limit = None
                elif kind == "reset-credit":
                    world.credit_release()
                    model.limit = None
                elif kind == "release-limits":
                    released = world.release_limits()
                    expected = model.limited(now) and model.limit[2] == "guessed"
                    if released != expected:
                        violations.append(f"step {index} {step}: released {released}, expected {expected}")
                    if expected:
                        model.limit = None
                elif kind == "release":
                    world.release()
                    model.hold = None
                else:
                    world.clock.at = now + timedelta(minutes=step[1])
                now = world.clock()
                where = f"step {index} {step} at +{(now - START).total_seconds() / 3600:g} h"
                if world.candidate() and (model.held(now) or model.limited(now)):
                    violations.append(f"{where}: an admission candidate while held {model.held(now)}, "
                                      f"limited {model.limited(now)}")
                if world.closed() != model.closed(now):
                    violations.append(f"{where}: closed for {sorted(world.closed())}, "
                                      f"expected {sorted(model.closed(now))}")
        finally:
            world.close()
    return violations


#: The defect's shapes, run on every pass whatever the generator draws.
HOLD_THEN_LIMIT_THEN_RESET = [("read-reset",), ("hold", 6), ("attempt-limit", 72, "provider-limit", "reported"),
                              ("publish-reset",), ("advance", 240)]
HOLD_THEN_LIMIT_THEN_TIMER_READ = [("hold", 1), ("attempt-limit", 72, "provider-limit", "reported"),
                                   ("read-reset",)]
HOLD_THEN_SHORTER_LIMIT_THEN_RELEASE = [("hold", 6), ("attempt-limit", 3, "credits", "reported"), ("release",)]
HOLD_THEN_GUESSED_LIMIT_THEN_RELEASE_LIMITS = [("hold", 2), ("attempt-limit", 7, "provider-limit", "guessed"),
                                               ("release-limits",)]
SHAPES = (HOLD_THEN_LIMIT_THEN_RESET, HOLD_THEN_LIMIT_THEN_TIMER_READ, HOLD_THEN_SHORTER_LIMIT_THEN_RELEASE,
          HOLD_THEN_GUESSED_LIMIT_THEN_RELEASE_LIMITS)

PROPERTY = settings(max_examples=int(os.environ.get("SUBFLEET_HOLD_EXAMPLES", "100")), deadline=None,
                    database=None, suppress_health_check=[HealthCheck.too_slow])


@PROPERTY
@given(st.lists(STEPS, min_size=1, max_size=10))
@example(HOLD_THEN_LIMIT_THEN_RESET)
@example(HOLD_THEN_LIMIT_THEN_TIMER_READ)
@example(HOLD_THEN_SHORTER_LIMIT_THEN_RELEASE)
@example(HOLD_THEN_GUESSED_LIMIT_THEN_RELEASE_LIMITS)
def test_any_interleaving_admits_the_lane_only_with_no_hold_and_no_limit_in_force(steps):
    """C-9.6, C-11.2, C-18.1: for any interleaving of hold, limit (ending before or after the hold), reset,
    reset credit, `lanes release --limits`, `lanes release` and clock advance, the lane is an admission
    candidate only if no operator hold and no limit is in force, and it is refused for exactly those."""
    violations = play(steps)
    assert not violations, violations


@pytest.mark.parametrize("shape", SHAPES, ids=["reset", "timer-read", "shorter-limit-release", "release-limits"])
def test_the_property_catches_the_merge_it_replaced(shape, monkeypatch):
    """Not vacuous: with holds merged into limits again (`HOLD_REASONS` empty is release/217's
    `put_closure`), each of the defect's shapes breaks the invariant."""
    assert not play(shape)
    monkeypatch.setattr(store_module, "HOLD_REASONS", ())
    assert play(shape)


def test_the_generator_alone_finds_the_merge_it_replaced(monkeypatch):
    """Not vacuous by generation either: with the old merge back and no explicit shape, generated
    interleavings reach a counterexample (in about 40 to 75 of them when measured, well inside 300)."""
    monkeypatch.setattr(store_module, "HOLD_REASONS", ())
    found = find(st.lists(STEPS, min_size=1, max_size=10), lambda steps: bool(play(steps)),
                 settings=settings(max_examples=300, deadline=None, database=None,
                                   suppress_health_check=list(HealthCheck)))
    assert play(found)
