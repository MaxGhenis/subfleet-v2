"""Adversarial round-12 liveness checks; all process inspection is scripted."""
import json

import pytest

from subfleet import procs, protocol
from tests.fake.test_quarantine_self_resolve import Clock, assert_released
from tests.fake.test_review_pr131_probes import BOOT, NEXT_BOOT, script_table
from tests.fake.test_review_pr131_round11 import owned_writer
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize("local_source", ["owned", "provider", "descendant", "unknown-boot", "foreign-launch"])
@pytest.mark.parametrize("ending", ["absent", "zombie", "different-start", "reboot"])
@pytest.mark.parametrize("resolver", ["automatic", "operator"])
def test_collision_hold_releases_when_colliding_process_ends(
        state_daemon, monkeypatch, local_source, ending, resolver):
    """L1: foreign history stays live, but no dead local writer holds forever."""
    daemon, ours, _, rows = owned_writer(state_daemon, monkeypatch)
    clock = Clock(monkeypatch, daemon)
    rows.pop(200)
    rows.pop(201)
    rows[202] = (1, 202, "Ss", "p202")
    current = {"pid": 202, "boot_id": BOOT, "proc_start": "p202"}
    if local_source in {"provider", "descendant"}:
        daemon.store.update_attempt(ours["attempt_id"], evidence_json=json.dumps({
            local_source + "_identities": [current],
        }))
    elif local_source == "unknown-boot":
        incomplete = {**current, "boot_id": ""}
        daemon.store.update_attempt(ours["attempt_id"], evidence_json=json.dumps({
            "owned_identities": {"202": incomplete},
            "owned_identity_history": {"202": [incomplete]},
        }))
    elif local_source == "foreign-launch":
        other = daemon.store.one("SELECT * FROM attempts WHERE attempt_id<>?", (ours["attempt_id"],))
        daemon.store.update_attempt(other["attempt_id"], guardian_pid=202, pgid=202,
                                    boot_id=BOOT, proc_start="p202")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: frozenset())
    script_table(monkeypatch, rows, markers="")
    census = daemon._contain(ours)
    assert 202 in census.live_pids, census.to_dict()
    daemon._quarantine(ours, census, "round-12 process identity collision")
    held = daemon.store.get_attempt(ours["attempt_id"])
    daemon.store.update_attempt(held["attempt_id"], quarantine_recheck_at="")
    if resolver == "automatic":
        daemon._recheck_quarantines()
    else:
        daemon._resolve_quarantine(held, protocol.KillArgs(held["job_id"], confirm_dead=True))
    assert daemon.store.get_attempt(held["attempt_id"])["state"] == "quarantined"
    if ending == "absent":
        rows.pop(202)
    elif ending == "zombie":
        rows[202] = (1, 202, "Z", "p202")
    elif ending == "different-start":
        rows[202] = (1, 303, "Ss", "unrelated-start")
    else:
        script_table(monkeypatch, rows, markers="", boot=NEXT_BOOT)
        if local_source == "unknown-boot":
            # A missing boot cannot disprove a same-start replacement. L1's
            # evidence-empty premise holds only once that ambiguous PID ends.
            clock.advance()
            if resolver == "automatic":
                daemon._recheck_quarantines()
            else:
                daemon._resolve_quarantine(held, protocol.KillArgs(held["job_id"], confirm_dead=True))
            assert daemon.store.get_attempt(held["attempt_id"])["state"] == "quarantined"
            rows.pop(202)
    # Keep unrelated process rows 100/101 present, so the liveness
    # oracle is stronger than clearing the entire simulated process table.
    assert {100, 101} <= rows.keys()
    clock.advance()
    if resolver == "automatic":
        daemon._recheck_quarantines()
    else:
        daemon._resolve_quarantine(held, protocol.KillArgs(held["job_id"], confirm_dead=True))
    assert_released(daemon, held)


def test_process_world_s1_s2_k1_l1_p1_200_examples(monkeypatch):
    """Exercise the independent process-world oracle at a larger step budget."""
    from hypothesis import Phase, settings
    from hypothesis.stateful import run_state_machine_as_test
    from tests.fake.test_quarantine_process_world import ProofProcessWorldMachine

    monkeypatch.setenv("SF_WORLD_POOL", "1")
    run_state_machine_as_test(ProofProcessWorldMachine, settings=settings(
        max_examples=200, stateful_step_count=40, deadline=30000,
        database=None, derandomize=True,
        phases=[phase for phase in Phase if phase != Phase.explain],
    ))
