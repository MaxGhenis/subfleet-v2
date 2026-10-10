"""Real app decoders accept the daemon's reported ruling evidence."""
from pathlib import Path
import json
import shutil
import subprocess
import sys

import pytest

from subfleet import protocol
from subfleet.status_json import build_status
from tests.disk_floor_model import Files, LOWER, file, lowering, stamp
from tests.fake.test_admission_disk import enable, fake_clock, non_git_workspace, submit  # noqa: F401
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.frontend.swift import compile_probe

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("xcrun") is None,
                                reason="native Swift validation requires macOS developer tools")


@pytest.fixture(scope="session")
def disk_ruling_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("disk-ruling-swift") / "probe",
                         ROOT / "tests/frontend/DiskRulingProbe.swift", "SUBFLEET_MODEL_TEST")


@pytest.mark.parametrize("fields", [
    {"floor_gb": "Infinity"}, {"release_margin_gb": "Infinity"}, {"why": float("nan")},
    {"why": {"\ud800": [float("inf"), float("-inf"), "\udfff"]}},
    {"ruling": "\ud800"}, {"why": "\ud800"},
    {"why": {"n": 10 ** 309}}, {"why": [-(2 ** 64), 2 ** 53 + 1]},
], ids=["infinite-floor", "infinite-margin", "nan-why", "nested-why", "surrogate-ruling", "surrogate-why",
        "huge-int-why", "past-exact-int-why"])
def test_app_decodes_live_ruling_status_and_why(disk_ruling_probe, state_daemon, monkeypatch, tmp_path, fields):
    daemon, harness = state_daemon
    fake_clock(monkeypatch)
    enable(daemon, monkeypatch, 34)
    daemon.policy["admission"]["disk"].update(lower_path=LOWER, raise_path=None)
    daemon._disk.read_ruling = Files(file(lowering(**fields)))
    job = submit(daemon, harness)
    daemon._admit()
    path = tmp_path / "response.json"
    for op, args in (("daemon.status", {}), ("why", {"job_id": job})):
        result = daemon.dispatch(op, args)
        path.write_bytes(protocol.encode({"v": 1, "id": "review", "ok": True, "result": result}))
        decoded = subprocess.run([str(disk_ruling_probe), str(path), op], check=True,
                                 capture_output=True, text=True, timeout=10)
        assert decoded.stdout.strip() == "decoded"
    status = daemon.dispatch("daemon.status", {})
    snapshot = build_status(status, now=stamp(0))
    # Snapshot ignores disk today; its JSON parser must still accept the field.
    snapshot["disk"] = status["disk"]
    path.write_bytes(json.dumps(snapshot, allow_nan=False, ensure_ascii=False).encode("utf-8"))
    decoded = subprocess.run([str(disk_ruling_probe), str(path), "snapshot"], check=True,
                             capture_output=True, text=True, timeout=10)
    assert decoded.stdout.strip() == "decoded"
