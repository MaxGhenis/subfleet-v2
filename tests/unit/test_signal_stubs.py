"""Signal doubles must keep authorization active through fake delivery."""
from contextlib import contextmanager

import pytest

from subfleet.procs import ProcessIdentity
from tests.signal_stubs import authorized_signal_stub


@pytest.mark.parametrize("allowed", [False, True])
def test_signal_stub_honors_authorization_through_delivery(allowed):
    events = []
    identity = ProcessIdentity(200, "boot", "start")

    @contextmanager
    def authorize():
        events.append("enter")
        try:
            yield allowed
        finally:
            events.append("exit")

    def deliver(recorded, sig):
        assert recorded == identity and sig == 9
        assert events == ["enter"]
        events.append("deliver")
        return True

    assert authorized_signal_stub(deliver)(identity, 9, authorize=authorize) is allowed
    assert events == (["enter", "deliver", "exit"] if allowed else ["enter", "exit"])
