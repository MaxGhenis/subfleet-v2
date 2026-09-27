"""tools/daemon_qos_compare.py and the QoS options of tools/store_contention_repro.py (2026-09-27, C-5.1)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

REPO = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare = load("daemon_qos_compare")
repro = load("store_contention_repro")


@given(st.integers(0, 40))
def test_abba_order_meets_the_load_evenly(rounds):
    """Each round runs both schedulings, and each pair of rounds puts each first once."""
    sequence = compare.order(rounds)
    assert len(sequence) == 2 * rounds
    assert all(set(sequence[2 * n:2 * n + 2]) == set(compare.QOS) for n in range(rounds))
    counts = Counter(sequence)
    assert counts["utility"] == counts["inherit"] == rounds
    firsts = Counter(sequence[2 * n] for n in range(rounds))
    assert abs(firsts["utility"] - firsts["inherit"]) <= 1


def test_the_summary_survives_failed_runs():
    runs = [{"n": 1, "tool": "store", "qos": "utility", "result": {"error": "exit 1"}},
            {"n": 1, "tool": "census", "qos": "utility", "result": {"error": "exit 2"}},
            {"n": 2, "tool": "store", "qos": "inherit", "result": {"ops": {}, "admission": {}}}]
    text = compare.summary(runs)
    assert "error: exit 1" in text and "error: exit 2" in text
    assert "| 2 | inherit |" in text


def test_qos_argv_clamps_only_on_request():
    assert repro.qos_argv("utility", ["python", "-c", "x"]) == ["/usr/sbin/taskpolicy", "-c", "utility",
                                                                 "python", "-c", "x"]
    assert repro.qos_argv("inherit", ["python"]) == ["python"]


@pytest.mark.skipif(sys.platform != "darwin" or not os.access(repro.TASKPOLICY, os.X_OK),
                    reason="taskpolicy(8) and ps -M are macOS's")
def test_thread_priorities_reads_every_thread_of_a_clamped_process():
    """The rig's own evidence that the clamp applied: `ps -M` per thread, never `ps -E`."""
    code = "import threading,time\nfor _ in range(3): threading.Thread(target=time.sleep, args=(5,)).start()\ntime.sleep(5)"
    child = subprocess.Popen(repro.qos_argv("utility", [sys.executable, "-c", code]))
    try:
        deadline = 50
        while deadline and sum(repro.thread_priorities(child.pid).values()) < 4:
            subprocess.run(["/bin/sleep", "0.1"])
            deadline -= 1
        counts = repro.thread_priorities(child.pid)
        assert sum(counts.values()) >= 4, counts
        assert all(int(priority) <= 20 for priority in counts), counts
    finally:
        child.kill()
        child.wait()


def test_a_store_lock_hold_ends_when_the_lock_is_free():
    """Review of 885142a5, finding 4 (Astra's probe): the harness timed a hold after
    WatchedLock.release returned, which frees the lock first and then logs, so time the
    lock was free was counted as held. With 250 ms of work after the unlock, the recorded
    hold must end before another thread could take the lock, not after that work."""
    import ast
    import threading
    import time
    from types import SimpleNamespace
    from subfleet.lockwatch import WatchedLock
    functions = [node for node in ast.parse(repro.DAEMON).body
                 if isinstance(node, ast.FunctionDef) and node.name in {"acquire", "release"}]
    assert len(functions) == 2
    lock = WatchedLock("hold-timer-test")
    released, taken = threading.Event(), threading.Event()
    seen = {}

    def after_unlock(*_):
        seen["unlocked"] = time.monotonic()
        released.set()
        assert taken.wait(5)
        time.sleep(.25)

    lock.watch = SimpleNamespace(hold_s=0, released=after_unlock)
    env = {"time": time, "lock": lock, "local": threading.local(), "measuring": threading.Event(),
           "real_acquire": lock.acquire, "real_release": lock.release,
           "admission": {key: [] for key in ("waits", "all_holds", "all_hold_cpu", "holds", "hold_cpu",
                                              "reserve_holds", "reserve_hold_cpu")}}
    env["measuring"].set()
    exec(compile(ast.Module(body=functions, type_ignores=[]), "harness-wrappers", "exec"), env)

    def contender():
        assert released.wait(5) and lock._lock.acquire(timeout=5)
        seen["taken"] = time.monotonic()
        lock._lock.release()
        taken.set()

    worker = threading.Thread(target=contender)
    worker.start()
    env["acquire"]()
    started = env["local"].since
    env["release"]()
    worker.join(5)
    held = env["admission"]["all_holds"][0]
    assert held <= seen["taken"] - started + .01, (held, seen["taken"] - started)
    assert held < .25
