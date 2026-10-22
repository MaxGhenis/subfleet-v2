"""C-5.12: generated inspection tables agree with the deciding census."""

from contextlib import contextmanager
from dataclasses import asdict
import json

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.fake_kernel import FakeKernel, procargs
from tests.unit.test_census_counted import GUARDIAN, REBORN, ident
from tests.unit.test_census_procargs import attempt_row, core_for
from tests.unit.test_procs import BOOT_A, START


@st.composite
def observations(draw):
    pids = draw(st.lists(st.integers(50, 100), unique=True, max_size=12))
    guardian_start = draw(st.sampled_from([START, REBORN]))
    rows = {GUARDIAN: (1, GUARDIAN, "S", guardian_start), 43: (GUARDIAN, GUARDIAN, "S", START)}
    earlier = [1, GUARDIAN, 43, 999]
    for pid in pids:
        rows[pid] = (draw(st.sampled_from(earlier)), draw(st.sampled_from([GUARDIAN, pid, 500])),
                     draw(st.sampled_from(["S", "R", "Z"])), draw(st.sampled_from([START, REBORN, ""])))
        earlier.append(pid)
    # The insertion order cannot serve as a topological sort.
    rows = dict(draw(st.permutations(list(rows.items()))))
    evidence = {}
    for key in ("owned_identities", "responsible_identities", "counted_identities"):
        seeds = draw(st.lists(st.sampled_from([43, 998, 999, *pids]), unique=True, max_size=5))
        evidence[key] = {str(pid): asdict(ident(pid, draw(st.sampled_from([START, REBORN])))) for pid in seeds}
    return rows, evidence


def walk(rows, roots):
    reached = set(roots)
    while True:
        next_reached = reached | {pid for pid, row in rows.items() if row[0] in reached}
        if next_reached == reached:
            return {pid for pid in reached if pid in rows and not rows[pid][2].startswith("Z")}
        reached = next_reached


@settings(max_examples=250, derandomize=True, deadline=None, database=None)
@given(observations())
def test_inspection_ownership_memory_census_and_expiry(observation):
    rows, before = observation
    table = procs.ProcessTable(rows, BOOT_A)
    row = attempt_row(**before)
    core = core_for(row)
    transactions = []
    original = core.store.transaction
    @contextmanager
    def transaction(*args, **kwargs):
        transactions.append(args)
        with original(*args, **kwargs) as tx:
            yield tx
    core.store.transaction = transaction
    Daemon._record_owned(core, dict(row), table)
    if rows[GUARDIAN][3] != START:
        assert transactions == []
        assert json.loads(row["evidence_json"]) == before
        return
    after = json.loads(row["evidence_json"])
    group = {pid for pid, r in rows.items() if r[1] == GUARDIAN and not r[2].startswith("Z")}
    gained = {int(pid) for pid, value in after["owned_identities"].items()
              if before["owned_identities"].get(pid) != value}
    assert gained <= group
    roots = {GUARDIAN} | {pid for pid in group if rows[pid][3]}
    for seeds in before.values():
        for pid_text, value in seeds.items():
            pid, r = int(pid_text), rows.get(int(pid_text))
            if r is None or (r[3] == value["proc_start"] and
                             (r[2].startswith("Z") or table.is_process(pid, BOOT_A, r[3]))):
                roots.add(pid)
    expected_walk = walk(rows, roots)
    newly_counted = {int(pid) for pid, value in after["counted_identities"].items()
                     if before["counted_identities"].get(pid) != value}
    assert newly_counted <= expected_walk | group
    remembered = {int(pid) for key, values in after.items() if key.endswith("_identities")
                  for pid, value in values.items()
                  if table.identity(int(pid)) is not None and value == asdict(table.identity(int(pid)))}
    assert {pid for pid in expected_walk if table.identity(pid)} <= remembered
    written, snapshot = len(transactions), row["evidence_json"]
    Daemon._record_owned(core, dict(row), table)
    assert len(transactions) == written and row["evidence_json"] == snapshot
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(procs, "snapshot", lambda: table)
        patch.setattr(procs, "boot_id", lambda: BOOT_A)
        patch.setattr(procs, "_read", lambda *a: pytest.fail("generated tables must not inspect host processes"))
        patch.setattr(procs, "kernel", FakeKernel(
            {pid: procargs(["fixture"]) for pid in rows if table.live(pid)},
            starts={pid: r[3] or None for pid, r in rows.items()}))
        census = Daemon._contain(core, dict(row))
    unreadable_roots = {int(pid) for key, seeds in after.items() if key.endswith("_identities")
                        for pid in seeds
                        if int(pid) in rows and not rows[int(pid)][3]}
    # An unreadable walk row has no identity to persist; it remains an
    # unverifiable walk anchor, just like an unreadable recorded seed.
    unreadable_walk = {pid for pid in expected_walk if not rows[pid][3]}
    assert census.descendant_pids <= remembered | unreadable_walk | walk(rows, unreadable_roots)
    if census.descendant_pids & unreadable_walk:
        assert census.unverifiable
    # Every former command is now absent; repeated calls on the same table
    # cannot count as the second observation. A readable live birth survives.
    # Start a fresh observation pair: the generated first table may itself
    # already have proved a seed absent. Restart durability has its own test.
    reset = json.loads(row['evidence_json'])
    reset.pop('counted_absent', None)
    row['evidence_json'] = json.dumps(reset)
    core._counted_tables = {}
    remaining = {pid: r for pid, r in rows.items() if pid in (GUARDIAN, 43)}
    first = procs.ProcessTable(remaining, BOOT_A)
    Daemon._record_owned(core, dict(row), first)
    once = json.loads(row["evidence_json"])["counted_identities"]
    assert after["counted_identities"].items() <= once.items()
    written = len(transactions)
    Daemon._record_owned(core, dict(row), first)
    assert len(transactions) == written
    Daemon._record_owned(core, dict(row), procs.ProcessTable(remaining, BOOT_A))
    kept = json.loads(row["evidence_json"])["counted_identities"]
    for pid, value in once.items():
        live = int(pid) in remaining and remaining[int(pid)][3] == value["proc_start"]
        assert (pid in kept) is live
