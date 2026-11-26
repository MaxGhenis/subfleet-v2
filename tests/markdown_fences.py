"""Backtick-fenced code blocks, read as CommonMark reads them (D-PRR1).

CommonMark 0.31, section 4.5, for the one kind of fence subfleet writes. An
opening fence is a line of up to three spaces, then three or more backticks,
then an info string that holds no backtick. The block ends at the first later
line of up to three spaces, then at least as many backticks as opened it, then
only spaces or tabs, or else at the end of the document. Lines end at LF, CR LF
or CR, as in the spec, and not at the other separators `str.splitlines` splits
on. Content keeps its line endings and loses up to the opening fence's
indentation. Containers (lists, block quotes) are out of scope: the recipe this
reads has none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LINE_ENDING = re.compile(r"(\r\n|\r|\n)")
_OPENING = re.compile(r"( {0,3})(`{3,})([^`]*)")
_CLOSING = re.compile(r" {0,3}(`{3,})[ \t]*")


@dataclass(frozen=True)
class FencedBlock:
    """One block: its fences, info string and content, and where it lies."""

    fence: str             # the opening fence's backtick run
    closing: str | None    # the closing fence's run, None when the document ends first
    info: str              # the info string, stripped
    content: str           # every line between the fences, with its line ending
    start: int             # offset of the opening fence line
    end: int               # offset just past the closing fence line and its ending


def fenced_blocks(text: str) -> list[FencedBlock]:
    """Every backtick-fenced block in `text`, in order."""
    pieces = _LINE_ENDING.split(text)
    lines = list(zip(pieces[0::2], [*pieces[1::2], ""]))
    offsets = [0]
    for line, ending in lines:
        offsets.append(offsets[-1] + len(line) + len(ending))
    blocks = []
    index = 0
    while index < len(lines):
        opening = _OPENING.fullmatch(lines[index][0])
        if opening is None:
            index += 1
            continue
        indent, fence, info = opening.groups()
        end = index + 1
        closing = None
        while end < len(lines):
            found = _CLOSING.fullmatch(lines[end][0])
            if found and len(found.group(1)) >= len(fence):
                closing = found.group(1)
                break
            end += 1
        content = "".join(_dedent(line, len(indent)) + ending
                          for line, ending in lines[index + 1:end])
        blocks.append(FencedBlock(fence=fence, closing=closing, info=info.strip(),
                                  content=content, start=offsets[index],
                                  end=offsets[min(end + 1, len(lines))]))
        index = end + 1
    return blocks


def shell_block(text: str) -> str:
    """The content of the one closed `sh` block in `text`; anything else fails."""
    blocks = fenced_blocks(text)
    assert [block.info for block in blocks] == ["sh"], [block.info for block in blocks]
    assert blocks[0].closing is not None, "the shell block is never closed"
    return blocks[0].content


def _dedent(line: str, width: int) -> str:
    stripped = line.lstrip(" ")
    return line[min(width, len(line) - len(stripped)):]
