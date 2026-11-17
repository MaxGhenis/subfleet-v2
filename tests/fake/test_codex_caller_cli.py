"""C-17.6: real CLI processes use a durable daemon's caller-session filters.

The daemon serves its production connection handler and dispatch methods, with
no admission loop or provider launch. Completion is synthetic; this test makes
no claim about guardian survival or operating-system process identity.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

from tests.fake.conftest import REPO
from tests.fake.test_state_contract import state_daemon  # noqa: F401 (fixture)


@pytest.fixture
def tmp_path():
    """Keep this module's state_daemon socket below macOS's pathname limit."""
    directory = Path(tempfile.mkdtemp(prefix="sf-codex-", dir="/tmp"))
    try:
        yield directory
    finally:
        assert directory.parent == Path("/tmp") and directory.name.startswith("sf-codex-")
        assert not directory.is_symlink()
        shutil.rmtree(directory)


@pytest.fixture
def cli_daemon(state_daemon):  # noqa: F811
    service, harness = state_daemon
    # state_daemon deliberately records synthetic process identity. An unknown
    # holder lets the unmodified CLI use the answering socket without inspecting
    # host processes; truncating this test file preserves its inode and flock.
    (harness.root / "daemon.lock").write_bytes(b"")
    stopped = threading.Event()
    errors = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(harness.root / "daemon.sock"))
    listener.listen(16)
    listener.settimeout(.1)

    def serve():
        try:
            while not stopped.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if stopped.is_set():
                        break
                    raise
                service._hold_connection(conn)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve, name="codex-caller-test-server")
    thread.start()
    try:
        yield service, harness
    finally:
        stopped.set()
        listener.close()
        thread.join(timeout=5)
        service.close()  # joins production reader/worker threads; fixture close is idempotent
        assert not thread.is_alive(), "test daemon accept loop did not stop"
        assert not errors, errors


def cli(daemon, session: dict[str, str], *args: str) -> list[dict]:
    # Build a small environment instead of inheriting the test runner's caller
    # identity or lane markers. All session values below are synthetic; HOME and
    # the daemon socket/store stay inside this test's temporary state root.
    env = {
        "PATH": os.defpath,
        "HOME": str(daemon.root / "home"),
        "SUBFLEET_HOME": str(daemon.root),
        "PYTHONPATH": str(REPO),
        **session,
    }
    result = subprocess.run(
        [sys.executable, "-m", "subfleet", *args, "--json"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_c17_6_codex_run_and_mine_keep_thread_and_claude_callers_separate(cli_daemon):
    """Run records a namespaced caller; both --mine verbs resolve it over the socket.

    The Claude caller deliberately uses the first Codex thread's raw id. A lost
    prefix would mingle both callers, even though their providers differ.
    """
    service, daemon = cli_daemon
    sessions = [
        ({"CODEX_THREAD_ID": "synthetic-shared-thread"}, "codex:synthetic-shared-thread"),
        ({"CODEX_THREAD_ID": "synthetic-other-thread"}, "codex:synthetic-other-thread"),
        ({"CLAUDE_CODE_SESSION_ID": "synthetic-shared-thread"}, "synthetic-shared-thread"),
    ]
    jobs = []
    for session, caller in sessions:
        submitted = cli(
            daemon, session, "run", "-m", "astra", "-s", "read-only",
            "-C", str(daemon.workdir), "--allow-tmp", json.dumps({"scenario": "ok"}),
        )
        assert len(submitted) == 1
        receipt = submitted[0]
        assert receipt["detached"] is True
        assert receipt["wait_inline"] is False
        if "CODEX_THREAD_ID" in session:
            assert receipt["reason"] == "inside a Codex session — the job must outlive it"
        job_id = receipt["job_id"]
        assert daemon.job(job_id)["caller_session"] == caller
        jobs.append(job_id)

    # Only caller ownership is under test. Give each submitted job a terminal
    # row and matching notice without starting an admission/guardian process.
    with service.store.transaction() as tx:
        for job_id in jobs:
            tx.execute("UPDATE jobs SET state='succeeded',rc=0,finished_at=created_at WHERE job_id=?",
                       (job_id,))
            service._notice(tx, {"job_id": job_id}, "Synthetic completion for caller filtering.")
    service._notify()

    for (session, caller), job_id in zip(sessions, jobs, strict=True):
        listed = cli(daemon, session, "runs", "--mine")
        assert [job["job_id"] for job in listed] == [job_id]
        assert listed[0]["caller_session"] == caller
        waited = cli(daemon, session, "wait", "--mine", "--timeout", "20")
        assert [job["job_id"] for job in waited] == [job_id]
        assert waited[0]["state"] == "succeeded"
        assert daemon.finished(job_id)["state"] == "succeeded"
