"""What a regex call reads, counted: a backtracking matcher over `re`'s own parse of the
pattern that counts each character it tests (#233 review r5, P3-1).

Round 6's meter counted a regex call by its span, from `pos` to `endpos`. A pattern
whose failed attempts read far, restarted at each position, reads more than that:
`(\\\\*)(["'])` reads a run of backslashes no quote follows again from each of its
backslashes, 8,000 of them about 32 million times, and the meter counted the run's
8,000. This matcher tries the pattern where `re` does (each position of a scan, from
`pos`) and as `re` does (greedy repeats longest first, lazy ones shortest first,
possessive ones and atomic groups never given back, lookarounds, branches in order), and
counts a read for each character test: one for a literal, a class or `.`, one or two
for `^`, `$` or `\\b`, and a lookbehind's characters before the position. `re` makes
the same attempts; its optimizations (a literal prefix found by a fast scan, a repeat
that checks the next literal before it gives a character back) only read less. Each
character test is decided by `re` itself, a one-character pattern compiled from the
same node with the same flags, so case folding and categories are `re`'s.

`scan` gives the count and the spans of the matches, which the caller checks against
`re`'s: a pattern this matcher reads otherwise fails there, and a construct it does not
know raises.
"""
from __future__ import annotations

import re
import sys
from re import _compiler as sre_compile
from re import _constants as C
from re import _parser as sre_parse

#: The single-character nodes.
_CHAR_OPS = {C.LITERAL, C.NOT_LITERAL, C.IN, C.ANY}
#: A word character, as `re` reads one in a `str` pattern (`\b`).
_WORD = re.compile(r"\w")


class OverBudget(Exception):
    """More was read than the budget the caller allows: the count is past it."""


class _Count:
    def __init__(self, budget: int):
        self.reads, self.budget = 0, budget

    def add(self, count: int = 1) -> None:
        self.reads += count
        if self.reads > self.budget:
            raise OverBudget


def _char_test(node, flags: int):
    """`re`'s answer for one character against `node`, cached per character."""
    state = sre_parse.State()
    state.flags = flags
    compiled = sre_compile.compile(sre_parse.SubPattern(state, [node]), flags)
    answers: dict[str, bool] = {}

    def test(char: str) -> bool:
        answer = answers.get(char)
        if answer is None:
            answer = answers[char] = compiled.fullmatch(char) is not None
        return answer
    return test


class Emulated:
    """One compiled pattern, as this matcher reads it."""

    def __init__(self, pattern: re.Pattern):
        tree = sre_parse.parse(pattern.pattern, pattern.flags)
        self.pattern = pattern
        self.count = _Count(sys.maxsize)
        self.run = self._sequence(list(tree), tree.state.flags)

    # --- building ---------------------------------------------------------------------

    def _sequence(self, nodes, flags):
        parts = [self._node(node, flags) for node in nodes]

        def run(text, pos, end, index=0):
            if index == len(parts):
                yield pos
                return
            for at in parts[index](text, pos, end):
                yield from run(text, at, end, index + 1)
        return run

    def _node(self, node, flags):
        op, av = node
        owner = self
        if op in _CHAR_OPS:
            test = _char_test(node, flags)

            def char(text, pos, end):
                if pos < end:
                    owner.count.add()
                    if test(text[pos]):
                        yield pos + 1
            return char
        if op is C.AT:
            return self._at(av, flags)
        if op is C.SUBPATTERN:
            _group, add, remove, sub = av
            return self._sequence(list(sub), (flags | add) & ~remove)
        if op is C.ATOMIC_GROUP:
            inner = self._sequence(list(av), flags)

            def atomic(text, pos, end):
                for at in inner(text, pos, end):
                    yield at
                    return
            return atomic
        if op is C.BRANCH:
            alternatives = [self._sequence(list(alternative), flags) for alternative in av[1]]

            def branch(text, pos, end):
                for alternative in alternatives:
                    yield from alternative(text, pos, end)
            return branch
        if op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
            return self._repeat(op, av, flags)
        if op in (C.ASSERT, C.ASSERT_NOT):
            return self._assert(op, av, flags)
        raise NotImplementedError(f"regex_reads: {op} in {self.pattern.pattern!r}")

    def _at(self, where, flags):
        owner = self
        if flags & re.MULTILINE:                    # as `re`'s compiler reads `^` and `$`
            where = {C.AT_BEGINNING: C.AT_BEGINNING_LINE, C.AT_END: C.AT_END_LINE}.get(where, where)

        def at(text, pos, end):
            if where in (C.AT_BEGINNING, C.AT_BEGINNING_STRING):
                found = pos == 0
            elif where is C.AT_BEGINNING_LINE:
                if pos:
                    owner.count.add()
                found = pos == 0 or text[pos - 1] == "\n"
            elif where is C.AT_END:
                if pos < end:
                    owner.count.add()
                found = pos == end or (pos == end - 1 and text[pos] == "\n")
            elif where is C.AT_END_LINE:
                if pos < end:
                    owner.count.add()
                found = pos == end or text[pos] == "\n"
            elif where is C.AT_END_STRING:
                found = pos == end
            elif where in (C.AT_BOUNDARY, C.AT_NON_BOUNDARY, C.AT_UNI_BOUNDARY, C.AT_UNI_NON_BOUNDARY):
                owner.count.add((pos > 0) + (pos < end))
                before = pos > 0 and _WORD.match(text[pos - 1]) is not None
                after = pos < end and _WORD.match(text[pos]) is not None
                found = (before != after) is (where in (C.AT_BOUNDARY, C.AT_UNI_BOUNDARY))
            else:
                raise NotImplementedError(f"regex_reads: {where} in {self.pattern.pattern!r}")
            if found:
                yield pos
        return at

    def _repeat(self, op, av, flags):
        low, high, item = av
        owner = self
        nodes = list(item)
        if len(nodes) == 1 and nodes[0][0] in _CHAR_OPS:
            test = _char_test(nodes[0], flags)

            def longest(text, pos, end, limit):
                taken = 0
                while taken < limit and pos + taken < end:
                    owner.count.add()
                    if not test(text[pos + taken]):
                        break
                    taken += 1
                return taken

            if op is C.MIN_REPEAT:
                def lazy(text, pos, end):
                    taken = longest(text, pos, end, low)
                    if taken < low:
                        return
                    while True:
                        yield pos + taken
                        if taken >= high or longest(text, pos + taken, end, 1) == 0:
                            return
                        taken += 1
                return lazy

            def greedy(text, pos, end):
                taken = longest(text, pos, end, high)
                if taken < low:
                    return
                if op is C.POSSESSIVE_REPEAT:
                    yield pos + taken
                    return
                for at in range(pos + taken, pos + low - 1, -1):
                    yield at
            return greedy
        inner = self._sequence(nodes, flags)

        def more(text, pos, end, done):
            if done < high:
                for at in inner(text, pos, end):
                    if at == pos and done >= low:
                        continue                    # an empty iteration ends the repeat
                    yield from more(text, at, end, done + 1)
            if done >= low:
                yield pos

        def fewer(text, pos, end, done):
            if done >= low:
                yield pos
            if done < high:
                for at in inner(text, pos, end):
                    if at == pos and done >= low:
                        continue
                    yield from fewer(text, at, end, done + 1)

        if op is C.MIN_REPEAT:
            return lambda text, pos, end: fewer(text, pos, end, 0)
        if op is C.POSSESSIVE_REPEAT:
            def possessive(text, pos, end):
                for at in more(text, pos, end, 0):
                    yield at
                    return
            return possessive
        return lambda text, pos, end: more(text, pos, end, 0)

    def _assert(self, op, av, flags):
        direction, sub = av
        inner = self._sequence(list(sub), flags)
        width = sub.getwidth()
        if direction < 0 and width[0] != width[1]:
            raise NotImplementedError(f"regex_reads: a lookbehind of varying width in {self.pattern.pattern!r}")

        def check(text, pos, end):
            if direction > 0:
                found = next(inner(text, pos, end), None) is not None
            else:
                start = pos - width[0]
                found = start >= 0 and any(at == pos for at in inner(text, start, pos))
            if found is (op is C.ASSERT):
                yield pos
        return check

    # --- running ----------------------------------------------------------------------

    def _at_pos(self, text, pos, end, whole):
        for at in self.run(text, pos, end):
            if not whole or at == end:
                return at
        return None

    def scan(self, kind: str, text: str, pos: int = 0, endpos: int = sys.maxsize,
             budget: int = sys.maxsize) -> tuple[int, list[tuple[int, int]]]:
        """What `kind` (`search`, `match`, `fullmatch`, `finditer`, `sub`) reads of `text`
        from `pos` to `endpos`, and the spans of its matches. `OverBudget` past `budget`."""
        end = min(endpos, len(text))
        pos = max(0, pos)
        self.count = _Count(budget)
        spans = []
        if kind in ("match", "fullmatch"):
            at = self._at_pos(text, pos, end, kind == "fullmatch") if pos <= end else None
            if at is not None:
                spans.append((pos, at))
            return self.count.reads, spans
        start, empty_at = pos, -1
        while start <= end:
            at = self._at_pos(text, start, end, False)
            if at is None or (at == start and start == empty_at):
                start += 1
                continue
            spans.append((start, at))
            if kind == "search":
                break
            if at == start:
                empty_at = start
            start = at
        return self.count.reads, spans


_EMULATED: dict[tuple[str, int], Emulated] = {}


def emulated(pattern: re.Pattern) -> Emulated:
    key = (pattern.pattern, pattern.flags)
    if key not in _EMULATED:
        _EMULATED[key] = Emulated(pattern)
    return _EMULATED[key]


def real_spans(kind: str, pattern: re.Pattern, text: str, pos: int = 0, endpos: int = sys.maxsize):
    """The spans `re` gives for the same call."""
    if kind == "sub":
        return [match.span() for match in pattern.finditer(text)]
    if kind == "finditer":
        return [match.span() for match in pattern.finditer(text, pos, endpos)]
    found = getattr(pattern, kind)(text, pos, endpos)
    return [] if found is None else [found.span()]
