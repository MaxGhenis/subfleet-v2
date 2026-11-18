"""D-TMP1: recording provider processes and tools see the private directory."""

import os
from pathlib import Path
import subprocess
import sys
import time

from subfleet import guardian
from tests.unit.test_guardian import guardian_identity  # noqa: F401


def test_guardian_provider_and_tool_receive_private_tmpdir(tmp_path, monkeypatch, guardian_identity):
    from subfleet.attempt_tmp import AttemptTemp
    record = AttemptTemp(tmp_path / "store").create("job/a1")
    scratch = Path(record["path"])
    monkeypatch.setenv("TMPDIR", str(scratch))
    tool = "import tempfile,sys; assert tempfile.gettempdir()==sys.argv[1]"
    code = ("import os,stat,pathlib,subprocess,sys; p=pathlib.Path(os.environ['TMPDIR']); "
            "assert p.is_dir() and p.stat().st_uid==os.getuid(); "
            "assert stat.S_IMODE(p.stat().st_mode)==0o700; "
            "(p/'provider-output').write_text('dummy'); "
            "subprocess.run([sys.executable,'-c'," + repr(tool) + ",str(p)],check=True); print('OK')")
    assert guardian.run_guardian([sys.executable, "-c", code], attempt_dir=tmp_path, cwd=str(tmp_path),
        stdin_path=None, stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr")) == 0
    assert (tmp_path / "stdout").read_text().strip() == "OK"
    assert (scratch / "provider-output").read_text() == "dummy"


def test_concurrent_provider_processes_observe_distinct_private_tmpdirs(tmp_path):
    from subfleet.attempt_tmp import AttemptTemp
    manager = AttemptTemp(tmp_path / "store")
    release = tmp_path / "release"
    records = [manager.create(f"job-{index}/a1") for index in range(2)]
    assert records[0]["path"] != records[1]["path"]
    code = ("import os,pathlib,time; p=pathlib.Path(os.environ['TMPDIR']); "
            "(p/'ready').write_text('dummy'); "
            "end=time.monotonic()+15; release=pathlib.Path(" + repr(str(release)) + "); "
            "exec('while not release.exists() and time.monotonic()<end: time.sleep(.01)'); "
            "assert release.exists(); assert (p/'ready').read_text()=='dummy'; print('OK')")
    children = []
    try:
        for record in records:
            children.append(subprocess.Popen([sys.executable, "-c", code], cwd=tmp_path,
                env={"PATH": os.defpath, "TMPDIR": record["path"]}, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        deadline = time.monotonic() + 10
        while not all((Path(record["path"]) / "ready").exists() for record in records) and time.monotonic() < deadline:
            time.sleep(.01)
        assert all((Path(record["path"]) / "ready").exists() for record in records)
        assert all(child.poll() is None for child in children)
    finally:
        release.write_text("dummy")
        for child in children:
            try:
                output, error = child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise
            assert child.returncode == 0 and output.strip() == b"OK", error.decode()
