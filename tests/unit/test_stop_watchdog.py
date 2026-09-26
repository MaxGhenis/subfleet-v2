"""C-5.8a: a stopping daemon's process is ended at its grace, whatever is stuck.

On 2026-09-25 pid 93697 kept `daemon.lock` with its socket shut, because
`close()` waited without a deadline for pool threads parked on a lock
(docs/reports/2026-09-25-daemon-stop-wedge.md). `watch_stop` bounds that.
Each case runs the real `watch_stop` in a child process, since the bound ends
the process it runs in.

Invariants, for every way a stop can fail to finish (`STUCK`):
- bounded: the process ends no earlier than the grace after the stop was
  armed, and no later than the grace plus scheduling slack;
- named: `daemon.log` gets one stopping line, then every thread's stack,
  including the stuck thread's own frame;
- robust: if faulthandler cannot arm, a plain timer still ends the process
  at the grace, and `close()` still drains and unlocks;
- the timer is armed only by a stop. A process that is never stopped is
  never ended, and a stop that finishes in time exits with its own status.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess
import sys
import threading
import time

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from subfleet import daemon as daemon_module


REPO = Path(__file__).resolve().parents[2]
#: How late past its grace a stuck child may end on a loaded machine. The
#: timer is faulthandler's C thread, so this covers the dump and `_exit`.
SLACK_S = 5.0

CHILD = r'''
import json, os, re, resource, sys, threading, time
from pathlib import Path
from subfleet.daemon import watch_stop

kind, grace, log, delay = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4])
stopping = threading.Event()
arm = watch_stop(stopping, grace, log)
if kind == "arm-fails":
    import faulthandler

    def refuse(*_args, **_kwargs):
        raise RuntimeError("unable to start watchdog thread")

    faulthandler.dump_traceback_later = refuse


def report(**fields):
    print(json.dumps(fields), flush=True)


def park_forever():
    threading.Event().wait()


def stuck_behind_lock(lock):
    # The shape 93697 was sampled in: pool threads parked on one lock whose
    # holder never lets go, while close() joins them.
    with lock:
        pass


def stuck_holding_the_gil():
    # A C loop that never yields the GIL; a watchdog in Python would never run.
    re.match(r"(a+)+$", "a" * 40 + "b")


def stop():
    time.sleep(delay)
    report(stopped_at=time.monotonic())
    if kind != "set-only":
        arm()                          # as the daemon's own stop paths do, first
    stopping.set()                     # anything else only sets the event


if kind == "unstopped":
    time.sleep(3 * grace)
    report(done=True)
    sys.exit(0)

if kind == "descriptors":
    # A stop that follows running out of descriptors (the EMFILE crashes):
    # nothing can be opened any more, so the dump must use what was opened at start.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, 128), hard))
    hoard = []
    try:
        while True:
            hoard.append(os.open(os.devnull, os.O_RDONLY))
    except OSError as exc:
        report(exhausted=exc.errno)

if kind == "clean":
    stop()
    time.sleep(grace / 4)
    sys.exit(0)

held = threading.Lock()
if kind in ("lock", "descriptors", "set-only", "arm-fails"):
    held.acquire()                     # and never released
    worker = threading.Thread(target=stuck_behind_lock, args=(held,), name="subfleet-api_11")
elif kind == "gil":
    worker = None
elif kind == "exit-join":
    worker = threading.Thread(target=park_forever, name="subfleet-io_3")
else:
    raise SystemExit(f"unknown kind {kind}")

if worker is not None:
    worker.start()
stop()
if kind == "gil":
    hog = threading.Thread(target=stuck_holding_the_gil, name="subfleet-control")
    hog.start()
    hog.join()
elif kind == "exit-join":
    sys.exit(0)                        # interpreter shutdown joins the parked thread
else:
    worker.join()                      # as pool.shutdown(wait=True) does
'''

#: Every way the drain can fail to finish, and the frame the dump must name.
STUCK = {
    "lock": "stuck_behind_lock",
    "gil": "stuck_holding_the_gil",
    "exit-join": "park_forever",
    "descriptors": "stuck_behind_lock",
    # Not a stop path the daemon has: `stopping` set by anything else, which
    # only the watching thread notices.
    "set-only": "stuck_behind_lock",
}


def run_child(tmp_path: Path, kind: str, grace: float, delay: float = 0.0,
              timeout: float = 30.0) -> tuple[int, list[dict], float, str]:
    log = tmp_path / f"{kind}.log"
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, kind, str(grace), str(log), str(delay)],
        cwd=REPO, capture_output=True, text=True, timeout=timeout,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"},
    )
    ended = time.monotonic()
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    stopped = next((line["stopped_at"] for line in lines if "stopped_at" in line), started)
    text = log.read_text() if log.exists() else ""
    assert "Traceback" not in proc.stderr, proc.stderr
    return proc.returncode, lines, ended - stopped, text


def stopping_line(grace: float) -> str:
    return f"stopping: if this process is still running in {grace:g} s, "


def assert_bounded(rc: int, elapsed: float, text: str, grace: float, frame: str) -> None:
    assert rc == 1, text
    assert grace - 0.05 <= elapsed <= grace + SLACK_S, (elapsed, text)
    # The stop path and the watching thread both call `arm`; one line, one timer.
    assert text.count("stopping:") == 1, text
    assert stopping_line(grace) + "every thread's stack follows and it exits 1" in text
    assert text.index("stopping:") < text.index("Timeout (")
    assert f" in {frame}\n" in text, text


@pytest.mark.parametrize("kind", sorted(STUCK))
def test_c5_8a_a_stop_that_cannot_finish_ends_the_process_within_its_grace(tmp_path, kind):
    """C-5.8a: for each way the drain can hang, the process still ends on time
    and the dump names the stuck frame."""
    rc, lines, elapsed, text = run_child(tmp_path, kind, grace=1.0)
    assert_bounded(rc, elapsed, text, 1.0, STUCK[kind])
    if kind == "descriptors":
        assert {"exhausted": 24} in lines, lines          # EMFILE before the stop


@settings(max_examples=6, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(kind=st.sampled_from(sorted(STUCK)),
       grace=st.floats(min_value=0.3, max_value=1.5),
       delay=st.floats(min_value=0.0, max_value=0.4))
def test_c5_8a_the_bound_holds_for_any_grace_and_stop_time(tmp_path, kind, grace, delay):
    """C-5.8a property: bounded exit for every grace, stop moment and stuck kind."""
    rc, _lines, elapsed, text = run_child(tmp_path, f"{kind}", grace=grace, delay=delay)
    assert_bounded(rc, elapsed, text, grace, STUCK[kind])
    (tmp_path / f"{kind}.log").unlink()


def test_c5_8a_a_stop_that_finishes_in_time_exits_with_its_own_status(tmp_path):
    """C-5.8a: the bound never cuts a stop short or rewrites its status."""
    rc, _lines, elapsed, text = run_child(tmp_path, "clean", grace=2.0)
    assert rc == 0
    assert elapsed < 2.0
    assert stopping_line(2.0) in text
    assert "Timeout (" not in text


def test_c5_8a_a_stop_still_ends_when_faulthandler_cannot_arm(tmp_path):
    """C-5.8a: faulthandler failing to start its watchdog does not disable the
    bound; a plain timer ends the process at the same moment, without a dump."""
    rc, _lines, elapsed, text = run_child(tmp_path, "arm-fails", grace=1.0)
    assert rc == 1, text
    assert 1.0 - 0.05 <= elapsed <= 1.0 + SLACK_S, (elapsed, text)
    assert text.count("stopping:") == 1, text
    assert stopping_line(1.0) + "it exits 1 with no stack dump (faulthandler: RuntimeError)" in text
    assert "Timeout (" not in text


def test_c5_8a_only_a_stop_arms_the_bound(tmp_path):
    """C-5.8a: a daemon that is never stopped is never ended, and says nothing."""
    rc, lines, _elapsed, text = run_child(tmp_path, "unstopped", grace=0.4)
    assert rc == 0 and {"done": True} in lines
    assert text == ""


def test_c5_8a_main_arms_the_bound_before_anything_else_sees_the_stop(tmp_path, monkeypatch):
    """C-5.8a: `main` watches the daemon it built, with that daemon's grace. The
    signal handler, `close()` (through `on_stop`) and the end of serving each
    arm on their own thread before `stopping` is set, and an exception out of
    `serve_forever` still starts the bound."""
    events = []
    handlers = {}

    class Stub:
        def __init__(self, root):
            self.root = Path(root)
            self.stopping = threading.Event()
            self.stop_grace_s = 7.5
            self.on_stop = None
            self.log = logging.getLogger("test_stop_watchdog.stub")

        def serve_forever(self):
            assert self.on_stop is arm, "close() must be able to arm"
            handlers[daemon_module.signal.SIGTERM]()
            assert events == ["arm:unset"], events
            raise OSError(24, "Too many open files")

    def arm():
        events.append("arm:" + ("set" if built[0].stopping.is_set() else "unset"))

    built = []
    watched = []
    monkeypatch.setattr(daemon_module, "Daemon", lambda root: built.append(Stub(root)) or built[-1])
    monkeypatch.setattr(daemon_module, "watch_stop",
                        lambda stopping, grace, log: watched.append((stopping, grace, log)) or arm)
    monkeypatch.setattr(daemon_module.signal, "signal",
                        lambda sig, handler: handlers.__setitem__(sig, lambda: handler(sig, None)))
    with pytest.raises(OSError):
        daemon_module.main(["--state-root", str(tmp_path)])
    [daemon] = built
    assert watched == [(daemon.stopping, 7.5, daemon.root / "daemon.log")]
    assert set(handlers) == {daemon_module.signal.SIGTERM, daemon_module.signal.SIGINT}
    # The handler armed, then set; the end of serving armed again (a no-op in
    # the real `arm`, which arms once) and left the event set.
    assert events == ["arm:unset", "arm:set"]
    assert daemon.stopping.is_set()


def test_c5_8a_close_arms_on_its_own_thread_before_it_waits_for_anything(tmp_path, monkeypatch):
    """C-5.8a: `close()` calls `on_stop` first, before `stopping` is set and
    before it joins or drains anything, on the thread that is closing."""
    from subfleet.daemon import Daemon
    daemon = Daemon(tmp_path / "root", desktop_prober=lambda: None)
    calls = []
    daemon.on_stop = lambda: calls.append(
        (threading.current_thread(), daemon.stopping.is_set()))
    original_stop = daemon.timers.stop
    monkeypatch.setattr(daemon.timers, "stop", lambda: (calls.append("timers.stop"), original_stop()))
    daemon.close()
    daemon.close()                    # a second close arms nothing
    assert calls == [(threading.current_thread(), False), "timers.stop"]


def test_c5_8a_a_failed_arm_never_stops_close_from_draining_and_unlocking(tmp_path):
    """C-5.8a: whatever `on_stop` does, `close()` still sets `stopping`, drains
    and releases `daemon.lock`."""
    import fcntl
    import os
    from subfleet.daemon import Daemon
    daemon = Daemon(tmp_path / "root", desktop_prober=lambda: None)

    def refuse():
        raise RuntimeError("unable to start watchdog thread")

    daemon.on_stop = refuse
    daemon.close()
    assert daemon.stopping.is_set()
    fd = os.open(tmp_path / "root" / "daemon.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)    # free: close() finished
    finally:
        os.close(fd)


def test_c5_8a_the_grace_outlasts_probe_containment_and_the_backstops_outlast_the_grace():
    """C-5.8a: a stop's probe containment (SIGTERM, TERM_GRACE_S, SIGKILL, settle)
    finishes before the bound; launchd and `daemon stop` wait past the bound
    before their own SIGKILL, so the daemon's dump comes first."""
    import inspect
    from subfleet import cli
    from subfleet.contracts import KILL_SETTLE_S, STOP_BACKSTOP_S, STOP_GRACE_S, TERM_GRACE_S
    assert TERM_GRACE_S + KILL_SETTLE_S + 5 <= STOP_GRACE_S
    assert STOP_BACKSTOP_S >= 5
    assert cli.DAEMON_STOP_WAIT_S == STOP_GRACE_S + STOP_BACKSTOP_S
    default = inspect.signature(daemon_module.Daemon).parameters["stop_grace_s"].default
    assert default == STOP_GRACE_S
