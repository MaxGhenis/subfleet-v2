"""C-13.4: observers arriving after quarantine and after a crash still hold."""

from contextlib import ExitStack
import json
import sys

import pytest

from subfleet import procs, retention, retention_archive as rarch
from tests.unit.test_retention_worktrees import owned  # noqa: F401
from tests.unit.test_retention_worktree_cwd import observer

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS in-process cwd read")


@pytest.mark.real_cwd
@pytest.mark.parametrize("crash", [False, True], ids=["after-move", "resume-quarantined"])
def test_observer_after_move_is_rechecked_before_pruning(owned, monkeypatch, crash):
    store, root, _, worktree = owned
    original = rarch.Retirement.quarantine
    child = None
    class Crash(BaseException):
        pass
    with ExitStack() as stack:
        def move(retirement):
            nonlocal child
            original(retirement)
            assert retirement.state == "quarantined"
            child = stack.enter_context(observer(retirement.q_worktree))
            if crash:
                raise Crash
        monkeypatch.setattr(rarch.Retirement, "quarantine", move)
        monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset({child.pid}) if child else frozenset())
        if crash:
            with pytest.raises(Crash):
                retention.maintenance(store, root, max_jobs=0, holders=lambda *a, **k: {})
            assert json.loads((root / "retention/job/journal.json").read_text())["state"] == "quarantined"
            monkeypatch.setattr(rarch.Retirement, "quarantine", original)
        result = retention.maintenance(store, root, max_jobs=0, holders=lambda *a, **k: {})
        assert result["pruned"] == []
        assert f"pid {child.pid}: cwd in worktree" in result["deferred"]["job"]
        assert worktree.exists() and store.get_job("job")


@pytest.mark.real_cwd
def test_retention_holds_a_worktree_recorded_with_different_case(owned, monkeypatch):
    store, root, _, worktree = owned
    recorded = worktree.with_name(worktree.name.upper())
    if not recorded.is_dir():
        pytest.skip("requires case-insensitive APFS")
    store.update_job("job", worktree=str(recorded))
    with observer(worktree) as child:
        monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset({child.pid}))
        result = retention.maintenance(store, root, max_jobs=0, holders=lambda *a, **k: {})
        assert result["pruned"] == []
        assert f"pid {child.pid}: cwd in worktree" in result["deferred"]["job"]
