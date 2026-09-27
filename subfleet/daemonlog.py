"""`daemon.log`, rotated by size, and read across a rotation (C-2.5).

The daemon writes its log through several descriptors: the logging handler's
stream, which `faulthandler`'s SIGUSR1 registration also writes through (C-3.6);
the descriptor `watch_stop` opens at start for the C-5.8a dump; and stdout and
stderr when launchd (`StandardOutPath`, `StandardErrorPath`) or `daemon start`
point them at the log.

Rotation is by name: the log is linked to `daemon.log.1` (the older ones move
up one) and a new file takes its name in one rename. Then each writer is moved
to the new file, by the means that is safe for how it is written:

- The handler, written by every thread, gets a new stream under its own lock
  (`setStream`), as any log line holds it, and SIGUSR1 is registered again on
  that stream, which only changes the file faulthandler writes to. The old
  stream stays open for `REPLACED_KEEP_S`, so a dump already under way
  finishes. No descriptor is moved under a writer: on macOS a `dup2` over a
  descriptor another thread is writing makes that write fail with EBADF
  (measured on 26.6.2: 366,398 of 400,000 writes while one thread swapped).
- `watch_stop`'s descriptor and stdout and stderr cannot be given a new number,
  so the new file is `dup2`-ed under theirs. They are written only as the
  daemon stops (and no rotation starts once it is stopping) or when something
  has already gone wrong (a thread's traceback, a warning); such a write in
  the moment of the move can fail and be lost.

Copy-truncate was the other choice. It needs as much free space as the log
(the volume this was built for was full), copies the log every rotation, and
loses whatever is written between the copy and the truncate. The daemon's
children never hold the log (their stdout and stderr are `/dev/null`, a pipe
or a file of their own), so renaming leaves no other process writing to a
renamed file.

The readers (`daemon logs`, `daemon stacks`, `tools/store_contention_repro.py`)
follow a position through rotations: a `Mark` is a file's identity and an
offset in it, and `read_since` finds that file among `daemon.log`,
`daemon.log.1`, ... and reads on from there.
"""

from __future__ import annotations

import errno
import logging
import math
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, NamedTuple

#: C-2.5 defaults, all policy (`daemon_log.*`): rotate at 20 MiB, keep five
#: rotated files, look every 5 s. A `max_bytes` of 0 turns rotation off.
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUPS = 5
DEFAULT_CHECK_S = 5.0
#: The smallest `max_bytes` policy accepts, other than 0: one SIGUSR1 dump of
#: a busy daemon is tens of KiB, and a log that rotates on every look keeps
#: nothing to read.
MIN_MAX_BYTES = 64 * 1024
#: The most rotated files policy may keep; readers look no further.
MAX_BACKUPS = 99
#: A rotation that keeps failing says so at most this often.
TROUBLE_EVERY_S = 600
#: How long `close()` waits for a rotation in progress before it leaves the
#: streams open rather than close a descriptor a rotation may still use.
CLOSE_WAIT_S = 2.0
#: How long a stream the handler no longer writes stays open: a SIGUSR1 dump
#: that began on it before faulthandler was pointed at the new stream writes
#: to its descriptor number until it ends, and closing that number under it
#: could let the dump's rest land in whatever reuses the number. A dump takes
#: milliseconds; the file's space (a deleted log's) is freed within a minute.
REPLACED_KEEP_S = 60.0

_OPEN_APPEND = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC

#: Descriptors opened with `open_follower`, by the log's absolute path. A
#: rotation of that log moves each one that is still on the file it replaces.
#: Held across a rotation's renames and moves, so a follower opened meanwhile
#: is never left on the old file.
_followers_lock = threading.Lock()
_followers: dict[str, set[int]] = {}


def _key(path: str | os.PathLike) -> str:
    return os.path.abspath(os.fspath(path))


def open_follower(path: str | os.PathLike) -> int:
    """An append descriptor on the log at `path` that rotation keeps on the
    current file with `dup2` (C-2.5): for a descriptor written only when
    nothing else is going on, as `watch_stop`'s is, since a write racing the
    move can fail. Kept for the life of the process; one closed before then
    goes through `release`, so no rotation moves a number that was reused."""
    with _followers_lock:
        fd = os.open(path, _OPEN_APPEND, 0o600)
        _followers.setdefault(_key(path), set()).add(fd)
    return fd


def release(path: str | os.PathLike, fd: int) -> None:
    """Close a descriptor `open_follower` gave, taking it out of rotation first."""
    with _followers_lock:
        _followers.get(_key(path), set()).discard(fd)
        os.close(fd)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _same_file(fd: int, identity: tuple[int, int]) -> bool:
    try:
        return _identity(os.fstat(fd)) == identity
    except OSError:
        return False


def backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


class DaemonLog:
    """The daemon's `daemon.log`: the logging handler that writes it, and the
    thread that rotates it (C-2.5).

    Only the `subfleet-logrotate` thread (or a caller of `check`) renames,
    links, unlinks or moves a writer, holding `_lock`, which nothing else
    takes. Request threads, pools and the control loop meet it only in the
    handler's lock, held for one stream swap as for one log line.
    `on_stream(stream)` runs under that lock after each swap: the daemon
    registers SIGUSR1 on the new stream there.
    """

    def __init__(self, path: str | os.PathLike, *,
                 on_stream: Callable[[IO[str]], None] | None = None):
        self.path = Path(path)
        self.max_bytes, self.backups, self.check_s = DEFAULT_MAX_BYTES, DEFAULT_BACKUPS, DEFAULT_CHECK_S
        self.on_stream = on_stream
        self._lock = threading.Lock()
        self._closed = False
        self.handler = logging.StreamHandler(os.fdopen(os.open(self.path, _OPEN_APPEND, 0o600), "a"))
        # (when, stream) for each stream the handler stopped writing, kept
        # open `REPLACED_KEEP_S` for a dump still writing to it.
        self._replaced: list[tuple[float, IO[str]]] = []
        self._stop = threading.Event()
        self._stopping: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._log: logging.Logger | None = None
        self.rotations = 0
        # [when trouble was last said, failures held back since]
        self._trouble = [-math.inf, 0]

    @property
    def stream(self) -> IO[str]:
        """The stream the handler writes now; a rotation replaces it."""
        return self.handler.stream

    @property
    def fd(self) -> int:
        return self.handler.stream.fileno()

    def configure(self, *, max_bytes: int, backups: int, check_s: float) -> None:
        """The `daemon_log` policy section, validated by `load_policy`."""
        self.max_bytes, self.backups, self.check_s = int(max_bytes), int(backups), float(check_s)

    def start(self, log: logging.Logger, stopping: threading.Event | None = None) -> None:
        """Look every `check_s` on a thread of its own, saying what happens in
        `log` (a logger this handler serves). Nothing is rotated or moved once
        `stopping` is set: the C-5.8a dump is then due through its descriptor."""
        self._log, self._stopping = log, stopping
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="subfleet-logrotate", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=CLOSE_WAIT_S)

    def close(self) -> None:
        """Stop looking and close the streams. A rotation still in progress
        after `CLOSE_WAIT_S` keeps them open instead: it may yet use them."""
        self.stop()
        if not self._lock.acquire(timeout=CLOSE_WAIT_S):
            return
        try:
            if not self._closed:
                self._closed = True
                self.handler.acquire()
                try:
                    self.handler.stream.close()
                finally:
                    self.handler.release()
                self._close_replaced(math.inf)
        finally:
            self._lock.release()

    def _run(self) -> None:
        while not self._stop.wait(self.check_s):
            self.look()

    def look(self) -> str | None:
        """One pass of the thread: `check`, and a failure said at most every
        `TROUBLE_EVERY_S`, never raised."""
        try:
            return self.check()
        except Exception as exc:                          # noqa: BLE001 - the log must never stop the daemon
            self._say_trouble(exc)
            return None

    def check(self) -> str | None:
        """One look: `"rotated"`, `"reopened"` (the name no longer held this
        log, which was moved or deleted: writing moves to what the name holds
        now, or to a new file), or None. Raises what a failed step raised;
        nothing changes for the writers until the new file has the name."""
        with self._lock:
            if self._closed or (self._stopping is not None and self._stopping.is_set()):
                return None
            self._close_replaced(REPLACED_KEEP_S)
            ours = os.fstat(self.fd)
            try:
                there = os.stat(self.path)
            except FileNotFoundError:
                there = None
            if there is None or _identity(there) != _identity(ours):
                result, size = self._follow_the_name(ours, there), None
            elif (self.max_bytes and ours.st_size >= self.max_bytes
                  and stat.S_ISREG(os.lstat(self.path).st_mode)):
                # Only a log that is a file at the name: one an operator put
                # behind a symlink (or `/dev/null`) is theirs to manage.
                result, size = self._rotate(ours), ours.st_size
            else:
                return None
            self._trouble[1] = 0
        if result == "rotated":
            self._say(logging.INFO, f"daemon.log: rotated at {size} bytes to {backup_path(self.path, 1).name}, "
                      f"keeping {self.backups} (C-2.5)")
        else:
            self._say(logging.WARNING, "daemon.log: the log was moved or deleted under the daemon; "
                      "writing to the file now at that name (C-2.5)")
        return result

    # --- the steps, all under `_lock` ------------------------------------------

    def _fresh(self) -> tuple[int, Path]:
        """A new, empty log beside the old one, not yet at its name."""
        new = self.path.with_name(self.path.name + ".new")
        new.unlink(missing_ok=True)                       # a rotation that died part way
        return os.open(new, _OPEN_APPEND | os.O_EXCL, 0o600), new

    def _rotate(self, ours: os.stat_result) -> str:
        first = backup_path(self.path, 1)
        # Make room at `.1` only when it is taken: a retry after a step below
        # failed finds it free and deletes nothing more. The oldest goes
        # before anything is created, so a full volume has room back first.
        shift = os.path.lexists(first)
        if shift:
            backup_path(self.path, self.backups).unlink(missing_ok=True)
        new_fd, new = self._fresh()
        try:
            if shift:
                for index in range(self.backups - 1, 0, -1):
                    try:
                        os.rename(backup_path(self.path, index), backup_path(self.path, index + 1))
                    except FileNotFoundError:
                        pass
            with _followers_lock:
                # A second name for this log, then the new file takes the
                # first in one step, so `daemon.log` never goes missing; on a
                # file system without hard links, a rename (the name is
                # missing until the next one, and the writers keep writing).
                linked = True
                try:
                    os.link(self.path, first)
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        raise
                    os.rename(self.path, first)
                    linked = False
                try:
                    os.rename(new, self.path)
                except OSError:
                    # Back to one name, so the retry finds `.1` free and shifts
                    # nothing again. After the rename fallback the name is
                    # missing instead, and the next look makes a new file.
                    if linked:
                        first.unlink(missing_ok=True)
                    raise
                fd, new_fd = new_fd, -1                   # `_switch` owns it from here
                self._switch(fd, _identity(ours))
        finally:
            if new_fd >= 0:
                os.close(new_fd)
        # Rotated files past `backups`: a policy that now keeps fewer.
        for index in range(self.backups + 1, MAX_BACKUPS + 1):
            backup_path(self.path, index).unlink(missing_ok=True)
        self.rotations += 1
        return "rotated"

    def _follow_the_name(self, ours: os.stat_result, there: os.stat_result | None) -> str:
        if there is None:
            new_fd, new = self._fresh()
            try:
                with _followers_lock:
                    # `link`, not `rename`: a file that took the name since
                    # the look is kept, and followed next time.
                    os.link(new, self.path)
                    new.unlink()
                    fd, new_fd = new_fd, -1               # `_switch` owns it from here
                    self._switch(fd, _identity(ours))
            finally:
                if new_fd >= 0:
                    os.close(new_fd)
            return "reopened"
        # Something else has the name now. Write to it only if it is a file or
        # a device (`/dev/null`) that a write cannot block on: never a FIFO.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISCHR(info.st_mode)):
                raise OSError(f"{self.path} is not a file; the daemon keeps writing to the log it had")
            os.set_blocking(fd, True)
            with _followers_lock:
                adopted, fd = fd, -1                      # `_switch` owns it from here
                self._switch(adopted, _identity(ours))
        finally:
            if fd >= 0:
                os.close(fd)
        return "reopened"

    def _switch(self, new_fd: int, old: tuple[int, int]) -> None:
        """Every writer of the log `old` names writes `new_fd`'s file from now
        on; `new_fd` becomes the handler's stream. It owns `new_fd` whatever
        happens: once the handler writes it, no caller may close that number."""
        try:
            new = _identity(os.fstat(new_fd))
            stream = os.fdopen(new_fd, "a")
        except BaseException:
            os.close(new_fd)
            raise
        self.handler.acquire()
        try:
            replaced = self.handler.setStream(stream)
            if self.on_stream is not None:
                try:
                    self.on_stream(stream)
                except Exception:                         # noqa: BLE001 - the swap is done either way
                    pass
        finally:
            self.handler.release()
        self._replaced.append((time.monotonic(), replaced))
        numbers = {replaced.fileno(), new_fd}
        family: set[tuple[int, int]] | None = None

        def ours(fd: int) -> bool:
            """On the log this replaced, or on one of its rotated files: a
            move that failed part way at an earlier rotation left it there."""
            nonlocal family
            try:
                identity = _identity(os.fstat(fd))
            except OSError:
                return False
            if identity == old:
                return True
            if identity == new:
                return False
            if family is None:
                family = set()
                for index in range(1, MAX_BACKUPS + 1):
                    try:
                        family.add(_identity(os.stat(backup_path(self.path, index))))
                    except OSError:
                        pass
            return identity in family

        followers = _followers.get(_key(self.path), set())
        for fd in sorted(followers):
            if fd in numbers or _same_file(fd, new):
                continue
            if ours(fd):
                os.dup2(new_fd, fd, inheritable=False)
            else:
                followers.discard(fd)                     # closed without `release`: never touch it again
        # stdout and stderr when they are this log (launchd's StandardOutPath
        # and StandardErrorPath, `daemon start`): tracebacks and warnings
        # written to them land in the current file too, and the old file's
        # space is freed once it ages out. Children inherit them.
        for fd in (1, 2):
            if fd not in numbers and fd not in followers and ours(fd):
                os.dup2(new_fd, fd, inheritable=True)

    def _close_replaced(self, older_than: float) -> None:
        now = time.monotonic()
        kept = []
        for when, stream in self._replaced:
            if now - when >= older_than:
                try:
                    stream.close()
                except OSError:
                    pass
            else:
                kept.append((when, stream))
        self._replaced = kept

    # --- saying so -------------------------------------------------------------

    def _say(self, level: int, text: str) -> None:
        if self._log is None:
            return
        try:
            self._log.log(level, "%s", text)
        except Exception:                                 # noqa: BLE001 - a report never fails a rotation
            pass

    def _say_trouble(self, exc: BaseException) -> None:
        when, held = self._trouble
        now = time.monotonic()
        if now - when < TROUBLE_EVERY_S:
            self._trouble[1] = held + 1
            return
        self._trouble[:] = [now, 0]
        more = f" ({held} more since the last report)" if held else ""
        self._say(logging.WARNING, f"daemon.log could not be rotated: {type(exc).__name__}: {exc}{more}; "
                  f"trying again every {self.check_s:g} s (C-2.5)")


# --- reading across rotations --------------------------------------------------

class Mark(NamedTuple):
    """A position in the log: a file's identity and an offset in it."""
    dev: int
    ino: int
    offset: int


def _names(path: Path) -> Iterator[Path]:
    """The log, then its rotated files, newest first."""
    yield path
    for index in range(1, MAX_BACKUPS + 1):
        yield backup_path(path, index)


def _open_regular(path: Path) -> int | None:
    """A read descriptor on `path` when it is a regular file, else None. Never
    blocks in `open()`, even on a FIFO at the name."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _pread(fd: int, start: int, end: int) -> bytes:
    pieces = []
    while start < end:
        piece = os.pread(fd, min(end - start, 1 << 20), start)
        if not piece:
            break
        pieces.append(piece)
        start += len(piece)
    return b"".join(pieces)


def mark(path: str | os.PathLike) -> Mark | None:
    """The end of the log now, or None when there is no log."""
    fd = _open_regular(Path(path))
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        return Mark(info.st_dev, info.st_ino, info.st_size)
    finally:
        os.close(fd)


def read_since(path: str | os.PathLike, since: Mark | None) -> tuple[bytes, Mark | None]:
    """Everything written to the log after `since`, across any rotations, and
    the mark to read on from. From a mark of None (there was no log), the
    current file from its start. A marked file whose size fell below the mark
    (truncated in place) is read from its start; a marked file no longer
    kept is read as the rotated files that are."""
    path = Path(path)
    opened: list[tuple[int, os.stat_result]] = []
    newer: list[tuple[int, os.stat_result]] = []          # newest first
    found: tuple[int, os.stat_result] | None = None
    seen: set[tuple[int, int]] = set()
    try:
        for index, name in enumerate(_names(path)):
            if since is None and index > 0:
                break
            fd = _open_regular(name)
            if fd is None:
                continue
            info = os.fstat(fd)
            opened.append((fd, info))
            if _identity(info) in seen:
                continue
            seen.add(_identity(info))
            if since is not None and _identity(info) == (since.dev, since.ino):
                found = (fd, info)
                break
            newer.append((fd, info))
        pieces = []
        if found is not None:
            fd, info = found
            pieces.append(_pread(fd, since.offset if info.st_size >= since.offset else 0, info.st_size))
        for fd, info in reversed(newer):
            pieces.append(_pread(fd, 0, info.st_size))
        last = newer[0][1] if newer else found[1] if found else None
        after = Mark(last.st_dev, last.st_ino, last.st_size) if last is not None else since
        return b"".join(pieces), after
    finally:
        for fd, _ in opened:
            os.close(fd)


def tail(path: str | os.PathLike, lines: int) -> tuple[list[str], Mark | None]:
    """The log's last `lines` lines, reaching into rotated files when the
    current one has fewer, and the mark just past them (to follow from)."""
    path = Path(path)
    chunks: list[bytes] = []                              # newest file first
    newlines, after = 0, None
    seen: set[tuple[int, int]] = set()
    for index, name in enumerate(_names(path)):
        fd = _open_regular(name)
        if fd is None:
            if index == 0:
                continue
            break                                         # rotated files are numbered without gaps
        try:
            info = os.fstat(fd)
            if _identity(info) in seen:
                continue
            seen.add(_identity(info))
            if after is None:
                after = Mark(info.st_dev, info.st_ino, info.st_size)
            if lines <= 0:
                break
            end, pieces = info.st_size, []
            while end > 0 and newlines <= lines:
                start = max(0, end - (1 << 16))
                piece = _pread(fd, start, end)
                newlines += piece.count(b"\n")
                pieces.append(piece)
                end = start
            chunks.append(b"".join(reversed(pieces)))
        finally:
            os.close(fd)
        if newlines > lines:
            break
    if lines <= 0:
        return [], after
    text = b"".join(reversed(chunks)).decode(errors="replace")
    return text.splitlines()[-lines:], after
