"""The daemon's side of the delete path (C-8.7, d2660).

The op reaches the path with retention's dormancy. The path steps on the
retention worker: the worker has a turn when the hourly pass or a delete step
is due; a due pass goes first; a delete step runs alone in its turn, under the
lock the pass and the unused-tree sweep take, and leaves the pass's clock
alone; a step that raises ends the request (its type is logged, never its
text) and the turn still sweeps what it was offered. The daemon is built
without `__init__`, as the other partial-daemon tests build it.

d3085: a pass that leaves retention due (a catch-up, a holder-scan failure
with a job waiting for its check 2, a deadline after progress, a raise) owes
the path one step, which runs before the next pass; a pass that sets the
hourly clock owes nothing. So between two passes that run while retention is
due, the path always takes a step. A step taken while the pass is due returns
`DEFERRED`, so the retention key keeps its C-5.10 failure count.
"""
from __future__ import annotations

import concurrent.futures
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
    d._retire_lock = threading.Lock()
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


# d3085: the step a pass that leaves retention due owes the delete path.

class Marks:
    """`timers.mark` as the pass calls it: the key and its keywords."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, key, **kwargs):
        self.calls.append((key, kwargs))


def passing(path, monkeypatch, outcomes, *, due: bool = True) -> tuple[Daemon, list]:
    """A partial daemon whose hourly pass returns (or raises) `outcomes` in turn."""
    d = partial(path, due=due)
    d.timers = SimpleNamespace(cancel=threading.Event(), mark=Marks())
    d._sweep_attempt_tmpdirs = lambda: None
    d._prune_service_notices = lambda: None
    d._retention_state = None
    d.store = d.root = None
    queue = list(outcomes)
    calls: list = []

    def maintenance(*args, **kwargs):
        calls.append(kwargs)
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(daemon_module, "maintenance", maintenance)
    return d, calls


def make_due(d: Daemon) -> None:
    d._last_maintenance = time.monotonic() - (RETENTION_INTERVAL_S + 5)


#: Pass outcomes that leave retention due, as `_retention` reads them, and how the call ends.
LEAVE_DUE = {
    "catch-up": ({"pruned": ["j1"], "more": True}, None),
    "deadline after progress": ({"pruned": ["j1"], "interrupted": "deadline"}, TimeoutError),
    "scan failed, a job waits for check 2": ({"holder_scan_failed": True, "awaiting_check2": ["j1"]}, None),
    "pass raised": (RuntimeError("store busy"), RuntimeError),
}
#: Pass outcomes that set the hourly clock.
HOURLY = {
    "finished": {"pruned": ["j1"]},
    "finished with nothing to do": {},
    "deadline before progress": {"interrupted": "deadline"},
    "cancelled": {"interrupted": "cancelled"},
    "scan failed": {"holder_scan_failed": True},
}


def run_pass(d: Daemon, raises) -> None:
    if raises is None:
        d._retention()
    else:
        with pytest.raises(raises):
            d._retention()


@pytest.mark.parametrize("name", sorted(LEAVE_DUE))
def test_a_pass_that_leaves_retention_due_owes_the_delete_path_one_step_before_the_next_pass(monkeypatch, name):
    outcome, raises = LEAVE_DUE[name]
    path = FakePath()
    d, calls = passing(path, monkeypatch, [outcome, {"pruned": ["j2"]}])
    run_pass(d, raises)                         # retention was due and nothing was owed: the pass runs
    assert len(calls) == 1 and path.steps == []
    make_due(d)                                 # the catch-up or retry clock has come round
    clock = d._last_maintenance
    turn = d._retention()                       # the owed step runs although the pass is due
    assert len(calls) == 1 and len(path.steps) == 1 and path.held == [True]
    assert turn is daemon_module.DEFERRED       # neither the pass's success nor its failure (C-5.10)
    assert d._last_maintenance == clock         # the step leaves the pass's clock alone
    d._retention()                              # one step settles the debt: the due pass goes next
    assert len(calls) == 2 and len(path.steps) == 1


@pytest.mark.parametrize("name", sorted(HOURLY))
def test_a_pass_that_sets_the_hourly_clock_owes_nothing(monkeypatch, name):
    path = FakePath()
    d, calls = passing(path, monkeypatch, [HOURLY[name], {"pruned": ["j2"]}])
    d._retention()
    assert d._retention_delete_owed is False
    assert d.timers.mark.calls[-1][1]["next_due"] > daemon_module.after(RETENTION_INTERVAL_S - 60)
    make_due(d)
    d._retention()                              # a due pass goes first, as before d3085
    assert len(calls) == 2 and path.steps == []


def test_an_owed_step_needs_a_due_delete_path(monkeypatch):
    path = FakePath(pending=False)
    d, calls = passing(path, monkeypatch, [{"pruned": ["j1"], "more": True}, {"pruned": ["j2"]}])
    d._retention()
    make_due(d)
    d._retention()                              # nothing to step: the pass runs
    assert len(calls) == 2 and path.steps == []


def test_a_dormant_daemon_takes_no_owed_step_and_its_pass_settles_the_debt(monkeypatch):
    path = FakePath()
    d, calls = passing(path, monkeypatch, [{"pruned": ["j1"], "more": True}, {"pruned": ["j2"]}])
    d._retention()
    monkeypatch.setenv(RETENTION_DORMANT_ENV, "1")
    make_due(d)
    assert d._retention_delete_turn() is False and path.steps == []
    d._retention_dormant_logged = True          # the dormant warning, logged once, is not under test
    d._retention()                              # the dormant pass sets the hourly clock
    assert d._retention_delete_owed is False and len(calls) == 1
    monkeypatch.delenv(RETENTION_DORMANT_ENV)
    make_due(d)
    d._retention()                              # awake again, nothing is owed: the due pass goes first
    assert len(calls) == 2 and path.steps == []


def test_a_step_that_raises_still_settles_the_debt(monkeypatch, caplog):
    path = FakePath(raises=RuntimeError("step text"))
    d, calls = passing(path, monkeypatch, [{"pruned": ["j1"], "more": True}, {"pruned": ["j2"]}])
    d._retention()
    make_due(d)
    with caplog.at_level(logging.ERROR, logger="retention-delete-daemon-test"):
        d._retention()
    assert path.failed == ["step: RuntimeError"] and d._retention_delete_owed is False
    d._retention()
    assert len(calls) == 2


@pytest.mark.parametrize("seed", range(8))
def test_between_two_passes_run_while_retention_is_due_the_path_takes_a_step(monkeypatch, seed):
    """Any order of pass outcomes, with the delete path pending throughout and
    every turn taken once retention is due (the worst case: a busy worker pool
    that misses every catch-up window): a pass that left retention due is
    always followed by one step, and a pass that set the hourly clock by the
    next pass."""
    import random
    rng = random.Random(seed)
    kinds = [("due", LEAVE_DUE[n][0]) for n in sorted(LEAVE_DUE)] + [("hourly", HOURLY[n]) for n in sorted(HOURLY)]
    plan = [rng.choice(kinds) for _ in range(60)]
    path = FakePath()
    d, calls = passing(path, monkeypatch, [outcome for _, outcome in plan])
    trace: list[str] = []
    for kind, _outcome in plan:
        for _turn in range(3):                  # at most a step and the pass; a stuck debt fails, never hangs
            make_due(d)
            steps, passes = len(path.steps), len(calls)
            try:
                d._retention()
            except (TimeoutError, RuntimeError):
                pass                            # a pass that ends in a raise; a step never raises here
            if len(path.steps) > steps:
                assert len(calls) == passes     # a step's turn runs no pass
                trace.append("step")
                continue
            assert len(calls) == passes + 1
            trace.append("pass:" + kind)
            break
        else:
            pytest.fail(f"no pass ran in three turns: {trace[-6:]}")
    for first, second in zip(trace, trace[1:]):
        if first == "pass:due":
            assert second == "step", trace
        else:
            assert second != "step", trace
    assert len(calls) == len(plan) and trace.count("step") == sum(1 for kind, _ in plan[:-1] if kind == "due")


def test_a_catch_up_leaves_a_window_whose_step_settles_the_debt(monkeypatch):
    """A catch-up sets the pass 5 s out. A turn in that window steps as it did
    before d3085 (a plain step, not a deferred one) and settles the debt."""
    path = FakePath()
    d, calls = passing(path, monkeypatch, [{"pruned": ["j1"], "more": True}, {"pruned": ["j2"]}])
    d._retention()
    assert d._retention_due() and d._retention_delete_owed
    assert d._retention() is None and len(path.steps) == 1 and not d._retention_delete_owed
    make_due(d)
    d._retention()                              # nothing owed: the due pass goes first
    assert len(calls) == 2 and len(path.steps) == 1


class Inline:
    """A worker pool that runs each job at once, so the test sees `_schedule`'s
    C-5.10 bookkeeping."""

    def submit(self, fn, *args):
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def scheduled(path, monkeypatch, outcomes):
    """`passing`, with what `_schedule` reads, offered as the control loop offers it."""
    d, calls = passing(path, monkeypatch, outcomes)
    d.store = SimpleNamespace(generation=0)
    d._busy_lock, d._busy, d.stopping = threading.Lock(), set(), threading.Event()
    d._worker_retry_at, d._worker_failures = {}, {}
    pool = Inline()

    def offer():
        if d._retention_due():
            d._schedule("retention", d._retention, paced=True, pool=pool)
    return d, calls, offer


#: Passes that fail, so `_schedule` holds the key for its C-5.10 retry delay.
FAILING = {
    "deadline after progress": {"pruned": ["j1"], "interrupted": "deadline"},
    "pass raised": RuntimeError("store busy"),
}


@pytest.mark.parametrize("name", sorted(FAILING))
def test_through_the_worker_clock_an_owed_step_keeps_the_failing_pass_backing_off(monkeypatch, caplog, name):
    """C-5.10 through `_schedule`: a failing pass holds the key for its retry
    delay; when the delay has passed, the owed step runs, keeps the failure
    count and frees the key, so the pass runs next and, failing again, waits
    longer; a pass that finishes clears the count."""
    path = FakePath()
    d, calls, offer = scheduled(path, monkeypatch, [FAILING[name], FAILING[name], {"pruned": ["j3"]}])
    with caplog.at_level(logging.CRITICAL, logger="retention-delete-daemon-test"):
        offer()                                 # pass 1 fails
        assert len(calls) == 1 and d._worker_failures["retention"] == 1
        offer()                                 # its retry delay holds the key
        assert len(calls) == 1 and path.steps == []
        d._worker_retry_at["retention"] = 0.0   # the delay has passed
        offer()                                 # the owed step, though the pass is due
        assert len(calls) == 1 and len(path.steps) == 1
        assert d._worker_failures["retention"] == 1 and "retention" not in d._worker_retry_at
        offer()                                 # the pass goes next, at once, and fails again
        assert len(calls) == 2 and d._worker_failures["retention"] == 2
        assert d._worker_retry_at["retention"] - time.monotonic() > daemon_module.worker_retry_delay(1)
        d._worker_retry_at["retention"] = 0.0
        offer()                                 # its owed step
        assert len(calls) == 2 and len(path.steps) == 2
        offer()                                 # a pass that finishes clears the count and owes nothing
        assert len(calls) == 3 and "retention" not in d._worker_failures and not d._retention_delete_owed
