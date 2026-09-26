#!/usr/bin/env python3
"""Reproduce the 2026-09-25 daemon stall under synthetic load, and measure it.

On 2026-09-25, with the machine at load 85-100, a socket `list` took 34-94 s
though its statement takes 1 ms; 18 daemon threads waited on the store lock
and the daemon used 3% CPU. The load was about 21 concurrent PostToolUse
hooks (`list` mine running, `notice.pending`, then a 60 s `wait` long poll per
running job) against a store with a week of history.

This tool builds that situation against a fake-provider daemon from any
checkout (`--code`), so a baseline and a fix can be measured the same way:

- a state root seeded with history: accepted jobs with notices, events and
  decisions (the live store had 308 accepted jobs, 638k events, 33k
  decisions of ~11 KB);
- `--running` long jobs, each with a hook-style waiter (a `wait` long poll of
  60 s, repeated), owned by the first sessions;
- `--sessions` hook emulators, each making a "Bash call" every 1-4 s: `list`
  mine running, `notice.pending`, and a waiter for any job it has not armed;
- a stream of short jobs (one every `--short-every` s, `--short-s` long) whose
  waiters measure how long a `wait` takes to return after its job's terminal
  state is committed (sampled from a read-only connection every 10 ms);
- probes timing `list` (mine, running), `show`, `ping` and `daemon.status`
  once a second each, on their own threads;
- `--gate-pollers` clients polling `gate.poll` at 4 Hz, as `subfleet gate`
  does while its peer runs (each poll reads every `gate.state` event);
- `--spinners` CPU-bound processes; everything this tool starts runs at
  `--nice` so the contention is among its own processes, not other work;
- `--duty`: the daemon is let run only this fraction of each 100 ms
  (SIGSTOP/SIGCONT), which pins it to a small share of one core without
  loading the machine; the live daemon used 3% of a core during the stall;
- with a checkout that has C-3.6, SIGUSR1 stack dumps mid-run, and a summary
  of the lock-watch lines in daemon.log (holders by innermost subfleet frame).

The report is JSON (`--report`) plus a text summary on stdout. Every number
comes from this run; nothing is estimated.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

DAEMON = r"""
import collections, json, os, signal, sys, threading, time
from pathlib import Path
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon
from tests.fake import profile as fake_profile
from tests.fake_adapter import FakeAdapter
register("codex", FakeAdapter)
fake_profile.install()
daemon = Daemon(Path(sys.argv[1]))
watch = getattr(daemon, "lock_watch", None)
if watch is not None:                           # C-3.6 thresholds, lowered to see shorter holds
    watch.hold_s = float(os.environ.get("SFR_HOLD_S", watch.hold_s))
    watch.wait_s = float(os.environ.get("SFR_WAIT_S", watch.wait_s))
    watch.every_s = float(os.environ.get("SFR_EVERY_S", watch.every_s))
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: daemon.stopping.set())

# A poor man's profiler: every 2 ms, each thread that is not parked in an idle
# wait is counted against its innermost subfleet frames. A thread waiting for a
# watched lock is counted as such, against its caller.
IDLE = {("threading.py", "wait"), ("queue.py", "get"), ("socket.py", "accept"), ("socket.py", "readinto"),
        ("selectors.py", "select"), ("thread.py", "_worker"), ("threading.py", "_wait_for_tstate_lock")}
samples, gathering = collections.Counter(), threading.Event()
def sampler():
    me = threading.get_ident()
    names = {}
    while not daemon.stopping.wait(.002):
        if not gathering.is_set():
            continue
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            inner = frame
            where = (os.path.basename(inner.f_code.co_filename), inner.f_code.co_name)
            if where in IDLE:
                continue
            ours = []
            f = frame
            while f is not None and len(ours) < 4:
                if "/subfleet/" in f.f_code.co_filename:
                    ours.append(f"{os.path.basename(f.f_code.co_filename)}:{f.f_code.co_name}")
                f = f.f_back
            kind = "lock-wait" if where == ("lockwatch.py", "_wait") else "busy"
            samples[(kind, where[1], " < ".join(ours))] += 1
if os.environ.get("SFR_SAMPLE"):
    threading.Thread(target=sampler, daemon=True).start()
    signal.signal(signal.SIGUSR2, lambda *_: gathering.set())
try:
    daemon.serve_forever()
finally:
    if os.environ.get("SFR_SAMPLE"):
        Path(os.environ["SFR_SAMPLE"]).write_text(json.dumps(
            [[k, n] for k, n in samples.most_common(80)], indent=1))
    daemon.close()
"""

SPINNER = "while True:\n    pass\n"
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples: dict[str, list[tuple[float, float, str]]] = collections.defaultdict(list)

    def add(self, op: str, started: float, seconds: float, outcome: str) -> None:
        with self.lock:
            self.samples[op].append((started, seconds, outcome))


class Rig:
    def __init__(self, args):
        self.args = args
        self.code = Path(args.code).resolve()
        self.root = Path(args.root or tempfile.mkdtemp(prefix="sfr-", dir="/tmp")).resolve()
        self.sock = self.root / "daemon.sock"
        self.env = {**os.environ, "PYTHONPATH": str(self.code), "SUBFLEET_HOME": str(self.root),
                    # Belt and braces for the mirror: an empty session store of its own.
                    "SUBFLEET_SESSION_STORE": str(self.root / "session-store")}
        if args.sample:
            self.env["SFR_SAMPLE"] = str(self.root / "samples.json")
        if args.hold_s is not None:
            self.env.update(SFR_HOLD_S=str(args.hold_s), SFR_WAIT_S=str(args.hold_s), SFR_EVERY_S="0")
        self.python = args.python or sys.executable
        self.daemon: subprocess.Popen | None = None
        self.spinners: list[subprocess.Popen] = []
        self.rec = Recorder()
        self.stop = threading.Event()
        self.measuring = threading.Event()
        self.armed: set[str] = set()
        self.armed_lock = threading.Lock()
        self.terminal_at: dict[str, float] = {}      # job id -> first time seen terminal
        self.returned_at: dict[str, float] = {}      # job id -> when its waiter's wait returned it
        self.errors = collections.Counter()

    # --- the daemon ---------------------------------------------------------

    def prepare(self) -> None:
        root = self.root
        (root / "work").mkdir(parents=True, exist_ok=True)
        (root / "home").mkdir(exist_ok=True)
        policy = json.loads((self.code / "subfleet/default_policy.json").read_text())
        policy.setdefault("reserve", {})["models"] = []
        policy.setdefault("conversations", {})["catalog_interval_s"] = 0
        # The desktop sidebar mirror writes into the Claude app's session store
        # under the real HOME; a state root with no merge base must not run it.
        policy.setdefault("sessions", {})["mirror_interval_s"] = 0
        policy.setdefault("caps", {})["max_active_attempts"] = self.args.running + 3
        (root / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
        lanes = [{"lane_id": f"codex-{n}", "provider": "codex", "account_key": f"codex:fake{n}",
                  "credential_ref": str(root / "home"), "credential_kind": "home",
                  "credential_epoch": 1, "home": str(root / "home"), "owner": "v2",
                  "desktop": False, "enabled": True} for n in range(1, self.args.lanes + 1)]
        (root / "lanes.json").write_text(json.dumps(lanes))

    def start_daemon(self) -> None:
        log = (self.root / "harness.log").open("ab")
        self.daemon = subprocess.Popen([self.python, "-c", DAEMON, str(self.root)], cwd=self.code,
                                       env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                raise SystemExit(f"daemon exited {self.daemon.returncode}; see {self.root}/harness.log")
            try:
                if self.call("ping", {}, timeout=5).get("ok"):
                    return
            except OSError:
                pass
            time.sleep(.2)
        raise SystemExit("daemon did not answer within 120 s")

    def stop_daemon(self) -> None:
        if self.daemon and self.daemon.poll() is None:
            self.daemon.send_signal(signal.SIGTERM)
            try:
                self.daemon.wait(30)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait(10)

    def seed(self) -> dict:
        """History like the live store's, written while the daemon is down."""
        a = self.args
        db = sqlite3.connect(self.root / "state.sqlite3")
        db.execute("PRAGMA journal_mode=WAL")
        now = time.time()
        with db:
            jobs, attempts, notices = [], [], []
            for i in range(a.history):
                job = f"20260920-{i:06d}-history-{i}"
                created = iso(now - 86400 * 5 + i * 60)
                jobs.append((job, str(uuid.uuid4()), "seed", "dispatch", "succeeded", str(self.root / "work"),
                             str(self.root / "prompt-seed.md"), "read-only", f"history-{i % 40}",
                             f"{job}/a1", 0, created, created, created))
                attempts.append((f"{job}/a1", job, 1, "codex-1", "astra", "succeeded", created, created, created))
                notices.append((job, f"history-{i % 40}", f"{job}: ok", "acknowledged", created))
            db.executemany("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,"
                           "sandbox,caller_session,accepted_attempt_id,rc,created_at,started_at,finished_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", jobs)
            db.executemany("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,"
                           "reserved_at,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)", attempts)
            db.executemany("INSERT INTO notices(job_id,session_id,text,state,created_at) VALUES (?,?,?,?,?)",
                           notices)
            kinds = ["timer.run", "timer.verdict", "lease.acquired", "lease.released", "readings.insert",
                     "attempt.reserved", "decisions.insert", "artifact.recorded"]
            payload = json.dumps({"detail": "x" * 80})
            db.executemany("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                           ((iso(now - 86400 * 5 + i), kinds[i % len(kinds)],
                             jobs[i % len(jobs)][0] if jobs and i % 3 == 0 else None, payload)
                            for i in range(a.events)))
            db.executemany("INSERT INTO readings(lane_id,scope,window,utilization,resets_at,label,source,observed_at) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           ((f"codex-{1 + i % a.lanes}", "account", ("5h", "7d")[i % 2], .1, iso(now + 3600),
                             "provider", "seed", iso(now - 86400 * 5 + i * 10)) for i in range(a.readings)))
            probe = {"state": "completed", "lane_id": "codex-1", "model_id": "astra", "detail": "y" * 300}
            db.executemany("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                           ((iso(now - 86400 * 4 + i), "probe.state", None,
                             json.dumps({**probe, "holder": f"probe:seed-{i}"})) for i in range(a.probe_events)))
            gate = {"id": "seed-gate", "status": "reviewing", "rounds": [{"n": 1, "notes": "z" * 6000}]}
            db.executemany("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)",
                           ((iso(now - 86400 * 3 + i), "gate.state", None,
                             json.dumps({"gate_id": f"seed-gate-{i}", "transition": "seed",
                                         "state": {**gate, "id": f"seed-gate-{i}"}})) for i in range(a.gate_events)))
            decision = json.dumps({"policy_hash": "seed", "candidates": ["x" * 100] * 100})
            db.executemany("INSERT INTO decisions(job_id,evaluated_at,policy_hash,decision_json) VALUES (?,?,?,?)",
                           ((jobs[i % len(jobs)][0], iso(now - 3600), "seed", decision)
                            for i in range(a.decisions if jobs else 0)))
        (self.root / "prompt-seed.md").write_text("seed")
        sizes = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                 for t in ("jobs", "attempts", "events", "decisions", "notices", "readings")}
        db.close()
        return sizes

    def start_spinners(self) -> None:
        for _ in range(self.args.spinners):
            self.spinners.append(subprocess.Popen([self.python, "-c", SPINNER], stdin=subprocess.DEVNULL))

    def stop_spinners(self) -> None:
        for proc in self.spinners:
            proc.kill()
        for proc in self.spinners:
            proc.wait(10)

    # --- the socket ---------------------------------------------------------

    def call(self, op: str, args: dict, *, timeout: float) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(self.sock))
            conn.sendall((json.dumps({"v": 1, "id": uuid.uuid4().hex[:8], "op": op, "args": args}) + "\n").encode())
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = conn.recv(1 << 16)
                if not chunk:
                    raise ConnectionError("closed without a response")
                buffer += chunk
        return json.loads(buffer)

    def timed(self, op: str, args: dict, *, label: str | None = None, timeout: float = 240) -> dict | None:
        started = time.monotonic()
        try:
            response = self.call(op, args, timeout=timeout)
            outcome = "ok" if response.get("ok") else f"error:{(response.get('error') or {}).get('code')}"
        except (OSError, ValueError) as exc:
            response, outcome = None, f"exception:{type(exc).__name__}"
            self.errors[f"{label or op}:{type(exc).__name__}"] += 1
        if self.measuring.is_set():
            self.rec.add(label or op, started, time.monotonic() - started, outcome)
        return response

    # --- the load -----------------------------------------------------------

    def submit(self, session: str, delay_s: float) -> str | None:
        prompt = self.root / f"prompt-{uuid.uuid4().hex}.md"
        prompt.write_text(json.dumps({"scenario": "ok", "delay_s": delay_s}))
        response = self.timed("submit", {
            "request_id": str(uuid.uuid4()), "kind": "dispatch", "workdir": str(self.root / "work"),
            "prompt_path": str(prompt), "sandbox": "read-only", "pinned_model": "astra",
            "allow_tmp": True, "caller_session": session})
        if response and response.get("ok"):
            return response["result"]["job_id"]
        return None

    def waiter(self, session: str, job_id: str) -> None:
        """The PostToolUse hook's `_wait_and_deliver`: 60 s long polls until the job ends."""
        try:
            while not self.stop.is_set():
                response = self.timed("wait", {"job_ids": [job_id], "deadline_s": 60}, timeout=300)
                if response is None:
                    time.sleep(.25)
                    continue
                result = response.get("result") or {}
                if result.get("timeout"):
                    continue
                jobs = result.get("jobs") or []
                if any(j.get("job_id") == job_id and j.get("state") in TERMINAL for j in jobs):
                    self.returned_at.setdefault(job_id, time.monotonic())
                    self.timed("notice.pending", {"session_id": session}, label="notice.pending(deliver)")
                    return
        finally:
            with self.armed_lock:
                self.armed.discard(job_id)

    def arm(self, session: str, job_id: str) -> None:
        with self.armed_lock:
            if job_id in self.armed:
                return                              # the hook's file lease: one waiter per job
            self.armed.add(job_id)
        threading.Thread(target=self.waiter, args=(session, job_id), daemon=True).start()

    def hook(self, session: str) -> None:
        """A session making a Bash call every 1-4 s: what each PostToolUse hook asks."""
        rng = random.Random(session)
        while not self.stop.wait(rng.uniform(1, 4)):
            response = self.timed("list", {"mine": session, "running": True}, label="list(hook)")
            if rng.random() < .3:
                self.timed("notice.pending", {"session_id": session}, label="notice.pending(prompt)")
            for job in ((response or {}).get("result") or {}).get("jobs") or []:
                self.arm(session, job["job_id"])

    def short_jobs(self) -> None:
        n = 0
        while not self.stop.wait(self.args.short_every):
            session = f"sess-{n % self.args.sessions}"
            n += 1
            job_id = self.submit(session, self.args.short_s)
            if job_id:
                self.arm(session, job_id)

    def terminal_watch(self) -> None:
        """When each job's terminal state becomes visible to a read-only reader."""
        db = sqlite3.connect(f"file:{self.root / 'state.sqlite3'}?mode=ro", uri=True, timeout=30)
        while not self.stop.wait(.01):
            try:
                for job_id, in db.execute("SELECT job_id FROM jobs WHERE state IN "
                                          "('succeeded','failed','cancelled','lost') AND job_id NOT LIKE '%-history-%'"):
                    self.terminal_at.setdefault(job_id, time.monotonic())
            except sqlite3.Error:
                pass
        db.close()

    def probe(self, op: str, args: dict, label: str) -> None:
        while not self.stop.wait(1):
            self.timed(op, args, label=label)

    def gate_poller(self, n: int) -> None:
        # An unknown gate reads every gate.state event before it says so, as a
        # live one does before it finds its own (GateService._load).
        while not self.stop.wait(.25):
            self.timed("gate.poll", {"gate_id": f"no-such-gate-{n}"}, label="gate.poll")

    def throttle(self) -> None:
        """Let the daemon run `duty` of every 100 ms."""
        run, pause = .1 * self.args.duty, .1 * (1 - self.args.duty)
        pid = self.daemon.pid
        try:
            while not self.stop.is_set():
                os.kill(pid, signal.SIGSTOP)
                time.sleep(pause)
                os.kill(pid, signal.SIGCONT)
                time.sleep(run)
        except ProcessLookupError:
            pass
        finally:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def cpu_seconds(self) -> float | None:
        out = subprocess.run(["/bin/ps", "-o", "time=", "-p", str(self.daemon.pid)],
                             capture_output=True, text=True).stdout.strip()
        if not out:
            return None
        parts = [float(part) for part in out.replace("-", ":").split(":")]
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds

    def dumps(self, at: list[float]) -> None:
        started = time.monotonic()
        for offset in at:
            if self.stop.wait(max(0, started + offset - time.monotonic())):
                return
            if self.daemon and self.daemon.poll() is None:
                self.daemon.send_signal(signal.SIGUSR1)

    # --- the run ------------------------------------------------------------

    def run(self) -> dict:
        a = self.args
        if a.nice:
            os.nice(a.nice)
        self.prepare()
        self.start_daemon()
        self.stop_daemon()
        sizes = self.seed()
        self.start_daemon()
        diagnostics = (self.code / "subfleet/lockwatch.py").exists()
        log_start = (self.root / "daemon.log").stat().st_size
        threads = []
        try:
            for i in range(a.running):
                job_id = self.submit(f"sess-{i % a.sessions}", a.warmup + a.duration + 120)
                if job_id:
                    self.arm(f"sess-{i % a.sessions}", job_id)
            self.start_spinners()
            for i in range(a.sessions):
                threads.append(threading.Thread(target=self.hook, args=(f"sess-{i}",), daemon=True))
            threads.append(threading.Thread(target=self.short_jobs, daemon=True))
            threads.append(threading.Thread(target=self.terminal_watch, daemon=True))
            for op, args, label in (("list", {"mine": "sess-0", "running": True}, "probe:list"),
                                    ("ping", {}, "probe:ping"),
                                    ("show", {"job_id": "20260920-000000-history-0"}, "probe:show"),
                                    ("daemon.status", {}, "probe:status")):
                threads.append(threading.Thread(target=self.probe, args=(op, args, label), daemon=True))
            for n in range(a.gate_pollers):
                threads.append(threading.Thread(target=self.gate_poller, args=(n,), daemon=True))
            if a.duty < 1:
                threads.append(threading.Thread(target=self.throttle, daemon=True))
            if diagnostics and a.dumps:
                threads.append(threading.Thread(
                    target=self.dumps, args=([a.warmup + a.duration * f for f in (.5, .8)],), daemon=True))
            for thread in threads:
                thread.start()
            time.sleep(a.warmup)
            if a.sample:
                self.daemon.send_signal(signal.SIGUSR2)      # the sampler starts counting
            self.measuring.set()
            measured_from, cpu_from = time.monotonic(), self.cpu_seconds()
            time.sleep(a.duration)
            self.measuring.clear()
            measured_s, cpu_to = time.monotonic() - measured_from, self.cpu_seconds()
            final = self.timed("daemon.status", {}, label="final-status", timeout=600) or {}
            self.admission = ((final.get("result") or {}).get("admission") or {})
            self.daemon_cpu = (None if cpu_from is None or cpu_to is None
                               else round((cpu_to - cpu_from) / measured_s, 3))
        finally:
            self.stop.set()
            self.stop_spinners()
            self.stop_daemon()
            self.stop_providers()
        return self.report(sizes, measured_s, log_start, diagnostics)

    def stop_providers(self) -> None:
        """Guardians outlive their daemon by design (C-5.1); these are ours to end."""
        for receipt in (self.root / "jobs").glob("*/a*/start.json"):
            try:
                os.killpg(json.loads(receipt.read_text())["pgid"], signal.SIGKILL)
            except (OSError, ValueError, KeyError):
                pass

    # --- the report ---------------------------------------------------------

    def report(self, sizes: dict, measured_s: float, log_start: int, diagnostics: bool) -> dict:
        def stats(values):
            values = sorted(values)
            if not values:
                return {}
            pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]
            return {"n": len(values), "p50": round(pick(.5), 4), "p90": round(pick(.9), 4),
                    "p99": round(pick(.99), 4), "max": round(values[-1], 4),
                    "mean": round(statistics.fmean(values), 4)}
        ops = {}
        for op, samples in sorted(self.rec.samples.items()):
            outcomes = collections.Counter(outcome for _, _, outcome in samples)
            entry = stats([seconds for _, seconds, _ in samples])
            entry["outcomes"] = dict(outcomes)
            entry["rate_per_s"] = round(len(samples) / measured_s, 2)
            ops[op] = entry
        wakes = [self.returned_at[j] - self.terminal_at[j] for j in self.returned_at if j in self.terminal_at]
        text = (self.root / "daemon.log").read_text(errors="replace")[log_start:]
        watch = collections.Counter()
        holders = collections.Counter()
        held = [float(x) for x in re.findall(r"lock released after (\d+\.\d+) s", text)]
        for block in re.split(r"\n(?=\S)", text):
            head = block.split("\n", 1)[0]
            for kind, pattern in (("hold", " lock held "), ("released", " lock released after "),
                                  ("wait", " has waited ")):
                if pattern in head:
                    watch[kind] += 1
            if "Holder's stack:" in block:
                frames = re.findall(r'File "[^"]*/subfleet/([^"]+)", line (\d+), in (\w+)',
                                    block.split("Holder's stack:")[1])
                if frames:
                    path, line, func = frames[-1]
                    holders[f"{path}:{line} {func}"] += 1
        return {"code": str(self.code), "root": str(self.root), "diagnostics": diagnostics,
                "daemon_cpu_cores": getattr(self, "daemon_cpu", None),
                "route_evaluations": getattr(self, "admission", {}).get("route_evaluations"),
                "args": vars(self.args), "store": sizes, "measured_s": round(measured_s, 1),
                "ops": ops, "wake_after_terminal_s": stats(wakes), "wakes_unmatched":
                    len(self.returned_at) - len(wakes), "client_errors": dict(self.errors),
                "lockwatch_lines": dict(watch), "holders_innermost": holders.most_common(15),
                "long_holds_s": {"n": len(held), "max": max(held, default=None),
                                 "sum": round(sum(held), 2)},
                "stack_dumps": text.count("Current thread 0x"),
                "samples": (json.loads((self.root / "samples.json").read_text())
                            if (self.root / "samples.json").exists() else None)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--code", default=str(Path(__file__).resolve().parents[1]),
                        help="checkout whose daemon is measured (default: this one)")
    parser.add_argument("--python", help="interpreter for the daemon (default: this one)")
    parser.add_argument("--root", help="state root (default: a new /tmp/sfr-* directory)")
    parser.add_argument("--lanes", type=int, help="fake codex lanes (default: --running + 4, "
                        "so short jobs are not queued behind the long ones)")
    parser.add_argument("--history", type=int, default=300)
    parser.add_argument("--events", type=int, default=200_000)
    parser.add_argument("--decisions", type=int, default=3000)
    parser.add_argument("--readings", type=int, default=34_000)
    parser.add_argument("--probe-events", type=int, default=6_500)
    parser.add_argument("--gate-events", type=int, default=980)
    parser.add_argument("--gate-pollers", type=int, default=1)
    parser.add_argument("--duty", type=float, default=1.0,
                        help="fraction of each 100 ms the daemon may run (default 1: unthrottled)")
    parser.add_argument("--running", type=int, default=6)
    parser.add_argument("--sessions", type=int, default=21)
    parser.add_argument("--short-every", type=float, default=8.0)
    parser.add_argument("--short-s", type=float, default=3.0)
    parser.add_argument("--spinners", type=int, default=os.cpu_count() or 8)
    parser.add_argument("--nice", type=int, default=10)
    parser.add_argument("--warmup", type=float, default=20.0)
    parser.add_argument("--duration", type=float, default=90.0)
    parser.add_argument("--dumps", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--sample", action="store_true", help="count busy daemon threads every 2 ms")
    parser.add_argument("--hold-s", type=float, help="report store-lock holds and waits past this "
                        "many seconds, every one (C-3.6's defaults are 2 s and 5 s, one a minute)")
    parser.add_argument("--report", help="write the JSON report here")
    args = parser.parse_args()
    if args.lanes is None:
        args.lanes = args.running + 4
    report = Rig(args).run()
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(f"code {report['code']}  root {report['root']}  measured {report['measured_s']} s")
    print(f"store {report['store']}  daemon CPU {report['daemon_cpu_cores']} cores")
    print(f"route evaluations (C-6.3): {report['route_evaluations']}")
    for op, entry in report["ops"].items():
        print(f"  {op:28s} n={entry.get('n', 0):5d} {entry.get('rate_per_s', 0):6.1f}/s  "
              f"p50={entry.get('p50', 0):8.3f}  p90={entry.get('p90', 0):8.3f}  "
              f"p99={entry.get('p99', 0):8.3f}  max={entry.get('max', 0):8.3f}  {entry.get('outcomes')}")
    print(f"  wait returned after terminal commit: {report['wake_after_terminal_s']}")
    print(f"  client errors: {report['client_errors']}")
    print(f"  lock-watch lines: {report['lockwatch_lines']}  long holds: {report['long_holds_s']}")
    for holder, count in report["holders_innermost"]:
        print(f"    {count:4d}  {holder}")
    if report.get("samples"):
        total = sum(n for _, n in report["samples"])
        print(f"  thread samples (2 ms), top 25 of {total}:")
        for (kind, where, ours), n in report["samples"][:25]:
            print(f"    {n:6d} {kind:9s} {where:24s} {ours}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
