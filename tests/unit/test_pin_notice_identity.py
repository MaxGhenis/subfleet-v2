"""Retirement associations require the whole row; legacy pin associations do not."""
from __future__ import annotations

import hashlib
import json

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import store as store_module
from subfleet.store import pin_notice_jobs


def notice(notice_id=1, session_id="s", created_at="t", text="the outcome"):
    return {"notice_id": notice_id, "session_id": session_id, "created_at": created_at, "text": text}


def event(event_id, row, *, kind="retention.notice_retired", text=None, digest=True, job_id=None):
    data = {"service_notice_id": row["notice_id"], "session_id": row["session_id"],
            "created_at": row["created_at"]}
    if digest:
        data["outcome_sha256"] = hashlib.sha256(
            (row["text"] if text is None else text).encode("utf-8", "surrogatepass")).hexdigest()
    return {"event_id": event_id, "kind": kind, "job_id": job_id or f"job-{event_id}",
            "data_json": json.dumps(data)}


def query_for(events):
    """The indexed event query, with rows shaped as the SELECT asks SQLite for."""
    def query(sql, params):
        kind = next(kind for kind in ("job.pin_noticed", "retention.notice_retired")
                    if f"kind='{kind}'" in sql)
        columns = sql.split("SELECT ", 1)[1].split(" FROM", 1)[0].split(",")
        result = []
        for item in events:
            try:
                data = json.loads(item["data_json"])
            except ValueError:
                continue
            if (item["kind"] == kind and isinstance(data, dict)
                    and data.get("service_notice_id") in params):
                result.append({key: item[key] for key in columns})
        return result
    return query


@pytest.mark.parametrize("field", [None, "notice_id", "session_id", "created_at", "text"])
def test_retirement_association_requires_all_four_fields(field):
    original = notice()
    row = {**original}
    if field is not None:
        row[field] = 2 if field == "notice_id" else f"different {field}"
    found = pin_notice_jobs(query_for([event(1, original)]), [row])
    assert found == ({1: "job-1"} if field is None else {})


def test_retirement_event_without_digest_names_nothing():
    row = notice()
    assert pin_notice_jobs(query_for([event(1, row, digest=False)]), [row]) == {}


def test_retirement_hashes_only_matching_rows_and_each_at_most_once(monkeypatch):
    rows = [notice(number, text=f"text {number}") for number in range(1, 8)]
    events = [event(1, rows[0], text="a different outcome"), event(2, rows[0]),
              event(3, rows[0], text="another different outcome"),
              event(4, rows[1], digest=False),
              event(5, rows[2], kind="job.pin_noticed", digest=False),
              event(6, {**rows[3], "session_id": "different"}),
              event(7, {**rows[4], "created_at": "different"})]
    hashes, real_hash = [], hashlib.sha256

    def hashing(value):
        hashes.append(value)
        return real_hash(value)

    monkeypatch.setattr(store_module.hashlib, "sha256", hashing)
    assert pin_notice_jobs(query_for(events), rows) == {1: "job-2", 3: "job-5"}
    assert hashes == [b"text 1"]


@pytest.mark.parametrize("latest_matches", [False, True])
def test_newest_matching_event_wins_across_event_kinds(latest_matches):
    row = notice()
    events = [event(1, row, kind="job.pin_noticed", digest=False),
              event(2, row, text=row["text"] if latest_matches else "different outcome")]
    assert pin_notice_jobs(query_for(events), [row]) == {1: f"job-{2 if latest_matches else 1}"}


def test_a_newer_pin_event_needs_no_digest_or_hash(monkeypatch):
    row = notice()
    events = [event(1, row), event(2, row, kind="job.pin_noticed", digest=False)]

    def refuse_hash(*args, **kwargs):
        raise AssertionError("the newest pin association needs no text hash")

    monkeypatch.setattr(store_module.hashlib, "sha256", refuse_hash)
    assert pin_notice_jobs(query_for(events), [row]) == {1: "job-2"}


def test_pin_noticed_associations_match_the_previous_key_function():
    # Copy of the head's key function, using its legacy default. In particular,
    # neither a missing digest nor a different text changes a pin association.
    def previous_key(data_json):
        try:
            data = json.loads(data_json or "{}")
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("service_notice_id"), int):
            return None
        return data["service_notice_id"], data.get("session_id"), data.get("created_at")

    rows = [notice(1, text="different from the original pin"), notice(2), notice(3)]
    data = ["{", "null", "[]", '{}', '{"service_notice_id":1.0,"session_id":"s","created_at":"t"}',
            '{"service_notice_id":1,"session_id":"different","created_at":"t"}',
            '{"service_notice_id":1,"session_id":"s","created_at":"different"}',
            '{"service_notice_id":true,"session_id":"s","created_at":"t"}',
            '{"service_notice_id":2,"session_id":"s","created_at":"t"}',
            '{"service_notice_id":3,"session_id":"s","created_at":"t","outcome_sha256":"wrong"}',
            '{"service_notice_id":2,"session_id":"s","created_at":"t"}']
    events = [{"event_id": index, "kind": "job.pin_noticed", "job_id": f"job-{index}", "data_json": payload}
              for index, payload in enumerate(data)]
    wanted = {(row["notice_id"], row["session_id"], row["created_at"]): row["notice_id"] for row in rows}
    expected = {}
    for item in reversed(events):
        key = previous_key(item["data_json"])
        if key in wanted:
            expected.setdefault(wanted[key], item["job_id"])
    assert pin_notice_jobs(query_for(events), rows, kinds=("job.pin_noticed",)) == expected
    assert expected == {1: "job-7", 2: "job-10", 3: "job-9"}


TEXT = st.text(max_size=16)
ROW_SPECS = st.lists(st.tuples(st.integers(1, 4), st.sampled_from([None, "s0", "s1"]),
                              st.sampled_from(["t0", "t1"]), TEXT),
                    min_size=1, max_size=4, unique_by=lambda row: row[0])
EVENT_SPECS = st.lists(st.tuples(st.integers(1, 4), st.sampled_from([None, "s0", "s1"]),
                                st.sampled_from(["t0", "t1"]), TEXT, st.booleans()), max_size=12)


@settings(max_examples=100, deadline=None, derandomize=True, database=None)
@given(row_specs=ROW_SPECS, event_specs=EVENT_SPECS, data=st.data())
def test_property_retirement_identity_matches_a_four_field_reference(row_specs, event_specs, data):
    rows = [notice(*spec) for spec in row_specs]
    events = [event(index, notice(*spec[:4]), digest=spec[4])
              for index, spec in enumerate(event_specs, 1)]
    # Every example has colliding first-three fields and both equal/unequal texts;
    # the newest event's text varies, so event ordering cannot stand in for identity.
    for row in rows:
        texts = [row["text"], row["text"] + "!"]
        if data.draw(st.booleans()):
            texts.reverse()
        for text in texts:
            events.append(event(len(events) + 1, row, text=text))
    expected = {}
    for row in rows:
        for item in reversed(events):
            payload = json.loads(item["data_json"])
            if (payload["service_notice_id"] == row["notice_id"]
                    and payload["session_id"] == row["session_id"]
                    and payload["created_at"] == row["created_at"]
                    and payload.get("outcome_sha256") == hashlib.sha256(
                        row["text"].encode("utf-8", "surrogatepass")).hexdigest()):
                expected[row["notice_id"]] = item["job_id"]
                break
    assert pin_notice_jobs(query_for(events), rows) == expected
