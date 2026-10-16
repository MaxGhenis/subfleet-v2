"""Run one foreground verification with a wall bound and task-local temp state."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def reap(child):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
    child.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1480)
    parser.add_argument("--log", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    assert 0 < args.seconds <= 1480  # Leave time to terminate and reap below 25 minutes.
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    task_temp = Path((ROOT / ".fix9-tempdir").read_text().strip())
    assert task_temp.is_dir() and "tmp" not in task_temp.parts
    log = ROOT / args.log
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMPDIR": str(task_temp), "UV_CACHE_DIR": str(task_temp / "uv-cache"),
           "PYTHONDONTWRITEBYTECODE": "1", "HYPOTHESIS_STORAGE_DIRECTORY": str(task_temp / "hypothesis"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    started = time.monotonic()
    with log.open("w") as output:
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = child.wait(timeout=args.seconds)
        except subprocess.TimeoutExpired:
            reap(child)
            output.write(f"\nWALL BOUND {args.seconds}s: owned process group reaped\n")
            code = 124
        except BaseException:
            # Target only this verification's process group, then reap it.
            reap(child)
            raise
    print(f"exit={code} elapsed={time.monotonic()-started:.1f}s log={args.log}", flush=True)
    return code


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"verification interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
