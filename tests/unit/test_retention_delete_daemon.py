"""The daemon's side of the delete path (C-8.7, d2660).

The op reaches the path with retention's dormancy. The path steps on the
retention worker: the worker has a turn when the hourly pass or a delete step
is due; a due pass goes first; a delete step runs alone in its turn, under the
lock the pass and the unused-tree sweep take, and leaves the pass's clock
alone; a step that raises ends the request (its type is logged, never its
text) and the turn still sweeps what it was offered. The daemon is built
without `__init__`, as the other partial-daemon tests build it.
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet import retention_delete as rd
from subfleet.contracts import Exit
from subfleet.daemon import RETENTION_DORMANT_ENV, RETENTION_INTERVAL_S, Daemon
from subfleet.policy import RETENTION_DEFAULTS
from subfleet.store import Store

GOOD = {"ruling": "d-test:2026-11-09:ruled", "jobs": ["job-a", "job-b"], "min_age_days": 14, "min_free_gb": 0}


class FakePath:
    """Stands in for `DeletePath` where only the scheduling is under test."""

    def __init__(self, pending: bool = True, raises: BaseException | None = None):
        self._pending, self.raises = pending, raises
        self.steps: list[dict] = []
        self.failed: list[str] = []
        self.held: list[bool] = []
        self.lock: threading.Lock | None = None

    def pending(self) -> bool:
        return self._pending

    def step(self, **kwargs):
        self.steps.append(kwargs)
        probe: list[bool] = []
        other = threading.Thread(target=lambda: probe.append(self.lock.acquire(blocking=False)))
        other.start()
        other.join()
        if probe[0]:
            self.lock.release()
        self.held.append(not probe[0])          # no other thread could take the lock during the step
        if self.raises is not None:
            raise self.raises

    def fail(self, why: str) -> None:
        self.failed.append(why)


def partial(path, *, due: bool = False, policy: dict | None = None) -> Daemon:
    d = object.__new__(Daemon)
    d._retention_delete = path
    d._last_maintenance = time.monotonic() - (RETENTION_INTERVAL_S + 5 if due else 0)
    d.policy = policy if policy is not None else {}
    d._unrun_lock = threading.Lock()
    d._unrun_dirty = False
    d.timers = SimpleNamespace(cancel=threading.Event())
    d.log = logging.getLogger("retention-delete-daemon-test")
    d._retention_pins = lambda: set()
    d.swept = 0

    def sweep() -> int:
        d.swept += 1
        d._unrun_dirty = False
        return 0
    d._sweep_unrun_worktrees = sweep
    if isinstance(path, FakePath):
        path.lock = d._unrun_lock
    return d


@pytest.fixture(autouse=True)
def awake(monkeypatch):
    monkeypatch.delenv(RETENTION_DORMANT_ENV, raising=False)


@pytest.fixture
def no_pass(monkeypatch):
    """The hourly pass, which must not run in a turn a delete step took."""
    calls: list = []
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **k: calls.append((a, k)) or {})
    return calls


@pytest.mark.parametrize("due,pending,dormant,turn", [
    (False, False, False, False),
    (False, True, False, True),
    (True, False, False, True),
    (True, True, False, True),
    (False, True, True, False),     # a dormant daemon gives the delete path no turn
    (True, True, True, True),       # the pass still has its own (and logs that it is dormant)
])
def test_the_retention_worker_has_a_turn_when_the_pass_or_a_delete_step_is_due(monkeypatch, due, pending, dormant,
                                                                               turn):
    if dormant:
        monkeypatch.setenv(RETENTION_DORMANT_ENV, "1")
    d = partial(FakePath(pending=pending), due=due)
    assert d._retention_delete_ready() is (pending and not dormant)
    assert d._retention_due() is turn


def test_a_delete_step_runs_alone_in_its_turn_under_the_lock_and_leaves_the_pass_clock(no_pass):
    path = FakePath()
    d = partial(path, policy={"retention": {"turn_keep_days": 2, "remote_less_history_bytes": 123}})
    clock = d._last_maintenance
    d._retention()
    assert len(path.steps) == 1 and path.held == [True] and no_pass == []
    assert d._last_maintenance == clock
    assert path.steps[0] == {"pins": d._retention_pins, "turn_keep_s": 2 * 86400.0, "remote_less_history_bytes": 123,
                             "cancel": d.timers.cancel}
    assert path.failed == [] and d.swept == 0


def test_a_delete_step_takes_retentions_budget_from_its_defaults():
    path = FakePath()
    d = partial(path)
    assert d._retention_delete_turn() is True
    assert path.steps[0]["turn_keep_s"] == float(RETENTION_DEFAULTS["turn_keep_days"]) * 86400
    assert path.steps[0]["remote_less_history_bytes"] == int(RETENTION_DEFAULTS["remote_less_history_bytes"])


def test_a_due_pass_goes_first_and_the_step_waits():
    path = FakePath()
    d = partial(path, due=True)
    assert d._retention_delete_turn() is False and path.steps == []


def test_dormant_retention_never_steps_the_delete_path(monkeypatch):
    monkeypatch.setenv(RETENTION_DORMANT_ENV, "1")
    path = FakePath()
    d = partial(path)
    assert d._retention_delete_turn() is False and path.steps == []


def test_nothing_due_takes_no_step():
    path = FakePath(pending=False)
    d = partial(path)
    assert d._retention_delete_turn() is False and path.steps == []


def test_a_step_that_raises_ends_the_request_and_the_turn_still_sweeps(caplog, no_pass):
    path = FakePath(raises=RuntimeError("a secret-looking text that must not reach the log"))
    d = partial(path)
    d._unrun_dirty = True                       # a sweep offered while the step held the lock
    with caplog.at_level(logging.ERROR, logger="retention-delete-daemon-test"):
        d._retention()
    assert path.failed == ["step: RuntimeError"] and d.swept == 1 and no_pass == []
    assert "retention delete: a step failed: RuntimeError" in caplog.text
    assert "secret-looking" not in caplog.text


def test_a_kill_is_not_caught_by_the_turn():
    """Only `Exception` ends the request; a `BaseException` (the daemon going
    down) leaves the journal for the restart, as a real kill does."""
    path = FakePath(raises=KeyboardInterrupt())
    d = partial(path)
    with pytest.raises(KeyboardInterrupt):
        d._retention_delete_turn()
    assert path.failed == []


@pytest.fixture
def real(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    store = Store(root / "state.sqlite3")
    path = rd.DeletePath(store, root, holders=lambda watches, cancel=None: {}, cwd_pids=lambda p: frozenset())
    yield path
    store.close()


def test_the_op_reaches_the_path_and_the_worker_runs_a_dry_run_to_its_end(real, no_pass):
    assert "retention.delete" in protocol.OPS
    d = partial(real)
    assert d.dispatch("retention.delete", {"action": "status"}) == {"apply": None, "dry_run": None, "journals": []}
    started = d.dispatch("retention.delete", {"action": "dry-run", "request": GOOD, "unknown": "ignored"})
    assert started["status"] == "running" and started["mode"] == "dry-run"
    clock = d._last_maintenance
    turns = 0
    while d._retention_due() and turns < 10:
        d._retention()
        turns += 1
    dry = real.status()["dry_run"]
    assert dry["status"] == "done" and turns == 2 and no_pass == []
    # Neither job has a row in this store, so each stops at gate 2 and nothing is touched.
    assert {line["job_id"]: (line["gate"], line["reason"]) for line in dry["results"]} == {
        "job-a": ("finished", "no job row"), "job-b": ("finished", "no job row")}
    assert d._last_maintenance == clock and not (real.root / "retention-delete").exists()


def test_the_op_carries_retentions_dormancy(real, monkeypatch):
    monkeypatch.setenv(RETENTION_DORMANT_ENV, "1")
    d = partial(real)
    with pytest.raises(protocol.ProtocolError) as caught:
        d.dispatch("retention.delete", {"action": "apply", "request": GOOD})
    assert caught.value.reason == "retention-delete-dormant" and caught.value.code == Exit.REFUSED
    assert d.dispatch("retention.delete", {"action": "status"})["apply"] is None
    assert not d._retention_due()


def test_the_op_without_an_action_is_refused_as_invalid_args(real):
    d = partial(real)
    with pytest.raises(protocol.ProtocolError) as caught:
        d.dispatch("retention.delete", {"request": GOOD})
    assert caught.value.code == Exit.INVALID_INPUT
