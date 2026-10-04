#!/usr/bin/env python3
"""Compare the daemon's latency-critical paths at the `utility` QoS and at the default, under the same load.

The installed daemon runs under launchd's `ProcessType` `Standard`, which on this Mac clamps every one of its
threads, and every process it starts, to the `utility` QoS (priority 20; a shell's processes run at 31). This
tool measures what that costs, never against the live daemon: each run starts its own fake-provider daemon or
census reader, alternating the two schedulings in ABBA order so that both meet the machine's load as it drifts.

- `tools/store_contention_repro.py` with `--daemon-qos utility` and with `inherit`: every outermost store-lock
  hold, admission's holds, each thread's wait for the lock, `daemon.status`, `ping` and `list` round trips, and
  a turn's time from queued to reserved;
- `tools/census_under_load.py` with `--qos utility` and with `inherit`: the census's marker source read in a
  process with GIL-hogging threads, through the socket reader and the pipe reader 2.1.7 shipped.

`--spinners N` adds N busy processes this tool owns, at `--spinner-qos` (default `utility`, as agent work
runs, so the added load does not compete with the operator's own apps), around every run of both tools. The
load average is recorded before and after each run. Every number in the summary comes from these runs.

Usage: uv run python tools/daemon_qos_compare.py --out DIR [--code CHECKOUT] [--census-tool PATH]
           [--census-python PYTHON] [--rounds 2] [--duration 90] [--census-seconds 60] [--spinners 0]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKPOLICY = "/usr/sbin/taskpolicy"
SPINNER = "while True:\n    pass\n"
QOS = ("utility", "inherit")


def order(rounds: int) -> list[str]:
    """ABBA: utility, inherit, inherit, utility, ... so a drift in load falls on both alike."""
    sequence: list[str] = []
    for n in range(rounds):
        sequence += list(QOS) if n % 2 == 0 else list(reversed(QOS))
    return sequence


def spinners(count: int, qos: str, python: str) -> list[subprocess.Popen]:
    prefix = [TASKPOLICY, "-c", "utility"] if qos == "utility" else []
    return [subprocess.Popen([*prefix, python, "-c", SPINNER], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(count)]


def stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        process.kill()
    for process in processes:
        process.wait(10)


def loadavg() -> list[float]:
    return [round(value, 1) for value in os.getloadavg()]


def store_run(args, qos: str, report: Path) -> dict:
    command = [args.python, str(HERE / "store_contention_repro.py"), "--code", args.code,
               "--daemon-qos", qos, "--spinners", "0", "--nice", "0", "--no-dumps",
               "--backlog", "48", "--backlog-s", "30", "--turns-every", "10",
               "--warmup", str(args.warmup), "--duration", str(args.duration), "--report", str(report)]
    started = time.monotonic()
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode or not report.exists():
        return {"error": f"exit {done.returncode}", "stderr": done.stderr[-2000:]}
    result = json.loads(report.read_text())
    result["wall_s"] = round(time.monotonic() - started, 1)
    return result


def census_run(args, qos: str, report: Path) -> dict:
    command = [args.census_python, args.census_tool, "--qos", qos, "--hogs", str(args.hogs),
               "--seconds", str(args.census_seconds), "--load", "0"]
    before = loadavg()
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode:
        return {"error": f"exit {done.returncode}", "stderr": done.stderr[-2000:]}
    result = json.loads(done.stdout)
    result["loadavg_before"] = before
    report.write_text(json.dumps(result, indent=1) + "\n")
    return result


def pick(entry: dict | None, *keys) -> str:
    entry = entry or {}
    values = [entry.get(key) for key in keys]
    return " / ".join("-" if value is None else f"{value:.3f}" for value in values)


def summary(runs: list[dict]) -> str:
    lines = ["| Run | Daemon QoS | Load before → after | Daemon threads by priority | "
             "Store-lock holds p50 / p99 / max (s) | Admission holds p50 / p99 (s) | Store-lock waits p50 / p99 (s) | "
             "`daemon.status` p50 / p99 (s) | `ping` p50 / p99 (s) | Hook `list` p50 / p99 (s) | "
             "Turn queued→reserved p50 / p99 (s) | Daemon CPU (cores) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for run in runs:
        if run["tool"] != "store":
            continue
        r = run["result"]
        if "error" in r:
            lines.append(f"| {run['n']} | {run['qos']} | error: {r['error']} |" + " |" * 9)
            continue
        ops, admission, load = r.get("ops", {}), r.get("admission", {}), r.get("loadavg") or {}
        lines.append(
            f"| {run['n']} | {run['qos']} | {load.get('start', ['?'])[0]} → {load.get('end', ['?'])[0]} | "
            f"{r.get('daemon_thread_priorities')} | "
            f"{pick(admission.get('store_holds_s'), 'p50', 'p99', 'max')} | "
            f"{pick(admission.get('holds_s'), 'p50', 'p99')} | {pick(admission.get('store_waits_s'), 'p50', 'p99')} | "
            f"{pick(ops.get('probe:status'), 'p50', 'p99')} | {pick(ops.get('probe:ping'), 'p50', 'p99')} | "
            f"{pick(ops.get('list(hook)'), 'p50', 'p99')} | "
            f"{pick(admission.get('turn_queued_to_reserved_s'), 'p50', 'p99')} | {r.get('daemon_cpu_cores')} |")
    lines += ["", "| Run | Census QoS | Load before → after | Reader | n | p50 (s) | max (s) | Past the 10 s cap | Reads p50 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for run in runs:
        if run["tool"] != "census":
            continue
        r = run["result"]
        if "error" in r:
            lines.append(f"| {run['n']} | {run['qos']} | error: {r['error']} |" + " |" * 6)
            continue
        for reader, entry in r.get("readers", {}).items():
            lines.append(f"| {run['n']} | {run['qos']} | {r.get('loadavg_before', ['?'])[0]} → {r['loadavg'][0]} | "
                         f"{reader} | {entry['n']} | {entry['p50_s']} | {entry['max_s']} | {entry['failed']} | "
                         f"{entry['reads_p50']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="directory for the raw reports and summary.md")
    parser.add_argument("--code", default=str(HERE.parent), help="checkout whose daemon is measured")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the store rig and its daemon")
    parser.add_argument("--census-tool", default=str(HERE / "census_under_load.py"))
    parser.add_argument("--census-python", default=sys.executable)
    parser.add_argument("--rounds", type=int, default=2, help="ABBA pairs: each round runs both schedulings")
    parser.add_argument("--warmup", type=float, default=20)
    parser.add_argument("--duration", type=float, default=90)
    parser.add_argument("--census-seconds", type=float, default=60)
    parser.add_argument("--hogs", type=int, default=4)
    parser.add_argument("--spinners", type=int, default=0)
    parser.add_argument("--spinner-qos", choices=QOS, default="utility")
    parser.add_argument("--tools", default="store,census", help="comma-separated: store, census")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tools = [tool for tool in args.tools.split(",") if tool]
    runs: list[dict] = []
    for n, qos in enumerate(order(args.rounds), 1):
        for tool in tools:
            load = spinners(args.spinners, args.spinner_qos, args.python)
            try:
                time.sleep(3 if load else 0)
                path = out / f"{tool}-{n:02d}-{qos}.json"
                started = loadavg()
                result = store_run(args, qos, path) if tool == "store" else census_run(args, qos, path)
            finally:
                stop(load)
            runs.append({"n": n, "tool": tool, "qos": qos, "spinners": args.spinners,
                         "spinner_qos": args.spinner_qos, "loadavg_at_start": started, "result": result})
            print(f"{tool} run {n} ({qos}) done; load {loadavg()}", flush=True)
    (out / "runs.json").write_text(json.dumps(
        [{**run, "result": {k: v for k, v in run["result"].items() if k != "samples"}} for run in runs],
        indent=1) + "\n")
    text = summary(runs)
    (out / "summary.md").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
