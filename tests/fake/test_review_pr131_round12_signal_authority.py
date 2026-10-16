"""Round-12 cross-attempt signal authority probe using production signal code."""
import json

import pytest

from subfleet import procs
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import BOOT, script_table
from tests.fake.test_state_contract import state_daemon  # noqa: F401

_REAL_SAME_PROCESS = procs.same_process


@pytest.mark.parametrize("same_start", [False, True])
@pytest.mark.parametrize("foreign_state", ["running", "quarantined"])
def test_recycled_owned_pid_in_verified_foreign_session_is_not_signalled(
        state_daemon, monkeypatch, same_start, foreign_state):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    foreign, _ = running(daemon, harness, 300, 301)
    rows = {200: (1, 200, "Ss", "p200"), 201: (200, 200, "S", "p201"),
            202: (201, 200, "S", "p202"),
            300: (1, 300, "Ss", "p300"), 301: (300, 300, "S", "p301")}
    script_table(monkeypatch, rows)
    daemon._record_owned(ours, procs.snapshot())
    before = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
    assert before["owned_identities"]["202"] == {
        "pid": 202, "boot_id": BOOT, "proc_start": "p202"}

    # The original local writer and its guardian/provider all exited. The
    # independent foreign guardian is authenticated in its own session/group
    # 300, and its provider forks a new writer on the recycled PID. lstart may
    # collide within the same second. An old process from session 200 cannot
    # join independently created session/group 300, so the current group proof
    # distinguishes the new foreign incarnation from the old local authority.
    rows.clear()
    new_start = "p202" if same_start else "p202-new"
    rows.update({300: (1, 300, "Ss", "p300"), 301: (300, 300, "S", "p301"),
                 202: (301, 300, "S", new_start)})
    script_table(monkeypatch, rows)
    daemon._record_owned(foreign, procs.snapshot())
    captured = json.loads(daemon.store.get_attempt(foreign["attempt_id"])["evidence_json"])
    current = {"pid": 202, "boot_id": BOOT, "proc_start": new_start}
    if same_start:
        assert "202" not in captured["owned_identities"]
        assert current in captured["ambiguous_identities"]
    else:
        assert captured["owned_identities"]["202"] == current
    if foreign_state == "quarantined":
        daemon._quarantine(foreign, daemon._contain(foreign), "foreign writers remain")
    assert daemon.store.get_attempt(foreign["attempt_id"])["state"] == foreign_state

    # Keep production liveness, same_process, signal_group and signal_process;
    # only process inspection and the final kernel delivery seam are scripted.
    # An absent local guardian cannot authorize any group signal.
    monkeypatch.setattr(procs, "same_process", _REAL_SAME_PROCESS)
    deliveries = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: deliveries.append((pid, int(sig))))
    monkeypatch.setattr(procs.os, "killpg", lambda *args: pytest.fail("absent guardian allowed group signal"))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    assert not deliveries, (deliveries, new_start, rows)
    if same_start:
        actual = daemon.store.get_attempt(ours["attempt_id"])
        assert actual["state"] == "quarantined"
        assert json.loads(actual["evidence_json"])["owned_identities"]["202"] == before["owned_identities"]["202"]
