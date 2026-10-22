"""C-5.3--5.7: independent adversarial fixtures for PR 185 (review only)."""

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.unit.test_census_recycled_pid import install, start
from tests.unit.test_procs import BOOT_A


def ident(pid, seconds=0, boot=BOOT_A):
    return procs.ProcessIdentity(pid, boot, start(seconds))


def test_shifted_legacy_boot_keeps_live_guardian_and_escaped_child(monkeypatch):
    """C-5.3: boot timestamp correction is unknown and retains every writer."""
    install(monkeypatch, {42: (1, 42, "Ss", start()),
                          43: (42, 43, "Ss", start(1))})
    result = procs.containment(42, 42, 43, "job/a1", recorded_identities={
        42: ident(42, boot="99"), 43: ident(43, 1, boot="99")})
    assert result.live_pids == {42, 43}
    assert result.unverifiable and not result.verified_empty and not result.recycled


@pytest.mark.parametrize("began", [start(1), "", "unparseable"])
def test_unrecorded_live_child_after_guardian_exit_is_retained(monkeypatch, began):
    """C-5.5: no child identity must still keep a live root and its setsid child."""
    install(monkeypatch, {43: (1, 43, "Ss", began), 44: (43, 44, "Ss", start(2))})
    result = procs.containment(42, 42, 43, "job/a1", recorded_identities={42: ident(42)},
                               started_at="2026-10-11T01:00:00.900Z")
    assert result.descendant_pids == {43, 44}
    assert not result.verified_empty


def test_guardian_exec_preserves_start_identity(monkeypatch):
    """C-5.3: XNU forkproc copies p_start across exec; command never defines identity."""
    install(monkeypatch, {42: (1, 42, "Ss", start()), 43: (42, 42, "S", start(1))})
    result = procs.containment(42, 42, 43, "job/a1", recorded_identities={42: ident(42), 43: ident(43, 1)})
    assert result.group_pids == result.descendant_pids == {42, 43}
    assert not result.recycled and not result.verified_empty


def test_dead_leader_keeps_group_survivors(monkeypatch):
    """C-5.5: a disappeared leader cannot erase the old group's surviving writer."""
    install(monkeypatch, {43: (1, 42, "S", start(1)), 44: (1, 42, "S", start(2))})
    result = procs.containment(42, 42, None, "job/a1", recorded_identities={42: ident(42)})
    assert result.group_pids == {43, 44}
    assert not result.verified_empty


@pytest.mark.parametrize("guardian_state", [None, "Z", "recycled"])
def test_departing_guardian_keeps_setsid_child_parent_links(monkeypatch, guardian_state):
    """C-5.5: a ps scan may read the child before its parent exits or becomes a zombie."""
    rows = {43: (42, 43, "Ss", start(1)), 44: (43, 44, "Ss", start(2))}
    if guardian_state is not None:
        rows[42] = (1, 42, "Ss" if guardian_state == "recycled" else guardian_state,
                    start(1000) if guardian_state == "recycled" else start())
    if guardian_state == "recycled":
        # This variant is a consistent instant: the old guardian died, its
        # escaped member was reparented to init, and 42 now belongs to a stranger.
        rows[43] = (1, 43, "Ss", start(1))
    install(monkeypatch, rows)
    # A running attempt's row need not yet have child_pid; the guardian only
    # writes that PID in exit.json after wait(). The child has changed session,
    # so there are no live members of group 42. Its executable's environment
    # may be unavailable to ps (XNU omits environments for restricted images).
    result = procs.containment(42, 42, None, "job/a1", root="/fixture",
                               recorded_identities={42: ident(42), 43: ident(43, 1)})
    assert not result.verified_empty, result.to_dict()
    if guardian_state != "recycled":
        assert {43, 44} <= result.descendant_pids


def test_setsid_member_remains_in_walk(monkeypatch):
    """C-5.5: leaving the guardian's group keeps the parent's descendant source."""
    install(monkeypatch, {42: (1, 42, "Ss", start()), 43: (42, 43, "Ss", start(1)),
                          44: (43, 44, "Ss", start(2))})
    result = procs.containment(42, 42, None, "job/a1", recorded_identities={42: ident(42)})
    assert result.group_pids == {42} and result.descendant_pids == {42, 43, 44}
    assert not result.verified_empty


def test_orphan_setsid_member_marker_survives_recycled_roots(monkeypatch):
    """C-5.5: independent markers retain an escaped orphan when both roots died."""
    install(monkeypatch, {42: (1, 42, "Ss", start(100)),
                          44: (1, 44, "Ss", start(2))}, marked={44})
    result = procs.containment(42, 42, 43, "job/a1", root="/fixture",
                               recorded_identities={42: ident(42), 43: ident(43, 1)})
    assert result.marker_pids == result.live_pids == {44}
    assert not result.verified_empty
    serialized = json.dumps(result.to_dict())
    assert "PRIVATE_TOKEN" not in serialized and "secret-sentinel" not in serialized
    assert "fake" not in serialized and "SUBFLEET_ATTEMPT" not in serialized


def test_clock_rollback_cannot_prove_unrecorded_child_is_recycled(monkeypatch):
    """C-5.3, C-5.5: wall clock rolled back after start.json, before child birth."""
    # Guardian wrote started_at at 01:00:00; the clock stepped back 120 seconds
    # before this actual provider was born. It survived guardian death, execed
    # with a clean environment, and created a child in a separate session.
    # Its immutable kernel birth time is 00:58:01, without any PID reuse.
    install(monkeypatch, {43: (1, 43, "Ss", start(-119)),
                          44: (43, 44, "Ss", start(-118))})
    result = procs.containment(42, 42, 43, "job/a1", recorded_identities={42: ident(42)},
                               started_at="2026-10-11T01:00:00Z")
    assert not result.verified_empty, result.to_dict()
    assert result.descendant_pids == {43, 44}


@pytest.mark.parametrize("child_root", [True, False])
def test_recorded_setsid_survivor_is_individually_signalled(monkeypatch, child_root):
    """C-5.4, C-5.6: the filter keeps previously owned escaped survivors signalable."""
    rows = {43: (1 if child_root else 42, 43, "Ss", start(1))}
    a = {"attempt_id": "job/a1", "job_id": "job", "seq": 1, "guardian_pid": 42,
         "child_pid": 43 if child_root else None, "pgid": 42, "boot_id": BOOT_A, "proc_start": start(),
         "started_at": "2026-10-11T01:00:00Z",
         "evidence_json": json.dumps({"owned_identities": {"43": asdict(ident(43, 1))}})}
    signalled, quarantined, saved = [], [], {}

    class Store:
        def get_attempt(self, aid):
            return a

        def one(self, *args):
            return {"id": 1}

        @contextmanager
        def transaction(self, *args, **kwargs):
            yield self

        def execute(self, sql, args):
            if sql.startswith("UPDATE attempts SET killed_by"):
                saved.update(json.loads(args[1])["owned_identities"])

    core = SimpleNamespace(root=Path("/fixture"), store=Store(), term_grace_s=0, kill_settle_s=0,
                           stopping=SimpleNamespace(wait=lambda _: False),
                           _read_json=lambda _: None,
                           _publish=lambda *args: None,
                           _quarantine=lambda *args: quarantined.append(args),
                           _begin_finalizing=lambda *args: pytest.fail("live survivor finalized"))
    core._cancel_info = lambda job_id: None
    core._contain = lambda attempt: Daemon._contain(core, attempt)
    install(monkeypatch, rows)
    monkeypatch.setattr(procs, "same_process", lambda pid, boot, began:
                        pid in rows and boot == BOOT_A and rows[pid][3] == began)
    monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: False)
    monkeypatch.setattr(procs, "signal_process", lambda owned, sig, **kwargs: signalled.append((owned, sig)))
    Daemon._kill_attempt(core, a)
    assert signalled == [(ident(43, 1), signal.SIGKILL)]
    assert saved["43"] == asdict(ident(43, 1))
    assert len(quarantined) == 1
