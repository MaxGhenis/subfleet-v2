"""C-5.10: failure causes use the daemon's safe handler and bounded cadence.

These cores have no store, processes, or background threads. Their inline future
deliberately propagates callback errors, unlike Future's usual callback wrapper,
so rendering a hostile exception cannot silently escape ``done``.
"""

from __future__ import annotations

import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, daemon_log, protocol
from subfleet.daemon import Daemon


BOUNDED = settings(max_examples=24, derandomize=True, database=None, deadline=None)
MARKER = "SECRET7syntheticMarkerValue0123456789"


class InlineFuture:
    def __init__(self, function, *args):
        try:
            self.value, self.error = function(*args), None
        except Exception as exc:
            self.value, self.error = None, exc

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value

    def add_done_callback(self, callback):
        callback(self)


class RecordingFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.records = []

    def filter(self, record):
        self.records.append(record)
        return True


def core():
    """An independent logger writes only through the real daemon handler."""
    service = Daemon.__new__(Daemon)
    service.stream = io.StringIO()
    service.log = logging.Logger("subfleet.daemon.test-worker-cause", logging.ERROR)
    service.log.propagate = False
    handler = daemon_log.ScrubbingStreamHandler(service.stream)
    service.recording = RecordingFilter()
    handler.addFilter(service.recording)
    service.log.addHandler(handler)
    service._busy_lock, service._busy = threading.Lock(), set()
    service._worker_failures, service._worker_retry_at = {}, {}
    service._control_traced, service._request_traced = set(), set()
    service._request_traced_lock = threading.Lock()
    service.stopping = threading.Event()
    service.store = SimpleNamespace(generation=0)
    service.workers = SimpleNamespace(submit=InlineFuture)
    service._run_worker = lambda function, *args: function(*args)
    service.notifications = []
    service._notify = lambda: service.notifications.append(service.store.generation)
    service.marks = []
    service.timers = SimpleNamespace(mark=lambda *args, **kwargs: service.marks.append((args, kwargs)))
    service._end_stream = lambda *args: None
    return service


def fail():
    raise RuntimeError("fixture failure cause")


def schedule(service, function=fail, *, paced=True, key="admission"):
    service._worker_retry_at.clear()  # advance straight to this key's next eligible pass
    service._schedule(key, function, paced=paced)
    assert key not in service._busy


def causes(service):
    return [record for record in service.recording.records if record.exc_info]


def run_control(service, outcomes):
    """One query per iteration; ``None`` completes and an exception raises."""
    state = SimpleNamespace(index=0, waits=0)

    def query(_):
        value = outcomes[state.index]
        state.index += 1
        if isinstance(value, Exception):
            raise value
        return []

    def wait(_):
        state.waits += 1

    service.stopping = SimpleNamespace(is_set=lambda: state.waits >= len(outcomes), wait=wait)
    service.store.query = query
    service._forget_paced = service._forget_answers = lambda _: None
    service._pending_exports = service._pending_worktree_holds = lambda: []
    service._recovery_complete = threading.Event()
    service._recover_then_start_timers = lambda: None
    service._schedule = lambda *args, **kwargs: None
    service._last_maintenance, service.tick_s = float("inf"), 0
    Daemon._control.__wrapped__(service)
    assert state.waits == len(outcomes)


def request(service, monkeypatch, *, operation="fixture.op"):
    replies = []
    monkeypatch.setattr(daemon_module, "send_reply", lambda *args, **kwargs: replies.append(args[2]))
    service._answer(None, threading.Lock(), protocol.Request(operation, id="fixture"), None, lambda: False)
    assert len(replies) == 1
    assert replies[0].error.message == "operation failed; inspect daemon status"


@BOUNDED
@given(n=st.integers(min_value=1, max_value=145))
def test_paced_cause_and_exact_summary_cadence(n):
    """Causes are 1 mod 32; unchanged summaries are powers of two plus causes."""
    service = core()
    for _ in range(n):
        schedule(service)
    expected = [number for number in range(1, n + 1)
                if number & (number - 1) == 0 or number % 32 == 1]
    assert len(service.recording.records) == len(expected)
    assert len(causes(service)) == len(range(1, n + 1, 32))
    for record, number in zip(service.recording.records, expected):
        assert bool(record.exc_info) == (number % 32 == 1)
        assert record.getMessage() == (
            "worker admission failed: RuntimeError "
            f"({number} in a row, next try in {daemon_module.worker_retry_delay(number):g} s)")
    assert service._worker_failures == {"admission": n}


def test_one_shot_worker_always_logs_a_cause():
    service = core()
    for _ in range(5):
        schedule(service, paced=False, key="confirm-dead")
    assert len(causes(service)) == 5
    assert [record.getMessage() for record in causes(service)] == [
        "worker confirm-dead failed: RuntimeError"] * 5


def test_retention_error_is_type_only_and_deferred_keeps_count():
    service = core()
    schedule(service, key="retention")
    assert service.marks[0][0] == ("retention",)
    assert service.marks[0][1]["error"] == "RuntimeError"
    assert service.marks[0][1]["next_due"]
    schedule(service, lambda: daemon_module.DEFERRED, key="retention")
    assert service._worker_failures == {"retention": 1}
    assert "retention" not in service._worker_retry_at
    schedule(service, lambda: None, key="retention")
    assert not service._worker_failures


def test_done_notifies_only_for_store_generation_and_always_releases_key():
    service = core()
    schedule(service)
    assert not service.notifications

    def changed_then_failed():
        service.store.generation += 1
        raise RuntimeError("changed store before failing")

    schedule(service, changed_then_failed)
    assert service.notifications == [1]
    assert not service._busy


class BadString(RuntimeError):
    def __str__(self):
        raise RuntimeError("exception string cannot render")


class BadRepr:
    def __repr__(self):
        raise RuntimeError("argument repr cannot render")


class BadExceptionRepr(RuntimeError):
    def __repr__(self):
        raise RuntimeError("exception repr cannot render")


class BadTraceback(RuntimeError):
    def __getattribute__(self, name):
        if name == "__traceback__":
            raise RuntimeError("traceback cannot be inspected")
        return super().__getattribute__(name)


def hostile(kind):
    if kind == "str":
        return BadString()
    if kind == "repr":
        return RuntimeError(BadRepr())
    if kind == "exception-repr":
        return BadExceptionRepr("fixture exception repr")
    if kind == "traceback":
        return BadTraceback("fixture exception traceback")
    if kind == "cycle-cause":
        first, second = RuntimeError("outer"), RuntimeError("inner")
        first.__cause__, second.__cause__ = second, first
        return first
    if kind == "cycle-context":
        first, second = RuntimeError("outer"), RuntimeError("inner")
        first.__context__, second.__context__ = second, first
        return first
    if kind == "huge":
        return RuntimeError("x" * 262_144)
    if kind == "group":
        return ExceptionGroup("fixture group", [RuntimeError("child"), BadString()])
    if kind == "non-string":
        return RuntimeError(123, None, {"fixture": [1, 2]})
    if kind == "notes":
        error = RuntimeError("noted failure")
        error.add_note("fixture note")
        return error
    raise AssertionError(kind)


HOSTILE = ["str", "repr", "exception-repr", "traceback", "cycle-cause", "cycle-context", "huge", "group", "non-string", "notes"]


@pytest.mark.parametrize("kind", HOSTILE)
@pytest.mark.parametrize("site", ["worker", "control", "request"])
def test_hostile_exception_cause_never_escapes_or_skips_handler(kind, site, monkeypatch, capsys):
    """All three sites return normally and actually hand the cause to the scrubber."""
    service, error = core(), hostile(kind)

    def raise_error(*args, **kwargs):
        raise error

    if site == "worker":
        schedule(service, raise_error, paced=False)
        assert not service._busy
    elif site == "control":
        run_control(service, [error])
    else:
        service.dispatch = raise_error
        request(service, monkeypatch)
    assert len(causes(service)) == 1
    assert service.stream.getvalue()
    assert capsys.readouterr().err == ""


def test_control_cause_deduplication_resets_after_completed_iteration():
    service = core()
    run_control(service, [RuntimeError("one"), RuntimeError("two"), None, RuntimeError("three")])
    assert [bool(record.exc_info) for record in service.recording.records] == [True, False, True]
    assert [record.getMessage() for record in service.recording.records] == [
        "control iteration failed: RuntimeError"] * 3


def test_first_trace_holds_at_most_256_and_starts_again_when_full():
    traced = set()
    assert daemon_module.TRACED_CAUSES_MAX == 256
    for number in range(520):
        assert Daemon._first_trace(traced, (number,))
        assert 1 <= len(traced) <= 256
        assert not Daemon._first_trace(traced, (number,))
    assert len(traced) == 8


def test_control_and_request_use_the_bounded_signature_sets(monkeypatch):
    service = core()
    monkeypatch.setattr(daemon_module, "raise_signature", lambda exc: (exc.args[0],))
    run_control(service, [RuntimeError(str(number)) for number in range(260)])
    assert len(service._control_traced) == 4
    assert len(causes(service)) == 260

    service = core()
    counter = iter(range(260))

    def dispatch(*args, **kwargs):
        raise RuntimeError(str(next(counter)))

    service.dispatch = dispatch
    for _ in range(260):
        request(service, monkeypatch)
    assert len(service._request_traced) == 4
    assert len(causes(service)) == 260


def test_request_deduplicates_per_operation_and_updates_under_lock(monkeypatch):
    service = core()
    service.dispatch = lambda *args, **kwargs: fail()

    class CheckedLock:
        def __init__(self):
            self.held, self.entries = False, 0

        def __enter__(self):
            assert not self.held
            self.held, self.entries = True, self.entries + 1

        def __exit__(self, *args):
            self.held = False

    service._request_traced_lock = guard = CheckedLock()

    def first_trace(traced, signature):
        assert guard.held
        return Daemon._first_trace(traced, signature)

    service._first_trace = first_trace
    for op in ["fixture.one", "fixture.one", "fixture.two", "fixture.two"]:
        request(service, monkeypatch, operation=op)
    assert [bool(record.exc_info) for record in service.recording.records] == [True, False, True, False]
    assert guard.entries == 4
    assert len(service._request_traced) == 2
    assert [record.getMessage() for record in service.recording.records] == [
        f"request {op} failed: RuntimeError" for op in ["fixture.one", "fixture.one", "fixture.two", "fixture.two"]]


def test_concurrent_requests_log_one_cause_for_one_signature(monkeypatch):
    service = core()
    barrier = threading.Barrier(4)

    def dispatch(*args, **kwargs):
        barrier.wait(timeout=5)
        raise RuntimeError("concurrent fixture failure")

    service.dispatch = dispatch
    monkeypatch.setattr(daemon_module, "send_reply", lambda *args, **kwargs: None)

    def answer(_):
        service._answer(None, threading.Lock(), protocol.Request("fixture.op", id="fixture"), None, lambda: False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(answer, range(4)))
    assert len(service.recording.records) == 4
    assert len(causes(service)) == 1


def raised(message):
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


def raised_elsewhere(message):
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


@BOUNDED
@given(first=st.text(max_size=500), second=st.text(max_size=500))
def test_raise_signature_ignores_message_and_distinguishes_raise_site(first, second):
    one, two, other = raised(first), raised(second), raised_elsewhere(first)
    assert daemon_module.raise_signature(one) == daemon_module.raise_signature(two)
    assert daemon_module.raise_signature(one) != daemon_module.raise_signature(other)


@pytest.mark.parametrize("kind", HOSTILE)
def test_raise_signature_never_renders_hostile_exception(kind):
    signature = daemon_module.raise_signature(hostile(kind))
    assert isinstance(signature, tuple)
    hash(signature)


def test_raise_signature_preserves_distinct_sites_with_hostile_metadata():
    def first(message):
        try:
            raise BadTraceback(message)
        except BadTraceback as exc:
            return exc

    def second(message):
        try:
            raise BadTraceback(message)
        except BadTraceback as exc:
            return exc

    one, two, other = first("one"), first("two"), second("one")
    assert daemon_module.raise_signature(one) == daemon_module.raise_signature(two)
    assert daemon_module.raise_signature(one) != daemon_module.raise_signature(other)


@pytest.mark.parametrize("site", ["worker", "control", "request"])
def test_exception_message_notes_chain_and_source_are_scrubbed(site, monkeypatch, tmp_path, capsys):
    """A dummy credential in each traceback surface cannot bypass the handler."""
    service = core()
    source = tmp_path / "fixture_raise.py"
    source.write_text("def fixture_raise():\n    raise RuntimeError(\"password=" + MARKER + "\")\n")
    namespace = {}
    exec(compile(source.read_text(), str(source), "exec"), namespace)
    try:
        namespace["fixture_raise"]()
    except RuntimeError as inner:
        error = RuntimeError("password=" + MARKER)
        error.add_note("token=" + MARKER)
        error.__cause__ = inner

    def raise_error(*args, **kwargs):
        raise error

    if site == "worker":
        schedule(service, raise_error, paced=False)
    elif site == "control":
        run_control(service, [error])
    else:
        service.dispatch = raise_error
        request(service, monkeypatch)
    written = service.stream.getvalue() + capsys.readouterr().err
    assert len(causes(service)) == 1
    for offset in range(len(MARKER) - 11):
        assert MARKER[offset:offset + 12] not in written
    assert "REDACTED" in written
