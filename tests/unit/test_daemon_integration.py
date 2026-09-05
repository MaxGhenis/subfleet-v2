"""Integration of lane-owned guard and credential seams without real providers."""
import gc
import fcntl
import json
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from subfleet import daemon as module
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon


def lane(home):
    return Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                str(home), LaneOwner.V2, False)


def test_c14_2_daemon_consumes_guard_preflight_and_passes_override(tmp_path, monkeypatch):
    """C-14.2, C-14.3 daemon preflight supplies the exact reviewed adapter override."""
    guard = ModuleType("subfleet.guard")
    preflight = ModuleType("subfleet.guard.preflight")
    calls = []
    def check(binary, **kwargs):
        calls.append((binary, kwargs))
        return SimpleNamespace(ok=True, override="hooks=reviewed", message="ok", fix=None)
    preflight.preflight = check
    monkeypatch.setitem(sys.modules, "subfleet.guard", guard)
    monkeypatch.setitem(sys.modules, "subfleet.guard.preflight", preflight)
    adapter = SimpleNamespace(codex_bin="codex")
    assert Daemon._guard_override(adapter, lane(tmp_path), str(tmp_path)) == "hooks=reviewed"
    assert calls == [("codex", {"home": str(tmp_path), "workdir": str(tmp_path)})]
    preflight.preflight = lambda *args, **kwargs: SimpleNamespace(ok=False, override=None,
        message="trust hash mismatch", fix="restore TRUST")
    with pytest.raises(AdapterError, match="trust hash mismatch") as error:
        Daemon._guard_override(adapter, lane(tmp_path), str(tmp_path))
    assert error.value.code == 7 and error.value.fix == "restore TRUST"


def test_c6_5_api_key_home_is_refused_without_exposing_its_value(tmp_path):
    """C-6.5, C-10.5 API-key homes are refused with code 7 and a value-free error."""
    secret = "test-value-never-store-this"
    (tmp_path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": secret}))
    with pytest.raises(AdapterError) as error:
        Daemon._validate_home(lane(tmp_path))
    assert error.value.code == 7
    assert secret not in str(error.value)
    assert "subscription" in error.value.fix


def test_c5_8_failed_identity_initialization_releases_flock(tmp_path, monkeypatch):
    """C-5.3, C-5.8 failed identity inspection cannot strand the singleton lock."""
    def unavailable():
        raise module.procs.InspectionError("test inspection failure")
    monkeypatch.setattr(module.procs, "boot_id", unavailable)
    with pytest.raises(module.procs.InspectionError):
        Daemon(tmp_path)
    gc.collect()
    fd = os.open(tmp_path / "daemon.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
