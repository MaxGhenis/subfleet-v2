"""Run a command and hold every process below it under a phys_footprint cap (D-CREDIT-MEM1).

macOS enforces no address-space limit (`ulimit -v` and `RLIMIT_AS` do nothing there),
and RSS leaves compressed pages out: on 2026-11-04 a pytest worker grew to about
127 GB, 126 GB of it compressed, while its RSS stayed small. This tool reads what
`top`'s MEM column shows, `ri_phys_footprint` from `proc_pid_rusage`, which counts
compressed and swapped pages too.

It starts the command in a session of its own and, every `--interval` seconds, reads
the footprint of each process below it (`proc_listpids(PROC_PPID_ONLY)`, never the
whole process table). A process over `--cap-mb` first gets SIGUSR1, which a Python
process that registered `faulthandler` for it (`tools/footprint_stacks.py`, a pytest
plugin) answers with every thread's stack; a second later it gets SIGKILL. At
`--deadline` seconds every process still below the command is killed. The log
(`--log`, JSON lines) holds each kill, a sample of the largest footprints every
`--every` seconds and each process's peak.

    python tools/footprint_watch.py --cap-mb 3072 --deadline 540 --log watch.jsonl -- pytest ...

Standard library only.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import struct
import subprocess
import sys
import time

_PROC_PPID_ONLY = 6
_RUSAGE_INFO_V0 = 0
#: struct rusage_info_v0: a 16-byte uuid, then 64-bit counters; ri_resident_size is the
#: 7th and ri_phys_footprint the 8th.
_RESIDENT_AT, _FOOTPRINT_AT = 16 + 6 * 8, 16 + 7 * 8
_CTL_KERN, _KERN_PROCARGS2 = 1, 49

_libc = ctypes.CDLL(None, use_errno=True)
_libc.proc_listpids.restype = ctypes.c_int
_libc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
_libc.proc_pid_rusage.restype = ctypes.c_int
_libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libc.sysctl.restype = ctypes.c_int
_libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]


def footprint(pid: int) -> tuple[int, int] | None:
    """(phys_footprint, resident) in bytes, or None once `pid` is gone."""
    buffer = ctypes.create_string_buffer(512)
    if _libc.proc_pid_rusage(pid, _RUSAGE_INFO_V0, buffer) != 0:
        return None
    return (struct.unpack_from("=Q", buffer.raw, _FOOTPRINT_AT)[0],
            struct.unpack_from("=Q", buffer.raw, _RESIDENT_AT)[0])


def children(pid: int) -> list[int]:
    needed = _libc.proc_listpids(_PROC_PPID_ONLY, pid, None, 0)
    if needed <= 0:
        return []
    listed = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 64))()
    got = _libc.proc_listpids(_PROC_PPID_ONLY, pid, listed, ctypes.sizeof(listed))
    return [child for child in listed[:max(got, 0) // ctypes.sizeof(ctypes.c_int)] if child > 0]


def descendants(pid: int) -> list[int]:
    found, pending = [], [pid]
    while pending:
        for child in children(pending.pop(0)):
            if child not in found:
                found.append(child)
                pending.append(child)
    return found


def command_line(pid: int) -> str:
    size = ctypes.c_size_t(os.sysconf("SC_ARG_MAX"))
    buffer = ctypes.create_string_buffer(size.value)
    name = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    if _libc.sysctl(name, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return "?"
    data = buffer.raw[:size.value]
    count = struct.unpack_from("=i", data)[0]
    words = [part for part in data[4:].split(b"\0") if part][1:1 + count]
    return " ".join(word.decode(errors="replace") for word in words)[:400]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cap-mb", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True, help="seconds")
    parser.add_argument("--log", required=True)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--every", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    cap = args.cap_mb * 1024 * 1024
    log = open(args.log, "a", buffering=1)

    def note(**fields) -> None:
        log.write(json.dumps({"t": round(time.time(), 3), **fields}) + "\n")

    process = subprocess.Popen(command, start_new_session=True)
    began = time.monotonic()
    peaks: dict[int, dict] = {}
    killed: dict[int, float] = {}
    last_sample = 0.0
    note(event="start", pid=process.pid, command=" ".join(command)[:400], cap_mb=args.cap_mb)
    try:
        while process.poll() is None:
            now = time.monotonic()
            sample = []
            for pid in [process.pid, *descendants(process.pid)]:
                found = footprint(pid)
                if found is None:
                    continue
                size, resident = found
                peak = peaks.setdefault(pid, {"peak": 0, "command": None})
                if size > peak["peak"]:
                    peak["peak"] = size
                    if peak["command"] is None:
                        peak["command"] = command_line(pid)
                sample.append((size, resident, pid))
                if size > cap and pid not in killed:
                    killed[pid] = now
                    note(event="over-cap", pid=pid, footprint_mb=size >> 20, resident_mb=resident >> 20,
                         command=command_line(pid), after_s=round(now - began, 1))
                    try:
                        os.kill(pid, signal.SIGUSR1)
                    except OSError:
                        pass
                elif pid in killed and now - killed[pid] > 1.0:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        note(event="killed", pid=pid, footprint_mb=size >> 20)
                    except OSError:
                        pass
            if now - last_sample >= args.every:
                last_sample = now
                sample.sort(reverse=True)
                note(event="sample", after_s=round(now - began, 1),
                     top=[{"pid": pid, "footprint_mb": size >> 20, "resident_mb": resident >> 20}
                          for size, resident, pid in sample[:4]])
            if now - began > args.deadline:
                note(event="deadline", after_s=round(now - began, 1))
                for pid in [process.pid, *descendants(process.pid)]:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                break
            time.sleep(args.interval)
        rc = process.wait()
    finally:
        top = sorted(peaks.items(), key=lambda item: -item[1]["peak"])[:12]
        note(event="end", rc=process.poll(), after_s=round(time.monotonic() - began, 1),
             peaks=[{"pid": pid, "peak_mb": found["peak"] >> 20, "command": found["command"]} for pid, found in top])
        log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
