"""The guardian's control relay: numbered stdin frames, applied once (C-26.4).

A conversation turn's provider reads its stdin from a pipe the guardian owns.
The daemon never holds that pipe: it connects to a private Unix socket the
guardian listens on and sends numbered frames. The guardian appends each new
frame to `stdin.jsonl` with fsync, then writes it to the pipe, then
acknowledges by number. A frame whose number was already applied is
acknowledged again without being written, and a gap is refused, so a daemon
that crashes between sending and hearing back can resend safely and the
provider never reads the same input twice.

The guardian does not interpret provider messages. A frame is either
`{"seq": n, "op": "write", "line": "<one line>", "tag": "<daemon label>"}` or
`{"seq": n, "op": "close", "tag": ...}`. `tag` is the daemon's own label for
the frame (for example `user-message` or `approval:ap-…`), logged so a
rebuilt driver knows what it already sent; the guardian only stores it.

Large lines (an image attachment in base64) are logged by digest, not
content: the daemon keeps the original, and replay needs to know only that the
frame was applied.
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


def _log_record(frame: dict) -> dict:
    line = frame.get("line")
    record = {"seq": frame["seq"], "op": frame["op"], "tag": frame.get("tag")}
    if isinstance(line, str):
        encoded = line.encode("utf-8")
        if len(encoded) <= LOG_INLINE_MAX:
            record["line"] = line
        else:
            record["sha256"] = hashlib.sha256(encoded).hexdigest()
            record["bytes"] = len(encoded)
    return record


def read_log(path: str | Path) -> list[dict]:
    """Every applied frame, in order. A torn last line (crash mid-append before
    fsync returned) was never written to the pipe, so it is ignored."""
    records: list[dict] = []
    try:
        with open(path, "rb") as stream:
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    break
                if not isinstance(record, dict) or record.get("seq") != len(records) + 1:
                    break
                records.append(record)
    except FileNotFoundError:
        pass
    return records


class RelayServer:
    """The guardian side. One connection at a time; frames applied in order."""

    def __init__(self, path: str | Path, log_path: str | Path):
        self.path = Path(path)
        self.log_path = Path(log_path)
        self._records = read_log(self.log_path)
        self._closed = any(r.get("op") == "close" for r in self._records)
        self._pipe: int | None = None
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

    def serve(self, pipe_fd: int) -> None:
        """Start relaying into `pipe_fd` (the child's stdin) on a daemon thread."""
        self._pipe = pipe_fd
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
        seq, op = frame.get("seq") if isinstance(frame, dict) else None, None
        if not isinstance(frame, dict) or type(seq) is not int or seq < 1:
            return {"ok": False, "error": "bad-frame"}
        op = frame.get("op")
        if op not in ("write", "close") or (op == "write" and not isinstance(frame.get("line"), str)):
            return {"seq": seq, "ok": False, "error": "bad-frame"}
        if op == "write" and "\n" in frame["line"]:
            return {"seq": seq, "ok": False, "error": "newline-in-line"}
        with self._lock:
            if seq <= len(self._records):
                return {"seq": seq, "ok": True, "dup": True}
            if seq != len(self._records) + 1:
                return {"seq": seq, "ok": False, "error": "gap", "last": len(self._records)}
            if self._closed or self._pipe is None:
                return {"seq": seq, "ok": False, "error": "closed"}
            record = _log_record(frame)
            self._append(record)
            self._records.append(record)
            if op == "close":
                self._closed = True
                try:
                    os.close(self._pipe)
                except OSError:
                    pass
                self._pipe = None
                return {"seq": seq, "ok": True}
            data = (frame["line"] + "\n").encode("utf-8")
            try:
                view = memoryview(data)
                while view:
                    written = os.write(self._pipe, view)
                    view = view[written:]
            except OSError as exc:
                # The frame is logged and the child is gone or closed its stdin:
                # nothing more can be delivered, and the log says what was tried.
                self._closed = True
                return {"seq": seq, "ok": False, "error": "closed", "errno": exc.errno}
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

    def send(self, seq: int, op: str, *, line: str | None = None, tag: str | None = None) -> Ack:
        frame: dict = {"seq": seq, "op": op, "tag": tag}
        if line is not None:
            frame["line"] = line
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
