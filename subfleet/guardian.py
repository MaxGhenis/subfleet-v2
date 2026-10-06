"""One detached provider supervisor; durable files only, never database rows."""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .procs import InspectionError, boot_id, pipe_above_stdio, proc_start

# C-5.1: agent work runs at the `utility` QoS whatever the daemon's own scheduling, so a
# daemon at the default QoS (launchd `ProcessType` `Interactive`) never lifts it over the
# operator's apps. A QoS clamp is the one setting every process the provider starts
# inherits and none can raise: measured on 2026-09-27, a thread's own QoS
# (`pthread_set_qos_class_self_np`) and `nice` reach no child, fork or posix_spawn
# (docs/reports/2026-09-27-daemon-qos.md). taskpolicy(8) sets the clamp and execs the
# provider in its own place, so its pid, group, environment and argv are the provider's.
TASKPOLICY = "/usr/sbin/taskpolicy"
PROVIDER_QOS = "utility"
PROVIDER_QOS_ENV = "SUBFLEET_PROVIDER_QOS"    # `inherit`: the provider runs at the guardian's QoS
# taskpolicy exits 66 (EX_NOINPUT) with this line when its posix_spawn of the provider fails.
CLAMP_SPAWN_FAILED_RC = 66
CLAMP_SPAWN_FAILED = b"taskpolicy: posix_spawn: "
_ERRNO_BY_TEXT = {os.strerror(number): number for number in errno.errorcode}


def provider_qos() -> str | None:
    """The QoS clamp the provider starts under, or None when it inherits the guardian's.

    Only `inherit` opts out. A host without taskpolicy(8) inherits too: that is not macOS,
    where it ships in the base system."""
    if os.environ.get(PROVIDER_QOS_ENV, PROVIDER_QOS) == "inherit" or not os.access(TASKPOLICY, os.X_OK):
        return None
    return PROVIDER_QOS


def provider_argv(argv: list[str], qos: str | None) -> list[str]:
    """argv as the guardian spawns it: under `taskpolicy -c <qos>` when clamped."""
    return [TASKPOLICY, "-c", qos, *argv] if qos else list(argv)


def clamp_spawn_error(rc: int, stderr_path: str, name: str) -> str | None:
    """C-5.2's spawn_error when taskpolicy could not spawn the provider, else None.

    Popen's own failure reads `[Errno 2] No such file or directory: 'codex'`; under the
    clamp the same failure is taskpolicy's exit 66 with its reason as the first line of
    the provider's stderr, and it is given back in Popen's words."""
    if rc != CLAMP_SPAWN_FAILED_RC:
        return None
    try:
        with open(stderr_path, "rb") as stream:
            head = stream.read(256).split(b"\n", 1)[0]
    except OSError:
        return None
    if not head.startswith(CLAMP_SPAWN_FAILED):
        return None
    reason = head[len(CLAMP_SPAWN_FAILED):].decode("ascii", "replace").strip()
    number = _ERRNO_BY_TEXT.get(reason)
    return str(OSError(number, reason, name)) if number else f"{reason}: {name!r}"


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
                 start_delay_s: float = 0, launch_fd: int | None = None,
                 control_socket: str | None = None, relay_peer_lock: str | None = None) -> int:
    """Run argv in a new session, writing start before spawn and exit after wait.

    With `control_socket`, the child's stdin is a pipe fed only through the relay
    (C-26.4): the socket is bound before `start.json` names it, and frames are
    applied once each, in order, logged to `stdin.jsonl`."""
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
    relay = None
    if control_socket:
        if stdin_path:
            raise RuntimeError("a control socket and a stdin file are exclusive")
        from .relay import RelayServer, advertisement, daemon_peer_check
        relay = RelayServer(control_socket, attempt_dir / "stdin.jsonl",
                            allowed_peer=daemon_peer_check(relay_peer_lock) if relay_peer_lock else None)
        relay.bind()
        start["control_socket"] = str(control_socket)
        start["relay"] = advertisement()          # version and frame cap (review IR-27)
    _receipt(attempt_dir / "start.json", start)
    child = None
    spawn_error = None
    qos = provider_qos()
    command = provider_argv(argv, qos)

    def record_child():
        # The guardian has not reaped this child, so its PID cannot yet be
        # recycled. Publish before waiting or serving the long-lived relay.
        start["child_pid"] = child.pid
        try:
            child_start = proc_start(child.pid)
        except InspectionError:
            child_start = None
        if child_start:
            start["child_identity"] = {"pid": child.pid, "boot_id": start["boot_id"], "proc_start": child_start}
        _receipt(attempt_dir / "start.json", start)
        return bool(child_start)

    def spawn(stdin, stdout, stderr):
        nonlocal child, spawn_error
        gate_read, gate_write = pipe_above_stdio()
        try:
            error_read, error_write = pipe_above_stdio()
        except BaseException:
            os.close(gate_read)
            os.close(gate_write)
            raise
        try:
            child = subprocess.Popen(
                [sys.executable, "-I", "-S", str(Path(__file__).with_name("provider_gate.py")),
                 str(gate_read), str(error_write), *command],
                cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr,
                pass_fds=(gate_read, error_write))
            os.close(gate_read)
            gate_read = None
            os.close(error_write)
            error_write = None
            if record_child():
                # Both the file and its containing directory are fsynced before
                # the launcher can exec taskpolicy/provider in its recorded PID.
                os.write(gate_write, b"1")
            else:
                spawn_error = "provider launch identity unavailable; execution gate refused"
            return error_read
        except BaseException:
            os.close(error_read)
            # A publication failure must close the gate and reap the launcher.
            # EOF cannot execute provider code, including on a hard parent crash.
            os.close(gate_write)
            gate_write = None
            if child is not None:
                child.wait()
            raise
        finally:
            for fd in (gate_read, gate_write, error_write):
                if fd is not None:
                    os.close(fd)

    error_read = None

    try:
        with _output(Path(stdout_path)) as stdout, _output(Path(stderr_path)) as stderr:
            if relay is not None:
                read_end, write_end = os.pipe()
                try:
                    error_read = spawn(read_end, stdout, stderr)
                except BaseException:
                    os.close(write_end)
                    raise
                finally:
                    os.close(read_end)
                relay.serve(write_end, child=child)
                try:
                    rc = child.wait()
                finally:
                    relay.stop()
            else:
                with open(stdin_path or os.devnull, "rb") as stdin:
                    # Timer observation begins at the provider launch boundary, after
                    # credential lookup and worker queueing (C-23.19).
                    if os.environ.get("SUBFLEET_PROBE"):
                        _receipt(attempt_dir / "request.json", {"requested_at": _utc()})
                    error_read = spawn(stdin, stdout, stderr)
                    rc = child.wait()
    except OSError as exc:
        rc = 127
        # OSError contains the executable/path and errno, never child env.
        spawn_error = str(exc)
    finally:
        if error_read is not None:
            try:
                failure = os.read(error_read, 4096)
                if failure:
                    spawn_error = json.loads(failure)["spawn_error"]
            finally:
                os.close(error_read)
    if spawn_error:
        rc, child = 127, None
    if qos and child is not None and not spawn_error:
        spawn_error = clamp_spawn_error(rc, stderr_path, argv[0])
        if spawn_error:
            # No provider ran, so the receipt and the streams read as Popen's own failure
            # leaves them. taskpolicy's line lives on in spawn_error; left in the stderr
            # file, an adapter would read it as the provider's (Codex's transient pattern
            # matches "Resource temporarily unavailable", its limit one "Disc quota exceeded").
            rc, child = 127, None
            try:
                os.truncate(stderr_path, 0)
            except OSError:
                pass
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
    parser.add_argument("--control-socket")
    parser.add_argument("--relay-peer-lock", help="accept relay connections only from the daemon this lock names")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a provider command is required after --")
    rc = run_guardian(command, attempt_dir=args.attempt_dir, cwd=args.cwd,
                      stdin_path=args.stdin_path, stdout_path=args.stdout_path,
                      stderr_path=args.stderr_path, start_delay_s=args.start_delay_s,
                      launch_fd=args.launch_fd, control_socket=args.control_socket,
                      relay_peer_lock=args.relay_peer_lock)
    if rc < 0:
        # Preserve subprocess's signal returncode as well as the raw receipt.
        if -rc not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(-rc, signal.SIG_DFL)
        os.kill(os.getpid(), -rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
