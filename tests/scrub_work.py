"""The work one `scrub_secrets` call does, counted rather than timed (D-SCRUB-LINEAR).

The Bearer rule's linearity test (`test_the_bearer_rule_keeps_scrubbing_linear`, named
`test_the_bearer_lookahead_keeps_scrubbing_linear` before #239) timed the scrub at 2,048
and 8,192 characters and failed on shared CI runners at about 9x the CPU for 4x the
input, on code its PRs never touched (#238's run 39682771461, #265's run 40326659520).
The counts here are the same on every run of one interpreter:
- `reads`: what each regex call the scrub makes reads, counted by `tests.regex_reads`
  (each character test, at each position a scan tries, as #233's cut meter counts),
  each call's matches checked against `re`'s own. Every compiled pattern the scrub's
  modules hold is counted (`MODULES`: the head's `handoff`, and `scrub_release`,
  release/217's frozen pipeline, which every mode runs first since #239), the ones
  their rule tuples hold (`_TOKEN_RULES`, `_BLOCK_RULES`) included: one counting
  stand-in for each pattern, wherever a module keeps it, so `pattern is _HEADER_RE`
  and `pattern in (_PEM_RE, …)` still hold. A pattern both modules hold (`re.compile`'s
  cache hands them one object) has one stand-in, named after `handoff`: `over` and
  `widest` credit that name with both pipelines' reads (#266 review r4).
- `lines`: the lines of `subfleet` Python runs during the call (`sys.settrace`), so a
  loop of the scrubber's own counts too.
- `uncounted`: what `sys.setprofile` sees the reads miss. That is a regex call made
  from Python on a pattern no stand-in holds (one another module keeps, or one in a
  list or dict, or `re.sub` with a string), and each call the scrub makes to `re`'s
  API (a pattern compiled as it runs).

Neither count sees C-level `str` work (slices, `in`, `find`, `casefold`, `join`), nor
a regex call that C code makes on a pattern no stand-in holds (`map(p.search, …)`).
The profile sees a pattern's own calls only when Python makes them (#266 review r1,
P2-1); it names a call into `re`'s API however it is entered. The instructions the
CPU retires see both (`tests.cpu_scaling.fewest_instructions`), where the CPU's
counter is exposed (not on CI's virtual machines).

A scrub that reads more than its `budget` is stopped before `re` runs the call:
`OverBudget`, the call's pattern in `over`. A catastrophic pattern is thus never run
by `re`, which would hold the GIL for as long as it backtracks.

Every exit restores the modules' names and the interpreter's trace and profile. Run
long texts in a child (`_scrubber_probe`): the emulator nests a generator for each
repetition of a group, so the child raises the recursion limit. The differential's
short texts run in-process at the default limit.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from subfleet.sessions import handoff, scrub_release
from tests import regex_reads

#: The modules whose patterns a scrub runs: the head's, and release/217's frozen
#: pipeline, which runs first in every mode (#239 round 8).
MODULES = (handoff, scrub_release)
#: The package's folder as its code objects name it (`co_filename`, not resolved).
SUBFLEET = str(Path(handoff.__file__).parents[1]) + os.sep
#: The count's own files: a regex call made from them is the count's, not the scrub's.
COUNTING = {__file__, regex_reads.__file__}
#: `re`'s own Python code (`re.compile`, `re.sub`, …), whose API the scrub never calls.
RE = os.path.dirname(re.__file__) + os.sep


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
    `uncounted`: the regex calls from Python, and the calls to `re`'s API, that the
    reads did not see (none of the C-level work above)."""
    work = Work(reads_per_char * max(1, len(text)))
    found: dict[int, tuple[str, re.Pattern]] = {}
    for module in MODULES:
        for name, value in vars(module).items():
            _patterns(value, f"{module.__name__.rpartition('.')[2]}.{name}", found)
    stand_ins = {key: _Counted(name, pattern, work) for key, (name, pattern) in found.items()}
    saved = {module: dict(vars(module)) for module in MODULES}
    replaced = {module: {name: new for name, value in names.items() if (new := _counted(value, stand_ins)) is not value}
                for module, names in saved.items()}

    def lines(frame, event, arg):
        work.lines += event == "line"
        return lines

    def calls(frame, event, arg):
        return lines if frame.f_code.co_filename.startswith(SUBFLEET) else None

    def regex_calls(frame, event, arg):
        if event == "call":                 # a pattern the scrub compiles, called from C or not
            # `re`'s API only: `Match.expand` enters the cached `re._compile_template`
            # from C, which matches nothing, and only before the cache holds the template.
            if (frame.f_code.co_filename.startswith(RE) and not frame.f_code.co_name.startswith("_")
                    and (caller := frame.f_back) is not None and caller.f_code.co_filename.startswith(SUBFLEET)):
                work.uncounted.add(f"re.{frame.f_code.co_name}")
        elif (event == "c_call" and isinstance(pattern := getattr(arg, "__self__", None), re.Pattern)
                and frame.f_code.co_filename not in COUNTING):
            work.uncounted.add(pattern.pattern[:60])

    previous = sys.gettrace(), sys.getprofile()
    for module, names in replaced.items():
        for name, value in names.items():
            setattr(module, name, value)
    sys.settrace(calls)
    sys.setprofile(regex_calls)
    try:
        scrubbed = handoff.scrub_secrets(text)
    except regex_reads.OverBudget:
        scrubbed = None
    finally:
        sys.setprofile(previous[1])
        sys.settrace(previous[0])
        for module, names in replaced.items():
            for name in names:
                setattr(module, name, saved[module][name])
    same = None if work.over else scrubbed == handoff.scrub_secrets(text)
    return {"chars": len(text), "reads": work.reads, "lines": work.lines, "over": work.over, "same": same,
            "uncounted": sorted(work.uncounted),
            "widest": sorted(work.by_pattern.items(), key=lambda item: -item[1])[:3]}
