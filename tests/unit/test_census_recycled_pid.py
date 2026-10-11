"""C-5.3, C-5.5: generated census identities and conservative fallbacks."""

from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
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
            child: (guardian, guardian, "S", start(1))}
    for offset in range(draw(st.integers(min_value=0, max_value=15))):
        pid = guardian + offset + 2
        parent = draw(st.sampled_from([1, *rows]))
        group = draw(st.sampled_from([guardian, pid, 2]))
        state = draw(st.sampled_from(["S", "R", "Z"]))
        rows[pid] = (parent, group, state, start(offset + 2))
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
    rows.pop(child if leader else guardian)
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
@given(tables())
def test_b_matching_roots_and_group_members_are_included(monkeypatch, generated):
    """C-5.5 (b): a matching root and every live member of its group remain."""
    guardian, child, rows, marked = generated
    with monkeypatch.context() as patch:
        install(patch, rows, marked)
        result = procs.containment(guardian, guardian, child, "job/a1",
                                   recorded_identities=recorded(rows))
    assert {guardian, child} <= result.descendant_pids
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
    """C-5.5 (d): with all identities matching, all three sets equal the old census."""
    guardian, child, rows, marked = generated
    with monkeypatch.context() as patch:
        install(patch, rows, marked)
        before = procs.containment(guardian, guardian, child, "job/a1", root="/fixture")
        after = procs.containment(guardian, guardian, child, "job/a1", root="/fixture",
                                  recorded_identities=recorded(rows),
                                  started_at="2026-10-11T02:00:00Z",
                                  finished_at="2026-10-11T00:00:00Z")
    assert (after.group_pids, after.descendant_pids, after.marker_pids, after.identities) == (
        before.group_pids, before.descendant_pids, before.marker_pids, before.identities)
    assert not after.recycled and not after.unverifiable


@PROPERTY
@given(tables(), st.booleans())
def test_e_recycled_root_never_becomes_a_new_kill_target(monkeypatch, generated, leader):
    """C-5.4, C-5.6 (e): _kill_attempt never records/signals the stranger's identity."""
    guardian, child, rows, _ = generated
    target = guardian if leader else child
    old = procs.ProcessIdentity(target, BOOT_A, start(-100))
    if leader:
        # Its entire new group belongs to the stranger; both old roots died.
        rows[guardian] = (1, guardian, "Ss", rows[guardian][3])
        rows.pop(child)
    else:
        # A matching guardian can still own its members, but the recycled
        # child's unrelated group must never refresh the child's old identity.
        rows[child] = (1, child, "Ss", rows[child][3])
    a = {"attempt_id": "job/a1", "job_id": "job", "seq": 1, "guardian_pid": guardian,
         "child_pid": None if leader else child, "pgid": guardian, "boot_id": BOOT_A,
         "proc_start": old.proc_start if leader else rows[guardian][3],
         "started_at": "2026-10-11T01:00:00Z", "evidence_json": json.dumps({
             "owned_identities": {str(target): asdict(old)}})}
    saved, signalled = {}, []

    class Store:
        @contextmanager
        def transaction(self, *args, **kwargs):
            yield self

        def execute(self, sql, args):
            saved.update(json.loads(args[1])["owned_identities"])

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


@pytest.mark.parametrize("seconds,expected", [(-1, False), (0, True), (1, True), (60, True), (61, False)])
def test_unrecorded_root_uses_attempt_time_bounds(monkeypatch, seconds, expected):
    """C-5.5: unrecorded roots outside start/exit bounds cannot be the attempt's."""
    install(monkeypatch, {42: (1, 42, "S", start(seconds))})
    result = procs.containment(42, 42, None, "job/a1",
                               started_at="2026-10-11T01:00:00.900Z",
                               finished_at="2026-10-11T01:01:00Z")
    assert (42 in result.descendant_pids) is expected
    assert (42 in result.group_pids) is expected
    assert result.verified_empty is (not expected)


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
