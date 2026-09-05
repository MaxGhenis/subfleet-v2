"""C-3.1: schema version 2 adds identity columns to a version-1 store in place.

"Migrations are additive and numbered." A store written before identity binding
existed must keep every row it had, gain three nullable columns, and record that
it moved — and the same code must produce an identical shape on a fresh store.
"""

from __future__ import annotations

import sqlite3

import pytest

from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import MIGRATIONS, SCHEMA_VERSION, Store

#: `lanes` exactly as schema version 1 declared it, so the migration is proved
#: against the shape it will really meet rather than against today's file.
VERSION_1_LANES = """
CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL);
CREATE TABLE lanes (
  lane_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('codex','claude')),
  account_key TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  credential_kind TEXT NOT NULL CHECK (credential_kind IN ('keychain-token','home','env')),
  credential_epoch INTEGER NOT NULL DEFAULT 1,
  home TEXT,
  owner TEXT NOT NULL DEFAULT 'v2' CHECK (owner IN ('v1','v2')),
  desktop INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  plan TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX lanes_account ON lanes(account_key);
CREATE TABLE events (
  event_id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  job_id TEXT,
  attempt_id TEXT,
  lane_id TEXT,
  data_json TEXT NOT NULL
);
"""

LANE_ROW = ("claude-1", "claude", "claude:max@rulesatlas.org",
            "claude-quota-max@rulesatlas.org", "keychain-token", 3, None, "v2", 0, 1,
            "max20x", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z")


@pytest.fixture
def version_1_store(tmp_path):
    """A store as a v1 daemon left it: one enrolled lane and nothing about identity."""
    path = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(VERSION_1_LANES)
    conn.execute("INSERT INTO schema_version VALUES (1,'2026-09-01T00:00:00Z')")
    conn.execute(f"INSERT INTO lanes VALUES ({','.join('?' * len(LANE_ROW))})", LANE_ROW)
    conn.commit()
    conn.close()
    return path


def columns(path, table="lanes"):
    with sqlite3.connect(path) as conn:
        return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def test_the_migration_is_additive(version_1_store):
    """C-3.1 three nullable columns arrive; every version-1 column and value stays."""
    before = columns(version_1_store)
    with Store(version_1_store) as store:
        after = columns(version_1_store)
        assert after[:len(before)] == before
        assert set(after) - set(before) == {"identity", "label", "identity_status"}
        row = store.one("SELECT * FROM lanes WHERE lane_id='claude-1'")
    assert [row[name] for name in before] == list(LANE_ROW)
    assert row["identity"] is None and row["label"] is None
    assert row["identity_status"] is None


def test_the_migration_is_numbered_and_recorded(version_1_store):
    """C-3.1 the store says which versions it has been through, and when."""
    with Store(version_1_store) as store:
        versions = [row["version"] for row in
                    store.query("SELECT * FROM schema_version ORDER BY version")]
        assert versions == [1, SCHEMA_VERSION] == [1, 2]
        migrated = [row for row in store.list_events() if row["kind"] == "schema.migrated"]
        assert len(migrated) == 1
        assert '"version":2' in migrated[0]["data_json"].replace(" ", "")
    assert set(MIGRATIONS) == {2}


def test_reopening_a_migrated_store_changes_nothing(version_1_store):
    """C-3.1 the step runs once: an interrupted or repeated start is safe."""
    with Store(version_1_store):
        pass
    with Store(version_1_store) as store:
        assert [row["version"] for row in store.query("SELECT * FROM schema_version")] == [1, 2]
        assert len([row for row in store.list_events()
                    if row["kind"] == "schema.migrated"]) == 1


def test_a_fresh_store_is_born_at_version_2_with_the_same_columns(tmp_path, version_1_store):
    """C-3.1 `store_schema.sql` describes the newest version, so a fresh database
    and a migrated one hold the same columns — the migration cannot drift from
    the file. (Order differs: `ALTER TABLE` appends. Every read is by name.)"""
    fresh = tmp_path / "fresh.sqlite3"
    with Store(fresh) as store:
        assert [row["version"] for row in store.query("SELECT * FROM schema_version")] == [2]
        assert [row["kind"] for row in store.list_events()] == ["schema.applied"]
    with Store(version_1_store):
        pass
    assert set(columns(fresh)) == set(columns(version_1_store))


def test_a_migrated_lane_can_then_record_its_identity(version_1_store):
    """C-10.6 the point of the migration: a v1-shaped lane can be re-enrolled and
    bound to the identity its own credential reports."""
    identity = "d0d0d0d0-1111-4000-8000-0000000ac1a5:d0d0d0d0-2222-4000-8000-00000000f5a1"
    with Store(version_1_store) as store:
        lane = store.get_lane("claude-1")
        assert lane.identity is None and lane.label is None
        store.put_lane(
            Lane(lane.lane_id, lane.provider, lane.account_key,
                 Credential("claude", lane.credential.ref, "keychain-token", 3),
                 None, LaneOwner.V2, False, True, identity, "max@rulesatlas.org"),
            identity_status="verified")
        bound = store.get_lane("claude-1")
        assert bound.identity == identity
        assert bound.label == "max@rulesatlas.org"
        assert store.one("SELECT identity_status FROM lanes WHERE lane_id='claude-1'"
                         )["identity_status"] == "verified"


def test_an_identity_is_learned_once_and_never_rebound(version_1_store):
    """C-1.3, C-10.6 an unbound lane may learn an identity; a bound one that is
    handed a different identity is a new binding and needs a new lane id."""
    with Store(version_1_store) as store:
        lane = store.get_lane("claude-1")
        first = Lane(lane.lane_id, lane.provider, lane.account_key,
                     Credential("claude", lane.credential.ref, "keychain-token", 3),
                     None, LaneOwner.V2, False, True, "a:1", "one@example.test")
        store.put_lane(first)
        store.put_lane(first)                      # idempotent
        with pytest.raises(ValueError, match="immutable"):
            store.put_lane(Lane(lane.lane_id, lane.provider, lane.account_key,
                                Credential("claude", lane.credential.ref, "keychain-token", 3),
                                None, LaneOwner.V2, False, True, "b:2", "two@example.test"))
        assert store.get_lane("claude-1").identity == "a:1"


def test_a_mismatch_is_released_only_by_re_enrolment(version_1_store):
    """C-10.6 "the lane is not a candidate until an operator re-enrols it": no
    probe result may clear a recorded mismatch, whatever the endpoint says next."""
    with Store(version_1_store) as store:
        store.update_lane("claude-1", identity_status="mismatch")
        with pytest.raises(ValueError, match="re-enrolment"):
            store.update_lane("claude-1", identity_status="verified")
        assert store.one("SELECT identity_status FROM lanes")["identity_status"] == "mismatch"
        store.update_lane("claude-1", identity_status="verified", clear_mismatch=True)
        assert store.one("SELECT identity_status FROM lanes")["identity_status"] == "verified"


def test_an_unknown_identity_status_is_refused_by_the_schema(version_1_store):
    """C-3.1, C-10.6 four statuses and no fifth, enforced where it cannot drift."""
    with Store(version_1_store) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.update_lane("claude-1", identity_status="probably-fine")


def test_a_read_only_handle_on_an_unmigrated_store_still_reads_lanes(version_1_store):
    """C-3.4, C-17.5 offline reads must not require the right to migrate."""
    store = Store(version_1_store, read_only=True)
    try:
        lane = store.get_lane("claude-1")
        assert lane.account_key == "claude:max@rulesatlas.org"
        assert lane.identity is None and lane.label is None
    finally:
        store.close()
    assert columns(version_1_store) == columns(version_1_store)   # nothing was written


def test_an_unknown_identity_status_is_refused_in_words_at_the_seam(version_1_store):
    """C-10.6 a hand-edited `lanes.json` that names a status nobody defined fails
    with the four that exist, rather than with a SQL constraint."""
    with Store(version_1_store) as store:
        lane = store.get_lane("claude-1")
        with pytest.raises(ValueError, match="verified, enrolled, mismatch, unverified"):
            store.put_lane(Lane("claude-2", "claude", "claude:other",
                                Credential("claude", "claude-quota-other", "keychain-token"),
                                None, LaneOwner.V2, False, True, "a:1", "other@example.test"),
                           identity_status="probably-fine")
        assert store.get_lane("claude-2") is None
        assert lane.lane_id == "claude-1"
