"""D-WALL-RETRY1: generated attempt ledgers and the wall's two readers.

The reference below deliberately spells out the contract instead of importing
the production exemption constant. The old readers are frozen here for the
unchanged-behaviour property. Every generated ledger lives in a real Store;
no provider, user state, or process inspection is involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet.contracts import AttemptState, OutcomeClass
from subfleet.daemon import Daemon
from subfleet.store import Store


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
LIVE = ("reserved", "starting", "running", "finalizing")
TERMINAL = tuple(state.value for state in AttemptState if state.value not in LIVE)
CLASSES = (None, *(outcome.value for outcome in OutcomeClass))
STOPS = (None, "max_wall_s", "client-request")
PROPERTY = settings(max_examples=200, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])

# Frozen from a8624a4899a2: keep the old SQL independent of LIVE_TICK.
OLD_LIVE_TICK = (
    "SELECT a.attempt_id,a.job_id,a.seq,a.state,j.cancel_requested_at,"
    "a.started_at,a.reserved_at,j.max_wall_s,"
    "COALESCE((SELECT SUM(MAX(0,unixepoch(p.finished_at,'subsec')-"
    "unixepoch(p.started_at,'subsec')+0.001)) FROM attempts p WHERE p.job_id=a.job_id "
    "AND p.attempt_id<>a.attempt_id AND p.state NOT IN "
    "('reserved','starting','running','finalizing')),0.0) AS prior_running_s "
    "FROM attempts a LEFT JOIN jobs j USING(job_id) "
    "WHERE a.state IN ('reserved','starting','running','finalizing')"
)


def stamp(microseconds: int) -> str:
    return (NOW + timedelta(microseconds=microseconds)).isoformat()


def elapsed(start: str, end: str) -> float:
    return (datetime.fromisoformat(end.replace("Z", "+00:00")) -
            datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def exempt(row: dict) -> bool:
    return (row["state"] in {"succeeded", "failed", "interrupted", "lost", "cancelled"}
            and row["outcome_class"] in {"limited", "transient", "auth-dead"}
            and row["killed_by"] is None)


def reference_running_s(rows: list[dict], now: datetime = NOW) -> float:
    """The three-part exclusion rule, with the established timestamp rules."""
    total = 0.0
    for row in rows:
        if row["state"] == "reserved" or exempt(row):
            continue
        end = row["finished_at"]
        if end is None and row["state"] in {"starting", "running"}:
            end = now.isoformat()
        if row["started_at"] is not None and end is not None:
            total += max(0.0, elapsed(row["started_at"], end))
    return total


def old_running_s(rows: list[dict], now: datetime = NOW) -> float:
    """Frozen a8624a4899a2 _running_s, including its timestamp semantics."""
    total = 0.0
    for row in rows:
        if row["state"] == "reserved":
            continue
        start = row["started_at"]
        end = row["finished_at"] or (now.isoformat() if row["state"] in ("starting", "running") else None)
        if start and end:
            total += max(0.0, elapsed(start, end))
    return total


@st.composite
def attempt(draw, *, states=TERMINAL, milliseconds=False):
    # Independent, missing and reversed timestamps exercise legacy/incomplete rows.
    quantum = 1000 if milliseconds else 1
    start = draw(st.one_of(st.none(), st.integers(-100_000_000, 10_000_000)))
    finish = draw(st.one_of(st.none(), st.integers(-100_000_000, 10_000_000)))
    return {"state": draw(st.sampled_from(states)),
            "outcome_class": draw(st.sampled_from(CLASSES)),
            "killed_by": draw(st.sampled_from(STOPS)),
            "started_at": None if start is None else stamp(start * quantum),
            "finished_at": None if finish is None else stamp(finish * quantum)}


@st.composite
def sequences(draw, *, milliseconds=False):
    rows = draw(st.lists(attempt(milliseconds=milliseconds), max_size=7))
    if draw(st.booleans()):
        rows.append(draw(attempt(states=LIVE, milliseconds=milliseconds)))
    return rows


def completed(state="failed", outcome_class=None, killed_by=None, duration=3.0):
    return {"state": state, "outcome_class": outcome_class, "killed_by": killed_by,
            "started_at": stamp(-round(duration * 1_000_000)), "finished_at": stamp(0)}


def running(outcome_class=None, duration=2.0, state="running"):
    return {"state": state, "outcome_class": outcome_class, "killed_by": None,
            "started_at": stamp(-round(duration * 1_000_000)), "finished_at": None}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = Store(tmp_path / "wall.sqlite3", readers=2)
    with store.transaction(None) as tx:
        tx.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,"
                   "created_at,updated_at) VALUES ('codex-1','codex','fake','fake','env',?,?)",
                   (stamp(0), stamp(0)))
    store.add_job(job_id="wall-property", request_id="wall-property", payload_digest="fake",
                  kind="run", workdir=str(tmp_path), prompt_path=str(tmp_path / "prompt.md"),
                  sandbox="read-only", state="running", max_wall_s=100)
    service = Daemon.__new__(Daemon)
    service.store = store

    class Clock(datetime):
        at = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.at if tz else cls.at.replace(tzinfo=None)

    monkeypatch.setattr(daemon_module, "datetime", Clock)
    try:
        yield service, Clock
    finally:
        store.close()


def load(service, rows, order=None):
    """Replace just this fixture's attempts; preserve sequence across permutations."""
    with service.store.transaction(None) as tx:
        tx.execute("DELETE FROM attempts")
        for index in range(len(rows)) if order is None else order:
            row = rows[index]
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,"
                       "outcome_class,killed_by,started_at,finished_at,reserved_at) "
                       "VALUES (?,'wall-property',?,'codex-1','fake',?,?,?,?,?,?)",
                       (f"wall-property/a{index + 1}", index + 1, row["state"], row["outcome_class"],
                        row["killed_by"], row["started_at"], row["finished_at"], stamp(0)))


@PROPERTY
@example(rows=[completed(outcome_class="limited"), running()])
@example(rows=[completed(outcome_class="transient"), running()])
@example(rows=[completed(outcome_class="auth-dead"), running()])
@example(rows=[completed(outcome_class="limited", killed_by="max_wall_s"), running()])
@example(rows=[completed(outcome_class="transient", killed_by="client-request"), running()])
@example(rows=[completed(state="quarantined", outcome_class="limited"), running()])
@example(rows=[completed(state="lost", outcome_class="unknown"), running()])
@example(rows=[completed(outcome_class="unknown"), running()])
@given(rows=sequences())
def test_wall_retry_exclusion_matches_reference(ledger, rows):
    """Invariant 1: only a terminal, unstopped outside fault is removed."""
    service, clock = ledger
    clock.at = NOW
    load(service, rows)
    assert service._running_s("wall-property") == pytest.approx(reference_running_s(rows), abs=1e-9)


@PROPERTY
@example(row=running(outcome_class="limited"))
@example(row=running(outcome_class="transient", state="starting"))
@example(row=running(outcome_class="auth-dead"))
@given(row=attempt(states=("starting", "running", "finalizing")))
def test_wall_retry_live_attempt_keeps_its_own_time(ledger, row):
    """Invariant 2: even an interim outside-fault class cannot remove live time."""
    service, clock = ledger
    clock.at = NOW
    load(service, [row])
    assert service._running_s("wall-property") == pytest.approx(old_running_s([row]), abs=1e-9)


@PROPERTY
@given(rows=sequences(), advance_us=st.integers(0, 100_000_000), added=attempt())
def test_wall_retry_fixed_ledger_and_additions_are_monotone(ledger, rows, advance_us, added):
    """Invariant 4: time and adding a row never lower a fixed ledger's sum."""
    service, clock = ledger
    clock.at = NOW
    load(service, rows)
    before = service._running_s("wall-property")
    clock.at = NOW + timedelta(microseconds=advance_us)
    later = service._running_s("wall-property")
    assert later >= before - 1e-9
    # Insert the terminal addition before a possible live last row.
    augmented = rows[:-1] + [added, rows[-1]] if rows and rows[-1]["state"] in LIVE else rows + [added]
    load(service, augmented)
    after = service._running_s("wall-property")
    assert after >= later - 1e-9
    if exempt(added):
        assert after == pytest.approx(later, abs=1e-9)


@PROPERTY
@example(priors=[completed(outcome_class=None)], own_us=2_000_000)
@example(priors=[completed(outcome_class="limited")], own_us=2_000_000)
@example(priors=[completed(outcome_class="limited", killed_by="max_wall_s")], own_us=2_000_000)
@example(priors=[completed(state="quarantined", outcome_class="limited")], own_us=2_000_000)
@example(priors=[completed(state="lost", outcome_class="unknown")], own_us=2_000_000)
@given(priors=st.lists(attempt(milliseconds=True), max_size=7), own_us=st.integers(0, 100_000_000))
def test_wall_retry_tick_projection_agrees_with_exact_sum(ledger, priors, own_us):
    """Invariant 5: running-row tick projection is early by at most 1 ms/prior.

    The wall prefilter uses age only for a running provider without an exit.
    Guardian receipts have millisecond precision; SQL rounds those canonical
    prior timestamps with at most its documented millisecond allowance.
    Other live states and arbitrary microsecond timestamps are covered by the
    exact-sum properties.
    """
    service, clock = ledger
    clock.at = NOW
    rows = priors + [running(duration=own_us / 1_000_000)]
    load(service, rows)
    projected = service.store.query(daemon_module.LIVE_TICK)[0]["prior_running_s"] + own_us / 1_000_000
    exact = service._running_s("wall-property")
    assert exact == pytest.approx(reference_running_s(rows), abs=1e-9)
    counted = sum(not exempt(row) for row in priors)
    assert projected >= exact - 1e-6
    assert projected - exact <= counted * .001 + 1e-6


@PROPERTY
@given(rows=sequences())
def test_wall_retry_nonexempt_ledgers_match_frozen_readers(ledger, rows):
    """Invariant 6: both frozen readers agree on ledgers unchanged by this rule."""
    service, clock = ledger
    clock.at = NOW
    unchanged = [{**row, "outcome_class": "unknown"} if exempt(row) else row for row in rows]
    load(service, unchanged)
    assert service._running_s("wall-property") == pytest.approx(old_running_s(unchanged), abs=1e-9)
    old = {row["attempt_id"]: row["prior_running_s"] for row in service.store.query(OLD_LIVE_TICK)}
    new = {row["attempt_id"]: row["prior_running_s"] for row in service.store.query(daemon_module.LIVE_TICK)}
    assert new.keys() == old.keys()
    for aid in old:
        assert new[aid] == pytest.approx(old[aid], abs=1e-9)


@PROPERTY
@given(rows=sequences(), data=st.data())
def test_wall_retry_row_order_does_not_change_either_sum(ledger, rows, data):
    """Invariant 7: row insertion/query order changes neither reader's sum."""
    service, clock = ledger
    clock.at = NOW
    load(service, rows)
    exact = service._running_s("wall-property")
    ticks = service.store.query(daemon_module.LIVE_TICK)
    order = data.draw(st.permutations(tuple(range(len(rows)))))
    load(service, rows, order)
    # The job/sequence index can hide insertion order. Force SQLite to reverse
    # unordered scans as well, and keep both readers on this connection.
    with service.store.transaction(None) as tx:
        tx.execute("PRAGMA reverse_unordered_selects=ON")
        try:
            assert service._running_s("wall-property") == pytest.approx(exact, abs=1e-9)
            reversed_ticks = service.store.query(daemon_module.LIVE_TICK)
        finally:
            tx.execute("PRAGMA reverse_unordered_selects=OFF")
    before = {row["attempt_id"]: row for row in ticks}
    after = {row["attempt_id"]: row for row in reversed_ticks}
    assert before.keys() == after.keys()
    for aid, old in before.items():
        assert after[aid]["prior_running_s"] == pytest.approx(old["prior_running_s"], abs=1e-9)
        assert {key: value for key, value in after[aid].items() if key != "prior_running_s"} == {
            key: value for key, value in old.items() if key != "prior_running_s"}


def test_wall_retry_reader_sees_class_written_in_same_transaction(ledger):
    """Finalization's reader shares the writer connection, including with a pool."""
    service, _ = ledger
    load(service, [completed(outcome_class=None)])
    assert service._running_s("wall-property") == 3
    with service.store.transaction(None) as tx:
        tx.execute("UPDATE attempts SET outcome_class='transient'")
        assert service._running_s("wall-property") == 0


@PROPERTY
@given(max_attempts=st.integers(1, 5), lane_fault=st.booleans(), cap=st.integers(1, 100),
       requested_us=st.lists(st.integers(0, 200_000_000), min_size=6, max_size=6),
       missed_wall_us=st.lists(st.integers(0, 10_000_000), min_size=6, max_size=6),
       outcomes=st.lists(st.sampled_from(CLASSES), min_size=6, max_size=6))
def test_wall_retry_provider_time_is_bounded_by_attempts_times_cap(
        ledger, max_attempts, lane_fault, cap, requested_us, missed_wall_us, outcomes):
    """Invariant 3: retries plus one lane fault each buy at most one own cap.

    A delayed enforcement may add the separately generated overrun. Completing
    an outside fault can restore the next attempt's budget but cannot restore
    its own live time. The fake-provider tests cover real retry admission.
    """
    service, clock = ledger
    clock.at = NOW
    rows = []
    provider_time = overruns = 0.0
    allowed_attempts = max_attempts + int(lane_fault)
    for index in range(allowed_attempts):
        prior = reference_running_s(rows)
        remaining = max(0.0, cap - prior)
        normal_time = min(requested_us[index] / 1_000_000, remaining)
        load(service, rows + [running(duration=normal_time)])
        assert service._running_s("wall-property") == pytest.approx(prior + normal_time, abs=1e-6)
        assert normal_time <= cap
        overrun = missed_wall_us[index] / 1_000_000
        duration = normal_time + overrun
        load(service, rows + [running(duration=duration)])
        assert service._running_s("wall-property") >= duration - 1e-6
        outcome = "auth-dead" if lane_fault and index == 0 else outcomes[index]
        rows.append(completed(outcome_class=outcome, duration=duration))
        provider_time += duration
        overruns += overrun
    assert provider_time <= allowed_attempts * cap + overruns + 1e-6


@pytest.mark.parametrize(("durations", "classes", "spent", "remaining"), [
    ((5159, 7986), ("limited", "transient"), 8455, 13145),
    ((5312, 8080), ("limited", "transient"), 8209, 13391),
    ((1169.5,), ("transient",), 20430.9, 1169.1),
])
def test_wall_retry_incident_replay_gives_final_attempt_the_full_cap(durations, classes, spent, remaining):
    priors = [completed(outcome_class=cls, duration=duration) for duration, cls in zip(durations, classes)]
    assert reference_running_s(priors) == 0
    assert 21600 - reference_running_s(priors) == 21600
    assert 21600 - reference_running_s(priors + [running(duration=spent)]) == pytest.approx(remaining)
