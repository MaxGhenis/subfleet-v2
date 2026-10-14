"""The supervisor a conversation file op's subprocesses run under (C-25.3,
`subfleet/supervisor.py`): what a call returns, checked against `subprocess.run`;
what it ends, and when; and the gate `close()` closes without waiting on a spawn.

Invariants, each a test below:

- A call returns what `subprocess.run(capture_output=True)` returns for a command
  that leaves nothing running (differential, for random input, output and exit).
- Nothing a command leaves in its process group runs once its call has returned
  or raised: at its exit, at its cap, at a cut, and at `close()`.
- A call is bounded: it returns within its cap plus `CLEANUP_S` for the group's
  end, plus `CLEANUP_S` for output a process outside the group holds.
- `close()` returns within its timeout, never waits on a spawn, and no command
  starts once it has begun: a spawn it overtook runs nothing.
- Each leader is reaped exactly once, by its call or by a reaper thread, and only
  after it has left the registry `close()` signals from.

Each command that starts a child records the child's pid in a file; a test kills
only those pids, at its end, whatever happened.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import hypothesis
import pytest
from hypothesis import strategies as st

from subfleet import supervisor
from subfleet.supervisor import Closed, Supervisor

SETTINGS = dict(deadline=None, derandomize=True,
                suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture])


class Refused(Exception):
    """The refusal the tests' supervisors raise once closed."""


def running(pid: int) -> bool:
    """Whether `pid` runs: a zombie (killed, not yet reaped) does not."""
    state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def pids_in(path: Path) -> list[int]:
    return [int(word) for word in path.read_text().split()] if path.exists() else []


@contextlib.contextmanager
def ending(path: Path):
    """End, at the block's end, the processes recorded in `path`."""
    try:
        yield
    finally:
        for pid in pids_in(path):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def until(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(.01)


def in_thread(fn) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def body():
        try:
            outcome["result"] = fn()
        except BaseException as exc:     # noqa: BLE001 - the test reads it
            outcome["error"] = exc

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, outcome


TEXT = st.text(st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=200)


# --- what a call returns ------------------------------------------------------------


@hypothesis.settings(max_examples=25, **SETTINGS)
@hypothesis.given(data=st.binary(max_size=200_000), err=TEXT, code=st.integers(0, 255))
def test_a_call_returns_what_subprocess_run_returns(data, err, code):
    """Differential: stdin through `cat` (up to three pipe buffers each way, so input
    and output move together), stderr and the exit status, as `subprocess.run` gives
    them."""
    argv = ["/bin/sh", "-c", 'cat; printf "%s" "$1" >&2; exit "$2"', "sh", err, str(code)]
    plain = subprocess.run(argv, input=data, capture_output=True, timeout=60)
    supervised = Supervisor().run(argv, input=data, timeout=60)
    assert (supervised.returncode, supervised.stdout, supervised.stderr) == (plain.returncode, plain.stdout,
                                                                           plain.stderr)
    assert supervised.args == argv


@hypothesis.settings(max_examples=15, **SETTINGS)
@hypothesis.given(out=st.lists(st.sampled_from(["a", "\r", "\n", "\r\n", "é"]), max_size=40).map("".join),
                  errors=st.sampled_from([None, "replace", "backslashreplace"]))
def test_text_is_decoded_as_subprocess_run_decodes_it(out, errors):
    """Differential: `text=True` decodes with the locale's encoding and universal
    newlines, and `errors` as given, as `subprocess.run` does."""
    argv = ["/bin/sh", "-c", 'printf "%s" "$1"; printf "%s" "$1" >&2', "sh", out]
    plain = subprocess.run(argv, capture_output=True, text=True, errors=errors, timeout=60)
    supervised = Supervisor().run(argv, text=True, errors=errors, timeout=60)
    assert (supervised.stdout, supervised.stderr) == (plain.stdout, plain.stderr)


def test_a_command_not_on_the_path_raises_as_subprocess_run_does():
    with pytest.raises(FileNotFoundError) as plain:
        subprocess.run(["subfleet-no-such-command"], capture_output=True)
    with pytest.raises(FileNotFoundError) as supervised:
        Supervisor().run(["subfleet-no-such-command"], timeout=5)
    assert (supervised.value.errno, supervised.value.filename) == (plain.value.errno, plain.value.filename)


def test_run_uses_the_supervisor_its_context_names(monkeypatch):
    """`supervisor.run` is `subprocess.run` unless the context names a supervisor
    (`supervising`, which a file op sets). A thread the op starts names none, unless
    the build makes threads inherit their starter's context (free-threaded 3.14:
    `sys.flags.thread_inherit_context`), when it names the op's."""
    plain_calls, real = [], subprocess.run

    def recording(*args, **kwargs):
        plain_calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "run", recording)
    assert supervisor.run(["true"], timeout=10).returncode == 0 and plain_calls == [["true"]]
    owner = Supervisor()
    with supervisor.supervising(owner):
        assert supervisor.current() is owner
        thread, seen = in_thread(supervisor.current)
        thread.join(10)
        assert seen["result"] is (owner if getattr(sys.flags, "thread_inherit_context", 0) else None)
        assert supervisor.run(["true"], timeout=10).returncode == 0
    assert supervisor.current() is None and plain_calls == [["true"]]


# --- what it ends, and when -----------------------------------------------------------


@hypothesis.settings(max_examples=8, **SETTINGS)
@hypothesis.given(children=st.lists(st.sampled_from(["holds the pipes", "detached"]), max_size=3),
                  code=st.integers(0, 3))
def test_what_a_command_leaves_in_its_group_ends_when_it_exits(tmp_path_factory, children, code):
    """A child holding the command's pipes held `subprocess.run` until its cap; a
    detached one outlived it. Both end when the command exits, and the call returns
    at once with the command's own status."""
    pids = tmp_path_factory.mktemp("pids") / "pids"
    starts = "".join(("sleep 30 &\n" if kind == "holds the pipes" else "sleep 30 </dev/null >/dev/null 2>&1 &\n")
                     + f'echo $! >> "{pids}"\n' for kind in children)
    with ending(pids):
        began = time.monotonic()
        result = Supervisor().run(["/bin/sh", "-c", starts + f"exit {code}"], timeout=60)
        took = time.monotonic() - began
        left = [pid for pid in pids_in(pids) if running(pid)]
    assert result.returncode == code and len(pids_in(pids)) == len(children)
    assert left == [] and took < 10, (left, took)


def test_at_its_cap_the_whole_group_ends_within_the_cleanup_bound(tmp_path):
    pids = tmp_path / "pids"
    with ending(pids):
        began = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as late:
            Supervisor().run(["/bin/sh", "-c", f'sleep 30 & echo $$ $! >> "{pids}"; printf partial; sleep 30'],
                             timeout=1.0)
        took = time.monotonic() - began
        left = [pid for pid in pids_in(pids) if running(pid)]
    assert late.value.timeout == 1.0 and late.value.output == b"partial"
    assert len(pids_in(pids)) == 2 and left == [], left
    assert took < 1.0 + supervisor.CLEANUP_S + 3, took


def test_a_cut_stops_the_command_and_returns_one_byte_past_the_limit():
    began = time.monotonic()
    result = Supervisor().run(["yes"], timeout=60, stdout_limit=1000)
    assert len(result.stdout) == 1001 and result.returncode == -signal.SIGKILL
    assert time.monotonic() - began < 10


def test_a_process_that_leaves_its_group_cannot_hold_the_call(tmp_path):
    """Out of reach, by design: a process that leaves the group (`setsid`, as a daemon
    does) survives the call. Its hold on the call's pipes is bounded: the call
    returns `CLEANUP_S` after the command exits, with the output it had."""
    pids = tmp_path / "pids"
    escape = ("import os, sys, time\n"
              "if os.fork() == 0:\n"
              "    os.setsid()\n"
              f"    open({str(pids)!r}, 'a').write(f'{{os.getpid()}} ')\n"
              "    time.sleep(30)\n"
              "    os._exit(0)\n"
              f"while not os.path.exists({str(pids)!r}): time.sleep(0.01)   # it has left the group\n"
              "print('before', flush=True)\n")
    with ending(pids):
        began = time.monotonic()
        result = Supervisor().run([sys.executable, "-c", escape], timeout=60)
        took = time.monotonic() - began
        until(lambda: pids_in(pids), "the escaped child to record itself")
        escaped = [pid for pid in pids_in(pids) if running(pid)]
    assert result.returncode == 0 and result.stdout == b"before\n"
    assert escaped and took < supervisor.CLEANUP_S + 15, (escaped, took)


def test_a_leader_sigkill_has_not_ended_yet_is_reaped_later_and_reported_by_close(monkeypatch):
    """With no time for cleanup, the call cannot see its leader end after SIGKILL: it
    raises at once (the command did not finish), the leader stays registered until
    a reaper thread sees it end, and is reaped exactly once."""
    monkeypatch.setattr(supervisor, "CLEANUP_S", 0.0)
    owner = Supervisor()
    spawned, real = [], subprocess.Popen

    class Recording(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    monkeypatch.setattr(supervisor.subprocess, "Popen", Recording)
    with pytest.raises(subprocess.TimeoutExpired):
        owner.run(["sleep", "30"], timeout=0.3)
    until(lambda: not owner.running(), "the reaper to release the leader")
    until(lambda: spawned[0].returncode is not None, "the reaper to reap the leader")
    assert spawned[0].returncode == -signal.SIGKILL
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0].pid, os.WNOHANG)          # reaped once, by the reaper
    assert owner.close(2) == set()


# --- close() and the gate -------------------------------------------------------------


def test_close_ends_a_running_group_and_its_call_raises_the_refusal(tmp_path):
    """C-25.3: a file op's git still running at close() (with a hook it waits for,
    and a child of that hook) ends with its whole group before close() returns, and
    the op's call fails with the service's refusal (`store-closed`)."""
    pids = tmp_path / "pids"
    owner = Supervisor(refuse=lambda: Refused("closed"))
    with ending(pids):
        thread, outcome = in_thread(lambda: owner.run(
            ["/bin/sh", "-c", f'sleep 30 & echo $$ $! >> "{pids}"; wait'], timeout=60))
        until(lambda: len(pids_in(pids)) == 2, "the command and its child to start")
        began = time.monotonic()
        held = owner.close(5)
        took = time.monotonic() - began
        left = [pid for pid in pids_in(pids) if running(pid)]
        thread.join(30)
    assert held == set() and took < 5 and left == [], (held, took, left)
    assert isinstance(outcome.get("error"), Refused), outcome
    with pytest.raises(Refused):
        owner.run(["true"], timeout=5)


def test_close_never_waits_on_a_spawn_and_the_gate_it_closed_runs_nothing(tmp_path, monkeypatch):
    """C-25.3 (Astra's review of cc65f36, finding 2): a spawn held under the
    supervisor's lock held close() with it (Python's spawn waits, with no bound, for
    the child's exec), and a spawn moved out of the lock could run its command after
    close() had returned. The spawn runs outside the lock, behind a gate opened only
    while the supervisor is open: close() returns while the spawn is held, and the
    command, once spawned, never runs."""
    marker = tmp_path / "ran"
    owner = Supervisor(refuse=lambda: Refused("closed"))
    entered, release, spawned = threading.Event(), threading.Event(), []
    real = subprocess.Popen

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        spawned.append(real(*args, **kwargs))
        return spawned[-1]

    monkeypatch.setattr(supervisor.subprocess, "Popen", held)
    thread, outcome = in_thread(lambda: owner.run(["touch", str(marker)], timeout=30))
    try:
        assert entered.wait(30)
        began = time.monotonic()
        held_groups = owner.close(5)
        took = time.monotonic() - began
    finally:
        release.set()
    thread.join(30)
    assert took < 1.0 and held_groups == set(), took
    assert isinstance(outcome.get("error"), Refused), outcome
    assert not marker.exists(), "the command ran after close() had returned"
    assert spawned[0].returncode in (125, -signal.SIGKILL)   # the gate refused, and the shell is reaped


def test_nothing_is_spawned_once_closed(monkeypatch):
    owner = Supervisor()
    owner.close(1)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned after close()"))
    with pytest.raises(Closed):
        owner.run(["true"], timeout=5)


@hypothesis.settings(max_examples=6, **SETTINGS)
@hypothesis.given(starts=st.lists(st.floats(0, 0.3), min_size=1, max_size=4), close_at=st.floats(0, 0.5),
                  first=st.floats(0, 0.3))
def test_for_random_timings_close_ends_every_group_and_nothing_runs_after_it(tmp_path_factory, starts, close_at,
                                                                           first):
    """Commands start at random times, each starting a child that writes a marker a
    little later and then waiting; close() comes at a random time. close() returns
    within its bound, every call fails with the refusal, nothing recorded runs after
    close() returns, and no marker is written after it."""
    base = tmp_path_factory.mktemp("timings")
    pids = base / "pids"
    owner = Supervisor(refuse=lambda: Refused("closed"))
    threads = []
    with ending(pids):
        for n, delay in enumerate(starts):
            script = (f"sleep {first}; ( sleep 0.4; touch \"{base}/marker-{n}\" ) </dev/null >/dev/null 2>&1 &\n"
                      f'echo $$ $! >> "{pids}"\nsleep 30\n')
            threads.append(in_thread(lambda script=script, delay=delay: (time.sleep(delay),
                                                                         owner.run(["/bin/sh", "-c", script],
                                                                                   timeout=60))[1]))
        time.sleep(close_at)
        began = time.monotonic()
        held = owner.close(5)
        closed_at, took = time.time(), time.monotonic() - began
        left = [pid for pid in pids_in(pids) if running(pid)]
        for thread, _ in threads:
            thread.join(30)
        time.sleep(0.8)                     # past every marker's delay
    assert held == set() and took < 5 and left == [], (held, took, left)
    assert all(isinstance(outcome.get("error"), Refused) for _, outcome in threads), [o for _, o in threads]
    late = [m.name for m in base.glob("marker-*") if m.stat().st_mtime > closed_at]
    assert late == [], f"written after close() returned: {late}"


@pytest.mark.parametrize("helper", ["the person check's ps", "the handoff brief's git", "retention's git"])
def test_every_helper_a_file_op_reaches_starts_its_process_through_the_supervisor(helper, tmp_path):
    """C-25.3: the helpers that start a process for a file op, beside salvage's and
    the diff's git (tested with them), go through `supervisor.run`: under a closed
    supervisor each is refused before it spawns anything."""
    from subfleet import retention_git
    from subfleet.conversations import peers
    from subfleet.sessions import handoff
    calls = {"the person check's ps": lambda: peers._ps(["/bin/ps", "-p", str(os.getpid()), "-o", "pid="]),
             "the handoff brief's git": lambda: handoff._run_git(tmp_path, ["rev-parse", "--show-toplevel"]),
             "retention's git": lambda: retention_git.run(["--version"])}
    closed = Supervisor(refuse=lambda: Refused("closed"))
    closed.close(1)
    with supervisor.supervising(closed), pytest.raises(Refused):
        calls[helper]()
    assert calls[helper]() is not None             # unsupervised, as before
