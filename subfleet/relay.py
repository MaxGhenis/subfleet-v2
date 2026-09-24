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

import errno
import hashlib
import json
import os
import socket
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

FRAME_MAX = 64 * 1024 * 1024        # one image in base64 plus JSON, with margin
LOG_INLINE_MAX = 64 * 1024          # frames logged verbatim up to this size
ACK_TIMEOUT_S = 60.0
SOCKET_DIR_NAME = "run"


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
        with open(path, "rb") as stream:
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
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
        `child` is the guardian's own `Popen`, the only process `signal` reaches."""
        self._pipe = pipe_fd
        self._child = child
        if self._closed:
            self._close_pipe()
        self._thread = threading.Thread(target=self._accept_loop, name="subfleet-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """End relaying at once: the child has exited and its receipt must not wait
        for an idle daemon connection."""
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._server is not None:
            self._server.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._close_pipe()

    def _close_pipe(self) -> None:
        with self._lock:
            if self._pipe is not None:
                try:
                    os.close(self._pipe)
                except OSError:
                    pass
                self._pipe = None

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
        reader = conn.makefile("rb")
        while not self._stop.is_set():
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
        conn.sendall((json.dumps(body, separators=(",", ":")) + "\n").encode())

    def apply(self, frame: dict) -> dict:
        """Apply one frame. Public so the unit tests can drive it without a socket."""
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
        with self._lock:
            if seq <= len(self._records):
                logged = self._records[seq - 1]
                if logged["sha256"] != frame["sha256"] or logged["op"] != op:
                    return {"seq": seq, "ok": False, "error": "conflict"}
                if logged["status"] != "written":
                    return {"seq": seq, "ok": False, "error": "no-child" if op == "signal" else "failed"}
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
                if child is not None and child.poll() is None:
                    try:
                        child.send_signal(SIGNALS[frame["sig"]])
                        delivered = True
                    except OSError:
                        pass
                record["status"] = "written" if delivered else "failed"
                self._append({"kind": "written" if delivered else "failed", "seq": seq})
                return {"seq": seq, "ok": delivered, **({} if delivered else {"error": "no-child"})}
            if self._closed or self._pipe is None:
                return {"seq": seq, "ok": False, "error": "closed"}
            intent = _intent(frame)
            self._append(intent)
            record = {**intent, "status": "pending"}
            self._records.append(record)
            if op == "close":
                try:
                    os.close(self._pipe)
                except OSError:
                    pass
                self._pipe = None
                self._closed = True
                self._append({"kind": "written", "seq": seq})
                record["status"] = "written"
                return {"seq": seq, "ok": True}
            data = (line + "\n").encode("utf-8")
            try:
                view = memoryview(data)
                while view:
                    written = os.write(self._pipe, view)
                    view = view[written:]
            except OSError as exc:
                # The provider may have read part of this frame. Nothing more is
                # written; the turn's delivery is for reconciliation (C-24.6).
                self._closed = True
                record["status"] = "failed"
                try:
                    self._append({"kind": "failed", "seq": seq, "errno": exc.errno})
                except OSError:
                    pass
                return {"seq": seq, "ok": False, "error": "failed", "errno": exc.errno}
            self._append({"kind": "written", "seq": seq})
            record["status"] = "written"
            return {"seq": seq, "ok": True}

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


class RelayError(Exception):
    """The relay could not be reached or did not answer; the frame's fate is unknown
    until the same number is sent again."""


class RelayClient:
    """The daemon side: one connection, reopened on demand; frames resent by number."""

    def __init__(self, path: str | Path, *, timeout_s: float = ACK_TIMEOUT_S):
        self.path = str(path)
        self.timeout_s = timeout_s
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

    def send(self, seq: int, op: str, *, line: str | None = None, tag: str | None = None,
             sig: str | None = None) -> Ack:
        frame: dict = {"seq": seq, "op": op, "tag": tag, "sha256": frame_sha256(op, line, sig)}
        if line is not None:
            frame["line"] = line
        if sig is not None:
            frame["sig"] = sig
        payload = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self._connect()
            assert self._sock is not None and self._reader is not None
            self._sock.sendall(payload)
            raw = self._reader.readline(1 << 16)
        except (OSError, RelayError) as exc:
            self.close()
            raise RelayError(f"relay send failed: {exc}") from exc
        if not raw:
            self.close()
            raise RelayError("relay closed the connection before acknowledging")
        body = json.loads(raw)
        return Ack(seq=body.get("seq", seq), ok=bool(body.get("ok")), dup=bool(body.get("dup")),
                   error=body.get("error"))
