"""C-3.7: the rewritten hot reads answer exactly what the old ones did.

Each rewrite moved a search from Python into SQLite, or narrowed what a view
reads, because the old form fetched and parsed a whole history on a hot path:
every reading for each capacity view, every probe.state event per probe lease,
every gate.state event per gate poll, every job.submitted event per `list`,
and the reset-credit history once per lane. The old forms are kept here as
the reference, and each test compares the two over randomized stores
(differential tests; seeded, so a failure reproduces).
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from subfleet import capacity
from subfleet.actions import ResetCredits
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.gate.service import GateError, GateService
from subfleet.store import Store, _json

SEEDS = range(40)
BASE = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "state.sqlite3", readers=2)
    for n in range(1, 4):
        home = str(tmp_path / f"home{n}")
        handle.put_lane(Lane(f"codex-{n}", "codex", f"codex:acct{n}", Credential("codex", home, "home"),
                             home, LaneOwner.V2, False))
    yield handle
    handle.close()


def event(store, kind: str, data: dict, **keys) -> None:
    with store.transaction("test.seed") as tx:
        tx.execute("INSERT INTO events(ts,kind,job_id,lane_id,data_json) VALUES (?,?,?,?,?)",
                   ("2026-09-25T00:00:00Z", kind, keys.get("job_id"), keys.get("lane_id"), _json(data)))


# --- readings -------------------------------------------------------------------

def timestamp(rng: random.Random) -> str:
    instant = BASE - timedelta(seconds=rng.choice((0, 0, 1, 5, 60, 3600, rng.randrange(86400))))
    shape = rng.random()
    if shape < .7:
        return instant.strftime("%Y-%m-%dT%H:%M:%SZ")                        # what utc_now writes
    if shape < .8:
        return instant.isoformat(timespec="seconds")                           # +00:00
    if shape < .9:
        return instant.strftime("%Y-%m-%dT%H:%M:%S.") + f"{rng.randrange(10**6):06d}Z"
    return (instant + timedelta(hours=2)).astimezone(timezone(timedelta(hours=2))).isoformat()


@pytest.mark.parametrize("seed", SEEDS)
def test_the_reading_candidates_hold_every_keys_newest(store, seed):
    rng = random.Random(seed)
    with store.transaction("test.readings") as tx:
        for _ in range(rng.randrange(1, 120)):
            tx.execute("INSERT INTO readings(lane_id,scope,window,utilization,resets_at,label,source,observed_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (f"codex-{rng.randrange(1, 4)}", rng.choice(("account", "model:astra")),
                        rng.choice(("5h", "7d", "seven_day")), rng.choice((None, .1, .5, .99)),
                        rng.choice((None, "2026-09-26T00:00:00Z")),
                        rng.choice(("provider", "unknown", "admission-observed")), "test", timestamp(rng)))
    everything, candidates = store.list_readings(), store.latest_reading_candidates()
    assert len(candidates) <= len(everything)
    assert {row["reading_id"] for row in candidates} <= {row["reading_id"] for row in everything}
    now = BASE + timedelta(seconds=30)
    assert capacity.latest_readings(candidates, now=now) == capacity.latest_readings(everything, now=now)


def test_the_reading_candidates_are_few_when_timestamps_are_canonical(store):
    with store.transaction("test.readings") as tx:
        for n in range(3000):
            tx.execute("INSERT INTO readings(lane_id,scope,window,label,source,observed_at) VALUES (?,?,?,?,?,?)",
                       (f"codex-{1 + n % 3}", "account", ("5h", "7d")[n % 2], "provider", "test",
                        (BASE - timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    assert len(store.latest_reading_candidates()) == 6                          # one per key


def test_a_view_from_the_store_matches_one_from_every_reading(store):
    rng = random.Random(7)
    with store.transaction("test.readings") as tx:
        for _ in range(200):
            tx.execute("INSERT INTO readings(lane_id,scope,window,utilization,label,source,observed_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"codex-{rng.randrange(1, 4)}", "account", rng.choice(("5h", "7d")), .2,
                        "provider", "test", timestamp(rng)))
    now = BASE + timedelta(seconds=30)
    everything = SimpleNamespace(**{name: getattr(store, name) for name in
                                    ("lane_rows", "list_readings", "list_closures", "list_attempts", "list_jobs")})
    assert capacity.from_store(store, now=now) == capacity.from_store(everything, now=now)


# --- probe records ----------------------------------------------------------------

def old_probe_record(store, holder):
    for row in store.query("SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id DESC"):
        record = json.loads(row["data_json"])
        if record.get("holder") == holder:
            return record
    return None


@pytest.mark.parametrize("seed", SEEDS)
def test_the_probe_record_is_the_newest_for_its_holder(tmp_path, seed):
    rng = random.Random(seed)
    daemon = Daemon(tmp_path / "state")
    try:
        holders = ["probe:a", "probe:b", "probe:c", 7, None]
        for n in range(rng.randrange(0, 60)):
            record = {"state": rng.choice(("reserved", "running", "completed", "quarantined")), "n": n}
            holder = rng.choice(holders)
            if holder is not None:
                record["holder"] = holder
            event(daemon.store, rng.choice(("probe.state", "probe.state", "timer.run")), record)
        for asked in ("probe:a", "probe:b", "probe:c", "probe:none", "7"):
            assert daemon._probe_record(asked) == old_probe_record(daemon.store, asked)
    finally:
        daemon.close()


# --- gates ------------------------------------------------------------------------

def old_load(store, gate_id):
    for row in store.query("SELECT data_json FROM events WHERE kind='gate.state' ORDER BY event_id DESC"):
        payload = json.loads(row["data_json"])
        if payload.get("gate_id") == gate_id and "state" in payload:
            return payload["state"]
    raise GateError(f"unknown gate: {gate_id}")


@pytest.mark.parametrize("seed", SEEDS)
def test_a_gate_loads_its_newest_state(store, seed):
    rng = random.Random(seed)
    service = GateService(SimpleNamespace(store=store, root=None))
    for n in range(rng.randrange(0, 40)):
        payload = {"gate_id": rng.choice(("g-1", "g-2", "g-3", 3)), "transition": "t"}
        if rng.random() < .8:
            payload["state"] = {"id": payload["gate_id"], "version": n, "status": rng.choice(("reviewing", "agreed"))}
        event(store, rng.choice(("gate.state", "gate.state", "gate.other")), payload)
    for asked in ("g-1", "g-2", "g-3", "g-none", "3"):
        try:
            expected = old_load(store, asked)
        except GateError as exc:
            with pytest.raises(GateError, match=str(exc)):
                service._load(asked)
        else:
            assert service._load(asked) == expected


# --- submission records -------------------------------------------------------------

def test_batches_and_submissions_are_found_by_job_id(tmp_path):
    daemon = Daemon(tmp_path / "state")
    try:
        for n in range(30):
            job = f"20260925-{n:06d}-sub"
            data = {"caller": {"pid": n}}
            if n % 3 == 0:
                data["batch"] = {"label": f"b{n}"}
            event(daemon.store, "job.submitted", data, job_id=job)
            event(daemon.store, "job.submitted", {"later": True}, job_id=job)   # the first one counts
        ids = [f"20260925-{n:06d}-sub" for n in range(30)]
        assert daemon._batches(ids) == {job: {"label": f"b{n}"} for n, job in enumerate(ids) if n % 3 == 0}
        assert daemon._submitted(ids[4]) == {"caller": {"pid": 4}}
        assert daemon._submitted("20260925-999999-none") == {}
        for sql in ("SELECT job_id,data_json FROM events WHERE +kind='job.submitted' AND job_id IN (?)",
                    "SELECT data_json FROM events WHERE job_id=? AND +kind='job.submitted' ORDER BY event_id LIMIT 1"):
            plan = " ".join(row["detail"] for row in daemon.store.query("EXPLAIN QUERY PLAN " + sql, ("x",)))
            assert "events_job" in plan, plan
    finally:
        daemon.close()


# --- reset-credit overrides -----------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_a_shared_override_context_answers_as_a_fresh_read(store, seed):
    rng = random.Random(seed)
    credits = ResetCredits(store, {})
    for n in range(rng.randrange(0, 25)):
        lane = rng.randrange(1, 5)                                   # codex-4 has no lane row
        state = rng.choice(("confirmed", "confirmed", "unknown", "failed", "pending"))
        updated = (BASE - timedelta(days=rng.choice((0, 1, 6, 8)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        request = rng.choice(({}, {"lane_id": f"codex-{lane}"}, {"account_key": f"codex:acct{lane}"},
                              {"account_key": "codex:other"}))
        store.add_action(action_id=f"act-{n}", kind=rng.choice(("reset-credit", "reset-credit", "other")),
                         op_key=f"codex:acct{lane}:op{n}", subject=rng.choice((f"codex-{lane}", f"codex:acct{lane}")),
                         state=state, request_json=json.dumps(request), created_at=updated, updated_at=updated)
        if rng.random() < .2:
            event(store, "action.reconciled", {"action_id": f"act-{n}"})
    context = credits.override_context()
    for lane in ("codex-1", "codex-2", "codex-3", "codex-4"):
        assert (credits.confirmed_override(lane, now=BASE, context=context)
                == credits.confirmed_override(lane, now=BASE))


# --- session events -------------------------------------------------------------------

SESSION_KINDS = ("session.nudged", "session.revived", "session.retired", "session.unretired")


def old_session_events(store, kinds, session_ids):
    marks = ",".join("?" for _ in kinds)
    latest = {}
    for row in store.query(f"SELECT event_id,kind,ts,data_json FROM events WHERE kind IN ({marks}) "
                           "ORDER BY event_id DESC", kinds):
        try:
            data = json.loads(row["data_json"])
        except (TypeError, ValueError):
            continue
        session = data.get("session_id")
        if not isinstance(session, str) or (session_ids is not None and session not in session_ids):
            continue
        latest.setdefault(f"{row['kind']}:{session}", {**data, "at": row["ts"], "event_id": row["event_id"]})
    return latest


@pytest.mark.parametrize("seed", SEEDS)
def test_session_events_are_the_newest_per_kind_and_session(tmp_path, seed):
    rng = random.Random(seed)
    daemon = Daemon(tmp_path / "state")
    try:
        with daemon.store.transaction("test.sessions") as tx:
            for n in range(rng.randrange(0, 80)):
                kind = rng.choice(SESSION_KINDS + ("tickle",))
                if rng.random() < .05:
                    text = "not json {"
                else:
                    data = {"n": n}
                    session = rng.choice(("s-1", "s-2", "s-3", 5, None, "missing"))
                    if session != "missing":
                        data["session_id"] = session
                    text = json.dumps(data)
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           (f"2026-09-25T00:00:{n % 60:02d}Z", kind, text))
        for kinds in (SESSION_KINDS, ("session.nudged",), ("session.retired", "session.unretired")):
            for wanted in (None, set(), {"s-1"}, {"s-2", "s-3", "s-9"}, {"5"}):
                assert daemon._session_events(kinds, wanted) == old_session_events(daemon.store, kinds, wanted)
    finally:
        daemon.close()
