"""Conservative failed-confirmation groups must not become kill authority."""

import json

import pytest

from subfleet import procs
from tests.fake.test_quarantine_detached_writers import running
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_review_pr131_round5 import late_scan
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
@pytest.mark.parametrize("source", ["marker", "cwd"])
@pytest.mark.parametrize("confirmation", ["stable", "unavailable"])
def test_failed_confirmation_cannot_promote_retained_group_to_signal_authority(
        state_daemon, monkeypatch, tmp_path, consumer, source, confirmation):
    daemon, harness = state_daemon
    a = running(daemon, harness)
    record = {"holder": "probe:" + a["job_id"], "job_id": a["job_id"], "lane_id": a["lane_id"],
              "directory": str(tmp_path / "probe"), "guardian_pid": 100, "pgid": 100,
              "boot_id": BOOT, "proc_start": "guardian", "state": "running"}
    writer = procs.ProcessIdentity(99, BOOT, "observed-writer")
    late_scan(monkeypatch, a, [writer, None], group=700, source=source)
    if consumer == "probe":
        # Probe markers use their holder as the attempt marker.
        monkeypatch.setattr(procs, "_read", lambda *args, **kwargs:
                            f"99 writer SUBFLEET_ATTEMPT={record['holder']}\n" if source == "marker" else "")
        captured = daemon._probe_census(record)
        evidence = record
    else:
        captured = daemon._contain(a)
        evidence = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    assert captured.unverifiable
    assert any(r["pid"] == 99 and r["pgid"] == 700 for r in evidence["lineage_roots"])
    assert "99" not in evidence.get("owned_identities", {})

    # The sampled writer exits; only member 200 remains in its retained group.
    # The verified guardian is alive in its separate, owned group 100.
    marker = record["holder"] if consumer == "probe" else a["attempt_id"]
    script_table(monkeypatch, {100: (1, 100, "S", "guardian"),
                              200: (1, 700, "S", "retained-member")},
                 markers=f"200 writer SUBFLEET_ATTEMPT={marker}\n" if source == "marker" else "")
    monkeypatch.setattr(procs, "cwd_pids", lambda workdir: {200} if source == "cwd" else set())
    member = procs.ProcessIdentity(200, BOOT, "retained-member")
    reads = 0

    def identity(pid):
        nonlocal reads
        assert pid == 200
        reads += 1
        if confirmation == "unavailable" and reads % 2 == 0:
            raise procs.InspectionError("confirmation unavailable")
        return member

    monkeypatch.setattr(procs, "identity", identity)
    monkeypatch.setattr(procs, "same_process", lambda pid, *args, **kwargs: pid in {100, 200})
    signalled_groups, signalled_members = [], []
    monkeypatch.setattr(procs, "signal_group", lambda group, *args, **kwargs: signalled_groups.append(group))
    monkeypatch.setattr(procs, "signal_process", lambda ident, *args, **kwargs: signalled_members.append(ident.pid))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    if consumer == "probe":
        assert not daemon._contain_probe(record)
        evidence = record
    else:
        daemon._kill_attempt(daemon.store.get_attempt(a["attempt_id"]))
        evidence = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    assert set(signalled_groups) <= {100}
    print("AUTHORITY", consumer, source, confirmation, signalled_members, evidence.get("owned_identities"))
    assert 200 not in signalled_members, "retained group member was signalled without recorded ownership"
    assert "200" not in evidence.get("owned_identities", {})
