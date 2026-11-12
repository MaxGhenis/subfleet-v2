"""C-5.8a: an armed stop ends at its grace or the kernel fallback margin.

On 2026-09-25 pid 93697 kept `daemon.lock` with its socket shut, because
`close()` waited without a deadline for pool threads parked on a lock
(docs/reports/2026-09-25-daemon-stop-wedge.md). `watch_stop` bounds that.
Each case runs the real `watch_stop` in a child process, since the bound ends
the process it runs in.

Invariants, for every way a stop can fail to finish (`STUCK`):
- bounded: the process ends no earlier than the grace after the stop was
  armed, and no later than the grace plus scheduling slack;
- named: in the ordinary-write cases, `daemon.log` gets one stopping line before
  a managed Python snapshot, or the independent C fallback, names the worker;
- robust: a kernel SIGALRM timer ends the process after a short margin if
  faulthandler cannot arm or finish its dump, without a thread or the GIL;
- the timer is armed only by a stop. A process that is never stopped is
  never ended, and a stop that finishes in time exits with its own status.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from subfleet import daemon as daemon_module


REPO = Path(__file__).resolve().parents[2]
#: How late past its grace a stuck child may end on a loaded machine. This
#: covers the kernel fallback margin, dump, and parent scheduling.
SLACK_S = 5.0
ALARM_MARGIN_S = 3.0


@pytest.fixture(autouse=True)
def fixture_process_identity(monkeypatch):
    """In-process close wiring owns its identities; child watchdogs stay real."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "stop-watchdog-fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "stop-watchdog-fixture-start")


CHILD = r'''
import json, os, re, resource, signal, sys, threading, time
from pathlib import Path
from subfleet.daemon import stop_request, watch_stop

kind, grace, log, delay = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4])


class NestedSignalEvent(threading.Event):
    """Astra's interleaving: a second SIGTERM lands while a handler is inside
    `set`, holding the event's lock, which is not reentrant."""
    sent = False

    def set(self):
        with self._cond:
            if not NestedSignalEvent.sent:
                NestedSignalEvent.sent = True
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)       # the nested handler runs here and blocks on _cond
            self._flag = True
            self._cond.notify_all()


stopping = (NestedSignalEvent() if kind in ("nested-signals", "nested-signals-arm-fails",
                                          "nested-before-kernel")
            else threading.Event())
arm = watch_stop(stopping, grace, log)
signal.signal(signal.SIGTERM, stop_request(stopping, arm))
if kind == "nested-before-kernel":
    real_setitimer = signal.setitimer

    def interrupted_setitimer(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        report(event_set_before_kernel=stopping.is_set())
        return real_setitimer(*args, **kwargs)

    signal.setitimer = interrupted_setitimer
if kind == "kernel-arm-fails":
    def refuse_setitimer(*args, **kwargs):
        report(kernel_arm_attempted=True)
        raise OSError("injected setitimer failure")

    signal.setitimer = refuse_setitimer
if kind in ("arm-fails", "nested-signals-arm-fails", "arm-fails-no-thread", "arm-fails-gil",
            "rearm-arm-fails"):
    import faulthandler

    def refuse(*_args, **_kwargs):
        raise RuntimeError("unable to start watchdog thread")

    faulthandler.dump_traceback_later = refuse
if kind in ("overlap-cancel-fails", "overlap-rearm"):
    # A real signal interrupts the outer arming call. On the original code
    # its inner arm succeeds and the watcher exits before the outer call
    # cancels that timer. An already claimed arm must return without setting
    # the stop event while the first invocation is still establishing it.
    import faulthandler
    real_later = faulthandler.dump_traceback_later
    watcher = next(t for t in threading.enumerate() if t.name == "subfleet-stop-watch")
    later_calls = 0

    def overlapping_later(*args, **kwargs):
        global later_calls
        later_calls += 1
        if later_calls == 1:
            os.kill(os.getpid(), signal.SIGTERM)
            if stopping.is_set():
                watcher.join()
                report(watcher_exited=True)
            if kind == "overlap-cancel-fails":
                # CPython cancels an existing watchdog before starting its
                # replacement, including when that start subsequently fails.
                faulthandler.cancel_dump_traceback_later()
                raise RuntimeError("unable to start watchdog thread")
        return real_later(*args, **kwargs)

    faulthandler.dump_traceback_later = overlapping_later
if kind == "nested-signals":
    # The first signal lands inside `arm`, after its check and before the timer.
    import faulthandler
    real_later = faulthandler.dump_traceback_later
    first = [True]

    def later(*args, **kwargs):
        if first[0]:
            first[0] = False
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)           # the handler runs here, inside the outer `arm`
        return real_later(*args, **kwargs)

    faulthandler.dump_traceback_later = later
if kind == "invalid-frame":
    # Force the C fallback to provide no usable worker frames, as 3.14 can.
    # Its real exit timer still runs, but its output goes to /dev/null.
    import faulthandler
    real_later = faulthandler.dump_traceback_later
    null_fd = os.open(os.devnull, os.O_WRONLY)

    def invalid_later(*args, **kwargs):
        os.write(kwargs["file"], b"Timeout (injected C dump)!\n  <invalid frame>\n")
        kwargs["file"] = null_fd
        return real_later(*args, **kwargs)

    faulthandler.dump_traceback_later = invalid_later
if kind == "slow-arm":
    # The old kernel timer started before C watchdog creation. A start delayed
    # beyond the dump margin made the kernel kill an otherwise working dump.
    import faulthandler
    from subfleet.contracts import STOP_DUMP_MARGIN_S
    real_later = faulthandler.dump_traceback_later

    def slow_later(*args, **kwargs):
        time.sleep(STOP_DUMP_MARGIN_S + 0.25)
        return real_later(*args, **kwargs)

    faulthandler.dump_traceback_later = slow_later
if kind == "hog-during-write":
    # The stopping line's write gives up the GIL and a C loop takes it for
    # good: only a timer started before the write can still fire.
    real_write = os.write

    def write(fd, data):
        if b"stopping:" in data:
            threading.Thread(target=stuck_holding_the_gil, name="subfleet-api_11", daemon=True).start()
            time.sleep(0.05)
        return real_write(fd, data)

    os.write = write
if kind == "blocked-dump":
    # Fill a pipe that nobody reads, then give its blocking writer to the
    # real C watchdog. Its diagnostic write cannot finish, so it cannot
    # reach faulthandler's own _exit(1).
    import faulthandler
    real_later = faulthandler.dump_traceback_later
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    while True:
        try:
            os.write(write_fd, b"x" * 4096)
        except BlockingIOError:
            break
    os.set_blocking(write_fd, True)

    def blocked_later(*args, **kwargs):
        kwargs["file"] = write_fd
        return real_later(*args, **kwargs)

    faulthandler.dump_traceback_later = blocked_later
    # Inject the same blocked sink into the new primary diagnostic path too.
    # The invariant is a blocked dump, regardless of which dumper reaches it.
    import subfleet.daemon as daemon_module
    real_snapshot = daemon_module._dump_stop_stacks
    daemon_module._dump_stop_stacks = lambda _fd, grace: real_snapshot(write_fd, grace)


def report(**fields):
    print(json.dumps(fields), flush=True)


if kind in ("gil", "hog-during-write", "arm-fails-gil"):
    report(gil_enabled=getattr(sys, "_is_gil_enabled", lambda: True)())


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
    if kind == "nested-signals-arm-fails":
        os.kill(os.getpid(), signal.SIGTERM)
        return
    if kind == "nested-signals":
        arm()                          # interrupted by SIGTERM during the first arm
        stopping.set()                 # another signal while this event's lock is held
        return
    if kind != "set-only":
        arm()                          # as the daemon's own stop paths do, first
    stopping.set()                     # anything else only sets the event


if kind == "unstopped":
    time.sleep(3 * grace)
    report(done=True)
    sys.exit(0)

if kind == "descriptors-cold-codecs":
    # A fresh daemon need not have loaded a diagnostic-only codec. Force that
    # cold state even if an unrelated import happened to populate the cache.
    import codecs, encodings
    codecs.unregister(encodings.search_function)
    codecs.register(encodings.search_function)
    encodings._cache.clear()
    sys.modules.pop("encodings.unicode_escape", None)
if kind in ("descriptors", "descriptors-cold-codecs"):
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
    time.sleep(0.3)
    sys.exit(0)

held = threading.Lock()
if kind in ("lock", "descriptors", "set-only", "arm-fails", "rearm", "nested-signals",
            "nested-signals-arm-fails", "arm-fails-no-thread", "overlap-cancel-fails",
            "overlap-rearm", "blocked-dump", "nested-before-kernel", "kernel-arm-fails",
            "rearm-arm-fails", "invalid-frame", "slow-arm", "descriptors-cold-codecs"):
    held.acquire()                     # and never released
    worker = threading.Thread(target=stuck_behind_lock, args=(held,), name="subfleet-api_11")
elif kind in ("gil", "hog-during-write", "arm-fails-gil"):
    worker = None
elif kind == "exit-join":
    worker = threading.Thread(target=park_forever, name="subfleet-io_3")
else:
    raise SystemExit(f"unknown kind {kind}")

if worker is not None:
    worker.start()
if kind == "arm-fails-no-thread":
    # All startup threads already exist. No Python thread can be created
    # after this point, including a fallback threading.Timer at stop time.
    def refuse_thread(*args, **kwargs):
        report(thread_start_attempted=True)
        raise RuntimeError("no threads remain at stop time")
    threading.Thread.start = refuse_thread
stop()
if kind.startswith("overlap-"):
    report(later_calls=later_calls)
if kind in ("gil", "arm-fails-gil"):
    hog = threading.Thread(target=stuck_holding_the_gil, name="subfleet-control")
    hog.start()
    hog.join()
elif kind == "hog-during-write":
    threading.Event().wait()
elif kind in ("rearm", "rearm-arm-fails"):
    while True:                        # every later arm must leave the deadline alone
        arm()
        time.sleep(0.05)
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
    # `arm` called again and again after the stop: the deadline must not move.
    "rearm": "stuck_behind_lock",
    # The GIL is taken for good while the stopping line is written.
    "hog-during-write": "stuck_holding_the_gil",
    # A SIGTERM inside `arm`, then another inside `Event.set`: the main thread
    # blocks for good on the event's lock, so only an armed timer ends it.
    "nested-signals": "set",
}
#: The GIL is lost where the stopping line is written, so no line is: the
#: timer, started first, must still fire.
NO_LINE = {"hog-during-write"}


def run_child(tmp_path: Path, kind: str, grace: float, delay: float = 0.0,
              timeout: float = 30.0) -> tuple[int, list[dict], float, str]:
    log = tmp_path / f"{kind}.log"
    # Hypothesis replays failed examples in this same fixture. Each child's
    # single-line and dump assertions must inspect only that child's output.
    log.unlink(missing_ok=True)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, kind, str(grace), str(log), str(delay)],
        cwd=REPO, capture_output=True, text=True, timeout=timeout,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin", "PYTHON_GIL": "1"},
    )
    ended = time.monotonic()
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    stopped = next((line["stopped_at"] for line in lines if "stopped_at" in line), started)
    text = log.read_text() if log.exists() else ""
    assert "Traceback" not in proc.stderr, proc.stderr
    return proc.returncode, lines, ended - stopped, text


def stopping_line(grace: float) -> str:
    return f"stopping: if this process is still running in {grace:g} s, "


def assert_bounded(rc: int, elapsed: float, text: str, grace: float, kind: str) -> None:
    frame = STUCK[kind]
    assert rc == 1, text
    assert grace - 0.05 <= elapsed <= grace + SLACK_S, (elapsed, text)
    assert "Timeout (" in text, text
    if kind not in NO_LINE:
        # The stop path and the watching thread both call `arm`; one line, one timer.
        assert text.count("stopping:") == 1, text
        assert stopping_line(grace) + "the stacks of its threads follow and it exits 1" in text
        assert text.index("stopping:") < text.index("Timeout (")
    assert f" in {frame}\n" in text, text


@pytest.mark.parametrize("kind", sorted(STUCK))
def test_c5_8a_a_stop_that_cannot_finish_ends_the_process_within_its_grace(tmp_path, kind):
    """C-5.8a: for each way the drain can hang, the process still ends on time
    and the dump names the stuck frame."""
    rc, lines, elapsed, text = run_child(tmp_path, kind, grace=1.0)
    assert_bounded(rc, elapsed, text, 1.0, kind)
    if kind == "descriptors":
        assert {"exhausted": 24} in lines, lines          # EMFILE before the stop
    if kind in ("gil", "hog-during-write"):
        assert {"gil_enabled": True} in lines, lines


@settings(max_examples=6, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(kind=st.sampled_from(sorted(STUCK)),
       grace=st.floats(min_value=0.3, max_value=1.5),
       delay=st.floats(min_value=0.0, max_value=0.4))
def test_c5_8a_the_bound_holds_for_any_grace_and_stop_time(tmp_path, kind, grace, delay):
    """C-5.8a property: bounded exit for every grace, stop moment and stuck kind."""
    rc, _lines, elapsed, text = run_child(tmp_path, f"{kind}", grace=grace, delay=delay)
    assert_bounded(rc, elapsed, text, grace, kind)
    (tmp_path / f"{kind}.log").unlink()


def test_c5_8a_a_stop_that_finishes_in_time_exits_with_its_own_status(tmp_path):
    """C-5.8a: the bound never cuts a stop short or rewrites its status."""
    rc, _lines, elapsed, text = run_child(tmp_path, "clean", grace=10.0)
    assert rc == 0
    assert elapsed < 10.0
    assert stopping_line(10.0) in text
    assert "Timeout (" not in text


@pytest.mark.parametrize("kind", ["arm-fails", "nested-signals-arm-fails",
                                 "overlap-cancel-fails", "arm-fails-gil", "rearm-arm-fails"])
def test_c5_8a_a_stop_still_ends_when_faulthandler_cannot_arm(tmp_path, kind):
    """The kernel bound survives an unset/deadlocked stop event, reentrant
    cancellation, a permanent GIL hog, and later arms that keep coming. The
    Python watcher exits where it can progress; otherwise only an ITIMER_REAL
    that a later `arm` leaves alone ends the process (round-4 review, low 1)."""
    rc, lines, elapsed, text = run_child(tmp_path, kind, grace=1.0)
    # C-5.8a promises the bound, not that a missing C timer prevents the
    # preexisting Python watcher from exiting first. A deadlocked stop event
    # or permanent GIL hog still requires the independent kernel timer.
    kernel_only = kind in {"nested-signals-arm-fails", "arm-fails-gil"}
    assert rc == (-signal.SIGALRM if kernel_only else 1), text
    earliest = 1.0 + ALARM_MARGIN_S if kernel_only else 1.0
    assert earliest - 0.05 <= elapsed <= 1.0 + SLACK_S, (elapsed, text)
    assert text.count("stopping:") == 1, text
    assert "C stack fallback unavailable (faulthandler: RuntimeError)" in text
    assert ("Python thread stacks" in text) is not kernel_only
    if kind == "arm-fails-gil":
        assert {"gil_enabled": True} in lines, lines
    elif kind == "overlap-cancel-fails":
        assert {"later_calls": 1} in lines, lines


def test_c5_8a_fallback_needs_no_new_thread_at_stop_time(tmp_path):
    """N3b: thread exhaustion after startup cannot disable fallback exit."""
    rc, lines, elapsed, text = run_child(tmp_path, "arm-fails-no-thread", grace=1.0)
    # Either the original preexisting watcher or the kernel alarm can enforce
    # this property; creating a new threading.Timer cannot.
    assert rc in (1, -signal.SIGALRM), text
    assert 1.0 - 0.05 <= elapsed <= 1.0 + SLACK_S, (elapsed, text)
    assert {"thread_start_attempted": True} not in lines, lines
    assert text.count("stopping:") == 1, text
    assert "faulthandler: RuntimeError" in text, text
    if rc == 1:
        assert "Python thread stacks" in text


def test_c5_8a_overlapping_arms_cannot_replace_the_first_watchdog(tmp_path):
    """A signal inside an in-flight arming call cannot rearm faulthandler,
    reset its deadline or produce a second stopping line (Astra 2/N5)."""
    rc, lines, elapsed, text = run_child(tmp_path, "overlap-rearm", grace=1.0)
    assert {"later_calls": 1} in lines, lines
    assert_bounded(rc, elapsed, text, 1.0, "lock")


def test_c5_8a_a_signal_inside_arming_cannot_publish_an_unbounded_stop(tmp_path):
    """Reentry after the once claim but before setitimer must defer Event.set."""
    rc, lines, elapsed, text = run_child(tmp_path, "nested-before-kernel", grace=1.0)
    assert {"event_set_before_kernel": False} in lines, lines
    assert_bounded(rc, elapsed, text, 1.0, "lock")


def test_c5_8a_kernel_timer_failure_ends_the_process_immediately(tmp_path):
    """An unusable kernel bound must not leave a claimed, unbounded stop."""
    rc, lines, elapsed, text = run_child(tmp_path, "kernel-arm-fails", grace=10.0)
    assert rc == 1, text
    assert {"kernel_arm_attempted": True} in lines, lines
    assert elapsed < 10.0, elapsed
    assert text == ""


def test_c5_8a_a_blocked_faulthandler_dump_cannot_keep_the_process_alive(tmp_path):
    """The default-action alarm also ends a C watchdog blocked on its dump."""
    rc, _lines, elapsed, text = run_child(tmp_path, "blocked-dump", grace=1.0)
    assert rc == -signal.SIGALRM, text
    assert 1.0 + ALARM_MARGIN_S - 0.05 <= elapsed <= 1.0 + SLACK_S, (elapsed, text)
    assert text.count("stopping:") == 1, text


def test_c5_8a_repeated_stop_skips_setting_an_already_set_event():
    """N3a: calling Event.set again could reenter its held condition lock."""
    class SetOnceEvent(threading.Event):
        calls = 0

        def set(self):
            self.calls += 1
            assert self.calls == 1, "an already set stop event must not be set again"
            super().set()

    event = SetOnceEvent()
    arms = []
    stop = daemon_module.stop_request(event, lambda: arms.append(event.is_set()))
    stop(signal.SIGTERM, None)
    stop(signal.SIGINT, None)
    assert event.is_set() and event.calls == 1
    assert arms == [False, True]


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


def test_c5_8a_catalog_sigterm_goes_out_before_the_first_other_wait(tmp_path, monkeypatch):
    """The catalog's grace overlaps all of close's other waits, starting at the top."""
    from types import SimpleNamespace
    from subfleet.conversations import service as service_module

    daemon = daemon_module.Daemon(tmp_path / "root", desktop_prober=lambda: None)
    service = daemon.conversations
    process = SimpleNamespace(pid=4_000_001, returncode=None)
    process.poll = lambda: process.returncode
    signals = []

    def killpg(pid, sig):
        assert pid == process.pid
        signals.append(sig)
        process.returncode = -sig

    service._catalog_proc = process
    monkeypatch.setattr(service_module.os, "killpg", killpg)
    original_wait = daemon._waits_answered.wait_for

    def wait(*args, **kwargs):
        assert signals == [signal.SIGTERM]
        assert service._catalog_fence is None and service._closed
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(daemon._waits_answered, "wait_for", wait)
    closed = False
    try:
        daemon.close()
        closed = True
        assert service._catalog_proc is None
    finally:
        # Release the real daemon even when the negative control stops close
        # at its first wait; the fake child needs no further signals or waits.
        if not closed:
            process.returncode = 0
            monkeypatch.setattr(daemon._waits_answered, "wait_for", original_wait)
            daemon._closed = False
            daemon.close()


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


@pytest.mark.parametrize("start_in_flight", [False, True])
def test_c5_8a_the_grace_outlasts_probe_containment_and_the_backstops_outlast_the_grace(
        tmp_path, monkeypatch, start_in_flight):
    """C-5.8a: real close spends every named remaining bound under a fake clock.

    An in-flight start hands off at its deadline during the answer wait. Every
    modelled wait spends its full remaining timeout; an unmodelled bounded wait
    fails rather than silently disappearing from the 28 s total. Probe
    containment and the watchdog still precede the unchanged external backstops.
    """
    import inspect
    from subfleet import cli
    from subfleet.contracts import STOP_BACKSTOP_S, STOP_DUMP_MARGIN_S, STOP_GRACE_S, TERM_GRACE_S
    from subfleet.conversations.service import CATALOG_KILL_AFTER_S, CATALOG_TERM_GRACE_S
    assert TERM_GRACE_S + 10 <= STOP_GRACE_S
    assert CATALOG_KILL_AFTER_S == 60 and CATALOG_TERM_GRACE_S == 10
    from types import SimpleNamespace
    from subfleet.conversations import service as service_module
    from tests.unit.test_conversation_service import FakeRun

    daemon = daemon_module.Daemon(tmp_path / "root", desktop_prober=lambda: None)
    daemon.lock_watch.stop()
    daemon.lock_watch._stop.clear()
    now = [1000.0]
    waits = []
    service = daemon.conversations
    process = FakeRun(4_000_000)
    process.stubborn = True
    if start_in_flight:
        service._catalog_starting = True
        service._catalog_handoff.clear()
    else:
        service._catalog_proc = process

    def spend(name, timeout):
        waits.append((name, timeout))
        deadline = now[0] + timeout
        if name == "answers" and start_in_flight:
            now[0] += service_module.CATALOG_HANDOFF_WAIT_S
            service._catalog_starting = False
            service._catalog_proc = process
            service.begin_close()
        now[0] = deadline

    def join(name):
        return SimpleNamespace(join=lambda timeout: spend(name, max(0, timeout)))

    def wait(timeout):
        spend("catalog", timeout)
        raise subprocess.TimeoutExpired("catalog", timeout)

    def send(pid, sig):
        assert pid == process.pid
        process.signals.append(sig)

    def handoff_wait(timeout):
        spend("handoff", timeout)
        return True

    original_timers_stop = daemon.timers.stop

    def timers_stop():
        spend("probe-containment", TERM_GRACE_S)
        original_timers_stop()

    runner = join("runner")
    runner.stop = lambda: None
    runner.attempt_id = "fake/a1"
    service.runners[runner.attempt_id] = runner
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: now[0])
    service.clock = lambda: now[0]
    daemon.lock_watch.clock = lambda: now[0]
    daemon.lock_watch._thread = join("lockwatch")
    daemon._control_thread = join("control")
    daemon._readers = [join("reader"), join("reader")]
    monkeypatch.setattr(daemon._waits_answered, "wait_for", lambda predicate, timeout: spend("answers", timeout))
    monkeypatch.setattr(daemon.store._returned, "wait_for", lambda predicate, timeout: spend("store-readers", timeout))
    monkeypatch.setattr(daemon.timers, "stop", timers_stop)
    monkeypatch.setattr(service._catalog_handoff, "wait", handoff_wait)
    monkeypatch.setattr(process, "wait", wait)
    monkeypatch.setattr(service_module.os, "killpg", send)
    original_join = threading.Thread.join
    original_event_wait = threading.Event.wait
    original_condition_wait = threading.Condition.wait_for
    original_condition_sleep = threading.Condition.wait
    original_process_wait = subprocess.Popen.wait

    def unmodelled_wait(original):
        signature = inspect.signature(original)

        def checked(receiver, *args, **kwargs):
            timeout = signature.bind(receiver, *args, **kwargs).arguments.get("timeout")
            assert timeout is None, f"unmodelled bounded wait: {original.__qualname__}"
            return original(receiver, *args, **kwargs)
        return checked

    try:
        with monkeypatch.context() as bounded:
            bounded.setattr(threading.Thread, "join", unmodelled_wait(original_join))
            bounded.setattr(threading.Event, "wait", unmodelled_wait(original_event_wait))
            bounded.setattr(threading.Condition, "wait_for", unmodelled_wait(original_condition_wait))
            bounded.setattr(threading.Condition, "wait", unmodelled_wait(original_condition_sleep))
            bounded.setattr(subprocess.Popen, "wait", unmodelled_wait(original_process_wait))
            daemon.close()
    finally:
        process.end()
    named_stop_bound = now[0] - 1000.0
    assert named_stop_bound == 28
    assert waits == [("answers", 2), ("control", 2), ("probe-containment", 15),
                     ("reader", 2), ("reader", 0), ("handoff", 0), ("catalog", 0), ("catalog", 2),
                     ("runner", 5), ("lockwatch", 0), ("store-readers", 0)]
    assert named_stop_bound <= STOP_GRACE_S - 2
    assert STOP_BACKSTOP_S >= 5
    assert 0 < STOP_DUMP_MARGIN_S < STOP_BACKSTOP_S
    assert cli.DAEMON_STOP_WAIT_S == STOP_GRACE_S + STOP_BACKSTOP_S
    default = inspect.signature(daemon_module.Daemon).parameters["stop_grace_s"].default
    assert default == STOP_GRACE_S


def test_begin_close_during_start_is_nonblocking(tmp_path, monkeypatch):
    from subfleet.conversations import catalog as catalog_module
    from subfleet.conversations import service as service_module
    from tests.unit.test_conversation_service import FakeRun

    daemon = daemon_module.Daemon(tmp_path / "root", desktop_prober=lambda: None)
    service = daemon.conversations
    entered, release = threading.Event(), threading.Event()
    process = FakeRun(4_000_000)
    signals = []
    fence = []
    errors = []

    def spawn(root, *, fence_fd):
        fence.extend(service._catalog_fence)
        entered.set()
        assert release.wait(5)
        import os
        os.fstat(fence[0])
        with pytest.raises(OSError):
            os.fstat(fence[1])
        return process

    def send(pid, sig):
        signals.append(sig)
        process.end(-sig)

    def guarded(action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(catalog_module, "spawn_refresh", spawn)
    monkeypatch.setattr(service_module.os, "killpg", send)
    starter = threading.Thread(target=lambda: guarded(service._start_catalog))
    closer = threading.Thread(target=lambda: guarded(daemon.close))
    try:
        starter.start()
        assert entered.wait(5)
        deadline = time.monotonic() + 0.25
        closer.start()
        assert daemon.stopping.wait(max(0, deadline - time.monotonic()))
        assert daemon.timers.cancel.wait(max(0, deadline - time.monotonic()))
        assert service._closed and service._catalog_fence is not None
        release.set()
        starter.join(5)
        closer.join(5)
        assert not starter.is_alive() and not closer.is_alive() and not errors
        assert signals == [signal.SIGTERM]
        assert service._catalog_fence is None and service._catalog_proc is None
    finally:
        release.set()
        starter.join(5)
        if closer.ident is not None:
            closer.join(5)
        process.end()
        if errors:
            daemon._closed = False
            daemon.close()


@pytest.mark.parametrize("kind", ["invalid-frame", "slow-arm"])
def test_c5_8a_python_snapshot_names_the_held_frame_before_the_backstop(tmp_path, kind):
    """A missing C frame or slow watchdog creation cannot lose a parked worker."""
    rc, _lines, elapsed, text = run_child(tmp_path, kind, grace=1.0)
    assert rc == 1, (rc, text)
    assert 1.0 <= elapsed <= 1.0 + SLACK_S, (elapsed, text)
    snapshot = text[text.index("Python thread stacks"):]
    assert "[subfleet-api_11]" in snapshot
    assert " in stuck_behind_lock\n" in snapshot


def test_c5_8a_the_managed_snapshot_needs_no_new_descriptor_or_codec(tmp_path):
    """The descriptor reserved at startup suffices for managed frame names too."""
    rc, lines, elapsed, text = run_child(tmp_path, "descriptors-cold-codecs", grace=1.0)
    assert rc == 1, (rc, text)
    assert {"exhausted": 24} in lines
    assert 1.0 - 0.05 <= elapsed <= 1.0 + SLACK_S, (elapsed, text)
    assert "Python thread stacks" in text, text
    assert " in stuck_behind_lock\n" in text, text
