"""Lay out `docs/acceptance-contract.md` one sentence per line (defect D-MRG3).

Git merges line by line, and changes to adjacent lines conflict. The contract kept each
clause on one line of up to 18,000 characters, so two pull requests that changed
different sentences of one clause, or the last words of one clause and the first of the
next, conflicted with no word changed by both. GitHub runs no CI on a conflicting pull
request (D-CI1), so each conflict cost a merge and a fresh CI run.

`relayout` starts a line at each sentence of every paragraph, and after each semicolon.
A list item's later lines are indented to its content, as continuation lines, so the
rendered document is unchanged: Markdown joins a paragraph's lines with soft breaks.
Headings, tables, fenced code and blank lines are left alone, and so is a paragraph with
a hard line break. Of the 185 merges from 2026-10-01 to 2026-10-14 in which both sides
changed the contract, 128 conflicted on it; with the history moved out (C-20.6) 71 would
have, and with this layout as well, 37 (`docs/reports/2026-10-14-d-mrg3-contract-replay.md`).

    python3 tools/contract_layout.py            # exit 1 and name the paragraphs to fix
    python3 tools/contract_layout.py --write    # rewrite the contract in place
    python3 tools/contract_layout.py --stdout   # print the laid-out contract

Standard library only, like the rest of the repository.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "docs" / "acceptance-contract.md"

FENCE = re.compile(r"^\s*(```|~~~)")
TABLE = re.compile(r"^\s*\|")
HEADING = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
#: A setext underline or a thematic break: a line of its own, never part of a paragraph.
RULE = re.compile(r"^ {0,3}(?:=+|-+|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})[ \t]*$")
ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+)")
#: What may not begin a continuation line: it would start a block of its own (a list
#: item, heading, quote, fence, table row, HTML block, or a setext underline) instead of
#: continuing the paragraph.
BLOCK_START = re.compile(r"^(?:[-*+]\s|\d{1,9}[.)]\s|#{1,6}(?:\s|$)|>|```|~~~|\||<|[=-]+\s*$)")
#: A sentence ends at `.`, `!` or `?`, optionally followed by a closing bracket or quote.
END = re.compile(r"[.!?][)\]\"'”’]?$")
#: A sentence may begin with a capital letter, a code span, a quote, a bracket or emphasis.
BEGIN = re.compile(r"[A-Z`\"'“‘(\[*_]")
#: A period after these words does not end a sentence.
ABBREVIATIONS = frozenset({
    "al", "approx", "ca", "cf", "dr", "e.g", "eq", "etc", "fig", "i.e", "incl", "mr", "ms",
    "no", "nos", "p", "pp", "resp", "sec", "st", "u.s", "viz", "vs",
})


def _code_spans(text: str) -> list[tuple[int, int]]:
    """CommonMark code spans: a run of N backticks to the next run of exactly N."""
    spans = []
    i = 0
    while i < len(text):
        if text[i] != "`":
            i += 1
            continue
        j = i
        while j < len(text) and text[j] == "`":
            j += 1
        run = j - i
        k = j
        close = None
        while k < len(text):
            if text[k] != "`":
                k += 1
                continue
            m = k
            while m < len(text) and text[m] == "`":
                m += 1
            if m - k == run:
                close = m
                break
            k = m
        if close is None:
            i = j          # an unmatched run is literal text
        else:
            spans.append((i, close))
            i = close
    return spans


def sentences(text: str) -> list[str]:
    """Split one paragraph's text (no line breaks) into sentences and semicolon clauses.

    A boundary is a run of spaces outside any code span, where the next part would not
    begin a Markdown block of its own, and either after a semicolon or after a sentence
    end (not an abbreviation) and before a sentence start. Joining the result with single
    spaces gives back `text` with each boundary's run of spaces collapsed to one.
    """
    spans = _code_spans(text)
    out, start = [], 0
    for match in re.finditer(r" +", text):
        a, b = match.span()
        if a == 0 or b == len(text):
            continue
        if any(s < a < e for s, e in spans):
            continue
        before, after = text[start:a], text[b:]
        if BLOCK_START.match(after):
            continue
        if not before.endswith(";"):
            if not END.search(before) or not BEGIN.match(after):
                continue
            word = before.rsplit(" ", 1)[-1].rstrip(".!?)]\"'”’").lstrip("(\"'`*_[")
            if word.lower() in ABBREVIATIONS:
                continue
        out.append(before)
        start = b
    out.append(text[start:])
    return out


def _paragraph_end(lines: list[str], start: int) -> int:
    """The line after the paragraph that starts at `start`."""
    end = start + 1
    while end < len(lines):
        nxt = lines[end]
        # A line that may start a block of its own (a list item, quote, HTML, a setext
        # underline) ends the paragraph: relayout never joins it to the line before.
        if (not nxt.strip() or FENCE.match(nxt) or TABLE.match(nxt) or HEADING.match(nxt)
                or RULE.match(nxt) or ITEM.match(nxt) or BLOCK_START.match(nxt.lstrip())):
            break
        end += 1
    return end


def _paragraphs(lines: list[str]):
    """Yield (start, end) line ranges of the paragraphs relayout may rewrite."""
    i, fenced = 0, False
    while i < len(lines):
        line = lines[i]
        if FENCE.match(line):
            fenced = not fenced
            i += 1
            continue
        if (fenced or not line.strip() or TABLE.match(line) or HEADING.match(line)
                or RULE.match(line)):
            i += 1
            continue
        end = _paragraph_end(lines, i)
        yield i, end
        i = end


def _prefix(line: str) -> tuple[str, str]:
    """A paragraph's first-line prefix and the indent of its continuation lines."""
    item = ITEM.match(line)
    if item:
        return item.group(0), " " * len(item.group(0))
    indent = line[:len(line) - len(line.lstrip(" "))]
    return indent, indent


def relayout_paragraph(lines: list[str]) -> list[str]:
    """One paragraph's lines, one sentence per line."""
    if any(line.endswith(("  ", "\\")) for line in lines[:-1]) or "\t" in "".join(lines):
        return list(lines)        # a hard line break or a tab: leave it as written
    if lines[0].startswith("    ") or lines[0].lstrip().startswith((">", "<")):
        return list(lines)        # maybe code, a quote or HTML: leave it as written
    first, indent = _prefix(lines[0])
    text = " ".join([lines[0][len(first):].strip(), *(line.strip() for line in lines[1:])])
    parts = sentences(text.strip())
    return [first + parts[0], *(indent + part for part in parts[1:])]


def relayout(text: str) -> str:
    """The contract with every paragraph one sentence per line."""
    lines = text.split("\n")
    out, done = [], 0
    for start, end in _paragraphs(lines):
        out.extend(lines[done:start])
        out.extend(relayout_paragraph(lines[start:end]))
        done = end
    out.extend(lines[done:])
    return "\n".join(out)


def paragraph_texts(text: str) -> list[str]:
    """Each paragraph's words, its line breaks and runs of spaces read as one space.

    `relayout` changes where lines break and nothing else, so it keeps this list.
    """
    lines = text.split("\n")
    texts = []
    for start, end in _paragraphs(lines):
        texts.append(" ".join(" ".join(lines[start:end]).split()))
    return texts


#: A clause's first line: `- **C-x.y**`, or `- **C-x.y Title.**` as C-8.5 is written.
CLAUSE = re.compile(r"^- \*\*(C-\d+\.\d+[a-z]?)\b[^*\n]*\*\*")


def clauses(text: str) -> dict[str, str]:
    """Each clause's whole text, its line breaks and runs of spaces read as one space.

    A clause is its `- **C-x.y**` list item's paragraph, then every line up to the next
    that is not blank and not indented: its indented paragraphs or tables. Read a clause
    this way, never by its first line.
    """
    lines, found, i = text.split("\n"), {}, 0
    while i < len(lines):
        match = CLAUSE.match(lines[i])
        if not match:
            i += 1
            continue
        end = _paragraph_end(lines, i)
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line[0].isspace():
                break                     # the next clause, heading or top-level text
            if not line.strip() or any(rule.match(line) for rule in (TABLE, FENCE, HEADING, RULE)):
                end += 1
            else:                         # a paragraph of the clause, with its lazy lines
                end = _paragraph_end(lines, end)
        found[match.group(1)] = " ".join(" ".join(lines[i:end]).split())
        i = end
    return found


def words(text: str) -> str:
    """The whole text with its line breaks and runs of spaces read as one space."""
    return " ".join(text.split())


def unlaid(text: str) -> list[int]:
    """1-based first lines of the paragraphs that are not one sentence per line."""
    lines = text.split("\n")
    wrong = []
    for start, end in _paragraphs(lines):
        if relayout_paragraph(lines[start:end]) != lines[start:end]:
            wrong.append(start + 1)
    return wrong


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", nargs="?", type=Path, default=CONTRACT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite the file in place")
    mode.add_argument("--stdout", action="store_true", help="print the laid-out file")
    args = parser.parse_args(argv)
    text = args.path.read_text(encoding="utf-8")
    if args.stdout:
        sys.stdout.write(relayout(text))
        return 0
    if args.write:
        new = relayout(text)
        if new != text:
            args.path.write_text(new, encoding="utf-8")
        return 0
    wrong = unlaid(text)
    for line in wrong:
        print(f"{args.path}:{line}: not one sentence per line", file=sys.stderr)
    if wrong:
        print(f"fix: python3 tools/contract_layout.py --write {args.path}", file=sys.stderr)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
