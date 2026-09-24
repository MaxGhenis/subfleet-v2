"""Deterministic ownership checks; real-process acceptance lives in tests/process."""

import json
import signal
import subprocess

import pytest

import os

from subfleet import client, procs

STARTED = "Sat Sep  5 10:00:00 2026"


def census(monkeypatch, *, groups="", parents="", markers="", fail=None):
    def read(argv, *, empty_ok=False):
        if fail is not None and any(os.path.basename(str(a)) == fail for a in argv):
            raise procs.InspectionError("unavailable")
        if os.path.basename(argv[0]) == "sysctl" and argv[1:2] == ["-n"]:
            return "{ sec = 100, usec = 123 }"
        if "pid=,stat=" in argv:
            return groups
        if "pid=,ppid=,pgid=,stat=,lstart=" in argv:
            # A row given without a start identity started at STARTED.
            return "".join((row if len(row.split(None, 4)) == 5 else f"{row} {STARTED}") + "\n"
                           for row in parents.splitlines() if row.strip())
        if "pid=,command=" in argv:
            return markers
        if "lstart=" in argv:
            return STARTED
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


def test_marker_row_without_an_environment_never_matches(monkeypatch):
    """C-5.5 a CS_RESTRICT process's ps -E row carries only its arguments, so the marker cannot match it."""
    census(monkeypatch, parents="99 1 99 S\n100 1 100 S\n",
           markers="99 /bin/sleep 30\n100 python -c pass SUBFLEET_ATTEMPT=job/a1 SUBFLEET_ROOT=/root\n")
    result = procs.containment(None, None, None, "job/a1", root="/root")
    assert result.marker_pids == {100}


def test_containment_recorded_identity_keeps_an_escaped_orphan(monkeypatch):
    """C-5.5 a recorded writer outside the group, orphaned, and invisible to the marker is still live."""
    census(monkeypatch, parents="42 1 42 Z\n99 1 99 S\n", markers="99 /bin/sleep 30\n")
    writer = procs.ProcessIdentity(99, "100", STARTED)
    assert procs.containment(42, 42, None, "job/a1").verified_empty
    result = procs.containment(42, 42, None, "job/a1", recorded=[writer])
    assert result.recorded_pids == {99}
    assert result.live_pids == {99}
    assert not result.verified_empty
    assert result.identities[99] == writer
    assert result.shapes[99] == {"ppid": 1, "pgid": 99, "stat": "S"}
    assert result.to_dict()["recorded_pids"] == [99]


def test_containment_walks_below_a_recorded_process(monkeypatch):
    """C-5.5 the fourth source is a kept process and every live descendant it started after the last census."""
    census(monkeypatch, parents="99 1 99 S\n100 99 99 S\n101 100 101 S\n102 100 102 Z\n")
    result = procs.containment(42, 42, None, "job/a1", recorded=[procs.ProcessIdentity(99, "100", STARTED)])
    assert result.recorded_pids == {99, 100, 101}
    assert not result.descendant_pids
    assert set(result.identities) == {99, 100, 101}


@pytest.mark.parametrize("row", ["99 1 99 S Sun Sep  6 11:00:00 2026", "99 1 99 Z", ""],
                         ids=["reused-pid", "zombie", "exited"])
def test_containment_recorded_identity_that_is_gone_is_not_live(monkeypatch, row):
    """C-5.3, C-5.5 a recorded pid now held by another start, a zombie, or nothing counts as dead."""
    census(monkeypatch, parents=row + "\n" if row else "")
    result = procs.containment(42, 42, None, "job/a1", recorded=[procs.ProcessIdentity(99, "100", STARTED)])
    assert result.verified_empty
    assert not result.recorded_pids


def test_containment_recorded_identity_from_another_boot_session_is_dead(monkeypatch):
    """C-5.3, C-5.5 a boot-session UUID that differs is another boot: the recorded process is gone."""
    census(monkeypatch, parents="99 1 99 S\n")
    monkeypatch.setattr(procs, "boot_id", lambda: "7e0a5b4c-1111-4222-8333-944455556666")
    other = procs.ProcessIdentity(99, "0b1c2d3e-aaaa-4bbb-8ccc-9dddeeeeffff", STARTED)
    assert procs.containment(42, 42, None, "job/a1", recorded=[other]).verified_empty


def test_containment_recorded_identity_of_uncertain_boot_is_unverifiable(monkeypatch):
    """C-5.3, C-5.5 a legacy boot timestamp that differs is unknown, never dead: the census cannot release."""
    census(monkeypatch, parents="99 1 99 S\n")
    result = procs.containment(42, 42, None, "job/a1", recorded=[procs.ProcessIdentity(99, "99", STARTED)])
    assert result.unverifiable and not result.verified_empty
    assert result.errors == ("identity inspection unavailable for recorded pid 99",)


def test_containment_recorded_identities_add_no_process_reads(monkeypatch):
    """C-5.5 recorded identities are checked against the one snapshot, not with a ps call per pid."""
    reads = []
    census(monkeypatch, parents="99 1 99 S\n")
    inner = procs._read

    def counting(argv, **kwargs):
        reads.append(list(argv))
        return inner(argv, **kwargs)
    monkeypatch.setattr(procs, "_read", counting)
    dead = [procs.ProcessIdentity(pid, "100", STARTED) for pid in range(1000, 1500)]
    result = procs.containment(42, 42, None, "job/a1", recorded=[*dead, procs.ProcessIdentity(99, "100", STARTED)])
    assert result.recorded_pids == {99}
    assert not any("-p" in argv for argv in reads)


@pytest.mark.parametrize("leader_row", ["", "99 1 99 Z"], ids=["exited", "zombie"])
def test_containment_counts_the_group_a_kept_escapee_left(monkeypatch, leader_row):
    """C-5.5 a background job left by a kept setsid'd shell is the attempt's, although no census saw it."""
    census(monkeypatch, parents=(leader_row + "\n" if leader_row else "") + "150 1 99 S\n151 150 151 S\n")
    shell = procs.ProcessIdentity(99, "100", STARTED)
    result = procs.containment(42, 42, None, "job/a1", recorded=[shell], escaped=[shell])
    assert result.recorded_pids == {150, 151}
    assert not result.verified_empty
    assert procs.containment(42, 42, None, "job/a1", recorded=[shell]).verified_empty


def test_containment_ignores_a_group_whose_leader_pid_was_reused(monkeypatch):
    """C-5.3, C-5.5 once the kept escapee's pid belongs to another process, the group with that id is not the attempt's."""
    census(monkeypatch, parents="99 1 99 S Sun Sep  6 11:00:00 2026\n150 99 99 S\n")
    shell = procs.ProcessIdentity(99, "100", STARTED)
    assert procs.containment(42, 42, None, "job/a1", recorded=[shell], escaped=[shell]).verified_empty


def test_containment_ignores_a_group_recorded_on_another_boot(monkeypatch):
    """C-5.3, C-5.5 an escapee recorded on another boot leads no group of this boot."""
    census(monkeypatch, parents="150 1 99 S\n")
    monkeypatch.setattr(procs, "boot_id", lambda: "7e0a5b4c-1111-4222-8333-944455556666")
    shell = procs.ProcessIdentity(99, "0b1c2d3e-aaaa-4bbb-8ccc-9dddeeeeffff", STARTED)
    assert procs.containment(42, 42, None, "job/a1", recorded=[shell], escaped=[shell]).verified_empty


def test_containment_matched_legacy_identity_is_reported_under_the_current_boot(monkeypatch):
    """C-5.3, C-5.5 a kept identity recorded under the kern.boottime fallback comes back under the boot-session UUID."""
    census(monkeypatch, parents="99 1 99 S\n")
    current = "7e0a5b4c-1111-4222-8333-944455556666"
    monkeypatch.setattr(procs, "boot_id", lambda: current)
    result = procs.containment(42, 42, None, "job/a1", recorded=[procs.ProcessIdentity(99, "100", STARTED)])
    assert result.recorded_pids == {99}
    assert result.identities[99] == procs.ProcessIdentity(99, current, STARTED)


def test_containment_an_exited_leaders_group_counts_only_while_it_has_members(monkeypatch):
    """C-5.5 a kept leader's group stops counting once empty: its id may be reused, and no error is raised."""
    shell = procs.ProcessIdentity(99, "100", STARTED)
    census(monkeypatch, parents="150 1 99 S\n")
    occupied = procs.containment(42, 42, None, "job/a1", recorded=[shell], escaped=[shell])
    assert occupied.kept_groups == {99} and occupied.recorded_pids == {150}
    census(monkeypatch, parents="150 1 150 S\n")
    empty = procs.containment(42, 42, None, "job/a1", recorded=[shell], escaped=[shell])
    assert not empty.kept_groups and empty.verified_empty


def test_containment_an_exited_leader_of_unknown_boot_with_members_is_unverifiable(monkeypatch):
    """C-5.3, C-5.5 whether a living group is a kept leader's cannot be told: the census cannot release."""
    census(monkeypatch, parents="150 1 99 S\n")
    shell = procs.ProcessIdentity(99, "99", STARTED)      # legacy boot seconds that no longer match
    result = procs.containment(42, 42, None, "job/a1", recorded=[shell], escaped=[shell])
    assert result.unverifiable and not result.verified_empty
    assert result.errors == ("identity inspection unavailable for kept group leader 99",)
    census(monkeypatch, parents="")
    assert procs.containment(42, 42, None, "job/a1", recorded=[shell], escaped=[shell]).verified_empty


@pytest.mark.parametrize("row,seen", [("42 1 42 S", True), ("42 1 42 S Sun Sep  6 11:00:00 2026", False),
                                      ("42 1 42 Z", False), ("", False)],
                         ids=["alive", "reused", "zombie", "exited"])
def test_containment_reports_whether_its_snapshot_shows_the_leader(monkeypatch, row, seen):
    """C-5.3, C-5.5 the census says whether its own snapshot shows the recorded guardian alive."""
    census(monkeypatch, parents=row + "\n" if row else "")
    result = procs.containment(42, 42, None, "job/a1", leader=procs.ProcessIdentity(42, "100", STARTED))
    assert result.leader_verified is seen
    assert procs.containment(42, 42, None, "job/a1").leader_verified is False
