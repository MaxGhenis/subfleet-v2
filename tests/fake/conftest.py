"""Isolated daemon process and read-only observation helpers (C-20.1)."""

from __future__ import annotations

from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import pytest


REPO = Path(__file__).resolve().parents[2]
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


class Harness:
    def __init__(self, root: Path):
        self.root = root = root.resolve()
        self.workdir = root / "work"
        self.workdir.mkdir()
        # C-11.7 is covered end to end by tests/e2e/test_reserve.py; the fake-daemon
        # cases describe mechanics the rule sits on top of, so the policy starts with it off.
        policy = json.loads((REPO / "subfleet/default_policy.json").read_text())
        policy.setdefault("reserve", {})["models"] = []
        (root / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
        self.process: subprocess.Popen | None = None
        self.logs = []
        self.root.joinpath("lanes.json").write_text(json.dumps([{
            "lane_id": "codex-1", "provider": "codex", "account_key": "codex:fake",
            "credential_ref": str(root / "home"), "credential_kind": "home",
            "credential_epoch": 1, "home": str(root / "home"), "owner": "v2",
            "desktop": False, "enabled": True,
        }]))
        (root / "home").mkdir()

    def start(self, *options: str) -> Harness:
        log = (self.root / f"harness-{len(self.logs)}.log").open("wb")
        self.logs.append(log)
        env = {**os.environ, "PYTHONPATH": str(REPO), "SUBFLEET_HOME": str(self.root)}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.fake.run_daemon", "--state-root", str(self.root),
             *options], cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )

        def ready():
            if self.process.poll() is not None:
                raise AssertionError(f"daemon exited {self.process.returncode}: {self.log_text()}")
            try:
                return self.request("daemon.status")["ok"]
            except (OSError, ValueError):
                return False

        self.until(ready, timeout=5)
        return self

    def log_text(self) -> str:
        return "\n".join(Path(stream.name).read_text(errors="replace") for stream in self.logs)

    def connect(self) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(self.root / "daemon.sock"))
        return client

    def request(self, op: str, **args) -> dict:
        with self.connect() as client:
            client.sendall((json.dumps({"v": 1, "id": "test", "op": op, "args": args}) + "\n").encode())
            with client.makefile("rb") as stream:
                line = stream.readline()
                if not line:
                    raise ConnectionError("daemon closed without a response")
                return json.loads(line)

    def call(self, op: str, **args) -> dict:
        response = self.request(op, **args)
        assert response["ok"], response
        return response["result"]

    def submit_args(self, scenario: str = "ok", delay_s: float = 0, **overrides) -> dict:
        request_id = overrides.pop("request_id", str(uuid.uuid4()))
        prompt = self.root / f"prompt-{uuid.uuid4().hex}.md"
        settings = {"scenario": scenario, "delay_s": delay_s}
        if scenario in {"nested-setsid", "nested-setsid-platform", "platform-escape-exit", "platform-escape-on-term",
                        "platform-handoff-exit"}:
            settings["marker"] = str(self.root / "escaped.pid")
        prompt.write_text(json.dumps(settings))
        return {
            "request_id": request_id, "kind": "dispatch", "workdir": str(self.workdir),
            "prompt_path": str(prompt), "sandbox": "read-only", "pinned_model": "astra",
            "allow_tmp": True, "caller_session": "fake-session", **overrides,
        }

    def submit(self, scenario: str = "ok", delay_s: float = 0, **overrides) -> str:
        return self.call("submit", **self.submit_args(scenario, delay_s, **overrides))["job_id"]

    def rows(self, sql: str, params=()) -> list[dict]:
        with sqlite3.connect(f"file:{self.root / 'state.sqlite3'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def job(self, job_id: str) -> dict:
        return self.rows("SELECT * FROM jobs WHERE job_id=?", (job_id,))[0]

    def attempts(self, job_id: str) -> list[dict]:
        return self.rows("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (job_id,))

    def until(self, predicate, timeout=5):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            result = predicate()
            if result:
                return result
            time.sleep(.015)
        raise AssertionError(f"condition timed out after {timeout}s\n{self.log_text()}")

    def attempt_state(self, job_id: str, state: str) -> dict:
        def match():
            attempts = self.attempts(job_id)
            return attempts[-1] if attempts and attempts[-1]["state"] == state else None
        return self.until(match)

    def finished(self, job_id: str) -> dict:
        def match():
            job = self.job(job_id)
            if job["state"] not in TERMINAL:
                return None
            if job["out_path"] and self.rows("SELECT * FROM leases WHERE lease_key=?",
                                              (f"out:{job['out_path']}",)):
                return None
            # Export commits after terminal state. The lease may disappear
            # between these reads, so return the row after that commit, not
            # the pre-export snapshot (which can omit export_error).
            return self.job(job_id)
        return self.until(match)

    def crash(self) -> None:
        assert self.process is not None
        self.process.kill()
        self.process.wait(timeout=3)

    def close(self) -> None:
        # The test owns these provider identities. Cleanup leaves no test writer.
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        from subfleet.procs import same_process
        for receipt in (self.root / "jobs").glob("*/a*/start.json"):
            with suppress(OSError, ValueError, KeyError):
                start = json.loads(receipt.read_text())
                if same_process(start["guardian_pid"], start["boot_id"], start["proc_start"]):
                    os.killpg(start["pgid"], signal.SIGKILL)
        # The C-5.5 fixtures' shell and intermediate first, so they start no writer after this.
        for marker in (self.root / "escaped.pid.shell", self.root / "escaped.pid.intermediate",
                       self.root / "escaped.pid"):
            if not (marker.exists() and marker.read_text().strip().isdecimal()):
                continue
            pid = int(marker.read_text())
            # Inspect the exact recorded marker PID and require our fixture argv.
            command = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                                     text=True, capture_output=True, check=False).stdout
            platform_writer = command.split() == ["/bin/sleep", "31.4159"]   # C-5.5 fixture
            handoff_shell = command.startswith("/bin/sh -c") and "/bin/sleep 31.4159 &" in command
            intermediate = "SUBFLEET_FAKE_MARKER" in command and command.rstrip().endswith("31.4159")
            if ((str(REPO / "tests/bin/fakeprov") in command and "--escaped-child" in command)
                    or platform_writer or handoff_shell or intermediate):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        for stream in self.logs:
            stream.close()


@pytest.fixture(scope="session")
def process_inspection_available():
    from subfleet.procs import InspectionError, boot_id, proc_start
    try:
        boot_id()
        if not proc_start(os.getpid()):
            pytest.skip("C-5.3 requires process identity; ps returned no current process")
    except InspectionError as exc:
        pytest.skip(f"C-5.3 real daemon tests require permitted sysctl/ps inspection: {exc}")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "rep_" + report.when, report)


@pytest.fixture
def daemon(request, process_inspection_available):
    # AF_UNIX on macOS has a 104-byte path limit; pytest's default temp path is longer.
    with tempfile.TemporaryDirectory(prefix="sf-", dir="/tmp") as directory:
        harness = Harness(Path(directory))
        try:
            yield harness
        finally:
            harness.close()
            # Keep the state root of a failed test so daemon.log and the store can be read.
            report = getattr(request.node, "rep_call", None)
            if report is not None and report.failed:
                import re, shutil, sys
                keep = Path("/tmp/sf-failed") / re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
                shutil.rmtree(keep, ignore_errors=True)
                shutil.copytree(directory, keep, symlinks=True, ignore_dangling_symlinks=True)
                print(f"\n[daemon harness] kept state root at {keep}", file=sys.stderr)
