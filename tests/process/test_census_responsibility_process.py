"""D-CEN1, C-5.1, C-5.5 on the real kernel: guardians started as responsibility roots.

`procs.spawn_disclaimed` starts a guardian as its own responsible process with
`posix_spawn`, as Popen started it before in every other way. Every process the
provider starts then names the guardian as responsible, whatever it does to its
session, group, parent, title or environment, and the census counts it by that;
but only while the guardian lives, since the kernel names a process as its own
responsible process once the one that was is gone. So a rooted guardian stays after
its exit receipt until nothing else names it. Every process here is this test's own
and is stopped on failure too; escapers exit by themselves within a minute.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest

from subfleet import procs

REPO = Path(__file__).resolve().parents[2]
PERL_ESCAPE = ("import os, sys\n"
               "if os.fork(): sys.exit(0)\n"
               "os.execv('/usr/bin/perl', ['/usr/bin/perl', '-e', "
               "'$0 = \"renamed-escaper\"; open(my $o, \">\", \"$ENV{ESCAPER}.tmp\") or die; print $o $$; "
               "close($o); rename(\"$ENV{ESCAPER}.tmp\", $ENV{ESCAPER}); sleep 60;'])\n")
PROVIDER = ("import os, subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {PERL_ESCAPE!r}], start_new_session=True)\n"
            "while not os.path.exists(os.environ['ESCAPER']): time.sleep(.02)\n")


@pytest.fixture(scope="module", autouse=True)
def responsibility_available():
    if sys.platform != "darwin":
        pytest.skip("responsibility is macOS's")
    if not procs.disclaim_available():
        pytest.skip("this macOS cannot start a process as its own responsible process")
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-5.3 host sandbox denies ps/sysctl; no ownership bypass")


def executable(pid: int) -> str:
    """`proc_pidpath`: the file the kernel runs for `pid`, symlinks resolved."""
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(4096)
    size = libc.proc_pidpath(ctypes.c_int(pid), buffer, ctypes.c_uint32(len(buffer)))
    assert size > 0, ctypes.get_errno()
    return buffer.value.decode()


def stop(child) -> None:
    """Kill and reap a process this test started (unreaped, its pid is still its own)."""
    if child is not None and child.poll() is None:
        child.kill()
    if child is not None:
        child.wait(timeout=10)


def stop_identity(identity: procs.ProcessIdentity | None) -> None:
    if identity is not None:
        procs.signal_process(identity, signal.SIGKILL)


def wait_for(path: Path, timeout: float = 30) -> str:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(.02)
    raise AssertionError(f"{path} did not appear")


def test_d_cen1_a_disclaimed_guardian_starts_as_popen_started_it(tmp_path):
    """C-5.1: stdin, stdout and stderr on /dev/null; every descriptor closed but the
    launch gate, which the child reads above 2; the working directory and exactly the
    environment given; SIGPIPE at its default before Python sets its own. And it is its
    own responsible process, running the daemon's own executable (the identity a
    privacy check asks about), where a Popen child names this process's responsible."""
    gate_r, gate_w = procs.pipe_above_stdio()
    stray_r, stray_w = os.pipe()                     # inheritable: must not reach the child
    os.set_inheritable(stray_r, True)
    report = tmp_path / "report.json"
    code = ("import json, os, sys\n"
            "fd = int(sys.argv[1]); gate = os.read(fd, 1)\n"
            "fds = sorted(int(n) for n in os.listdir('/dev/fd') if n.isdigit())\n"
            "json.dump({'gate': gate.decode(), 'cwd': os.getcwd(), 'env': dict(os.environ), 'fds': fds,\n"
            "           'std': [os.fstat(n).st_rdev for n in (0, 1, 2)]}, open(sys.argv[2], 'w'))\n")
    child = control = None
    try:
        child = procs.spawn_disclaimed([sys.executable, "-c", code, str(gate_r), str(report)],
                                       env={"PATH": "/usr/bin:/bin", "MARK": "1"}, cwd=str(tmp_path),
                                       pass_fds=(gate_r,))
        root = procs.responsibility_root(child.pid)
        assert root is not None and root["pid"] == child.pid and root["unique_id"] > 0
        assert executable(child.pid) == executable(os.getpid())       # the daemon's own binary
        control = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        assert procs.responsibility_root(control.pid) is None
        assert procs.kernel.responsible(control.pid) == procs.kernel.responsible(os.getpid())
        os.write(gate_w, b"1")
        assert child.wait(timeout=30) == 0
        seen = json.loads(report.read_text())
        assert seen["gate"] == "1" and seen["cwd"] == os.path.realpath(tmp_path)
        assert {key: seen["env"][key] for key in ("PATH", "MARK")} == {"PATH": "/usr/bin:/bin", "MARK": "1"}
        assert "SUBFLEET_ATTEMPT" not in seen["env"] and "HOME" not in seen["env"]
        assert seen["std"] == [os.stat("/dev/null").st_rdev] * 3
        # Beyond 0-2 and the gate, the child holds one descriptor: the directory it lists.
        assert gate_r in seen["fds"] and len(set(seen["fds"]) - {0, 1, 2, gate_r}) == 1
    finally:
        stop(child)
        stop(control)
        for fd in (gate_r, gate_w, stray_r, stray_w):
            os.close(fd)


def test_d_cen1_the_handle_answers_as_popen_does(tmp_path):
    """`poll`, `wait` with and without a timeout, `kill`, an exit status, a missing
    executable and a NUL in the environment, as Popen answers them."""
    sleeper = procs.spawn_disclaimed(["/bin/sleep", "30"], env={}, cwd="/")
    try:
        assert sleeper.poll() is None
        with pytest.raises(subprocess.TimeoutExpired):
            sleeper.wait(timeout=.1)
        sleeper.kill()
        assert sleeper.wait(timeout=10) == -signal.SIGKILL and sleeper.poll() == -signal.SIGKILL
    finally:
        stop(sleeper)
    three = procs.spawn_disclaimed([sys.executable, "-c", "raise SystemExit(3)"], env={}, cwd="/")
    assert three.wait() == 3
    with pytest.raises(FileNotFoundError):
        procs.spawn_disclaimed([str(tmp_path / "absent")], env={}, cwd="/")
    with pytest.raises(ValueError):
        procs.spawn_disclaimed(["/bin/sleep", "1"], env={"A": "x\0y"}, cwd="/")


def guardian(tmp_path: Path, *, disclaimed: bool, attempt: str):
    """A real guardian (`python -m subfleet.guardian`) whose provider leaves a renamed
    perl escaper outside its group and exits."""
    escaper_file = tmp_path / "escaper.pid"
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO), "ESCAPER": str(escaper_file),
           "SUBFLEET_ATTEMPT": attempt, "SUBFLEET_JOB": attempt.split("/")[0], "SUBFLEET_ROOT": str(tmp_path)}
    argv = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(tmp_path / "attempt"),
            "--cwd", str(tmp_path), "--stdout-path", str(tmp_path / "stdout"),
            "--stderr-path", str(tmp_path / "stderr"), "--", sys.executable, "-c", PROVIDER]
    if disclaimed:
        child = procs.spawn_disclaimed(argv, env=env, cwd=str(REPO))
    else:
        child = subprocess.Popen(argv, env=env, cwd=REPO, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = int(wait_for(escaper_file))
    return child, procs.identity(pid)


def test_d_cen1_a_rooted_guardian_stays_until_nothing_names_it(tmp_path):
    """Regressions 2 and 3 at the census: the guardian writes its exit receipt and stays,
    because the escaper names it; the census counts the escaper by responsibility (perl
    shows no marker), never a stranger of the same uid that is its own responsible
    process, created later and carrying this root's marker for `<attempt>0`. Once the
    escaper is gone the guardian ends by itself and the census is verified empty."""
    attempt = f"resp-{uuid.uuid4().hex[:8]}/a1"
    child, escaper = guardian(tmp_path, disclaimed=True, attempt=attempt)
    stranger = None
    try:
        assert escaper is not None
        wait_for(tmp_path / "attempt" / "exit.json")
        stranger = procs.spawn_disclaimed(
            [sys.executable, "-c", "import time; time.sleep(60)"], cwd="/",
            env={"PATH": "/usr/bin:/bin", "SUBFLEET_ATTEMPT": attempt + "0", "SUBFLEET_ROOT": str(tmp_path)})
        time.sleep(.5)
        assert child.poll() is None, "a rooted guardian left while its escaper ran"
        start = json.loads((tmp_path / "attempt" / "start.json").read_text())
        root = procs.ResponsibleRoot(child.pid, start["boot_id"], procs.responsibility_root(child.pid)["unique_id"])
        receipt = json.loads((tmp_path / "attempt" / "exit.json").read_text())
        recorded = {child.pid: procs.ProcessIdentity(child.pid, start["boot_id"], start["proc_start"])}
        census = procs.containment(child.pid, child.pid, receipt["child_pid"], attempt, root=str(tmp_path),
                                   recorded_identities=recorded, responsible_root=root)
        assert escaper.pid in census.responsible_pids and escaper.pid not in census.marker_pids
        assert census.group_pids == {child.pid}
        assert stranger.pid not in census.live_pids
        # Its marker is invisible: the marker source alone does not count it.
        assert escaper.pid not in procs.containment(None, None, None, attempt, root=str(tmp_path)).live_pids
        stop_identity(escaper)
        assert child.wait(timeout=30) == 0
        deadline = time.monotonic() + 10
        while not procs.containment(child.pid, child.pid, receipt["child_pid"], attempt, root=str(tmp_path),
                                    recorded_identities=recorded, responsible_root=root).verified_empty:
            assert time.monotonic() < deadline, "the census did not empty once the escaper and guardian left"
            time.sleep(.05)
        assert stranger.poll() is None
    finally:
        stop_identity(escaper)
        stop(stranger)
        stop(child)


def test_d_cen1_a_guardian_that_is_not_a_root_leaves_at_its_receipt(tmp_path):
    """The control: a guardian Popen started names this test's responsible process, so
    nothing names it and it exits after its receipt as before, escaper or not."""
    child, escaper = guardian(tmp_path, disclaimed=False, attempt=f"resp-{uuid.uuid4().hex[:8]}/a1")
    try:
        assert child.wait(timeout=30) == 0
        assert procs.liveness(escaper.pid, escaper.boot_id, escaper.proc_start) == "alive"
    finally:
        stop_identity(escaper)
        stop(child)


def test_d_cen1_a_platform_binary_is_found_through_its_root(tmp_path):
    """`/bin/sleep` started with the attempt's markers, under a root: whatever its
    argument buffer shows (on macOS 26 an Apple platform binary's showed no environment,
    2026-10-13), it names the root and is counted while the root lives."""
    attempt = f"resp-{uuid.uuid4().hex[:8]}/a1"
    pidfile = tmp_path / "sleep.pid"
    code = ("import os, subprocess, sys, time\n"
            f"p = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True, env={{'SUBFLEET_ATTEMPT': {attempt!r}}})\n"
            f"open({str(pidfile)!r} + '.tmp', 'w').write(str(p.pid)); os.rename({str(pidfile)!r} + '.tmp', {str(pidfile)!r})\n"
            "time.sleep(60)\n")
    root_process = procs.spawn_disclaimed([sys.executable, "-c", code], env={"PATH": "/usr/bin:/bin"}, cwd="/")
    sleeper = None
    try:
        sleeper = procs.identity(int(wait_for(pidfile)))
        root = procs.ResponsibleRoot(root_process.pid, procs.boot_id(),
                                     procs.responsibility_root(root_process.pid)["unique_id"])
        census = procs.containment(None, None, None, attempt, responsible_root=root)
        assert sleeper.pid in census.responsible_pids
        assert root_process.pid in census.responsible_pids          # the root, while it lives
    finally:
        stop_identity(sleeper)
        stop(root_process)
