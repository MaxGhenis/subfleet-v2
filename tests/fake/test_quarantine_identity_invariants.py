"""Independent identity oracle across every source and both resolver paths."""

import json

from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import procs, protocol
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_review_pr131_probes import BOOT, ORIGINAL_CENSUS, ident, quarantine
from tests.fake.test_review_pr131_round3 import held
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(source="marker", missing=False, old_state="Z", pid=99, group=800, generation=1)
@example(source="cwd", missing=False, old_state="S", pid=400, group=801, generation=2)
@example(source="descendant", missing=True, old_state="S", pid=200, group=802, generation=3)
@given(source=st.sampled_from(["lineage", "group", "descendant", "cwd", "marker"]),
       missing=st.booleans(), old_state=st.sampled_from(["S", "Z"]),
       pid=st.integers(200, 600), group=st.integers(700, 900), generation=st.integers(1, 10000))
def test_observed_identity_retention_reuse_uncertainty_and_resolver_parity(
        state_daemon, monkeypatch, source, missing, old_state, pid, group, generation):
    daemon, harness = state_daemon
    daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
    clock = Clock(monkeypatch, daemon)
    attempts = getattr(daemon, "_identity_invariant_attempts", None)
    if attempts is None:
        attempts = [quarantine(daemon, harness) for _ in range(2)]
        daemon._identity_invariant_attempts = attempts
    writer_start = f"writer-{generation}"
    world = {"phase": 0}

    def rows():
        phase = world["phase"]
        if phase == 3:
            return {}
        if phase == 0:
            if source in {"cwd", "marker"}:
                return {pid: (1, group + 1000, old_state, "old-unrelated")}
            result = {pid: (100 if source == "descendant" else 1,
                            100 if source == "group" else group, "S", "" if missing else writer_start)}
            if source == "descendant":
                result[100] = (1, 100, "S", "guardian-start")
            return result
        return {pid: (1, group + (2000 if phase == 1 else 3000), "S",
                      writer_start if phase == 1 else "replacement")}

    def current(pid):
        if world["phase"] == 0 and source in {"cwd", "marker"}:
            return None if missing else procs.ProcessIdentity(pid, BOOT, writer_start)
        return procs.ProcessTable(rows(), BOOT).identity(pid)

    monkeypatch.setattr(procs, "containment", ORIGINAL_CENSUS)
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(rows(), BOOT))
    monkeypatch.setattr(procs, "identity", current)
    monkeypatch.setattr(procs, "_stat", lambda p: "S" if world["phase"] < 3 else None)
    monkeypatch.setattr(procs, "process_group", lambda p: group if world["phase"] == 0 else group + 2000)
    monkeypatch.setattr(procs, "_read", lambda argv, **kw:
        f"{pid} writer SUBFLEET_ROOT={daemon.root}\n"
        if source == "marker" and world["phase"] == 0 else "")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir:
        frozenset({pid}) if source == "cwd" and world["phase"] == 0 else frozenset())

    for a in attempts:
        evidence = {"owned_identities": {str(pid): ident(pid, writer_start)}} if source == "lineage" else {}
        daemon.store.update_attempt(a["attempt_id"], state="quarantined", evidence_json=json.dumps(evidence),
                                    quarantine_reason="{}", quarantine_recheck_at="")
        daemon.store.update_job(a["job_id"], state="lost", rc=125)
        daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])

    for phase in range(4):
        world["phase"] = phase
        # Two attempts see the same independently generated process world.
        censuses = [daemon._contain(a).to_dict() for a in attempts]
        assert censuses[0] == censuses[1]
        clock.advance()
        daemon._resolve_quarantine(attempts[0], None)
        daemon._resolve_quarantine(attempts[1], protocol.KillArgs(attempts[1]["job_id"], confirm_dead=True))
        outcomes = [held(daemon, a) for a in attempts]
        assert [(a["state"], bool(leases)) for a, leases in outcomes] == [
            (outcomes[0][0]["state"], bool(outcomes[0][1]))] * 2
        should_hold = phase < 2 or (phase == 2 and missing)
        for actual, leases in outcomes:
            assert (actual["state"] == "quarantined") == should_hold
            assert bool(leases) == should_hold
            if phase == 0:
                roots = json.loads(actual["evidence_json"])["lineage_roots"]
                assert any(root["pid"] == pid and root["proc_start"] == ("" if missing else writer_start)
                           for root in roots)
                if source in {"cwd", "marker"} and not missing:
                    assert {root["pgid"] for root in roots if root["pid"] == pid} == {group}
