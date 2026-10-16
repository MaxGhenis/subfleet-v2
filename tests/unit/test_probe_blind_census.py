"""Post-receipt probe containment obeys the same blind-census rule as attempts."""

import json
from types import SimpleNamespace

import pytest

from subfleet import daemon as dm, procs
from tests.unit.test_daemon_settle import daemon, JOB  # noqa: F401

BLIND = procs.Containment(unverifiable=True, errors=(procs.CWD_UNAVAILABLE,))
EMPTY = procs.Containment()


def probe(daemon, monkeypatch, script, then=EMPTY, *, receipt=True):
    directory = daemon.root / "probe"
    directory.mkdir()
    if receipt:
        (directory / "exit.json").write_text(json.dumps({"rc": 0}))
    record = {"holder": "probe:blind", "job_id": JOB, "lane_id": "codex-1",
              "directory": str(directory), "state": "starting", "owned_identities": {}}
    clock, taken, saved = [0.0], [], []
    steps = iter(script)

    def census(row, **kwargs):
        duration, value = next(steps, (0.0, then))
        clock[0] += duration
        taken.append((clock[0], value))
        return value

    def wait(delay):
        clock[0] += delay
        return False

    monkeypatch.setattr(dm, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=wait))
    daemon.stopping = SimpleNamespace(wait=wait, is_set=lambda: False)
    daemon._probe_census = census
    daemon._save_probe = lambda value: saved.append(dict(value))
    daemon.store.acquire_lease("native:probe-blind", record["holder"])
    return record, taken, saved


def test_a_probe_blind_for_44_seconds_after_its_receipt_retries_without_quarantine(daemon, monkeypatch):
    record, taken, _ = probe(daemon, monkeypatch, [(20, BLIND), (20, BLIND), (4, BLIND)])
    assert daemon._contain_probe(record)
    assert record["state"] == "contained"
    assert [value for _, value in taken] == [BLIND, BLIND, BLIND, EMPTY]
    assert daemon.store.one("SELECT 1 FROM leases WHERE holder=?", (record["holder"],))
    assert not daemon.store.one("SELECT 1 FROM events WHERE kind='probe.quarantined'")


@pytest.mark.parametrize("failure", [BLIND, procs.Containment(
    unverifiable=True, errors=("responsibility enumeration unavailable",))])
def test_a_probe_whose_sources_stay_blind_quarantines_at_the_inspection_deadline(daemon, monkeypatch, failure):
    record, taken, _ = probe(daemon, monkeypatch, [], then=failure)
    assert not daemon._contain_probe(record)
    assert record["state"] == "quarantined"
    assert taken[-1][0] - taken[0][0] >= daemon.exit_inspect_s
    assert len(taken) <= 12
    assert daemon.store.one("SELECT 1 FROM leases WHERE holder=?", (record["holder"],))


@pytest.mark.parametrize("value", [procs.Containment(responsible_pids=frozenset({99})),
    procs.Containment(unverifiable=True, errors=("responsibility read unavailable for pid 99",))])
def test_positive_responsibility_or_a_failed_attribution_is_not_blind(value):
    assert not value.blind


def test_a_probe_without_an_exit_receipt_keeps_the_termination_protocol(daemon, monkeypatch):
    record, taken, _ = probe(daemon, monkeypatch, [], then=BLIND, receipt=False)
    assert not daemon._contain_probe(record)
    assert record["state"] == "quarantined"
    assert taken[-1][0] < daemon.exit_inspect_s


def test_a_writer_seen_after_a_blind_probe_ends_the_inspection_extension(daemon, monkeypatch):
    writer = procs.Containment(responsible_pids=frozenset({99}))
    record, taken, _ = probe(daemon, monkeypatch, [(0, BLIND), (0, writer)])
    assert daemon._contain_probe(record)
    assert [value for _, value in taken] == [BLIND, writer, EMPTY]
    assert taken[-1][0] < daemon.exit_settle_s + daemon.inspect_interval_s


def test_a_blind_probe_read_after_the_first_snapshot_keeps_the_same_inspection_window(daemon, monkeypatch):
    guardian = procs.Containment(group_pids=frozenset({99}))
    record, taken, _ = probe(daemon, monkeypatch, [(0, guardian)], then=BLIND)
    assert not daemon._contain_probe(record)
    assert record["state"] == "quarantined"
    assert taken[0][1] == guardian
    assert taken[-1][0] - taken[0][0] >= daemon.exit_inspect_s
    assert len(taken) <= 12
