"""The guardian's control relay: numbered stdin frames, applied once (C-26.4).

A conversation turn's provider reads its stdin from a pipe the guardian owns.
The daemon never holds that pipe: it connects to a private Unix socket the
guardian listens on and sends numbered frames. The guardian appends each new
frame to `stdin.jsonl` with fsync, then writes it to the pipe, then
acknowledges by number. A frame whose number was already applied is
acknowledged again without being written, and a gap is refused, so a daemon
that crashes between sending and hearing back can resend safely and the
provider never reads the same input twice.

The guardian does not interpret provider messages. A frame is
`{"seq": n, "op": "write", "line": "<one line>", "tag": "<daemon label>"}`,
`{"seq": n, "op": "close", "tag": ...}`, or `{"seq": n, "op": "signal",
"sig": "INT", "tag": ...}`, which the guardian delivers to its own child, a
process it has not reaped and whose pid therefore cannot have been reused
(design D-13: SIGINT ends a Claude turn where SIGTERM would leave it
resumable). `tag` is the daemon's own label for
the frame (for example `user-message` or `approval:ap-…`), logged so a
rebuilt driver knows what it already sent; the guardian only stores it.

Large lines (an image attachment in base64) are logged by digest, not
content: the daemon keeps the original, and replay needs to know only that the
frame was applied.

The relay advertises its version and frame cap twice: in `start.json`
(`relay: {"version", "frame_max"}`, published before the daemon can connect)
and in its answer to `{"op": "status"}`. A status request is not a frame: it
carries no number, is never logged, and is answered after any frame being
applied has finished (frames and status share one lock and one connection at
a time), with the number of frames applied, whether stdin is closed, whether
the child still runs, and the cap. A daemon asks for it before it sends or
replays anything on a new runner (review IR-27). A relay older than version 2
answers it `bad-frame`, as it answers anything without a number; the daemon
then relies on the log file, as before.

Each frame carries the SHA-256 of its line. The log holds an `intent` record
(fsynced before the pipe write) and then a `written` or `failed` record, so a
write that did not finish is never mistaken for one that did: a resent number
is a duplicate only when its hash matches the logged intent and the intent was
written; otherwise the reply says `conflict` or `failed` and the daemon treats
the turn's delivery as unknown. When the guardian is given the daemon's lock
file it accepts a connection only from the process that lock names (pid, boot
id and start time), so another process of the same user cannot write to the
provider's stdin.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import select
import socket
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

from .state_files import open_state

FRAME_MAX = 64 * 1024 * 1024        # one image in base64 plus JSON, with margin
LOG_INLINE_MAX = 64 * 1024          # frames logged verbatim up to this size
ACK_TIMEOUT_S = 60.0
SOCKET_DIR_NAME = "run"
RELAY_VERSION = 2                   # 2: `status` and the advertised frame cap (review IR-27)
REPLY_TIMEOUT_S = 1.0               # a departed/nonreading client cannot hold child exit; also stop's
                                    # wait for an exchange already read (C-26.4, #230 review r1)
JOIN_TIMEOUT_S = 1.0                # stop's wait for the relay thread once that exchange is done or cut
CLEANUP_TIMEOUT_S = 1.0             # stop's wait for its cleanup; stop returns within the three summed
                                    # (C-26.4, #236 review r1)
REPLY_MAX = 1 << 16                 # an acknowledgement or status line
WRITE_POLL_S = 0.05                 # a stdin write waiting for room looks this often for stop's cut
# Fixed text: nothing of the frame, its tag or its client (#230 review r1).
CUT_LOG_LINE = "relay stop: an exchange still running at its bound was cut; its connection was shut down"


def _stderr_line(text: str) -> None:
    """Write one line to descriptor 2 with raw writes, never through `logging` or
    `sys.stderr` (#236 review r2). A write held there (a pipe nobody reads) then
    holds no lock that interpreter shutdown takes: `logging.shutdown`, run at exit,
    takes every handler's lock, and the exit's flush of the standard streams takes
    their buffers' locks, so either would hold the guardian's exit for as long as
    the write. Best effort: an error drops the line."""
    data = memoryview((text + "\n").encode())
    try:
        while data:
            data = data[os.write(2, data):]
    except OSError:
        pass


def advertisement() -> dict:
    """What `start.json` and a status answer say about this relay (IR-27)."""
    return {"version": RELAY_VERSION, "frame_max": FRAME_MAX}


def socket_path(state_root: str | Path, attempt_id: str) -> Path:
    """`<state root>/run/<16 hex>.sock`, short enough for AF_UNIX (103 bytes)."""
    digest = hashlib.sha256(attempt_id.encode()).hexdigest()[:16]
    return Path(state_root) / SOCKET_DIR_NAME / f"{digest}.sock"


SIGNALS = {"INT": 2}


def line_sha256(line: str | None) -> str:
    return hashlib.sha256((line or "").encode("utf-8")).hexdigest()


def frame_sha256(op: str, line: str | None = None, sig: str | None = None) -> str:
    return line_sha256(f"signal:{sig}" if op == "signal" else line if op == "write" else None)


def _intent(frame: dict) -> dict:
    line = frame.get("line")
    record = {"kind": "intent", "seq": frame["seq"], "op": frame["op"], "tag": frame.get("tag"),
              "sha256": frame["sha256"]}
    if frame["op"] == "signal":
        record["sig"] = frame.get("sig")
    if isinstance(line, str):
        encoded = line.encode("utf-8")
        record["bytes"] = len(encoded)
        if len(encoded) <= LOG_INLINE_MAX:
            record["line"] = line
    return record


def read_log(path: str | Path) -> list[dict]:
    """Every frame the guardian took responsibility for, in order, each with its
    outcome: `status` is `written`, `failed`, or `pending` (an intent whose write
    never finished: the provider may have read part of it). A torn last line was
    never followed by a pipe write and is ignored."""
    frames: list[dict] = []
    try:
        with open_state(path) as stream:              # the daemon reads it: never a FIFO's open()
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    break
                if not isinstance(record, dict):
                    break
                kind = record.get("kind")
                if kind == "intent" and record.get("seq") == len(frames) + 1:
                    frames.append({**record, "status": "pending"})
                elif kind in ("written", "failed") and frames and record.get("seq") == frames[-1]["seq"]:
                    frames[-1]["status"] = kind
                    if kind == "failed":
                        frames[-1]["errno"] = record.get("errno")
                else:
                    break
    except FileNotFoundError:
        pass
    return frames


def _peer_pid(conn: socket.socket) -> int | None:
    """The connecting process's pid (macOS LOCAL_PEERPID; Linux SO_PEERCRED)."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            import struct
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", creds)[0]
        return conn.getsockopt(0, 2)          # SOL_LOCAL, LOCAL_PEERPID
    except OSError:
        return None


def daemon_peer_check(lock_path: str | Path):
    """Accept only the daemon `daemon.lock` names, re-read at every connection so a
    restarted daemon is accepted and its predecessor is not."""
    from . import procs

    def allowed(pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            lock = json.loads(Path(lock_path).read_text())
        except (OSError, ValueError):
            return False
        if lock.get("pid") != pid:
            return False
        try:
            return bool(procs.same_process(pid, lock.get("boot_id"), lock.get("proc_start")))
        except Exception:
            return False
    return allowed


# The kernel exit peek (C-26.4, #271 r4). The guardian's blocking `child.wait()` holds
# Popen's `_waitpid_lock` across `waitpid`, and while it is held `poll()` answers None
# without asking the kernel, even for a child that has exited. `waitid` with WNOWAIT asks
# the kernel and leaves the child waitable, so the guardian's wait stays its only reaper
# and records its real status. CPython 3.12 leaves `os.waitid` out of its macOS build;
# there libc's own `waitid`, the same system call, is called through ctypes.
_PEEK_FLAGS = os.WEXITED | os.WNOHANG | os.WNOWAIT
# Fixed text but for the errno's name.
PEEK_FAILURE_LOG_LINE = "relay exit peek: waitid failed ({errno}); the child's exit reads as unproved (logged once)"


class _Exited(NamedTuple):
    si_pid: int
    si_status: int                  # the exit code, or the signal that ended the child


class _SigInfo(ctypes.Structure):
    """Darwin's `siginfo_t` (sys/signal.h): 104 bytes on arm64 and x86_64."""
    _fields_ = [("si_signo", ctypes.c_int), ("si_errno", ctypes.c_int), ("si_code", ctypes.c_int),
                ("si_pid", ctypes.c_int), ("si_uid", ctypes.c_uint32), ("si_status", ctypes.c_int),
                ("si_addr", ctypes.c_void_p), ("si_value", ctypes.c_void_p), ("si_band", ctypes.c_long),
                ("_pad", ctypes.c_ulong * 7)]


def _bind_libc_waitid():
    """libc's `waitid(idtype_t, id_t, siginfo_t *, int)`, or None off macOS (the
    structure above is Darwin's) or where it cannot be bound."""
    if sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).waitid
    except (OSError, AttributeError):
        return None
    function.restype = ctypes.c_int
    function.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.POINTER(_SigInfo), ctypes.c_int]
    return function


_libc_waitid = _bind_libc_waitid()


def _waitid_os(pid: int) -> _Exited | None:
    """The peek through `os.waitid`: the child's exit, or None while it runs."""
    found = os.waitid(os.P_PID, pid, _PEEK_FLAGS)
    return None if found is None else _Exited(found.si_pid, found.si_status)


def _waitid_libc(pid: int) -> _Exited | None:
    """The same peek through libc's `waitid`. With WNOHANG a running child leaves the
    zeroed siginfo's pid 0. A failure raises OSError (ECHILD as ChildProcessError)."""
    if _libc_waitid is None:
        raise OSError(errno.ENOSYS, "libc waitid is not bound on this platform")
    info = _SigInfo()
    if _libc_waitid(os.P_PID, pid, ctypes.byref(info), _PEEK_FLAGS) != 0:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure))
    return _Exited(info.si_pid, info.si_status) if info.si_pid else None


#: The binding `child_exited` peeks through, chosen once, at import.
EXIT_PEEK = "os.waitid" if hasattr(os, "waitid") else "libc waitid"
_exit_peek = _waitid_os if hasattr(os, "waitid") else _waitid_libc
_peek_failure_logged = False


def child_exited(child) -> bool:
    """Whether the guardian's child has exited (C-26.4): Popen's own evidence, or else
    the kernel's, read without reaping. While the child runs no other process can hold
    its pid, and it stays this process's child until reaped, so a peek that finds an
    exit at that pid, or ECHILD (the guardian's wait reaped it), proves this child's
    exit. Any other failure proves nothing and is logged once. A child without a real
    pid (the tests' fakes) is not peeked."""
    global _peek_failure_logged
    if getattr(child, "returncode", None) is not None or child.poll() is not None:
        return True
    pid = getattr(child, "pid", None)
    if type(pid) is not int or pid <= 0:
        return False
    try:
        return _exit_peek(pid) is not None
    except ChildProcessError:
        return True
    except OSError as exc:
        if not _peek_failure_logged:
            _peek_failure_logged = True
            _stderr_line(PEEK_FAILURE_LOG_LINE.format(errno=errno.errorcode.get(exc.errno, exc.errno)))
        return False


class RelayServer:
    """The guardian side. One connection at a time; frames applied in order."""

    def __init__(self, path: str | Path, log_path: str | Path, *, allowed_peer=None):
        self.path = Path(path)
        self.log_path = Path(log_path)
        self._allowed_peer = allowed_peer
        self._records = read_log(self.log_path)
        self._closed = any(r.get("op") == "close" or (r["status"] != "written" and r.get("op") != "signal")
                           for r in self._records)
        self._pipe: int | None = None
        self._child = None
        # Taken in this order. Stop's cleanup takes the pipe lock alone, never the
        # journal's, so no record's write or fsync can hold it (#236 review r1).
        self._exchange_lock = threading.Lock()   # read through reply, including close
        self._journal_lock = threading.Lock()    # a whole frame: its records in order, _records, _closed
        self._pipe_lock = threading.Lock()       # the pipe's descriptor: a write or the close, never a record
        self._stop = threading.Event()
        self._cut = threading.Event()            # stop's bound passed: a stdin write gives up
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._conn: socket.socket | None = None

    @property
    def last_applied(self) -> int:
        return len(self._records)

    def bind(self) -> None:
        """Bind before `start.json` is published, so the daemon can connect as soon
        as it sees the receipt; connections wait in the backlog until `serve`."""
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(errno.EPERM, f"relay directory is not a private directory: {directory}")
        os.chmod(directory, 0o700)
        try:
            existing = os.lstat(self.path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise OSError(errno.EEXIST, f"refusing to replace a non-socket at {self.path}")
            os.unlink(self.path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            server.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        server.listen(4)
        server.settimeout(0.25)
        self._server = server

    def serve(self, pipe_fd: int, child=None) -> None:
        """Start relaying into `pipe_fd` (the child's stdin) on a daemon thread;
        `child` is the guardian's own `Popen`, the only process `signal` reaches.
        The pipe's write end, the guardian's alone, becomes non-blocking, so a
        write the child does not read waits where `stop` can cut it."""
        self._pipe = pipe_fd
        self._child = child
        if self._closed:
            self._close_pipe()
        else:
            os.set_blocking(pipe_fd, False)
        self._thread = threading.Thread(target=self._accept_loop, name="subfleet-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Wake idle reads, then finish an exchange already read, within a bound.

        Closing stdin can make the child exit before its acknowledgement is
        sent. Keep the write half alive for that reply. A connection published
        after this wakeup sees _stop before starting its first read. An idle
        client never delays exit. An exchange already read has REPLY_TIMEOUT_S,
        the bound its reply has on its own, to finish; one still going then is
        cut (`_cut_exchange`) and stop goes on. A reader that holds the child's
        stdin open without reading (a process the child started, after the child
        exited) held that wait for as long as it lived (#230 review r1).

        What follows is bounded too (#236 review r1): the relay thread gets
        JOIN_TIMEOUT_S to end, and the cleanup (`_clean_up`) CLEANUP_TIMEOUT_S on a
        thread of its own, because the pipe's close waits for a write under way
        and the unlink and the cut's line can wait on the disk and on stderr.
        So stop returns within REPLY_TIMEOUT_S + JOIN_TIMEOUT_S + CLEANUP_TIMEOUT_S
        whatever the child, a stdin reader or the journal's disk does. A cleanup
        still running then finishes on its own; what it has not closed when the
        guardian exits, the exit closes. With no thread to be had for the cleanup,
        stop runs only its steps that cannot wait (`_clean_up_inline`) and returns
        within the same bound (#236 review r2).
        """
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        # The reader owns this across readline/apply/reply. SHUT_RD wakes an
        # idle read; an exchange already read keeps its write half until done
        # or until the bound passes.
        if self._exchange_lock.acquire(timeout=REPLY_TIMEOUT_S):
            self._exchange_lock.release()
        else:
            self._cut_exchange()
        if self._thread is not None:
            self._thread.join(timeout=JOIN_TIMEOUT_S)
        cleanup = self._cleanup_thread
        if cleanup is None:                 # a second stop waits for the first one's cleanup
            cleanup = threading.Thread(target=self._clean_up, name="subfleet-relay-stop", daemon=True)
            try:
                cleanup.start()
            except RuntimeError:            # no thread to be had: only what cannot wait, here
                self._clean_up_inline()
                return
            self._cleanup_thread = cleanup
        cleanup.join(timeout=CLEANUP_TIMEOUT_S)

    def _cut_exchange(self) -> None:
        """End the exchange `stop` waited for: a stdin write waiting for room gives
        up and is logged `failed` (ECANCELED), which releases the pipe lock that
        `_close_pipe` takes; the connection is shut down both ways, so a reply or
        a read under way ends and the client reads end of stream, no answer.
        A shutdown does not wait for a send or receive blocked on the connection
        (measured, #236 review r1); the cut's line is the cleanup's, where stop's
        wait for its write is bounded."""
        conn = self._conn
        if conn is not None:
            # Each half on its own: once `stop` has shut the read half, macOS
            # refuses SHUT_RDWR (ENOTCONN) and leaves the write half open.
            for how in (socket.SHUT_RD, socket.SHUT_WR):
                try:
                    conn.shutdown(how)
                except OSError:
                    pass
        self._cut.set()                     # after the shutdown, so the write's answer reaches no one

    def _clean_up(self) -> None:
        """Stop's last steps, on the thread `stop` waits for within its bound:
        close the listening socket, close the pipe (waiting for the pipe lock, so
        a write the bound could not wait for lets go first, and the descriptor
        closes as soon as it does), write a cut's line to stderr, unlink the
        socket file. Each step is taken whether or not the one before failed; the
        one that touches the disk is last. This thread takes no lock but the pipe
        lock, which nothing at interpreter shutdown takes, so a step still held
        when the guardian exits cannot hold the exit (#236 review r2)."""
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        self._close_pipe()
        if self._cut.is_set():
            _stderr_line(CUT_LOG_LINE)
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _clean_up_inline(self) -> None:
        """Stop's cleanup when its thread cannot start, on `stop`'s own thread, so
        only the steps measured not to wait (#236 review r2): close the listening
        socket (9 to 18 µs with a connection in its backlog or a thread in
        `accept`), and close the pipe if no write holds the pipe lock now (under
        7 µs with a reader blocked in `read` or a full pipe). The rest is left. A
        pipe a write still holds closes at the guardian's exit. The cut line, whose
        write can wait on stderr, is not written; a cut write's durable record is
        its `failed` entry. The socket file, whose unlink can wait on the disk, is
        left to the daemon's finalization of the attempt (`release_socket`), or to
        the next `bind` at this path. Nothing listens on it, so a connect is refused,
        as for a guardian gone. A later `stop` tries the thread again."""
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        self._close_pipe(wait=False)

    def _close_pipe(self, *, wait: bool = True) -> None:
        """Close the pipe's write end under the pipe lock; without `wait`, only if no
        write holds that lock now."""
        if not self._pipe_lock.acquire(blocking=wait):
            return
        try:
            if self._pipe is not None:
                try:
                    os.close(self._pipe)
                except OSError:
                    pass
                self._pipe = None
        finally:
            self._pipe_lock.release()

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            with conn:
                if self._allowed_peer is not None and not self._allowed_peer(_peer_pid(conn)):
                    try:
                        self._reply(conn, {"ok": False, "error": "peer-refused"})
                    except OSError:
                        pass
                    continue
                conn.settimeout(None)
                self._conn = conn
                try:
                    self._serve_connection(conn)
                except OSError:
                    continue
                finally:
                    self._conn = None

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn.makefile("rb") as reader:
            while not self._stop.is_set():
                with self._exchange_lock:
                    if self._stop.is_set():
                        return
                    raw = reader.readline(FRAME_MAX + 1)
                    if not raw:
                        return
                    if not raw.endswith(b"\n") or len(raw) > FRAME_MAX:
                        self._reply(conn, {"ok": False, "error": "frame-too-large"})
                        return
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        self._reply(conn, {"ok": False, "error": "bad-json"})
                        continue
                    self._reply(conn, self.apply(frame))

    @staticmethod
    def _reply(conn: socket.socket, body: dict) -> None:
        previous_timeout = conn.gettimeout()
        try:
            conn.settimeout(REPLY_TIMEOUT_S)
            conn.sendall((json.dumps(body, separators=(",", ":")) + "\n").encode())
        finally:
            conn.settimeout(previous_timeout)

    def status(self) -> dict:
        """The relay's state (IR-27). Taken under the journal lock, which `apply`
        holds for a whole frame, so a frame being written when the request arrived
        has finished and been logged before the answer is built."""
        with self._journal_lock:
            child = self._child
            return {"ok": True, "status": {
                **advertisement(), "applied": len(self._records), "closed": self._closed,
                "child": "none" if child is None else "exited" if child_exited(child) else "running"}}

    def _refusal(self, seq: int, error: str, **details) -> dict:
        # A broken/closed pipe alone does not prove exit: a live child can close
        # stdin too. Preserve the child's exit evidence in the reply, since
        # shutdown may close the socket before a follow-up status arrives.
        child = self._child
        exited = child is not None and child_exited(child)
        return {"seq": seq, "ok": False, "error": error, **details,
                **({"child_exited": True} if exited else {})}

    def apply(self, frame: dict) -> dict:
        """Apply one frame. Public so the unit tests can drive it without a socket."""
        if isinstance(frame, dict) and frame.get("op") == "status" and "seq" not in frame:
            return self.status()
        if not isinstance(frame, dict) or type(frame.get("seq")) is not int or frame["seq"] < 1:
            return {"ok": False, "error": "bad-frame"}
        seq, op, line = frame["seq"], frame.get("op"), frame.get("line")
        if op not in ("write", "close", "signal") or (op == "write" and not isinstance(line, str)):
            return {"seq": seq, "ok": False, "error": "bad-frame"}
        if op == "signal" and frame.get("sig") not in SIGNALS:
            return {"seq": seq, "ok": False, "error": "bad-signal"}
        if op == "write" and ("\n" in line or "\r" in line):
            return {"seq": seq, "ok": False, "error": "newline-in-line"}
        if frame.get("sha256") != frame_sha256(op, line, frame.get("sig")):
            return {"seq": seq, "ok": False, "error": "bad-hash"}
        # The journal lock is held for the whole frame, so its records stay in
        # order (an intent, then its outcome, frame by frame); the pipe lock only
        # around the pipe itself, so no record's write or fsync holds it.
        with self._journal_lock:
            if seq <= len(self._records):
                logged = self._records[seq - 1]
                if logged["sha256"] != frame["sha256"] or logged["op"] != op:
                    return {"seq": seq, "ok": False, "error": "conflict"}
                if logged["status"] != "written":
                    return self._refusal(seq, "no-child" if op == "signal" else "failed")
                return {"seq": seq, "ok": True, "dup": True}
            if seq != len(self._records) + 1:
                return {"seq": seq, "ok": False, "error": "gap", "last": len(self._records)}
            if op == "signal":
                # A signal is not stdin: it is allowed after stdin closed, while
                # the child lives. Logged like any frame, so it is sent once.
                intent = _intent(frame)
                self._append(intent)
                record = {**intent, "status": "pending"}
                self._records.append(record)
                child = self._child
                delivered = False
                if child is not None and not child_exited(child):
                    try:
                        child.send_signal(SIGNALS[frame["sig"]])
                        delivered = True
                    except OSError:
                        pass
                record["status"] = "written" if delivered else "failed"
                self._append({"kind": "written" if delivered else "failed", "seq": seq})
                return {"seq": seq, "ok": delivered, **({} if delivered else {"error": "no-child"})}
            if self._closed or self._pipe is None:
                return self._refusal(seq, "closed")
            intent = _intent(frame)
            self._append(intent)
            record = {**intent, "status": "pending"}
            self._records.append(record)
            if op == "close":
                self._close_pipe()          # or stop's cleanup closed it first: stdin is closed either way
                self._closed = True
                self._append({"kind": "written", "seq": seq})
                record["status"] = "written"
                return {"seq": seq, "ok": True}
            data = (line + "\n").encode("utf-8")
            try:
                with self._pipe_lock:
                    self._write_line(data)
            except OSError as exc:
                # The provider may have read part of this frame. Nothing more is
                # written; the turn's delivery is for reconciliation (C-24.6).
                # ECANCELED: `stop` cut the write at its bound, or its cleanup closed the pipe first.
                self._closed = True
                record["status"] = "failed"
                try:
                    self._append({"kind": "failed", "seq": seq, "errno": exc.errno})
                except OSError:
                    pass
                return self._refusal(seq, "failed", errno=exc.errno)
            self._append({"kind": "written", "seq": seq})
            record["status"] = "written"
            return {"seq": seq, "ok": True}

    def _write_line(self, data: bytes) -> None:
        """Write all of `data` to the child's stdin; the caller holds the pipe
        lock. On the non-blocking pipe `serve` sets, a full pipe waits for room
        here, looking every WRITE_POLL_S for `stop`'s cut, which raises
        ECANCELED, as does a pipe stop's cleanup has already closed (it may close
        it between the frame's intent and this write); a blocking pipe (the unit
        tests' `apply` without `serve`) just blocks in the write."""
        view = memoryview(data)
        poller = None
        while view:
            if self._cut.is_set() or self._pipe is None:
                raise OSError(errno.ECANCELED, "relay stopped before the line was written")
            try:
                view = view[os.write(self._pipe, view):]
            except BlockingIOError:
                if poller is None:
                    poller = select.poll()
                    poller.register(self._pipe, select.POLLOUT)
                poller.poll(WRITE_POLL_S * 1000)

    def _append(self, record: dict) -> None:
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)


@dataclass
class Ack:
    seq: int
    ok: bool
    dup: bool = False
    error: str | None = None
    child_exited: bool = False


class RelayError(Exception):
    """The relay could not be reached or did not answer; the frame's fate is unknown
    until the same number is sent again."""


class FrameTooLarge(Exception):
    """The frame exceeds the relay's advertised cap and was not sent (IR-27): its
    fate is known, nothing reached the relay."""

    def __init__(self, size: int, cap: int):
        super().__init__(f"frame of {size} bytes exceeds the relay cap of {cap}")
        self.size = size
        self.cap = cap


class RelayClient:
    """The daemon side: one connection, reopened on demand; frames resent by number."""

    def __init__(self, path: str | Path, *, timeout_s: float = ACK_TIMEOUT_S):
        self.path = str(path)
        self.timeout_s = timeout_s
        self.frame_max = FRAME_MAX       # replaced by the relay's own advertisement
        self._sock: socket.socket | None = None
        self._reader = None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._reader = None

    def _connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        try:
            sock.connect(self.path)
        except OSError as exc:
            sock.close()
            raise RelayError(f"relay unavailable: {exc}") from exc
        self._sock = sock
        self._reader = sock.makefile("rb")

    def _exchange(self, payload: bytes, *, before_send: Callable[[], None] | None = None) -> dict:
        try:
            self._connect()
            assert self._sock is not None and self._reader is not None
            if before_send is not None:
                before_send()                  # after connection setup, before transmission
            self._sock.sendall(payload)
            raw = self._reader.readline(REPLY_MAX)
        except (OSError, RelayError) as exc:
            self.close()
            raise RelayError(f"relay send failed: {exc}") from exc
        if not raw:
            self.close()
            raise RelayError("relay closed the connection before acknowledging")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            self.close()
            raise RelayError(f"relay answered with a malformed line: {exc}") from exc
        if not isinstance(body, dict):
            self.close()
            raise RelayError("relay answered with something other than an object")
        return body

    def status(self) -> dict | None:
        """The relay's state (IR-27), or None from a relay older than version 2,
        which refuses a request without a number as `bad-frame`. Adopts the
        advertised frame cap."""
        body = self._exchange(b'{"op":"status"}\n')
        if not body.get("ok"):
            if body.get("error") == "bad-frame":
                return None
            raise RelayError(f"relay refused status: {body.get('error')}")
        status = body.get("status")
        if not isinstance(status, dict) or type(status.get("applied")) is not int:
            raise RelayError("relay status is malformed")
        cap = status.get("frame_max")
        if type(cap) is int and cap > 0:
            self.frame_max = cap
        return status

    def send(self, seq: int, op: str, *, line: str | None = None, tag: str | None = None,
             sig: str | None = None, before_send: Callable[[], None] | None = None) -> Ack:
        """Send one frame, sampling its optional clock immediately before the socket write."""
        frame: dict = {"seq": seq, "op": op, "tag": tag, "sha256": frame_sha256(op, line, sig)}
        if line is not None:
            frame["line"] = line
        if sig is not None:
            frame["sig"] = sig
        payload = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > self.frame_max:
            # The relay would refuse it and end the connection; it is never sent.
            raise FrameTooLarge(len(payload), self.frame_max)
        body = self._exchange(payload, before_send=before_send)
        return Ack(seq=body.get("seq", seq), ok=bool(body.get("ok")), dup=bool(body.get("dup")),
                   error=body.get("error"), child_exited=body.get("child_exited") is True)
