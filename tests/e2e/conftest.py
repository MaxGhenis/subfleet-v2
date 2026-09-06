"""Real CLI/daemon/provider-adapter harness (C-20.1, C-21)."""

from __future__ import annotations

from contextlib import suppress
import base64
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

import pytest

from tests.fake.profile import derived_identity as claude_identity

REPO = Path(__file__).resolve().parents[2]
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}

# Loaded only in the actual subfleetd entry point. These observers use existing
# constructor hooks, retain real adapters/process inspection, and never fabricate
# a receipt, store row, provider response, or publication operation.
OBSERVERS = '''\
import os
from pathlib import Path
import sys

if Path(sys.argv[0]).name == "subfleetd":
    import json
    import time
    from subfleet.daemon import Daemon
    from tests.fake.run_daemon import audit_publication
    from tests.fake import profile as fake_profile
    root = Path(os.environ["SUBFLEET_HOME"])
    audit_publication(root / "publication.jsonl")
    # C-10.6: the profile endpoint answers from a fixture, never the network.
    # C-10.3: no desktop credential exists in a test HOME, and none is read.
    fake_profile.install()
    original_init = Daemon.__init__
    def observed_init(self, *args, **kwargs):
        kwargs.setdefault("desktop_prober", lambda: None)
        boundary = os.environ.get("SUBFLEET_E2E_HOLD_AT")
        if boundary:
            def hold(name, job_id, attempt_id):
                if name == boundary:
                    (root / ("hook-" + name + ".json")).write_text(json.dumps({
                        "job_id": job_id, "attempt_id": attempt_id}))
                    while not (root / "release-hook").exists():
                        time.sleep(.01)
            kwargs["crash_hook"] = hold
        kwargs["guardian_start_delay_s"] = float(
            os.environ.get("SUBFLEET_E2E_START_DELAY_S", "0"))
        original_init(self, *args, **kwargs)
    Daemon.__init__ = observed_init
'''


class CLIResult(NamedTuple):
    rc: int
    stdout: str
    stderr: str


class E2E:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.prompt = self.root / "prompt.md"
        self.prompt.write_bytes(b"Produce the fixture deliverable.\n")
        self.out = self.workdir / "out.md"
        self.process: subprocess.Popen | None = None
        self.logs = []
        shutil.copyfile(REPO / "subfleet/default_policy.json", self.root / "policy.json")
        # C-11.7's reserve rule is exercised by tests/e2e/test_reserve.py; every other case
        # here describes admission mechanics the rule sits on top of, so it starts off.
        self.policy_update(lambda policy: policy.setdefault("reserve", {}).update(models=[]))
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        for provider in ("codex", "claude"):
            (binary_dir / provider).symlink_to(REPO / "tests/bin" / provider)
        (binary_dir / "python3").symlink_to(sys.executable)
        observer_dir = self.root / "observers"
        observer_dir.mkdir()
        (observer_dir / "sitecustomize.py").write_text(OBSERVERS)
        user_home = self.root / "user-home"
        projects = user_home / ".claude/projects"
        projects.mkdir(parents=True)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("SUBFLEET_", "CLAUDE", "CODEX", "ANTHROPIC", "GIT_"))}
        self.env.update({
            "HOME": str(user_home), "SUBFLEET_HOME": str(self.root),
            "PATH": os.pathsep.join((str(binary_dir), "/usr/sbin", "/sbin", "/bin",
                                     os.environ.get("PATH", "/usr/bin"))),
            "PYTHONPATH": os.pathsep.join((str(observer_dir), str(REPO))),
            "CLAUDE_CODE_SESSION_ID": "e2e-caller", "CLAUDECODE": "1",
            "CODEX_API_KEY": "e2e-codex-key-must-be-removed",
            "OPENAI_API_KEY": "e2e-openai-key-must-be-removed",
            "ANTHROPIC_API_KEY": "e2e-anthropic-key-must-be-removed",
            "E2E_CLAUDE_TOKEN_1": "fake-subscription-token-1",
            "E2E_CLAUDE_TOKEN_2": "fake-subscription-token-2",
            "CLAUDE_FAKE_PROJECTS_DIR": str(projects),
            "SUBFLEET_FAKE_DIAGNOSTICS_PATH": str(self.root / "codex-env.json"),
            "SUBFLEET_FAKE_ENV_REPORT": str(self.root / "claude-env.json"),
            "SUBFLEET_FAKE_STDIN_REPORT": str(self.root / "claude-stdin.md"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Subfleet E2E", "GIT_AUTHOR_EMAIL": "e2e@example.test",
            "GIT_COMMITTER_NAME": "Subfleet E2E", "GIT_COMMITTER_EMAIL": "e2e@example.test",
        })
        self.init_repo()
        lanes = []
        for number in (1, 2):
            home = self.root / f"codex-{number}"
            home.mkdir()
            # Unsigned fixture JWT: the real enroll parser accepts paid-plan and
            # account claims. No enrollment/probe HTTP request runs in this suite.
            claims = {"https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus", "chatgpt_account_id": f"fake-{number}"}}
            token = "fixture." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fixture"
            (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
                "access_token": token, "account_id": f"fake-{number}",
            }}))
            lanes.extend([
                {"lane_id": f"codex-{number}", "provider": "codex",
                 "account_key": f"codex:fake-{number}", "credential_kind": "home",
                 "credential_ref": str(home), "home": str(home), "owner": "v2"},
                # C-1.4, C-10.6: a Claude lane records the identity its own
                # credential reports; the fake profile endpoint agrees, unless a
                # test sets SUBFLEET_FAKE_PROFILE to make one credential lie.
                {"lane_id": f"claude-{number}", "provider": "claude",
                 "account_key": f"claude:{claude_identity(number)[0]}",
                 "credential_kind": "env", "owner": "v2",
                 "credential_ref": f"E2E_CLAUDE_TOKEN_{number}",
                 "identity": claude_identity(number)[0],
                 "label": claude_identity(number)[1],
                 "identity_status": "verified"},
            ])
        (self.root / "lanes.json").write_text(json.dumps(lanes))

    def setup_token_lane(self, number: int) -> None:
        """C-10.6: re-record one Claude lane as a setup-token enrolment.

        Such a lane has the operator's label and no identity, because the token
        it holds cannot ask the profile endpoint who it is (403).
        """
        path = self.root / "lanes.json"
        lanes = json.loads(path.read_text())
        for lane in lanes:
            if lane["lane_id"] == f"claude-{number}":
                lane.update(identity=None, identity_status="enrolled",
                            account_key=f"claude:{lane['label']}")
        path.write_text(json.dumps(lanes))

    def init_repo(self):
        """C-6.5, C-13.1: use a disposable feature branch with a salvage baseline."""
        if (self.workdir / ".git").exists():
            return
        subprocess.run(["git", "init", "-b", "feature/e2e"], cwd=self.workdir,
                       env=self.env, check=True, capture_output=True, text=True)
        (self.workdir / "tracked.txt").write_text("baseline\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.workdir,
                       env=self.env, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "C-13.1 disposable e2e baseline"],
                       cwd=self.workdir, env=self.env, check=True, capture_output=True, text=True)

    def start(self, scenario="success", delay_s=0, env=None):
        assert self.process is None or self.process.poll() is not None
        daemon_env = {**self.env, "SUBFLEET_FAKE_SCENARIO": scenario,
                      "SUBFLEET_FAKE_DELAY_S": str(delay_s), **(env or {})}
        log = (self.root / f"daemon-{len(self.logs)}.log").open("wb")
        self.logs.append(log)
        executable = Path(sys.executable).parent / "subfleetd"
        assert executable.is_file(), "run uv sync --group dev to install subfleetd"
        self.process = subprocess.Popen(
            [str(executable), "--foreground", "--state-root", str(self.root)],
            env=daemon_env, cwd=REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=log)

        def ready():
            assert self.process.poll() is None, self.log_text()
            result = self.cli("daemon", "status", "--json")
            if result.rc != 0:
                return False
            status = json.loads(result.stdout)
            return status.get("ping") and (status.get("lock") or {}).get("pid") == self.process.pid

        self.until(ready)
        return self

    def policy_update(self, change):
        """Edit policy.json in place before the daemon starts; `change(policy)` mutates it."""
        path = self.root / "policy.json"
        policy = json.loads(path.read_text())
        change(policy)
        path.write_text(json.dumps(policy, indent=2) + "\n")

    def enable_reserve(self, *, probe_interval_s=1):
        """C-11.7 on, with a fast probe cycle so the usage sensor reads within a test."""
        def change(policy):
            policy["reserve"] = {**policy.get("reserve", {}), "models": ["fable"], "usage_spacing_s": 0}
            policy.setdefault("timers", {})["probe_interval_s"] = probe_interval_s
        self.policy_update(change)

    def cli(self, *argv, timeout=20):
        result = subprocess.run([sys.executable, "-m", "subfleet.cli", *map(str, argv)],
                                cwd=REPO, env=self.env, input="", text=True,
                                capture_output=True, timeout=timeout)
        return CLIResult(result.returncode, result.stdout, result.stderr)

    def run_args(self, model="astra", *extra):
        return ["run", "-m", model, "-C", str(self.workdir), "-p", str(self.prompt),
                "--allow-tmp", *map(str, extra)]

    def rows(self, sql, params=()):
        with sqlite3.connect(f"file:{self.root / 'state.sqlite3'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def job(self, job_id):
        return self.rows("SELECT * FROM jobs WHERE job_id=?", (job_id,))[0]

    def attempts(self, job_id):
        return self.rows("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (job_id,))

    def show(self, job_id):
        result = self.cli("runs", "show", job_id, "--json")
        assert result.rc == 0, result
        return json.loads(result.stdout)

    def until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        raise AssertionError(f"condition timed out after {timeout}s\n{self.log_text()}")

    def log_text(self):
        return "\n".join(Path(log.name).read_text(errors="replace") for log in self.logs)

    def crash(self):
        assert self.process is not None and self.process.poll() is None
        self.process.kill()
        self.process.wait(timeout=3)

    def close(self):
        # Unblock a test-held worker before asking the daemon to join workers.
        (self.root / "release-hook").touch()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.crash()
        from subfleet.procs import same_process
        owned = {}
        for path in (self.root / "jobs").glob("*/a*/start.json"):
            with suppress(OSError, ValueError, KeyError):
                start = json.loads(path.read_text())
                owned[start["guardian_pid"]] = start
        # A failed starting test can leave a guardian before its first receipt.
        # Its committed identity is still sufficient to clean up our process.
        with suppress(sqlite3.Error):
            for row in self.rows("SELECT guardian_pid,pgid,boot_id,proc_start FROM attempts "
                                 "WHERE guardian_pid IS NOT NULL"):
                owned.setdefault(row["guardian_pid"], row)
        for pid, start in owned.items():
            if same_process(pid, start["boot_id"], start["proc_start"]) is True:
                with suppress(ProcessLookupError):
                    os.killpg(start["pgid"], signal.SIGKILL)
                # The recorded guardian can still precede setsid on a launch
                # failure; only signal its pid after rechecking its identity.
                if same_process(pid, start["boot_id"], start["proc_start"]) is True:
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
        for log in self.logs:
            log.close()


@pytest.fixture(scope="session")
def e2e_process_inspection():
    from subfleet.procs import InspectionError, boot_id, proc_start
    try:
        boot_id()
        if not proc_start(os.getpid()):
            pytest.skip("C-5.3 e2e requires a visible current process from ps")
    except InspectionError as exc:
        pytest.skip(f"C-5.3 e2e requires permitted ps/sysctl inspection: {exc}")


@pytest.fixture
def e2e(request, e2e_process_inspection):
    # macOS AF_UNIX's sun_path cannot fit pytest's usual temporary root.
    with tempfile.TemporaryDirectory(prefix="sf-e2e-", dir="/tmp") as directory:
        harness = E2E(Path(directory))
        try:
            yield harness
        finally:
            harness.close()
            # Keep the state root of a failed test so daemon logs, receipts and the
            # store (with any quarantine census) can be read afterwards.
            report = getattr(request.node, "rep_call", None)
            if report is not None and report.failed:
                import re, shutil, sys
                keep = Path("/tmp/sf-failed") / re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
                shutil.rmtree(keep, ignore_errors=True)
                shutil.copytree(directory, keep, symlinks=True, ignore_dangling_symlinks=True)
                print(f"\n[e2e harness] kept state root at {keep}", file=sys.stderr)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "rep_" + report.when, report)
