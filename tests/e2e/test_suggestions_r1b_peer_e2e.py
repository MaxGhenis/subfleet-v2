"""Review r1b of PR 181: the person-only check on `suggestion.start` and `.dismiss`
through a real fixture daemon's socket (C-25.6), the review's probes kept.

Every request targets a fixture daemon in a temporary state root; nothing touches
the installed app or live state.

- From a process with no controlling terminal, with the turn markers set, unset or
  forged, the CLI and a direct socket request are refused (exit 7) and the
  suggestion stays open.
- A turn's child that double-forks, is reparented to launchd, calls `setsid` and
  takes a new pseudo-terminal is refused too. Release/217's D-PB1 change requires
  an admitted app identity or caller ancestry; a terminal grants no permission.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from tests.e2e.conftest import E2E
from tests.e2e.test_conversations import CLIENT, Conversations

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")


@pytest.fixture
def r1b_daemon(e2e_process_inspection):
    with tempfile.TemporaryDirectory(prefix="sf-r1b-peer-", dir="/private/tmp") as folder:
        harness = E2E(Path(folder))
        try:
            harness.start()
            yield harness
        finally:
            harness.close()


def create_suggestion(conv) -> str:
    return conv.call("suggestion.create", request_id=str(uuid.uuid4()), title="Review fixture",
                     tldr="Local fixture only", brief="Write only the fixture deliverable.",
                     workspace=str(conv.e2e.workdir))["suggestion"]["suggestion_id"]


@pytest.mark.parametrize("marker", ["set", "unset", "forged"])
@pytest.mark.parametrize("verb", ["start", "dismiss"])
def test_r1b_a_caller_with_no_terminal_is_refused_whatever_its_markers(r1b_daemon, marker, verb):
    harness = r1b_daemon
    conv = Conversations(harness)
    sid = create_suggestion(conv)
    environment = dict(harness.env)
    if marker == "set":
        environment.update(SUBFLEET_TURN_JOB="fixture-job", SUBFLEET_JOB="fixture-job")
    elif marker == "forged":
        environment.update(SUBFLEET_TURN_JOB="some-other-turn", SUBFLEET_JOB="forged-job")
    else:
        for key in [key for key in environment if key.startswith("SUBFLEET_") and key != "SUBFLEET_HOME"]:
            environment.pop(key)
    cli = subprocess.run([sys.executable, "-m", "subfleet.cli", "suggest", "--" + verb, sid, "--json"],
                         capture_output=True, text=True, timeout=30, env=environment, start_new_session=True)
    assert cli.returncode == 7, (cli.stdout, cli.stderr)
    request = {"v": 1, "id": "fixture", "op": "suggestion." + verb, "args": {"suggestion_id": sid}}
    direct = subprocess.run([sys.executable, "-c", CLIENT, str(harness.root / "daemon.sock"), json.dumps(request)],
                            capture_output=True, text=True, timeout=30, env=environment, start_new_session=True)
    response = json.loads(direct.stdout.split("RESPONSE ", 1)[1])
    assert response["ok"] is False and response["error"]["code"] == 7
    assert conv.call("suggestion.list", suggestion_ids=[sid])["suggestions"][0]["state"] == "open"


ORPHAN = r'''
import fcntl, json, os, pty, signal, socket, sys, termios, time
pid = os.fork()
if pid:
    os._exit(0)
signal.alarm(15)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
deadline = time.monotonic() + 5
while os.getppid() != 1 and time.monotonic() < deadline:
    time.sleep(.01)
os.setsid()
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
for descriptor in (0, 1, 2):
    os.dup2(slave, descriptor)
with open(sys.argv[3] + '.pid', 'w') as stream:
    stream.write(str(os.getpid()))
try:
    request = json.loads(sys.argv[2])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(8)
        client.connect(sys.argv[1])
        client.sendall((json.dumps(request) + '\n').encode())
        with client.makefile('rb') as stream:
            response = json.loads(stream.readline())
    result = {'response': response, 'ppid': os.getppid(), 'tty': os.ttyname(slave)}
except BaseException as error:
    result = {'error': repr(error), 'ppid': os.getppid()}
with open(sys.argv[3] + '.partial', 'w') as stream:
    json.dump(result, stream)
os.rename(sys.argv[3] + '.partial', sys.argv[3])
os._exit(0)
'''


@pytest.mark.parametrize("verb", ["start", "dismiss"])
def test_r1b_a_reparented_child_with_its_own_terminal_cannot_decide(r1b_daemon, verb):
    harness = r1b_daemon
    conv = Conversations(harness)
    sid = create_suggestion(conv)
    program = harness.root / "orphan_fixture.py"
    program.write_text(ORPHAN)
    result_path = harness.root / "orphan_result.json"
    request = {"v": 1, "id": "orphan-fixture", "op": "suggestion." + verb, "args": {"suggestion_id": sid}}
    sanitized = {key: value for key, value in harness.env.items() if not key.startswith("SUBFLEET_")}
    launched = subprocess.run([sys.executable, str(program), str(harness.root / "daemon.sock"), json.dumps(request),
                               str(result_path)], env=sanitized, capture_output=True, text=True, timeout=5)
    assert launched.returncode == 0, launched.stderr
    deadline = time.monotonic() + 18
    while not result_path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    # The child has an unconditional 15-second alarm and writes its result just before it exits.
    pid_file = Path(str(result_path) + ".pid")
    try:
        assert result_path.exists(), "the orphan fixture produced no result before its deadline"
        result = json.loads(result_path.read_text())
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            gone_by = time.monotonic() + 3
            while time.monotonic() < gone_by:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(.02)
            else:
                os.kill(pid, signal.SIGKILL)          # never leave the fixture's own child behind
    assert "error" not in result, result
    assert result["response"]["ok"] is False and result["response"]["error"]["code"] == 7, result
