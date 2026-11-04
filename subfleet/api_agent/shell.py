"""The `bash` tool: one persistent bash under the seatbelt profile (C-31.13).

The harness writes each command to a file in its control folder, which the sandbox
cannot write, and has bash source it with stdin from /dev/null. A marker carrying a
fresh nonce, the exit status and `$PWD` follows each command on the shell's stdout,
so the harness knows where the output ends and where the shell now stands.

The shell runs in its own process group inside the harness's session, so the
guardian's containment still covers it, and the harness kills the group on a
timeout, a restart and at exit.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

BASH = "/bin/bash"
COMMAND_TIMEOUT_S = 900.0
HEAD_BYTES = 40_000
TAIL_BYTES = 20_000
#: Room for the marker itself (and a long $PWD) at the end of the tail.
MARGIN = 8_192


@dataclass
class ShellResult:
    output: str
    rc: int | None
    cwd: str
    timed_out: bool = False
    exited: bool = False


class _Capture:
    """The output of one command, its middle dropped past HEAD_BYTES + TAIL_BYTES."""

    def __init__(self, initial: bytes = b"") -> None:
        self.data = bytearray()
        self.head = b""
        self.total = 0
        self.cut = False
        self.feed(initial)

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.data += chunk
        if not self.cut and len(self.data) > HEAD_BYTES + TAIL_BYTES + MARGIN:
            self.head = bytes(self.data[:HEAD_BYTES])
            del self.data[:len(self.data) - TAIL_BYTES - MARGIN]
            self.cut = True
        elif self.cut and len(self.data) > TAIL_BYTES + 2 * MARGIN:
            del self.data[:len(self.data) - TAIL_BYTES - MARGIN]

    def text(self, end: int) -> str:
        body = bytes(self.data[:end])
        if not self.cut:
            return body.decode("utf-8", "replace")
        tail = body[-TAIL_BYTES:]
        dropped = self.total - (len(self.data) - end) - len(self.head) - len(tail)
        return (self.head.decode("utf-8", "replace") + f"\n[... {dropped} bytes of output omitted ...]\n"
                + tail.decode("utf-8", "replace"))


class Shell:
    def __init__(self, prefix: list[str], env: Mapping[str, str], workdir: Path, control: Path, *,
                 timeout_s: float = COMMAND_TIMEOUT_S) -> None:
        self._prefix = list(prefix)
        self._env = dict(env)
        self._workdir = str(workdir)
        self._control = control
        self._timeout = timeout_s
        self._proc: subprocess.Popen | None = None
        self._cond = threading.Condition()
        self._capture = _Capture()
        self._eof = False
        self._reader: threading.Thread | None = None
        self._count = 0
        self.cwd = self._workdir

    # -- lifecycle ---------------------------------------------------------------------
    def _start(self) -> None:
        self._eof = False
        self._capture = _Capture()
        self.cwd = self._workdir
        self._proc = subprocess.Popen([*self._prefix, BASH, "--noprofile", "--norc"], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self._workdir,
                                      env=self._env, close_fds=True, process_group=0)
        self._reader = threading.Thread(target=self._read, args=(self._proc,), name="api-agent-shell",
                                        daemon=True)
        self._reader.start()

    def _read(self, proc: subprocess.Popen) -> None:
        """Feed this shell's output to the capture. The thread owns the pipe and closes
        it itself, so its descriptor number is never reused under a blocked read."""
        stream = proc.stdout
        fd = stream.fileno()
        try:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                with self._cond:
                    if proc is not self._proc:
                        return
                    if not chunk:
                        self._eof = True
                        self._cond.notify_all()
                        return
                    self._capture.feed(chunk)
                    self._cond.notify_all()
        finally:
            stream.close()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # anything the shell left in its group
        except OSError:
            pass

    def restart(self) -> None:
        self.close()
        self._start()

    # -- commands ----------------------------------------------------------------------
    def run(self, command: str) -> ShellResult:
        if self._proc is None or self._proc.poll() is not None:
            self.close()
            self._start()
        self._count += 1
        nonce = secrets.token_hex(12)
        script = self._control / f"command-{self._count:05d}.sh"
        fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(command)
            if not command.endswith("\n"):
                handle.write("\n")
        marker = re.compile(rb"\n__SF_" + nonce.encode() + rb"_RC=(-?\d+);PWD=(.*?);__SF_" + nonce.encode()
                            + rb"_END\n", re.S)
        line = (f"{{ . {shlex.quote(str(script))}; }} </dev/null 2>&1; "
                f"printf '\\n__SF_{nonce}_RC=%s;PWD=%s;__SF_{nonce}_END\\n' \"$?\" \"$PWD\"\n")
        with self._cond:
            leftover = bytes(self._capture.data)
            self._capture = _Capture(leftover)
        try:
            self._proc.stdin.write(line.encode())
            self._proc.stdin.flush()
        except OSError:
            pass
        deadline = time.monotonic() + self._timeout
        with self._cond:
            while True:
                found = marker.search(self._capture.data)
                if found:
                    text = self._capture.text(found.start())
                    rest = bytes(self._capture.data[found.end():])
                    self._capture = _Capture(rest)
                    self.cwd = found.group(2).decode("utf-8", "surrogateescape")
                    return ShellResult(text, int(found.group(1)), self.cwd)
                if self._eof:
                    text = self._capture.text(len(self._capture.data))
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    text = self._capture.text(len(self._capture.data))
                    self._cond.release()
                    try:
                        self.close()
                    finally:
                        self._cond.acquire()
                    return ShellResult(text, None, self._workdir, timed_out=True)
                self._cond.wait(min(remaining, 1.0))
        proc = self._proc
        try:
            rc = proc.wait(timeout=5) if proc is not None else None
        except subprocess.TimeoutExpired:
            rc = None
        self.close()
        return ShellResult(text, rc, self._workdir, exited=True)
