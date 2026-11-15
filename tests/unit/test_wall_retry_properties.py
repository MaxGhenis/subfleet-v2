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
                       "outcome_class,killed_by,started_at,finished_at,reserved_at,evidence_json) "
                       "VALUES (?,'wall-property',?,'codex-1','fake',?,?,?,?,?,?,?)",
                       (f"wall-property/a{index + 1}", index + 1, row["state"], row["outcome_class"],
                        row["killed_by"], row["started_at"], row["finished_at"], stamp(0),
                        row.get("evidence_json")))


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


# How a generated attempt ends when its provider exits on its own, as (state,
# class): a lane fault is an auth-dead attempt that `Daemon._lane_fault` would
# accept; a plain auth-dead one is not (a pinned lane, a changed workspace).
ENDINGS = {"limited": ("failed", "limited"), "transient": ("failed", "transient"),
           "lane-fault": ("failed", "auth-dead"), "auth-dead": ("failed", "auth-dead"),
           "lost": ("lost", "unknown"), "ok": ("succeeded", "ok"), "unknown": ("failed", "unknown"),
           "content-filter": ("failed", "content-filter"), "cli-too-old": ("failed", "cli-too-old")}
# C-4.5's classes that admit another attempt on this unpinned read-only job,
# subject to `Daemon._attempts_left`; a lane fault is admitted regardless.
RETRIED = {"limited", "transient", "lost"}
SAFETY = 9  # More attempts than any job may have: 5 plus one lane fault.


def wall_stop_ms(service, clock, start, cap):
    """Production `_running_s`: the first millisecond of a live attempt's own
    time at which the wall's sum reaches `cap`, or None if it never does."""
    def reached(ms):
        clock.at = start + timedelta(milliseconds=ms)
        return service._running_s("wall-property") >= cap

    if not reached(cap * 1000):
        return None
    low, high = 0, cap * 1000
    while low < high:
        middle = (low + high) // 2
        low, high = (low, middle) if reached(middle) else (middle + 1, high)
    return low


@PROPERTY
@example(max_attempts=3, cap=10, requested_ms=[9_000, 9_000, 13_000] + [0] * 6,
         missed_ms=[0, 0, 4_000] + [0] * 6, endings=["transient"] * 9)
@example(max_attempts=1, cap=10, requested_ms=[6_000, 20_000] + [0] * 7,
         missed_ms=[0] * 9, endings=["lane-fault", "transient"] + ["limited"] * 7)
@example(max_attempts=2, cap=10, requested_ms=[6_000, 20_000] + [0] * 7,
         missed_ms=[0, 2_000] + [0] * 7, endings=["lost", "limited"] + ["limited"] * 7)
@given(max_attempts=st.integers(1, 5), cap=st.integers(1, 100),
       requested_ms=st.lists(st.integers(0, 200_000), min_size=SAFETY, max_size=SAFETY),
       missed_ms=st.lists(st.integers(0, 10_000), min_size=SAFETY, max_size=SAFETY),
       endings=st.lists(st.sampled_from(sorted(ENDINGS)), min_size=SAFETY, max_size=SAFETY))
def test_wall_retry_provider_time_is_bounded_by_attempts_times_cap(
        ledger, max_attempts, cap, requested_ms, missed_ms, endings):
    """Invariant 3: retries plus one lane fault each buy at most one own cap.

    Production code decides both halves of the bound. `_running_s` decides
    when the wall stops each live attempt, and `_attempts_left` (with
    `_uncharged`) decides whether another attempt is admitted. A provider
    asks for `requested_ms` of its own time, and the daemon may check the
    wall `missed_ms` late (D-WALL1's overrun cases). An overrun counts as
    finalization measures it, with `_running_s(finishing_attempt_id=...)`,
    or as the late stop's cancel record would. The daemon-level case is
    `tests/fake/test_wall_retry.py::test_a_job_whose_every_attempt_ends_transient_is_bounded`.
    """
    service, clock = ledger
    service.store.update_job("wall-property", max_attempts=max_attempts, max_wall_s=cap)
    job = service.store.get_job("wall-property")
    rows, provider_ms, overruns, cursor = [], 0, 0.0, 0
    for index in range(SAFETY):
        seq, ending = index + 1, endings[index]
        start = NOW + timedelta(milliseconds=cursor)
        live = {"state": "running", "outcome_class": None, "killed_by": None,
                "started_at": stamp(cursor * 1000), "finished_at": None}
        load(service, rows + [live])
        stop = wall_stop_ms(service, clock, start, cap)
        late = 0 if ending == "lost" else missed_ms[index]
        own = requested_ms[index]
        stopped = stop is not None and own >= stop + late
        if stopped:
            own = stop + late
            clock.at = start + timedelta(milliseconds=own)
            # The wall's cancel record carries this sum: its excess is the late stop's overrun.
            overruns += max(0.0, service._running_s("wall-property") - cap)
            state, outcome_class, killed_by = "interrupted", "unknown", "max_wall_s"
        else:
            (state, outcome_class), killed_by = ENDINGS[ending], None
        with service.store.transaction(None) as tx:
            moved = service._uncharged(tx, "wall-property", before=seq)
        fault = ending == "lane-fault" and not stopped and moved == 0
        rows.append({**live, "state": state, "outcome_class": outcome_class, "killed_by": killed_by,
                     "finished_at": stamp((cursor + own) * 1000),
                     "evidence_json": '{"lane_fault": {"class": "auth-dead"}}' if fault else None})
        load(service, rows)
        provider_ms += own
        if not stopped and state != "lost":
            # Finalization's own comparison, after its terminal class is written.
            running_s = service._running_s("wall-property", finishing_attempt_id=f"wall-property/a{seq}")
            overruns += max(0.0, running_s - cap)
        with service.store.transaction(None) as tx:
            left = service._attempts_left(tx, job, {"seq": seq})
        if stopped or not (fault or left and ending in RETRIED):
            break
        cursor += own + 60_000  # Waits between attempts spend no wall.
    else:
        pytest.fail(f"admission kept retrying past {SAFETY} attempts")
    with service.store.transaction(None) as tx:
        lane_faults = service._uncharged(tx, "wall-property", before=len(rows) + 1)
    assert lane_faults <= 1
    assert len(rows) <= max_attempts + lane_faults
    assert provider_ms / 1000 <= (max_attempts + lane_faults) * cap + overruns + 1e-6


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
