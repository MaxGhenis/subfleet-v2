"""C-5.10, C-23.14, C-25.7: review r1 of #308's URL forms and terminal escapes, on every
route a worker, control-loop or request cause reaches daemon.log or a person's terminal.

Every value here is made up. Each URL form leaked whole (or after its first `@`) through
the handler before fix r1; each route is the real one: the worker's `done()`, `_control`,
`_answer`, `scrubbed_tail` (`daemon logs`, `daemon start`) and `LogFollower`
(`daemon logs --follow`). A URL with no password is the control: it is written whole.
"""
from __future__ import annotations

import linecache
import logging
import traceback

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import daemon_log
from tests.unit.test_worker_failure_cause import causes, core, request, run_control, schedule

#: r1's leaking forms, each with a made-up password of its own.
URL_FORMS = {
    "underscore-scheme": ("my_postgres://app:{}@db.test", "Fx1underMark0001zz"),
    "underscore-cluster": ("redis_cluster://svc:{}@cache.test", "Fx1clusterMark002zz"),
    "digit-first-scheme": ("9p://u:{}@host.test", "Fx1ninePMark00003zz"),
    "scheme-after-digit": ("retry=2postgres://app:{}@db.test", "Fx1afterDigit0004zz"),
    "empty-user": ("redis://:{}@cache.test:6379/0", "Fx1noUserMark0005zz"),
    "raw-slash": ("https://alice:pa/{}@host.test", "Fx1slashMark00006zz"),
    "second-at": ("ftp://anon:Pw@{}@host.test", "Fx1atSignMark0007zz"),
}
#: Written whole on every route: a URL with a port, a path and a query but no password.
CONTROL_URL = "https://status.example.test:8443/health?check=1"
ROUTES = ["message", "note", "cause", "context", "source", "group-member", "control", "request", "tail",
          "follow"]


def _hidden(written: str, marker: str) -> None:
    assert marker not in written
    assert all(marker[at:at + 12] not in written for at in range(len(marker) - 11)), written


def _raised_in_source(text: str) -> BaseException:
    """A RuntimeError whose raise line, as linecache gives it to the renderer, holds `text`."""
    filename = "<c510-r1-source-line>"
    source = f"raise RuntimeError('backend refused')  # {text}\n"
    linecache.cache[filename] = (len(source), None, [source], filename)
    try:
        exec(compile(source, filename, "exec"), {})
    except RuntimeError as exc:
        assert text in "".join(traceback.format_exception(exc))
        return exc
    raise AssertionError("the source did not raise")


def _cause(route: str, text: str) -> BaseException:
    if route in ("message", "control", "request"):
        return RuntimeError(f"backend refused {text}")
    if route == "note":
        exc = RuntimeError("backend refused")
        exc.add_note(f"while reaching {text}")
        return exc
    if route in ("cause", "context"):
        exc = RuntimeError("backend refused")
        setattr(exc, f"__{route}__", ValueError(f"cannot reach {text}"))
        return exc
    if route == "source":
        return _raised_in_source(text)
    if route == "group-member":
        return ExceptionGroup("backends refused", [RuntimeError("first"), ValueError(f"cannot reach {text}")])
    raise AssertionError(route)


def _written(route: str, text: str, monkeypatch) -> str:
    """What `route` writes of a cause holding `text`, beside what reached stderr."""
    if route == "tail":
        return "\n".join(daemon_log.scrubbed_tail(f"earlier line\nworker admission failed: {text}\nlater\n", 5))
    if route == "follow":
        follower = daemon_log.LogFollower("earlier line\n")
        return "\n".join(follower.feed(f"worker admission failed: {text}\n") + follower.flush())
    service, error = core(), _cause(route, text)

    def raise_error(*args, **kwargs):
        raise error

    if route == "control":
        run_control(service, [error])
    elif route == "request":
        service.dispatch = raise_error
        request(service, monkeypatch)
    else:
        schedule(service, raise_error, paced=False)
    assert len(causes(service)) == 1
    return service.stream.getvalue()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("form", sorted(URL_FORMS))
def test_r1_url_forms_are_hidden_on_every_route(form, route, monkeypatch, capsys):
    shape, marker = URL_FORMS[form]
    written = _written(route, f"{shape.format(marker)} via {CONTROL_URL}", monkeypatch)
    written += capsys.readouterr().err
    _hidden(written, marker)
    assert CONTROL_URL in written


@pytest.mark.parametrize("route", ROUTES)
def test_a_url_without_a_password_is_written_whole_on_every_route(route, monkeypatch):
    plain = "https://alice@db.example.test:5432/main?sslmode=require"
    written = _written(route, f"{plain} and {CONTROL_URL}", monkeypatch)
    assert plain in written and CONTROL_URL in written


# --- terminal escapes (review r1's P2) ---------------------------------------------------

#: OSC 52 (a clipboard write), then a screen clear: what a terminal acts on unseen.
OSC52 = "\x1b]52;c;ZmFrZQ==\x07 clipboard \x1b[2J"
HIDDEN_KEY_MARKER = "AnsiKeyMark3232zzFx1"
#: A password key a reset sequence splits; a terminal shows it as `password=`.
HIDDEN_KEY = f"pass\x1b[0mword={HIDDEN_KEY_MARKER}"
TERMINAL_ROUTES = ["message", "control", "request", "tail", "follow"]


def _no_controls(written: str) -> None:
    assert not daemon_log._CONTROL.search(written), repr(written)


@pytest.mark.parametrize("route", TERMINAL_ROUTES)
def test_osc52_and_screen_clear_are_written_as_visible_escapes(route, monkeypatch):
    written = _written(route, OSC52, monkeypatch)
    _no_controls(written)
    assert "\\x1b]52;c;ZmFrZQ==\\x07 clipboard \\x1b[2J" in written


@pytest.mark.parametrize("route", TERMINAL_ROUTES)
def test_a_key_an_escape_splits_is_still_a_password(route, monkeypatch):
    written = _written(route, f"login refused {HIDDEN_KEY} for {CONTROL_URL}", monkeypatch)
    _no_controls(written)
    _hidden(written, HIDDEN_KEY_MARKER)
    assert CONTROL_URL in written


def test_c1_controls_bell_and_other_c0_controls_are_visible():
    written = daemon_log.scrubbed("ordinary \x9b31m text \x85 next \x07bell \x00nul \x7fdel \x0bvt")
    _no_controls(written)
    assert written == "ordinary \\x9b31m text \\x85 next \\x07bell \\x00nul \\x7fdel \\x0bvt"


def test_a_record_without_controls_is_written_as_before():
    line = "worker admission failed: RuntimeError (1 in a row, next try in 0.5 s)"
    assert daemon_log.scrubbed(line) == line
    block = "Traceback (most recent call last):\n\tindented by a tab\r\nRuntimeError: fixture"
    assert daemon_log.scrubbed(block) == block


CONTROL_PIECES = ["\x1b", "\x1b[", "\x1b]52;c;", "\x07", "\x9b", "\x9d", "\x9c", "\x85", "\x0b", "\x00", "\x7f",
                  "[0m", "pass", "word", "=", "token", ": ", " ", "\n", "\t", "Fx1valueMark0042zz"]


@settings(max_examples=60, derandomize=True, database=None, deadline=None)
@given(pieces=st.lists(st.sampled_from(CONTROL_PIECES), max_size=24))
def test_no_control_character_but_tab_and_line_breaks_is_ever_written(pieces):
    """Invariant (fix r1): whatever a record or the file holds, the handler, the tail and
    the follower write no C0 control but tab, line feed and carriage return, no DEL and
    no C1 control."""
    text = "".join(pieces)
    _no_controls(daemon_log.scrubbed(text))
    _no_controls("\n".join(daemon_log.scrubbed_tail(text + "\n", 40)))
    follower = daemon_log.LogFollower("")
    _no_controls("\n".join(follower.feed(text + "\n") + follower.flush()))


# --- the traceback's cost is bounded before Python renders it (review r1's P3) -----------

def _chain(length: int) -> BaseException:
    previous = None
    for index in range(length):
        try:
            raise RuntimeError(f"fixture link {index}")
        except RuntimeError as exc:
            exc.__context__ = previous
            previous = exc
    return previous


class CountingTracebackException(traceback.TracebackException):
    made = 0

    def __init__(self, *args, **kwargs):
        type(self).made += 1
        super().__init__(*args, **kwargs)


def _exception_text(exc: BaseException) -> str:
    return daemon_log.ScrubbingFormatter()._exception_text((type(exc), exc, exc.__traceback__))


def test_a_chain_within_the_bound_is_rendered_as_python_renders_it():
    exc = _chain(daemon_log.EXCEPTIONS_RENDERED_MAX)
    assert _exception_text(exc) == logging.Formatter().formatException((type(exc), exc, exc.__traceback__))


@pytest.mark.parametrize("length", [daemon_log.EXCEPTIONS_RENDERED_MAX + 1, 2000, 5000])
def test_a_longer_chain_renders_only_its_two_ends(length, monkeypatch):
    exc = _chain(length)
    CountingTracebackException.made = 0
    monkeypatch.setattr(traceback, "TracebackException", CountingTracebackException)
    text = _exception_text(exc)
    ends = daemon_log.CHAIN_ENDS_RENDERED
    assert CountingTracebackException.made == 2 * ends
    omitted = length - 2 * ends
    assert f"… [{omitted:,} chained exceptions not rendered;" in text
    shown = [index for index in range(length) if f"RuntimeError: fixture link {index}\n" in text]
    assert shown == [*range(ends), *range(length - ends, length)]
    assert text.rstrip().endswith(f"RuntimeError: fixture link {length - 1}")


def test_a_group_of_many_members_is_rendered_without_them(monkeypatch):
    group = ExceptionGroup("many members", [RuntimeError(f"member {index}") for index in range(10_000)])
    CountingTracebackException.made = 0
    monkeypatch.setattr(traceback, "TracebackException", CountingTracebackException)
    text = _exception_text(group)
    assert CountingTracebackException.made == 1
    assert "ExceptionGroup: many members (10000 sub-exceptions)" in text
    assert "member 0" not in text


def test_a_long_chain_logged_by_a_site_writes_both_ends(monkeypatch):
    service, error = core(), _chain(2000)
    run_control(service, [error])
    written = service.stream.getvalue()
    assert "RuntimeError: fixture link 0" in written and "RuntimeError: fixture link 1999" in written
    assert "chained exceptions not rendered" in written
    assert len(written) <= 2 * (daemon_log.RECORD_SCRUB_MAX + 100)
