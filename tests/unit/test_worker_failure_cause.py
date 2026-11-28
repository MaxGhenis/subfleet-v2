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
    # Release's admission pacing (D-ADMIT-IDLE1, #301), as `Daemon.__init__` sets it:
    # `_schedule("admission", ...)` reads it first and an admission failure wakes it.
    service._admission_pace_lock = threading.Lock()
    service._admission_wake_seq = 0
    service._admission_capacity_revision = 0
    service._admission_rest_until = 0.0
    service._admission_closure_due = float("inf")
    service._admission_policy_seen = None
    service._admission_thread = threading.local()
    return service


def fail():
    raise RuntimeError("fixture failure cause")


def schedule(service, function=fail, *, paced=True, key="admission"):
    service._worker_retry_at.clear()  # advance straight to this key's next eligible pass
    service._schedule(key, function, paced=paced)
    assert key not in service._busy


def causes(service):
    return [record for record in service.recording.records if record.exc_info]


def run_control(service, outcomes, *, exports=lambda: []):
    """One query per iteration; ``None`` completes, an exception raises and a callable is
    called (and may raise). ``exports`` stands for the pending exports the loop reads
    after a query that returns."""
    state = SimpleNamespace(index=0, waits=0)

    def query(_):
        value = outcomes[state.index]
        state.index += 1
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value()
        return []

    def wait(_):
        state.waits += 1

    service.stopping = SimpleNamespace(is_set=lambda: state.waits >= len(outcomes), wait=wait)
    service.store.query = query
    service._forget_paced = service._forget_answers = lambda _: None
    service._pending_exports, service._pending_worktree_holds = exports, lambda: []
    service._recovery_complete = threading.Event()
    service._recover_then_start_timers = lambda: None
    service._schedule = lambda *args, **kwargs: None
    service._retention_due = lambda: False
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
    # The logged causes are the fixture's own errors, so an iteration that fails on
    # something the fixture never set up shows here by name, not as a count.
    assert [str(record.exc_info[1]) for record in causes(service)] == ["one", "three"]


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


# --- review r1 of #308: hostile types, re-raised objects and the signature's parts --------

class ListQualname(type):
    """A metaclass whose `__qualname__` reads as an unhashable list."""

    def __getattribute__(cls, name):
        if name == "__qualname__":
            return ["fixture", "qualname"]
        return super().__getattribute__(name)


class UnhashableName(str):
    def __hash__(self):
        raise RuntimeError("fixture name cannot be hashed")


class RaisingHashQualname(type):
    """A metaclass whose `__qualname__` reads as a str whose hash raises."""

    def __getattribute__(cls, name):
        if name == "__qualname__":
            return UnhashableName("fixture")
        return super().__getattribute__(name)


class RaisingName(type):
    """A metaclass whose `__name__` cannot be read."""

    def __getattribute__(cls, name):
        if name == "__name__":
            raise RuntimeError("fixture name cannot be read")
        return super().__getattribute__(name)


HOSTILE_TYPES = {
    "list-qualname": ListQualname("ListQualnameError", (RuntimeError,), {}),
    "raising-hash-qualname": RaisingHashQualname("RaisingHashError", (RuntimeError,), {}),
    "raising-name": RaisingName("RaisingNameError", (RuntimeError,), {}),
}


def raise_hostile(kind):
    """The same raise site every time, so its causes share one signature."""
    raise HOSTILE_TYPES[kind]("fixture hostile type")


@pytest.mark.parametrize("kind", sorted(HOSTILE_TYPES))
@pytest.mark.parametrize("site", ["control", "request"])
def test_hostile_exception_types_never_raise_into_control_or_request(kind, site, monkeypatch, capsys):
    """Review r1, P2: a metaclass's `__qualname__` (a list, a str whose hash raises) or
    `__name__` (raising) leaves the loop running and the request answered, and the
    signature still deduplicates: two raises from one place log one cause."""
    service = core()
    if site == "control":
        errors = []
        for _ in range(2):
            try:
                raise_hostile(kind)
            except RuntimeError as exc:
                errors.append(exc)
        run_control(service, errors)
    else:
        service.dispatch = lambda *args, **kwargs: raise_hostile(kind)
        for _ in range(2):
            request(service, monkeypatch)
    assert [bool(record.exc_info) for record in service.recording.records] == [True, False]
    name = type.__dict__["__name__"].__get__(HOSTILE_TYPES[kind])
    assert all(record.getMessage().endswith(f"failed: {name}") for record in service.recording.records)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("kind", sorted(HOSTILE_TYPES))
def test_raise_signature_of_a_hostile_type_is_plain_and_hashable(kind):
    try:
        raise_hostile(kind)
    except RuntimeError as exc:
        signature = daemon_module.raise_signature(exc)
    hash(signature)
    assert signature[0][0] == type.__dict__["__qualname__"].__get__(HOSTILE_TYPES[kind])

    def parts(value):
        if isinstance(value, tuple):
            for item in value:
                yield from parts(item)
        else:
            yield value

    assert {type(part) for part in parts(signature)} <= {str, int, type(None)}


def test_first_trace_treats_an_uncomparable_signature_as_new():
    traced = {("fixture",)}
    assert Daemon._first_trace(traced, (["unhashable"],))
    assert Daemon._first_trace(traced, (UnhashableName("fixture"),))
    assert traced == {("fixture",)}


def test_one_object_raised_on_three_control_ticks_logs_one_cause():
    """Review r1, P2: each raise of the same object adds frames to its traceback (a
    stored failure raised again each tick); the signature keys on the frames it was
    first raised in and caught at, so three ticks log one cause."""
    service, stored = core(), RuntimeError("fixture failure stored and raised again")
    run_control(service, [stored, stored, stored])
    assert [bool(record.exc_info) for record in service.recording.records] == [True, False, False]


def fixture_helper_raises():
    raise RuntimeError("fixture failure raised in one helper")


def caught_at_one_call():
    try:
        fixture_helper_raises()
    except RuntimeError as exc:
        return exc


def caught_at_another_call():
    try:
        fixture_helper_raises()
    except RuntimeError as exc:
        return exc


def test_raise_signature_reads_the_outermost_frame():
    """Review r2 of #308, P2: two links raised at one line of one helper (the same
    innermost frame) and caught where two different lines called it (different
    outermost frames) key apart; the same call keys alike."""
    one, two = caught_at_one_call(), caught_at_another_call()
    assert daemon_module.raise_signature(one)[0][1] == daemon_module.raise_signature(two)[0][1]
    assert daemon_module.raise_signature(one) != daemon_module.raise_signature(two)
    assert daemon_module.raise_signature(one) == daemon_module.raise_signature(caught_at_one_call())


def test_one_helper_reached_from_two_control_lines_logs_two_causes():
    """The same, at the site: one helper raises when the loop's query calls it and when
    its pending exports call it, two lines of `_control`, so two causes; the query's
    again is not new."""
    service = core()
    run_control(service, [fixture_helper_raises, None, fixture_helper_raises], exports=fixture_helper_raises)
    assert [bool(record.exc_info) for record in service.recording.records] == [True, True, False]


def test_one_object_raised_from_three_requests_logs_one_cause(monkeypatch):
    service, stored = core(), RuntimeError("fixture failure stored and raised again")

    def dispatch(*args, **kwargs):
        raise stored

    service.dispatch = dispatch
    for _ in range(3):
        request(service, monkeypatch)
    assert [bool(record.exc_info) for record in service.recording.records] == [True, False, False]


def raised_after(raise_inner, *, suppress):
    """A RuntimeError raised at one line while handling what `raise_inner` raised:
    `from None` when `suppress`, else with it as its context."""
    try:
        try:
            raise_inner()
        except Exception:
            if suppress:
                raise RuntimeError("outer") from None
            raise RuntimeError("outer")
    except RuntimeError as exc:
        return exc


def inner_value_error():
    raise ValueError("inner")


def inner_key_error():
    raise KeyError("inner")


def test_raise_signature_honours_raise_from_none():
    """Kills r1's R1: a suppressed context is not part of the chain the signature reads."""
    one = raised_after(inner_value_error, suppress=True)
    two = raised_after(inner_key_error, suppress=True)
    assert len(daemon_module.raise_signature(one)) == 1
    assert daemon_module.raise_signature(one) == daemon_module.raise_signature(two)
    shown = raised_after(inner_value_error, suppress=False), raised_after(inner_key_error, suppress=False)
    assert len(daemon_module.raise_signature(shown[0])) == 2
    assert daemon_module.raise_signature(shown[0]) != daemon_module.raise_signature(shown[1])


def raised_on_one_of_two_lines(first):
    try:
        if first:
            raise RuntimeError("same function, first line")
        raise RuntimeError("same function, second line")
    except RuntimeError as exc:
        return exc


def test_raise_signature_reads_line_numbers():
    """Kills r1's R2: two raises in one function differ only by their lines."""
    one, two = raised_on_one_of_two_lines(True), raised_on_one_of_two_lines(False)
    assert daemon_module.raise_signature(one) != daemon_module.raise_signature(two)
    assert daemon_module.raise_signature(one) == daemon_module.raise_signature(raised_on_one_of_two_lines(True))


def raised_from(raise_inner):
    try:
        try:
            raise_inner()
        except Exception as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as exc:
        return exc


def inner_value_error_elsewhere():
    raise ValueError("inner")


def test_raise_signature_reads_the_whole_chain():
    """Kills r1's R3: one outer raise site, causes of one type raised at two places, two
    signatures."""
    one, two = raised_from(inner_value_error), raised_from(inner_value_error_elsewhere)
    assert daemon_module.raise_signature(one)[0] == daemon_module.raise_signature(two)[0]
    assert daemon_module.raise_signature(one) != daemon_module.raise_signature(two)
