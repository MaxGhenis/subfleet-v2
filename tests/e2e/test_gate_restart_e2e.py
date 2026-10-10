"""C-15.4 against the real `subfleetd`: a `subfleet gate` outlives a daemon restart.

Defect D-WT1's gate half, 2026-10-10: `subfleet gate` ended with exit 1 at any
lost poll answer or missing socket while its gate went on in the daemon, so an
install or a `launchctl kickstart -k` during a gate woke its session. Here a real
daemon is stopped as launchd stops it (SIGTERM) or dies (SIGKILL) while a real
`subfleet gate plan` polls a gate whose peer round is held by the fake Codex,
and the next daemon is started on the same state root. The next daemon's first
answers to gate ops come before its startup recovery has run ("daemon recovery is
in progress"), which the CLI must ask past too.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("how", ["terminate", "kill"])
def test_c15_4_a_gate_outlives_a_daemon_restart(e2e, how):
    release = e2e.root / "release-peer"
    env = {"SUBFLEET_FAKE_RELEASE_PATH": str(release)}
    e2e.start(scenario="gate-approve", env=env)
    plan = e2e.root / "plan.md"
    plan.write_text("A plan whose gate outlives a daemon restart.\n")
    gate = subprocess.Popen(
        [sys.executable, "-m", "subfleet.cli", "gate", "plan", str(plan), "--peer", "astra",
         "--main-approve", "--expect-sha256", hashlib.sha256(plan.read_bytes()).hexdigest(),
         "-C", str(e2e.root)],
        cwd=REPO, env=e2e.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        # The peer round is running (its fake Codex waits for `release`) and the CLI
        # is polling the gate before the daemon goes.
        job = e2e.until(lambda: next(iter(e2e.rows(
            "SELECT * FROM jobs WHERE kind='gate-review' AND state='running'")), None), timeout=60)
        e2e.until(lambda: e2e.rows("SELECT 1 FROM attempts WHERE job_id=? AND pgid IS NOT NULL",
                                   (job["job_id"],)), timeout=30)
        old = e2e.process
        if how == "terminate":
            old.terminate()                             # what `launchctl kickstart -k` sends
            old.wait(timeout=60)
        else:
            e2e.crash()
        assert gate.poll() is None, gate.communicate()
        e2e.start(scenario="gate-approve", env=env)
        assert e2e.process.pid != old.pid
        release.touch()
        out, err = gate.communicate(timeout=180)
    finally:
        release.touch()
        if gate.poll() is None:
            gate.kill()
            gate.communicate()
    assert gate.returncode == 0, (out, err, e2e.log_text()[-4000:])
    assert "completed" in out, out
    assert err.count("subfleet gate: daemon restarted; still polling gate ") == 1, err
    assert "without a response" not in err and "restart window" not in err, err
    assert "resume with" not in err, err
    jobs = e2e.rows("SELECT * FROM jobs WHERE kind='gate-review'")
    assert [j["job_id"] for j in jobs] == [job["job_id"]], jobs    # one round, one peer job
    gate_dir, = (e2e.root / "gates").iterdir()
    assert (gate_dir / "certificate.json").is_file()
