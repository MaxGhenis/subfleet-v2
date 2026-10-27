"""A fake daemon a test started never outlives that test (#250 fix round 2c).

2026-10-27: round 2b's provider exited during a hold at load 205, and its pytest went
with it before any teardown ran. The fake daemon `test_quar_start1_daemon.py` had started
in a session of its own (`isolated_group`) ran on with ppid 1, and the job was quarantined
for the writer it left. Two things now stop it:

1. `tests/fake/run_daemon.py`'s `ParentWatch`: once the test process that started it is
   gone, the daemon stops, and the guardians it recorded are ended by identity. Each case
   here starts a helper process standing in for that test, kills it with SIGKILL (no
   teardown can run), and requires the daemon, and what it started, to be gone within a
   few seconds: a daemon that is serving with a slow guardian still starting; one killed
   before it answered, so the watch may begin after its parent is gone (the harness
   passes its pid); and the real guardian that `tests/guardian_steps.py` pauses for the
   D-QUAR-START1 process tests.
2. The harness's teardown (`tests/fake/conftest.py`'s `daemon` fixture), on a failed test
   and on KeyboardInterrupt, run in a scratch pytest session: the daemon and the starting
   guardian it recorded are gone once the session returns.

Every process here is started by the test and stopped by its recorded identity on
failure too.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from subfleet import procs
from tests import failed_keep
from tests.fake.run_daemon import PARENT_GONE_EXIT_S

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="C-5.3's process identity reads macOS state")
pytest_plugins = ["pytester"]
REPO = Path(__file__).resolve().parents[2]
#: A stop takes the watch's look (0.2 s), the serve loop's (0.2 s) and the daemon's close;
#: past `PARENT_GONE_EXIT_S` the watch ends the process itself.
GONE_WITHIN_S = PARENT_GONE_EXIT_S + 5

#: The test process the daemon belongs to, as a helper: it starts what `stage` names,
#: records the identities to `out`, then waits to be killed (at most 300 s).
HELPER = textwrap.dedent('''
    import json, os, subprocess, sys, time
    from pathlib import Path
    from subfleet import procs
    from tests.fake.conftest import Harness

    root, out, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    # The guardian sleeps 60 s before its start.json, inside a 300 s start grace: it is still
    # starting when the test ends, whatever a census under load reads.
    SLOW_START = ("--start-delay", "60", "--start-grace-s", "300")

    def identity(pid):
        found = procs.identity(pid)
        return [found.pid, found.boot_id, found.proc_start]

    record = {}
    if stage in ("serving", "starting"):
        harness = Harness(root)
        if stage == "starting":
            harness.until = lambda *args, **kwargs: True      # back as soon as it is spawned
        harness.start(*SLOW_START, isolated_group=True)
        record["daemon"] = identity(harness.process.pid)
        if stage == "serving":
            job = harness.submit("ok", max_attempts=1)
            row = harness.until(lambda: next((a for a in harness.attempts(job) if a["guardian_pid"]), None),
                                timeout=60)
            record["guardian"] = [row["guardian_pid"], row["boot_id"], row["proc_start"]]
    else:
        steps, adir = root / "steps", root / "a1"
        steps.mkdir()
        gate_r, gate_w = os.pipe()
        env = {**os.environ, "GUARDIAN_STEPS_DIR": str(steps), "GUARDIAN_STEPS_PARENT": str(os.getpid())}
        guardian = subprocess.Popen(
            [sys.executable, "-m", "tests.guardian_steps", "--attempt-dir", str(adir), "--cwd", str(root),
             "--stdout-path", str(adir / "stdout"), "--stderr-path", str(adir / "stderr"),
             "--launch-fd", str(gate_r), "--", "/bin/sh", "-c", 'touch "$0"', str(root / "ran")],
            env=env, pass_fds=(gate_r,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        os.close(gate_r)
        os.write(gate_w, b"1")
        os.close(gate_w)
        deadline = time.monotonic() + 60
        while not (steps / "1.read-abandon").exists():
            assert guardian.poll() is None and time.monotonic() < deadline, guardian.returncode
            time.sleep(.02)
        record["daemon"] = identity(guardian.pid)
    out.write_text(json.dumps(record))
    time.sleep(300)
''')


def alive(found: list | None) -> bool:
    return bool(found) and procs.same_process(found[0], found[1], found[2])


def end(found: list | None, *, group: bool = False) -> None:
    """KILL a process (or its group) this test started, by its recorded identity."""
    if alive(found):
        if group:
            procs.signal_group(found[0], signal.SIGKILL, boot_id=found[1], proc_start=found[2])
        else:
            procs.signal_process(procs.ProcessIdentity(*found), signal.SIGKILL)


def gone_within(found: list, limit: float) -> float:
    began = time.monotonic()
    while alive(found):
        assert time.monotonic() - began < limit, f"pid {found[0]} outlived its test process by {limit} s"
        time.sleep(.05)
    return time.monotonic() - began


@pytest.mark.parametrize("stage", ["serving", "starting", "guardian-steps"])
def test_what_a_test_started_is_gone_soon_after_that_test_process_is_killed(tmp_path, stage,
                                                                             process_inspection_available):
    helper_path = tmp_path / "helper.py"
    helper_path.write_text(HELPER)
    out = tmp_path / "record.json"
    # AF_UNIX on macOS has a 104-byte path limit; pytest's default temp path is longer.
    with tempfile.TemporaryDirectory(prefix="sf-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        helper = subprocess.Popen([sys.executable, str(helper_path), str(root), str(out), stage], cwd=REPO,
                                  env={**os.environ, "PYTHONPATH": str(REPO)}, stdin=subprocess.DEVNULL)
        record: dict = {}
        try:
            deadline = time.monotonic() + 120
            while not out.exists():
                assert helper.poll() is None, f"the helper exited {helper.returncode} before recording"
                assert time.monotonic() < deadline, "the helper recorded nothing within 120 s"
                time.sleep(.05)
            time.sleep(.05)
            record = json.loads(out.read_text())
            assert alive(record["daemon"])
            if "guardian" in record:
                assert alive(record["guardian"])
            helper.kill()                                    # SIGKILL: no teardown, no finally
            helper.wait(timeout=30)
            took = gone_within(record["daemon"], GONE_WITHIN_S)
            print(f"{stage}: gone {took:.2f} s after its test process was killed")
            if "guardian" in record:
                # The guardian was still starting (`--start-delay 60`): no start.json, so only
                # the store recorded it, and `end_attempt_groups` found it there.
                gone_within(record["guardian"], 5)
                assert not list(root.glob("jobs/*/a1/start.json"))
            if stage == "guardian-steps":
                assert not (root / "ran").exists(), "the paused guardian spawned its provider"
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait(timeout=30)
            end(record.get("guardian"), group=True)
            end(record.get("daemon"), group=stage == "guardian-steps")


#: A scratch pytest session with the real harness, whose test fails or is interrupted
#: once its daemon is serving and a slow guardian is starting.
CONFTEST = '''
from tests.fake.conftest import daemon, process_inspection_available, pytest_runtest_makereport  # noqa: F401
'''
INNER = '''
import json
import os
from pathlib import Path

from subfleet import procs


def serving(harness):
    # The guardian sleeps 60 s before its start.json, inside a 300 s start grace.
    harness.start("--start-delay", "60", "--start-grace-s", "300", isolated_group=True)
    job = harness.submit("ok", max_attempts=1)
    row = harness.until(lambda: next((a for a in harness.attempts(job) if a["guardian_pid"]), None), timeout=60)
    found = procs.identity(harness.process.pid)
    Path(os.environ["PARENT_WATCH_RECORD"]).write_text(json.dumps({
        "daemon": [found.pid, found.boot_id, found.proc_start],
        "guardian": [row["guardian_pid"], row["boot_id"], row["proc_start"]]}))


def test_fails(daemon):
    serving(daemon)
    assert False, "deliberate failure"


def test_interrupted(daemon):
    serving(daemon)
    raise KeyboardInterrupt
'''


@pytest.mark.parametrize("ending", ["fails", "interrupted"])
def test_the_harness_stops_its_daemon_and_guardians_on_a_failed_or_interrupted_test(
        pytester, monkeypatch, tmp_path, ending, process_inspection_available):
    monkeypatch.setattr(failed_keep, "ROOT", tmp_path / "kept")
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("PARENT_WATCH_RECORD", str(record_path))
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_scratch_parent_watch=INNER)
    record: dict = {}
    try:
        run = pytester.inline_run("-p", "no:cacheprovider", "-q", "-k", f"test_{ending}", no_reraise_ctrlc=True)
        record = json.loads(record_path.read_text())
        reports = [report for report in run.getreports("pytest_runtest_logreport") if report.when == "call"]
        if ending == "fails":
            assert [report.outcome for report in reports] == ["failed"], reports
        else:
            assert run.ret == pytest.ExitCode.INTERRUPTED, run.ret
        # The session has returned: its teardown stopped the daemon, and ended the
        # guardian the store recorded as starting (it has no start.json).
        assert not alive(record["daemon"]), "the daemon outlived its test's teardown"
        gone_within(record["guardian"], 5)
    finally:
        end(record.get("guardian"), group=True)
        end(record.get("daemon"))
