"""Opt-in pytest guard for offline identity checks; never read live native state."""
import os
from pathlib import Path
import socket
import sys
import tempfile

import pytest

_workspace = Path(__file__).resolve().parents[1]
_protected = tuple(str(Path.home() / name) for name in (".subfleet", ".claude", ".codex"))
_active = False
_patch = None
_home = None


def _guard(event, args):
    if not _active:
        return
    if event in ("open", "os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.path.abspath(os.fsdecode(args[0]))
        if path == str(_workspace) or path.startswith(str(_workspace) + os.sep):
            return
        if any(path == root or path.startswith(root + os.sep) for root in _protected):
            raise AssertionError(f"identity test attempted live native state: {path}")
    if event == "socket.connect" and args[0].family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("identity checks are offline")


sys.addaudithook(_guard)


def pytest_configure(config):
    global _active, _home, _patch
    _home = tempfile.TemporaryDirectory(prefix="identity-native-home-")
    _patch = pytest.MonkeyPatch()
    _patch.setattr(Path, "home", classmethod(lambda cls: Path(_home.name)))
    _active = True


def pytest_unconfigure(config):
    global _active
    _active = False
    if _patch:
        _patch.undo()
    if _home:
        _home.cleanup()
