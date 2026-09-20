"""The socket boundary cannot turn malformed input into reserve authorization."""

import pytest

from subfleet.protocol import ProtocolError
from tests.fake.test_state_contract import state_daemon


@pytest.mark.parametrize("changes", [
    {"unmeasured_reserve_reason": ""},
    {"unmeasured_reserve_reason": " \n "},
    {"unmeasured_reserve_reason": True},
    {"unmeasured_reserve_reason": {"reason": "approved"}},
    {"unmeasured_reserve_reason": "x" * 2001},
    {"pinned_lane": None},
    {"pinned_model": None},
    {"kind": "resume"},
    {"kind": "revive"},
    {"kind": "gate-review"},
])
def test_invalid_authorization_is_rejected_before_creating_job(state_daemon, changes):
    service, harness = state_daemon
    args = harness.submit_args(pinned_lane="codex-1", pinned_model="astra",
                              unmeasured_reserve_reason="Operator approves an unmeasured probe")
    args.update(changes)
    with pytest.raises(ProtocolError, match="unmeasured"):
        service.dispatch("submit", args)
    assert service.store.list_jobs() == []
    assert service.store.list_attempts() == []


def test_authorization_dry_run_does_not_write_or_probe(state_daemon, monkeypatch):
    service, harness = state_daemon
    def unexpected_probe(*args, **kwargs):
        raise AssertionError("dry-run must not call the provider")
    monkeypatch.setattr(service, "_execute_probe", unexpected_probe)
    result = service.dispatch("submit", harness.submit_args(
        pinned_lane="codex-1", pinned_model="astra", dry_run=True,
        unmeasured_reserve_reason="Operator approves an unmeasured probe"))
    assert result["dry_run"] is True
    assert service.store.list_jobs() == []
    assert service.store.list_attempts() == []
    assert service.store.list_leases() == []
