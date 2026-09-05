"""One detached provider supervisor; durable files only, never database rows."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .procs import boot_id, proc_start


def atomic_publish(path: str | Path, data: bytes) -> None:
    """Publish a private file by temp, fsync, rename, directory fsync (C-8.1)."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _receipt(path: Path, value: dict) -> None:
    atomic_publish(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _output(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb")


def run_guardian(argv: list[str], *, attempt_dir: Path, cwd: str,
                 stdin_path: str | None, stdout_path: str, stderr_path: str,
                 start_delay_s: float = 0, launch_fd: int | None = None) -> int:
    """Run argv in a new session, writing start before spawn and exit after wait."""
    os.umask(0o077)
    os.setsid()
    # Keep the leader alive while a child ignores TERM, so the daemon can
    # recheck the recorded leader before escalating to SIGKILL (C-5.4/5.6).
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    if launch_fd is not None:
        # The daemon releases this gate only after committing the guardian
        # identity and starting state. EOF after a daemon crash cannot spawn
        # an unrecorded provider from a reserved attempt (C-4.2).
        try:
            released = os.read(launch_fd, 1) == b"1"
        finally:
            os.close(launch_fd)
        if not released:
            return 127
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if start_delay_s:
        time.sleep(max(0, start_delay_s))
    started = time.monotonic()
    start = {
        "guardian_pid": os.getpid(), "pgid": os.getpgrp(), "boot_id": boot_id(),
        "proc_start": proc_start(os.getpid()), "started_at": _utc(),
    }
    if not start["proc_start"]:
        raise RuntimeError("guardian could not establish its process start identity")
    _receipt(attempt_dir / "start.json", start)
    child = None
    spawn_error = None
    try:
        with _output(Path(stdout_path)) as stdout, _output(Path(stderr_path)) as stderr:
            with open(stdin_path or os.devnull, "rb") as stdin:
                # Timer observation begins at the provider launch boundary, after
                # credential lookup and worker queueing (C-23.19).
                if os.environ.get("SUBFLEET_PROBE"):
                    _receipt(attempt_dir / "request.json", {"requested_at": _utc()})
                child = subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr)
                rc = child.wait()
    except OSError as exc:
        rc = 127
        # OSError contains the executable/path and errno, never child env.
        spawn_error = str(exc)
    exit_receipt = {
        "rc": rc, "signal": -rc if rc < 0 else None, "finished_at": _utc(),
        "wall_s": round(time.monotonic() - started, 6),
        "child_pid": child.pid if child else None,
    }
    if spawn_error:
        exit_receipt["spawn_error"] = spawn_error
    _receipt(attempt_dir / "exit.json", exit_receipt)
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subfleet-guardian")
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--stdin-path", "--stdin")
    parser.add_argument("--stdout-path", "--stdout", required=True)
    parser.add_argument("--stderr-path", "--stderr", required=True)
    parser.add_argument("--start-delay-s", type=float, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--launch-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a provider command is required after --")
    rc = run_guardian(command, attempt_dir=args.attempt_dir, cwd=args.cwd,
                      stdin_path=args.stdin_path, stdout_path=args.stdout_path,
                      stderr_path=args.stderr_path, start_delay_s=args.start_delay_s,
                      launch_fd=args.launch_fd)
    if rc < 0:
        # Preserve subprocess's signal returncode as well as the raw receipt.
        if -rc not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(-rc, signal.SIG_DFL)
        os.kill(os.getpid(), -rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
