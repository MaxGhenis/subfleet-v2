"""Review r3 probe: does the production C-5.5 marker source see real detached processes?

Every process is started here with both Subfleet markers (and only this probe's
values) in its environment, in a new session, and stopped here by its exact
recorded PID. Prints one JSON line per case.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from subfleet import procs

BASE = Path(sys.argv[1]).resolve()
BASE.mkdir(parents=True, exist_ok=True)
ATTEMPT = "probe-r3/a1"
ROOT = str(BASE / "state-root")
PY = os.path.realpath(sys.executable)


def env():
    e = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    e.update(SUBFLEET_ATTEMPT=ATTEMPT, SUBFLEET_ROOT=ROOT)
    return e


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def gone(pid, wait=10.0):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def shape(pid):
    return subprocess.run(["/bin/ps", "-o", "ppid=,pgid=,stat=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.split()


def procargs_has_marker(pid):
    """What the kernel's argument area holds, read directly (own-user process)."""
    import ctypes
    import struct
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        return f"errno {ctypes.get_errno()}"
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return f"errno {ctypes.get_errno()}"
    return f"SUBFLEET_ATTEMPT={ATTEMPT}".encode() in buf.raw[:size.value]


def report(case, pid, **extra):
    time.sleep(1.0)
    assert alive(pid), f"{case}: {pid} is not running"
    c = procs.containment(None, None, None, ATTEMPT, root=ROOT)
    row = {"case": case, "pid": pid, "alive": alive(pid), "ppid/pgid/stat": shape(pid),
           "kernel_procargs_has_marker": procargs_has_marker(pid),
           "seen_by_census_marker_scan": pid in c.marker_pids,
           "census_verified_empty": c.verified_empty, "errors": list(c.errors), **extra}
    print(json.dumps(row), flush=True)


def simple(case, argv, cwd=None):
    proc = subprocess.Popen(argv, env=env(), start_new_session=True, cwd=cwd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        report(case, proc.pid, argv0=argv[0])
    finally:
        proc.kill()
        proc.wait(timeout=10)


def redis(case, *flags):
    d = BASE / case
    d.mkdir(exist_ok=True)
    pidfile = d / "r.pid"
    pidfile.unlink(missing_ok=True)
    argv = ["/opt/homebrew/bin/redis-server", "--port", "0", "--unixsocket", "r.sock",
            "--dir", str(d), "--save", "", "--appendonly", "no", "--logfile", str(d / "r.log"),
            "--pidfile", str(pidfile), *flags]
    daemonize = "--daemonize" in flags
    if daemonize:
        subprocess.run(argv, env=env(), cwd=d, check=True)
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        pid, proc = int(pidfile.read_text()), None
    else:
        proc = subprocess.Popen(argv, env=env(), cwd=d, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid = proc.pid
    try:
        report(case, pid, argv0=argv[0], flags=list(flags))
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGTERM)
        if proc is not None:
            proc.wait(timeout=10)
        assert gone(pid), f"redis {pid} did not exit"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def postgres():
    d = BASE / "pg"
    data = d / "data"
    d.mkdir(parents=True, exist_ok=True)
    if not (data / "PG_VERSION").exists():
        subprocess.run(["/opt/homebrew/bin/initdb", "-D", str(data), "-A", "trust", "-U", "probe"],
                       env=env(), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = free_port()
    subprocess.run(["/opt/homebrew/bin/pg_ctl", "-D", str(data), "-l", str(d / "log"), "-w",
                    "-o", f"-c listen_addresses=127.0.0.1 -p {port} -c unix_socket_directories=''", "start"],
                   env=env(), check=True, stdout=subprocess.DEVNULL)
    postmaster = int((data / "postmaster.pid").read_text().splitlines()[0])
    try:
        time.sleep(1.0)
        c = procs.containment(None, None, None, ATTEMPT, root=ROOT)
        table = procs.snapshot().rows
        children = sorted(pid for pid, row in table.items() if row[0] == postmaster)
        print(json.dumps({"case": "postgres via pg_ctl start", "postmaster": postmaster,
                          "ppid/pgid/stat": shape(postmaster),
                          "kernel_procargs_has_marker": procargs_has_marker(postmaster),
                          "postmaster_seen_by_census_marker_scan": postmaster in c.marker_pids,
                          "children": children,
                          "children_seen_by_census_marker_scan": sorted(set(children) & c.marker_pids),
                          "census_verified_empty": c.verified_empty, "errors": list(c.errors)}), flush=True)
    finally:
        subprocess.run(["/opt/homebrew/bin/pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                       env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert gone(postmaster, 20), f"postmaster {postmaster} did not exit"


CASES = {
    "python": lambda: simple("control: uv python sleeping", [PY, "-c", "import time; time.sleep(30)"]),
    "sh": lambda: simple("/bin/sh loop", ["/bin/sh", "-c", "while :; do sleep 1; done"]),
    "bash": lambda: simple("/bin/bash loop", ["/bin/bash", "-c", "while :; do sleep 1; done"]),
    "zsh": lambda: simple("/bin/zsh loop", ["/bin/zsh", "-c", "while :; do sleep 1; done"]),
    "sleep": lambda: simple("/bin/sleep", ["/bin/sleep", "30"]),
    "tail": lambda: simple("/usr/bin/tail -f", ["/usr/bin/tail", "-f", "/dev/null"]),
    "node": lambda: simple("node, no title", ["/opt/homebrew/bin/node", "-e", "setTimeout(()=>{}, 30000)"]),
    "node-title": lambda: simple("node, process.title set", ["/opt/homebrew/bin/node", "-e",
                                 "process.title='dev-server'; setTimeout(()=>{}, 30000)"]),
    "redis-daemonize": lambda: redis("redis-daemonize", "--daemonize", "yes"),
    "redis-daemonize-notitle": lambda: redis("redis-daemonize-notitle", "--daemonize", "yes", "--set-proc-title", "no"),
    "redis-foreground": lambda: redis("redis-foreground"),
    "postgres": postgres,
}

if __name__ == "__main__":
    for name in sys.argv[2:] or list(CASES):
        CASES[name]()
