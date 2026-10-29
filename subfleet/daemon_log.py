"""What the daemon writes to daemon.log is scrubbed where it is written (C-25.7, D-LOG1).

The daemon's handler (`ScrubbingStreamHandler`) formats every record through
`ScrubbingFormatter`:

- a one-line message, with the line its format builds around it, in C-25.5's
  line form (`refusals.scrubbed_line`): scrubbed whole, folded to one line,
  scrubbed again, and cut at `refusals.LOG_LINE_MAX` without splitting a marker;
- a message of several lines (a lock watch's stacks, C-3.6), the record's
  traceback and its stack in the block form (`scrubbed_block`): still several
  lines, scrubbed whole (`scrub_secrets(..., whole=True)`).

Before either, a record's text (its line, traceback and stack together) is cut
to `RECORD_SCRUB_MAX` characters, keeping whole lines of each part's head and
tail with a marker, at line breaks no match of the whole-text scrub crosses,
folded or not (`handoff.cut_whole`). Every regex the cut runs reads within its
two windows, at most 16 times `RECORD_SCRUB_MAX` characters in all, counted by what
each call reads, not its span (`test_daemon_log_cut_windows`, `tests/regex_reads.py`).
Past them it reads only in C scans: a fixed number of scans of the text, plus at
most 20 needle looks (`handoff._event_from`, `_event_before`: one for each quote
kind open at a candidate cut, two plain kinds and at most eight escaped ones a
side), each at most 64 finds of its quote, which together read the text at most
once (both counted). So a cut's work is linear in the text; C-25.7 gives its cost
measured against plain text, a measurement and no bound. The scrub reads at most
`RECORD_SCRUB_MAX` characters. A record is formatted before the handler's lock is
taken, so only its write holds the lock.
A one-line message whose cut keeps only the marker is written as the marker
with the record's level and logger, never its words. No reminder tag is written:
the scrub strips each reminder whose two tags a record holds, and every tag left
(an orphan, in any case or fold) is written as `REMINDER_MARK` (`neutralised`).

The two forms agree. Folded, a block is exactly the line form's text before
its cut: `scrubbed_block` writes the folded, rescrubbed text in place of the
block whenever folding would let the scrubber find more. So a value holding no
whitespace is in the block exactly when it is in the uncut line, and the cut
only drops text. Python writes a margin before each line of an exception
group's members; a group's block keeps only what both it and the same text
without the margins show, so it may hide more than the line form, never less.
Whether a part is a group's is read before it is cut, which may keep none of
its separators; a text with a line that starts with a group's margin is read
as one too.

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
(`logging.lastResort`). What the daemon says there itself outside a logger (a
start refused for its policy or a held lock, a retired alias's note) goes
through it as well (`write_scrubbed`), and so does what the CLI prints of the
same text (`scrubbed`: `daemon start`'s refusal). What a person reads of
daemon.log, `daemon start`'s tail and `daemon logs`, is the file scrubbed as a
record is, its lines kept, with no reminder tag (`scrubbed_tail`): a file long
enough to be cut is read with its reminders resolved first (`_reminders_resolved`:
each reminder the whole-text scrub strips taken out, every other tag marked) where
that leaves the scrub's first pass replacing what it replaced, else the cut keeps
only its marker, as at round 5. `--follow` first prints what `daemon logs -n N`
prints; then each new line is held until no match can cross the break after it,
and written so (`LogFollower`). The file is never rewritten, so it may hold what a
daemon before D-LOG1, or before round 6 of #233, wrote: raw values, and tags.
faulthandler stays as it is. It writes each frame's file, line number and
function name only, never an exception's message, a local or a source line
(CPython's `Python/traceback.c`, `dump_frame`); on Python 3.14, an all-threads
dump (`daemon stacks`) also names each thread Python started, and every daemon
thread's name is fixed words or ids.
"""
from __future__ import annotations

import bisect
import copy
import functools
import logging
import re
import sys
import threading
import warnings
from typing import Any, Callable

from . import refusals
from .sessions import scrub_release
from .sessions.handoff import (REDACTED, _head_scrub_secrets, cut_marker, cut_whole, quoted_value_spans,
                                reminder_tags, settled_end, shown_by_both)

#: What a record that cannot be scrubbed is written as (C-25.7).
SCRUB_FAILED = "[scrub failed]"
#: The most of one record's text (its line, traceback and stack together) the
#: handler scrubs (C-25.7). Longer text is cut first (`handoff.cut_whole`), whose
#: regexes read only its windows, and whose C scans past them are a fixed number of
#: passes and at most 20 needle looks.
RECORD_SCRUB_MAX = 16_384
#: What logging's report of a record it could not write becomes.
WRITE_FAILED = "[log write failed]"
#: What the handler writes, and what a person reads of daemon.log shows, in place of a
#: reminder tag (C-25.7, review r4 of #233). One run with no whitespace, quote, `,`, `;`,
#: `:`, `=`, `/` or `@` and no key's ending, so every scrub rule reads it as it reads a tag,
#: or hides more around it: a value the tag was part of (`password=<system-reminder>…`)
#: stays one value.
REMINDER_MARK = "[reminder-tag]"

#: A level or logger name a fixed line may carry; anything else reads as `?`.
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _named(value: Any) -> str:
    return value if type(value) is str and _NAME.match(value) else "?"


#: The line Python's traceback module writes before each member of an exception
#: group (`+-+---------------- 1 ----------------`, `  +---…---- 2 ----…`).
_GROUP_SEPARATOR = re.compile(r"(?m)^ *(?:\+-)?\+-{16} (?:\d+|\.\.\.) -{16}$")
#: What every such separator holds, which one C scan finds in an uncut part.
_GROUP_MARK = "+" + "-" * 16
#: The margin it writes before each line of a group and of its members: two spaces
#: a level, then `|` (or `+`, a group's first line) and a space
#: (`traceback._ExceptionPrintContext.emit`).
_GROUP_MARGIN = re.compile(r"(?:  )+[|+](?: |$)")
#: A line that starts with that margin.
_GROUP_MARGIN_LINE = re.compile(r"(?m)^(?:  )+[|+](?: |$)")
#: A whitespace-free run, as a value is.
_RUN = re.compile(r"\S+")


def scrubbed_block(text: str, *, group: bool = False, keep_lines: bool = False) -> str:
    """`text` for daemon.log in the block form: its lines kept, scrubbed whole
    (C-23.14). When folding it to one line would let the scrubber find more, the
    folded, rescrubbed text is written instead, so that folded, the block is
    always the line form's text before the cut; with `keep_lines`, for a person
    reading daemon.log (`scrubbed_tail`, `LogFollower`), the lines are kept then
    too and each whitespace-free run is written only where the folded rescrub
    shows it, else `[REDACTED]` (`_kept_block`). An exception group's traceback
    hides at least what its members' plain tracebacks hide (`_group_block`): a
    text is read as one when `group` says so (the handler decides on the uncut
    record, whose cut may keep no separator: review r2 of #233), when it holds a
    group's separator, or when any of its lines starts with a group's margin.

    Release/217's block form (`scrub_release`, frozen) and the head's each read the whole
    text, fold and second scrub included, and release's is written, but for each run the
    head's does not hold, which becomes `[REDACTED]` (`handoff.shown_by_both`): so it
    shows nothing either form hides (C-23.14, #239 round 8)."""
    group = bool(group or _GROUP_SEPARATOR.search(text) or _GROUP_MARGIN_LINE.search(text))
    return shown_by_both(_block_form(scrub_release.scrub_secrets, text, group, keep_lines),
                         _block_form(_head_scrub_secrets, text, group, keep_lines))


def _block_form(scrub: Callable, text: str, group: bool, keep_lines: bool) -> str:
    """`scrubbed_block`'s form with one scrubber (`scrub`, as `scrub_secrets` is called)."""
    plain = functools.partial(_kept_block if keep_lines else _plain_block, scrub=scrub)
    block = plain(text)
    if group:
        return _group_block(text, block, plain)
    return block


def _plain_block(text: str, *, scrub: Callable) -> str:
    clean, _ = scrub(text, strip_reminders=True, whole=True)
    folded = " ".join(clean.split())
    again, _ = scrub(folded, strip_reminders=True, whole=True)
    return clean.rstrip("\r\n") if again == folded else again


def _kept_block(text: str, *, scrub: Callable) -> str:
    """`_plain_block`, its lines kept where folding lets the scrubber find more: each
    run of the block is written only if the folded, rescrubbed text holds it too
    (review r3 of #233, P3-3: `daemon start`'s tail had become one folded line)."""
    clean, _ = scrub(text, strip_reminders=True, whole=True)
    folded = " ".join(clean.split())
    again, _ = scrub(folded, strip_reminders=True, whole=True)
    block = clean.rstrip("\r\n")
    return block if again == folded else _only(block, set(_RUN.findall(again)))


def _group_block(text: str, block: str, plain: Callable[[str], str]) -> str:
    """An exception group's traceback, `block` being its block form as written.

    Python writes a margin (`    | `) before each line of a group's members. A value
    on the line after its key (`token:\n<value>`) then has the margin where the value
    was, and the scrubber takes the `|` for the value (review r1 of #233, P3). So the
    text is scrubbed again with the margins taken off, as the plain traceback reads,
    and the two are kept line by line where each hides: a run of the margin-free
    line is written only if the same line of `block` holds it too, else
    `[REDACTED]`, and the line keeps its margin. Where scrubbing joined lines (a
    multi-line value, `Bearer` and its token), the lines no longer pair up and the
    margin-free text is written whole, without margins, by the same rule. Either
    way nothing is written that `block` hides, so the block never shows what the
    line form hides, nor what the plain traceback hides. `plain` scrubs the
    margin-free text as `block` was scrubbed (`_plain_block`, or `_kept_block`)."""
    lines = text.rstrip("\r\n").split("\n")
    margins = [match.group() if (match := _GROUP_MARGIN.match(line)) else "" for line in lines]
    bare = plain("\n".join(line[len(margin):] for line, margin in zip(lines, margins))).split("\n")
    kept = block.split("\n")
    if len(bare) == len(kept) == len(lines):
        return "\n".join(margin + _only(line, set(_RUN.findall(within)))
                         for margin, line, within in zip(margins, bare, kept))
    return _only("\n".join(bare), set(_RUN.findall(block)))


def _only(text: str, shown: set[str]) -> str:
    """`text` with each whitespace-free run not in `shown` replaced by `[REDACTED]`."""
    return _RUN.sub(lambda run: run.group() if run.group() in shown else REDACTED, text)


def neutralised(text: str) -> str:
    """`text` with each reminder tag replaced by `REMINDER_MARK`: either tag, in any case,
    folded or not (`İ`, `ı`, `ſ`), as `handoff.reminder_tags` finds them (C-25.7). Run on
    what the handler writes and on what a person reads of daemon.log, after the scrub,
    which has stripped every reminder whose two tags the text holds."""
    return _marked(text, reminder_tags(text))


def _marked(text: str, tags: list[tuple[int, int, bool]]) -> str:
    """`text` with each of `tags` (`reminder_tags`) replaced by `REMINDER_MARK`."""
    if not tags:
        return text
    pieces, cursor = [], 0
    for start, end, _ in tags:
        pieces.extend((text[cursor:start], REMINDER_MARK))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


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


def _within(parts: list[str], total: int) -> list[str]:
    """`parts` cut to `total` characters together before they are scrubbed: each
    to its share, a part shorter than an even share leaving the rest to the others
    (`handoff.cut_whole`, which keeps whole lines of its head and tail)."""
    shares, left = [0] * len(parts), total
    for count, index in enumerate(sorted(range(len(parts)), key=lambda at: len(parts[at]))):
        shares[index] = min(len(parts[index]), left // (len(parts) - count))
        left -= shares[index]
    return [cut_whole(part, share) for part, share in zip(parts, shares)]


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
            # No reminder tag reaches daemon.log (review r4 of #233, P3-2): the scrub strips
            # a reminder whose two tags the record holds, and the tags left are marked.
            return neutralised(self._scrubbed(record))
        except Exception as exc:            # noqa: BLE001 - never lose the line, never raise into the caller
            return failed_line(record, exc)

    def _scrubbed(self, record: logging.LogRecord) -> str:
        view = copy.copy(record)
        view.message = view.getMessage()
        if self.usesTime():
            view.asctime = self.formatTime(view, self.datefmt)
        parts = [self.formatMessage(view), *self._blocks(record)]
        # Whether a part is an exception group's is read before the cut, which may keep
        # none of its separators (review r2 of #233). Any part holding a separator's
        # mark is read as one: one C scan, and reading a text as a group hides more,
        # never less.
        groups = [_GROUP_MARK in part for part in parts]
        (line, *blocks), message = _within(parts, RECORD_SCRUB_MAX), parts[0]
        if line != message and line == cut_marker(len(message)) + "\n":
            # Nothing of the line is kept: the marker names the record's level and
            # logger, fixed metadata, never its words.
            text = refusals.scrubbed_line(f"{cut_marker(len(message))} ({_named(record.levelname)} record "
                                          f"from {_named(record.name)})")
        elif len(line.splitlines()) > 1:
            text = scrubbed_block(line, group=groups[0])
        else:
            text = refusals.scrubbed_line(line)
        for block, group in zip(blocks, groups[1:]):
            text += "\n" + scrubbed_block(block, group=group)
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

    def handle(self, record: logging.LogRecord):
        """`logging.Handler.handle`, but the record is formatted, so scrubbed, before
        the handler's lock is taken: only its write holds the lock, and a line never
        waits for another thread's record to be formatted (review r1 of #233)."""
        passed = self.filter(record)
        if isinstance(passed, logging.LogRecord):
            record = passed
        if passed:
            text = self.format(record)          # `ScrubbingFormatter.format` never raises
            with self.lock:
                self._write(record, text)
        return passed

    def emit(self, record: logging.LogRecord) -> None:
        self._write(record, self.format(record))

    def _write(self, record: logging.LogRecord, text: str) -> None:
        """`logging.StreamHandler.emit`'s write, of text already formatted."""
        try:
            self.stream.write(text + self.terminator)
            self.flush()
        except RecursionError:                  # as logging's own: issue 36272
            raise
        except Exception:                       # noqa: BLE001 - reported by `handleError`, a fixed line
            self.handleError(record)

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


def _record(message: str, level: int, exc_info: tuple | None = None) -> logging.LogRecord:
    return logging.makeLogRecord({"name": "subfleet.daemon", "levelno": level,
                                  "levelname": logging.getLevelName(level), "msg": message, "args": None,
                                  "exc_info": exc_info})


def _report(handler: logging.Handler, level: int, words: Callable[[], str], exc_info: tuple | None,
            left_out: str) -> None:
    """A record of `words()` written by `handler`; never raises. What cannot be
    rendered is not written, and a failure past the handler's own writes the
    fixed `left_out` line."""
    try:
        try:
            message = words()
        except Exception:                   # noqa: BLE001 - what cannot be rendered is not written
            message = "(its description cannot be rendered)"
        handler.handle(_record(message, level, exc_info))
    except Exception:                       # noqa: BLE001 - never raises into the caller
        try:
            sys.stderr.write(f"{SCRUB_FAILED} {left_out}\n")
        except Exception:                   # noqa: BLE001
            pass


def scrubbed(text: str) -> str:
    """`text` as the daemon's handler writes a record of it (C-25.7): cut to
    `RECORD_SCRUB_MAX`, then one line in the line form, several in the block
    form, or the fixed `[scrub failed]` line. For what the CLI prints of the
    daemon's words (`daemon start`'s refusal, its tail of daemon.log)."""
    return ScrubbingFormatter().format(_record(text, logging.INFO))


def write_scrubbed(text: str, level: int = logging.ERROR) -> None:
    """Write `text` to this process's stderr as the daemon's handler writes a
    record of it (`scrubbed`; a failed write is a fixed line). For what the daemon
    says on stderr, which is daemon.log under `daemon start` and launchd alike,
    outside a logger: a start refused for its policy or a held lock, a retired
    alias's note (C-25.7, C-11.1). Never raises."""
    _report(_StderrHandler(), level, lambda: text, None, "a line is left out")


def _tail_start(text: str, count: int) -> int:
    """Where `text`'s last `count` lines start (0 when it has no more)."""
    at = len(text) - 1 if text.endswith("\n") else len(text)
    for _ in range(count):
        at = text.rfind("\n", 0, at)
        if at < 0:
            return 0
    return at + 1


def _tail_limit(text: str, count: int) -> int:
    """What `scrubbed_tail` cuts `text` to: `RECORD_SCRUB_MAX` or, for more lines than its
    tail holds, twice what its last `count` lines hold and `RECORD_SCRUB_MAX` more."""
    return max(RECORD_SCRUB_MAX, 2 * (len(text) - _tail_start(text, count)) + RECORD_SCRUB_MAX)


def scrubbed_tail(text: str, count: int) -> list[str]:
    """The last `count` lines of `text`, a daemon.log, for a person's terminal (C-25.7:
    `daemon start`'s tail, `daemon logs`): the text scrubbed as the daemon's handler
    scrubs a record of it, cut first (`cut_whole`) to `_tail_limit`, then in the block
    form with its lines kept (`scrubbed_block(..., keep_lines=True)`), then its last
    `count` lines, with no reminder tag (`neutralised`). Whoever wrote the file, an
    earlier daemon included, no line shows a value the handler's own forms would hide.

    The cut keeps only its marker for a text that holds a reminder tag, and daemon.log is
    never rewritten, so at round 5 one tag anywhere left every later tail only the marker
    (review r4 of #233, P3-2). A text that must be cut is read with its reminders resolved
    first (`_reminders_resolved`), so it has no tag left, except where taking them out
    would change what the scrub's first pass replaces, which keeps that rule (review r5,
    P2-1); one that need not be is scrubbed whole, as before. Never raises: what cannot
    be scrubbed is the fixed `[scrub failed]` line."""
    if count <= 0:
        return []
    try:
        group = _GROUP_MARK in text
        if len(text) > _tail_limit(text, count):
            resolved = _reminders_resolved(text)
            if resolved is not None:        # else the marker rule: a tag keeps only the marker
                text = resolved
        block = scrubbed_block(cut_whole(text, _tail_limit(text, count)), group=group, keep_lines=True)
        return neutralised(block).splitlines()[-count:]
    except Exception as exc:                # noqa: BLE001 - the marker alone, never the text
        return [failed_line(_record("", logging.INFO), exc)]


def _reminders_resolved(text: str) -> str | None:
    """`text` with no reminder tag left, hiding what the whole-text scrub of `text` hides,
    for `scrubbed_tail` to cut (C-25.7, review r4 of #233, P3-2); None where it cannot say.

    - No tag: `text` itself.
    - No opening tag before a closing one: no reading of the text strips anything (its
      quoted values masked, its margins taken off, folded: each removes tags or keeps them
      in order, never adds or reorders one), so each tag is replaced by `REMINDER_MARK`,
      which every scrub rule reads as it reads a tag.
    - Otherwise each reminder the whole-text scrub strips is taken out, as that scrub finds
      it: after its first pass has masked quoted values across lines, the first opening tag
      left, to the first closing one after it, and so on (`_taken_out`). Taking them out
      joins what was on either side of each, as the strip does. Then every tag left is
      replaced by the marker.

    That scrub runs its first pass on `text` and strips the reminders only after it, but
    the cut and the tail's scrub run every pass on what is left. So what is left is used
    only where its first pass replaces exactly what the first pass over `text` replaces
    outside those reminders, each with the same words (review r5 of #233, P2-1: a reminder
    between a key's separator and a quote, `password=<system-reminder>…</system-reminder>"`,
    opened a value there that ran past a quote the first pass had masked, or swallowed a
    private key's or a data URI's first line). Then the whole-text scrub of what is left is,
    pass for pass, that of `text`. The same holds of the text read without a group's
    margins, as the block form also reads it, when it holds one: its reminders are the same,
    and taking them out leaves the margin-free reading of what is left, but for the line
    breaks that reminders ending the file leave before them, which the block form does not
    read and no rule but a data URI's body counts as hiding more (`_alike`, review r6 of
    #233, P3-A; the header rule masks a trailing `\\r` itself, which reveals nothing,
    review r1 of #251, P3-1).

    None (the marker rule: the cut keeps only its marker) where either does not hold, where
    those line breaks bring a data URI body that ends there to the 32 characters its rule
    needs, or where taking reminders out makes a tag where none was. Its cost, besides
    finding the tags in C scans: with orphans only, a marked copy; with reminders to take
    out, two passes of the quoted-value rule over the text (four, with a group's margins),
    and copies."""
    tags = reminder_tags(text)
    if not tags:
        return text
    first = next((start for start, _, opens in tags if opens), None)
    if first is None or not any(start > first and not opens for start, _, opens in tags):
        return _marked(text, tags)
    resolved = _taken_out(text, tags)
    if resolved is None:
        return None
    kept, pairs = resolved
    if _GROUP_MARGIN_LINE.search(text) is not None:
        bare = _margin_free(text)
        bare_tags = reminder_tags(bare)
        if len(bare_tags) != len(tags):
            return None
        bare_resolved = _taken_out(bare, bare_tags)
        if bare_resolved is None or bare_resolved[1] != pairs or not _alike(bare_resolved[0], _margin_free(kept)):
            return None
    kept_tags = reminder_tags(kept)
    if len(kept_tags) != len(tags) - sum(closing - opening + 1 for opening, closing in pairs):
        return None                         # a tag made where a reminder was taken out
    return _marked(kept, kept_tags)


def _margin_free(text: str) -> str:
    """`text` read without a group's margins, as `_group_block` reads it."""
    return "\n".join(line[len(match.group()):] if (match := _GROUP_MARGIN.match(line)) else line
                     for line in text.rstrip("\r\n").split("\n"))


#: The fewest body characters `handoff._DATA_URI_RE` takes for a data URI's body.
_BODY_MIN = 32
#: A data URI's body that runs to a text's end, as `_DATA_URI_RE` reads one (the same class
#: and flags): its `;base64,`, then body characters, line breaks among them.
_BODY_AT_END = re.compile(r"(?i);base64,[A-Za-z0-9+/=\s]*\Z")
#: How far from a text's end `_BODY_AT_END` is looked for: `;base64,` and the most body
#: characters that are still short of `_BODY_MIN`.
_BODY_REACH = len(";base64,") + _BODY_MIN - 1


def _alike(left: str, kept: str) -> bool:
    """Whether `left`, a group's text read without its margins with its reminders taken
    out, scrubs as `kept`, the margin-free reading of what is left (`_margin_free`), the
    text the block form reads (review r6 of #233, P3-A). `_margin_free` strips a text's
    trailing line breaks, as `_group_block` does, so where reminders end the file, `left`
    keeps the line break before them and `kept` does not. One scrub rule reads such a
    break to hide more: `_DATA_URI_RE` counts it among the 32 characters a body needs.
    (`_HEADER_RE` masks a trailing `\\r` as a header's value, but that mask covers only the
    `\\r`, and the CLI reads with universal newlines; review r1 of #251, P3-1.) So the two are
    alike when equal, or when equal but for trailing line breaks and no data URI body
    ends there that `left`'s breaks bring to 32 characters and `kept`'s leave short (a
    `;base64,` within `_BODY_REACH` characters of the end, body characters after it; its
    prefix is not read, so a `;base64,` with none counts too). With the breaks stripped
    alone, a body of 31 characters before a reminder that ends the file was hidden by the
    whole-text scrub and shown by the tail."""
    if left == kept:
        return True
    end = kept.rstrip("\r\n")
    if left.rstrip("\r\n") != end:
        return False
    body = _BODY_AT_END.search(end, max(0, len(end) - _BODY_REACH))
    if body is None:
        return True
    run = len(end) - body.start() - len(";base64,")
    return not run + len(kept) - len(end) < _BODY_MIN <= run + len(left) - len(end)


def _taken_out(text: str, tags: list[tuple[int, int, bool]]) -> tuple[str, list[tuple[int, int]]] | None:
    """`text` with the reminders the whole-text scrub of `text` strips taken out, and those
    reminders as indexes into `tags` (each its opening and its closing tag): `_resolve_blocks`
    after the scrub's first pass (`quoted_value_spans`), so a tag inside what that pass
    replaces is none. None unless that pass over what is left replaces exactly what it
    replaces in `text` outside those reminders, by the same words (review r5 of #233, P2-1).
    Every mode reads the text with release/217's scrub (`scrub_release`) and the head's
    (#239 round 8), each stripping reminders after its own first pass, so both first passes
    must take out the same reminders, each so."""
    released = _taken_out_by(scrub_release.quoted_value_spans, text, tags)
    return released if released is not None and released == _taken_out_by(quoted_value_spans, text, tags) else None


def _taken_out_by(first_pass: Callable, text: str,
                  tags: list[tuple[int, int, bool]]) -> tuple[str, list[tuple[int, int]]] | None:
    """`_taken_out` for one scrubber, `first_pass` its `quoted_value_spans`."""
    spans = first_pass(text)
    starts = [start for start, _, _ in spans]
    pairs, opening = [], None
    for index, (start, _, opens) in enumerate(tags):
        at = bisect.bisect_right(starts, start) - 1
        if at >= 0 and start < spans[at][1]:
            continue                        # inside a value the first pass replaces
        if opening is None:
            if opens:
                opening = index
        elif not opens:
            pairs.append((opening, index))
            opening = None
    if not pairs:
        return text, pairs
    removed = [(tags[opening][0], tags[closing][1]) for opening, closing in pairs]
    pieces, cursor = [], 0
    for begin, end in removed:
        pieces.append(text[cursor:begin])
        cursor = end
    pieces.append(text[cursor:])
    kept = "".join(pieces)
    expected, shift, index = [], 0, 0
    for start, end, out in spans:
        while index < len(removed) and removed[index][1] <= start:
            shift += removed[index][1] - removed[index][0]
            index += 1
        if index < len(removed) and removed[index][0] < end:
            if removed[index][0] <= start and end <= removed[index][1]:
                continue                    # taken out with its reminder
            return None                     # across a reminder's edge: never, as its tags show
        expected.append((start - shift, end - shift, out))
    return (kept, pairs) if first_pass(kept) == expected else None


#: The most text `daemon logs --follow` holds back while no break in it is settled
#: (C-25.7): past it, what it holds is written, scrubbed as it is.
FOLLOW_HOLD_MAX = RECORD_SCRUB_MAX
#: How much of what it has written `daemon logs --follow` scrubs new lines beside.
FOLLOW_CONTEXT = RECORD_SCRUB_MAX // 2


def _context_of(text: str) -> str:
    """The last `FOLLOW_CONTEXT` characters of `text` at most, from a line start."""
    if len(text) <= FOLLOW_CONTEXT:
        return text
    newline = text.find("\n", len(text) - FOLLOW_CONTEXT - 1)
    return "" if newline < 0 else text[newline + 1:]


class LogFollower:
    """daemon.log's new text, as `daemon logs --follow` writes it (C-25.7).

    Lines arrive as the daemon writes them, so a key, a flag or `Bearer` may end a line
    whose value the next one starts, and a quoted value, a private key or a data URI
    may not have ended yet, nor a private key's first line (`-----BEGIN RSA`, then
    `PRIVATE KEY-----`). Whole lines are held until a break after them is settled
    (`handoff.settled_end`): no match of the whole-text scrub could cross it, whatever
    is written next. They are then written in the block form with their lines kept, each
    run only where the same lines scrubbed after what came before them show it too (up
    to `FOLLOW_CONTEXT` characters of what was settled, and all that was not), and with
    no reminder tag (`neutralised`). Held text past `FOLLOW_HOLD_MAX` is written as it
    is, scrubbed so; `flush` writes what is held when following stops.

    `before` is the file as following starts, which the caller prints as `daemon logs
    -n N` does (`scrubbed_tail`), so following starts with the same lines (review r5 of
    #233, P3-2). Its end that is not settled yet (`shown`, at most `FOLLOW_HOLD_MAX`
    characters of whole lines) is printed then, and read again with what comes after
    it, by the break rule and by the scrub, until a break after it settles; only what
    arrives afterwards is held. A last line still being written is held whole, and
    written again once it ends."""

    def __init__(self, before: str):
        complete = before.rfind("\n") + 1
        start = max(0, complete - FOLLOW_CONTEXT - FOLLOW_HOLD_MAX)
        if start:
            newline = before.find("\n", start - 1, complete)
            start = complete if newline < 0 else newline + 1
        try:
            settled = start + settled_end(before[start:complete], 0)
        except Exception:                   # noqa: BLE001 - unsettled, so read again to the bound
            settled = start
        if complete - settled > FOLLOW_HOLD_MAX:
            settled = complete              # past the bound: as if settled, as held text is
        self.context = _context_of(before[:settled])
        self.shown = before[settled:complete]
        self.pending = before[complete:]

    def feed(self, text: str) -> list[str]:
        """The lines to write once `text` is appended to the file."""
        self.pending += text
        complete = self.pending.rfind("\n") + 1
        if not complete:
            return []
        before = self.context + self.shown
        try:
            settled = settled_end(before + self.pending[:complete], len(self.context)) - len(before)
        except Exception:                   # noqa: BLE001 - unsettled, so held to the bound
            settled = 0
        if settled <= 0:
            if complete <= FOLLOW_HOLD_MAX:
                return []
            settled = complete              # held past the bound: written as it is
        return self._write(settled)

    def flush(self) -> list[str]:
        """What is held, written when following stops: nothing more can come to it."""
        return self._write(len(self.pending)) if self.pending else []

    def _write(self, end: int) -> list[str]:
        written, self.pending = self.pending[:end], self.pending[end:]
        before = self.context + self.shown
        try:
            own = neutralised(scrubbed_block(written, group=_GROUP_MARK in written, keep_lines=True))
            if before:
                around = before + written
                shown = neutralised(scrubbed_block(around, group=_GROUP_MARK in around, keep_lines=True))
                # A margin's mark alone is no value; `shown` may be written without margins.
                own = _only(own, set(_RUN.findall(shown)) | {"|", "+"})
            lines = own.splitlines()
        except Exception as exc:            # noqa: BLE001 - the marker alone, never the text
            lines = [failed_line(_record("", logging.INFO), exc)]
        self.context, self.shown = _context_of(before + written), ""
        return lines


def install_process_hooks() -> Callable[[], None]:
    """Send what Python writes to this process's stderr (daemon.log, under `daemon
    start` and launchd alike) through `ScrubbingFormatter`: an uncaught exception's
    traceback, a thread's, an unraisable one's, a warning, and a record that found
    no handler. Returns what puts back each hook still this one."""
    handler = _StderrHandler()

    def report(level: int, words: Callable[[], str], exc_info: tuple | None) -> None:
        _report(handler, level, words, exc_info, "an uncaught exception is left out")

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
