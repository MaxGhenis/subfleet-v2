"""Deterministic ownership checks; real-process acceptance lives in tests/process."""

import errno
import fcntl
import itertools
import json
import signal
import subprocess
import threading
from pathlib import Path
from unittest import mock

import hypothesis
import pytest
from hypothesis import strategies as st

import os

from subfleet import client, procs


def census(monkeypatch, *, groups="", parents="", markers="", fail=None, session=None):
    def read(argv, *, empty_ok=False):
        if fail is not None and any(os.path.basename(str(a)) == fail for a in argv):
            raise procs.InspectionError("unavailable")
        if session is not None and "kern.bootsessionuuid" in argv:
            return session
        if os.path.basename(argv[0]) == "sysctl" and argv[1:2] == ["-n"]:
            return "{ sec = 100, usec = 123 }"
        if "pid=,stat=" in argv:
            return groups
        if "pid=,ppid=,pgid=,stat=,lstart=" in argv:
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


@pytest.mark.parametrize("failed", ["pid=,ppid=,pgid=,stat=,lstart=", "pid=,command="])
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
    assert sum(1 for argv in reads if "pid=,ppid=,pgid=,stat=,lstart=" in argv) == 1
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
    assert procs.liveness(42, "99", "Sat Sep  5 10:00:00 2026") == "unknown"
    assert procs.liveness(0, "100", "Sat Sep  5 10:00:00 2026") == "dead"
    assert procs.liveness(42, None, None) == "dead"
    census(monkeypatch, fail="ps")
    assert procs.liveness(42, "100", "Sat Sep  5 10:00:00 2026") == "unknown"
    assert procs.same_process(42, "100", "Sat Sep  5 10:00:00 2026") is False


START = "Sat Sep  5 10:00:00 2026"
BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"


def test_c5_12_a_census_is_two_ps_reads_however_many_processes_it_finds(monkeypatch):
    """C-5.5, C-5.12 identities come from the snapshot's `lstart`, not from a `ps` per pid."""
    reads = []
    rows = "".join(f"{pid} 42 42 S    {START}    \n" for pid in range(43, 63))
    census(monkeypatch, parents=f"42 1 42 Ss   {START}    \n" + rows)
    inner = procs._read

    def counting(argv, **kwargs):
        reads.append(list(argv))
        return inner(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting)
    result = procs.containment(42, 42, None, "job/a1")
    assert result.group_pids == set(range(42, 63)) and not result.unverifiable
    assert result.identities[50] == procs.ProcessIdentity(50, "100", START)
    assert [os.path.basename(argv[0]) for argv in reads if os.path.basename(argv[0]) == "ps"] == ["ps", "ps"]


def test_c5_12_a_pid_the_snapshot_cannot_describe_is_asked_about_singly(monkeypatch):
    """C-5.5 a marker process born after the snapshot still gets an identity, or leaves the census."""
    census(monkeypatch, parents=f"42 1 42 S {START}\n",
           markers="77 provider SUBFLEET_ATTEMPT=job/a1\n")
    result = procs.containment(42, 42, None, "job/a1")
    assert result.marker_pids == {77}
    assert result.identities[77] == procs.ProcessIdentity(77, "100", START)


def test_c5_12_one_table_answers_identity_for_every_recorded_process(monkeypatch):
    """C-5.3, C-5.12 a table matches pid, boot and start exactly; a zombie, a reused pid and a gone pid do not."""
    census(monkeypatch, parents=f"42 1 42 Ss   {START}    \n43 42 42 Z    {START}\n"
                                f"44 1 44 S    Sun Sep  6 11:00:00 2026\n", session=BOOT_A)
    table = procs.snapshot()
    assert table.boot() == BOOT_A
    assert table.is_process(42, BOOT_A, START)
    assert not table.is_process(42, BOOT_B, START)        # another boot: left to `liveness`
    assert not table.is_process(42, "100", START)         # a legacy record, not asked with `legacy`
    assert not table.is_process(43, BOOT_A, START)        # a zombie is not live
    assert not table.is_process(44, BOOT_A, START)        # the pid was reused
    assert not table.is_process(45, BOOT_A, START)        # gone
    assert not table.is_process(0, BOOT_A, START) and not table.is_process(42, None, None)
    assert table.group(42) == {42} and table.group(None) == frozenset()


def test_c5_12_a_census_that_finds_nothing_needs_no_boot_identity(monkeypatch):
    """C-5.5, C-5.12 a failed `sysctl` cannot make an empty census unverifiable; a census that finds a live pid
    says its identity is unavailable, not that `ps` could not enumerate."""
    census(monkeypatch, parents=f"1 0 1 Ss {START}\n7 1 7 S {START}\n", fail="sysctl")
    result = procs.containment(42, 42, None, "job/a1")
    assert result.verified_empty and result.errors == ()
    census(monkeypatch, parents=f"42 1 42 Ss {START}\n43 42 42 S {START}\n", fail="sysctl")
    result = procs.containment(42, 42, None, "job/a1")
    assert result.unverifiable and result.live_pids == {42, 43}
    assert sorted(result.errors) == ["identity inspection unavailable for pid 42",
                                     "identity inspection unavailable for pid 43"]


def test_c5_12_a_table_reads_the_boot_identity_once_and_only_when_it_needs_it(monkeypatch):
    """C-5.12 no `sysctl` for the read itself or for a pid it does not show, one for all the live pids it
    describes, and a failed one is the table's answer from then on."""
    reads = []
    census(monkeypatch, parents=f"42 1 42 Ss {START}\n43 42 42 S {START}\n", session=BOOT_A)
    inner = procs._read

    def counting(argv, **kwargs):
        reads.append(os.path.basename(argv[0]))
        return inner(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting)
    table = procs.snapshot()
    assert not table.is_process(45, BOOT_A, START) and not table.is_process(42, BOOT_A, "Sun Sep  6 11:00:00 2026")
    assert reads == ["ps"]
    procs.forget_boot_id()                                 # every table would share the module's cache
    assert table.is_process(42, BOOT_A, START) and table.identity(43) == procs.ProcessIdentity(43, BOOT_A, START)
    procs.forget_boot_id()
    assert table.identity(42) == procs.ProcessIdentity(42, BOOT_A, START)
    assert reads == ["ps", "sysctl"]
    census(monkeypatch, parents=f"42 1 42 Ss {START}\n", fail="sysctl")
    failing = procs._read
    sysctl = []

    def counting_sysctl(argv, **kwargs):
        if os.path.basename(argv[0]) == "sysctl":
            sysctl.append(argv[-1])
        return failing(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting_sysctl)
    failed = procs.snapshot()
    for _ in range(3):
        with pytest.raises(procs.InspectionError):
            failed.is_process(42, BOOT_A, START)
    assert sysctl == ["kern.bootsessionuuid", "kern.boottime"]      # one boot-identity read, failed, kept


def test_c5_12_a_table_matches_a_legacy_boot_record_only_when_asked_and_c5_3_agrees(monkeypatch):
    """C-5.3, C-5.12 a `kern.boottime` record is "alive" only when the caller asks for C-5.3's legacy match, as the
    shared table's inspection and owned recording (C-5.6) do, and C-5.3 agrees."""
    census(monkeypatch, parents=f"42 1 42 Ss {START}\n", session=BOOT_A)   # and kern.boottime says 100
    table = procs.snapshot()
    assert not table.is_process(42, "100", START)
    assert table.is_process(42, "100", START, legacy=True)
    assert not table.is_process(42, "99", START, legacy=True)       # shifted: unknown, never the same
    assert not table.is_process(42, BOOT_B, START, legacy=True)     # another boot
    assert not table.is_process(42, "100", "Sun Sep  6 11:00:00 2026", legacy=True)


def test_c5_12_a_table_whose_uuid_read_fell_back_cannot_answer_for_a_uuid_record(monkeypatch):
    """C-5.3, C-5.12 a table that holds `kern.boottime` seconds because its UUID read failed can only call a
    UUID-recorded process unknown, which is a failed read; a legacy record it still answers."""
    table = procs.ProcessTable({42: (1, 42, "Ss", START), 43: (1, 43, "S", "Sun Sep  6 11:00:00 2026")}, "1726000000")
    for legacy in (False, True):
        with pytest.raises(procs.InspectionError):
            table.is_process(42, BOOT_A, START, legacy=legacy)
    assert not table.is_process(43, BOOT_A, START)          # another process: no boot identity needed
    assert not table.is_process(44, BOOT_A, START)          # gone
    assert table.is_process(42, "1726000000", START)
    assert not table.is_process(42, "99", START, legacy=True)


def test_c5_12_an_unreadable_process_table_is_an_inspection_failure(monkeypatch):
    """C-5.5 a table that cannot be read or parsed proves nothing."""
    census(monkeypatch, fail="ps")
    with pytest.raises(procs.InspectionError):
        procs.snapshot()
    census(monkeypatch, parents="not a process row at all\n")
    with pytest.raises(procs.InspectionError):
        procs.snapshot()


def test_c5_12_boot_id_is_read_once_per_window_and_never_cached_on_failure(monkeypatch):
    """C-5.3, C-5.12 one boot-identity read serves BOOT_ID_TTL_S; a failed read is not remembered."""
    reads = []
    census(monkeypatch, session=BOOT_A)
    inner = procs._read

    def counting(argv, **kwargs):
        reads.append(os.path.basename(argv[0]))
        return inner(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting)
    assert [procs.boot_id() for _ in range(5)] == [BOOT_A] * 5
    assert reads == ["sysctl"]
    clock = procs.time.monotonic()
    monkeypatch.setattr(procs.time, "monotonic", lambda: clock + procs.BOOT_ID_TTL_S + 1)
    assert procs.boot_id() == BOOT_A and reads == ["sysctl", "sysctl"]
    procs.forget_boot_id()
    census(monkeypatch, fail="sysctl")
    with pytest.raises(procs.InspectionError):
        procs.boot_id()
    census(monkeypatch, session=BOOT_B)
    assert procs.boot_id() == BOOT_B


def test_c5_12_a_boot_identity_that_fell_back_to_boottime_is_not_remembered(monkeypatch):
    """C-5.3, C-5.12 one failed UUID read returns `kern.boottime` seconds once; kept, they would make
    every UUID-recorded process unknown, and so unsignallable, for BOOT_ID_TTL_S."""
    uuid_reads = []

    def read(argv, *, empty_ok=False):
        if argv[-1] == "kern.bootsessionuuid":
            uuid_reads.append(1)
            if len(uuid_reads) == 1:
                raise procs.InspectionError("sysctl inspection unavailable")     # one transient failure
            return BOOT_A + "\n"
        if argv[-1] == "kern.boottime":
            return "{ sec = 1726000000, usec = 0 } Sat Sep 10 10:00:00 2024\n"
        if argv[-1] == "lstart=":
            return START + "\n"
        if argv[-1] == "stat=":
            return "S\n"
        raise AssertionError(argv)
    monkeypatch.setattr(procs, "_read", read)
    assert procs.boot_id() == "1726000000"                  # the fallback, this once
    assert [procs.liveness(42, BOOT_A, START) for _ in range(3)] == ["alive"] * 3
    assert procs.same_process(42, BOOT_A, START) is True
    assert procs.boot_id() == BOOT_A and len(uuid_reads) == 2   # read again, and that one is kept


def test_c5_12_a_boot_mismatch_is_read_again_before_a_process_is_called_dead(monkeypatch):
    """C-5.3, C-5.12 a cached boot identity may be stale: only a fresh read may say "another boot"."""
    census(monkeypatch, session=BOOT_A)
    assert procs.boot_id() == BOOT_A                       # cached
    census(monkeypatch, session=BOOT_B)                    # what the kernel says now
    assert procs.liveness(42, BOOT_B, START) == "alive"
    assert procs.same_process(42, BOOT_B, START) is True
    assert procs.liveness(42, BOOT_A, START) == "dead"     # a real mismatch stays one


def test_c5_12_ps_and_sysctl_are_started_without_a_fork(monkeypatch):
    """C-5.12 `close_fds=False` is the condition under which CPython uses posix_spawn on macOS."""
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setattr(procs.subprocess, "run", run)
    assert procs._read(["/bin/ps"]) == "ok"
    assert seen["close_fds"] is False
    # posix_spawn also needs an executable named by path, not found on PATH.
    assert os.path.isabs(procs.TABLE_ARGV[0])


def test_c5_12_a_table_reads_kern_boottime_once_for_every_legacy_record(monkeypatch):
    """C-5.3, C-5.12 the `kern.boottime` seconds a legacy record is matched against are read once per table, the
    first time one needs them, and kept failed or not: every legacy attempt given the shared table shares one
    `sysctl`, and a UUID record needs none."""
    reads = []
    census(monkeypatch, parents=f"42 1 42 Ss {START}\n43 1 43 Ss {START}\n", session=BOOT_A)
    inner = procs._read

    def counting(argv, **kwargs):
        reads.append(argv[-1] if os.path.basename(argv[0]) == "sysctl" else "ps")
        return inner(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting)
    table = procs.snapshot()
    assert table.is_process(42, BOOT_A, START, legacy=True)
    assert reads == ["ps", "kern.bootsessionuuid"]
    assert table.is_process(42, "100", START, legacy=True) and table.is_process(43, "100", START, legacy=True)
    assert not table.is_process(43, "99", START, legacy=True)
    assert reads == ["ps", "kern.bootsessionuuid", "kern.boottime"]

    def no_seconds(argv, *, empty_ok=False):
        reads.append(argv[-1])
        raise procs.InspectionError("sysctl inspection unavailable")
    monkeypatch.setattr(procs, "_read", no_seconds)
    reads.clear()
    table = procs.ProcessTable({42: (1, 42, "Ss", START), 43: (1, 43, "Ss", START)}, BOOT_A)
    for pid in (42, 43, 42):
        with pytest.raises(procs.InspectionError):
            table.is_process(pid, "100", START, legacy=True)
    assert reads == ["kern.boottime"]


FAIL = object()


def test_c5_12_the_legacy_match_says_alive_exactly_where_liveness_does():
    """C-5.3, C-5.12, differential: the shared table's inspection asks `is_process(legacy=True)` where `liveness`
    was asked before. Over every combination of recorded boot identity, `sysctl` answers, recorded start and the
    process's state, the table says True exactly where `liveness` says "alive", raises (decides nothing) only
    where it says "unknown", and says False only where it says "dead" or "unknown", when the daemon asks
    `liveness` afresh. Exhaustive (2,520 cases); the final review of PR #37 compared 22 of them by hand."""
    lettered = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    records = [BOOT_A, BOOT_B, lettered, lettered.upper(), "{" + lettered + "}", BOOT_A + "\n",
               "100", "99", 100, "", None, "abc"]
    uuids = [BOOT_A, BOOT_B, lettered, lettered.upper(), "", "garbage", FAIL]
    seconds = ["{ sec = 100, usec = 0 }", "{ sec = 99, usec = 5 }", "100", "", FAIL]
    starts = [START, "Sun Sep  6 11:00:00 2026"]
    states = ["Ss", "Z", None]                                  # None: the pid is gone
    seen = set()
    for recorded, uuid, boottime, started, stat in itertools.product(records, uuids, seconds, starts, states):
        def read(argv, *, empty_ok=False):
            argv = [str(part) for part in argv]
            if os.path.basename(argv[0]) == "sysctl":
                answer = uuid if argv[-1] == "kern.bootsessionuuid" else boottime
                if answer is FAIL:
                    raise procs.InspectionError("sysctl inspection unavailable")
                return answer + "\n"
            if argv == procs.TABLE_ARGV:
                return f"1 0 1 Ss {START}\n" + (f"42 1 42 {stat} {START}\n" if stat else "")
            if argv[1:2] == ["-p"]:
                return "" if stat is None else (START if argv[-1] == "lstart=" else stat) + "\n"
            raise AssertionError(argv)
        with mock.patch.object(procs, "_read", read):
            procs.forget_boot_id()
            live = procs.liveness(42, recorded, started)
            procs.forget_boot_id()
            try:
                shown = procs.snapshot().is_process(42, recorded, started, legacy=True)
            except procs.InspectionError:
                shown = "raised"
            procs.forget_boot_id()
        case = (recorded, "fail" if uuid is FAIL else uuid, "fail" if boottime is FAIL else boottime, started, stat)
        assert (shown is True) == (live == "alive"), (case, shown, live)
        assert shown != "raised" or live == "unknown", (case, shown, live)
        assert shown is not False or live in ("dead", "unknown"), (case, shown, live)
        seen.add((shown, live))
    # Every answer occurs, so the comparison is not vacuous.
    assert {(True, "alive"), (False, "dead"), (False, "unknown"), ("raised", "unknown")} <= seen


def test_c5_12_inspections_reading_a_table_s_boot_identity_at_once_share_one_sysctl(monkeypatch):
    """C-5.12 a table's boot-identity read is serialised: two first callers at the same instant cost one `sysctl`.

    Final review of PR #37, 2026-09-26: removing the table's lock survived every test (mutant M20)."""
    reads = []
    together = threading.Barrier(2)

    def slow_boot_id():
        reads.append(1)
        try:
            together.wait(timeout=.5)                           # both inside at once only without the lock
        except threading.BrokenBarrierError:
            pass
        return BOOT_A
    monkeypatch.setattr(procs, "boot_id", slow_boot_id)
    table = procs.ProcessTable({42: (1, 42, "Ss", START)})
    found = []
    threads = [threading.Thread(target=lambda: found.append(table.boot()), daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert found == [BOOT_A, BOOT_A]
    assert reads == [1]


def test_c5_5_a_marker_gone_by_its_identity_read_needs_no_boot_identity(monkeypatch):
    """C-5.5, C-5.12 a marker process born after the snapshot and gone by the identity read leaves the census, which
    needs no `sysctl`: a failed one cannot make that census unverifiable.

    Final review of PR #37, 2026-09-26: reading the boot identity before checking the pid is live survived every
    test (mutant M22)."""
    def read(argv, *, empty_ok=False):
        if os.path.basename(argv[0]) == "sysctl":
            raise procs.InspectionError("unavailable")
        if "pid=,ppid=,pgid=,stat=,lstart=" in argv:
            return f"1 0 1 Ss {START}\n"
        if "pid=,command=" in argv:
            return "77 provider SUBFLEET_ATTEMPT=job/a1\n"
        if "stat=" in argv:
            return "S"                                          # alive when the marker scan looked
        if "lstart=" in argv:
            return ""                                           # gone by the identity read
        raise AssertionError(argv)
    monkeypatch.setattr(procs, "_read", read)
    result = procs.containment(42, 42, None, "job/a1")
    assert result.verified_empty and result.errors == (), result


def test_c5_5_the_whole_process_table_has_one_reader():
    """C-5.5, C-5.12 `snapshot()` is the one reader of the whole process table, and a row that is not a process is
    an `InspectionError` there. The merge of 2026-09-26 left a second, `_process_table()`, which nothing called and
    which let a `ValueError` escape; it is gone, so no new caller can take it up."""
    assert not hasattr(procs, "_process_table")


def test_c5_12_legacy_records_asked_about_one_table_at_once_share_one_kern_boottime(monkeypatch):
    """C-5.12 the legacy attempts given the shared table ask about it together, on the tick after it is read: the
    first reads `kern.boottime` and the others wait for that read rather than each running one.

    Review of these fixes, 2026-09-27: without a lock, 50 of 50 trials read it twice for two legacy records."""
    reads = []
    together = threading.Barrier(2)

    def read(argv, *, empty_ok=False):
        assert argv[-1] == "kern.boottime", argv
        reads.append(1)
        try:
            together.wait(timeout=.5)                           # both inside at once only without the lock
        except threading.BrokenBarrierError:
            pass
        return "{ sec = 100, usec = 0 }\n"
    monkeypatch.setattr(procs, "_read", read)
    table = procs.ProcessTable({42: (1, 42, "Ss", START), 43: (1, 43, "Ss", START)}, BOOT_A)
    found = []
    threads = [threading.Thread(target=lambda pid=pid: found.append(table.is_process(pid, "100", START, legacy=True)),
                                daemon=True) for pid in (42, 43)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert found == [True, True]
    assert reads == [1]

@pytest.mark.parametrize("error", [OSError(errno.EMFILE, "descriptor table full"), KeyboardInterrupt()],
                         ids=["emfile", "interrupt"])
@pytest.mark.parametrize("fail_at", [1, 2])
def test_a_pipe_that_cannot_be_moved_above_the_standard_streams_leaves_no_descriptor(monkeypatch, fail_at, error):
    """A full descriptor table (or any other exception) while either end is moved up
    closes every descriptor the call made and hands the error to the caller, which then
    launches nothing."""
    real_pipe, real_fcntl = os.pipe, fcntl.fcntl
    made, moves = [], []

    def pipe():
        ends = real_pipe()
        made.extend(ends)
        return ends

    def move(fd, command, arg=0):
        if command != fcntl.F_DUPFD_CLOEXEC:
            return real_fcntl(fd, command, arg)
        moves.append(fd)
        if len(moves) == fail_at:
            raise error
        made.append(real_fcntl(fd, command, arg))
        return made[-1]

    monkeypatch.setattr(procs.os, "pipe", pipe)
    monkeypatch.setattr(procs.fcntl, "fcntl", move)
    with pytest.raises(type(error)) as raised:
        procs.pipe_above_stdio()
    monkeypatch.undo()
    assert raised.value is error
    assert len(made) == 2 + fail_at - 1
    for fd in made:
        with pytest.raises(OSError) as gone:
            os.fstat(fd)
        assert gone.value.errno == errno.EBADF


def test_a_pipe_is_returned_empty_whatever_its_raw_ends_took_in(monkeypatch):
    """While its raw write end sat at 1 or 2, another thread's output to that stream
    could land in the pipe (review of e3c35ff). A gate holding a byte its owner never
    wrote is a refusal to its guardian, or a release before its identity is recorded;
    so the pipe comes back empty and blocking, and the owner's byte is the first read."""
    real_pipe = os.pipe

    def pipe():
        ends = real_pipe()
        os.write(ends[1], b"1" + b"stray output\n" * 500)
        return ends

    monkeypatch.setattr(procs.os, "pipe", pipe)
    read_fd, write_fd = procs.pipe_above_stdio()
    monkeypatch.undo()
    try:
        assert os.get_blocking(read_fd)
        os.set_blocking(read_fd, False)
        with pytest.raises(BlockingIOError):
            os.read(read_fd, 1)
        os.set_blocking(read_fd, True)
        os.write(write_fd, b"0")
        assert os.read(read_fd, 2) == b"0"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_containment_finds_a_marker_under_a_root_that_is_not_ascii(monkeypatch):
    """C-5.5 (2026-09-27): `ps` runs in the C locale, where it prints `é` as `M-CM-)`,
    so the census looks for the root as `ps` prints it. Matched as written, a root
    that is not printable ASCII found no marked process, and one outside the group
    and the walk was in no source: the census said verified empty while it lived."""
    census(monkeypatch, parents="42 1 42 S\n",
           markers=("   99 python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/subfleet-JosM-CM-)-root\n"
                    "  100 python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/subfleet-Jose-root\n"))
    found = procs.containment(None, None, None, "job/a1", root="/tmp/subfleet-José-root")
    assert found.marker_pids == {99}
    assert not found.verified_empty
    assert procs.containment(None, None, None, "job/a1", root="/tmp/subfleet-Jose-root").marker_pids == {100}


def test_containment_looks_for_the_attempt_id_as_ps_prints_it_too(monkeypatch):
    """C-5.5: the attempt marker is rendered like the root. Attempt ids are ASCII
    today (a timestamp, a `[a-z0-9-]` slug, `probe:` and hex), where rendering changes
    nothing; an id that were not would still be found."""
    census(monkeypatch, parents="42 1 42 S\n",
           markers="   99 python SUBFLEET_ATTEMPT=jM-CM-)b/a1 SUBFLEET_ROOT=/tmp/root\n")
    assert procs.containment(None, None, None, "jéb/a1", root="/tmp/root").marker_pids == {99}


def test_containment_keeps_the_space_a_root_ends_in(monkeypatch):
    """C-5.5: a root that ends in a space ends the row when SUBFLEET_ROOT is the last
    variable, so only the pid's padding is stripped from a row."""
    census(monkeypatch, parents="42 1 42 S\n",
           markers="   99 python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/root \n")
    assert procs.containment(None, None, None, "job/a1", root="/tmp/root ").marker_pids == {99}


def test_two_roots_that_print_alike_are_one_root_to_the_census(monkeypatch):
    """C-5.5, intended: `ps` passes `VIS_NOSLASH`, so its notation puts no backslash
    before an `M-` form and cannot tell `é` from the text `M-CM-)`; the census counts a
    marked process of either root for both. That errs toward holding on, and it needs
    the attempt id too."""
    census(monkeypatch, parents="42 1 42 S\n",
           markers="   99 python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/aM-CM-)\n")
    assert procs.containment(None, None, None, "job/a1", root="/tmp/aé").marker_pids == {99}
    assert procs.containment(None, None, None, "job/a1", root="/tmp/aM-CM-)").marker_pids == {99}


@pytest.mark.parametrize("line", [
    "python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/a b",                 # the root, then more after a space
    "python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/b SUBFLEET_ROOT=/tmp/a",  # inside a longer root
    "python SUBFLEET_ATTEMPT=job/a1 NOTE=see SUBFLEET_ROOT=/tmp/a here SUBFLEET_ROOT=/tmp/b",  # in another variable
    "python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/a",                   # the attempt marker as argv text
], ids=["root then a space", "inside a longer root", "in another variable", "attempt in argv"])
def test_a_process_counts_when_its_line_holds_both_markers_as_tokens(monkeypatch, line):
    """C-5.5, intended (and older than `ps_text`): a process counts when its printed
    line holds both markers as space-bounded tokens, wherever they come from (its
    argv, another variable's value, or a longer root that holds a space), because
    `ps` joins every string with a space. Each errs toward holding on: no caller
    signals a pid the marker source alone found (C-5.6)."""
    census(monkeypatch, parents="42 1 42 S\n", markers=f"   99 {line}\n")
    assert procs.containment(None, None, None, "job/a1", root="/tmp/a").marker_pids == {99}


def test_a_longer_root_without_a_space_is_another_root(monkeypatch):
    """C-5.5: the root's token ends where the printed line has a space or ends, so a
    root that only starts like the census's root is another root."""
    census(monkeypatch, parents="42 1 42 S\n",
           markers="   99 python SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/ab\n")
    assert procs.containment(None, None, None, "job/a1", root="/tmp/a").marker_pids == frozenset()


@pytest.mark.parametrize("line", [
    "python SUBFLEET_ATTEMPT=job/a1 MY_SUBFLEET_ROOT=/tmp/a",
    "python MY_SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/tmp/a",
], ids=["root", "attempt"])
def test_a_marker_is_a_whole_token_from_its_name(monkeypatch, line):
    """C-5.5: each marker starts where the printed line starts or has a space, so a
    variable whose name only ends in a marker's name is not that marker."""
    census(monkeypatch, parents="42 1 42 S\n", markers=f"   99 {line}\n")
    assert procs.containment(None, None, None, "job/a1", root="/tmp/a").marker_pids == frozenset()


def test_ps_text_prints_every_byte_as_ps_printed_it():
    """C-5.5: `PS_BYTES` is what `ps` printed for each of the 255 bytes an environment
    can hold, measured on macOS 26.6.2 (tests/fixtures/ps_vis_bytes.json; the process
    test `test_ps_rendering.py` measures again wherever it runs)."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ps_vis_bytes.json"
    measured = json.loads(fixture.read_text())["bytes"]
    assert sorted(measured) == [f"{value:02X}" for value in range(1, 256)]
    assert {name: procs.PS_BYTES[int(name, 16)] for name in measured} == measured


@pytest.mark.parametrize("value,printed", [
    ("/tmp/subfleet-José-root", "/tmp/subfleet-JosM-CM-)-root"),
    ("a\tb\nc", "a\\011b\\012c"),
    ("a back\\slash and a space ", "a back\\slash and a space "),
    ("\u00a0", "M-B\\240"),
    ("\x01\x1b\x7f", "^A^[^?"),
    (os.fsdecode(b"\x80\x89\xa0\xde\xff"), "M^@M^I\\240M-^M^?"),
    ("M-CM-)", "M-CM-)"),
])
def test_ps_text_examples(value, printed):
    """C-5.5: a value as `ps` prints it, byte by byte, from its `os.fsencode` bytes."""
    assert procs.ps_text(value) == printed


@hypothesis.settings(max_examples=300, deadline=None)
@hypothesis.given(raw=st.binary(max_size=64).map(lambda raw: raw.replace(b"\0", b"")))
def test_ps_text_invariants(raw):
    """C-5.5 invariants the census's `(?:^|\\s)` and `(?=\\s|$)` boundaries rely on: for
    any bytes a value can hold, `ps_text` prints only printable ASCII, with a space
    exactly where the value had one (so no other byte can end or start a variable),
    prints printable ASCII as itself, and prints a value one byte at a time."""
    printed = procs.ps_text(os.fsdecode(raw))
    assert all(" " <= char <= "~" for char in printed)
    assert printed.count(" ") == raw.count(b" ")
    assert printed == "".join(procs.PS_BYTES[byte] for byte in raw)
    if all(0x20 <= byte <= 0x7E for byte in raw):
        assert printed == raw.decode("ascii")


def ps_row(pid, argv, env):
    """A `ps -axEww -o pid=,command=` row as `ps` prints it in the C locale."""
    words = [*argv, *(f"{name}={value}" for name, value in env)]
    return f"{pid:>5} " + " ".join(procs.ps_text(word) for word in words) + "\n"


@hypothesis.settings(max_examples=200, deadline=None)
@hypothesis.given(raw=st.binary(min_size=1, max_size=48).map(lambda raw: raw.replace(b"\0", b"")).filter(bool),
                  root_last=st.booleans())
def test_containment_finds_the_marker_under_any_root(raw, root_last):
    """C-5.5 for every root: whatever bytes the state root holds, and wherever its
    variable falls, the census finds the process that carries both markers as `ps`
    prints them, and a root one byte longer does not. (What `ps` prints is pinned by
    the fixture above and measured by tests/process/test_ps_rendering.py.)"""
    root = os.fsdecode(raw)
    markers = [("SUBFLEET_ATTEMPT", "job/a1"), ("SUBFLEET_ROOT", root)]
    env = [("PATH", "/bin"), *(markers if root_last else markers[::-1])]
    with pytest.MonkeyPatch.context() as patch:
        census(patch, parents="42 1 42 S\n", markers=ps_row(99, ["python", "-c", "pass"], env))
        assert procs.containment(None, None, None, "job/a1", root=root).marker_pids == {99}
        longer = os.fsdecode(raw + b"x")
        assert procs.containment(None, None, None, "job/a1", root=longer).marker_pids == frozenset()


def test_a_root_that_cannot_be_encoded_leaves_the_census_unverifiable(monkeypatch):
    """C-5.5: a root `os.fsencode` cannot encode (a lone surrogate) is carried by no
    process, and the census cannot say what `ps` would print for it; the marker
    source is unavailable, never empty."""
    census(monkeypatch, parents="42 1 42 S\n", markers="   99 python SUBFLEET_ATTEMPT=job/a1\n")
    result = procs.containment(None, None, None, "job/a1", root="/tmp/\ud800")
    assert result.unverifiable and "marker enumeration unavailable" in result.errors


@pytest.mark.parametrize("value", ["/tmp/a\x00b", "\x00"])
def test_a_value_with_a_nul_is_one_no_environment_holds(monkeypatch, value):
    """C-5.5: a NUL ends an environment string, so no process carries a root or an
    attempt id that holds one. `ps_text` refuses it rather than printing `^@`, and a
    census asked for one is unverifiable, as for a lone surrogate, never empty."""
    with pytest.raises(ValueError):
        procs.ps_text(value)
    census(monkeypatch, parents="42 1 42 S\n", markers="   99 python SUBFLEET_ATTEMPT=job/a1\n")
    assert procs.containment(None, None, None, "job/a1", root=value).unverifiable
    assert procs.containment(None, None, None, "job/a\x001", root="/tmp/a").unverifiable
