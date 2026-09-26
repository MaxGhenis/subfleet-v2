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
from subfleet.capacity import _iso, _time
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


def same(a, b) -> bool:
    """Equal as JSON text: a NaN is not equal to itself as a float."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


#: Numbers json.dumps writes and json.loads reads that SQLite's json_valid refuses
#: (review of 5841d8b: the rewritten reads dropped such payloads).
NOT_FINITE = (float("nan"), float("inf"), float("-inf"))


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
            if rng.random() < .2:
                record["utilization"] = rng.choice(NOT_FINITE)
            event(daemon.store, rng.choice(("probe.state", "probe.state", "timer.run")), record)
        for asked in ("probe:a", "probe:b", "probe:c", "probe:none", "7"):
            assert same(daemon._probe_record(asked), old_probe_record(daemon.store, asked)), asked
    finally:
        daemon.close()


def test_a_probe_record_with_a_nan_is_still_the_newest(tmp_path):
    """The json_valid guard alone would have skipped it and returned the older one."""
    daemon = Daemon(tmp_path / "state")
    try:
        event(daemon.store, "probe.state", {"holder": "probe:a", "state": "reserved"})
        event(daemon.store, "probe.state", {"holder": "probe:a", "state": "running", "utilization": float("nan")})
        event(daemon.store, "probe.state", {"holder": "probe:b", "state": "running", "x": float("inf")})
        assert daemon._probe_record("probe:a")["state"] == "running"
        assert daemon._probe_record("probe:b")["state"] == "running"
    finally:
        daemon.close()


def test_a_probe_payload_that_is_not_json_is_passed_over(tmp_path):
    """No writer makes one (every payload is json.dumps of a dict). The old walk raised
    on reaching it, for every holder older than it; the lookup now passes over it."""
    daemon = Daemon(tmp_path / "state")
    try:
        event(daemon.store, "probe.state", {"holder": "probe:a", "state": "reserved"})
        with daemon.store.transaction("test.seed") as tx:
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                       ("2026-09-25T00:00:00Z", "probe.state", '{"holder":"probe:a",'))
        with pytest.raises(json.JSONDecodeError):
            old_probe_record(daemon.store, "probe:a")
        assert daemon._probe_record("probe:a")["state"] == "reserved"
        assert daemon._probe_record("probe:none") is None
    finally:
        daemon.close()


def test_a_probe_payload_with_a_repeated_key_is_never_another_holders(tmp_path):
    """Review of 1516f3b: SQLite reads a repeated key's first value (the index, and
    json_valid accepts the payload), Python its last. No writer makes such a payload;
    if one were there, the lookup must not return it as another holder's record,
    as the index's hit alone did. It is found under its first value only, so the
    last value's holder gets its previous record (the old walk returned this one).
    Being the newest indexed row under its first value, it also hides that
    holder's older records (C-3.7 names both differences; review of 0e43105)."""
    daemon = Daemon(tmp_path / "state")
    try:
        event(daemon.store, "probe.state", {"holder": "probe:a", "state": "older"})
        with daemon.store.transaction("test.seed") as tx:
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                       ("2026-09-25T00:00:00Z", "probe.state", '{"holder":"probe:b","holder":"probe:a","state":"twice"}'))
        assert daemon.store.one("SELECT json_valid(data_json) ok, json_extract(data_json,'$.holder') first "
                                "FROM events WHERE data_json LIKE '%twice%'") == {"ok": 1, "first": "probe:b"}
        assert daemon._probe_record("probe:b") is None and old_probe_record(daemon.store, "probe:b") is None
        assert daemon._probe_record("probe:a") == {"holder": "probe:a", "state": "older"}
        # probe:b's own older record, written before the repeated-key payload, is hidden.
        with daemon.store.transaction("test.seed") as tx:
            tx.execute("DELETE FROM events WHERE kind='probe.state'")
        event(daemon.store, "probe.state", {"holder": "probe:b", "state": "genuine-older"})
        with daemon.store.transaction("test.seed") as tx:
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                       ("2026-09-25T00:00:00Z", "probe.state", '{"holder":"probe:b","holder":"probe:a","state":"twice"}'))
        assert old_probe_record(daemon.store, "probe:b") == {"holder": "probe:b", "state": "genuine-older"}
        assert daemon._probe_record("probe:b") is None
    finally:
        daemon.close()


def test_the_probe_record_lookup_uses_its_indexes(tmp_path):
    """Review of 5841d8b, finding 3: 8.20 ms a lookup over 6.5k events, 0.08 ms with the index."""
    from subfleet.daemon import PROBE_RECORD
    daemon = Daemon(tmp_path / "state")
    try:
        plan = " | ".join(row["detail"] for row in daemon.store.query("EXPLAIN QUERY PLAN " + PROBE_RECORD, ("x",)))
        assert "USING INDEX events_probe_holder" in plan, plan
        assert "USING INDEX events_not_json" in plan, plan
        assert "SCAN events" not in plan, plan
    finally:
        daemon.close()


def test_the_probe_record_lookup_does_not_grow_with_history(tmp_path):
    """Thousands of other holders' records, and a lookup for one with none (as each
    Timers `probe:timer:*` lease is): SQLite reads a handful of rows, not every one."""
    daemon = Daemon(tmp_path / "state")
    try:
        with daemon.store.transaction("test.seed") as tx:
            tx.executemany("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           [("2026-09-25T00:00:00Z", "probe.state", _json({"holder": f"probe:{n}", "state": "done"}))
                            for n in range(6500)])
        from subfleet.daemon import PROBE_RECORD
        conn = daemon.store.connection
        steps = []
        conn.set_progress_handler(lambda: steps.append(1), 100)
        try:
            with daemon.store.transaction("test.count") as tx:
                assert tx.execute(PROBE_RECORD, ("probe:timer:none",)).fetchall() == []
        finally:
            conn.set_progress_handler(None, 0)
        assert len(steps) < 20, len(steps)            # a scan of 6.5k rows is thousands of steps
    finally:
        daemon.close()


def test_an_existing_store_gains_the_indexes_at_start_whatever_its_payloads(tmp_path):
    """C-3.1: the schema file is applied at every start, so a store written before
    these indexes gets them when the daemon next opens it, even with a payload in it
    that is not JSON; the unguarded index the review proposed fails there."""
    path = tmp_path / "state.sqlite3"
    Store(path).close()
    import sqlite3
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX events_probe_holder")
    raw.execute("DROP INDEX events_not_json")
    for text in ('{"holder":"probe:a","state":"reserved"}', "not json", '{"holder":"probe:a","state":"running","u":NaN}'):
        raw.execute("INSERT INTO events(ts,kind,data_json) VALUES ('2026-09-25T00:00:00Z','probe.state',?)", (text,))
    raw.commit()
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        raw.execute("CREATE INDEX unguarded ON events(json_extract(data_json,'$.holder')) WHERE kind='probe.state'")
    raw.close()
    store = Store(path, readers=2)
    try:
        names = {row["name"] for row in store.query("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"events_probe_holder", "events_not_json"} <= names
        with store.transaction("test.after") as tx:                  # a row that is not JSON still writes
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES ('2026-09-25T00:00:01Z','probe.state','{')")
        assert [row["data_json"] for row in store.query(
            "SELECT data_json FROM events WHERE kind='probe.state' AND NOT json_valid(data_json) ORDER BY event_id")] == [
            "not json", '{"holder":"probe:a","state":"running","u":NaN}', "{"]
    finally:
        store.close()


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

def old_belongs_to_lane(store, action, lane_id):
    """`ResetCredits._belongs_to_lane` before 88fb6d5: a lane row read per question."""
    lane = store.get_lane(lane_id)
    if lane is None:
        return action["subject"] == lane_id
    request = json.loads(action.get("request_json") or "{}")
    account = request.get("account_key")
    if account is not None and account != lane.account_key:
        return False
    if request.get("lane_id") == lane_id or account == lane.account_key:
        return True
    if action["subject"] in (lane_id, lane.account_key):
        return True
    return (action["subject"] == lane.home and
            action["op_key"].startswith(lane.account_key + ":"))


def old_confirmed_override(store, lane_id, *, now):
    """`ResetCredits.confirmed_override` before 88fb6d5, kept as the independent
    reference: it read the reconciliations, the whole reset-credit history and a
    lane row per action itself, on every question (review of 5841d8b: the test
    used to compare the new code with itself)."""
    instant = _time(now)
    reconciled = {json.loads(row["data_json"]).get("action_id") for row in store.query(
        "SELECT data_json FROM events WHERE kind='action.reconciled'")}
    history = store.query("SELECT * FROM actions WHERE kind='reset-credit' ORDER BY created_at,action_id")
    for action in reversed(history):
        if (not old_belongs_to_lane(store, action, lane_id) or action["state"] != "confirmed"
                or action["action_id"] in reconciled):
            continue
        confirmed = _time(action["updated_at"])
        if confirmed + timedelta(days=7) <= instant:
            return None
        return {"action_id": action["action_id"], "confirmed_at": _iso(confirmed),
                "weekly_reset_at": _iso(confirmed + timedelta(days=7)), "clock_source": "guessed"}
    return None


@pytest.mark.parametrize("seed", SEEDS)
def test_a_shared_override_context_answers_as_a_fresh_read(store, tmp_path, seed):
    """The new context-sharing forms, each against the pre-88fb6d5 reference: a
    context shared across lanes, a fresh read per question, and the context a
    view reads in its snapshot, with lane lookups answered from its lane rows."""
    from subfleet.timers import Timers
    rng = random.Random(seed)
    credits = ResetCredits(store, {})
    homes = {lane: str(tmp_path / f"home{lane}") for lane in range(1, 4)}
    for n in range(rng.randrange(0, 25)):
        lane = rng.randrange(1, 5)                                   # codex-4 has no lane row
        state = rng.choice(("confirmed", "confirmed", "unknown", "failed", "pending"))
        updated = (BASE - timedelta(days=rng.choice((0, 1, 6, 8)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        request = rng.choice(({}, {"lane_id": f"codex-{lane}"}, {"account_key": f"codex:acct{lane}"},
                              {"account_key": "codex:other"}))
        subject = rng.choice((f"codex-{lane}", f"codex:acct{lane}", homes.get(lane, "/nowhere")))
        account = rng.choice((f"codex:acct{lane}", f"codex:acct{lane}", "codex:other"))  # a rebound home
        store.add_action(action_id=f"act-{n}", kind=rng.choice(("reset-credit", "reset-credit", "other")),
                         op_key=f"{account}:op{n}", subject=subject,
                         state=state, request_json=json.dumps(request), created_at=updated, updated_at=updated)
        if rng.random() < .2:
            event(store, "action.reconciled", {"action_id": f"act-{n}"})
    context = credits.override_context()
    viewed = Timers(store, tmp_path / "timers", {}).view_rows(store.lane_rows())["overrides"]
    for lane in ("codex-1", "codex-2", "codex-3", "codex-4"):
        expected = old_confirmed_override(store, lane, now=BASE)
        assert credits.confirmed_override(lane, now=BASE, context=context) == expected, lane
        assert credits.confirmed_override(lane, now=BASE) == expected, lane
        assert Timers(store, tmp_path / "timers", {}).actions.confirmed_override(
            lane, now=BASE, context=viewed) == expected, lane


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
                    if rng.random() < .15:
                        data["weight"] = rng.choice(NOT_FINITE)     # json.loads reads it; json_valid does not
                    text = json.dumps(data)
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           (f"2026-09-25T00:00:{n % 60:02d}Z", kind, text))
        for kinds in (SESSION_KINDS, ("session.nudged",), ("session.retired", "session.unretired")):
            for wanted in (None, set(), {"s-1"}, {"s-2", "s-3", "s-9"}, {"5"}):
                assert same(daemon._session_events(kinds, wanted), old_session_events(daemon.store, kinds, wanted))
    finally:
        daemon.close()


def test_a_session_event_with_a_nan_is_still_the_newest(tmp_path):
    daemon = Daemon(tmp_path / "state")
    try:
        event(daemon.store, "session.nudged", {"session_id": "s-1", "n": 1})
        event(daemon.store, "session.nudged", {"session_id": "s-1", "n": 2, "weight": float("nan")})
        for wanted in (None, {"s-1"}):
            assert daemon._session_events(("session.nudged",), wanted)["session.nudged:s-1"]["n"] == 2
    finally:
        daemon.close()


def test_a_session_payload_with_a_repeated_key_is_never_another_sessions(tmp_path):
    """As for probe records: SQL filters and groups by the first `session_id`, the
    loop reads the last; a row is never counted for a session it was not read
    for, and the payload json_valid refuses is never given to json_extract."""
    daemon = Daemon(tmp_path / "state")
    try:
        event(daemon.store, "session.nudged", {"session_id": "s-1", "n": 1})
        with daemon.store.transaction("test.seed") as tx:
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                       ("2026-09-25T00:00:01Z", "session.nudged", '{"session_id":"s-x","session_id":"s-1","n":2}'))
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                       ("2026-09-25T00:00:02Z", "session.nudged", 'not json {'))
        for wanted in ({"s-x"}, {"s-1"}, None):
            found = daemon._session_events(("session.nudged",), wanted)
            assert "session.nudged:s-x" not in found, (wanted, found)
            assert all(entry["session_id"] == key.split(":", 1)[1] for key, entry in found.items())
    finally:
        daemon.close()


def test_the_session_events_read_their_unparsed_rows_by_index(tmp_path):
    daemon = Daemon(tmp_path / "state")
    try:
        seen = []
        query = daemon.store.query
        daemon.store.query = lambda sql, params=(): seen.append((sql, params)) or query(sql, params)
        daemon._session_events(SESSION_KINDS, None)
        daemon._session_events(SESSION_KINDS, {"s-1"})
        del daemon.store.query
        for sql, params in seen:
            plan = " | ".join(row["detail"] for row in daemon.store.query("EXPLAIN QUERY PLAN " + sql, params))
            assert "USING INDEX events_not_json" in plan, plan
    finally:
        daemon.close()
