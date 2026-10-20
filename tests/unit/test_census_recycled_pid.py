"""C-5.3, C-5.5: generated census identities and conservative fallbacks."""

from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, event, given, settings, strategies as st
import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.unit.test_procs import BOOT_A, BOOT_B, census


PROPERTY = settings(max_examples=200, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])
BASE = datetime(2026, 10, 11, 1, tzinfo=timezone.utc)


def start(seconds=0):
    return (BASE + timedelta(seconds=seconds)).strftime("%a %b %d %H:%M:%S %Y")


@st.composite
def tables(draw):
    guardian = draw(st.integers(min_value=100, max_value=10000))
    child = guardian + 1
    rows = {guardian: (1, guardian, "Ss", start()),
            child: (guardian, guardian, "S", start(1)),
            guardian + 2: (1, guardian + 2, "Ss", start(2))}
    for offset in range(draw(st.integers(min_value=0, max_value=15))):
        pid = guardian + offset + 3
        parent = draw(st.sampled_from([1, *rows]))
        group = draw(st.sampled_from([guardian, pid, 2]))
        state = draw(st.sampled_from(["S", "R", "Z"]))
        rows[pid] = (parent, group, state, start(offset + 3))
    states = []
    for pid in (guardian, child):
        state = draw(st.sampled_from(["live", "absent", "zombie", "recycled"]))
        states.append(state)
        ppid, pgid, stat, began = rows[pid]
        if state == "absent":
            del rows[pid]
        elif state == "zombie":
            rows[pid] = (ppid, pgid, "Z", began)
        elif state == "recycled":
            rows[pid] = (1, pid, "Ss", start(1000))
    event("root states: " + "/".join(states))
    marked = draw(st.sets(st.sampled_from(list(rows))))
    return guardian, child, rows, marked


def install(patch, rows, marked=(), boot=BOOT_A):
    procs.forget_boot_id()
    census(patch, session=boot,
           parents="".join(f"{pid} {ppid} {pgid} {state} {began}\n"
                           for pid, (ppid, pgid, state, began) in rows.items()),
           markers="".join(f"{pid} fake SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/fixture "
                            "PRIVATE_TOKEN=secret-sentinel\n" for pid in marked))


def recorded(rows, boot=BOOT_A):
    return {pid: procs.ProcessIdentity(pid, boot, row[3]) for pid, row in rows.items()}


@PROPERTY
@given(tables(), st.booleans(), st.booleans())
def test_a_recycled_root_and_group_are_excluded(monkeypatch, generated, leader, different_boot):
    """C-5.3, C-5.5 (a): no group or walk comes from a recycled root."""
    guardian, child, rows, marked = generated
    root = guardian if leader else child
    # Model the stranger's own group/tree; the attempt's other root is gone.
    rows.pop(child if leader else guardian, None)
    rows[root] = (1, root, "Ss", start(1000))
    rows = {pid: (root if pid != root else 1, root, row[2], row[3]) for pid, row in rows.items()}
    boot = BOOT_B if different_boot else BOOT_A
    known = {root: procs.ProcessIdentity(root, BOOT_A, rows[root][3] if different_boot else start())}
    with monkeypatch.context() as patch:
        install(patch, rows, marked & rows.keys(), boot)
        result = procs.containment(guardian, guardian, None if leader else child, "job/a1",
                                   root="/fixture", recorded_identities=known)
    assert result.group_pids == result.descendant_pids == frozenset()
    assert root in result.recycled
    assert "secret-sentinel" not in json.dumps(result.to_dict())


@PROPERTY
@given(tables(), st.booleans(), st.booleans())
def test_b_matching_roots_and_group_members_are_included(monkeypatch, generated, leader, recycle_other):
    """C-5.5 (b): a matching root and every live member of its group remain."""
    guardian, child, rows, marked = generated
    root, other = (guardian, child) if leader else (child, guardian)
    rows[root] = (1, root, "Ss", start(0 if leader else 1))
    known = recorded(rows)
    if recycle_other and other in rows:
        known[other] = procs.ProcessIdentity(other, BOOT_A, start(-100))
    with monkeypatch.context() as patch:
        install(patch, rows, marked)
        result = procs.containment(guardian, guardian, child, "job/a1",
                                   recorded_identities=known)
    assert root in result.descendant_pids
    if other == guardian and recycle_other and other in rows:
        assert not result.group_pids
    else:
        assert result.group_pids == {pid for pid, row in rows.items()
                                     if row[1] == guardian and not row[2].startswith("Z")}


@PROPERTY
@given(tables(), st.booleans())
def test_c_markers_are_unchanged_by_recycled_identities(monkeypatch, generated, recycle):
    """C-5.5 (c): identity filtering never removes a matching environment marker."""
    guardian, child, rows, marked = generated
    known = recorded(rows)
    if recycle:
        known[guardian] = procs.ProcessIdentity(guardian, BOOT_A, start(-100))
        known[child] = procs.ProcessIdentity(child, BOOT_A, start(-99))
    with monkeypatch.context() as patch:
        install(patch, rows, marked)
        result = procs.containment(guardian, guardian, child, "job/a1", root="/fixture",
                                   recorded_identities=known)
    assert result.marker_pids == {pid for pid in marked if not rows[pid][2].startswith("Z")}


@PROPERTY
@given(tables())
def test_d_matching_identities_preserve_the_entire_census(monkeypatch, generated):
    """C-5.5 (d): matching identities never shrink any existing census source."""
    guardian, child, rows, marked = generated
    with monkeypatch.context() as patch:
        install(patch, rows, marked)
        before = procs.containment(guardian, guardian, child, "job/a1", root="/fixture")
        after = procs.containment(guardian, guardian, child, "job/a1", root="/fixture",
                                  recorded_identities=recorded(rows),
                                  started_at="2026-10-11T02:00:00Z",
                                  finished_at="2026-10-11T00:00:00Z")
    # Independent recorded survivors can extend the walk after reparenting.
    assert before.group_pids <= after.group_pids
    assert before.descendant_pids <= after.descendant_pids
    assert before.marker_pids == after.marker_pids
    assert before.identities.items() <= after.identities.items()
    # C-5.5 now requires every matching recorded orphan, not just those already
    # reachable from the named roots. Pin the entire resulting live walk.
    expected = {pid for pid, row in rows.items() if not row[2].startswith("Z")}
    assert after.descendant_pids == expected
    assert after.identities == {pid: recorded(rows)[pid] for pid in expected}
    assert not after.recycled and not after.unverifiable


@PROPERTY
@given(tables(), st.booleans())
def test_e_recycled_root_never_becomes_a_new_kill_target(monkeypatch, generated, leader):
    """C-5.4, C-5.6 (e): _kill_attempt never records/signals the stranger's identity."""
    guardian, child, rows, _ = generated
    target = guardian if leader else child
    rows[target] = (1, target, "Ss", start(0 if leader else 1))
    old = procs.ProcessIdentity(target, BOOT_A, start(-100))
    if leader:
        # Its entire new group belongs to the stranger; both old roots died.
        rows[guardian] = (1, guardian, "Ss", rows[guardian][3])
        rows.pop(child, None)
    else:
        rows[guardian] = (1, guardian, "Ss", start())
        # A matching guardian can still own its members, but the recycled
        # child's unrelated group must never refresh the child's old identity.
        rows[child] = (1, child, "Ss", rows[child][3])
    survivor = guardian + 2
    survivor_identity = procs.ProcessIdentity(survivor, BOOT_A, rows[survivor][3])
    a = {"attempt_id": "job/a1", "job_id": "job", "seq": 1, "guardian_pid": guardian,
         "child_pid": None if leader else child, "pgid": guardian, "boot_id": BOOT_A,
         "proc_start": old.proc_start if leader else rows[guardian][3],
         "started_at": "2026-10-11T01:00:00Z", "evidence_json": json.dumps({
             "owned_identities": {str(target): asdict(old), str(survivor): asdict(survivor_identity)}})}
    saved, signalled = {}, []

    class Store:
        def get_job(self, job_id):
            return None

        @contextmanager
        def transaction(self, *args, **kwargs):
            yield self

        def execute(self, sql, args):
            value = args[0] if sql.startswith("UPDATE attempts SET evidence_json") else args[1]
            saved.update(json.loads(value)["owned_identities"])

        def get_attempt(self, aid):
            return a

    core = SimpleNamespace(root=Path("/fixture"), store=Store(), term_grace_s=0, kill_settle_s=0,
                           stopping=SimpleNamespace(wait=lambda _: False),
                           _read_json=lambda path: {"rc": 0} if path.name == "exit.json" else None,
                           _begin_finalizing=lambda *args: None, _quarantine=lambda *args: None)
    core._contain = lambda attempt: Daemon._contain(core, attempt)
    with monkeypatch.context() as patch:
        install(patch, rows)
        patch.setattr(procs, "same_process", lambda pid, boot, began:
                      pid in rows and rows[pid][3] == began and boot == BOOT_A)
        patch.setattr(procs, "signal_group", lambda *args, **kwargs: False)
        patch.setattr(procs, "signal_process", lambda identity, sig: signalled.append(identity))
        Daemon._kill_attempt(core, a)
    assert saved[str(target)] == asdict(old)
    assert all(identity.pid != target for identity in signalled)
    assert survivor_identity in signalled
    allowed = {survivor} | ({pid for pid, row in rows.items() if row[1] == guardian}
                            if not leader else set())
    assert {identity.pid for identity in signalled} <= allowed
    assert all(identity.boot_id == BOOT_A and identity.proc_start == rows[identity.pid][3]
               for identity in signalled)


@PROPERTY
@given(tables())
def test_f_recorded_survivors_remain_after_roots_depart(monkeypatch, generated):
    """A matching writer survives root absence, zombie rows and positive reuse."""
    guardian, child, rows, _ = generated
    known = recorded(rows)
    known[guardian] = procs.ProcessIdentity(guardian, BOOT_A, start())
    known[child] = procs.ProcessIdentity(child, BOOT_A, start(1))
    matching = {pid for pid, row in rows.items()
                if not row[2].startswith("Z") and known[pid].proc_start == row[3]}
    with monkeypatch.context() as patch:
        install(patch, rows)
        result = procs.containment(guardian, guardian, None, "job/a1",
                                   recorded_identities=known)
    assert matching <= result.descendant_pids
    assert not result.verified_empty
    assert guardian + 2 in result.live_pids  # fully reparented, separate session


@PROPERTY
@given(tables(), st.sampled_from(["missing-boot", "missing-recorded-start",
                                 "missing-current-start", "legacy", "failed-boot"]))
def test_g_incomparable_survivor_never_reads_dead(monkeypatch, generated, failure):
    guardian, child, rows, _ = generated
    survivor = guardian + 2
    known = recorded(rows)
    if failure == "missing-boot":
        known[survivor] = procs.ProcessIdentity(survivor, "", start(2))
    elif failure == "missing-recorded-start":
        known[survivor] = procs.ProcessIdentity(survivor, BOOT_A, "")
    elif failure == "missing-current-start":
        rows[survivor] = (1, survivor, "Ss", "")
    elif failure == "legacy":
        known[survivor] = procs.ProcessIdentity(survivor, "99", start(2))
    with monkeypatch.context() as patch:
        install(patch, rows)
        if failure == "failed-boot":
            def unavailable():
                raise procs.InspectionError("boot unavailable")
            patch.setattr(procs, "boot_id", unavailable)
        result = procs.containment(guardian, guardian, child, "job/a1",
                                   recorded_identities=known)
    assert survivor in result.descendant_pids
    assert result.unverifiable and not result.verified_empty
    assert survivor not in result.recycled


@pytest.mark.parametrize("seconds", [-1, 0, 1, 60, 61])
def test_unrecorded_root_cannot_be_excluded_by_wall_clock_bounds(monkeypatch, seconds):
    """C-5.3, C-5.5: receipt/birth clock samples never prove reuse without identity."""
    install(monkeypatch, {42: (1, 42, "S", start(seconds))})
    result = procs.containment(42, 42, None, "job/a1",
                               started_at="2026-10-11T01:00:00.900Z",
                               finished_at="2026-10-11T01:01:00Z")
    assert result.descendant_pids == result.group_pids == {42}
    assert result.unverifiable and not result.verified_empty and not result.recycled


@pytest.mark.parametrize("began", ["", "unparseable", start(20)])
def test_unrecorded_root_without_usable_bounds_is_retained(monkeypatch, began):
    """C-5.5: missing or unreadable bounds err toward holding the root."""
    install(monkeypatch, {42: (1, 42, "S", began)})
    assert 42 in procs.containment(42, 42, None, "job/a1", started_at="invalid").live_pids


@pytest.mark.parametrize("boot", ["100", "99"])
def test_legacy_boot_timestamp_unknown_never_establishes_death(monkeypatch, boot):
    """C-5.3: a shifted legacy timestamp holds roots/groups and stays unknown."""
    install(monkeypatch, {42: (1, 42, "S", start())})
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, boot, start())})
    assert result.group_pids == result.descendant_pids == {42}
    assert not result.recycled
    assert result.unverifiable is (boot == "99")


def test_absent_leader_preserves_survivors(monkeypatch):
    """C-5.5: no process at the leader PID means its surviving group is retained."""
    install(monkeypatch, {43: (1, 42, "S", start())})
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, start())})
    assert result.group_pids == {43} and not result.recycled


def test_failed_boot_inspection_never_excludes_root(monkeypatch):
    """C-5.3, C-5.5: a boot read failure leaves the root/group unverifiable."""
    install(monkeypatch, {42: (1, 42, "S", start())})
    def unavailable():
        raise procs.InspectionError("boot unavailable")
    monkeypatch.setattr(procs, "boot_id", unavailable)
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, start())})
    assert result.group_pids == result.descendant_pids == {42}
    assert result.unverifiable and not result.recycled


def test_uuid_against_timestamp_fallback_holds_root(monkeypatch):
    """C-5.3: a UUID record cannot establish death against a failed UUID read."""
    install(monkeypatch, {42: (1, 42, "S", start())}, boot="100")
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, start())})
    assert result.group_pids == result.descendant_pids == {42}
    assert result.unverifiable and not result.recycled


def test_missing_start_inspection_holds_recorded_root(monkeypatch):
    """C-5.3, C-5.5: no lstart is uncertainty, never proof of a reused identity."""
    install(monkeypatch, {42: (1, 42, "S", "")})
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, start())})
    assert result.group_pids == result.descendant_pids == {42}
    assert result.unverifiable and not result.recycled


def test_changed_start_proves_death_even_if_boot_read_fails(monkeypatch):
    """C-5.3: positive start mismatch suffices; excluded evidence can lack a boot ID."""
    install(monkeypatch, {42: (1, 42, "S", start(1000))})
    def unavailable():
        raise procs.InspectionError("boot unavailable")
    monkeypatch.setattr(procs, "boot_id", unavailable)
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, start())})
    assert result.verified_empty
    assert result.recycled[42]["now"]["boot_id"] is None


def test_cached_boot_mismatch_needs_fresh_confirmation(monkeypatch):
    """C-5.12: a table's stale UUID cannot alone prune a matching start identity."""
    rows = {42: (1, 42, "S", start())}
    install(monkeypatch, rows)
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(rows, BOOT_B))
    reads = []
    read = procs._read
    def counting(argv, **kwargs):
        reads.append(argv)
        return read(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting)
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, BOOT_A, start())})
    assert result.group_pids == result.descendant_pids == {42}
    assert not result.unverifiable and not result.recycled
    assert sum("kern.bootsessionuuid" in argv for argv in reads) == 1


@pytest.mark.parametrize("boot", ["99", BOOT_A])
def test_zombie_root_with_empty_group_needs_no_boot_read(monkeypatch, boot):
    """C-5.3, C-5.12: a zombie alone proves empty even when boot inspection fails."""
    install(monkeypatch, {42: (1, 42, "Z", start()), 7: (1, 7, "S", start())})
    def unavailable():
        raise procs.InspectionError("boot unavailable")
    monkeypatch.setattr(procs, "boot_id", unavailable)
    result = procs.containment(42, 42, None, "job/a1",
                               recorded_identities={42: procs.ProcessIdentity(42, boot, start())})
    assert result.verified_empty and not result.errors and not result.recycled
