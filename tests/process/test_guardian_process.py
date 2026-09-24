"""Real macOS ownership checks, explicitly skipped when OS inspection is denied."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet import procs
from tests.restricted import cs_restricted


@pytest.fixture(scope="module", autouse=True)
def macos_inspection():
    if sys.platform != "darwin":
        pytest.skip("C-5.3 requires macOS sysctl kern.boottime and BSD ps")
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-5.3 host sandbox denies ps/sysctl; no ownership bypass")


def wait_file(path, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.exists():
            return path
        time.sleep(0.01)
    raise AssertionError(f"receipt did not appear: {path}")


def argv(tmp_path, command, *, delay=0):
    return [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(tmp_path),
            "--cwd", str(tmp_path), "--stdout-path", str(tmp_path / "stdout"),
            "--stderr-path", str(tmp_path / "stderr"), "--start-delay-s", str(delay), "--", *command]


def environment(marker, root):
    """The C-5.1 marker, scoped by a state root of the test's own so concurrent runs never see each other."""
    return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "SUBFLEET_ATTEMPT": marker, "SUBFLEET_JOB": marker.split("/")[0], "SUBFLEET_ROOT": str(root)}


def cleanup(process):
    current = procs.identity(process.pid)
    if current is not None:
        procs.signal_group(process.pid, signal.SIGKILL, boot_id=current.boot_id, proc_start=current.proc_start)
    process.wait(timeout=3)


def test_guardian_detaches_and_survives_submitting_process_exit(tmp_path):
    """C-5.1 and C-5.2 a submitting process can exit before its detached job finishes."""
    command = argv(tmp_path, [sys.executable, "-c", "import time; time.sleep(.15); print('survived'); raise SystemExit(4)"])
    launcher = subprocess.run([sys.executable, "-c",
        "import subprocess,sys; p=subprocess.Popen(sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)",
        *command], capture_output=True, text=True, env=environment("survive/a1", tmp_path), timeout=3, check=True)
    guardian_pid = int(launcher.stdout.strip())
    start = json.loads(wait_file(tmp_path / "start.json").read_text())
    assert start["guardian_pid"] == start["pgid"] == guardian_pid
    assert start["pgid"] != os.getpgrp()
    receipt = json.loads(wait_file(tmp_path / "exit.json").read_text())
    assert receipt["rc"] == 4
    assert (tmp_path / "stdout").read_text() == "survived\n"


def test_guardian_delayed_receipt_retains_owned_identity(tmp_path):
    """C-4.2 starting waits for a delayed start receipt and then adopts running."""
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", "import time; time.sleep(.2)"], delay=.15),
                               env=environment("delayed/a1", tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(.05)
        assert not (tmp_path / "start.json").exists()
        recorded = procs.identity(process.pid)
        assert recorded is not None
        start = json.loads(wait_file(tmp_path / "start.json").read_text())
        assert start["proc_start"] == recorded.proc_start
        assert procs.same_process(process.pid, start["boot_id"], start["proc_start"])
        assert process.wait(timeout=3) == 0
    finally:
        cleanup(process)


def test_ignore_sigterm_escalates_and_verifies_containment(tmp_path):
    """C-5.4 and C-5.6 a TERM-ignoring child is killed under a verified leader."""
    command = [sys.executable, "-c", "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('ready').touch(); time.sleep(30)"]
    process = subprocess.Popen(argv(tmp_path, command), env=environment("ignore/a1", tmp_path),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        start = json.loads(wait_file(tmp_path / "start.json").read_text())
        wait_file(tmp_path / "ready")
        assert procs.signal_group(process.pid, signal.SIGTERM, boot_id=start["boot_id"], proc_start=start["proc_start"])
        time.sleep(.05)
        assert procs.same_process(process.pid, start["boot_id"], start["proc_start"])
        assert procs.signal_group(process.pid, signal.SIGKILL, boot_id=start["boot_id"], proc_start=start["proc_start"])
        assert process.wait(timeout=3) == -signal.SIGKILL
        assert procs.containment(process.pid, process.pid, None, "ignore/a1", root=str(tmp_path)).verified_empty
    finally:
        cleanup(process)


def test_nested_setsid_survives_group_and_remains_contained_evidence(tmp_path):
    """C-5.5 and C-5.7 an orphan setsid writer remains evidence requiring quarantine."""
    grandchild = "import os,time; from pathlib import Path; Path('escape.pid').write_text(str(os.getpid())); time.sleep(30)"
    provider = "import subprocess,sys,time; from pathlib import Path; " \
               f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],start_new_session=True); " \
               "time.sleep(.2)"
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", provider]),
                               env=environment("escape/a1", tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    escaped = None
    try:
        escaped_pid = int(wait_file(tmp_path / "escape.pid").read_text())
        escaped = procs.identity(escaped_pid)
        assert escaped is not None
        assert process.wait(timeout=3) == 0
        receipt = json.loads((tmp_path / "exit.json").read_text())
        result = procs.containment(process.pid, process.pid, receipt["child_pid"], "escape/a1", root=str(tmp_path))
        assert not result.group_pids
        assert escaped_pid in result.marker_pids
        assert not result.verified_empty
        assert escaped_pid in result.to_dict()["live_pids"]
    finally:
        cleanup(process)
        if escaped is not None:
            procs.signal_process(escaped, signal.SIGKILL)


def test_marker_matches_only_environments_the_kernel_returns(tmp_path, kernel_hides_restricted_environments):
    """C-5.5 the marker finds a process exactly when the kernel returns its environment.

    /bin/sleep and /bin/sh run with CS_RESTRICT, as Apple's own executables do
    (tools/marker_visibility.py), so with SIP on they carry the marker and are
    still invisible to it, while a Python interpreter with the same
    environment is found (observed 2026-09-23 on Darwin 25.6). A host with SIP
    relaxed returns every environment, and the marker then finds all three.
    """
    commands = {"sleep": ["/bin/sleep", "30"], "sh": ["/bin/sh", "-c", "sleep 30; :"],
                "python": [sys.executable, "-c", "import time; time.sleep(30)"]}
    started = {name: subprocess.Popen(command, env=environment("readable/a1", tmp_path), start_new_session=True,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
               for name, command in commands.items()}
    try:
        until = time.monotonic() + 3
        census = procs.containment(None, None, None, "readable/a1", root=str(tmp_path))
        while started["python"].pid not in census.marker_pids and time.monotonic() < until:
            time.sleep(.05)
            census = procs.containment(None, None, None, "readable/a1", root=str(tmp_path))
        assert started["python"].pid in census.marker_pids
        for name, process in started.items():
            hidden = kernel_hides_restricted_environments and cs_restricted(process.pid)
            assert (process.pid in census.marker_pids) is not hidden, name
        if kernel_hides_restricted_environments:
            assert started["sleep"].pid not in census.marker_pids
    finally:
        for process in started.values():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def test_recorded_identity_keeps_a_restricted_setsid_orphan(tmp_path, kernel_hides_restricted_environments):
    """C-5.5 a CS_RESTRICT writer seen before it lost its parent stays in the census; unrecorded, no source finds it."""
    release = tmp_path / "release"
    provider = "\n".join([
        "import subprocess, time",
        "from pathlib import Path",
        "p = subprocess.Popen(['/bin/sh', '-c', 'exec /bin/sleep 30'], start_new_session=True,",
        "                     stdin=subprocess.DEVNULL)",
        "Path('escape.pid').write_text(str(p.pid))",
        "deadline = time.monotonic() + 10",
        f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:",
        "    time.sleep(.02)",
    ])
    root = str(tmp_path)
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", provider]),
                               env=environment("orphan/a1", tmp_path), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    escaped = None
    try:
        escaped_pid = int(wait_file(tmp_path / "escape.pid").read_text())
        escaped = procs.identity(escaped_pid)
        # The census the daemon takes every 0.5 s while the guardian lives.
        seen = procs.containment(process.pid, process.pid, None, "orphan/a1", root=root)
        assert escaped_pid in seen.descendant_pids and escaped_pid not in seen.group_pids
        escaped = seen.identities[escaped_pid]
        release.touch()
        assert process.wait(timeout=5) == 0
        receipt = json.loads(wait_file(tmp_path / "exit.json").read_text())
        blind = procs.containment(process.pid, process.pid, receipt["child_pid"], "orphan/a1", root=root)
        assert procs.same_process(escaped.pid, escaped.boot_id, escaped.proc_start)
        if kernel_hides_restricted_environments:
            # The gap: group, walk and marker all come back empty while the writer runs.
            assert cs_restricted(escaped_pid)
            assert blind.verified_empty
        else:
            assert blind.marker_pids == {escaped_pid}
        kept = procs.containment(process.pid, process.pid, receipt["child_pid"], "orphan/a1", root=root,
                                 recorded=[escaped])
        assert kept.recorded_pids == {escaped_pid}
        assert not kept.verified_empty
        assert kept.shapes[escaped_pid]["ppid"] == 1
    finally:
        cleanup(process)
        if escaped is not None:
            procs.signal_process(escaped, signal.SIGKILL)


def test_marker_only_census_misses_a_restricted_group_member(tmp_path, kernel_hides_restricted_environments):
    """C-5.5 a census with no group and no walk root misses a CS_RESTRICT writer carrying the marker; its group finds it.

    This is the shape of a `git worktree add` whose #!/bin/sh hook left a
    background writer: git has exited, the writer is in the add's group, and
    only the recorded group can see it.
    """
    root = str(tmp_path)
    leader = subprocess.Popen(
        [sys.executable, "-c", "import subprocess; "
         "p = subprocess.Popen(['/bin/sh', '-c', 'sleep 30; :'], stdin=subprocess.DEVNULL); print(p.pid)"],
        env=environment("group/a1", tmp_path), start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        writer = int(leader.stdout.readline())
        assert leader.wait(timeout=3) == 0
        marker_only = procs.containment(None, None, None, "group/a1", root=root)
        if kernel_hides_restricted_environments:
            assert cs_restricted(writer)
            assert marker_only.verified_empty
        else:
            assert writer in marker_only.marker_pids
        grouped = procs.containment(leader.pid, None, None, "group/a1", root=root)
        assert writer in grouped.group_pids and not grouped.verified_empty
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)


def test_writer_orphaned_before_any_census_is_invisible(tmp_path, kernel_hides_restricted_environments):
    """C-5.5 the stated limit: a restricted writer no census saw, in no kept group, with no live kept ancestor.

    The census below comes back verified empty while the writer runs. If a
    source is ever added that sees it, this test fails until it and the
    contract's statement of the limit are updated together.
    """
    if not kernel_hides_restricted_environments:
        pytest.skip("C-5.5: this host returns CS_RESTRICT environments, so the marker sees the writer")
    provider = ("import subprocess; from pathlib import Path; "
                "p = subprocess.Popen(['/bin/sh', '-c', 'exec /bin/sleep 30'], start_new_session=True, "
                "stdin=subprocess.DEVNULL); Path('escape.pid').write_text(str(p.pid))")
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", provider]),
                               env=environment("unseen/a1", tmp_path), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    escaped = None
    try:
        escaped_pid = int(wait_file(tmp_path / "escape.pid").read_text())
        escaped = procs.identity(escaped_pid)
        if process.wait(timeout=5) != 0 or escaped is None or not cs_restricted(escaped_pid):
            pytest.fail("the scenario did not run: provider failed, or the writer is gone or not restricted")
        receipt = json.loads(wait_file(tmp_path / "exit.json").read_text())
        census = procs.containment(process.pid, process.pid, receipt["child_pid"], "unseen/a1", root=str(tmp_path))
        assert procs.same_process(escaped.pid, escaped.boot_id, escaped.proc_start)
        assert census.verified_empty and not census.unverifiable
    finally:
        cleanup(process)
        if escaped is not None:
            procs.signal_process(escaped, signal.SIGKILL)


def test_kept_group_outlives_its_leader_across_a_write(tmp_path):
    """C-5.5 a kept shell's group still counts after the shell exits and a write drops what died.

    The shell E leads its own group and leaves a loop M in it. After E exits,
    M starts N, a new pid, so the next census writes (and drops what has
    died); then M hands a /bin/sleep K to a subshell that exits at once and
    exits itself, so K has no parent any census kept and only E's group ties
    it to the attempt (found in review, 2026-09-24: the first write after E
    exited used to drop E, and K then went unseen).
    """
    from subfleet.daemon import Daemon
    root, marker = str(tmp_path), "group-outlives/a1"
    loop = (f'while [ ! -e "{tmp_path}/step" ]; do /bin/sleep 0.05; done; '
            f'/bin/sleep 30 & echo $! > "{tmp_path}/n.pid"; '
            f'while [ ! -e "{tmp_path}/fork" ]; do /bin/sleep 0.05; done; '
            f'( /bin/sleep 30 & echo $! > "{tmp_path}/k.pid" ); exit 0')
    shell = (f'/bin/sh -c \'{loop}\' & echo $! > "{tmp_path}/m.pid"; '
             f'while [ ! -e "{tmp_path}/exit" ]; do /bin/sleep 0.05; done')
    leader = subprocess.Popen(["/bin/sh", "-c", shell], env=environment(marker, tmp_path), start_new_session=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def census(evidence):
        result = procs.containment(None, None, None, marker, root=root,
                                   recorded=Daemon._recorded(evidence), escaped=Daemon._escaped(evidence))
        return result, Daemon._keep_census(evidence, result, leader=False)

    def gone(pid):
        return not subprocess.run(["/bin/ps", "-p", str(pid), "-o", "pid="], capture_output=True,
                                  text=True).stdout.strip()
    try:
        m = int(wait_file(tmp_path / "m.pid").read_text())
        shell_identity = procs.identity(leader.pid)
        evidence = {"census_identities": {str(leader.pid): {"pid": shell_identity.pid, "boot_id": shell_identity.boot_id,
                                                            "proc_start": shell_identity.proc_start}}}
        census(evidence)
        assert str(leader.pid) in evidence["census_leaders"] and str(m) in evidence["census_identities"]
        (tmp_path / "exit").touch()
        assert leader.wait(timeout=3) == 0
        (tmp_path / "step").touch()
        n = int(wait_file(tmp_path / "n.pid").read_text())
        result, wrote = census(evidence)
        assert wrote and str(n) in evidence["census_identities"]
        assert str(leader.pid) in evidence["census_identities"] and leader.pid in result.kept_groups
        (tmp_path / "fork").touch()
        k = int(wait_file(tmp_path / "k.pid").read_text())
        until = time.monotonic() + 3
        while not gone(m) and time.monotonic() < until:
            time.sleep(.05)
        result, _ = census(evidence)
        assert k in result.recorded_pids and leader.pid in result.kept_groups
        assert result.shapes[k]["ppid"] == 1 and result.shapes[k]["pgid"] == leader.pid
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=3)
