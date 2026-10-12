"""C-5.7a: a probe's newest record is found by index, and is what a full scan finds.

`_probe_record` used to fetch every probe.state event ever written and parse
each in Python until one named the holder (8,058 such events in the live store
on 2026-09-27, never pruned), on every admission pass for every probe lease and
in every capacity view. It is now the release line's C-3.7 lookup: one step of
the partial index `events_probe_holder`, plus the rare payloads SQLite does not
read as JSON. The old walk is kept here as the reference, and the properties
compare the two over any history a writer can produce (differential tests).

`_save_probe` appends a record only when it differs from the newest its holder
already has, so a record written twice leaves one row.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet.daemon import PROBE_RECORD, Daemon, probe_evidence
from subfleet.store import Store, _json


class Probes:
    """Just the store half of the daemon: its two probe-record methods, unchanged."""

    _probe_record = Daemon._probe_record
    _save_probe = Daemon._save_probe

    def __init__(self, store: Store):
        self.store = store


@contextmanager
def fresh_store():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "state.sqlite3")
        try:
            yield store
        finally:
            store.close()


def old_probe_record(store: Store, holder):
    """The walk this replaced, verbatim: every probe.state event, newest first."""
    for row in store.query("SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id DESC"):
        record = json.loads(row["data_json"])
        if record.get("holder") == holder:
            return record
    return None


def insert(store: Store, kind: str, text: str) -> None:
    with store.transaction("test.seed") as tx:
        tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)", ("2026-09-27T00:00:00Z", kind, text))


def same(a, b) -> bool:
    """Equal as the store would write them: NaN is not equal to itself in Python."""
    return _json(a) == _json(b)


# --- the property: equal to a full scan ------------------------------------------------

HOLDERS = ["probe:a", "probe:b", "probe:c", "probe:timer:x", 7, None]
SCALARS = st.one_of(st.none(), st.booleans(), st.integers(-2**40, 2**40), st.text(max_size=6),
                    st.floats(allow_nan=True, allow_infinity=True))
PAYLOADS = st.builds(
    lambda holder, fields, has_holder: ({"holder": holder} if has_holder else {}) | fields,
    st.sampled_from(HOLDERS),
    st.dictionaries(st.sampled_from(["state", "u", "containment", "n"]),
                    st.one_of(SCALARS, st.dictionaries(st.text(max_size=3), SCALARS, max_size=3)),
                    max_size=3),
    st.booleans())
WRITES = st.lists(st.tuples(st.sampled_from(["probe.state", "probe.state", "probe.state", "timer.run"]),
                            PAYLOADS, st.booleans()),
                  max_size=40)


@settings(max_examples=150, deadline=None)
@given(writes=WRITES)
def test_c5_7a_the_newest_record_for_any_holder_is_what_a_full_scan_finds(writes):
    """For any history of payloads a writer can make (json.dumps of a dict,
    NaN and Infinity included, holders that are not strings or are missing,
    other kinds interleaved, `add_event`'s audit rows among them), the indexed
    lookup returns exactly what the old walk of every probe.state event did."""
    with fresh_store() as store:
        for kind, payload, through_add_event in writes:
            if through_add_event:
                store.add_event(kind, data=payload)       # also writes an audit row of the same kind
            else:
                insert(store, kind, _json(payload))
        probes = Probes(store)
        # The domain is a lease holder: `leases.holder TEXT NOT NULL`, or a
        # `probe:...` string minted beside it. Payload holders range wider (7,
        # None, missing) to show they never answer for a string.
        for asked in [holder for holder in HOLDERS if isinstance(holder, str)] + ["probe:none", "7", ""]:
            assert same(probes._probe_record(asked), old_probe_record(store, asked)), asked


def test_asking_for_no_holder_is_outside_the_domain_and_finds_nothing():
    """Intended divergence, found by the property above and minimized to one
    payload: the old walk answered `None` with the newest payload that has no
    `holder` key (`add_event`'s audit rows are all such), because `.get` gives
    None; SQL's `= NULL` matches nothing. No caller asks it: every one passes a
    lease's holder, which the schema makes NOT NULL, or a `probe:` string."""
    with fresh_store() as store:
        store.add_event("probe.state", data={"state": "no holder"})
        assert old_probe_record(store, None) == {}                  # the audit row, newest
        assert Probes(store)._probe_record(None) is None


# --- the property: a save writes exactly when the record changed -------------------------

RECORDS = st.lists(st.tuples(st.sampled_from(["probe:a", "probe:b", "probe:c"]),
                             st.sampled_from(["reserved", "starting", "quarantined", "contained"]),
                             st.sampled_from([[], [900003], [900003, 900004]]),
                             st.sampled_from(["S", "R", "S+"]),
                             st.sampled_from([None, 0.5, float("nan")])),
                   min_size=1, max_size=40)


@settings(max_examples=150, deadline=None)
@given(saves=RECORDS)
def test_c5_7a_a_probe_record_is_appended_exactly_when_it_changed(saves):
    """Model: the newest record kept per holder. A save appends one probe.state
    record exactly when it differs from that (a NaN included, compared as it
    is written), and the lookup then returns the model's newest record."""
    with fresh_store() as store:
        probes = Probes(store)
        newest: dict[str, dict] = {}
        appended: dict[str, int] = {}
        for holder, state, pids, stat, extra in saves:
            record = {"holder": holder, "job_id": None, "lane_id": "codex-1", "state": state,
                      "containment": {"live_pids": pids,
                                      "shapes": {str(pid): {"ppid": 1, "pgid": pid, "stat": stat} for pid in pids}},
                      "u": extra}
            changed = holder not in newest or not same(newest[holder], record)
            assert probes._save_probe(record) is changed
            if changed:
                newest[holder] = record
                appended[holder] = appended.get(holder, 0) + 1
        for holder in ("probe:a", "probe:b", "probe:c"):
            rows = [row for row in store.query("SELECT data_json FROM events WHERE kind='probe.state'")
                    if json.loads(row["data_json"]).get("holder") == holder]
            assert len(rows) == appended.get(holder, 0)
            assert same(probes._probe_record(holder), newest.get(holder))


def test_c5_7a_probe_evidence_ignores_only_run_states():
    """The recheck's comparison: a pid's `stat` is not evidence; everything else is."""
    identity = {"pid": 3, "boot_id": "boot", "proc_start": "Sun Sep 27 09:00:00 2026"}
    base = {"holder": "probe:a", "state": "quarantined", "child_pid": 9,
            "owned_identities": {"3": identity},
            "containment": {"group_pids": [], "descendant_pids": [], "marker_pids": [3], "live_pids": [3],
                            "errors": [], "unverifiable": False, "identities": {"3": identity},
                            "shapes": {"3": {"ppid": 1, "pgid": 3, "stat": "S"}}}}

    def varied(**containment):
        return {**base, "containment": {**base["containment"], **containment}}
    reused = {**identity, "proc_start": "Sun Sep 27 10:00:00 2026"}
    assert probe_evidence(base) == probe_evidence(varied(shapes={"3": {"ppid": 1, "pgid": 3, "stat": "R+"}}))
    for other in (varied(shapes={"3": {"ppid": 2, "pgid": 3, "stat": "S"}}),     # reparented
                  varied(live_pids=[3, 4]), varied(errors=["marker enumeration unavailable"]),
                  varied(unverifiable=True), {**base, "state": "contained"}, {**base, "child_pid": 10},
                  varied(identities={"3": reused}),                              # the pid was reused
                  varied(marker_pids=[], group_pids=[3]),                        # same pid, another source
                  {**base, "owned_identities": {}}):                             # authority changed
        assert probe_evidence(base) != probe_evidence(other)
    assert probe_evidence(None) is None
    assert probe_evidence({"holder": "probe:a", "containment": None}) == _json({"holder": "probe:a", "containment": None})
    assert probe_evidence({"holder": "probe:a", "containment": {"shapes": {"3": "odd"}}}) is not None


# --- examples ported from the release line (C-3.7) --------------------------------------

def test_a_probe_record_with_a_nan_is_still_the_newest(tmp_path):
    """json.dumps writes NaN, json_valid refuses it, so it is not in the index;
    the lookup still finds it, through `events_not_json`."""
    daemon = Daemon(tmp_path / "state")
    try:
        daemon.store.add_event("probe.state", data={"holder": "probe:a", "state": "reserved"})
        daemon.store.add_event("probe.state", data={"holder": "probe:a", "state": "running", "u": float("nan")})
        daemon.store.add_event("probe.state", data={"holder": "probe:b", "state": "running", "x": float("inf")})
        assert daemon._probe_record("probe:a")["state"] == "running"
        assert daemon._probe_record("probe:b")["state"] == "running"
        assert same(daemon._probe_record("probe:a"), old_probe_record(daemon.store, "probe:a"))
    finally:
        daemon.close()


def test_a_probe_payload_that_is_not_json_is_passed_over(tmp_path):
    """No writer makes one; the old walk raised on it, the lookup passes over it."""
    daemon = Daemon(tmp_path / "state")
    try:
        daemon.store.add_event("probe.state", data={"holder": "probe:a", "state": "reserved"})
        insert(daemon.store, "probe.state", '{"holder":"probe:a",')
        with pytest.raises(json.JSONDecodeError):
            old_probe_record(daemon.store, "probe:a")
        assert daemon._probe_record("probe:a")["state"] == "reserved"
        assert daemon._probe_record("probe:none") is None
    finally:
        daemon.close()


def test_a_probe_payload_with_a_repeated_key_is_never_another_holders(tmp_path):
    """Intended divergence, named in C-5.7a: a payload with `holder` twice (no
    writer makes one) is indexed under SQLite's first value and read by Python
    under the last. It is never returned for the wrong holder, and it hides the
    first value's older records, where the old walk found the older one."""
    daemon = Daemon(tmp_path / "state")
    try:
        daemon.store.add_event("probe.state", data={"holder": "probe:a", "state": "older"})
        insert(daemon.store, "probe.state", '{"holder":"probe:b","holder":"probe:a","state":"twice"}')
        assert daemon._probe_record("probe:b") is None and old_probe_record(daemon.store, "probe:b") is None
        assert old_probe_record(daemon.store, "probe:a")["state"] == "twice"      # the walk reads the last value
        assert daemon._probe_record("probe:a") == {"holder": "probe:a", "state": "older"}
        with daemon.store.transaction("test.reset") as tx:
            tx.execute("DELETE FROM events WHERE kind='probe.state'")
        daemon.store.add_event("probe.state", data={"holder": "probe:b", "state": "genuine-older"})
        insert(daemon.store, "probe.state", '{"holder":"probe:b","holder":"probe:a","state":"twice"}')
        assert old_probe_record(daemon.store, "probe:b") == {"holder": "probe:b", "state": "genuine-older"}
        assert daemon._probe_record("probe:b") is None                          # hidden, as C-5.7a says
    finally:
        daemon.close()


def test_the_probe_record_lookup_uses_its_indexes(tmp_path):
    """One index step for the holder and one range for payloads that are not JSON; never a scan."""
    daemon = Daemon(tmp_path / "state")
    try:
        plan = " | ".join(row["detail"] for row in daemon.store.query("EXPLAIN QUERY PLAN " + PROBE_RECORD, ("x",)))
        assert "USING INDEX events_probe_holder" in plan, plan
        assert "USING INDEX events_not_json" in plan, plan
        assert "SCAN events" not in plan, plan
    finally:
        daemon.close()


def test_the_probe_record_lookup_does_not_grow_with_history(tmp_path):
    """Thousands of other holders' records, and a lookup for one with none (as
    each Timers `probe:timer:*` lease is): SQLite reads a handful of rows."""
    daemon = Daemon(tmp_path / "state")
    try:
        with daemon.store.transaction("test.seed") as tx:
            tx.executemany("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           [("2026-09-27T00:00:00Z", "probe.state", _json({"holder": f"probe:{n}", "state": "done"}))
                            for n in range(6500)])
        steps = []
        conn = daemon.store.connection
        conn.set_progress_handler(lambda: steps.append(1), 100)
        try:
            assert daemon.store.query(PROBE_RECORD, ("probe:timer:none",)) == []
            assert len(daemon.store.query(PROBE_RECORD, ("probe:6499",))) == 1
        finally:
            conn.set_progress_handler(None, 0)
        assert len(steps) < 20, len(steps)            # a scan of 6.5k rows is thousands of steps
    finally:
        daemon.close()


def test_an_existing_store_gains_the_indexes_at_start_whatever_its_payloads(tmp_path):
    """C-3.1: the schema file is applied at every start, so a store written before
    these indexes gets them when the daemon next opens it, even with a payload in
    it that is not JSON; an index without the json_valid guard fails there."""
    path = tmp_path / "state.sqlite3"
    Store(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX events_probe_holder")
    raw.execute("DROP INDEX events_not_json")
    for text in ('{"holder":"probe:a","state":"reserved"}', "not json", '{"holder":"probe:a","state":"running","u":NaN}'):
        raw.execute("INSERT INTO events(ts,kind,data_json) VALUES ('2026-09-27T00:00:00Z','probe.state',?)", (text,))
    raw.commit()
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        raw.execute("CREATE INDEX unguarded ON events(json_extract(data_json,'$.holder')) WHERE kind='probe.state'")
    raw.close()
    store = Store(path)
    try:
        names = {row["name"] for row in store.query("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"events_probe_holder", "events_not_json"} <= names
        with store.transaction("test.after") as tx:                  # a row that is not JSON still writes
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES ('2026-09-27T00:00:01Z','probe.state','{')")
        assert [row["data_json"] for row in store.query(
            "SELECT data_json FROM events WHERE kind='probe.state' AND NOT json_valid(data_json) ORDER BY event_id")] == [
            "not json", '{"holder":"probe:a","state":"running","u":NaN}', "{"]
        assert Probes(store)._probe_record("probe:a")["state"] == "running"
    finally:
        store.close()


def test_the_index_definitions_are_the_release_lines():
    """Both lines open one store (`~/.subfleet/state.sqlite3`), and `CREATE INDEX
    IF NOT EXISTS` keeps whichever definition came first, so they must be one."""
    schema = (Path(__file__).resolve().parents[2] / "subfleet" / "store_schema.sql").read_text()
    assert ("CREATE INDEX IF NOT EXISTS events_probe_holder ON events(json_extract(data_json,'$.holder'), "
            "event_id DESC)\n  WHERE kind='probe.state' AND json_valid(data_json);") in schema
    assert ("CREATE INDEX IF NOT EXISTS events_not_json ON events(kind, event_id DESC) "
            "WHERE NOT json_valid(data_json);") in schema
