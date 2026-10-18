"""Lay out `docs/acceptance-contract.md` one sentence per line (defect D-MRG3).

Git merges line by line, and changes to adjacent lines conflict. The contract kept each
clause on one line (the longest, C-8.4, 18,180 characters), so two pull requests that changed
different sentences of one clause, or the last words of one clause and the first of the
next, conflicted with no word changed by both. GitHub runs no CI on a conflicting pull
request (D-CI1), so each conflict cost a merge and a fresh CI run.

`relayout` starts a line at each sentence of every plain paragraph, and after each
semicolon. A list item's later lines are indented to its content, as continuation lines,
so the rendered document is unchanged: Markdown joins a paragraph's lines with soft
breaks. It rewrites only what it can read with certainty:

- fenced code, HTML blocks, tables, headings, rules, block quotes, indented code, link
  reference definitions and footnote definitions are left as they are;
- so is a paragraph whose line breaks may carry meaning: a hard break, a tab, a raw
  `<pre>`-like element, or a code span, link destination or title, autolink or HTML tag
  that spans lines;
- so is everything from a fence or HTML block opened inside a list item, or from a line
  indented with a tab, to the end of the document: this reader does not track where such
  a container ends;
- so is a paragraph that follows, with no blank line between, a block that left such a
  span open;
- and so is a document with carriage returns.

It never breaks a line inside one of those spans, and never starts a line that Markdown
would read as a block of its own. Of the 185 merges in
`docs/reports/2026-10-14-d-mrg3-contract-replay.tsv`, each one in which both sides changed
the contract, 128 conflicted on it; with the history moved out (C-20.7) 71 would have,
and with this layout as well, 37.

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

# Block structure. Indentation is counted in spaces only; a line indented with a tab is
# left to `_ambiguous`.
#: An opening code fence: three or more backticks or tildes (CommonMark 4.5).
FENCE = re.compile(r"^ *(`{3,}|~{3,})(.*)$")
TABLE = re.compile(r"^ *\|")
#: A GFM table's delimiter row (`---|:---:`, `| --- |`, and in cmark-gfm even `-:`); one
#: that is also a rule or setext underline (`---`) is read as that instead.
DELIMITER = re.compile(r"^ *\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
HEADING = re.compile(r"^ {0,3}#{1,6}([ \t]|$)")
#: A setext underline or a thematic break: a line of its own, never part of a paragraph.
RULE = re.compile(r"^ {0,3}(?:=+|-+|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})[ \t]*$")
ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])([ \t]+|$)")
#: A link reference definition may start here (its label can span lines or hold escapes,
#: so any paragraph opening with `[` and holding `]:` is read as one).
LINK_DEFINITION = re.compile(r"^ {0,3}\[")
#: A line that is itself plainly a link reference definition's start.
LINK_DEFINITION_LINE = re.compile(r"^ {0,3}\[[^\]]*\]:")
#: A footnote definition, which in cmark-gfm (GitHub) interrupts a paragraph.
FOOTNOTE = re.compile(r"^ {0,3}\[\^[^\]\s]+\]:")
#: What may not begin a continuation line: it would start a block of its own (a list
#: item, heading, quote, fence, table row, HTML block, footnote, or a setext underline)
#: instead of continuing the paragraph.
BLOCK_START = re.compile(r"^(?:[-*+](?:[ \t]|$)|\d{1,9}[.)](?:[ \t]|$)|#{1,6}(?:[ \t]|$)|>|```|~~~|"
                         r"\||<|\[\^[^\]\s]+\]:|[=-]+[ \t]*$)")
#: HTML blocks that end at a marker rather than at a blank line (CommonMark 4.6, kinds 1-5).
HTML_ENDS = (
    (re.compile(r"^ {0,3}<(script|pre|style|textarea)(\s|>|$)", re.I),
     re.compile(r"</(script|pre|style|textarea)>", re.I)),
    (re.compile(r"^ {0,3}<!--"), re.compile(r"-->")),
    (re.compile(r"^ {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r"^ {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r"^ {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
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
#: An HTML open or closing tag (CommonMark 6.6): attribute values may hold `>` in quotes.
TAG = re.compile(
    r"<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|"
    r"\"[^\"]*\"))?)*\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>")
HTML_ALONE_TAG = re.compile(r"^ {0,3}(?:" + TAG.pattern + r")[ \t]*$")
AUTOLINK = re.compile(r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|"
                      r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
                      r"[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)>")
#: Inline raw HTML that runs to an end marker (CommonMark 6.6): comments, processing
#: instructions, declarations and CDATA.
RAW_ENDS = (("<!--", "-->"), ("<?", "?>"), ("<![CDATA[", "]]>"))
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
_ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _angle_end(text: str, at: int) -> tuple[int, bool] | None:
    """Where the construct starting with `<` at `at` ends, and whether it is left open:
    an autolink, a comment, processing instruction, declaration or CDATA (to its marker,
    open to the end of the text if the marker never comes), or an HTML tag. None when the
    `<` is plain text."""
    rest = text[at:]
    for opening, closing in RAW_ENDS:
        if rest.startswith(opening):
            end = text.find(closing, at + len(opening))
            return (len(text), True) if end < 0 else (end + len(closing), False)
    if re.match(r"<![A-Za-z]", rest):
        end = text.find(">", at)
        return (len(text), True) if end < 0 else (end + 1, False)
    match = AUTOLINK.match(text, at) or TAG.match(text, at)
    if match:
        return match.end(), False
    if re.match(r"</?[A-Za-z]", rest) and ">" not in rest:
        return len(text), True            # a tag that may close past this text
    return None


def _link_end(text: str, at: int) -> tuple[int, bool]:
    """Where an inline link's `(destination "title")` starting at `at` ends (the `(`),
    and whether it is left open."""
    depth, quote, k = 0, None, at
    while k < len(text):
        ch = text[k]
        if ch == "\\" and k + 1 < len(text):
            k += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and text[k - 1] in " \t\n":
            quote = ch
        elif ch == "<" and k == at + 1:
            close = text.find(">", k)
            if close < 0:
                return len(text), True
            k = close
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return k + 1, False
        k += 1
    return len(text), True


def _scan(text: str, backticks: bool, angles: bool,
          links: bool) -> tuple[list[tuple[int, int]], bool]:
    """One left-to-right read (CommonMark 6): code spans, autolinks and raw HTML have equal
    precedence, first come first served; an inline link's destination and title are read
    where its `](` comes after an open `[`. Backslash escapes are honoured outside code.
    A construct with no end protects the rest of the text and marks it left open."""
    spans, is_open, i, brackets = [], False, 0, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in _ESCAPABLE:
            i += 2
            continue
        if ch == "`" and backticks:
            j = i
            while j < len(text) and text[j] == "`":
                j += 1
            run, k, close = j - i, j, None
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
                spans.append((i, len(text)))
                is_open = True
                i = j
            else:
                spans.append((i, close))
                i = close
            continue
        if ch == "<" and angles:
            found = _angle_end(text, i)
            if found:
                end, left_open_here = found
                spans.append((i, end))
                is_open |= left_open_here
                i = end
                continue
        if links and ch == "[":
            brackets += 1
        elif links and ch == "]" and brackets:
            brackets -= 1
            if i + 1 < len(text) and text[i + 1] == "(":
                end, left_open_here = _link_end(text, i + 1)
                spans.append((i + 1, end))
                is_open |= left_open_here
                i = end
                continue
        i += 1
    return spans, is_open


def _inline(text: str) -> tuple[list[tuple[int, int]], bool]:
    """Spans no line break may enter or leave, and whether any construct was left open.

    The union of three reads: the full one, and code spans alone and raw HTML alone, so a
    mistake in precedence can only protect more, never less."""
    spans, is_open = [], False
    for flags in ((True, True, True), (True, False, False), (False, True, False)):
        found, opened = _scan(text, *flags)
        spans += found
        is_open |= opened
    return spans, is_open


def protected(text: str) -> list[tuple[int, int]]:
    return _inline(text)[0]


def left_open(text: str) -> bool:
    """Whether a backtick run, link destination, autolink or HTML construct has no end."""
    return _inline(text)[1]


def _inside(spans: list[tuple[int, int]], at: int) -> bool:
    return any(s < at < e for s, e in spans)


def starts_block(line: str) -> bool:
    """Whether a line, as a paragraph's continuation line, could start a block instead."""
    body = line.lstrip(" ")
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
    """The next blank line, or the next line that opens a fence, an HTML block or a list
    item, or that `_ambiguous` stops at: any of these can interrupt a table, a quote or
    indented code, and the reader must see it."""
    end = start + 1
    while end < len(lines) and lines[end].strip() and _fence_end(lines, end) is None \
            and html_kind(lines[end]) not in (1, 2, 3, 4, 5, 6) \
            and not ITEM.match(lines[end]) and not _ambiguous(lines[end]):
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
    closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
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
    """A line inside a container whose end this reader does not track, so nothing from
    it on is read: one indented with a tab, or a fence or HTML block that opens indented
    or after a list marker."""
    lead = line[:len(line) - len(line.lstrip())]
    if "\t" in lead:
        return True
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
                or _table_ahead(lines, end) or _ambiguous(nxt):
            break
        end += 1
    return end


#: Blocks whose text can never run on into the next block's inline text. A table, quote
#: or definition can: renderers differ on where they end (markdown-it ends a table at a
#: line without a pipe, cmark-gfm does not), and what follows may be their continuation.
NOT_INLINE = {"fence", "html", "heading", "rule"}


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
    if line.lstrip(" ").startswith(">"):
        return "quote", _until_interrupted(lines, i)
    if _indent(line) >= 4:
        return "indented", _until_interrupted(lines, i)
    if FOOTNOTE.match(line):
        return "definition", _until_interrupted(lines, i)
    if LINK_DEFINITION.match(line):
        end = _paragraph_end(lines, i)
        if "]:" in "\n".join(lines[i:end]):
            return "definition", _until_interrupted(lines, i)
    if HEADING.match(line):
        return "heading", i + 1
    if RULE.match(line):
        return "rule", i + 1
    if _empty_item(line):
        return "item", i + 1
    return "paragraph", _paragraph_end(lines, i)


def _exact_depth(text: str, kind: str, depth: int) -> int:
    """How many raw `<pre>`-like elements are open after a block, walking its tags in
    order: every opening tag counts (the safe direction); a closing tag counts only in a
    paragraph or an HTML block, and only where it is real HTML (not in a code span, a
    comment, or a footnote, whose text GitHub moves to the end of the page)."""
    if kind == "fence":
        return depth
    events = [(tag.start(), +1) for tag in EXACT_TAG.finditer(text) if not tag.group(1)]
    if kind == "html":
        for tag in EXACT_TAG.finditer(text):
            inside_comment = any(s <= tag.start() < e for s, e in
                                 ((m.start(), m.end()) for m in re.finditer(r"<!--.*?-->", text, re.S)))
            if tag.group(1) and not inside_comment:
                events.append((tag.start(), -1))
    elif kind == "paragraph":
        for start, end in _scan(text, True, True, True)[0]:
            if TAG.fullmatch(text, start, end) and text[start + 1] == "/" \
                    and EXACT_TAG.match(text, start):
                events.append((start, -1))
    for _, step in sorted(events):
        depth = max(0, depth + step)
    return depth


def _blocks(lines: list[str]):
    """Yield (start, end, paragraph): every block, and whether relayout may rewrite it.

    Not rewritten, besides what is not a paragraph:
    - a paragraph inside a raw `<pre>`, `<textarea>`, `<script>` or `<style>` that an
      earlier block opened, where line breaks show;
    - a paragraph that follows, with no blank line between, a block that left a backtick
      run, link destination, autolink or tag open, where an inline span may run on into
      it (only a blank line ends that);
    - a paragraph right after a list item, definition or quote line it may continue
      lazily (a footnote's text, which GitHub moves to the end of the page);
    - everything from `_ambiguous` on.
    """
    i, depth, left, tail = 0, 0, False, False
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            left = tail = False           # a blank line ends every paragraph
            i += 1
            continue
        if _ambiguous(line):
            yield i, len(lines), False    # the rest is left as written
            return
        kind, end = _classify(lines, i)
        yield i, end, kind == "paragraph" and depth == 0 and not left and not tail
        text = "\n".join(lines[i:end])
        # Right after a list item, definition or quote line, a paragraph may be its lazy
        # continuation (a footnote's, which GitHub moves): count no closing tag there.
        depth = _exact_depth(text, "tail" if tail and kind == "paragraph" else kind, depth)
        tail = tail or kind in ("item", "definition", "quote")
        if kind not in NOT_INLINE:
            left = left or left_open(text)
        i = end


def _empty_item(line: str) -> bool:
    """A list marker with nothing after it, or with a block after it (a heading, quote,
    fence, table, HTML, rule or another marker): a line of its own, never joined."""
    item = ITEM.match(line)
    if not item:
        return False
    content = line[item.end():]
    return not content.strip() or starts_block(content) or bool(
        HEADING.match(content) or TABLE.match(content) or LINK_DEFINITION_LINE.match(content))


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
    if any(line.endswith(("  ", "\\")) for line in lines) or "\t" in raw or "\r" in raw:
        return True                       # a hard break, a tab or a carriage return
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
    """The contract with every paragraph one sentence per line (a text with carriage
    returns is returned as it is)."""
    if "\r" in text:
        return text
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
