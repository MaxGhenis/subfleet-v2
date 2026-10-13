"""D-CEN1: failed reads must not discard a guardian's responsibility lineage."""

from types import SimpleNamespace

import pytest

from subfleet import guardian, procs
from subfleet.daemon import Daemon


@pytest.mark.parametrize("unavailable", [None, procs.InspectionError("self read failed")])
def test_guardian_retries_an_unknown_self_read_before_leaving(monkeypatch, unavailable):
    reads = iter([unavailable, 42, 42, 42])
    members = iter([procs.InspectionError("listing failed"), {60}, frozenset()])
    sleeps = []

    def responsible(pid):
        assert pid == 42
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    def responsible_for(pid):
        assert pid == 42
        value = next(members)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(guardian.os, "getpid", lambda: 42)
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(responsibility=True, responsible=responsible))
    monkeypatch.setattr(procs, "responsible_for", responsible_for)
    monkeypatch.setattr(guardian.time, "sleep", sleeps.append)
    guardian.linger()
    assert len(sleeps) == 3                 # unknown self, failed listing, live escaper
    assert sleeps == sorted(sleeps)
    assert max(sleeps) <= guardian.LINGER_MAX_S


def test_guardian_without_the_spi_leaves_without_a_read(monkeypatch):
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(responsibility=False))
    guardian.linger()


def test_a_failed_root_read_is_not_evidence_that_a_child_is_unrooted(monkeypatch):
    def failed(pid):
        raise procs.InspectionError("responsibility unavailable")
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(responsibility=True, responsible=failed))
    with pytest.raises(procs.InspectionError):
        procs.responsibility_root(42)


def test_a_known_root_with_an_unreadable_unique_id_keeps_its_root(monkeypatch):
    def failed(pid):
        raise procs.InspectionError("unique id unavailable")
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(responsibility=True,
                        responsible=lambda pid: pid, process=failed))
    assert procs.responsibility_root(42) == {"pid": 42, "unique_id": None}


@pytest.mark.parametrize("unavailable", [None, procs.InspectionError("root read failed")])
def test_a_successful_disclaimed_spawn_always_records_a_root(monkeypatch, unavailable):
    child = procs.Spawned(["python"], 42)
    child.returncode = 0                   # synthetic handle: never waitpid or signal a host pid
    monkeypatch.setattr(procs, "spawn_disclaimed", lambda *args, **kwargs: child)

    def root(pid):
        assert pid == child.pid
        if isinstance(unavailable, Exception):
            raise unavailable
        return unavailable
    monkeypatch.setattr(procs, "responsibility_root", root)
    core = SimpleNamespace(_unrooted_logged=False)
    found, recorded = Daemon._spawn_guardian(core, ["python"], {}, 9, "/fixture")
    assert found is child
    assert recorded == {"pid": 42, "unique_id": None}
    assert not core._unrooted_logged


def test_a_fallback_spawn_needs_no_responsibility_metadata(monkeypatch):
    """A missing spawn SPI can coexist with a readable responsibility SPI: fallback
    still starts as before and must not depend on a responsibility read succeeding."""
    child = SimpleNamespace(pid=42)
    monkeypatch.setattr(procs, "spawn_disclaimed", lambda *args, **kwargs: child)
    monkeypatch.setattr(procs, "responsibility_root", lambda pid: pytest.fail("fallback read responsibility"))
    monkeypatch.setattr(procs, "disclaim_available", lambda: False)
    warnings = []
    core = SimpleNamespace(_unrooted_logged=False,
                           log=SimpleNamespace(warning=lambda *args: warnings.append(args)))
    found, recorded = Daemon._spawn_guardian(core, ["python"], {}, 9, "/fixture")
    assert found is child and recorded is None
    assert len(warnings) == 1
