"""C-16.8 against the real `subfleetd` and CLI: a stop answers what it committed.

Review of #43, 2026-09-25: `close()` shut every client socket before it drained
the pools, so a submit that committed during the drain answered into a closed
socket; `subfleet run` printed "no answer from the daemon", re-sent to a daemon
that was gone, and reported the outcome unknown although the job existed.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time

from tests.e2e.conftest import REPO
from tests.e2e.test_stop_bound import SLACK_S, _wait_for_exit_with_lock


def _run_in_background(e2e, *argv):
    """`subfleet run ...` on its own process, so the test can stop the daemon under it."""
    return subprocess.Popen([sys.executable, "-m", "subfleet.cli", *map(str, argv)],
                            cwd=REPO, env=e2e.env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _listener_closed(e2e) -> bool:
    """True once a connect is refused: `close()` has answered its backlog and shut the listener."""
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(str(e2e.root / "daemon.sock"))
        except (ConnectionRefusedError, FileNotFoundError):
            return True
    return False


def test_c16_8_a_submit_that_commits_during_a_stop_reaches_its_client(e2e):
    """C-16.8, C-16.3, C-6.2: the incident shape end to end. The submit has
    committed and is parked before its reply when SIGTERM arrives; it answers
    after the stop has shut the listener, `run` prints the job once with no
    re-send, the daemon exits 0 well inside its bound, and the job then runs."""
    grace_s = 20.0
    e2e.start(env={
        "SUBFLEET_E2E_HOLD_AT": "submitted",            # parks after the commit, before the reply
        "SUBFLEET_E2E_STOP_GRACE_S": str(grace_s),
        "SUBFLEET_E2E_STOP_REPLY_S": "10",
    })
    client = _run_in_background(e2e, *e2e.run_args("astra", "-d"))
    marker = e2e.root / "hook-submitted.json"
    e2e.until(marker.exists)
    job_id = json.loads(marker.read_text())["job_id"]

    old = e2e.process
    signalled = time.monotonic()
    old.send_signal(signal.SIGTERM)
    e2e.until(lambda: "stopping:" in (e2e.root / "daemon.log").read_text(), timeout=grace_s)
    # The stop is past taking work: no new connection is accepted. On `main`
    # before C-16.8 the parked submit's socket was already shut by now.
    e2e.until(lambda: _listener_closed(e2e), timeout=grace_s)
    (e2e.root / "release-hook").touch()

    out, err = client.communicate(timeout=60)
    assert client.returncode == 0, err
    assert out.strip() == job_id
    assert "no answer from the daemon" not in err and "is unknown" not in err, err
    old.wait(timeout=grace_s + SLACK_S)
    assert old.returncode == 0                          # a clean stop, not the C-5.8a bound
    assert time.monotonic() - signalled < grace_s
    log = (e2e.root / "daemon.log").read_text()
    assert "stopping: client replies drained" in log and "still unanswered" not in log, log
    assert "Timeout (" not in log

    # The committed job is real, and the only one for its request id: a fresh
    # daemon runs it to the end.
    e2e.start()
    waited = e2e.cli("wait", job_id, "--timeout", "30", timeout=45)
    assert waited.rc == 0, waited.stderr
    [job] = e2e.rows("SELECT job_id, state FROM jobs WHERE request_id="
                     "(SELECT request_id FROM jobs WHERE job_id=?)", (job_id,))
    assert job == {"job_id": job_id, "state": "succeeded"}


def test_c16_8_a_submit_that_never_answers_ends_at_the_bound_and_is_reported_unknown(e2e):
    """C-16.8, C-5.8a, C-16.3: a submit parked for good after its commit cannot
    hold the stop. Its connection is shut at the reply deadline, so `run` re-sends,
    meets no daemon and reports the outcome unknown with the request id; the
    process ends at its grace with the parked frame in the dump, and the job the
    commit made is there for `--request-id` to settle."""
    grace_s = 5.0
    e2e.start(env={
        "SUBFLEET_E2E_HOLD_AT": "submitted",            # never released
        "SUBFLEET_E2E_STOP_GRACE_S": str(grace_s),
        "SUBFLEET_E2E_STOP_REPLY_S": "1.5",
    })
    client = _run_in_background(e2e, *e2e.run_args("astra", "-d", "--json"))
    marker = e2e.root / "hook-submitted.json"
    e2e.until(marker.exists)
    job_id = json.loads(marker.read_text())["job_id"]

    old = e2e.process
    signalled = time.monotonic()
    old.send_signal(signal.SIGTERM)
    e2e.until(lambda: "stopping:" in (e2e.root / "daemon.log").read_text(),
              timeout=grace_s + SLACK_S)
    _wait_for_exit_with_lock(old, e2e.root / "daemon.lock", signalled + grace_s + SLACK_S,
                             lambda: f"C-5.8a: still running {grace_s + SLACK_S:g} s after SIGTERM\n"
                                     f"{e2e.log_text()}")
    elapsed = time.monotonic() - signalled
    assert grace_s <= elapsed <= grace_s + SLACK_S, elapsed
    assert old.returncode == 1
    log = (e2e.root / "daemon.log").read_text()
    assert "1 request(s) still unanswered" in log, log
    dump = log[log.index("Timeout ("):]
    for frame in (" in hold\n", " in _boundary\n", " in submit\n", " in close\n"):
        assert frame in dump, (frame, dump)

    out, err = client.communicate(timeout=90)
    assert client.returncode == 1, (out, err)
    [entry] = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert entry["outcome"] == "unknown" and entry["job_id"] is None, entry
    [job] = e2e.rows("SELECT job_id, request_id FROM jobs WHERE job_id=?", (job_id,))
    assert entry["request_id"] == job["request_id"]
    assert job["request_id"] in err                    # the settle command names it (C-16.3)
