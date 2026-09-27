"""C-5.7a: a probe record is written only when it changed, and a recheck compares
records without the run states that say nothing about containment.

`_save_probe` appends a record only when it differs from the newest its holder
already has, so a record written twice leaves one row. `probe_evidence` is what
a recheck compares: the record without each live pid's `stat`.

The lookup these build on is C-3.7's (`PROBE_RECORD`, `events_probe_holder`,
`events_not_json`); its differential and index tests are in
`tests/unit/test_store_contention_queries.py`. Ported from main's PR #50, where
this file also carries those lookup tests; the two kept here are the ones this
line did not have.
"""

from __future__ import annotations

import copy
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet.daemon import (PROBE_RECHECK_BASE_S, PROBE_RECHECK_CEILING_S, Daemon, probe_evidence,
                             probe_recheck_delay)
from subfleet.store import Store, _json


class Probes:
    """Just the store half of the daemon: its two probe-record methods, unchanged."""

    _probe_record = Daemon._probe_record
    _save_probe = Daemon._save_probe

    def __init__(self, store: Store):
        self.store = store


@contextmanager
def fresh_store(readers: int = 0):
    """`readers=0` reads through the writer; the daemon's store keeps read
    connections (C-3.7), so a save outside a transaction compares with the
    newest record through one of them."""
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "state.sqlite3", readers=readers)
        try:
            yield store
        finally:
            store.close()


def old_probe_record(store: Store, holder):
    """The walk C-3.7 replaced, verbatim: every probe.state event, newest first."""
    for row in store.query("SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id DESC"):
        record = json.loads(row["data_json"])
        if record.get("holder") == holder:
            return record
    return None


def same(a, b) -> bool:
    """Equal as the store would write them: NaN is not equal to itself in Python."""
    return _json(a) == _json(b)


def holder_rows(store: Store, holder: str) -> list[dict]:
    return [json.loads(row["data_json"]) for row in store.query(
        "SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id")
        if json.loads(row["data_json"]).get("holder") == holder]


# --- the property: a save writes exactly when the record changed -------------------------

RECORDS = st.lists(st.tuples(st.sampled_from(["probe:a", "probe:b", "probe:c"]),
                             st.sampled_from(["reserved", "starting", "quarantined", "contained"]),
                             st.sampled_from([[], [900003], [900003, 900004]]),
                             st.sampled_from(["S", "R", "S+"]),
                             st.sampled_from([None, 0.5, float("nan")])),
                   min_size=1, max_size=40)


@pytest.mark.parametrize("readers", [0, 2])
@settings(max_examples=150, deadline=None)
@given(saves=RECORDS)
def test_c5_7a_a_probe_record_is_appended_exactly_when_it_changed(readers, saves):
    """Model: the newest record kept per holder. A save appends one probe.state
    record exactly when it differs from that (a NaN included, compared as it
    is written), and the lookup then returns the model's newest record, whether
    the store reads through its writer or through read connections."""
    with fresh_store(readers) as store:
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
            assert len(holder_rows(store, holder)) == appended.get(holder, 0)
            assert same(probes._probe_record(holder), newest.get(holder))


@pytest.mark.parametrize("readers", [0, 2])
def test_c5_7a_a_save_inside_a_transaction_compares_with_its_own_rows(readers):
    """`_contain_probe` saves inside its `probe.quarantined` transaction. There the
    comparison reads the writer (C-3.7: a thread holding the store lock reads
    its own uncommitted rows), so a record saved earlier in the transaction
    counts, and a rolled-back save leaves nothing a later save compares with."""
    with fresh_store(readers) as store:
        probes = Probes(store)
        record = {"holder": "probe:a", "job_id": None, "lane_id": "codex-1", "state": "quarantined"}
        with store.transaction("probe.quarantined"):
            assert probes._save_probe(record) is True
            assert probes._save_probe(dict(record)) is False
        assert len(holder_rows(store, "probe:a")) == 1
        changed = {**record, "state": "contained"}
        with pytest.raises(RuntimeError), store.transaction("probe.quarantined"):
            assert probes._save_probe(changed) is True
            raise RuntimeError("the transaction fails after the save")
        assert holder_rows(store, "probe:a") == [record]
        assert probes._save_probe(changed) is True
        assert holder_rows(store, "probe:a") == [record, changed]


# --- what a recheck compares --------------------------------------------------------------

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


def without_run_states(record: dict) -> dict:
    """The reference `probe_evidence` is checked against: a deep copy with every
    shape's `stat` removed, and nothing else touched."""
    value = copy.deepcopy(record)
    containment = value.get("containment")
    if isinstance(containment, dict) and isinstance(containment.get("shapes"), dict):
        for shape in containment["shapes"].values():
            if isinstance(shape, dict):
                shape.pop("stat", None)
    return value


SHAPES = st.dictionaries(st.sampled_from(["3", "4", "5"]),
                         st.fixed_dictionaries({"ppid": st.sampled_from([1, 2]), "pgid": st.sampled_from([3, 4])},
                                               optional={"stat": st.sampled_from(["S", "R", "R+", "Ss", "U"])}),
                         max_size=3)
CENSUSES = st.fixed_dictionaries(
    {"live_pids": st.lists(st.sampled_from([3, 4, 5]), unique=True, max_size=3),
     "errors": st.lists(st.sampled_from(["group enumeration unavailable", "marker enumeration unavailable"]),
                        unique=True, max_size=2),
     "unverifiable": st.booleans()},
    optional={"shapes": SHAPES})
EVIDENCE_RECORDS = st.fixed_dictionaries(
    {"holder": st.sampled_from(["probe:a", "probe:b"]),
     "state": st.sampled_from(["containing", "quarantined", "contained"])},
    optional={"containment": st.one_of(st.none(), CENSUSES), "child_pid": st.sampled_from([None, 9, 10]),
              "u": st.sampled_from([0.5, float("nan"), float("inf")])})


@settings(max_examples=300, deadline=None)
@given(first=EVIDENCE_RECORDS, second=EVIDENCE_RECORDS)
def test_c5_7a_probe_evidence_is_the_record_without_its_run_states(first, second):
    """For any two records: their evidence is equal exactly when the records are
    equal once every live pid's `stat` is removed (so a run state never counts,
    and anything else always does), and reading it changes neither record."""
    kept = _json(first), _json(second)
    assert (probe_evidence(first) == probe_evidence(second)) == (
        _json(without_run_states(first)) == _json(without_run_states(second)))
    assert (_json(first), _json(second)) == kept, "probe_evidence mutated the record it was given"


@settings(max_examples=100, deadline=None)
@given(record=EVIDENCE_RECORDS, stats=st.lists(st.sampled_from(["S", "R", "R+", "T", "U"]), min_size=3, max_size=3))
def test_c5_7a_any_run_state_reads_as_the_same_evidence(record, stats):
    """The same census with every live pid in another run state is the same evidence."""
    moved = copy.deepcopy(record)
    shapes = (moved.get("containment") or {}).get("shapes") or {}
    for shape, stat in zip(shapes.values(), stats):
        shape["stat"] = stat
    assert probe_evidence(moved) == probe_evidence(record)


# --- the recheck clock --------------------------------------------------------------------

@given(looks=st.integers(min_value=-5, max_value=10_000))
def test_c5_7a_the_recheck_delay_doubles_from_one_second_to_a_minute(looks):
    """1, 2, 4, ... seconds after the 1st, 2nd, 3rd look, capped at 60, never
    below 1 (a count below one is the first look), and never an overflow."""
    delay = probe_recheck_delay(looks)
    assert PROBE_RECHECK_BASE_S <= delay <= PROBE_RECHECK_CEILING_S
    assert delay == min(PROBE_RECHECK_CEILING_S, PROBE_RECHECK_BASE_S * 2 ** (max(looks, 1) - 1))
    assert probe_recheck_delay(looks + 1) >= delay
    assert [probe_recheck_delay(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]


# --- two lookup facts this line had not pinned (C-3.7) -------------------------------------

def test_asking_for_no_holder_is_outside_the_domain_and_finds_nothing():
    """Intended divergence, found on main by a property over the lookup and
    minimized to one payload: the old walk answered `None` with the newest
    payload that has no `holder` key (`add_event`'s audit rows are all such),
    because `.get` gives None; SQL's `= NULL` matches nothing. No caller asks
    it: every one passes a lease's holder, which the schema makes NOT NULL, or
    a `probe:` string minted beside it."""
    with fresh_store() as store:
        store.add_event("probe.state", data={"state": "no holder"})
        assert old_probe_record(store, None) == {}                  # the audit row, newest
        assert Probes(store)._probe_record(None) is None


def test_the_index_definitions_are_the_ones_main_copies():
    """Both lines open one store (`~/.subfleet/state.sqlite3`), and `CREATE INDEX
    IF NOT EXISTS` keeps whichever definition came first. Main copies these two
    statements character for character (its C-5.7a), so they must not drift."""
    schema = (Path(__file__).resolve().parents[2] / "subfleet" / "store_schema.sql").read_text()
    assert ("CREATE INDEX IF NOT EXISTS events_probe_holder ON events(json_extract(data_json,'$.holder'), "
            "event_id DESC)\n  WHERE kind='probe.state' AND json_valid(data_json);") in schema
    assert ("CREATE INDEX IF NOT EXISTS events_not_json ON events(kind, event_id DESC) "
            "WHERE NOT json_valid(data_json);") in schema
