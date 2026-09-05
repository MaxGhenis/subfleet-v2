"""Deterministic ownership checks; real-process acceptance lives in tests/process."""

import json
import signal
import subprocess

import pytest

import os

from subfleet import client, procs


def census(monkeypatch, *, groups="", parents="", markers="", fail=None):
    def read(argv, *, empty_ok=False):
        if fail is not None and any(os.path.basename(str(a)) == fail for a in argv):
            raise procs.InspectionError("unavailable")
        if os.path.basename(argv[0]) == "sysctl" and argv[1:2] == ["-n"]:
            return "{ sec = 100, usec = 123 }"
        if "pid=,stat=" in argv:
            return groups
        if "pid=,ppid=,pgid=,stat=" in argv:
            return parents
        if "pid=,command=" in argv:
            return markers
        if "lstart=" in argv:
            return "Sat Sep  5 10:00:00 2026"
        if "stat=" in argv:
            return "S"
        raise AssertionError(argv)
    monkeypatch.setattr(procs, "_read", read)


def test_boot_identity_uses_sysctl_seconds(monkeypatch):
    """C-5.3 boot identity extracts only kern.boottime seconds."""
    census(monkeypatch)
    assert procs.boot_id() == "100"


@pytest.mark.parametrize("parent_path", ["/fixture/bin", None])
def test_daemon_identity_matches_cli_outside_utc(monkeypatch, parent_path):
    """C-5.3: locale/timezone differences cannot make a live daemon lock look stale."""
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    if parent_path is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", parent_path)
    calls = []

    def ps(argv, **kwargs):
        env = kwargs.get("env", os.environ)
        calls.append(dict(env))
        utc = env.get("TZ") == "UTC" and env.get("LC_ALL") == "C"
        # Model ps rendering the same process under two parent environments.
        started = "Sat Sep  5 14:00:00 2026" if utc else "sam. sept.  5 10:00:00 2026"
        prefix = "S " if argv[-1] == "state=,lstart=" else ""
        return subprocess.CompletedProcess(argv, 0, prefix + started + "\n", "")

    monkeypatch.setattr(procs.subprocess, "run", ps)
    recorded = procs.proc_start(4242)
    assert client.same_process(4242, None, recorded) is True
    assert " ".join(recorded.split()) == client.proc_start(4242)
    assert calls[0] == {"LC_ALL": "C", "LANG": "C", "TZ": "UTC",
                        "PATH": parent_path or "/usr/bin:/bin"}
    assert os.environ["TZ"] == "America/New_York"


@pytest.mark.parametrize("boot,started,expected", [
    ("100", "Sat Sep  5 10:00:00 2026", True),
    ("101", "Sat Sep  5 10:00:00 2026", False),
    ("100", "Sat Sep  5 10:00:01 2026", False),
])
def test_same_process_requires_both_identities(monkeypatch, boot, started, expected):
    """C-5.3 pid reuse or a different boot cannot establish ownership."""
    census(monkeypatch)
    assert procs.same_process(42, boot, started) is expected


def test_zombie_is_not_the_same_live_process(monkeypatch):
    """C-5.3 and C-5.5 zombies do not count as live processes."""
    monkeypatch.setattr(procs, "proc_start", lambda pid: "recorded")
    monkeypatch.setattr(procs, "_stat", lambda pid: "Z+")
    assert not procs.same_process(42, "100", "recorded")


def test_containment_three_sources_find_setsid_escape(monkeypatch):
    """C-5.5 an escaped orphan remains visible through its inherited marker."""
    census(monkeypatch, parents="42 1 42 S\n43 42 43 S\n44 42 42 Z\n99 1 99 S\n",
           markers="99 python SUBFLEET_ATTEMPT=job/a1 PRIVATE_TOKEN=secret-sentinel\n"
                   "100 python SUBFLEET_ATTEMPT=job/a10\n")
    result = procs.containment(42, 42, None, "job/a1")
    assert result.group_pids == {42}
    assert result.descendant_pids == {42, 43}
    assert result.marker_pids == {99}
    assert result.live_pids == {42, 43, 99}
    assert not result.verified_empty
    assert result.shapes[43] == {"ppid": 42, "pgid": 43, "stat": "S"}
    assert set(result.to_dict()["shapes"]) == {"42", "43", "99"}
    assert "secret-sentinel" not in json.dumps(result.to_dict())
    assert "PRIVATE_TOKEN" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("failed", ["pid=,ppid=,pgid=,stat=", "pid=,command="])
def test_containment_failed_source_is_unverifiable(monkeypatch, failed):
    """C-5.5 every enumeration source must succeed before releasing a workspace."""
    census(monkeypatch, fail=failed)
    result = procs.containment(42, 42, None, "job/a1")
    assert result.unverifiable
    assert not result.verified_empty


def test_containment_empty_all_sources_proves_release(monkeypatch):
    """C-5.5 verified empty requires all three sources to return no live pid."""
    census(monkeypatch)
    assert procs.containment(42, 42, None, "job/a1").verified_empty


def test_containment_descends_from_recorded_child_after_guardian_exit(monkeypatch):
    """C-5.5 recorded child roots preserve a descendant census after reparenting."""
    census(monkeypatch, parents="43 1 43 S\n44 43 43 S\n45 44 43 S\n")
    assert procs.containment(42, 42, 43, "job/a1").descendant_pids == {43, 44, 45}


def test_signal_group_refuses_reused_leader(monkeypatch):
    """C-5.4 group signals require the recorded leader's complete identity."""
    census(monkeypatch)
    monkeypatch.setattr(procs.os, "getpgrp", lambda: 7)
    monkeypatch.setattr(procs.os, "killpg", lambda *args: pytest.fail("unexpected signal"))
    assert not procs.signal_group(42, signal.SIGTERM, boot_id="old", proc_start="old")


def test_signal_group_checks_recorded_leader_before_signal(monkeypatch):
    """C-5.4 a matching group leader permits signalling exactly its own group."""
    census(monkeypatch)
    sent = []
    monkeypatch.setattr(procs.os, "getpgrp", lambda: 7)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(procs.os, "killpg", lambda *args: sent.append(args))
    assert procs.signal_group(42, signal.SIGTERM, boot_id="100", proc_start="Sat Sep  5 10:00:00 2026")
    assert sent == [(42, signal.SIGTERM)]


def test_signal_survivor_rechecks_original_identity(monkeypatch):
    """C-5.6 pid reuse prevents signalling an individually recorded survivor."""
    census(monkeypatch)
    monkeypatch.setattr(procs.os, "kill", lambda *args: pytest.fail("unexpected signal"))
    assert not procs.signal_process(procs.ProcessIdentity(42, "100", "old-start"), signal.SIGKILL)


def test_empty_bsd_ps_selector_is_not_inspection_failure(monkeypatch):
    """C-5.5 BSD ps status 1 with empty output means a valid empty selection."""
    monkeypatch.setattr(procs.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", ""))
    assert procs._read(["/bin/ps", "-p", "99999"], empty_ok=True) == ""


def test_ps_permission_denial_is_not_empty(monkeypatch):
    """C-5.5 permission failures remain unverifiable, even with no process rows."""
    monkeypatch.setattr(procs.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "denied"))
    with pytest.raises(procs.InspectionError):
        procs._read(["/bin/ps"], empty_ok=True)


def test_proc_start_retry_recovers_from_one_empty_pass(monkeypatch):
    """C-5.3 hardening: a single empty ps pass on a fresh pid does not lose the identity."""
    answers = iter([None, None, "Sat Sep  5 10:00:00 2026"])
    calls = []
    monkeypatch.setattr(procs, "proc_start", lambda pid: (calls.append(pid), next(answers))[1])
    monkeypatch.setattr(procs.time, "sleep", lambda s: None)
    assert procs.proc_start_retry(4242, tries=5, delay_s=0) == "Sat Sep  5 10:00:00 2026"
    assert calls == [4242, 4242, 4242]


def test_proc_start_retry_stops_when_process_is_gone(monkeypatch):
    """C-5.3 hardening: retries stop as soon as the caller reports the process exited."""
    calls = []
    monkeypatch.setattr(procs, "proc_start", lambda pid: (calls.append(pid), None)[1])
    monkeypatch.setattr(procs.time, "sleep", lambda s: None)
    assert procs.proc_start_retry(7, tries=5, delay_s=0, alive=lambda: False) is None
    assert calls == [7]


def test_proc_start_retry_reraises_when_every_pass_fails(monkeypatch):
    """C-5.3 hardening: inspection that never succeeds still reports unavailable."""
    def boom(pid):
        raise procs.InspectionError("ps inspection unavailable")
    monkeypatch.setattr(procs, "proc_start", boom)
    monkeypatch.setattr(procs.time, "sleep", lambda s: None)
    with pytest.raises(procs.InspectionError):
        procs.proc_start_retry(9, tries=3, delay_s=0)


def test_containment_marker_requires_the_state_root_when_given(monkeypatch):
    """C-5.5 a marker match from another state root is not a writer of this attempt."""
    census(monkeypatch, parents="42 1 42 S\n",
           markers=("99 python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/other-root\n"
                    "100 python SUBFLEET_ROOT=/tmp/this-root SUBFLEET_ATTEMPT=job/a1\n"))
    scoped = procs.containment(42, 42, None, "job/a1", root="/tmp/this-root")
    assert scoped.marker_pids == {100}
    unscoped = procs.containment(42, 42, None, "job/a1")
    assert unscoped.marker_pids == {99, 100}


def test_containment_group_and_walk_share_one_snapshot(monkeypatch):
    """C-5.5 the group and the descendant walk are read from one process-table snapshot."""
    reads = []
    original = census(monkeypatch, parents="42 1 42 S\n43 42 42 S\n50 1 42 S\n60 50 60 S\n")
    inner = procs._read

    def counting(argv, **kwargs):
        reads.append(list(argv))
        return inner(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting)
    result = procs.containment(42, 42, None, "job/a1")
    # 50 kept the group after reparenting to launchd; 60 is its child in a new group.
    assert result.group_pids == {42, 43, 50}
    assert result.descendant_pids == {42, 43}
    assert result.live_pids == {42, 43, 50}
    assert result.shapes[50] == {"ppid": 1, "pgid": 42, "stat": "S"}
    assert sum(1 for argv in reads if "pid=,ppid=,pgid=,stat=" in argv) == 1
    assert not any("-g" in argv for argv in reads)
    assert "command" not in json.dumps(result.to_dict()["shapes"])


def test_containment_zombie_group_member_is_not_live(monkeypatch):
    """C-5.5 a zombie in the recorded group is already reaped for containment purposes."""
    census(monkeypatch, parents="42 1 42 Z\n43 42 42 Z\n")
    assert procs.containment(42, 42, 43, "job/a1").verified_empty


def test_liveness_has_three_answers_and_unknown_never_means_dead(monkeypatch):
    """C-5.3, C-4.2 an inspection failure is "unknown"; only same_process collapses it to False."""
    census(monkeypatch)
    assert procs.liveness(42, "100", "Sat Sep  5 10:00:00 2026") == "alive"
    assert procs.liveness(42, "100", "Fri Sep  4 09:00:00 2026") == "dead"
    assert procs.liveness(42, "99", "Sat Sep  5 10:00:00 2026") == "dead"
    assert procs.liveness(0, "100", "Sat Sep  5 10:00:00 2026") == "dead"
    assert procs.liveness(42, None, None) == "dead"
    census(monkeypatch, fail="ps")
    assert procs.liveness(42, "100", "Sat Sep  5 10:00:00 2026") == "unknown"
    assert procs.same_process(42, "100", "Sat Sep  5 10:00:00 2026") is False
