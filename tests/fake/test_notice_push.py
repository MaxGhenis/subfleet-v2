"""C-15.7, C-23.50 end to end: a spawned daemon's own control loop wakes the
idle session a finished job belongs to, and stands aside for a `wait`.

The daemon is a real process (`tests.fake.run_daemon`) with the fake provider;
the session is a registry row in a `~/.claude` this test owns, whose process is
the test's own and whose inbox is a socket the test serves.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.fake.conftest import REPO

SESSION = "6c1d7d2e-4f0b-4b8e-9a51-1d2f3c4b5a69"


class Inbox:
    def __init__(self, path: Path):
        self.path = str(path)
        self.lines: list[dict] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        self.server.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn, conn.makefile("rb") as stream:
                self.lines.extend(json.loads(line) for line in stream if line.strip())

    def frames(self) -> list[dict]:
        return [line for line in self.lines if line.get("type") == "user"]

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.server.close()


@pytest.fixture
def session(daemon, monkeypatch):
    """One idle desktop session in a `~/.claude` the spawned daemon reads, and a
    push policy with no settling delay."""
    home = daemon.root / "claude"
    (home / "sessions").mkdir(parents=True)
    (home / "projects" / "-work").mkdir(parents=True)
    (home / "projects" / "-work" / f"{SESSION}.jsonl").write_text(
        json.dumps({"type": "user", "permissionMode": "default"}) + "\n")
    inbox = Inbox(daemon.root / "inbox.sock")
    pid = os.getpid()
    (home / "sessions" / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": SESSION, "cwd": str(daemon.workdir), "startedAt": 1790995600612,
        "kind": "interactive", "entrypoint": "claude-desktop", "status": "idle",
        "messagingSocketPath": inbox.path}))
    (home / "sessions" / f"{pid}.k.key").write_text(json.dumps({"peerToken": "tok"}))
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home))
    policy = json.loads((daemon.root / "policy.json").read_text())
    policy["notices"] = {"push_delay_s": 0, "push_interval_s": 0.2, "push_after_wait_s": 600}
    (daemon.root / "policy.json").write_text(json.dumps(policy))
    daemon.start()
    yield inbox
    inbox.close()


def test_a_finished_job_wakes_its_idle_session(daemon, session):
    """C-15.7: the job ends, and within a few passes the session's inbox has one
    `later` frame for it, declared `prompting` (the session's own class), and
    the notice is `offered` over `socket`."""
    job = daemon.submit("ok", caller_session=SESSION)
    daemon.finished(job)
    daemon.until(lambda: session.frames(), timeout=15)
    frame, = session.frames()
    assert session.lines[0] == {"type": "auth", "token": "tok"}
    assert (frame["priority"], frame["session_id"]) == ("later", SESSION)
    content = frame["message"]["content"]
    assert content.startswith('<cross-session-message from-name="subfleet" from-mode="prompting">')
    assert f"{job}: succeeded; rc=0;" in content
    notice, = daemon.rows("SELECT state,transport FROM notices WHERE job_id=?", (job,))
    assert (notice["state"], notice["transport"]) == ("offered", "socket")
    time.sleep(1.5)                                     # several more passes
    assert len(session.frames()) == 1, "a notice is pushed at most once"
    status = daemon.call("daemon.status")["notice_push"]
    assert status["pushed"] == 1 and status["enabled"] is True


def test_a_wait_the_session_ran_acknowledges_and_nothing_is_pushed(daemon, session):
    """C-23.50: the session's own `subfleet wait` told it the job ended and
    acknowledged its notice; the push never repeats it."""
    job = daemon.submit("ok", delay_s=1, caller_session=SESSION)
    env = {**os.environ, "PYTHONPATH": str(REPO), "SUBFLEET_HOME": str(daemon.root),
           "CLAUDE_CODE_SESSION_ID": SESSION}
    waited = subprocess.run([sys.executable, "-m", "subfleet", "wait", job, "--timeout", "60"],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=90)
    assert waited.returncode == 0, waited.stderr
    notice, = daemon.rows("SELECT state FROM notices WHERE job_id=?", (job,))
    assert notice["state"] == "acknowledged"
    time.sleep(1.5)
    assert session.frames() == []
