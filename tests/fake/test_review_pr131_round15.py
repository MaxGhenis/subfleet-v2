"""Round-15 guardian liveness witnesses using real delivery authorization."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import signal

import pytest

from subfleet import procs
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import BOOT
from tests.fake.test_state_contract import state_daemon


@pytest.mark.parametrize("guardian_claim", ["clear", "foreign", "ambiguous"])
def test_scalar_confirmed_guardian_missing_census_identity_is_signalled(
        state_daemon, monkeypatch, guardian_claim):
    daemon, harness = state_daemon
    ours, _ = running(daemon, harness, 200, 201)
    guardian = procs.ProcessIdentity(200, BOOT, "p200")
    child = procs.ProcessIdentity(201, BOOT, "p201")
    evidence = {"child_identity": asdict(child),
                "owned_identities": {"201": asdict(child)}}
    daemon.store.update_attempt(ours["attempt_id"], evidence_json=json.dumps(evidence))
    census = procs.Containment(
        group_pids=frozenset({200, 201}), identities={201: child},
        group_identities={201: child}, shapes={201: {"pgid": 200}},
        ambiguous_identities=(guardian,) if guardian_claim == "ambiguous" else (),
        foreign_ownership=procs.ForeignOwnership(
            claims=(guardian,) if guardian_claim == "foreign" else ()))
    monkeypatch.setattr(daemon, "_contain", lambda *args, **kwargs: census)
    monkeypatch.setattr(procs, "same_process", lambda pid, boot, start:
                        boot == BOOT and (pid, start) in {(200, "p200"), (201, "p201")})
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: 200)
    sent, authorizations = [], []
    authority = daemon._signal_claim_authority

    @contextmanager
    def observed_authority(*args):
        with authority(*args) as allowed:
            authorizations.append((args[1].pid, allowed))
            yield allowed

    monkeypatch.setattr(daemon, "_signal_claim_authority", observed_authority)
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(procs.os, "killpg", lambda *args: pytest.fail("group delivery"))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(ours)
    assert (201, signal.SIGKILL) in sent
    assert authorizations and all(allowed for _, allowed in authorizations)
    if guardian_claim == "clear":
        assert (200, signal.SIGTERM) in sent
        assert (200, signal.SIGKILL) in sent
        assert (200, True) in authorizations
        actual = json.loads(daemon.store.get_attempt(ours["attempt_id"])["evidence_json"])
        assert actual["owned_identities"]["200"] == asdict(guardian)
    else:
        assert all(pid != 200 for pid, _ in sent)
