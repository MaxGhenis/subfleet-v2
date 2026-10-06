"""Real macOS ownership checks, explicitly skipped off macOS or when OS inspection is denied."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet import procs
from tests.platform_gates import require_process_identity


@pytest.fixture(scope="module", autouse=True)
def macos_inspection():
    require_process_identity()


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


def environment(marker):
    return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "SUBFLEET_ATTEMPT": marker, "SUBFLEET_JOB": marker.split("/")[0]}


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
        *command], capture_output=True, text=True, env=environment("survive/a1"), timeout=3, check=True)
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
                               env=environment("delayed/a1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    process = subprocess.Popen(argv(tmp_path, command), env=environment("ignore/a1"),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        start = json.loads(wait_file(tmp_path / "start.json").read_text())
        wait_file(tmp_path / "ready")
        assert procs.signal_group(process.pid, signal.SIGTERM, boot_id=start["boot_id"], proc_start=start["proc_start"])
        time.sleep(.05)
        assert procs.same_process(process.pid, start["boot_id"], start["proc_start"])
        assert procs.signal_group(process.pid, signal.SIGKILL, boot_id=start["boot_id"], proc_start=start["proc_start"])
        assert process.wait(timeout=3) == -signal.SIGKILL
        assert procs.containment(process.pid, process.pid, None, "ignore/a1").verified_empty
    finally:
        cleanup(process)


def test_nested_setsid_survives_group_and_remains_contained_evidence(tmp_path):
    """C-5.5 and C-5.7 an orphan setsid writer remains evidence requiring quarantine."""
    grandchild = "import os,time; from pathlib import Path; Path('escape.pid').write_text(str(os.getpid())); time.sleep(30)"
    provider = "import subprocess,sys,time; from pathlib import Path; " \
               f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],start_new_session=True); " \
               "time.sleep(.2)"
    process = subprocess.Popen(argv(tmp_path, [sys.executable, "-c", provider]),
                               env=environment("escape/a1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
