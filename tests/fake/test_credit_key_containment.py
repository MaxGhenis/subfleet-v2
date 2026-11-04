"""C-31.17, invariant 6 at the process level: no exec block of a credit attempt holds the key.

A real daemon (`tests.fake.run_credit_daemon`: the fake daemon with the real credit lane
adapter) resolves a recognizable fake key through a stand-in agent-keychain helper and a
`claude-credits` registry (test overrides, both outside the state root), then launches
a real guardian and the real harness against a local fake Messages API. The hub decided
on 2026-11-04 that the key travels only over pipes: the launch gate to the guardian, a
pipe of its own to the harness.

- `test_no_process_of_a_credit_attempt_holds_the_key`: while the attempt runs (the fake
  holds its second request), the test reads the exec block, argv and environment
  (`KERN_PROCARGS2`), of the daemon, the guardian, the harness, its shell, a background
  process the shell started, and every other process below the daemon. No 12-character
  run of the key appears in any. macOS 26 returns a platform binary's block without its
  environment (`proc_blocks.environment`), so the shell's environment, which the
  background process inherits, is read from inside the shell (`env`) as well. The
  guardian holds no pipe once its provider runs. When the job has ended, no file under
  the state root holds a run of the key, the store included.
- `test_the_publish_hook_self_test_holds_no_key`: a review's launch first runs its
  publish hook's self-test (C-12.4). The runner writes that process's exec block, and its
  descendants', before the hook reads its payload; no run of the key is in any.

Both fail on da2eb8b56, whose daemon put the key in the guardian's environment and the
self-test's (docs/reports/credit-lane-integration-report.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from subfleet import home_paths
from tests.fake import proc_blocks
from tests.fake.anthropic_api import FakeAnthropic, Reply, bash, text
from tests.fake.conftest import REPO, TERMINAL, Harness

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="reads exec blocks with sysctl KERN_PROCARGS2")

EMAIL = "plan@example.org"
ORG = "00000000-0000-4000-8000-00000000000a"
LANE = {"lane_id": "claude-api-1", "provider": "claude-api", "account_key": f"claude-api:{EMAIL}",
        "credential_ref": f"claude-api-{EMAIL}", "credential_kind": "keychain-token", "credential_epoch": 1,
        "home": None, "owner": "v2", "desktop": False, "enabled": True, "identity": ORG, "label": EMAIL,
        "credit_usd": 200}
FINISH_S = 240


class CreditHarness(Harness):
    """`Harness` with `tests.fake.run_credit_daemon` as the daemon."""

    def start(self, *options: str) -> "CreditHarness":
        log = (self.root / f"harness-{len(self.logs)}.log").open("wb")
        self.logs.append(log)
        env = {**os.environ, "PYTHONPATH": str(REPO), "SUBFLEET_HOME": str(self.root)}
        # C-31.11: the harness honors the fake's URL only in a pytest session's process
        # tree; outside one it would send to api.anthropic.com. The runner checks again.
        assert home_paths.under_pytest(env), "the daemon would not run in this test session"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.fake.run_credit_daemon", "--state-root", str(self.root),
             "--parent-pid", str(os.getpid()), *options],
            cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)

        def ready():
            if self.process.poll() is not None:
                raise AssertionError(f"daemon exited {self.process.returncode}: {self.log_text()}")
            try:
                return self.request("daemon.status")["ok"]
            except (OSError, ValueError):
                return False
        self.until(ready, timeout=60)
        return self

    def done(self, job_id: str) -> dict:
        return self.until(lambda: (job := self.job(job_id))["state"] in TERMINAL and job, timeout=FINISH_S)


@dataclass
class World:
    harness: CreditHarness
    key: str
    keys: Path


@pytest.fixture
def world(monkeypatch, process_inspection_available):
    """A state root with one enrolled credit lane, and the stand-in keychain item, helper
    and registry its key resolves through. The key is new for each test."""
    key = "sk-ant-api03-SUBFLEETFAKEKEY" + secrets.token_urlsafe(48) + "-fakeCC"
    keys = Path(tempfile.mkdtemp(prefix="sfk-keys-")).resolve()
    root = Path(tempfile.mkdtemp(prefix="sfk-")).resolve()
    assert len(str(root / "daemon.sock")) < 100, "the daemon's socket path would pass AF_UNIX's limit"
    item = keys / "item"
    item.write_text(key + "\n")
    item.chmod(0o600)
    helper = keys / "agent-secret"
    helper.write_text(f'#!/bin/sh\n[ "$1" = get ] && [ "$2" = "claude-api-{EMAIL}" ] && '
                      f'exec /bin/cat {shlex.quote(str(item))}\nexit 1\n')
    helper.chmod(0o700)
    registry = keys / "registry.json"
    registry.write_text(json.dumps({"version": 1, "accounts": {EMAIL: {
        "fingerprint": "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:16], "org_id": ORG,
        "renews_day": 15}}}))
    monkeypatch.setenv("SUBFLEET_API_PLAN_REGISTRY", str(registry))
    monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", str(helper))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    harness = CreditHarness(root)
    (root / "lanes.json").write_text(json.dumps([LANE]))
    try:
        yield World(harness, key, keys)
    finally:
        try:
            harness.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(keys, ignore_errors=True)


def sent_key(request) -> str | None:
    bearer = request.headers.get("authorization", "")
    return bearer.removeprefix("Bearer ") if bearer.startswith("Bearer ") else request.headers.get("x-api-key")


def role(argv: list[bytes]) -> str:
    words = [os.path.basename(word).decode(errors="replace") for word in argv]
    if "subfleet.guardian" in words:
        return "guardian"
    if "subfleet.api_agent" in words:
        return "harness"
    if "tests.fake.run_credit_daemon" in words:
        return "daemon"
    if words and words[0] == "sleep":
        return "background"
    if "bash" in words[:1] or "sandbox-exec" in words[:1]:
        return "shell"
    return "other:" + (words[0] if words else "?")


def files_holding(root: Path, key: str) -> dict[str, int]:
    found = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            count = proc_blocks.leaks(path.read_bytes(), key)
            if count:
                found[str(path.relative_to(root))] = count
    return found


def test_no_process_of_a_credit_attempt_holds_the_key(world):
    """Invariant 6 and C-31.17 at the process level, mutants (a) and (d)."""
    entered, release = threading.Event(), threading.Event()

    def answer(body, index):
        if index == 0:
            return Reply([bash("t1", 'env > "$TMPDIR/tool-env.txt"; sleep 300 >/dev/null 2>&1 & echo started')],
                         "tool_use")
        entered.set()
        release.wait(FINISH_S)
        return Reply([text("The answer.")])

    seen: dict[str, list[tuple[int, int]]] = {}
    pipes: dict[int, list[int]] = {}
    argvs: dict[str, list[list[bytes]]] = {}
    environments: dict[str, bool] = {}
    with FakeAnthropic(answer, org_id=ORG) as fake:
        world.harness.start("--credit-api-url", fake.url)
        job_id = world.harness.submit(pinned_model="haiku-api")
        try:
            assert entered.wait(FINISH_S), f"the harness never sent its second request\n{world.harness.log_text()}"
            daemon = world.harness.process.pid
            for pid in [daemon, *proc_blocks.descendants(daemon)]:
                data = proc_blocks.block(pid)
                if data is None:
                    continue
                name = role(proc_blocks.argv(data))
                seen.setdefault(name, []).append((pid, proc_blocks.leaks(data, world.key)))
                argvs.setdefault(name, []).append(proc_blocks.argv(data))
                environments[name] = environments.get(name, False) or bool(proc_blocks.environment(data))
                if name == "guardian":
                    pipes[pid] = [fd for fd, kind in proc_blocks.descriptors(pid) if kind == proc_blocks.PIPE]
            # The shell's environment as the shell itself has it, read while the attempt
            # runs (the harness removes its private TMPDIR when it ends).
            dumps = list(world.harness.root.rglob("tool-env.txt"))
            shell_env = dumps[0].read_bytes() if len(dumps) == 1 else None
        finally:
            release.set()
        job = world.harness.done(job_id)
        requests = list(fake.requests)
    leaked = {f"{name} {pid}": count for name, found in seen.items() for pid, count in found if count}
    assert not leaked, f"runs of the key in exec blocks (argv or environment): {leaked}"
    assert shell_env is not None, "the shell's environment was not read"
    assert proc_blocks.leaks(shell_env, world.key) == 0, "runs of the key in the shell's environment"
    assert {"daemon", "guardian", "harness", "shell", "background"} <= set(seen), sorted(seen)
    # Each Python process's block was read whole, environment included.
    assert environments["daemon"] and environments["guardian"] and environments["harness"], environments
    assert b"SUBFLEET_API_LANE=claude-api-1" in shell_env
    assert len(seen["guardian"]) == 1 and len(seen["harness"]) == 1, seen
    # The guardian keeps no pipe once its provider runs: not the gate, and not its end of
    # the key's pipe (C-31.17).
    assert all(found == [] for found in pipes.values()), f"the guardian holds pipes: {pipes}"
    [guardian_argv], [harness_argv] = argvs["guardian"], argvs["harness"]
    assert b"--key-frame" in guardian_argv and b"--key-fd" in harness_argv
    # The key did reach the API, from the harness only, on every request.
    assert len(requests) == 2 and all(sent_key(request) == world.key for request in requests)
    assert job["state"] == "succeeded", (job, world.harness.log_text()[-3000:])
    assert files_holding(world.harness.root, world.key) == {}


def test_the_publish_hook_self_test_holds_no_key(world):
    """C-12.4's self-test runs in the launch's environment (`Daemon._publish_self_test`),
    which never holds the key (C-31.2, C-31.17); mutant (b)."""
    dump = Path(tempfile.mkdtemp(prefix="sfk-dump-")).resolve()
    try:
        with FakeAnthropic([Reply([text("The review.")])], org_id=ORG) as fake:
            world.harness.start("--credit-api-url", fake.url, "--self-test-dump", str(dump))
            job_id = world.harness.submit(pinned_model="haiku-api", task="review")
            job = world.harness.done(job_id)
        blocks = {path.name: path.read_bytes() for path in sorted(dump.glob("self-test-*.bin"))}
    finally:
        shutil.rmtree(dump, ignore_errors=True)
    assert blocks, f"the review's launch ran no publish hook self-test\n{world.harness.log_text()[-3000:]}"
    leaked = {name: count for name, data in blocks.items() if (count := proc_blocks.leaks(data, world.key))}
    assert not leaked, f"runs of the key in the self-test's exec blocks: {leaked}"
    # These are the self-test's own processes: the hook's command, in the launch's environment.
    assert any(b"SUBFLEET_ATTEMPT=" in data and b"publish" in data for data in blocks.values()), sorted(blocks)
    assert job["state"] == "succeeded", (job, world.harness.log_text()[-3000:])
    assert files_holding(world.harness.root, world.key) == {}
