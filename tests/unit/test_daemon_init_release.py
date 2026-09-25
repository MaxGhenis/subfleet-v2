"""A daemon whose construction fails gives back its log descriptor, handler, store and lock.

`main` exits when construction fails, so only an embedded daemon (the tests)
kept them: 20 failed constructions held 20 descriptors, and because the logger
is named by `id()`, a later daemon could inherit the stale handlers and write
into another state root's daemon.log (found by the 2026-09-25 descriptor audit).
"""

import logging
import tempfile
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon
from subfleet.descriptors import open_descriptors
from subfleet.policy import PolicyError


def test_a_failed_construction_releases_its_log_store_and_lock(monkeypatch):
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    with tempfile.TemporaryDirectory(prefix="sfi-", dir="/tmp") as directory:
        root = Path(directory)
        (root / "policy.json").write_text("{not json")
        before = open_descriptors()
        handlers = sum(len(logging.getLogger(name).handlers)
                       for name in list(logging.root.manager.loggerDict) if name.startswith("subfleet.daemon."))
        for _ in range(20):
            with pytest.raises((PolicyError, ValueError)):
                Daemon(root)
        assert open_descriptors() == before
        after = sum(len(logging.getLogger(name).handlers)
                    for name in list(logging.root.manager.loggerDict) if name.startswith("subfleet.daemon."))
        assert after == handlers
        # The lock was released too: a good policy now constructs and closes cleanly.
        (root / "policy.json").unlink()
        service = Daemon(root)
        service.close()
        assert open_descriptors() == before


def test_a_failing_release_step_does_not_hide_why_construction_failed(monkeypatch):
    """The error that made __init__ fail is the one raised, even when a release step fails too."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")

    def seed(self):
        raise RuntimeError("the lane roster is unreadable")
    monkeypatch.setattr(Daemon, "_seed_lanes", seed)
    monkeypatch.setattr(daemon_module.Store, "close", lambda self: (_ for _ in ()).throw(OSError("close failed")))
    with tempfile.TemporaryDirectory(prefix="sfi-", dir="/tmp") as directory:
        before = open_descriptors()
        with pytest.raises(RuntimeError, match="lane roster"):
            Daemon(Path(directory))
        # The store's own close failed, so its three descriptors may stay; the log and the lock did not.
        assert open_descriptors() <= before + 3
        monkeypatch.undo()
        Daemon(Path(directory)).close()                    # the lock was released
