"""The work one `scrub_secrets` call does, counted rather than timed (D-SCRUB-LINEAR).

`test_the_bearer_lookahead_keeps_scrubbing_linear` timed the scrub at 2,048 and 8,192
characters and failed on shared CI runners at about 9x the CPU for 4x the input, on
code its PRs never touched (#238's run 39682771461, #265's run 40326659520). The counts
here are the same on every run of one interpreter:
- `reads`: what each regex call the scrub makes reads, counted by `tests.regex_reads`
  (each character test, at each position a scan tries, as #233's cut meter counts),
  each call's matches checked against `re`'s own. Every compiled pattern `handoff`
  holds is counted, the ones its rule tuples hold (`_TOKEN_RULES`, `_BLOCK_RULES`)
  included: one counting stand-in for each pattern, wherever the module keeps it, so
  `pattern is _HEADER_RE` and `pattern in (_PEM_RE, …)` still hold. A regex call the
  stand-ins do not see (a pattern kept elsewhere, `re.sub` with a string, another
  module's) is named in `uncounted` (`sys.setprofile`), so none is read uncounted.
- `lines`: the lines of `subfleet` Python runs during the call (`sys.settrace`), so a
  loop of the scrubber's own counts too.

A scrub that reads more than its `budget` is stopped before `re` runs the call:
`OverBudget`, the call's pattern in `over`. A catastrophic pattern is thus never run
by `re`, which would hold the GIL for as long as it backtracks.

Run it in a child (`_scrubber_probe`): it replaces `handoff`'s patterns while it runs,
traces the interpreter, and the emulator nests a generator for each repetition of a
group, so the child raises the recursion limit.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from subfleet.sessions import handoff
from tests import regex_reads

#: The package's folder as its code objects name it (`co_filename`, not resolved).
SUBFLEET = str(Path(handoff.__file__).parents[1]) + os.sep
#: The count's own files: a regex call made from them is the count's, not the scrub's.
COUNTING = {__file__, regex_reads.__file__}


class Work:
    """What one scrub read and ran, the pattern that went past the budget, and the
    regex calls it made past the count."""

    def __init__(self, budget: int):
        self.reads, self.lines, self.budget = 0, 0, budget
        self.by_pattern: dict[str, int] = {}
        self.over: str | None = None
        self.uncounted: set[str] = set()


class _Counted:
    """A compiled pattern whose calls count what they read; `re` runs each call only
    once the count is within the budget."""

    def __init__(self, name: str, pattern: re.Pattern, work: Work):
        self._name, self._pattern, self._work = name, pattern, work
        self._emulated = regex_reads.emulated(pattern)

    def _count(self, kind, string, pos=0, endpos=sys.maxsize):
        text = str(string)
        try:
            reads, spans = self._emulated.scan(kind, text, pos, endpos, budget=self._work.budget - self._work.reads)
        except regex_reads.OverBudget:
            self._work.over = self._name
            raise
        assert spans == regex_reads.real_spans(kind, self._pattern, text, pos, endpos), (self._name, kind)
        self._work.reads += reads
        self._work.by_pattern[self._name] = self._work.by_pattern.get(self._name, 0) + reads

    def search(self, string, pos=0, endpos=sys.maxsize):
        self._count("search", string, pos, endpos)
        return self._pattern.search(string, pos, endpos)

    def match(self, string, pos=0, endpos=sys.maxsize):
        self._count("match", string, pos, endpos)
        return self._pattern.match(string, pos, endpos)

    def fullmatch(self, string, pos=0, endpos=sys.maxsize):
        self._count("fullmatch", string, pos, endpos)
        return self._pattern.fullmatch(string, pos, endpos)

    def finditer(self, string, pos=0, endpos=sys.maxsize):
        self._count("finditer", string, pos, endpos)
        return self._pattern.finditer(string, pos, endpos)

    def findall(self, string, pos=0, endpos=sys.maxsize):
        self._count("finditer", string, pos, endpos)        # the same attempts as finditer
        return self._pattern.findall(string, pos, endpos)

    def sub(self, repl, string, count=0):
        assert count == 0, "a counted substitution is counted as a whole scan"
        self._count("sub", string)
        return self._pattern.sub(repl, string)

    def __getattr__(self, name):
        if name in ("split", "subn", "scanner"):
            raise AttributeError(f"{name} is not counted: count it before the scrub uses it")
        return getattr(self._pattern, name)


def _patterns(value, name: str, found: dict[int, tuple[str, re.Pattern]]) -> None:
    """Each pattern in `value`, at any depth of tuples, by the first name that reaches it."""
    if isinstance(value, re.Pattern):
        found.setdefault(id(value), (name, value))
    elif isinstance(value, tuple):
        for index, part in enumerate(value):
            _patterns(part, f"{name}[{index}]", found)


def _counted(value, stand_ins: dict[int, _Counted]):
    """`value` with each pattern in it, at any depth of tuples, its stand-in."""
    if isinstance(value, re.Pattern):
        return stand_ins[id(value)]
    if isinstance(value, tuple):
        parts = tuple(_counted(part, stand_ins) for part in value)
        return value if all(new is old for new, old in zip(parts, value)) else parts
    return value


def scrub_work(text: str, *, reads_per_char: int) -> dict:
    """`scrub_secrets(text)`'s regex reads and `subfleet` lines, within a budget of
    `reads_per_char` reads a character of `text` (`over` names the pattern past it).
    `same`: the counted scrub gave what the scrub gives uncounted (None when stopped);
    `uncounted`: the regex calls the count did not see."""
    work = Work(reads_per_char * max(1, len(text)))
    found: dict[int, tuple[str, re.Pattern]] = {}
    for name, value in vars(handoff).items():
        _patterns(value, name, found)
    stand_ins = {key: _Counted(name, pattern, work) for key, (name, pattern) in found.items()}
    saved = dict(vars(handoff))
    replaced = {name: new for name, value in saved.items() if (new := _counted(value, stand_ins)) is not value}

    def lines(frame, event, arg):
        work.lines += event == "line"
        return lines

    def calls(frame, event, arg):
        return lines if frame.f_code.co_filename.startswith(SUBFLEET) else None

    def regex_calls(frame, event, arg):
        if (event == "c_call" and isinstance(pattern := getattr(arg, "__self__", None), re.Pattern)
                and frame.f_code.co_filename not in COUNTING):
            work.uncounted.add(pattern.pattern[:60])

    previous = sys.gettrace(), sys.getprofile()
    for name, value in replaced.items():
        setattr(handoff, name, value)
    sys.settrace(calls)
    sys.setprofile(regex_calls)
    try:
        scrubbed = handoff.scrub_secrets(text)
    except regex_reads.OverBudget:
        scrubbed = None
    finally:
        sys.setprofile(previous[1])
        sys.settrace(previous[0])
        for name in replaced:
            setattr(handoff, name, saved[name])
    same = None if work.over else scrubbed == handoff.scrub_secrets(text)
    return {"chars": len(text), "reads": work.reads, "lines": work.lines, "over": work.over, "same": same,
            "uncounted": sorted(work.uncounted),
            "widest": sorted(work.by_pattern.items(), key=lambda item: -item[1])[:3]}
