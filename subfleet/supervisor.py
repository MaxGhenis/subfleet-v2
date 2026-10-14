"""Subprocesses their owner can end: a launch gate, a process group supervised
through its leader's exit, and a `close()` that never waits on a spawn (C-25.3).

The conversation service's file ops run git: the worktree a worktree
conversation's create cuts, the snapshots and comparisons of `turn.diff` and
`conversation.diff`, and the lookups `workspace.check` makes. git runs the
repository's own hooks and filters, which may start processes of their own, and
git makes the leading directories of the path it is given. A post-checkout
hook that starts a background child and returns left that child in git's
process group, holding git's stdout and stderr: `subprocess.run` then waited
for those pipes to close until git's cap ran out, so a `git worktree add` that
had finished in milliseconds failed `worktree-failed` a minute later, and the
child lived on after the op, and after `close()`, free to write into a state
root its owner had removed (2026-10-14, Astra's review of cc65f36). A
`Supervisor` runs each such command so that:

- It starts only while the supervisor is open. The command is spawned behind a
  gate, `/bin/sh` waiting for a line on its stdin before it `exec`s the command
  (same pid), and the gate is opened under the supervisor's lock only while the
  supervisor is open. The spawn itself runs outside the lock, so `close()` never
  waits on one (Python's spawn waits, with no bound, for the child's exec); a
  spawn `close()` overtakes finds the gate refused, and nothing of the command
  runs.
- It runs in a process group of its own, which the gate's process leads and
  becomes the command. The group is signalled only while its leader is
  unreaped, so the group's id is still its own: the leader's exit is watched
  with kqueue (`NOTE_EXIT`), which does not reap it, and a call reaps its leader
  only after it has sent SIGKILL to the group and left the registry, under the
  lock `close()` signals under.
- It ends with its group. When the command exits, what it left in its group (a
  hook's background child) is sent SIGKILL; at its cap the whole group is. A
  call's cleanup after SIGKILL has a bound of its own (`CLEANUP_S`): a leader
  that SIGKILL has not ended by then is reaped by a thread of its own, and stays
  registered meanwhile.
- `close()` closes the gate, sends SIGKILL to every group still running, and
  waits at most its timeout, in all, for their calls to see them end.

A process that leaves its group (`setsid`, as a daemon does) is out of reach,
and so is a pipe it holds: a call reads the rest of its output for at most
`CLEANUP_S` once its group has ended.

`run()` is how the salvage and diff code, the worktree cut, retention's git
helper, the handoff brief's git and the person check's `ps` start a process: it
runs the command under the supervisor its caller's context names
(`supervising`), which the conversation service sets for each file op, and as
the plain `subprocess.run` it replaced everywhere else (the daemon's admission
and finalization snapshots, retention, the CLI).
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import locale
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator, Sequence

#: The gate: a line on stdin opens it, and the shell `exec`s the command, which
#: keeps its pid and group. End of input before a whole line refuses it: the
#: shell exits 125 having run nothing.
GATE = 'IFS= read -r _ || exit 125; exec "$@"'
SHELL = "/bin/sh"
#: How long a call waits for its leader to end after SIGKILL, and then for the
#: rest of its output once its group has ended.
CLEANUP_S = 2.0
_CHUNK = 65536

#: The supervisor the caller's context names (`supervising`), if any.
_current: contextvars.ContextVar[Supervisor | None] = contextvars.ContextVar("subfleet_supervisor", default=None)


class Closed(RuntimeError):
    """The supervisor has closed: no command starts after `close()`."""


@contextlib.contextmanager
def supervising(supervisor: Supervisor | None) -> Iterator[Supervisor | None]:
    """Run the block with `run()` starting its commands under `supervisor`."""
    token = _current.set(supervisor)
    try:
        yield supervisor
    finally:
        _current.reset(token)


def current() -> Supervisor | None:
    return _current.get()


def run(argv: Sequence, *, input: bytes | None = None, env: dict | None = None, cwd=None,
        timeout: float | None, text: bool = False, errors: str | None = None) -> subprocess.CompletedProcess:
    """`subprocess.run(argv, capture_output=True, ...)`, under the supervisor the
    context names when there is one (`Supervisor.run`)."""
    supervisor = _current.get()
    if supervisor is None:
        return subprocess.run(argv, input=input, env=env, cwd=cwd, capture_output=True, text=text, errors=errors,
                              timeout=timeout)
    return supervisor.run(argv, input=input, env=env, cwd=cwd, timeout=timeout, text=text, errors=errors)


class Supervisor:
    """Runs commands in process groups it can end, and ends them all at `close()`.

    `refuse` makes the exception a call raises once the supervisor has closed (the
    conversation service's is `store-closed`)."""

    def __init__(self, refuse: Callable[[], BaseException] | None = None):
        self._refuse = refuse or (lambda: Closed("closed: no command starts after close()"))
        self._lock = threading.Lock()
        self._released = threading.Condition(self._lock)
        self._closed = False
        self._groups: set[int] = set()       # leaders, each unreaped and leading its own group
        self._stopped: set[int] = set()      # the groups close() sent SIGKILL

    @property
    def closed(self) -> bool:
        return self._closed

    def running(self) -> set[int]:
        """The leaders of the groups still registered."""
        with self._lock:
            return set(self._groups)

    def run(self, argv: Sequence, *, input: bytes | None = None, env: dict | None = None, cwd=None,
            timeout: float | None, text: bool = False, errors: str | None = None,
            stdout_limit: int | None = None) -> subprocess.CompletedProcess:
        """Run `argv` to its end and return what `subprocess.run(capture_output=True)`
        would: its return code, stdout and stderr (decoded as `subprocess` decodes
        them under `text`).

        Raises what `subprocess.run` raises: `OSError` when it cannot start
        (`FileNotFoundError` for a command not on the PATH), and
        `subprocess.TimeoutExpired` once it has run `timeout` s, its group ended.
        Raises the supervisor's refusal when it has closed before the command
        started, or when `close()` ended the command. With `stdout_limit`, the
        command is stopped once its stdout holds more than that many bytes, and
        stdout is returned cut to `stdout_limit + 1` bytes, so the caller sees it
        was cut."""
        argv = list(argv)
        if self._closed:
            raise self._refuse()
        name = os.fsdecode(argv[0])
        if shutil.which(name, path=os.pathsep.join(os.get_exec_path(env))) is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
        process = subprocess.Popen([SHELL, "-c", GATE, "subfleet-gate", *argv], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                                   start_new_session=True)
        pipes = _Pipes(process, input)
        registered = kept = False            # kept: handed to a reaper thread, which closes the pipes
        try:
            registered = self._open(process, pipes)
            if not registered:
                # close() came first. The gate reads end of input and the shell exits
                # having run nothing; SIGKILL makes sure.
                pipes.close_stdin()
                _kill(process.pid)
                kept = self._finish(process, pipes, registered=False)
                if kept or self._closed:
                    raise self._refuse()
                raise OSError(errno.ESRCH, f"{name} ended before it started")
            pipes.start()
            deadline = None if timeout is None else time.monotonic() + timeout
            state = pipes.pump(deadline, limit=stdout_limit)
            # At its exit, what the command left in its group; at its cap or a cut, all
            # of it. Its leader is unreaped (only this call reaps it), so the group is
            # still its own.
            _kill(process.pid)
            kept = self._finish(process, pipes, registered=True)
            stopped = self._take_stopped(process.pid) if not kept else False
        except BaseException:
            if process.returncode is None and not kept:
                # Something unforeseen (an interrupt, a kqueue error): end the group
                # all the same, and leave its reaping to a thread of its own.
                _kill(process.pid)
                self._reap_in_thread(process, pipes, registered)
                kept = True
            raise
        finally:
            if not kept:
                pipes.close()
        if kept:
            # SIGKILL has not ended the leader within CLEANUP_S (a wait the kernel does
            # not interrupt). It is reaped when it ends; the command did not finish.
            raise subprocess.TimeoutExpired(argv, timeout if timeout is not None else 0.0,
                                            output=bytes(pipes.out), stderr=bytes(pipes.err))
        if stopped and process.returncode == -signal.SIGKILL:
            raise self._refuse()
        if state == "late":
            raise subprocess.TimeoutExpired(argv, timeout, output=bytes(pipes.out), stderr=bytes(pipes.err))
        out = bytes(pipes.out[: stdout_limit + 1]) if state == "cut" else bytes(pipes.out)
        err = bytes(pipes.err)
        if text:
            out, err = _decode(out, errors), _decode(err, errors)
        return subprocess.CompletedProcess(argv, process.returncode, out, err)

    def close(self, timeout: float) -> set[int]:
        """Start no command from now, send SIGKILL to every group still running, and
        wait at most `timeout` s in all for their calls to see them end. Returns the
        leaders still registered then (a group whose leader SIGKILL had not ended)."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._closed = True
            running = set(self._groups)
            for pid in running:
                _kill(pid)                   # registered, so unreaped: the group is still its own
            self._stopped |= running
            self._released.wait_for(lambda: not running & self._groups, max(0.0, deadline - time.monotonic()))
            return running & self._groups

    # --- one call's steps ------------------------------------------------------

    def _open(self, process: subprocess.Popen, pipes: _Pipes) -> bool:
        """Open the gate, under the lock `close()` signals under, unless `close()` came
        first or the gate's process has already ended (ended from outside)."""
        alive = pipes.watch_exit()
        with self._lock:
            if self._closed or not alive:
                return False
            self._groups.add(process.pid)
            try:
                os.write(pipes.stdin.fileno(), b"\n")     # one byte into an empty pipe: never blocks
            except BrokenPipeError:
                pass                                       # ended from outside; its exit is watched
            return True

    def _finish(self, process: subprocess.Popen, pipes: _Pipes, *, registered: bool) -> bool:
        """Wait at most CLEANUP_S for the leader to end, then leave the registry and
        reap it, and read the rest of the output for at most CLEANUP_S more. Returns
        True when the leader had not ended: a reaper thread then waits for it."""
        if not pipes.exited and pipes.pump(time.monotonic() + CLEANUP_S) != "exited":
            self._reap_in_thread(process, pipes, registered)
            return True
        if registered:
            self._release(process.pid)
        process.wait()                       # a zombie now: returns at once
        pipes.drain(time.monotonic() + CLEANUP_S)
        return False

    def _reap_in_thread(self, process: subprocess.Popen, pipes: _Pipes, registered: bool) -> None:
        """Reap a leader SIGKILL has not yet ended, once it ends. It stays registered
        until then (`close()` reports it), and its output is no longer read."""
        pipes.close_streams()

        def reap():
            try:
                with contextlib.suppress(OSError, ValueError):
                    pipes.pump(None)         # its exit; a closed or failed kqueue falls through to wait()
                if registered:
                    self._release(process.pid)
                    self._take_stopped(process.pid)
                process.wait()
            finally:
                pipes.close()

        threading.Thread(target=reap, name=f"subfleet-reap-{process.pid}", daemon=True).start()

    def _release(self, pid: int) -> None:
        """Leave the registry before the leader is reaped: once it is, its group's id
        may name another group, and `close()` must no longer signal it."""
        with self._lock:
            self._groups.discard(pid)
            self._released.notify_all()

    def _take_stopped(self, pid: int) -> bool:
        with self._lock:
            stopped = pid in self._stopped
            self._stopped.discard(pid)
            return stopped


class _Pipes:
    """One call's pipes and its leader's exit, moved and watched through one kqueue."""

    def __init__(self, process: subprocess.Popen, input: bytes | None):
        self.pid = process.pid
        self.kq = select.kqueue()
        self.stdin = process.stdin
        self.pending = memoryview(bytes(input or b""))
        self.out, self.err = bytearray(), bytearray()
        self.streams = {process.stdout.fileno(): (process.stdout, self.out),
                        process.stderr.fileno(): (process.stderr, self.err)}
        self.exited = False
        self.started = False                 # the output pipes are watched (`start`)

    def watch_exit(self) -> bool:
        """Watch for the leader's exit; False when it has already ended. The gate's
        process waits on its stdin, so only something outside ends it this early."""
        try:
            self.kq.control([select.kevent(self.pid, select.KQ_FILTER_PROC,
                                           select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)], 0, 0)
        except ProcessLookupError:
            self.exited = True
        return not self.exited

    def start(self) -> None:
        """Watch the output pipes, and stdin while there is input to write."""
        changes = []
        for fd in self.streams:
            os.set_blocking(fd, False)
            changes.append(select.kevent(fd, select.KQ_FILTER_READ, select.KQ_EV_ADD))
        if self.pending:
            os.set_blocking(self.stdin.fileno(), False)
            changes.append(select.kevent(self.stdin.fileno(), select.KQ_FILTER_WRITE, select.KQ_EV_ADD))
        else:
            self.close_stdin()
        self.kq.control(changes, 0, 0)
        self.started = True

    def pump(self, deadline: float | None, *, limit: int | None = None) -> str:
        """Move output and input until the leader exits ("exited"), stdout holds more
        than `limit` bytes ("cut"), or `deadline` passes ("late")."""
        while not self.exited:
            if limit is not None and len(self.out) > limit:
                return "cut"
            if not self._step(deadline):
                return "late"
        return "exited"

    def drain(self, deadline: float) -> bool:
        """Read both pipes to their end, until `deadline`; False if one is open then.
        A gate that never opened wrote nothing to read."""
        while self.started and self.streams:
            if not self._step(deadline):
                return False
        return True

    def _step(self, deadline: float | None) -> bool:
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            return False
        for event in self.kq.control(None, 8, left):
            if event.filter == select.KQ_FILTER_PROC:
                self.exited = True
            elif event.filter == select.KQ_FILTER_READ and event.ident in self.streams:
                self._read(event.ident)
            elif (event.filter == select.KQ_FILTER_WRITE and self.stdin is not None
                  and event.ident == self.stdin.fileno()):
                self._write()
        return True

    def _read(self, fd: int) -> None:
        stream, buffer = self.streams[fd]
        try:
            data = os.read(fd, _CHUNK)
        except BlockingIOError:
            return
        if data:
            buffer += data
        else:                                # end of the pipe: closing it removes its event
            del self.streams[fd]
            stream.close()

    def _write(self) -> None:
        try:
            written = os.write(self.stdin.fileno(), self.pending[:_CHUNK])
        except BlockingIOError:
            return
        except BrokenPipeError:
            written = len(self.pending)      # the command stopped reading; the rest is dropped
        self.pending = self.pending[written:]
        if not self.pending:
            self.close_stdin()

    def close_stdin(self) -> None:
        if self.stdin is not None:
            stdin, self.stdin = self.stdin, None
            with contextlib.suppress(OSError):
                stdin.close()

    def close_streams(self) -> None:
        for stream, _ in self.streams.values():
            stream.close()
        self.streams.clear()

    def close(self) -> None:
        self.close_stdin()
        self.close_streams()
        self.kq.close()


def _kill(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _decode(data: bytes, errors: str | None) -> str:
    """As `subprocess` decodes a stream under `text=True`: the locale's encoding
    (UTF-8 in UTF-8 mode), universal newlines."""
    encoding = "utf-8" if sys.flags.utf8_mode else locale.getencoding()
    return data.decode(encoding, errors or "strict").replace("\r\n", "\n").replace("\r", "\n")
