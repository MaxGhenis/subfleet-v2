"""Uncapped Python stacks, formatted outside the signal handler and app locks."""
from __future__ import annotations

import os
import sys
import threading


FORMAT = "named-v1"
COMPLETE = "=== end subfleet all-thread stack dump ==="


def dump_threads(fd: int) -> None:
    """Capture every Python frame, without traceback/linecache's file reads."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    frames = sys._current_frames()
    lines = [f"=== subfleet all-thread stack dump: {len(frames)} threads ===\n"]
    try:
        for ident, frame in sorted(frames.items()):
            lines.append(f"Thread 0x{ident:x} name={names.get(ident, '<unregistered>')!r} "
                         "(most recent call first):\n")
            while frame is not None:
                code = frame.f_code
                lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}\n')
                frame = frame.f_back
            lines.append("\n")
    finally:
        # Frames retain the locals of every live thread; keep none after a dump.
        frames.clear()
    lines.append(COMPLETE + "\n")
    data = memoryview("".join(lines).encode("utf-8", errors="backslashreplace"))
    while data:
        data = data[os.write(fd, data):]


class StackDumper:
    """A nonblocking self-pipe is the only work done by the Python handler.

    The worker owns a duplicate log descriptor, so even a slow dump cannot write
    through a descriptor closed and reused by daemon shutdown. It waits for pipe
    data, without polling, and coalesces requests received during one dump.
    """

    def __init__(self, fd: int):
        self._read, self._write = os.pipe()
        os.set_blocking(self._write, False)
        self._log = os.dup(fd)
        self._thread = threading.Thread(target=self._run, name="subfleet-stack-dump", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            for opened in (self._read, self._write, self._log):
                os.close(opened)
            raise

    def request(self) -> None:
        # No Event.set(), logging, store access, or lock acquisition in a signal
        # callback: it may interrupt the same thread while it holds those locks.
        try:
            os.write(self._write, b"s")
        except OSError:
            pass  # A full pipe already has a request; a closing one needs none.

    def _run(self) -> None:
        try:
            while os.read(self._read, 65536):
                try:
                    dump_threads(self._log)
                except Exception:
                    # Diagnostics must not kill the worker for later requests.
                    pass
        finally:
            os.close(self._read)
            os.close(self._log)

    def close(self) -> None:
        writer, self._write = self._write, -1
        if writer >= 0:
            os.close(writer)
            self._thread.join(timeout=1)
