"""Real macOS ownership checks, explicitly skipped when OS inspection is denied."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from subfleet import procs
from subfleet.contracts import KILL_SETTLE_S
from subfleet.guardian import LINGER_SETTLE_ENV
from subfleet.launch_env import LAUNCH_NAMES, build_environment, guardian_pass_args


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


def argv(tmp_path, command, *, env, delay=0):
    return [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(tmp_path),
            "--cwd", str(tmp_path), "--stdout-path", str(tmp_path / "stdout"),
            "--stderr-path", str(tmp_path / "stderr"), "--start-delay-s", str(delay),
            *guardian_pass_args(env), "--", *command]


def environment(marker):
    return build_environment({"PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                              "SUBFLEET_ATTEMPT": marker, "SUBFLEET_JOB": marker.split("/")[0]})


def cleanup(process):
    current = procs.identity(process.pid)
    if current is not None:
        procs.signal_group(process.pid, signal.SIGKILL, boot_id=current.boot_id, proc_start=current.proc_start)
    process.wait(timeout=3)


def test_guardian_detaches_and_survives_submitting_process_exit(tmp_path):
    """C-5.1 and C-5.2 a submitting process can exit before its detached job finishes."""
    env = environment("survive/a1")
    command = argv(tmp_path, [sys.executable, "-c", "import time; time.sleep(.15); print('survived'); raise SystemExit(4)"], env=env)
    launcher = subprocess.run([sys.executable, "-c",
        "import subprocess,sys; p=subprocess.Popen(sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)",
        *command], capture_output=True, text=True, env=env, timeout=3, check=True)
    guardian_pid = int(launcher.stdout.strip())
    start = json.loads(wait_file(tmp_path / "start.json").read_text())
    assert start["guardian_pid"] == start["pgid"] == guardian_pid
    assert start["pgid"] != os.getpgrp()
    receipt = json.loads(wait_file(tmp_path / "exit.json").read_text())
    assert receipt["rc"] == 4
    assert (tmp_path / "stdout").read_text() == "survived\n"


def test_guardian_delayed_receipt_retains_owned_identity(tmp_path):
    """C-4.2 starting waits for a delayed start receipt and then adopts running."""
    env = environment("delayed/a1")
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", "import time; time.sleep(.2)"], env=env, delay=.15),
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    env = environment("ignore/a1")
    process = subprocess.Popen(argv(tmp_path, command, env=env), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        start = json.loads(wait_file(tmp_path / "start.json").read_text())
        wait_file(tmp_path / "ready")
        assert procs.signal_group(process.pid, signal.SIGTERM, boot_id=start["boot_id"], proc_start=start["proc_start"])
        time.sleep(.05)
        assert procs.same_process(process.pid, start["boot_id"], start["proc_start"])
        assert procs.signal_group(process.pid, signal.SIGKILL, boot_id=start["boot_id"], proc_start=start["proc_start"])
        assert process.wait(timeout=3) == -signal.SIGKILL
        # C-5.6 re-reads for up to kill_settle_s: a killed process stays in the table
        # until the kernel finishes tearing it down, longer for a provider at the
        # `utility` QoS (C-5.1) under load (up to 0.78 s at load ~190, 2026-09-27).
        settle_until = time.monotonic() + KILL_SETTLE_S
        while not procs.containment(process.pid, process.pid, None, "ignore/a1").verified_empty:
            assert time.monotonic() < settle_until, "census not verified empty within kill_settle_s"
            time.sleep(.05)
    finally:
        cleanup(process)


def test_nested_setsid_survives_group_and_remains_contained_evidence(tmp_path):
    """C-5.5 and C-5.7 an orphan setsid writer remains evidence requiring quarantine."""
    grandchild = "import os,time; from pathlib import Path; Path('escape.pid').write_text(str(os.getpid())); time.sleep(30)"
    provider = "import subprocess,sys,time; from pathlib import Path; " \
               f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],start_new_session=True); " \
               "time.sleep(.2)"
    env = environment("escape/a1")
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", provider], env=env),
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    escaped = None
    try:
        escaped_pid = int(wait_file(tmp_path / "escape.pid").read_text())
        escaped = procs.identity(escaped_pid)
        assert escaped is not None
        assert process.wait(timeout=3) == 0
        receipt = json.loads((tmp_path / "exit.json").read_text())
        result = procs.containment(process.pid, process.pid, receipt["child_pid"], "escape/a1")
        assert not result.group_pids
        assert escaped_pid in result.marker_pids
        assert not result.verified_empty
        assert escaped_pid in result.to_dict()["live_pids"]
    finally:
        cleanup(process)
        if escaped is not None:
            procs.signal_process(escaped, signal.SIGKILL)


def test_daemon_authorized_markers_reach_provider_and_raw_marker_census(tmp_path):
    """The daemon's argv preserves launch authority through guardian and provider execs."""
    attempt = f"env-{uuid.uuid4().hex[:8]}/a1"
    env = environment(attempt)
    env.update({name: f"dummy-{name}" for name in LAUNCH_NAMES})
    env.update(SUBFLEET_ATTEMPT=attempt, SUBFLEET_JOB=attempt.split("/")[0],
               SUBFLEET_ROOT=str(tmp_path), SUBFLEET_EXIT_SETTLE_S="0.1")
    escaper = ("import os,time; from pathlib import Path; "
               "Path('escape.tmp').write_text(str(os.getpid())); "
               "Path('escape.tmp').rename('escape.pid'); time.sleep(60)")
    provider = ("import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
                f"print(json.dumps({{name: os.environ[name] for name in {sorted(LAUNCH_NAMES)!r} "
                "if name in os.environ}), flush=True)\n"
                f"subprocess.Popen([sys.executable, '-c', {escaper!r}], start_new_session=True)\n"
                "while not Path('escape.pid').exists(): time.sleep(.01)\n")
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", provider], env=env), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    escaped = None
    try:
        escaped_pid = int(wait_file(tmp_path / "escape.pid").read_text())
        escaped = procs.identity(escaped_pid)
        assert escaped is not None
        assert process.wait(timeout=5) == 0
        seen = json.loads((tmp_path / "stdout").read_text())
        # This one launch name is guardian-only, even when explicitly passed.
        assert seen == {name: env[name] for name in LAUNCH_NAMES - {LINGER_SETTLE_ENV}}
        census = procs.containment(None, None, None, attempt, root=str(tmp_path))
        assert not census.group_pids and not census.descendant_pids and not census.responsible_pids
        assert escaped_pid in census.marker_pids
        assert not census.verified_empty
    finally:
        cleanup(process)
        if escaped is not None:
            procs.signal_process(escaped, signal.SIGKILL)
