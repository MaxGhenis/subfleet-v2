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


def test_a_fork_and_exit_between_listing_and_read_cannot_prove_empty(monkeypatch):
    listings = iter([{42, 60}, {42, 61}])
    reads = []

    def responsible(pid):
        reads.append(pid)
        return None if pid == 60 else 42

    monkeypatch.setattr(procs, "kernel", SimpleNamespace(pids=lambda: frozenset(next(listings)),
                                                        responsible=responsible))
    assert procs.responsible_for(42) == {61}
    assert reads == [60, 61]


def test_a_responsibility_listing_that_keeps_growing_cannot_prove_empty(monkeypatch):
    listings, reads = [], []

    def pids():
        pid = 60 + len(listings)
        listings.append(pid)
        return frozenset({42, pid})

    monkeypatch.setattr(procs, "kernel", SimpleNamespace(pids=pids,
                       responsible=lambda pid: reads.append(pid)))
    with pytest.raises(procs.InspectionError, match="did not settle"):
        procs.responsible_for(42)
    assert len(listings) == procs.RESPONSIBILITY_ROUNDS
    assert reads == listings


def test_an_empty_responsibility_listing_has_read_every_new_pid(monkeypatch):
    listings = iter([{42, 60}, {42, 60, 61}, {42, 61}])
    reads = []
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(pids=lambda: frozenset(next(listings)),
                       responsible=lambda pid: reads.append(pid)))
    assert procs.responsible_for(42) == frozenset()
    assert reads == [60, 61]


@pytest.mark.parametrize("settle_s", [3.0, 4.0])
def test_linger_polls_at_point_one_until_the_exit_settle_window_passes(monkeypatch, settle_s):
    clock, sleeps, looks = [0.0], [], []
    ended = settle_s - .1

    def members(pid):
        looks.append(clock[0])
        return {60} if clock[0] < ended else frozenset()

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setenv(guardian.LINGER_SETTLE_ENV, str(settle_s))
    monkeypatch.setattr(guardian.os, "getpid", lambda: 42)
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(responsibility=True, responsible=lambda pid: 42))
    monkeypatch.setattr(procs, "responsible_for", members)
    monkeypatch.setattr(guardian.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guardian.time, "sleep", sleep)
    guardian.linger()
    assert looks[0] == 0                     # the first look is immediate
    assert ended <= clock[0] <= settle_s + 1e-9
    assert sleeps and max(sleeps) <= .1


def test_linger_backs_off_only_after_the_exit_settle_window(monkeypatch):
    clock, looks, sleeps = [0.0], [], []

    def members(pid):
        looks.append(clock[0])
        return {60} if clock[0] < 12 else frozenset()

    def sleep(delay):
        sleeps.append((clock[0], delay))
        clock[0] += delay

    monkeypatch.setenv(guardian.LINGER_SETTLE_ENV, "3")
    monkeypatch.setattr(guardian.os, "getpid", lambda: 42)
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(responsibility=True, responsible=lambda pid: 42))
    monkeypatch.setattr(procs, "responsible_for", members)
    monkeypatch.setattr(guardian.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guardian.time, "sleep", sleep)
    guardian.linger()
    assert all(delay <= .1 for at, delay in sleeps if at < 3)
    assert max(delay for _, delay in sleeps) == guardian.LINGER_MAX_S


def test_a_quarantined_lingering_guardian_is_reaped_by_the_next_spawn(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from subfleet import daemon

    @contextmanager
    def transaction(*args, **kwargs):
        yield SimpleNamespace(execute=lambda *args: None)

    monkeypatch.setattr(procs, "_abandoned", [])
    core = SimpleNamespace(root=tmp_path, _read_json=Daemon._read_json,
        store=SimpleNamespace(transaction=transaction),
        _job=lambda job: {"cancel_requested_at": None}, _earlier_attempt=lambda *args, **kwargs: "",
        _notice=lambda *args: None, _notify=lambda: None,
        _children={"job/a1": procs.Spawned(["owned-guardian"], 42)})
    daemon.Daemon._quarantine(core, {"job_id": "job", "attempt_id": "job/a1", "seq": 1},
                             procs.Containment(responsible_pids=frozenset({42, 60})), "still live")
    assert core._children == {}
    assert [child.pid for child in procs._abandoned] == [42]
    waited = []
    monkeypatch.setattr(procs.os, "waitpid", lambda pid, flags: waited.append((pid, flags)) or (pid, 0))
    monkeypatch.setattr(procs._spawn, "available", False)
    monkeypatch.setattr(procs.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=43))
    procs.spawn_disclaimed(["next-guardian"], env={}, cwd="/")
    assert waited == [(42, procs.os.WNOHANG)]
    assert procs._abandoned == []


@pytest.mark.parametrize("responsible", [42, 7, None, procs.InspectionError("unavailable")])
def test_only_a_self_responsible_daemon_disclaims_guardians(monkeypatch, responsible):
    def read(pid):
        assert pid == 42
        if isinstance(responsible, Exception):
            raise responsible
        return responsible

    monkeypatch.setattr(procs, "disclaim_available", lambda: True)
    monkeypatch.setattr(procs.os, "getpid", lambda: 42)
    monkeypatch.setattr(procs, "kernel", SimpleNamespace(responsible=read))
    assert procs.guardian_disclaim_allowed() is (responsible == 42)


def test_a_terminal_started_daemon_keeps_the_popen_privacy_identity(monkeypatch):
    from subfleet import daemon
    calls = []
    monkeypatch.setattr(daemon.subprocess, "Popen",
                        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(pid=60))
    monkeypatch.setattr(procs, "spawn_disclaimed", lambda *args, **kwargs: pytest.fail("disclaimed terminal"))
    core = SimpleNamespace(_disclaim_guardians=False, _unrooted_logged=False, exit_settle_s=4,
                           log=SimpleNamespace(warning=lambda *args: None))
    child, root = Daemon._spawn_guardian(core, ["python", "-m", "subfleet.guardian"], {"SECRET": "env-only"}, 9, "/pkg")
    assert child.pid == 60 and root is None
    assert calls == [(["python", "-m", "subfleet.guardian"], {
        "env": {"SECRET": "env-only", "SUBFLEET_EXIT_SETTLE_S": "4"}, "cwd": "/pkg", "pass_fds": (9,),
        "stdin": daemon.subprocess.DEVNULL, "stdout": daemon.subprocess.DEVNULL,
        "stderr": daemon.subprocess.DEVNULL, "close_fds": True})]
