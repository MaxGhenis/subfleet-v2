"""Clock corrections cannot manufacture process death or permit unsafe signals."""

import pytest

from subfleet import boot_identity, client, procs

SESSION = "66355737-51db-46d4-8f31-c928bc955e16"
OTHER = "f465f03e-c801-4ee0-a0e4-283540a3b06f"
START = "Sun Sep 20 23:26:42 2026"


def test_kernel_session_uuid_takes_priority_over_wall_clock():
    calls = []
    def read(argv):
        calls.append(argv[-1])
        return SESSION.upper() if argv[-1] == "kern.bootsessionuuid" else "{ sec = 1789915544 }"
    assert boot_identity.read_identity(read) == SESSION
    assert calls == ["kern.bootsessionuuid"]


def test_old_kernel_without_uuid_can_still_record_seconds():
    assert boot_identity.read_identity(lambda argv: "" if argv[-1] == "kern.bootsessionuuid"
                                       else "{ sec = 1789915544, usec = 508109 }") == "1789915544"


@pytest.mark.parametrize("recorded,expected", [(SESSION, "alive"), (OTHER, "dead"),
                                              ("1789915544", "alive"), ("1789915546", "unknown")])
def test_guardian_identity_handles_new_and_legacy_records(monkeypatch, recorded, expected):
    monkeypatch.setattr(procs, "identity", lambda pid: procs.ProcessIdentity(pid, SESSION, START))
    monkeypatch.setattr(procs, "_read", lambda argv: "{ sec = 1789915544, usec = 508109 }")
    assert procs.liveness(5362, recorded, START) == expected
    assert procs.same_process(5362, recorded, START) == (expected == "alive")


def test_legacy_drift_does_not_block_socket_or_authorize_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "boot_id", lambda: SESSION)
    monkeypatch.setattr(client, "_boot_read", lambda argv: "{ sec = 1789915544 }")
    # The identity check reads state and start from one `ps` (C-5.11), so that is the seam.
    monkeypatch.setattr(client, "proc_status", lambda pid: ("S", START))
    monkeypatch.setattr(procs, "identity", lambda pid: procs.ProcessIdentity(pid, SESSION, START))
    monkeypatch.setattr(procs, "_read", lambda argv: "{ sec = 1789915544 }")
    monkeypatch.setattr(procs.os, "killpg", lambda *args: pytest.fail("must not signal uncertain identity"))
    assert client.identity_report(5362, "1789915546", START)[0] is None
    instance = client.Client(tmp_path)
    monkeypatch.setattr(instance, "lock_info", lambda: {
        "pid": 5362, "boot_id": "1789915546", "proc_start": START})
    instance.check_available()  # the socket, not uncertain legacy metadata, answers availability
    assert not procs.signal_group(5362, 15, boot_id="1789915546", proc_start=START)


def test_missing_and_reused_pid_still_prove_death_despite_legacy_drift(monkeypatch):
    monkeypatch.setattr(client, "boot_id", lambda: SESSION)
    monkeypatch.setattr(client, "_boot_read", lambda argv: "{ sec = 1789915544 }")
    for status in (("", ""), ("S", "Mon Sep 21 10:00:00 2026")):   # gone; the pid reused
        monkeypatch.setattr(client, "proc_status", lambda pid, status=status: status)
        assert client.identity_report(5362, "1789915546", START)[0] is False


def test_client_records_real_reboot_with_session_uuid(monkeypatch):
    monkeypatch.setattr(client, "boot_id", lambda: OTHER)
    assert client.identity_report(5362, SESSION, START)[0] is False


def test_numeric_fallback_is_not_cached(monkeypatch):
    monkeypatch.setattr(client, "_BOOT_ID", [])
    values = iter(("1789915546", "1789915544", SESSION, OTHER))
    monkeypatch.setattr(client, "_read_boot_id", lambda: next(values))
    assert [client.boot_id() for _ in range(4)] == ["1789915546", "1789915544", SESSION, SESSION]
