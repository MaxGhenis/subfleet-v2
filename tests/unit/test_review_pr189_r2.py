"""Round-2 evidence: timeout cleanup must cover a child that changes groups."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.frontend import swift


def test_compiler_timeout_does_not_leave_a_child_in_its_own_group(tmp_path, monkeypatch):
    ready = tmp_path / "escaped.json"
    child_code = (
        "import json, os, pathlib, time; os.setpgid(0, 0); "
        f"pathlib.Path({str(ready)!r}).write_text(json.dumps(dict("
        "pid=os.getpid(), ppid=os.getppid(), pgid=os.getpgrp()))); time.sleep(60)"
    )
    wrapper_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(60)"
    )
    real_popen = subprocess.Popen
    owned = []

    def start_owned(command, **kwargs):
        process = real_popen(command, **kwargs)
        owned.append(process)
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "the task-owned child never started"
        return process

    try:
        monkeypatch.setattr(subprocess, "Popen", start_owned)
        with pytest.raises(subprocess.TimeoutExpired):
            swift.run_swift([sys.executable, "-c", wrapper_code], timeout=0.05)
        escaped = json.loads(ready.read_text())
        assert escaped["ppid"] == owned[0].pid
        assert escaped["pid"] == escaped["pgid"] != owned[0].pid
        assert owned[0].returncode == -signal.SIGKILL
        try:
            os.kill(escaped["pid"], 0)
        except ProcessLookupError:
            surviving = False
        else:
            surviving = True
        assert not surviving, f"run_swift left its task-owned child alive: {escaped}"
    finally:
        # Clean only PIDs created by this probe, including the escaped child.
        if ready.exists():
            pid = json.loads(ready.read_text())["pid"]
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in owned:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
