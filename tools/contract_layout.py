"""Lay out `docs/acceptance-contract.md` one sentence per line (defect D-MRG3).

Git merges line by line, and changes to adjacent lines conflict. The contract kept each
clause on one line (the longest, C-8.4, 18,180 characters), so two pull requests that changed
different sentences of one clause, or the last words of one clause and the first of the
next, conflicted with no word changed by both. GitHub runs no CI on a conflicting pull
request (D-CI1), so each conflict cost a merge and a fresh CI run.

`relayout` starts a line at each sentence of every plain paragraph, and after each
semicolon. A list item's later lines are indented to its content, as continuation lines,
so the rendered document is unchanged: Markdown joins a paragraph's lines with soft
breaks. It rewrites only what it can read with certainty: fenced code, HTML blocks,
tables, headings, rules, block quotes, link reference definitions and indented code are
left as they are, and so is a paragraph whose line breaks carry meaning (a hard break, a
tab, an escaped backtick, or a code span, link destination or HTML tag that spans lines).
It never breaks a line inside a code span, a link destination or title, or an HTML tag,
and never starts a line that Markdown would read as a block of its own.

Of the 185 merges in `docs/reports/2026-10-14-d-mrg3-contract-replay.tsv`, each one in
which both sides changed the contract, 128 conflicted on it; with the history moved out
(C-20.7) 71 would have, and with this layout as well, 37.

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

#: An opening code fence: three or more backticks or tildes (CommonMark 4.5).
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
TABLE = re.compile(r"^\s*\|")
#: A GFM table's delimiter row (`---|:---:`, `| --- |`, and in cmark-gfm even `-:`); one
#: that is also a rule or setext underline (`---`) is read as that instead.
DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
HEADING = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
#: A setext underline or a thematic break: a line of its own, never part of a paragraph.
RULE = re.compile(r"^ {0,3}(?:=+|-+|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})[ \t]*$")
ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+|$)")
LINK_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:")
#: What may not begin a continuation line: it would start a block of its own (a list
#: item, heading, quote, fence, table row, HTML block, or a setext underline) instead of
#: continuing the paragraph.
BLOCK_START = re.compile(r"^(?:[-*+](?:\s|$)|\d{1,9}[.)](?:\s|$)|#{1,6}(?:\s|$)|>|```|~~~|\||<|[=-]+\s*$)")
#: HTML blocks that end at a marker rather than at a blank line (CommonMark 4.6, kinds 1-5).
HTML_ENDS = (
    (re.compile(r"^\s*<(script|pre|style|textarea)(\s|>|$)", re.I),
     re.compile(r"</(script|pre|style|textarea)>", re.I)),
    (re.compile(r"^\s*<!--"), re.compile(r"-->")),
    (re.compile(r"^\s*<\?"), re.compile(r"\?>")),
    (re.compile(r"^\s*<![A-Za-z]"), re.compile(r">")),
    (re.compile(r"^\s*<!\[CDATA\["), re.compile(r"\]\]>")),
)
#: HTML blocks that end at a blank line (CommonMark 4.6): kind 6, a block-level tag, which
#: can interrupt a paragraph; kind 7, any other complete tag alone on its line, which
#: cannot. A line starting with `<` that is neither, nor kinds 1-5, is paragraph text.
HTML_BLOCK_TAG = re.compile(
    r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|"
    r"colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|"
    r"frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|"
    r"noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|"
    r"thead|title|tr|track|ul)(?:\s|/?>|$)", re.I)
HTML_ALONE_TAG = re.compile(
    r"^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|"
    r"'[^']*'|\"[^\"]*\"))?)*\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)\s*$")
#: Raw HTML elements whose whitespace shows: Markdown inside one keeps its line breaks.
EXACT_TAG = re.compile(r"<(/?)(pre|textarea|script|style)\b", re.I)
#: A sentence ends at `.`, `!` or `?`, optionally followed by a closing bracket or quote.
END = re.compile(r"[.!?][)\]\"'”’]?$")
#: A sentence may begin with a capital letter, a code span, a quote, a bracket or emphasis.
BEGIN = re.compile(r"[A-Z`\"'“‘(\[*_]")
#: A period after these words does not end a sentence.
ABBREVIATIONS = frozenset({
    "al", "approx", "ca", "cf", "dr", "e.g", "eq", "etc", "fig", "i.e", "incl", "mr", "ms",
    "no", "nos", "p", "pp", "resp", "sec", "st", "u.s", "viz", "vs",
})


def _code_spans(text: str, unmatched: list[int] | None = None) -> list[tuple[int, int]]:
    """CommonMark code spans: a run of N backticks to the next run of exactly N. The start
    of each run with no closer is appended to `unmatched` when it is given."""
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
            if unmatched is not None:
                unmatched.append(i)
            i = j          # an unmatched run is literal text
        else:
            spans.append((i, close))
            i = close
    return spans


def _inside(spans: list[tuple[int, int]], at: int) -> bool:
    return any(s < at < e for s, e in spans)


def protected(text: str) -> list[tuple[int, int]]:
    return _protected(text)[0]


def left_open(text: str) -> bool:
    """Whether a backtick run, a link destination or an HTML tag in `text` has no end."""
    return _protected(text)[1]


def _protected(text: str) -> tuple[list[tuple[int, int]], bool]:
    """Spans no line break may enter or leave: code spans, link destinations and titles
    (`](...)`), and HTML tags or autolinks (`<...>`). A span with no end runs to the end of
    the text, and so does a backtick run with no closer: in a paragraph this reader split
    from the next block, it may open a code span that ends there."""
    unmatched: list[int] = []
    spans = _code_spans(text, unmatched)
    spans += [(start, len(text)) for start in unmatched]
    is_open = bool(unmatched)
    for match in re.finditer(r"\]\(", text):
        start = match.end() - 1
        if _inside(spans, start):
            continue
        depth, quote, k = 0, None, start
        while k < len(text):
            ch = text[k]
            if quote:
                if ch == quote and text[k - 1] != "\\":
                    quote = None
            elif ch in "\"'" and text[k - 1] in " \n(":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        is_open |= k >= len(text)
        spans.append((start, k + 1))
    for match in re.finditer(r"<[A-Za-z/!?]", text):
        if _inside(spans, match.start()):
            continue
        close = text.find(">", match.start())
        is_open |= close < 0
        spans.append((match.start(), len(text) if close < 0 else close + 1))
    return spans, is_open


def starts_block(line: str) -> bool:
    """Whether a line, as a paragraph's continuation line, could start a block instead."""
    body = line.lstrip()
    return bool(BLOCK_START.match(body) or ITEM.match(line) or RULE.match(body)
                or DELIMITER.match(body) or FENCE.match(body))


def sentences(text: str) -> list[str]:
    """Split one paragraph's text (no line breaks) into sentences and semicolon clauses.

    A boundary is a run of spaces outside every protected span, either after a semicolon
    or after a sentence end (not an abbreviation) and before a sentence start, where the
    part after it would not begin a Markdown block of its own. Joining the result with
    single spaces gives back `text` with each boundary's run of spaces collapsed to one.
    """
    spans = protected(text)
    out, start = [], 0
    for match in re.finditer(r" +", text):
        a, b = match.span()
        if a == 0 or b == len(text):
            continue
        if _inside(spans, a):
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
    # A part that would start a block as a line of its own (`***`, `---|---`) stays on
    # the line before it.
    merged = [out[0]]
    for part in out[1:]:
        if starts_block(part):
            merged[-1] += " " + part
        else:
            merged.append(part)
    return merged


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _until_blank(lines: list[str], start: int) -> int:
    """The next blank line: where an HTML block of kinds 6 and 7 ends, whatever it holds."""
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return end


def _until_interrupted(lines: list[str], start: int) -> int:
    """The next blank line, or the next line that opens a fence or an HTML block: either
    can interrupt a table, a quote or indented code, and has an end of its own."""
    end = start + 1
    while end < len(lines) and lines[end].strip() and _fence_end(lines, end) is None \
            and html_kind(lines[end]) not in (1, 2, 3, 4, 5, 6):
        end += 1
    return end


def _fence_end(lines: list[str], start: int) -> int | None:
    """The line after a fenced block opening at `start`, or None if it is no fence."""
    match = FENCE.match(lines[start])
    if not match:
        return None
    fence, info = match.group(1), match.group(2)
    if fence[0] == "`" and "`" in info:
        return None                       # an inline code span, not a fence
    closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$")
    end = start + 1
    while end < len(lines) and not closing.match(lines[end]):
        end += 1
    return min(end + 1, len(lines))


def html_kind(line: str) -> int | None:
    """The CommonMark kind (1-7) of the HTML block this line opens, or None."""
    for kind, (opening, _) in enumerate(HTML_ENDS, 1):
        if opening.match(line):
            return kind
    if HTML_BLOCK_TAG.match(line):
        return 6
    if HTML_ALONE_TAG.match(line):
        return 7
    return None


def _html_end(lines: list[str], start: int) -> int | None:
    """The line after an HTML block opening at `start`, or None if it is not one."""
    kind = html_kind(lines[start])
    if kind is None:
        return None
    if kind <= 5:
        closing = HTML_ENDS[kind - 1][1]
        end = start
        while end < len(lines) and not closing.search(lines[end]):
            end += 1
        return min(end + 1, len(lines))
    return _until_blank(lines, start)


def _ambiguous(line: str) -> bool:
    """A fence or an HTML block that opens indented or after a list marker: it lives in a
    container whose end this reader does not track, so what follows it is not read."""
    rest = line
    while True:
        item = ITEM.match(rest)
        if not item or not rest[item.end():].strip():
            break
        rest = rest[item.end():]
    body = rest.lstrip(" ")
    if rest is line and _indent(line) == 0:
        return False
    if html_kind(body) is not None:
        return True
    match = FENCE.match(body)
    return bool(match) and not (match.group(1)[0] == "`" and "`" in match.group(2))


def _table_ahead(lines: list[str], at: int) -> bool:
    """Whether `lines[at]` is a table's header row: the next line is a delimiter row."""
    return at + 1 < len(lines) and bool(DELIMITER.match(lines[at + 1])) \
        and not RULE.match(lines[at + 1])


def _paragraph_end(lines: list[str], start: int) -> int:
    """The line after the paragraph that starts at `start`."""
    end = start + 1
    while end < len(lines):
        nxt = lines[end]
        # A line that may start a block of its own ends the paragraph: relayout never
        # joins it to the line before.
        if not nxt.strip() or HEADING.match(nxt) or TABLE.match(nxt) or starts_block(nxt) \
                or _table_ahead(lines, end):
            break
        end += 1
    return end


#: Blocks that end any paragraph before them, so no inline span runs across them.
INTERRUPTERS = {"fence", "html", "heading", "rule", "quote", "table"}


def _classify(lines: list[str], i: int) -> tuple[str, int]:
    """The kind of block that starts at `lines[i]` (not blank) and the line after it."""
    line = lines[i]
    end = _fence_end(lines, i)
    if end is not None:
        return "fence", end
    end = _html_end(lines, i)
    if end is not None:
        return "html", end
    if TABLE.match(line) or _table_ahead(lines, i):
        return "table", _until_interrupted(lines, i)
    if line.lstrip().startswith(">"):
        return "quote", _until_interrupted(lines, i)
    if _indent(line) >= 4:
        return "indented", _until_interrupted(lines, i)
    if LINK_DEFINITION.match(line):
        return "definition", _until_interrupted(lines, i)
    if HEADING.match(line):
        return "heading", i + 1
    if RULE.match(line):
        return "rule", i + 1
    if _empty_item(line):
        return "item", i + 1
    return "paragraph", _paragraph_end(lines, i)


def _blocks(lines: list[str]):
    """Yield (start, end, paragraph): every block, and whether relayout may rewrite it.

    Not rewritten, besides what is not a paragraph:
    - a paragraph inside a raw `<pre>`, `<textarea>`, `<script>` or `<style>` that an
      earlier block opened, where line breaks show;
    - a paragraph that follows, with no blank line between, a block that left a backtick
      run, link destination or tag open, where an inline span may run on into it.
    Everything from a fence or an HTML block that opens indented or after a list marker
    on is left as written: it sits in a container whose end this reader does not track.
    """
    i, depth, left = 0, 0, False
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            left = False                  # a blank line ends every paragraph
            i += 1
            continue
        if _ambiguous(line):
            yield i, len(lines), False    # the rest is left as written
            return
        kind, end = _classify(lines, i)
        if kind in INTERRUPTERS:
            left = False
        yield i, end, kind == "paragraph" and depth == 0 and not left
        if kind != "fence":
            for tag in EXACT_TAG.finditer("\n".join(lines[i:end])):
                depth = max(0, depth + (-1 if tag.group(1) else 1))
        if kind not in INTERRUPTERS:
            left = left or left_open("\n".join(lines[i:end]))
        i = end


def _empty_item(line: str) -> bool:
    """A list marker with nothing after it, or with a block after it (a heading, quote,
    fence, table, HTML, rule or another marker): a line of its own, never joined."""
    item = ITEM.match(line)
    if not item:
        return False
    content = line[item.end():]
    return not content.strip() or starts_block(content) or bool(
        HEADING.match(content) or TABLE.match(content) or LINK_DEFINITION.match(content))


def _paragraphs(lines: list[str]):
    """Yield (start, end) line ranges of the paragraphs relayout may rewrite."""
    for start, end, paragraph in _blocks(lines):
        if paragraph:
            yield start, end


def _prefix(line: str) -> tuple[str, str]:
    """A paragraph's first-line prefix and the indent of its continuation lines."""
    item = ITEM.match(line)
    if item:
        return item.group(0), " " * len(item.group(0))
    indent = line[:len(line) - len(line.lstrip(" "))]
    return indent, indent


def kept_as_written(lines: list[str]) -> bool:
    """Whether a paragraph's line breaks may carry meaning, so relayout leaves it alone."""
    raw = "\n".join(lines)
    if any(line.endswith(("  ", "\\")) for line in lines) or "\t" in raw or "\\`" in raw:
        return True                       # a hard break, a tab or an escaped backtick
    if EXACT_TAG.search(raw):
        return True                       # a raw <pre> or the like, whose whitespace shows
    item = ITEM.match(lines[0])
    if item and len(item.group(3)) >= 5:
        return True                       # a list item whose content is indented code
    if FENCE.match(lines[0][len(item.group(0)) if item else 0:]):
        return True                       # backticks that are not a fence, until split
    return any("\n" in raw[s:e] for s, e in protected(raw))


def relayout_paragraph(lines: list[str]) -> list[str]:
    """One paragraph's lines, one sentence per line."""
    if kept_as_written(lines):
        return list(lines)
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
