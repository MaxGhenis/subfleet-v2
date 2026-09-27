#!/usr/bin/env python3
"""What scheduling priority each way of lowering (or raising) QoS gives a process, its threads and its children.

Run from a shell (default QoS) or as a temporary launchd job (--context label) to see what launchd's
ProcessType gives. Each variant runs in a fresh interpreter so no QoS change leaks between them.
Priorities are read with `ps -M -o pri` (no environment is read). Output: one JSON document.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time

QOS = {"user-interactive": 0x21, "user-initiated": 0x19, "default": 0x15, "utility": 0x11,
       "background": 0x09, "unspecified": 0x00}
NAMES = {v: k for k, v in QOS.items()}
TASKPOLICY = "/usr/sbin/taskpolicy"
CHILD = ["/bin/sh", "-c", "/bin/sleep 4 & /bin/sleep 4; wait"]


def libc():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint, ctypes.c_int]
    lib.pthread_set_qos_class_self_np.restype = ctypes.c_int
    lib.qos_class_self.restype = ctypes.c_uint
    return lib


def thread_pris(pid: int) -> list[int]:
    """pid's threads' priorities from `ps -M`'s PRI column. (The version that wrote this
    directory's mech-*.json read every numeric token, so each array there starts with the
    pid; the priorities follow it. Fixed after the review of 885142a5.)"""
    rows = subprocess.run(["/bin/ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout.splitlines()
    if not rows:
        return []
    column, found = rows[0].split().index("PRI"), []
    for n, row in enumerate(rows[1:]):
        fields = row.split()
        index = column if n == 0 else column - 2          # a thread's row starts at its PID
        if index < len(fields):
            found.append(int(fields[index].rstrip("TRSUIZ")))
    return found


def tree(pid: int) -> dict:
    """pid's threads, and each child's and grandchild's threads."""
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        p, pp = (int(x) for x in line.split())
        kids.setdefault(pp, []).append(p)
    node = {"pid": pid, "pri": thread_pris(pid), "children": []}
    for child in kids.get(pid, []):
        node["children"].append({"pid": child, "pri": thread_pris(child),
                                 "children": [{"pid": g, "pri": thread_pris(g)} for g in kids.get(child, [])]})
    return node


def spawn_and_read(argv, **kw) -> dict:
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    time.sleep(1.0)
    shape = tree(proc.pid)
    proc.kill()
    proc.wait()
    return shape


def variant(name: str) -> dict:
    lib = libc()
    result: dict = {"variant": name, "pid": os.getpid()}
    rc = None
    if name == "baseline":
        pass
    elif name.startswith("thread-qos-"):
        rc = lib.pthread_set_qos_class_self_np(QOS[name.removeprefix("thread-qos-")], 0)
    elif name.startswith("nice-"):
        os.setpriority(os.PRIO_PROCESS, 0, int(name.removeprefix("nice-")))
    elif name == "nice-11-then-thread-qos-default":
        pass
    result["set_rc"] = rc
    result["qos_class_self"] = NAMES.get(lib.qos_class_self(), hex(lib.qos_class_self()))
    result["self_pri"] = thread_pris(os.getpid())
    # A new Python thread made after the change: what does it run at?
    seen = {}
    gate = threading.Event()

    def worker():
        seen["qos"] = NAMES.get(lib.qos_class_self(), "?")
        gate.wait(5)
    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.3)
    seen["pris_with_thread"] = thread_pris(os.getpid())
    gate.set()
    t.join()
    result["new_thread"] = seen
    # Raising from here: can a thread ask for more than it has?
    result["children"] = {
        "fork_exec": spawn_and_read(CHILD),                       # close_fds=True: _posixsubprocess fork+exec
        "posix_spawn": spawn_and_read(CHILD, close_fds=False),    # CPython uses posix_spawn here
        "taskpolicy_utility": spawn_and_read([TASKPOLICY, "-c", "utility", *CHILD]),
    }
    raise_rc = lib.pthread_set_qos_class_self_np(QOS["user-initiated"], 0)
    result["raise_to_user_initiated"] = {"rc": raise_rc, "qos_class_self": NAMES.get(lib.qos_class_self(), "?"),
                                         "self_pri": thread_pris(os.getpid())}
    result["children_after_raise"] = {"fork_exec": spawn_and_read(CHILD)}
    return result


def nice_then_qos() -> dict:
    lib = libc()
    os.setpriority(os.PRIO_PROCESS, 0, 11)
    before = thread_pris(os.getpid())
    rc = lib.pthread_set_qos_class_self_np(QOS["default"], 0)
    return {"variant": "nice-11-then-thread-qos-default", "before_qos": before, "rc": rc,
            "after_qos": thread_pris(os.getpid())}


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--variant":
        name = sys.argv[2]
        print(json.dumps(nice_then_qos() if name == "nice-11-then-thread-qos-default" else variant(name)))
        return 0
    out_path = None
    context = "shell"
    args = sys.argv[1:]
    while args:
        flag = args.pop(0)
        if flag == "--out":
            out_path = args.pop(0)
        elif flag == "--context":
            context = args.pop(0)
    report = {"context": context, "python": sys.executable, "loadavg": os.getloadavg(),
              "self_pri": thread_pris(os.getpid()), "variants": []}
    for name in ("baseline", "thread-qos-utility", "thread-qos-default", "nice-11", "nice-11-then-thread-qos-default"):
        run = subprocess.run([sys.executable, __file__, "--variant", name], capture_output=True, text=True)
        try:
            report["variants"].append(json.loads(run.stdout))
        except ValueError:
            report["variants"].append({"variant": name, "error": run.stderr[-2000:]})
    clamped = subprocess.run([TASKPOLICY, "-c", "utility", sys.executable, __file__, "--variant", "baseline"],
                             capture_output=True, text=True)
    try:
        report["variants"].append({"under": "taskpolicy -c utility", **json.loads(clamped.stdout)})
    except ValueError:
        report["variants"].append({"under": "taskpolicy -c utility", "error": clamped.stderr[-2000:]})
    text = json.dumps(report, indent=1)
    if out_path:
        with open(out_path + ".tmp", "w") as f:
            f.write(text)
        os.replace(out_path + ".tmp", out_path)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
