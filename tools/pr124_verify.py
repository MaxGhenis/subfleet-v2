"""Run one foreground verification slice with a deadline and exact-pid cleanup.

Usage: .venv/bin/python tools/pr124_verify.py NAME SECONDS COMMAND [ARG ...]
Logs and summaries stay in build/pr124-evidence; no child is detached.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "build/pr124-evidence"
PS_AVAILABLE = None


def descendants(parent):
    global PS_AVAILABLE
    if PS_AVAILABLE is False:
        return {parent}
    try:
        rows = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        PS_AVAILABLE = False
        return {parent}
    PS_AVAILABLE = True
    pairs = [tuple(map(int, row.split())) for row in rows.splitlines()]
    owned = {parent}
    while True:
        found = {pid for pid, ppid in pairs if ppid in owned}
        if found <= owned:
            return owned
        owned.update(found)


def run(name, seconds, command, cwd=ROOT):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    command = list(command)
    if any(command[i:i + 2] == ["-m", "pytest"] for i in range(len(command) - 1)):
        # Other lanes' pytest retention can delete the shared numbered temp
        # directory while this lane compiles. Own a short, isolated temp root.
        temporary = tempfile.mkdtemp(prefix="pr124-")
        command.extend(["--basetemp", temporary])
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    env.update(PATH=str(EVIDENCE / "bin") + os.pathsep + env["PATH"],
               PYTHONPATH=str(cwd), SWIFT_MODULECACHE_PATH="/private/tmp/pr124-swift-cache",
               CLANG_MODULE_CACHE_PATH="/private/tmp/pr124-clang-cache",
               SF_CUTOVER_PROBE_CACHE=str(EVIDENCE / "probes"))
    env["PR124_CHILD_RECORD"] = str(EVIDENCE / (name + "-children.jsonl"))
    started = time.monotonic()
    started_wall = time.time()
    owned = set()
    expired = False
    with (EVIDENCE / (name + ".log")).open("w") as log:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            while child.poll() is None:
                owned.update(descendants(child.pid))
                if time.monotonic() - started >= seconds:
                    expired = True
                    break
                time.sleep(0.25)
        finally:
            if expired or child.poll() is None:
                children = Path(env["PR124_CHILD_RECORD"])
                if children.exists():
                    events = [json.loads(line) for line in children.read_text().splitlines()]
                    active = {}
                    for event in events:
                        if event["at"] >= started_wall:
                            active[event["pid"]] = event["event"]
                    owned.update(pid for pid, event in active.items() if event == "started")
                records = ROOT / "build/app-cutover-evidence/compiler-processes.jsonl"
                if records.exists():
                    events = [json.loads(line) for line in records.read_text().splitlines()]
                    finished = {event["pid"] for event in events if event["event"] == "finished"}
                    for event in events:
                        if event["event"] == "started" and event["at"] >= started_wall and event["pid"] not in finished:
                            owned.update((event["pid"], event["wrapper"]))
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for pid in sorted(owned, reverse=True):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                # Compiler children can have their own process groups. Clean
                # only recorded descendants, never a process-name pattern.
                for pid in owned:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            child.wait()
    result = {"name": name, "exit_code": child.returncode, "timeout": expired,
              "seconds": round(time.monotonic() - started, 2), "command": command,
              "cwd": str(cwd), "pids": sorted(owned),
              "tail": (EVIDENCE / (name + ".log")).read_text().splitlines()[-8:]}
    (EVIDENCE / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("name", "exit_code", "timeout", "seconds", "tail")}), flush=True)
    return result


if __name__ == "__main__":
    result = run(sys.argv[1], float(sys.argv[2]), sys.argv[3:],
                 cwd=Path(os.environ.get("PR124_VERIFY_CWD", ROOT)))
    sys.exit(124 if result["timeout"] else result["exit_code"])
