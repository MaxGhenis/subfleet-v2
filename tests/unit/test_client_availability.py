"""C-5.3, C-5.13: stopped state never overrides the recorded process identity."""

import json

import pytest

from subfleet import client

START = "Sun Sep 20 23:26:42 2026"


@pytest.mark.parametrize("state", ["T", "T+", "Ts<", "R", "S", "Z", "U", "I"])
def test_state_is_read_with_identity_once(root, monkeypatch, state):
    """C-5.13 identity and state come from the same ps observation."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    calls = []

    def read(argv, **kwargs):
        calls.append(argv)
        return 0, state + " " + START

    monkeypatch.setattr(client, "_run", read)
    pid, alive, _, observed = client.Client(root).holder_report()
    assert pid == 12345 and alive == (not state.startswith("Z"))
    assert observed == state
    assert calls == [["/bin/ps", "-p", "12345", "-o", "state=,lstart="]]


@pytest.mark.parametrize("rc,output,expected", [(-1, "", None), (0, "", None),
                                             (1, "", False), (2, "", None),
                                             (0, "Z " + START, False)])
def test_unknown_and_gone_remain_distinct(root, monkeypatch, rc, output, expected):
    """C-5.3 inability to inspect a holder never proves death or a stop."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: (rc, output))
    probe = client.Client(root)
    assert probe.lock_holder_alive() is expected
    assert probe.stopped_holder() is None


@pytest.mark.parametrize("rc", [1, 2])
def test_failed_ps_cannot_verify_stopped_holder(root, monkeypatch, rc):
    """C-5.3, C-5.13 failed ps stdout cannot establish a stopped identity."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: (rc, "T+ " + START))
    probe = client.Client(root)
    assert probe.holder_report()[1:] == (None, "ps could not report on pid 12345", None)
    assert probe.stopped_holder() is None
    probe.check_available()  # unknown identity leaves the socket to decide


@pytest.mark.parametrize("output", ["T+", "T+ not-a-start", "? " + START,
                                      "T+ " + START + "\nS " + START,
                                      "T+ " + START + " extra"])
def test_malformed_ps_rows_are_unverifiable(monkeypatch, output):
    """C-5.3 one complete state/start row is required, even on ps success."""
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: (0, output))
    assert client.proc_status(12345) == (None, None)
    assert client.identity_status(12345, None, START)[0] is None


def test_reused_stopped_pid_is_stale_not_the_daemon(root, monkeypatch):
    """C-5.3, C-5.13 a stopped successor must never be diagnosed as our daemon."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: (0, "T Mon Sep 21 10:00:00 2026"))
    with pytest.raises(client.DaemonUnavailable) as caught:
        client.Client(root).check_available()
    assert not isinstance(caught.value, client.DaemonStopped)
    assert "is stale" in str(caught.value)


def test_stopped_refusal_after_busy_is_unavailable(root, monkeypatch):
    """C-16.7 a stopped holder never turns a refused retry into another busy wait."""
    probe = client.Client(root)
    monkeypatch.setattr(probe, "lock_holder_alive", lambda: True)
    stopped = client.DaemonStopped(12345, "T")
    stopped.__cause__ = ConnectionRefusedError()
    assert client.refused_while_busy(probe, stopped) is False
