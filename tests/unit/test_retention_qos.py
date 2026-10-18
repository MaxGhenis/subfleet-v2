"""Retention runs below the operator's apps (final review of e50716e8, N9): every
child it starts (`lsof`, git, the object readers) is under the guardian's
`taskpolicy -c` clamp, and its in-process work lowers only its own thread's
disk I/O policy, which it puts back."""
from __future__ import annotations

import os
import subprocess
import threading

import pytest

from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from subfleet import retention_holders as rholders
from subfleet import retention_qos as rqos
from subfleet.guardian import TASKPOLICY

pytestmark = pytest.mark.skipif(not os.access(TASKPOLICY, os.X_OK), reason="needs taskpolicy(8) (macOS)")


class Spawned(Exception):
    pass


def _capture(monkeypatch, module):
    seen: list[list[str]] = []

    def popen(argv, *args, **kwargs):
        seen.append(list(argv))
        raise Spawned()

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    return seen


def test_every_child_retention_starts_is_clamped(monkeypatch, tmp_path):
    monkeypatch.delenv(rqos.QOS_ENV, raising=False)
    seen = _capture(monkeypatch, rgit)
    with pytest.raises(Spawned):
        rgit.run(["rev-parse", "HEAD"], git_dir=tmp_path)
    with pytest.raises(Spawned):
        rgit.ObjectReader(tmp_path, "sha1")
    holders = _capture(monkeypatch, rholders)
    with pytest.raises(Exception):
        rholders.lsof_holders({"job": rholders.Watch(prefixes=[str(tmp_path)])})
    import subprocess as real
    blob = []
    monkeypatch.setattr(real, "Popen", lambda argv, *a, **k: blob.append(list(argv)) or (_ for _ in ()).throw(Spawned()))
    with pytest.raises(Spawned):
        rarch._BlobReader(tmp_path)
    for argv in seen + holders + blob:
        assert argv[:3] == [TASKPOLICY, "-c", "utility"], argv
    assert seen[0][3] == "git" and holders[0][3].endswith("lsof") and blob[0][3] == "git"


@pytest.mark.parametrize("setting, clamp", [("background", "background"), ("maintenance", "maintenance"),
                                            ("inherit", None), ("nonsense", "utility")])
def test_the_clamp_follows_its_setting(monkeypatch, setting, clamp):
    monkeypatch.setenv(rqos.QOS_ENV, setting)
    expected = [TASKPOLICY, "-c", clamp, "lsof"] if clamp else ["lsof"]
    assert rqos.argv(["lsof"]) == expected


def test_a_clamped_git_runs(monkeypatch, tmp_path):
    """Not only the argv: git really runs under the clamp and answers."""
    monkeypatch.delenv(rqos.QOS_ENV, raising=False)
    subprocess.run(["git", "init", "--quiet", str(tmp_path / "r")], check=True)
    assert rgit.run(["rev-parse", "--is-inside-work-tree"], cwd=tmp_path / "r").stdout.strip() == b"true"


def test_the_threads_disk_io_is_lowered_and_put_back(monkeypatch):
    monkeypatch.delenv(rqos.QOS_ENV, raising=False)
    before = rqos.thread_io_policy()
    assert before is not None
    seen = {}

    def other_thread():
        seen["other"] = rqos.thread_io_policy()

    with rqos.throttled_io():
        assert rqos.thread_io_policy() == rqos.IOPOL_UTILITY
        with rqos.throttled_io():                       # nested: the outer one stands
            assert rqos.thread_io_policy() == rqos.IOPOL_UTILITY
        assert rqos.thread_io_policy() == rqos.IOPOL_UTILITY
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
    assert rqos.thread_io_policy() == before
    assert seen["other"] == before                      # only this thread's
    monkeypatch.setenv(rqos.QOS_ENV, "background")
    with rqos.throttled_io():
        assert rqos.thread_io_policy() == rqos.IOPOL_THROTTLE
    monkeypatch.setenv(rqos.QOS_ENV, "inherit")
    with rqos.throttled_io():
        assert rqos.thread_io_policy() == before


def test_the_archive_and_the_deletion_run_throttled(monkeypatch, tmp_path):
    """The in-process steps (archive, final check, reclaim) run with the
    thread's I/O lowered; the transactions around them do not."""
    monkeypatch.delenv(rqos.QOS_ENV, raising=False)
    from tests.unit.retention_world import World, trust_temporary_directories
    from subfleet import retention
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    try:
        w.job("job-qos")
        policies: dict[str, list] = {}

        def spy(cls, attribute, step):
            real = getattr(cls, attribute)

            def wrapper(self, *args, **kwargs):
                policies.setdefault(step, []).append(rqos.thread_io_policy())
                return real(self, *args, **kwargs)

            monkeypatch.setattr(cls, attribute, wrapper)

        spy(rarch._Builder, "run", "archive")               # inside Retirement.archive
        spy(rarch.Retirement, "_final_check", "final_check")
        spy(rarch.Retirement, "_reclaim", "reclaim")
        spy(rarch.Retirement, "commit", "commit")            # a transaction: not throttled
        before = rqos.thread_io_policy()
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
        assert result["pruned"] == ["job-qos"]
        assert policies == {"archive": [rqos.IOPOL_UTILITY], "final_check": [rqos.IOPOL_UTILITY],
                            "reclaim": [rqos.IOPOL_UTILITY], "commit": [before]}, policies
        assert rqos.thread_io_policy() == before
    finally:
        w.close()
