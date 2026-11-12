"""C-5.10's causes use release/217's real daemon.log handler (C-25.7).

The C-23.14 probes deliberately leave sessions/handoff.py unchanged. The URL
case records the remaining scrubber limitation for the hub to route separately.
Single-call timing measurements are checked in beside the implementation report.
"""
from __future__ import annotations

import io
import linecache
import logging

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import daemon_log


MARKER = "q7Wd9Zk2Lp4Xv8Nm3Rt6Yh1Bs5Gc0Jf"
LIMIT = daemon_log.RECORD_SCRUB_MAX
# C-25.7's existing regression guard applies to the selected fixtures below;
# it is not a universal output bound. Redaction markers can expand short values
# beyond it. The handler's source scrub budget is LIMIT, plus cut markers.
WRITTEN_BOUND = 2 * (LIMIT + 100)
BOUNDED = settings(max_examples=48, derandomize=True, database=None, deadline=None)
MARKERS = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                  min_size=24, max_size=48)


def _write(message: object, exc: BaseException | None = None) -> str:
    stream = io.StringIO()
    handler = daemon_log.ScrubbingStreamHandler(stream)
    logger = logging.Logger("subfleet.daemon.worker-handler", logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.error("%s", message, exc_info=exc)
        return stream.getvalue()
    finally:
        logger.removeHandler(handler)
        handler.close()


def _hidden(output: str, marker: str) -> None:
    assert marker not in output
    assert all(marker[start:start + 12] not in output for start in range(len(marker) - 11)), output
    assert 0 < len(output) <= WRITTEN_BOUND


HEADER_ROWS = [
    pytest.param(f"RuntimeError: refused: Authorization: Basic {MARKER}", id="runtime-error-prefix"),
    pytest.param(f"curl -fsS -H 'Authorization: Basic {MARKER}' https://example.test", id="curl-header"),
    pytest.param(f"Command '['curl', '-H', 'X-Request-ID: 7\\nAuthorization: Basic {MARKER}', "
                 "'https://x.test']'", id="command-repr"),
    pytest.param(f"KeyError: 'response headers\\nAuthorization: Basic {MARKER}'", id="escaped-newline"),
]


@pytest.mark.parametrize("message", HEADER_ROWS)
def test_c2314_inline_headers_are_hidden_by_the_handler(message):
    _hidden(_write(message), MARKER)


def test_c2314_compound_assignment_is_hidden_by_the_handler():
    _hidden(_write("x_" * 3000 + f"password={MARKER}"), MARKER)


@pytest.mark.parametrize("message", [
    pytest.param("\n" * (64 * 1024), id="64kb-newlines"),
    pytest.param(("-----BEGIN PRIVATE KEY-----\n" * 10000)[:256 * 1024], id="256kb-pem-openings"),
    pytest.param("\n" * LIMIT, id="newlines-at-record-bound"),
    pytest.param(("-----BEGIN PRIVATE KEY-----\n" * 1000)[:LIMIT], id="pem-at-record-bound"),
])
def test_c2314_adversarial_records_are_bounded(message):
    output = _write(message)
    assert 0 < len(output) <= WRITTEN_BOUND


@pytest.mark.documented_limitation
def test_c2314_underscore_url_password_still_reaches_handler_output_documented_limitation():
    """Release/217's URL rule does not recognize an underscore scheme. C-5.10
    reports this to the hub; D-SCRUB-BOUNDARY1 owns the pending scrubber changes.
    A local exception helper using the same scrubber would keep this value too.
    """
    message = f"my_postgres://app:{MARKER}@db"
    assert _write(message) == message + "\n"


def _exception_with(marker: str, location: str) -> BaseException:
    if location == "message":
        return RuntimeError(f"backend refused password={marker}")
    if location == "note":
        exc = RuntimeError("backend refused")
        exc.add_note(f"Authorization: Basic {marker}")
        return exc
    if location == "chain":
        exc = RuntimeError("backend refused")
        exc.__cause__ = ValueError(f"password={marker}")
        return exc
    if location == "context":
        exc = RuntimeError("backend refused")
        exc.__context__ = ValueError(f"password={marker}")
        return exc
    raise AssertionError(location)


@BOUNDED
@given(marker=MARKERS, location=st.sampled_from(["message", "note", "chain", "context", "source"]))
def test_cause_credentials_in_messages_notes_chains_and_source_never_reach_handler(marker, location):
    """An unshaped marker needs only its key or header, including in the source
    line Python's traceback renderer retrieves from linecache.
    """
    if location != "source":
        _hidden(_write("worker admission failed", _exception_with(marker, location)), marker)
        return
    filename = "<c510-source-credential>"
    source = f"raise RuntimeError('backend refused')  # password={marker}\n"
    old = linecache.cache.get(filename)
    linecache.cache[filename] = (len(source), None, [source], filename)
    try:
        try:
            exec(compile(source, filename, "exec"), {})
        except RuntimeError as exc:
            # Prove this case exercised a credential-bearing source line.
            raw = logging.Formatter().formatException((type(exc), exc, exc.__traceback__))
            assert marker in raw
            _hidden(_write("worker admission failed", exc), marker)
    finally:
        if old is None:
            linecache.cache.pop(filename, None)
        else:
            linecache.cache[filename] = old


@BOUNDED
@given(marker=MARKERS, side=st.sampled_from(["head", "tail"]),
       relative=st.sampled_from(["inside", "before", "after"]))
def test_cause_credentials_at_cut_windows_never_leave_twelve_character_fragments(marker, side, relative):
    """Place the nominal head/tail window edge inside a credential or directly
    beside it. The handler must move its actual cut to a safe whole-line break.
    """
    message = "worker admission failed"
    prefix = "RuntimeError: "
    total = LIMIT * 3
    trace_limit = LIMIT - len(message)
    edge = trace_limit // 2 if side == "head" else total - trace_limit // 2
    marker_start = edge - len(marker) // 2 if relative == "inside" else edge if relative == "before" else edge - len(marker)
    credential = f"password={marker} ordinary\n"
    before_length = marker_start - len(prefix) - len("password=")
    assert before_length > 0
    # Many short lines offer safe breaks outside the credential's one line.
    before = ("ordinary diagnostic\n" * (before_length // 20) + " " * (before_length % 20))
    assert len(before) == before_length
    text = before + credential
    remaining = total - len(prefix) - len(text)
    assert remaining >= 4
    text += "ordinary diagnostic\n" * ((remaining - 4) // 20) + " " * ((remaining - 4) % 20) + "tail"
    exc = RuntimeError(text)
    raw = logging.Formatter().formatException((type(exc), exc, None))
    assert len(raw) == total
    assert raw.index(marker) == marker_start
    output = _write(message, exc)
    assert "characters omitted" in output
    _hidden(output, marker)


class Unprintable(Exception):
    def __str__(self):
        raise ValueError(f"cannot render password={MARKER}")

    def __repr__(self):
        raise ValueError(f"cannot repr password={MARKER}")


class UnprintableArgument:
    def __str__(self):
        raise ValueError(f"cannot render password={MARKER}")

    def __repr__(self):
        raise ValueError(f"cannot repr password={MARKER}")


def _pathological(kind: str) -> BaseException:
    if kind == "str-and-repr-raise":
        return Unprintable()
    if kind == "argument-rendering-raises":
        return RuntimeError(UnprintableArgument())
    if kind == "cyclic-cause":
        first, second = RuntimeError("first"), RuntimeError(f"password={MARKER}")
        first.__cause__, second.__cause__ = second, first
        return first
    if kind == "cyclic-context":
        first, second = RuntimeError("first"), RuntimeError(f"password={MARKER}")
        first.__context__, second.__context__ = second, first
        return first
    if kind == "huge-message":
        return RuntimeError(f"password={MARKER}\n" + "ordinary diagnostic\n" * 10000)
    if kind == "exception-group":
        return ExceptionGroup("backend refused", [RuntimeError(f"password={MARKER}"), Unprintable()])
    if kind == "nonstring-arguments":
        return RuntimeError(1, {"password": MARKER}, None)
    if kind == "notes":
        exc = RuntimeError("backend refused")
        exc.add_note(f"password={MARKER}")
        exc.__notes__.append(UnprintableArgument())
        return exc
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", [
    "str-and-repr-raise", "argument-rendering-raises", "cyclic-cause", "cyclic-context",
    "huge-message", "exception-group", "nonstring-arguments", "notes",
])
def test_pathological_causes_return_normally_and_write_bounded_without_credentials(kind):
    _hidden(_write("worker admission failed", _pathological(kind)), MARKER)


def test_rendering_failure_uses_formatter_fixed_line_without_raising(monkeypatch):
    def cannot_format(self, record):
        raise ValueError(f"formatter failed password={MARKER}")

    monkeypatch.setattr(daemon_log.ScrubbingFormatter, "_scrubbed", cannot_format)
    output = _write("worker admission failed", RuntimeError(f"password={MARKER}"))
    assert output.startswith("[scrub failed] ERROR record from subfleet.daemon.worker-handler (RuntimeError)")
    _hidden(output, MARKER)


def test_handler_shares_its_source_budget_between_message_traceback_and_stack(monkeypatch):
    """Instrument the actual record cut rather than infer a source bound from
    emitted length: replacing short values with markers can grow the latter.
    """
    original_within, original_cut = daemon_log._within, daemon_log.cut_whole
    budgets, cuts = [], []

    def cut(text, limit):
        bounded = original_cut(text, limit)
        cuts.append((len(text), limit, len(bounded)))
        return bounded

    def within(parts, total):
        bounded = original_within(parts, total)
        budgets.append((list(map(len, parts)), total, list(map(len, bounded))))
        return bounded

    monkeypatch.setattr(daemon_log, "cut_whole", cut)
    monkeypatch.setattr(daemon_log, "_within", within)
    message = "MESSAGE_HEAD\n" + "ordinary diagnostic\n" * 2000 + "MESSAGE_TAIL"
    exception = RuntimeError("CAUSE_HEAD\n" + "ordinary diagnostic\n" * 2500 + "CAUSE_TAIL")
    stack = "STACK_HEAD\n" + "ordinary diagnostic\n" * 3000 + "STACK_TAIL"
    record = logging.makeLogRecord({
        "name": "subfleet.daemon.worker-handler", "levelno": logging.ERROR, "levelname": "ERROR",
        "msg": message, "args": None, "exc_info": (type(exception), exception, None), "stack_info": stack,
    })
    stream = io.StringIO()
    handler = daemon_log.ScrubbingStreamHandler(stream)
    try:
        handler.handle(record)
    finally:
        handler.close()
    output = stream.getvalue()
    assert len(budgets) == 1 and len(cuts) == 3
    source_lengths, total, cut_lengths = budgets[0]
    assert len(source_lengths) == 3 and all(length > LIMIT for length in source_lengths)
    assert total == LIMIT
    assert [source for source, _, _ in cuts] == source_lengths
    assert sum(share for _, share, _ in cuts) == LIMIT
    assert [written for _, _, written in cuts] == cut_lengths
    assert all(written <= share + 100 for _, share, written in cuts)
    assert sum(cut_lengths) <= LIMIT + 3 * 100
    for part in ("MESSAGE", "CAUSE", "STACK"):
        assert part + "_HEAD" in output and part + "_TAIL" in output
    assert output.count("characters omitted") == 3
    assert len(output) <= WRITTEN_BOUND
