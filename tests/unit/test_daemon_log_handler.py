"""D-LOG1 (C-25.7): the daemon's handler scrubs every record it writes, and the
process hooks send Python's own stderr output through the same formatter.

The line form is C-25.5's (`refusals.scrubbed_line`); the block form keeps a
traceback's lines (`daemon_log.scrubbed_block`). Folded, a block is exactly the
uncut line form, so neither form shows a value the other hides. Every secret is a
`SECRET7…` dummy.
"""

from __future__ import annotations

import contextlib
import gc
import io
import logging
import os
import re
import sys
import threading
import time
import traceback
import warnings

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import daemon_log, refusals
from tests.unit.test_daemon_log_scrubbing import (
    KEY_QUOTES, KEYS, SEPARATORS, SUFFIXES, VALUE_STYLES, _assignment,
)

#: The uncut line form: `refusals.scrubbed_line` with no bound in reach.
UNCUT = 10 ** 9
BOUNDED = settings(max_examples=40, derandomize=True, database=None, deadline=None)


def fold(text: str) -> str:
    return " ".join(text.split())


class Written:
    """A logger whose only handler is the daemon's, writing to memory."""

    def __init__(self, name: str = "subfleet.daemon.test-handler"):
        self.stream = io.StringIO()
        self.handler = daemon_log.ScrubbingStreamHandler(self.stream)
        self.log = logging.getLogger(name)
        self._saved = (self.log.level, self.log.propagate)
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)
        self.log.propagate = False

    def text(self) -> str:
        return self.stream.getvalue()

    def close(self) -> None:
        self.log.removeHandler(self.handler)
        self.log.setLevel(self._saved[0])
        self.log.propagate = self._saved[1]


@pytest.fixture
def written():
    log = Written()
    try:
        yield log
    finally:
        log.close()


#: What the cases raise. Kept out of the `raise` lines, which a traceback shows: a
#: source line that opens a quoted credential value is masked to its closing quote,
#: lines between included (C-23.14's whole-text rule), and would decide a case.
CAUSE = "cannot read token=SECRET7cause9"
RUNNER = "app-server said: password='SECRET7runner9\n\""
STACK_WORDS = "token=SECRET7msg9"
LOCAL = "SECRET7local9"
THREAD = "worker said: client_secret: SECRET7thread9"
MAIN = "main said: Authorization: Bearer SECRET7mainthread9value"
REPR = "<Leak password=SECRET7repr9>"
FINALIZER = "finalizer said: password='SECRET7unraisable9'"
WARNING = "policy said: api_key = SECRET7warning9"


def raised(text: str) -> BaseException:
    try:
        raise RuntimeError(text)
    except RuntimeError as exc:
        return exc


def traceback_of(exc: BaseException) -> str:
    return logging.Formatter().formatException((type(exc), exc, exc.__traceback__))


SECRETS = SUFFIXES.map(lambda suffix: "SECRET7" + suffix)
#: One credential assignment and its secret, in the shapes the scrubbing tests build.
ASSIGNMENTS = st.tuples(KEYS, st.sampled_from(sorted(KEY_QUOTES)), st.sampled_from(SEPARATORS),
                        st.sampled_from(sorted(VALUE_STYLES)), SECRETS).map(
    lambda drawn: (_assignment(*drawn), drawn[4]))
#: Text between them. It cannot spell a header or `Bearer`: a header that takes the
#: key after it is a known scrubber rule-order case, the same in every form (#225 r2).
FILLER = st.text(alphabet="xyz0123456789 ,;()/.-_", max_size=30)
#: Messages and exception texts of one to three assignments, on one line or several.
TEXTS = st.lists(st.tuples(FILLER, st.sampled_from(["", " ", "\n", "\n  "]), ASSIGNMENTS), min_size=1, max_size=3)


def _text(parts) -> tuple[str, list[str]]:
    return ("".join(f"{filler}{gap}{assignment} " for filler, gap, (assignment, _) in parts),
            [secret for _, _, (_, secret) in parts])


# --- the formatter -------------------------------------------------------------


@BOUNDED
@given(message=st.text(max_size=300).filter(lambda text: len(text.splitlines()) <= 1))
def test_a_one_line_record_is_exactly_its_line_form(message):
    """Differential: what the handler writes for a one-line record is
    `refusals.scrubbed_line` of its message, whatever the message."""
    log = Written("subfleet.daemon.test-differential")
    try:
        log.log.warning("%s", message)
        assert log.text() == refusals.scrubbed_line(message) + "\n"
    finally:
        log.close()


@pytest.mark.parametrize("message", [
    "config error: password='SECRET7oneline9' at line 4",
    "x" * 2500 + " token=SECRET7long9",
    "{\\'password\\': \\'SECRET7quotedkey9\\', 'note': \"it's\"}",
])
def test_one_line_examples_match_the_line_form(written, message):
    written.log.info("%s", message)
    assert written.text() == refusals.scrubbed_line(message) + "\n"
    assert "SECRET7" not in written.text()


@BOUNDED
@given(message=TEXTS, words=TEXTS)
def test_nothing_the_handler_writes_keeps_a_secret_and_each_form_is_exact(message, words):
    """Property: for messages and exception texts built from credential shapes
    (mixed-case and quoted keys, `=`, `: ` and ` = `), no secret survives, the
    message is in its line or block form, the traceback in its block form, and
    folded, each block is the uncut line form of what it scrubbed."""
    text, secrets = _text(message)
    exc_text, exc_secrets = _text(words)
    exc = raised(exc_text)
    log = Written("subfleet.daemon.test-property")
    try:
        log.log.error("%s", text, exc_info=(type(exc), exc, exc.__traceback__))
        out = log.text()
    finally:
        log.close()
    for secret in secrets + exc_secrets:
        assert secret not in out, (secret, out)
    tb = traceback_of(exc)
    line = (daemon_log.scrubbed_block(text) if len(text.splitlines()) > 1 else refusals.scrubbed_line(text))
    assert out == line + "\n" + daemon_log.scrubbed_block(tb) + "\n"
    for source in (text, tb):
        assert fold(daemon_log.scrubbed_block(source)) == fold(refusals.scrubbed_line(source, UNCUT))


@BOUNDED
@given(text=st.text(max_size=200))
def test_a_block_folded_is_the_uncut_line_form_of_any_text(text):
    """Why neither form keeps what the other hides: folded, the block form is the
    uncut line form. A value with no whitespace in it is in one exactly when it is
    in the other; the line form's cut only drops text."""
    block = daemon_log.scrubbed_block(text)
    assert fold(block) == fold(refusals.scrubbed_line(text, UNCUT))
    assert refusals.scrubbed_line(text).removesuffix("…") in fold(refusals.scrubbed_line(text, UNCUT)) + " "


@pytest.mark.parametrize("text", [
    "\n  Cookie\n:\t\"SECRET7cookie9\"\n\ntail",
    "\n\nAuthorization:\n'SECRET7auth9\n  tail",
])
def test_a_block_that_folding_scrubs_further_is_written_folded(text):
    """Folded, these let the header rule take what follows; the block form writes
    that folded text, so it never shows what the line form hides."""
    block = daemon_log.scrubbed_block(text)
    assert "SECRET7" not in block and "tail" not in block
    assert block == refusals.scrubbed_line(text, UNCUT)


def test_a_multi_line_message_keeps_its_lines(written):
    """C-3.6: a lock watch's stacks stay readable; they are scrubbed whole."""
    stack = "".join(traceback.format_stack(limit=5))
    written.log.warning("%s", "store lock held 9.0 s. Holder's stack:\n" + stack + "token=SECRET7stack9\n")
    out = written.text()
    assert "SECRET7stack9" not in out
    assert out.startswith("store lock held 9.0 s. Holder's stack:\n")
    assert out.count("\n") >= stack.count("\n") + 1
    assert out == daemon_log.scrubbed_block("store lock held 9.0 s. Holder's stack:\n" + stack
                                            + "token=SECRET7stack9\n") + "\n"


def test_a_traceback_stays_multi_line_and_loses_its_secret(written):
    try:
        try:
            raise OSError(CAUSE)
        except OSError as cause:
            raise RuntimeError(RUNNER) from cause
    except RuntimeError:
        written.log.exception("turn runner %s failed", "job/a1")
    out = written.text()
    assert "SECRET7runner9" not in out and "SECRET7cause9" not in out
    assert out.startswith("turn runner job/a1 failed\nTraceback (most recent call last):\n")
    assert "The above exception was the direct cause" in out and "RuntimeError: app-server said:" in out


def test_a_stack_is_scrubbed_whole(written):
    def leaf(token):            # the frame shows the call's source line, never its locals
        written.log.warning("where %s", STACK_WORDS, stack_info=True)
    leaf(LOCAL)
    out = written.text()
    assert "SECRET7msg9" not in out and "SECRET7local9" not in out
    assert "Stack (most recent call last):" in out and "in leaf" in out


def test_an_exc_text_another_formatter_cached_is_not_used_raw(written):
    """`exc_info` is formatted afresh; a cached `exc_text` alone is scrubbed too."""
    exc = raised("password=SECRET7cached9")
    record = written.log.makeRecord(written.log.name, logging.ERROR, __file__, 1, "failed", None,
                                    (type(exc), exc, exc.__traceback__))
    record.exc_text = "a cache: password=SECRET7cached9"
    written.handler.handle(record)
    bare = written.log.makeRecord(written.log.name, logging.ERROR, __file__, 1, "failed", None, None)
    bare.exc_text = "Traceback:\nValueError: token=SECRET7text9"
    written.handler.handle(bare)
    out = written.text()
    assert "SECRET7" not in out and "RuntimeError: password=[REDACTED]" in out
    assert record.exc_text == "a cache: password=SECRET7cached9"        # the caller's record is as it was


# --- a scrubber that fails -------------------------------------------------------


def test_a_scrubber_that_raises_writes_the_fixed_marker(written, monkeypatch):
    """Nothing raised in the scrubber reaches the caller, and the record is not
    lost: its level, logger and exception class are written with the marker."""
    def broken(*args, **kwargs):
        raise RecursionError("scrubber broke on password=SECRET7broke9")
    monkeypatch.setattr(daemon_log.refusals, "scrubbed_line", broken)
    monkeypatch.setattr(daemon_log, "_head_scrub_secrets", broken)   # the block form's head pass (#239 round 8)
    exc = raised("token=SECRET7carried9")
    written.log.error("job %s failed: %s", "j1", "password=SECRET7message9",
                      exc_info=(type(exc), exc, exc.__traceback__))
    written.log.warning("plain %s", "api_key=SECRET7plain9")
    assert written.text().splitlines() == [
        "[scrub failed] ERROR record from subfleet.daemon.test-handler (RuntimeError) left out: RecursionError",
        "[scrub failed] WARNING record from subfleet.daemon.test-handler left out: RecursionError",
    ]


def test_a_message_that_cannot_be_rendered_writes_the_marker_and_not_its_arguments(written, capsys):
    """The handler itself, as the daemon's logger calls it (pytest's own capture
    handler would raise on such a record before ours ran)."""
    record = written.log.makeRecord(written.log.name, logging.INFO, __file__, 1, "%s %s",
                                    ("token=SECRET7args9",), None)         # one argument short
    written.handler.handle(record)
    assert written.text() == "[scrub failed] INFO record from subfleet.daemon.test-handler left out: TypeError\n"
    assert "SECRET7" not in capsys.readouterr().err


def test_a_failed_write_reports_a_fixed_line_not_the_raw_arguments(capsys):
    """logging's own report prints a record's message and arguments to stderr,
    which is daemon.log; the daemon's handler writes a fixed line instead."""
    class Full(io.StringIO):
        def write(self, text):
            raise OSError(28, "No space left on device")
    handler = daemon_log.ScrubbingStreamHandler(Full())
    record = logging.makeLogRecord({"name": "subfleet.daemon.full", "levelno": logging.WARNING,
                                    "levelname": "WARNING", "msg": "said %s", "args": ("token=SECRET7full9",)})
    handler.handle(record)
    err = capsys.readouterr().err
    assert err == "[log write failed] WARNING record from subfleet.daemon.full left out: OSError\n"


def test_the_marker_names_only_checked_names():
    record = logging.makeLogRecord({"name": "token=SECRET7name9", "levelname": "password=SECRET7level9"})
    assert daemon_log.failed_line(record, ValueError("SECRET7")) == "[scrub failed] ? record from ? left out: ValueError"


# --- what the daemon writes to stderr itself, and the CLI prints of it (#233 round 4) --


@BOUNDED
@given(text=st.text(max_size=300))
def test_scrubbed_is_the_handler_s_line_for_a_record_of_any_text(text):
    """Differential: `daemon_log.scrubbed`, which `write_scrubbed` writes and the CLI
    prints of the daemon's words, is what the daemon's handler writes for a record
    of the text, on one line or several."""
    log = Written("subfleet.daemon.test-scrubbed")
    try:
        log.log.info("%s", text)
        assert log.text() == daemon_log.scrubbed(text) + "\n"
    finally:
        log.close()


@BOUNDED
@given(message=TEXTS)
def test_scrubbed_keeps_no_secret_of_any_credential_shape(message):
    text, secrets = _text(message)
    shown = daemon_log.scrubbed(text)
    for secret in secrets:
        assert secret not in shown, (secret, shown)


#: #222's refusal as `daemon.main` writes it, its key's value a secret.
REFUSAL = "subfleet daemon: /r/policy.json: admission.token=SECRET7direct9: is not an admission setting (disk, ramp)"


def test_write_scrubbed_writes_the_handler_s_line_to_stderr_as_it_is_then(capsys):
    daemon_log.write_scrubbed(REFUSAL)
    assert capsys.readouterr().err == ("subfleet daemon: /r/policy.json: admission.token=[REDACTED] "
                                       "is not an admission setting (disk, ramp)\n")
    later = io.StringIO()
    with contextlib.redirect_stderr(later):
        daemon_log.write_scrubbed(REFUSAL)
    assert later.getvalue() == daemon_log.scrubbed(REFUSAL) + "\n"
    assert capsys.readouterr().err == ""


def test_write_scrubbed_with_a_scrubber_that_raises_writes_the_fixed_marker(monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RecursionError("scrubber broke on password=SECRET7broke9")
    monkeypatch.setattr(daemon_log.refusals, "scrubbed_line", broken)
    monkeypatch.setattr(daemon_log, "_head_scrub_secrets", broken)   # the block form's head pass (#239 round 8)
    daemon_log.write_scrubbed(REFUSAL)
    assert capsys.readouterr().err == "[scrub failed] ERROR record from subfleet.daemon left out: RecursionError\n"
    assert daemon_log.scrubbed(REFUSAL) == "[scrub failed] INFO record from subfleet.daemon left out: RecursionError"


def test_write_scrubbed_never_raises_when_stderr_cannot_be_written(monkeypatch):
    class Full(io.StringIO):
        def write(self, text):
            raise OSError(28, "No space left on device")
    monkeypatch.setattr(sys, "stderr", Full())
    daemon_log.write_scrubbed(REFUSAL)                  # nowhere left to say it, and no raise
    monkeypatch.setattr(sys, "stderr", None)
    daemon_log.write_scrubbed(REFUSAL)


# --- the process hooks -----------------------------------------------------------


@pytest.fixture
def hooks(capsys):
    """The process hooks installed, writing to stderr (pytest's capture), and put back after."""
    before = (sys.excepthook, threading.excepthook, sys.unraisablehook, warnings.showwarning, logging.lastResort)
    restore = daemon_log.install_process_hooks()
    try:
        yield lambda: capsys.readouterr().err
    finally:
        restore()
        assert (sys.excepthook, threading.excepthook, sys.unraisablehook, warnings.showwarning,
                logging.lastResort) == before


def test_an_uncaught_exception_in_a_thread_is_written_scrubbed(hooks):
    def work():
        raise RuntimeError(THREAD)
    thread = threading.Thread(target=work, name="subfleet-io_0")
    thread.start()
    thread.join()
    out = hooks()
    assert "SECRET7thread9" not in out
    assert out.startswith("Exception in thread subfleet-io_0\nTraceback (most recent call last):\n")
    assert "RuntimeError: worker said: client_secret: [REDACTED]" in out


def test_a_thread_that_exits_is_not_reported(hooks):
    thread = threading.Thread(target=sys.exit, args=(3,))
    thread.start()
    thread.join()
    assert hooks() == ""


def test_an_uncaught_exception_in_the_main_thread_is_written_scrubbed(hooks):
    exc = raised(MAIN)
    sys.excepthook(type(exc), exc, exc.__traceback__)
    out = hooks()
    assert "SECRET7mainthread9value" not in out
    assert out.startswith("uncaught exception\nTraceback (most recent call last):\n") and "RuntimeError:" in out


def test_an_unraisable_exception_is_written_scrubbed(hooks):
    class Leak:
        def __repr__(self):
            return REPR

        def __del__(self):
            raise ValueError(FINALIZER)
    Leak()
    gc.collect()
    out = hooks()
    assert "SECRET7unraisable9" not in out and "SECRET7repr9" not in out
    assert "Exception ignored" in out and "ValueError: finalizer said: password=" in out


def test_a_warning_is_written_scrubbed(hooks):
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(WARNING, UserWarning)
    out = hooks()
    assert "SECRET7warning9" not in out and "UserWarning: policy said: api_key = [REDACTED]" in out


def test_a_record_no_handler_takes_is_written_scrubbed(hooks):
    record = logging.makeLogRecord({"name": "subfleet.folders", "levelno": logging.WARNING, "levelname": "WARNING",
                                    "msg": "resource identity unavailable for %s",
                                    "args": ("/x/token=" + "SECRET7resort9",)})
    logging.lastResort.handle(record)
    out = hooks()
    assert "SECRET7resort9" not in out and out.startswith("resource identity unavailable for /x/token=")


def test_restoring_leaves_a_hook_someone_else_installed_since():
    before = sys.excepthook

    def theirs(*args):
        return None
    restore = daemon_log.install_process_hooks()
    sys.excepthook = theirs
    try:
        restore()
        assert sys.excepthook is theirs
        assert threading.excepthook is not None and logging.lastResort is not None
    finally:
        sys.excepthook = before


def test_the_daemon_writes_through_the_scrubbing_handler():
    """`Daemon.__init__` makes this handler; main installs the hooks (both read
    here from the source, as the real-daemon tests run them)."""
    import inspect
    from subfleet import daemon as daemon_module
    init = inspect.getsource(daemon_module.Daemon.__init__)
    assert "daemon_log.ScrubbingStreamHandler(os.fdopen(log_fd" in init
    assert "logging.StreamHandler(" not in init
    assert "daemon_log.install_process_hooks()" in inspect.getsource(daemon_module.main)


# --- exception groups (review r1 of #233, P3) -------------------------------------

#: Values on the line after their key. Inside an exception group Python writes a
#: `    | ` margin before each line of a member, and the scrubber took the `|` as the value.
NEXT_LINE_SHAPES = ["token:\n", "token =\n  ", "Authorization: Bearer\n", "Cookie: x_auth-token :\n "]
#: What a group's own lines and its members' lines start with: two spaces a level, then `| ` (or `+ `).
MARGIN = re.compile(r"(?:  )+[|+](?: |$)")


def group_of(members: list[BaseException], *, depth: int = 1, raise_it: bool = True,
             message: str = "several failed") -> BaseException:
    group: BaseException = ExceptionGroup(message, members)
    for _ in range(depth - 1):
        group = ExceptionGroup("outer", [group, ValueError("other")])
    if not raise_it:
        return group
    try:
        raise group
    except ExceptionGroup as caught:
        return caught


def written_for(exc: BaseException) -> str:
    log = Written("subfleet.daemon.test-group")
    try:
        log.log.error("failed", exc_info=(type(exc), exc, exc.__traceback__))
        return log.text()
    finally:
        log.close()


def values(text: str) -> set[str]:
    """The whitespace-free runs of `text` other than a group's margin."""
    return set(text.split()) - {"|", "+"}


@pytest.mark.parametrize("raise_it", [True, False], ids=["raised", "constructed"])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("shape", NEXT_LINE_SHAPES)
def test_an_exception_group_hides_what_its_member_alone_hides(shape, depth, raise_it):
    """The reviewer's input: a member's value on the line after its key was written
    raw inside a group (`    [REDACTED] SECRET7kv9`) though the same exception alone
    was scrubbed. The group's lines keep their margins, unless scrubbing joined two
    of them (`Bearer` and its token), when the group is written without them."""
    member = raised(shape + "SECRET7kv9")
    assert "SECRET7kv9" not in written_for(member)
    out = written_for(group_of([member, ValueError("other")], depth=depth, raise_it=raise_it))
    assert "SECRET7kv9" not in out, out
    assert "ExceptionGroup: several failed (2 sub-exceptions)" in out
    if "Bearer" not in shape:
        assert ("\n" + "  " * (depth + 1) + "| RuntimeError: ") in out, out


#: A member's text: a next-line shape or none, a secret, then the credential shapes above.
MEMBERS = st.tuples(st.sampled_from(NEXT_LINE_SHAPES + [""]), SECRETS, TEXTS)


def member_of(drawn) -> tuple[BaseException, list[str]]:
    shape, secret, words = drawn
    text, more = _text(words)
    return raised(f"{shape}{secret} {text}"), [secret, *more]


def without_margins(text: str) -> str:
    return "\n".join(line[len(match.group()):] if (match := MARGIN.match(line)) else line
                     for line in text.split("\n"))


@settings(max_examples=60, derandomize=True, database=None, deadline=None)
@given(drawn=MEMBERS, depth=st.integers(1, 2), raise_it=st.booleans())
def test_a_one_member_group_never_shows_a_secret_its_member_alone_hides(drawn, depth, raise_it):
    """Property: whatever a member's plain traceback hides, its group's hides too, for
    the next-line shapes and the credential shapes the property above builds, nested
    one or two deep, raised or only constructed."""
    exc, secrets = member_of(drawn)
    alone = written_for(exc)
    out = written_for(group_of([exc], depth=depth, raise_it=raise_it))
    for secret in secrets:
        if secret not in alone:
            assert secret not in out, (secret, out)


#: A group's own text over the bound (review r2 of #233, P3): its lines fill the kept head.
GROUP_CONTEXT = "\n".join(["group context line"] * 1200)


def distinct_member(drawn, index: int, pad: int = 0) -> tuple[BaseException, list[str]]:
    """`member_of`, with every secret made distinct: `SECRET7<member><n>…`, a fixed-width
    tag after `SECRET7`, so no secret is another's or a piece of one. `pad` lines of
    the member's own words come first, so its separator falls out of a cut group."""
    shape, secret, words = drawn
    text, _ = _text(words)
    count = iter(range(100))
    body = SECRET_RUN.sub(lambda run: f"SECRET7{index}{next(count):02d}{run.group()[7:]}", f"{shape}{secret} {text}")
    return raised("member line\n" * pad + body), SECRET_RUN.findall(body)


@settings(max_examples=60, derandomize=True, database=None, deadline=None)
@given(drawn=st.lists(MEMBERS, min_size=2, max_size=3), depth=st.integers(1, 2), raise_it=st.booleans(),
       over=st.booleans())
def test_a_several_member_group_never_shows_a_secret_its_member_alone_hides(drawn, depth, raise_it, over):
    """Property, restored for several members (review r2 of #233): whatever a member's
    plain traceback hides, its group's hides too. Round 2 had weakened it to the
    margins alone; its only failures were duplicate secrets, which the suffix strategy
    shrinks to, so another member showed that same string bare. With every secret
    distinct, it holds. `over`: the group's own lines and its last member's are long
    enough that the record is cut and its separators fall out of what is kept. The
    margins still never cost a secret the margin-free text or the line form hides."""
    members = [distinct_member(one, index, pad=1000 if over and index == len(drawn) - 1 else 0)
               for index, one in enumerate(drawn)]
    group = group_of([exc for exc, _ in members], depth=depth, raise_it=raise_it,
                     message=GROUP_CONTEXT if over else "several failed")
    out = written_for(group)
    for exc, secrets in members:
        alone = written_for(exc)
        for secret in secrets:
            if secret not in alone:
                assert secret not in out, (secret, out[-3000:])
    if not over:
        tb = traceback_of(group)
        block = daemon_log.scrubbed_block(tb)
        for _, secrets in members:
            for secret in secrets:
                if secret not in daemon_log.scrubbed_block(without_margins(tb)):
                    assert secret not in block, (secret, block)
                if secret not in refusals.scrubbed_line(tb, UNCUT):
                    assert secret not in block, (secret, block)


@BOUNDED
@given(words=st.lists(st.text(max_size=80), min_size=1, max_size=3), depth=st.integers(1, 2))
def test_a_group_s_block_never_shows_a_value_its_line_form_hides(words, depth):
    """The forms' invariant, for a group's traceback: folded, a block equals the uncut
    line form for any text without a group's margins; a group's block may hide more,
    never less."""
    tb = traceback_of(group_of([raised(text) for text in words], depth=depth))
    assert values(daemon_log.scrubbed_block(tb)) - {"[REDACTED]"} <= values(refusals.scrubbed_line(tb, UNCUT))


# --- a bound on what one record scrubs (review r1 of #233, P3) ----------------------

FOUR_MIB = 4 * 1024 * 1024
#: A generous guard on the clock for one 4 MiB record (D-TM1), the best of three runs: 1 s,
#: as the cost shapes' guard. The bound itself is counted, not timed
#: (`test_a_cut_s_work_does_not_grow_with_the_text`, and the regex and scan counts of
#: `test_daemon_log_cut_windows`): at 0.1 s this failed once at load 18 to 21, 0.1297 s
#: as the best of three (review r3 of #233, P3-2), and passed on rerun.
TIME_BOUND_S = 1.0
FRAME = '  File "/x/subfleet/module.py", line 12, in work\n    do_the_work(item, attempt)\n'


def four_mib(head="token=SECRET7head9\n", middle="password='SECRET7middle9'\n",
             tail="api_key = SECRET7tail9\n") -> str:
    """About 4 MiB of lines, with a secret at the start, in the middle and at the end."""
    half = "".join([FRAME] * ((FOUR_MIB // 2) // len(FRAME)))
    return head + half + middle + half + tail


class Scrubbed:
    """The longest text the scrubber is given while it is installed."""

    def __init__(self, monkeypatch):
        from subfleet.sessions import handoff
        self.longest = 0
        original = handoff._scrub

        def spy(text, *args, **kwargs):
            self.longest = max(self.longest, len(text))
            return original(text, *args, **kwargs)
        monkeypatch.setattr(handoff, "_scrub", spy)


def best_of_three(work) -> float:
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize("where", ["message", "traceback", "both"])
def test_a_four_mib_record_is_written_bounded_and_scrubbed_within_the_time_bound(where, monkeypatch):
    """1 MiB held the handler's lock for 1 to 1.5 s, 4 MiB for 4 to 6 s. A record's text
    is cut to `RECORD_SCRUB_MAX` characters before it is scrubbed, keeping its head and
    its tail at line breaks no match crosses, with a marker."""
    text = four_mib()
    exc = raised(text) if where != "message" else None
    scrubbed = Scrubbed(monkeypatch)
    outs = []

    def write():
        log = Written("subfleet.daemon.test-bound")
        try:
            message = text if where != "traceback" else "failed"
            log.log.error("%s", message, exc_info=(type(exc), exc, exc.__traceback__) if exc else None)
            outs.append(log.text())
        finally:
            log.close()
    elapsed = best_of_three(write)
    out = outs[-1]
    assert "SECRET7" not in out
    assert "characters omitted] …" in out
    assert out.startswith("token=[REDACTED]\n" if where != "traceback" else "failed\nTraceback")
    assert "api_key = [REDACTED]" in out
    assert scrubbed.longest <= daemon_log.RECORD_SCRUB_MAX + 100
    assert len(out) <= 2 * (daemon_log.RECORD_SCRUB_MAX + 100)
    assert elapsed < TIME_BOUND_S, elapsed


def test_a_four_mib_line_with_nowhere_safe_to_cut_keeps_only_its_marker(written):
    """One line has no line break to cut at: the marker says what was left out, and
    names the record's level and logger, fixed metadata, never its words (review r2
    of #233: the marker alone lost which logger had written it)."""
    written.log.warning("%s", "driver said: token=SECRET7oneline9 " + "x" * FOUR_MIB)
    assert written.text() == (f"… [{FOUR_MIB + 35:,} characters omitted] … "
                              "(WARNING record from subfleet.daemon.test-handler)\n")


def test_a_short_line_waits_for_a_four_mib_record_no_longer_than_the_bound(monkeypatch):
    """A short line logged while another thread's 4 MiB record held the lock waited 1.2 s
    behind 1 MiB; the bound on what one record scrubs bounds that wait too."""
    log = Written("subfleet.daemon.test-wait")
    inside = threading.Event()
    original = daemon_log.scrubbed_block

    def spy(text, **kwargs):
        if len(text) > 1000:
            inside.set()                    # the big record is being scrubbed: the lock is held
        return original(text, **kwargs)
    monkeypatch.setattr(daemon_log, "scrubbed_block", spy)
    text = four_mib()
    waits = []
    try:
        for _ in range(3):
            inside.clear()
            big = threading.Thread(target=log.log.warning, args=("%s", text))
            big.start()
            assert inside.wait(30)
            started = time.perf_counter()
            log.log.warning("short line")
            waits.append(time.perf_counter() - started)
            big.join(30)
        assert "SECRET7" not in log.text()
    finally:
        log.close()
    assert min(waits) < TIME_BOUND_S, waits


def shapes_with(secret: str) -> list[str]:
    """One or more lines that each hold `secret` the scrubber takes whole: credential
    shapes on a line, a key whose value is on the next line, a quoted value or a
    private key or a data URI spanning lines, a header and `Bearer` split, a reminder;
    and what only folding joins (review r2 of #233): a credential flag with its value
    on the next line, and a private key whose first and last lines are split."""
    return [
        f"token={secret}\n", f"token:\n{secret}\n", f"token =\n  {secret}\n",
        f"password='open\nmore {secret}\nclose'\n", f'\\"password\\": \\"a\n{secret}\n\\"\n',
        f"-----BEGIN PRIVATE KEY-----\nMIIabc\n{secret}\n-----END PRIVATE KEY-----\n",
        f"data:image/png;base64,QUJDREVGR0hJSktMTU5PUFFSU1RVVldY\n{secret}\nQUJD\n",
        f"Cookie\n:\t\"{secret}\"\n", f"Authorization: Bearer\n{secret}\n",
        f"<system-reminder>\n{secret}\n</system-reminder>\n", f"api_key = \"{secret}\"\n",
        f"deploy --password\n{secret} rest\n", f"run --db-token \r\n\n  {secret}\n",
        f"-----BEGIN RSA\nPRIVATE KEY-----\nMIIabc\n{secret}\n-----END RSA\nPRIVATE KEY-----\n",
    ]


def lines_with(secret: str) -> st.SearchStrategy:
    return st.sampled_from(shapes_with(secret))


#: Lines with nothing to scrub, some with quotes and separators that a cut must read.
PLAIN_LINES = st.sampled_from([
    "  File \"/x/a.py\", line 3, in f\n", "    raise RuntimeError(text)\n", "it's here\n", "\"\n", "'\n",
    "=\n", "x:\n", "plain words only\n", "\n", "a = 'one'\n", "b: \"two\"\n",
])
RECORD_LINES = st.lists(st.one_of(PLAIN_LINES, SECRETS.flatmap(lines_with)), min_size=1, max_size=40)
SECRET_RUN = re.compile(r"SECRET7[a-z0-9]*")


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(lines=RECORD_LINES, limit=st.integers(40, 400))
def test_a_record_s_cut_never_shows_a_secret_its_whole_text_hides(lines, limit):
    """Differential: in either form, the cut text shows no secret, nor a piece of one,
    that the whole text's scrub hides. The cut keeps at most `limit` characters and the
    marker, or only the marker when the text holds a reminder."""
    from subfleet.sessions import handoff
    text = "".join(lines)
    cut = handoff.cut_whole(text, limit)
    if len(text) <= limit:
        assert cut == text
    else:
        assert "characters omitted] …" in cut
        assert len(cut) <= limit + len(f"… [{len(text):,} characters omitted] …\n")
    for form in (daemon_log.scrubbed_block, lambda t: refusals.scrubbed_line(t, UNCUT)):
        assert set(SECRET_RUN.findall(form(cut))) <= set(SECRET_RUN.findall(form(text))), (text, cut)


@pytest.mark.parametrize("spanning", [
    "password='opened here\n{fill}SECRET7quoted9\n'\n",
    "\\'password\\': \\'opened\n{fill}SECRET7escaped9\n\\'\n",
    "-----BEGIN PRIVATE KEY-----\n{fill}SECRET7pem9\n-----END PRIVATE KEY-----\n",
    "data:image/png;base64,QUJDREVGR0hJSktMTU5PUFFSU1RVVldY\n{fill}SECRET7data9\nQUJD\n",
])
@pytest.mark.parametrize("at", ["head", "tail"])
def test_a_cut_never_falls_inside_a_match_that_spans_lines(spanning, at):
    """What the whole-text scrub takes across lines (a quoted value, a private key, a
    data URI) is never cut in two: scrub_bounded's neutral breaks are not enough there."""
    from subfleet.sessions import handoff
    fill = "QUJD\n" * 30
    body = spanning.format(fill=fill)
    filler = "plain words only\n" * 400
    text = body + filler if at == "head" else filler + body
    whole = daemon_log.scrubbed_block(text)
    assert "SECRET7" not in whole
    cut = handoff.cut_whole(text, 400)
    assert "characters omitted] …" in cut
    assert "SECRET7" not in daemon_log.scrubbed_block(cut)
    assert "SECRET7" not in refusals.scrubbed_line(cut, UNCUT)


def test_a_record_over_the_bound_holding_a_reminder_keeps_only_its_marker():
    """Stripping a reminder can join what was on either side of it; a record over the
    bound that holds one keeps only the marker."""
    from subfleet.sessions import handoff
    text = "plain\n" * 100 + "<system-reminder>\nSECRET7reminder9\n</system-reminder>\n" + "plain\n" * 100
    assert handoff.cut_whole(text, 200) == f"… [{len(text):,} characters omitted] …\n"


# --- the hooks in a test process (review r1 of #233, P3) --------------------------

INNER_TESTS = '''
import gc
import threading

from subfleet import daemon_log


def leak_thread():
    def work():
        raise RuntimeError("a thread failed")
    thread = threading.Thread(target=work, name="leak")
    thread.start()
    thread.join()


def leak_unraisable():
    class Finalized:
        def __del__(self):
            raise RuntimeError("a finalizer failed")
    Finalized()
    gc.collect()


def test_1_control():
    leak_thread()


def test_2_installs_the_daemon_s_hooks():
    daemon_log.install_process_hooks()          # as daemon.main does in-process


def test_3_a_thread_fails_after_them():
    leak_thread()


def test_4_a_finalizer_fails_after_them():
    leak_unraisable()
'''


def test_a_test_that_runs_main_leaves_pytest_its_own_hooks(tmp_path):
    """`daemon.main` run in-process (`test_daemon_accept`, `test_stop_watchdog`)
    installed the daemon's hooks for the rest of the session, so pytest stopped
    reporting a thread's exception or an unraisable one. The suite's conftest puts
    each test's hooks back: a failure after them is reported as before them. The
    sequence runs in its own pytest, with the suite's conftest as a plugin and both
    warnings made errors, and reads which tests failed."""
    import subprocess
    import xml.etree.ElementTree as ElementTree
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "test_inner_hooks.py").write_text(INNER_TESTS)
    (tmp_path / "none.ini").write_text("[pytest]\n")
    report = tmp_path / "inner.xml"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
         "-W", "error::pytest.PytestUnhandledThreadExceptionWarning",
         "-W", "error::pytest.PytestUnraisableExceptionWarning",
         "--rootdir", str(tmp_path), "-c", str(tmp_path / "none.ini"), f"--junitxml={report}",
         str(tmp_path / "test_inner_hooks.py")],
        cwd=root, capture_output=True, text=True, timeout=300)
    cases = {case.get("name"): case for case in ElementTree.parse(report).getroot().iter("testcase")}
    reported = {name for name, case in cases.items()
                if case.find("failure") is not None or case.find("error") is not None}
    assert set(cases) == {"test_1_control", "test_2_installs_the_daemon_s_hooks",
                          "test_3_a_thread_fails_after_them", "test_4_a_finalizer_fails_after_them"}, done.stdout
    assert reported == {"test_1_control", "test_3_a_thread_fails_after_them",
                        "test_4_a_finalizer_fails_after_them"}, done.stdout


def test_a_record_is_scrubbed_before_the_handler_s_lock_is_taken(monkeypatch):
    """Review r1 of #233: scrubbing, and Python's own formatting of a long
    traceback, ran under the handler's lock, so every other thread's line waited
    for them. The handler formats a record first; only its write holds the lock."""
    log = Written("subfleet.daemon.test-lock")
    free = []
    original = daemon_log.scrubbed_block

    def spy(text, **kwargs):
        def take():                             # another thread: free unless the record holds it
            if log.handler.lock.acquire(timeout=5):
                free.append(True)
                log.handler.lock.release()
            else:
                free.append(False)
        other = threading.Thread(target=take)
        other.start()
        other.join(30)
        return original(text, **kwargs)
    monkeypatch.setattr(daemon_log, "scrubbed_block", spy)
    try:
        log.log.warning("%s", "several lines\ntoken=SECRET7lock9")
    finally:
        log.close()
    assert free == [True]
    assert log.text() == "several lines\ntoken=[REDACTED]\n"


@pytest.mark.parametrize("before", [1_000, 40_000, FOUR_MIB])
def test_a_long_run_of_plain_words_still_keeps_its_tail(before):
    """Letters, digits, spaces and line breaks are all a data URI's body characters,
    so the run of them that reaches the tail's first line is a body only if
    `;base64,` opens it; the cut finds where it began, however far back."""
    from subfleet.sessions import handoff
    words = "plain words only\n" * (before // 17)
    cut = handoff.cut_whole("token=SECRET7first9\n" + words + "the last line\n", 400)
    assert cut.endswith("plain words only\nthe last line\n"), cut[-200:]
    body = "data:image/png;base64,\n" + words + "the last line\n"
    assert handoff.cut_whole(body, 400).endswith("characters omitted] …\n")


# --- the cut's cost (review r2 of #233, P2) -----------------------------------------

#: C-25.7: past this many distinct escaped quote kinds in its window, a side of a
#: record's cut keeps only the marker.
KINDS_MAX = 8
LIMIT = daemon_log.RECORD_SCRUB_MAX


def head_shape(size: int, quote: str = '"') -> str:
    """Review r2's head input: 122 kinds of escaped quote, each after `=`, in the first
    8 KiB, then plain words. Each kind open at a candidate cut cost a scan of the rest."""
    return "".join("k=" + "\\" * n + quote + "\n" for n in range(1, 123)) + "plain words only\n" * (size // 17)


def tail_shape(size: int, kinds: int = 120) -> str:
    """Review r2's tail input: plain words, a value opened (`k='`) just before the tail,
    escaped quotes of `kinds` kinds that open nothing filling the tail, and the closing
    quote at the end. `setdefault`'s default ran `_event_before` again at every line
    start; with eight kinds, under the cap, every line start is still refused."""
    lines, length, n = [], 0, 0
    while length < LIMIT // 2 + 256:
        n = n % kinds + 1
        lines.append("x" + "\\" * n + '"\n')
        length += len(lines[-1])
    return "plain words only\n" * (size // 17) + "k='\n" + "".join(lines) + "'\n"


#: The cost shapes: the reviewer's two, text dense with quotes, and long runs of
#: whitespace, which a cut that read each line start's neighbours anew walked again
#: and again (found in this round: blank lines after a key took minutes).
COST_SHAPES = {
    "head": head_shape,
    "head-single-quotes": lambda size: head_shape(size, "'"),
    "tail": tail_shape,
    "tail-eight-kinds": lambda size: tail_shape(size, KINDS_MAX),
    "quote-dense": lambda size: "'\"\n" * (size // 3),
    "json-dense": lambda size: 'k=\\"v\\", "t": \\\\\\"w\\\\\\"\n' * (size // 23),
    "blank-lines-after-a-key": lambda size: "token\n" + "\n" * size + ":\nSECRET7blank9\n",
    "blank-lines-after-bearer": lambda size: "Authorization: Bearer\n" + " \t\n" * (size // 3) + "SECRET7bearerblank9\n",
    "blank-lines-after-a-flag": lambda size: "deploy --password" + " \n" * (size // 2) + "SECRET7flagblank9 rest\n",
}


class Work:
    """What one call does, counted, not timed: the lines of `handoff` it runs, the C
    functions they call (each scan of the text is one such call), and its event
    computations (`_event_from`, `_event_before`). Past `budget` lines it stops the
    call: at review r2's head, a cut of blank lines after a key ran for minutes."""

    def __init__(self, budget: int = 3_000_000):
        self.lines = self.c_calls = self.events = 0
        self.budget = budget

    def run(self, call):
        from subfleet.sessions import handoff
        source = handoff.__file__
        computing = {handoff._event_from.__code__, handoff._event_before.__code__}

        def local(frame, event, arg):
            if event == "line":
                self.lines += 1
                if self.lines > self.budget:
                    raise OverflowError(f"over {self.budget:,} lines")
            return local

        def trace(frame, event, arg):
            if frame.f_code.co_filename != source:
                return None
            if event == "call" and frame.f_code in computing:
                self.events += 1
            return local

        def profile(frame, event, arg):
            if event == "c_call" and frame.f_code.co_filename == source:
                self.c_calls += 1

        collecting = gc.isenabled()
        gc.disable()                        # a collection's callbacks are not the cut's work
        sys.setprofile(profile)
        sys.settrace(trace)
        try:
            return call()
        finally:
            sys.settrace(None)
            sys.setprofile(None)
            if collecting:
                gc.enable()


@pytest.mark.timeout(600)
@pytest.mark.parametrize("shape", sorted(COST_SHAPES))
def test_a_cut_s_work_does_not_grow_with_the_text(shape):
    """Review r2 of #233, P2: one cut took 3.6 to 5.5 s at the head input and 4.5 to 4.8 s
    at the tail input. Counted, not timed: the lines it runs, its C calls (every scan)
    and its event computations are the same for 64 KiB and for 512 KiB of the shape;
    the work is the windows' and a fixed number of scans, whatever the length. Each
    side computes each quote kind's event at most once, of at most eight escaped kinds
    and the two plain ones."""
    from subfleet.sessions import handoff
    counts = []
    for size in (64 * 1024, 512 * 1024):
        text = COST_SHAPES[shape](size)
        work = Work()
        cut = work.run(lambda: handoff.cut_whole(text, LIMIT))
        counts.append((work.lines, work.c_calls, work.events))
        assert "SECRET7" not in daemon_log.scrubbed_block(cut)
    assert counts[0] == counts[1], counts
    lines, c_calls, events = counts[0]
    assert events <= 2 * (KINDS_MAX + 2), counts
    assert lines <= 40 * LIMIT and c_calls <= 20 * LIMIT, counts


@pytest.mark.timeout(30)
@pytest.mark.parametrize("where", ["message", "traceback"])
@pytest.mark.parametrize("shape", sorted(COST_SHAPES))
def test_a_four_mib_record_of_a_cost_shape_is_written_within_a_second(shape, where):
    """A generous guard on the clock, not a tight one (D-TM1): a 4 MiB record of each
    cost shape, as a message or a traceback, is written in under a second of CPU beyond
    Python's own formatting of its traceback, the better of two runs. Review r2
    measured 6.7 to 10.7 s for the head input. As wall time with Python's formatting in
    it, the blank lines after a key took 1.004 s at load about 55 (round 5): Python
    formats four million lines there; the counting tests carry the bound."""
    out, beyond = handler_cpu_beyond_formatting(COST_SHAPES[shape](FOUR_MIB), where)
    assert "SECRET7" not in out
    assert beyond < 1.0, beyond


def handler_cpu_beyond_formatting(text: str, where: str) -> tuple[str, float]:
    """What the handler writes of a record of `text` (its message, or the traceback of an
    exception raised with it), and the CPU it takes beyond Python's own formatting of
    that traceback (`formatException`, which the handler runs too): each the better of
    two runs, in CPU time, which a loaded host's other work does not add to."""
    exc = raised(text) if where == "traceback" else None
    outs = []

    def write():
        log = Written("subfleet.daemon.test-cost")
        try:
            log.log.error("%s", "failed" if exc else text, exc_info=(type(exc), exc, exc.__traceback__) if exc else None)
            outs.append(log.text())
        finally:
            log.close()
    formatter = daemon_log.ScrubbingFormatter()
    handler = min(best_of_one(write, time.process_time) for _ in range(2))
    formatting = min(best_of_one(lambda: formatter.formatException((type(exc), exc, exc.__traceback__)),
                                 time.process_time) for _ in range(2)) if exc else 0.0
    return outs[-1], handler - formatting


def best_of_one(work, clock=time.perf_counter) -> float:
    started = clock()
    work()
    return clock() - started


def test_a_side_with_more_than_eight_escaped_kinds_keeps_only_the_marker():
    """C-25.7's cap: eight distinct escaped quote kinds in a side's window are read;
    a ninth makes that side keep only the marker. Each kind open at a cut costs a look
    beyond the window, so the cap is what bounds the cut's work."""
    from subfleet.sessions import handoff
    words = "plain words only\n" * 4000

    def escaped(kinds: int) -> str:
        return "".join("k=" + "\\" * n + '"\n' for n in range(1, kinds + 1))
    for kinds, kept in ((KINDS_MAX, True), (KINDS_MAX + 1, False)):
        head = handoff.cut_whole(escaped(kinds) + words, 400)
        assert head.startswith("k=\\\"\n") is kept, head[:80]
        tail = handoff.cut_whole(words + "x\n" * 40 + escaped(kinds).replace("k=", "x"), 400)
        assert tail.endswith("…\n") is not kept, tail[-80:]


# --- what folding joins (review r2 of #233, P3) -------------------------------------

#: Review r2's daemon.log input: the tail's first line is the value of the flag that
#: ends the line before it. Both forms fold the text and scrub it again, and folded,
#: `--password\nSECRET` is `--password SECRET`, which the flags rule masks.
FLAG_TEXT = "w\n" * 20000 + "deploy --password\n" + "SECRET7flag9 rest\n" + "w\n" * 4087


def test_a_credential_flag_whose_value_starts_the_kept_tail_stays_hidden(written):
    """It wrote `… [31,826 characters omitted] …\\nSECRET7flag9 rest`: the uncut text
    hides the value in both forms, the cut one showed it."""
    assert FLAG_TEXT.index("SECRET7flag9") == len(FLAG_TEXT) - LIMIT // 2   # the tail's first line
    assert "SECRET7flag9" not in daemon_log.scrubbed_block(FLAG_TEXT)
    written.log.error("%s", FLAG_TEXT)
    assert "characters omitted] …" in written.text()
    assert "SECRET7flag9" not in written.text(), written.text()[-300:]


#: For each rule the scrubber applies (its names in `handoff`; `quoted`, `plain` and
#: `flags` are `_scrub_named_values`' three passes, `inline` is `_scrub_inline_headers`,
#: #239): what it masks, split where a line break may fall. Folding puts a space there.
RULE_EXAMPLES = {
    "_SYSTEM_REMINDER_RE": ("note <system-reminder>", "SECRET7reminder9 </system-reminder>"),
    "_PEM_RE": ("-----BEGIN RSA", "PRIVATE KEY-----\nMIIabcSECRET7pem9\n-----END RSA\nPRIVATE KEY-----"),
    "_DATA_URI_RE": ("data:image/png;base64,QUJDREVG", "SECRET7data9QUJDREVGR0hJSktMTU5PUFFSU1RVVldY"),
    "_JWT_RE": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".SECRET7jwt9abcdefgh"),
    "_PREFIXED_TOKEN_RE": ("key sk-proj-", "SECRET7prefixed9abcdefgh"),
    "_BEARER_RE": ("Authorization: Bearer", "SECRET7bearer9abcdefgh"),
    "_LONG_BASE64_RE": ("A" * 100, "SECRET7base9" + "A" * 100),
    "_HEADER_RE": ("Cookie:", "SECRET7cookie9"),
    "_URL_PASSWORD_RE": ("https://user:", "SECRET7url9@host"),
    "quoted": ("password='open", "SECRET7quoted9'"),
    "plain": ("token =", "SECRET7plain9"),
    "flags": ("deploy --password", "SECRET7flag9 rest"),
    "inline": ("sent Cookie:", "SECRET7inline9 rest"),
}
#: Where only folding joins the two parts: the rest are joined across a line break by
#: the rule itself (`\\s` in it), or not at all.
FOLDING_RULES = {"_PEM_RE", "flags"}
#: More of `_HEADER_RE`'s shapes (review r3 of #233): #231's (C-23.14), a header whose
#: line ends in a key whose value is on the next line, which the named-value rules take
#: whole, folded or not; and a header opening the text, whose next line only the line
#: form's folding joins to it, which `cut_whole`'s leading-header rule answers.
HEADER_EXAMPLES = {"231": ("Cookie: a=SECRET7hdrpre9; session_token=", "SECRET7hdr231val9 tail")}
LEADING_HEADER_EXAMPLE = ("Authorization: Basic", "SECRET7leadhdr9 rest")
RULE_CASES = [pytest.param(rule, example, id=rule) for rule, example in sorted(RULE_EXAMPLES.items())] + [
    pytest.param("_HEADER_RE", example, id=f"_HEADER_RE-{name}") for name, example in HEADER_EXAMPLES.items()] + [
    pytest.param("_HEADER_RE", LEADING_HEADER_EXAMPLE, id="_HEADER_RE-leading")]
GAPS = ("\n", " \n", "\n\n  ", "\r\n", "\t\n ")
FORMS = (daemon_log.scrubbed_block, lambda text: refusals.scrubbed_line(text, UNCUT))
MARGIN_LINE = re.compile(r"(?m)^(?:  )+[|+](?: |$)")


def forms_for(text: str) -> tuple:
    """The forms the daemon may write `text` in, cut or not: the block form; and for a
    text without a group's margins the line form, which folded the block equals
    (invariant 3). A group's block also reads it without margins, and may hide more."""
    return FORMS if not MARGIN_LINE.search(text) else FORMS[:1]


def _rule_names() -> set[str]:
    from subfleet.sessions import handoff
    rules = {pattern for pattern, _ in (*handoff._BLOCK_RULES, *handoff._TOKEN_RULES)} | {handoff._SYSTEM_REMINDER_RE}
    return {name for name, value in vars(handoff).items()
            if name.startswith("_") and isinstance(value, re.Pattern) and value in rules} | {
        "quoted", "plain", "flags", "inline"}


def test_every_scrub_rule_has_an_example_and_only_the_folding_rules_fold():
    """The refused breaks are derived from the rules: each rule has an example, and the
    ones where only folding joins what a line break parts are exactly `FOLDING_RULES`,
    each with its break rule (`handoff._folds_to_value`, the fold-tolerant private-key
    lines). A new rule fails here until it has an example."""
    from subfleet.sessions.handoff import scrub_secrets
    assert set(RULE_EXAMPLES) == _rule_names()

    def folds(text: str, secret: str) -> bool:
        return (secret in scrub_secrets(text, strip_reminders=True, whole=True)[0]
                and secret not in refusals.scrubbed_line(text, UNCUT))
    folding = set()
    for rule, (before, after) in [*RULE_EXAMPLES.items(), *(("_HEADER_RE", one) for one in HEADER_EXAMPLES.values())]:
        if folds("plain words\n" + before + "\n" + after + "\nmore words\n", SECRET_RUN.search(after).group()):
            folding.add(rule)
    assert folding == FOLDING_RULES
    # Leading the text, and only there, a header is joined to its next line by folding.
    before, after = LEADING_HEADER_EXAMPLE
    secret, text = SECRET_RUN.search(after).group(), before + "\n" + after + "\nmore words\n"
    assert folds(text, secret) and not folds("plain words\n" + text, secret)


def _aligned(before: str, gap: str, piece: str, at: str, limit: int):
    """Texts holding `piece` (`before`, `gap`, then the rest) whose cut falls inside
    `gap`, at the head's end or the tail's start; or that open with it (`start`)."""
    filler = "plain words only\n"
    if at == "start":
        yield piece + filler * 60
        return
    for pad in range(0, 160):
        if at == "tail":
            text, offset = filler * 60 + piece + "x\n" * pad, len(filler) * 60
            starts = [i + 1 for i in range(len(text) - limit // 2 - 1, len(text)) if text[i] == "\n"]
            cut = starts[0] if starts else None
        else:
            text, offset = "x\n" * pad + piece + filler * 60, 2 * pad
            cut = text.rfind("\n", 0, limit // 2) + 1
        if cut is not None and offset + len(before) < cut <= offset + len(before) + len(gap):
            yield text


@pytest.mark.parametrize("rule,example", RULE_CASES)
def test_a_cut_never_parts_what_a_rule_or_folding_joins(rule, example):
    """Differential, per rule and example: with the cut right at the break inside its
    example, at the head's end, the tail's start or the text's start, after every kind
    of gap, neither form of the cut text shows a secret the whole text's form hides."""
    from subfleet.sessions import handoff
    before, after = example
    for gap in GAPS:
        piece = before + gap + after + "\n"
        for at in ("head", "tail", "start"):
            texts = list(_aligned(before, gap, piece, at, 400))
            assert texts, (rule, gap, at)
            for text in texts:
                cut = handoff.cut_whole(text, 400)
                for form in FORMS:
                    assert set(SECRET_RUN.findall(form(cut))) <= set(SECRET_RUN.findall(form(text))), (rule, gap, at, cut)


def test_the_private_key_lines_a_cut_reads_are_those_folding_lets_the_scrubber_read():
    """Differential: the cut's fold-tolerant private-key lines match a text exactly when
    `_PEM_RE`'s own lines match it folded, for spaces, line breaks, tabs and CRLF."""
    import itertools
    from subfleet.sessions import handoff
    labels = ["", "RSA", "ENCRYPTED", "EC", "OPENSSH", "PRIVATE", "A1", "rsa", "RSA-1"]
    gaps = ["\n", " ", "\t", "\r\n", " \n ", "  ", ""]
    for edge, exact, folded in (("BEGIN", handoff._PEM_START_RE, handoff._PEM_START_FOLDED_RE),
                                ("END", handoff._PEM_END_RE, handoff._PEM_END_FOLDED_RE)):
        for first, label, second, third, block in itertools.product(gaps, labels, gaps, gaps, ["", " BLOCK", "\nBLOCK"]):
            line = f"-----{edge}{first}{label}{second if label else ''}PRIVATE{third}KEY{block}-----"
            assert (folded.fullmatch(line) is not None) == (exact.fullmatch(fold(line)) is not None), line


def test_the_whitespace_list_is_every_character_str_isspace_holds():
    """`handoff._SPACES` is what `str.isspace`, `str.strip` and `\\s` take as whitespace,
    which the cut's C scans and the rules must agree on."""
    from subfleet.sessions import handoff
    spaces = {chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace()}
    assert set(handoff._SPACES) == spaces
    assert all(re.fullmatch(r"\s", char) for char in spaces)
    assert not re.search(r"\s", "".join(chr(code) for code in range(0x3001) if chr(code) not in spaces))


# --- exception groups over the bound (review r2 of #233, P3) -----------------------------

#: Review r2's shapes: a value on the line after its key, in the last member of a group
#: whose own lines fill the kept head, so the cut keeps no separator.
OVER_BOUND_SHAPES = ["token:\n", "token =\n  ", "Authorization: Bearer\n"]


def over_bound_group(shape: str, *, raise_it: bool = True) -> BaseException:
    member = raised("\n".join(["member line"] * 1000) + "\n" + shape + "SECRET7" + "grpcut9")
    return group_of([member], raise_it=raise_it, message=GROUP_CONTEXT)


@pytest.mark.parametrize("raise_it", [True, False], ids=["raised", "constructed"])
@pytest.mark.parametrize("shape", OVER_BOUND_SHAPES)
def test_a_group_longer_than_the_bound_keeps_its_group_handling(shape, raise_it):
    """It wrote `    | token:\\n    [REDACTED] SECRET7grpcut9`: the separator search ran
    on the cut text, which kept none. Whether a part is a group is decided on the uncut
    part, and any line with a group's margin is read as one."""
    group = over_bound_group(shape, raise_it=raise_it)
    assert len(traceback_of(group)) > LIMIT
    out = written_for(group)
    assert "characters omitted] …" in out
    assert "SECRET7grpcut9" not in out, out[-400:]
    if "Bearer" not in shape:               # scrubbing joins `Bearer` and its token: no margins then
        assert "    | member line" in out


@pytest.mark.parametrize("shape", OVER_BOUND_SHAPES)
def test_a_block_with_a_group_s_margins_is_read_as_a_group(shape):
    """Any line with a group's margin is group text, separator or not."""
    text = "    | member line\n" * 3 + "".join("    | " + line for line in (shape + "SECRET7margin9\n").splitlines(True))
    assert "SECRET7margin9" not in daemon_log.scrubbed_block(text)
    assert "SECRET7margin9" not in daemon_log.scrubbed_block(text, group=True)


# --- the reviewer's generator, rebuilt (review r2 of #233) ---------------------------------

#: The CI run's texts; the report's run sets `SUBFLEET_CUT_FUZZ=16000`.
FUZZ_TEXTS = int(os.environ.get("SUBFLEET_CUT_FUZZ", "300"))
FUZZ_FILLER = ["plain words only", "  File \"/x/a.py\", line 3, in f", "    raise RuntimeError(text)", "it's here",
               '"', "'", "=", "x:", "", "a = 'one'", 'b: "two"', "w", "--verbose", "-p", "  + more", "data"]


def fuzz_text(rng) -> tuple[str, int]:
    """One text and a cut limit, as review r2's generator built them: the cut shapes
    above (`lines_with`), credential flags with their values a line or more on, CRLF,
    a leading header, JSON with escaped and double-escaped quotes, a group's margins
    and separators, split private-key lines, all among plain lines."""
    def secret() -> str:
        return "SECRET7" + "".join(rng.choice("abcdefghij0123456789") for _ in range(10))

    def piece() -> str:
        value = secret()
        flag = rng.choice(["--password", "--db-token", "x--api-key", "--client-secret", "--password-file", "-p"])
        return rng.choice([
            f"deploy {flag}{rng.choice(['', ' ', ' \t'])}\n{rng.choice(['', '  ', chr(10)])}{value} rest\n",
            f'{{"password": "{value}", "note": "it\'s"}}\n',
            f'{{\\"token\\": \\"{value}\\", \\"n\\": 1}}\n',
            f'{{\\\\\\"api_key\\\\\\": \\\\\\"{value}\\\\\\"}}\n',
            f"-----BEGIN RSA\nPRIVATE KEY-----\n{value}\n-----END RSA\nPRIVATE KEY-----\n",
            rng.choice(shapes_with(value)),
        ])
    lines = []
    for _ in range(rng.randint(1, 40)):
        lines.append(piece() if rng.random() < 0.35 else rng.choice(FUZZ_FILLER) + "\n")
    margin = rng.choice(["", "", "  | ", "    | "])
    if margin:
        separator = rng.choice(["  +---------------- 1 ----------------\n", "+-+---------------- 2 ----------------\n"])
        lines = [separator] + ["".join(margin + line for line in chunk.splitlines(True)) for chunk in lines]
    if rng.random() < 0.15:
        lines.insert(0, rng.choice(["Authorization: Bearer abc\n", "Cookie: a=b\n", "Set-Cookie:\n"]))
    text = "".join(lines)
    if rng.random() < 0.3:
        text = text.replace("\n", "\r\n")
    return text, rng.randint(40, 400)


def test_the_rebuilt_generator_finds_review_r2_s_leaks_shapes():
    """The generator reaches the shapes review r2's found: a flag whose value is a line
    on, and a group's margins."""
    import random
    texts = [fuzz_text(random.Random(seed))[0] for seed in range(400)]
    assert any("--password\n" in text or "--password\r\n" in text for text in texts)
    assert any("    | " in text and "+----------------" in text for text in texts)
    assert any(text.startswith(("Authorization:", "Cookie:", "Set-Cookie:")) for text in texts)


@pytest.mark.parametrize("seed", range(3))
def test_the_rebuilt_generator_finds_no_leak(seed, monkeypatch):
    """Differential over the generator's texts: what the handler writes of a record cut
    at the text's limit, and either form of `cut_whole`'s text, shows no secret the
    uncut record or text hides. The CI run is a bounded slice; the report's is 16,000."""
    import random
    from subfleet.sessions import handoff
    rng = random.Random(1000 + seed)
    for _ in range(FUZZ_TEXTS // 3 + (seed < FUZZ_TEXTS % 3)):
        text, limit = fuzz_text(rng)
        cut = handoff.cut_whole(text, limit)
        for form in forms_for(text):
            assert set(SECRET_RUN.findall(form(cut))) <= set(SECRET_RUN.findall(form(text))), (text, limit, cut)
        written = {}
        for bound in (limit, 10 ** 9):
            monkeypatch.setattr(daemon_log, "RECORD_SCRUB_MAX", bound)
            log = Written("subfleet.daemon.test-fuzz")
            try:
                log.log.error("%s", text)
                written[bound] = log.text()
            finally:
                log.close()
        assert set(SECRET_RUN.findall(written[limit])) <= set(SECRET_RUN.findall(written[10 ** 9])), (text, limit)
