"""C-5.12: short lived Claude command groups cannot grow the counted record."""

from dataclasses import asdict
import json

import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.unit.test_census_counted import ident, GUARDIAN, REBORN
from tests.unit.test_census_procargs import attempt_row, core_for
from tests.unit.test_procs import BOOT_A, START


def table(*rows):
    return procs.ProcessTable({GUARDIAN: (1, GUARDIAN, "S", START),
                               43: (GUARDIAN, GUARDIAN, "S", START), **dict(rows)}, BOOT_A)


@pytest.mark.parametrize("gone", ["absent", "recycled", "zombie"])
def test_counted_expires_only_after_two_distinct_absent_tables(gone):
    row = attempt_row(counted_identities={"61": asdict(ident(61))})
    core = core_for(row)
    rows = [] if gone == "absent" else [(61, (1, 61, "Z" if gone == "zombie" else "S", REBORN))]
    first = table(*rows)
    Daemon._record_owned(core, dict(row), first)
    assert "61" in json.loads(row["evidence_json"])["counted_identities"]
    events = len(core.store.events)
    Daemon._record_owned(core, dict(row), first)
    assert len(core.store.events) == events
    assert "61" in json.loads(row["evidence_json"])["counted_identities"]
    Daemon._record_owned(core, dict(row), table(*rows))
    assert "61" not in json.loads(row["evidence_json"])["counted_identities"]


def test_a_live_birth_resets_absence_and_is_never_expired():
    row = attempt_row(counted_identities={"61": asdict(ident(61))})
    core = core_for(row)
    for _ in range(3):
        Daemon._record_owned(core, dict(row), table())
        Daemon._record_owned(core, dict(row), table((61, (1, 61, "S", START))))
        assert json.loads(row["evidence_json"])["counted_identities"]["61"] == asdict(ident(61))


def test_the_second_absence_after_restart_expires_the_recorded_birth():
    row = attempt_row(counted_identities={"61": asdict(ident(61))})
    Daemon._record_owned(core_for(row), dict(row), table())
    assert "61" in json.loads(row["evidence_json"])["counted_identities"]
    Daemon._record_owned(core_for(row), dict(row), table())
    assert "61" not in json.loads(row["evidence_json"])["counted_identities"]


def test_failed_first_miss_write_is_retried_on_the_same_table():
    row = attempt_row(owned_identities={str(pid): asdict(ident(pid)) for pid in (GUARDIAN, 43)},
                      counted_identities={"61": asdict(ident(61))})
    core = core_for(row)
    original = core.store.transaction
    failures = [True]
    def transaction(*args, **kwargs):
        if failures.pop() if failures else False:
            raise OSError("scratch store write interrupted")
        return original(*args, **kwargs)
    core.store.transaction = transaction
    first = table()
    with pytest.raises(OSError):
        Daemon._record_owned(core, dict(row), first)
    Daemon._record_owned(core, dict(row), first)
    assert "61" in json.loads(row["evidence_json"])["counted_absent"]
    Daemon._record_owned(core, dict(row), table())
    assert "61" not in json.loads(row["evidence_json"])["counted_identities"]


def test_claude_shaped_command_groups_reach_a_steady_record_size():
    row, sizes = attempt_row(), []
    core = core_for(row)
    # 4,100 commands/hour, one independently led shell per sampled command.
    for command in range(4100):
        pid = 100 + command
        Daemon._record_owned(core, dict(row), table((pid, (43, pid, "S", START))))
        kept = json.loads(row["evidence_json"])["counted_identities"]
        assert set(kept) <= {str(pid), str(pid - 1)}
        sizes.append(len(row["evidence_json"]))
    assert max(sizes[1000:]) - min(sizes[1000:]) < 20


def test_a_deciding_census_keeps_its_seed_snapshot_while_inspection_expires_it(monkeypatch):
    row = attempt_row(counted_identities={"61": asdict(ident(61))})
    core = core_for(row)
    def deciding(*args, **kwargs):
        Daemon._record_owned(core, dict(row), table())
        Daemon._record_owned(core, dict(row), table())
        assert not json.loads(row["evidence_json"])["counted_identities"]
        assert kwargs["recorded_identities"][61] == ident(61)
        return procs.Containment()
    monkeypatch.setattr(procs, "containment", deciding)
    Daemon._contain(core, dict(row))
