"""Every launch gate the daemon hands a guardian sits above descriptor 2 (C-5.1).

`os.pipe()` returns the lowest free descriptors. In a daemon with fd 0, 1 or 2
closed, a gate's read end took that number; the guardian's /dev/null standard
streams replaced it there, and the guardian read end-of-file although the daemon
had written the gate byte. It exited 127 without starting the provider, so the
attempt, the probe or the re-enrolment never ran (review of 39223c9,
finding 4; the catalog's fence had the same seam, C-30.1).
"""

import json
import os
import subprocess
import sys

import pytest

from tests.fake.conftest import REPO


# The daemon's process, with some of fds 0, 1 and 2 closed (argv[4]) just before one
# launch path (argv[3]) runs through a real guardian and a real provider process (the
# fake provider, or a one-line script for a re-enrolment), reports the descriptors the
# daemon passed each guardian (`pass_fds`) and what the launch produced in argv[2], a
# file opened while all three were still open. It writes nothing to stdout or stderr,
# which may be closed or a gate.
GATE_CHILD = r"""
import json, os, subprocess, sys, traceback, types, warnings
from pathlib import Path
warnings.simplefilter("ignore")
root, launcher = Path(sys.argv[1]), sys.argv[3]
closed = [int(n) for n in sys.argv[4].split(",") if n]
report, result = open(sys.argv[2], "w"), {"pass_fds": []}
daemon = None
try:
    from subfleet import daemon as daemon_module
    from subfleet.adapters import registry
    from subfleet.contracts import attempt_dir
    from subfleet.daemon import AdapterError, Daemon, after, utcnow
    from tests.fake.conftest import Harness
    from tests.fake_adapter import FakeAdapter

    def popen(argv, **kwargs):
        if list(argv)[1:3] == ["-m", "subfleet.guardian"]:
            result["pass_fds"].append(list(kwargs.get("pass_fds", ())))
        return subprocess.Popen(argv, **kwargs)
    # Only the daemon's own launches are observed; they still spawn for real.
    daemon_module.subprocess = types.SimpleNamespace(**{**vars(subprocess), "Popen": popen})

    def held(fd):
        try:
            os.fstat(fd)
        except OSError:
            return False
        return True

    allocate = daemon_module.procs.pipe_above_stdio
    def gate():
        # Which closed descriptors were still free when the gate was made: os.pipe()
        # would have put it there, so the case reaches the hazard.
        result.setdefault("free_at_gate", []).append([fd for fd in closed if not held(fd)])
        return allocate()
    daemon_module.procs.pipe_above_stdio = gate
    registry._factories = {"codex": FakeAdapter, "claude": FakeAdapter}
    daemon_module.capacity.read_desktop_account = lambda: None
    harness = Harness(root)
    daemon = Daemon(root)
    if launcher == "enrollment":
        holder = "probe:timer:enroll:gate"
        daemon.store.acquire_lease("lane:codex-1:slot:0", holder)
        daemon.timers.active_holders.add(holder)
        for fd in closed:
            os.close(fd)
        try:
            done = daemon._enrollment_turn("codex-1", holder, [sys.executable, "-c", "print('provider ran')"],
                                           cwd=str(root), env=dict(os.environ), timeout=30)
            result.update(rc=done.returncode, stdout=done.stdout.strip())
        except AdapterError as exc:
            result.update(rc=None, refused=str(exc))
        daemon.timers.active_holders.discard(holder)
        daemon._recover_probes()
    elif launcher == "probe":
        job_id = daemon.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
        holder, directory = "probe:gate", root / "lanes" / "codex-1" / "probes" / "gate"
        directory.mkdir(parents=True)
        daemon.store.acquire_lease("lane:codex-1:slot:0", holder)
        daemon._save_probe({"holder": holder, "job_id": job_id, "lane_id": "codex-1", "model_id": "gpt-6-astra",
                            "directory": str(directory), "state": "reserved", "created_at": utcnow(),
                            "deadline_at": after(60), "owned_identities": {}})
        for fd in closed:
            os.close(fd)
        outcome = daemon._execute_probe(daemon.store.get_job(job_id), daemon.store.get_lane("codex-1"),
                                        daemon.policy["models"]["astra"], holder)
        result.update(outcome=outcome.cls.value, detail=outcome.detail, rc=outcome.evidence.get("rc"))
    else:
        job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
        daemon._admit()
        a = daemon.store.list_attempts(job_id)[0]
        result["state"] = a["state"]
        for fd in closed:
            os.close(fd)
        daemon._launch(a)
        child = daemon._children.get(a["attempt_id"])
        result["guardian_rc"] = None if child is None else child.wait(60)
        adir = attempt_dir(root, job_id, a["seq"])
        result["start"] = (adir / "start.json").is_file()
        receipt = adir / "exit.json"
        result["rc"] = json.loads(receipt.read_text())["rc"] if receipt.is_file() else None
except BaseException:
    result["error"] = traceback.format_exc()
finally:
    if daemon is not None:
        try:
            daemon.close()
        except BaseException:
            result.setdefault("error", traceback.format_exc())
report.write(json.dumps(result))
report.close()
"""


@pytest.mark.parametrize("closed", [(0,), (1,), (2,)], ids=lambda closed: f"closed-{closed[0]}")
@pytest.mark.parametrize("launcher", ["attempt", "probe", "enrollment"])
def test_a_guardian_starts_its_provider_whatever_standard_stream_the_daemon_has_closed(
        tmp_path, process_inspection_available, launcher, closed):
    """For each daemon path that launches a guardian (a job's attempt, a C-11.4 probe
    and a Claude lane's re-enrolment), with fd 0, 1 or 2 closed in the daemon, the gate
    the guardian inherits sits above 2 and the provider runs. Every set of the three a
    process may have closed is covered for the pipe itself in
    tests/process/test_pipe_above_stdio.py."""
    root, out = tmp_path / "state", tmp_path / "gate.json"
    root.mkdir()
    done = subprocess.run([sys.executable, "-c", GATE_CHILD, str(root), str(out), launcher,
                           ",".join(map(str, closed))],
                          cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=180)
    result = json.loads(out.read_text()) if out.exists() else {}
    assert done.returncode == 0 and result and "error" not in result, (done.returncode, done.stderr, result)
    assert len(result["pass_fds"]) == 1, result
    # Only the gate's read end: a guardian holding the write end too would never read
    # end-of-file there once the daemon died (C-4.2).
    assert len(result["pass_fds"][0]) == 1, result
    assert result["pass_fds"][0][0] > 2, f"the gate would be replaced by a standard stream: {result}"
    assert result["free_at_gate"] == [list(closed)], f"the launch no longer reaches the hazard: {result}"
    if launcher == "enrollment":
        assert (result["rc"], result.get("stdout")) == (0, "provider ran"), result
    elif launcher == "probe":
        assert (result["outcome"], result["rc"]) == ("ok", 0), result
    else:
        assert result["state"] == "reserved", result
        assert (result["guardian_rc"], result["start"], result["rc"]) == (0, True, 0), result
