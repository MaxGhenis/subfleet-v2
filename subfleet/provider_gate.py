"""Exec a provider in this PID only after its guardian durably records it.

Invoked by absolute path so the provider's cwd needs no package installation.
This launcher does no provider work before the one-byte acknowledgement.
"""

import json
import os
import sys


def main() -> int:
    launch_fd, error_fd = map(int, sys.argv[1:3])
    command = sys.argv[3:]
    os.set_inheritable(error_fd, False)
    try:
        released = os.read(launch_fd, 1) == b"1"
    finally:
        os.close(launch_fd)
    if not released:
        os.close(error_fd)
        return 127
    # Python ignores SIGPIPE; restore the disposition Popen's direct exec used.
    import signal
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        # Keep the diagnostic below the pipe's atomic-write capacity, including
        # JSON's worst-case unicode escaping, so wait cannot block on a writer.
        failure = str(OSError(exc.errno, exc.strerror, command[0]))
        os.write(error_fd, json.dumps({"spawn_error": failure[:400]}).encode())
        os.close(error_fd)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
