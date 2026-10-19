"""What the daemon writes to daemon.log is scrubbed where it is written (C-25.7, D-LOG1).

The daemon's handler (`ScrubbingStreamHandler`) formats every record through
`ScrubbingFormatter`:

- a one-line message, with the line its format builds around it, in C-25.5's
  line form (`refusals.scrubbed_line`): scrubbed whole, folded to one line,
  scrubbed again, and cut at `refusals.LOG_LINE_MAX` without splitting a marker;
- a message of several lines (a lock watch's stacks, C-3.6), the record's
  traceback and its stack in the block form (`scrubbed_block`): still several
  lines, scrubbed whole (`scrub_secrets(..., whole=True)`).

The two forms agree. Folded, a block is exactly the line form's text before
its cut: `scrubbed_block` writes the folded, rescrubbed text in place of the
block whenever folding would let the scrubber find more. So a value holding no
whitespace is in the block exactly when it is in the uncut line, and the cut
only drops text.

A record that cannot be scrubbed (the scrubber raised, or the record's own
message cannot be rendered) is written as one fixed `[scrub failed]` line that
names its level, its logger and the class of any exception it carries. Nothing
raised there reaches the caller. logging's own report of a failed write prints
a record's raw message and arguments to stderr; the handler writes a fixed line
there instead (`handleError`).

The daemon's stdout and stderr are daemon.log too: `daemon start` and the
launchd plist point both there. `install_process_hooks`, which `daemon.main`
calls at start, sends what Python itself writes there through the same
formatter: an uncaught exception (`sys.excepthook`), a thread's
(`threading.excepthook`), one Python could not raise (`sys.unraisablehook`), a
warning (`warnings.showwarning`), and a record no handler took
(`logging.lastResort`). faulthandler stays as it is. It writes each frame's
file, line number and function name only, never an exception's message, a
local or a source line (CPython's `Python/traceback.c`, `dump_frame`).
"""
from __future__ import annotations

import copy
import logging
import re
import sys
import threading
import warnings
from typing import Any, Callable

from . import refusals
from .sessions.handoff import scrub_secrets

#: What a record that cannot be scrubbed is written as (C-25.7).
SCRUB_FAILED = "[scrub failed]"
#: What logging's report of a record it could not write becomes.
WRITE_FAILED = "[log write failed]"

#: A level or logger name a fixed line may carry; anything else reads as `?`.
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _named(value: Any) -> str:
    return value if type(value) is str and _NAME.match(value) else "?"


def scrubbed_block(text: str) -> str:
    """`text` for daemon.log in the block form: its lines kept, scrubbed whole
    (C-23.14). When folding it to one line would let the scrubber find more, the
    folded, rescrubbed text is written instead, so that folded, the block is
    always the line form's text before the cut."""
    clean, _ = scrub_secrets(text, strip_reminders=True, whole=True)
    folded = " ".join(clean.split())
    again, _ = scrub_secrets(folded, strip_reminders=True, whole=True)
    return clean.rstrip("\r\n") if again == folded else again


def failed_line(record: Any, error: BaseException | None = None, marker: str = SCRUB_FAILED) -> str:
    """The fixed line a record is written as when it cannot be scrubbed: its
    level, its logger and the class of any exception it carries, never its words."""
    try:
        info = getattr(record, "exc_info", None)
        carried = (f" ({refusals.class_name(info[1])})"
                   if type(info) is tuple and len(info) == 3 and isinstance(info[1], BaseException) else "")
        cause = f": {refusals.class_name(error)}" if isinstance(error, BaseException) else ""
        return (f"{marker} {_named(getattr(record, 'levelname', None))} record from "
                f"{_named(getattr(record, 'name', None))}{carried} left out{cause}")
    except Exception:                       # noqa: BLE001 - the marker alone, whatever failed
        return marker


class ScrubbingFormatter(logging.Formatter):
    """Every record scrubbed whole: its line, its traceback and its stack (see the
    module docstring). The record the caller logged is left as it was."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None):
        super().__init__(fmt, datefmt)
        try:
            refusals.scrubbed_line("")      # the scrubber's lazy imports, before a handler's lock is held
        except Exception:                   # noqa: BLE001 - a scrubber that fails writes `failed_line`
            pass

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._scrubbed(record)
        except Exception as exc:            # noqa: BLE001 - never lose the line, never raise into the caller
            return failed_line(record, exc)

    def _scrubbed(self, record: logging.LogRecord) -> str:
        view = copy.copy(record)
        view.message = view.getMessage()
        if self.usesTime():
            view.asctime = self.formatTime(view, self.datefmt)
        line = self.formatMessage(view)
        text = scrubbed_block(line) if len(line.splitlines()) > 1 else refusals.scrubbed_line(line)
        for block in self._blocks(record):
            text += "\n" + scrubbed_block(block)
        return text

    def _blocks(self, record: logging.LogRecord):
        # Formatted from `exc_info` each time: an `exc_text` another formatter cached
        # on the record is used only when the record has nothing else.
        if record.exc_info:
            yield self.formatException(record.exc_info)
        elif record.exc_text:
            yield record.exc_text
        if record.stack_info:
            yield self.formatStack(record.stack_info)


class ScrubbingStreamHandler(logging.StreamHandler):
    """The daemon's handler: `ScrubbingFormatter` on its stream (C-25.7)."""

    def __init__(self, stream=None):
        super().__init__(stream)
        self.setFormatter(ScrubbingFormatter())

    def handleError(self, record: logging.LogRecord) -> None:
        """logging's own report prints the record's raw message and arguments to
        stderr, which is daemon.log: write a fixed line there instead."""
        try:
            stream = sys.stderr
            if stream is not None:
                stream.write(failed_line(record, sys.exception(), WRITE_FAILED) + "\n")
                stream.flush()
        except Exception:                   # noqa: BLE001 - nowhere left to say it
            pass


class _StderrHandler(ScrubbingStreamHandler):
    """The process's stderr as it is when a record is written, as `logging.lastResort`'s."""

    def __init__(self, level: int = logging.NOTSET):
        logging.Handler.__init__(self, level)
        self.setFormatter(ScrubbingFormatter())

    @property
    def stream(self):
        return sys.stderr


def install_process_hooks() -> Callable[[], None]:
    """Send what Python writes to this process's stderr (daemon.log, under `daemon
    start` and launchd alike) through `ScrubbingFormatter`: an uncaught exception's
    traceback, a thread's, an unraisable one's, a warning, and a record that found
    no handler. Returns what puts back each hook still this one."""
    handler = _StderrHandler()

    def report(level: int, words: Callable[[], str], exc_info: tuple | None) -> None:
        try:
            try:
                message = words()
            except Exception:               # noqa: BLE001 - what cannot be rendered is not written
                message = "(its description cannot be rendered)"
            handler.handle(logging.makeLogRecord({
                "name": "subfleet.daemon", "levelno": level, "levelname": logging.getLevelName(level),
                "msg": message, "args": None, "exc_info": exc_info}))
        except Exception:                   # noqa: BLE001 - a hook never raises
            try:
                sys.stderr.write(SCRUB_FAILED + " an uncaught exception is left out\n")
            except Exception:               # noqa: BLE001
                pass

    def excepthook(kind, value, tb) -> None:
        report(logging.ERROR, lambda: "uncaught exception", (kind, value, tb))

    def thread_hook(args) -> None:
        if args.exc_type is SystemExit:     # as threading's own hook: a thread's exit is no failure
            return
        report(logging.ERROR, lambda: f"Exception in thread {args.thread.name if args.thread else '?'}",
               (args.exc_type, args.exc_value, args.exc_traceback))

    def unraisable_hook(unraisable) -> None:
        def words() -> str:
            said = unraisable.err_msg or "Exception ignored in"
            return said if unraisable.object is None else f"{said}: {unraisable.object!r}"
        value = unraisable.exc_value
        report(logging.ERROR, words,
               (unraisable.exc_type, value, unraisable.exc_traceback) if value is not None else None)

    shown = warnings.showwarning

    def showwarning(message, category, filename, lineno, file=None, line=None) -> None:
        if file is not None and file is not sys.stderr:
            return shown(message, category, filename, lineno, file, line)      # its caller's own file
        report(logging.WARNING, lambda: warnings.formatwarning(message, category, filename, lineno, line), None)

    mine = {"excepthook": excepthook, "thread": thread_hook, "unraisable": unraisable_hook,
            "showwarning": showwarning, "last_resort": _StderrHandler(logging.WARNING)}
    previous = {"excepthook": sys.excepthook, "thread": threading.excepthook,
                "unraisable": sys.unraisablehook, "showwarning": shown, "last_resort": logging.lastResort}
    sys.excepthook = mine["excepthook"]
    threading.excepthook = mine["thread"]
    sys.unraisablehook = mine["unraisable"]
    warnings.showwarning = mine["showwarning"]
    logging.lastResort = mine["last_resort"]

    def restore() -> None:
        if sys.excepthook is mine["excepthook"]:
            sys.excepthook = previous["excepthook"]
        if threading.excepthook is mine["thread"]:
            threading.excepthook = previous["thread"]
        if sys.unraisablehook is mine["unraisable"]:
            sys.unraisablehook = previous["unraisable"]
        if warnings.showwarning is mine["showwarning"]:
            warnings.showwarning = previous["showwarning"]
        if logging.lastResort is mine["last_resort"]:
            logging.lastResort = previous["last_resort"]
    return restore
