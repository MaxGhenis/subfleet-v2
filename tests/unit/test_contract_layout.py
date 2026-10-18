"""C-20.7: the contract starts a line at each sentence and after each semicolon.

Git merges line by line, so the layout decides how often two pull requests conflict on
the contract. `tools/contract_layout.py` moves only line breaks: every paragraph keeps
its words, a list item's later lines continue it, and no line it makes can start a
Markdown block of its own. These tests pin the contract's layout and those properties.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.markdown_signature import signature
from tools import contract_layout as layout

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = (ROOT / "docs" / "acceptance-contract.md").read_text(encoding="utf-8")
SETTINGS = settings(max_examples=300, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow])


def test_the_contract_is_one_sentence_per_line():
    """C-20.7 `contract_layout.py` would change nothing; run it with --write if this fails."""
    assert layout.unlaid(CONTRACT) == []
    assert layout.relayout(CONTRACT) == CONTRACT


def test_the_cli_names_a_paragraph_to_fix(tmp_path, capsys):
    """C-20.7 the check exits 1 and names the line; --write fixes it."""
    path = tmp_path / "contract.md"
    path.write_text("# T\n\n- **C-1.1** One. Two; three.\n- **C-1.2** Fine.\n")
    assert layout.main([str(path)]) == 1
    assert f"{path}:3: not one sentence per line" in capsys.readouterr().err
    assert layout.main(["--write", str(path)]) == 0
    assert path.read_text() == "# T\n\n- **C-1.1** One.\n  Two;\n  three.\n- **C-1.2** Fine.\n"
    assert layout.main([str(path)]) == 0


def test_every_clause_is_read_whole():
    """C-20.7 `clauses` reads each `- **C-x.y**` item with all its lines, each id once.

    The other tests that parse the contract find clause ids on an item's first line
    (`test_desktop_ledger`, `test_invariants_index`); every id they find is here too.
    `clauses` also reads C-8.5, whose bold marker carries a title.
    """
    found = layout.clauses(CONTRACT)
    starts = re.findall(r"^- \*\*(C-\d+\.\d+[a-z]?)", CONTRACT, re.M)
    assert sorted(found) == sorted(starts), "a clause id is defined twice"
    assert set(re.findall(r"^- \*\*(C-\d+\.\d+[a-z]?)\*\*", CONTRACT, re.M)) <= set(found)
    for clause, text in found.items():
        assert text.startswith(f"- **{clause}")
        assert "\n" not in text and "  " not in text
    # A clause's later sentences and its indented paragraphs and tables are part of it.
    assert "a row on the in-place workspace keeps no in-place job" in found["C-8.4"]
    assert found["C-8.5"].startswith("- **C-8.5 Host push (d1098, option A).**")
    assert "Push follows committed acceptance" in found["C-8.5"]
    assert "| Answer | Action state | Submission evidence |" in found["C-23.16"]
    assert "## " not in " ".join(found.values())


#: Inputs generated documents found whose rendering relayout once changed, and what it
#: does with each now: a pipe row before a `<pre>` block; an empty list item; backticks
#: that become a fence once split; an item holding a heading; an HTML block that runs to
#: the blank line; a raw `<pre>` around a paragraph; a fence inside a list item; code
#: spans across a line read as a block start (two); a tag that does not open an HTML
#: block; a delimiter row without a pipe (cmark-gfm); a span open across several blocks.
FOUND_BY_GENERATION = {
    "| a. B | c; D |\n<pre>\n\nA. B; c\n</pre>": "| a. B | c; D |\n<pre>\n\nA. B; c\n</pre>",
    "1.\nv1. Admission.": "1.\nv1. Admission.",    # since r2: an item's lazy tail is kept
    "a. a. a. a.\n``` v1. `code`.": "a. a. a. a.\n``` v1. `code`.",
    "- # a.\na.": "- # a.\na.",
    "<div>\n\nA. B\n</div>\na.\n```.\n<pre>\n\nA. B; c\n</pre>\n~~~\n```\nA. B\n~~~":
        "<div>\n\nA.\nB\n</div>\na.\n```.\n<pre>\n\nA. B; c\n</pre>\n~~~\n```\nA. B\n~~~",
    "<div>\n\nA. B\n</div>\n<pre>\n\nA. B; c\n</pre>": "<div>\n\nA.\nB\n</div>\n<pre>\n\nA. B; c\n</pre>",
    "- a. a. a. a. a.\n  ```. a.\n~~~\n```\nA. B\n~~~": "- a. a. a. a. a.\n  ```. a.\n~~~\n```\nA. B\n~~~",
    "a. `.\n  a.\n``` `code`.": "a. `.\n  a.\n``` `code`.",
    "- >. `.\na.  `code`. a.": "- >. `.\na.  `code`. a.",
    "a. a.\n<span>.\na.\n~~~. a. a. a.\n<div>\n\nA. B\n</div>":
        "a. a.\n<span>. a.\n~~~. a. a. a.\n<div>\n\nA. B\n</div>",
    "a.\n-:": "a.\n-:",
    "a. ```.\n<b>.\n  <b>;\na ```.": "a. ```.\n<b>.\n  <b>;\na ```.",
}


#: Review r2 of #218: inputs whose rendering relayout changed at b624670a4 (footnotes
#: under GitHub's renderer, tabs, precedence of code, links and HTML, escapes, a lazy `===`,
#: closing tags that were not, link labels over lines, CRLF), and what it does with each.
REVIEW_R2 = {
    'The limit is 128 characters[^1], measured\n[^1]: Counted in code points, not bytes.\n':
        'The limit is 128 characters[^1], measured\n[^1]: Counted in code points, not bytes.\n',
    'A. [^1]: x':
        'A. [^1]: x',
    '\t```\nA. B\n```\nC. D\n```':
        '\t```\nA. B\n```\nC. D\n```',
    '\xa0```\nA. B. C':
        '\xa0```\nA. B. C',
    '<http://a`b> then `X.  Y` and `z`.':
        '<http://a`b> then `X.  Y` and `z`.',
    '[a](u "`") then `X.  Y` and `z`.':
        '[a](u "`") then `X.  Y` and `z`.',
    'X <span title="a>b. C">x</span>.':
        'X <span title="a>b. C">x</span>.',
    'X <!-- a > b. C --> d.':
        'X <!-- a > b. C --> d.',
    'A \\` b `\n2. X.  Y` D.':
        'A \\` b `\n2. X.  Y` D.',
    '- A `x\n===\nB.  C`':
        '- A `x\n===\nB.  C`',
    'Text <pre>\n\nSay `</pre>`.\n\nA. B':
        'Text <pre>\n\nSay `</pre>`.\n\nA. B',
    'x <!-- </pre> -->\n\nA. B':
        'x <!-- </pre> -->\n\nA.\nB',
    '[a\nb]: /u "A. B"\n\n[a b]':
        '[a\nb]: /u "A. B"\n\n[a b]',
    '[a\\]]: /u "A. B"\n\n[a\\]]':
        '[a\\]]: /u "A. B"\n\n[a\\]]',
    'A.  \r\nB. C\r\n':
        'A.  \r\nB. C\r\n',
    '</pre> <pre>.`\n\nE. F':
        '</pre> <pre>.`\n\nE. F',
    '… <pre>...\n+ [^2]: *;\n </pre> [^1].`\n\n<br>; 10:05Z.':
        '… <pre>...\n+ [^2]: *;\n </pre> [^1].`\n\n<br>; 10:05Z.',
    # Found by tools/contract_layout_fuzz.py (exotic, seeds 10 and 11) after the r2 fixes:
    # a list item opening a fence after a quote line, and a footnote's lazy text that
    # GitHub moves to the end of the page, past an open raw <pre>.
    '> -->;\n+ ``` X")\n\n  `a. B`."\n --;':
        '> -->;\n+ ``` X")\n\n  `a. B`."\n --;',
    '* [^2]: `a. B`!\n "T. X") &#59;,\n\n*** <pre>.; [^2]:':
        '* [^2]: `a. B`!\n "T. X") &#59;,\n\n*** <pre>.; [^2]:',
    # Found by the fuzzer when tables and quotes were first stopped at list items: a
    # code span may run from a table's or quote's text into the paragraph after it.
    '+ i.e.?\n  B | |\n  |---|---|\nAdmission v1..` ~~gone~~!\n10. "Q." `':
        '+ i.e.?\n  B | |\n  |---|---|\nAdmission v1..` ~~gone~~!\n10. "Q." `',
    '+ C-5.5.),\n  B | |\n  |---|---|\n``a 2),\n10. B. ` b``.*':
        '+ C-5.5.),\n  B | |\n  |---|---|\n``a 2),\n10. B. ` b``.*',
    '1. a:\n   B | |\n   |---|:-:|\na B..` \n2) \'s." `code`':
        '1. a:\n   B | |\n   |---|:-:|\na B..` \n2) \'s." `code`',
    '`x; y` (x).`\ne.g. (x).`\n |\n<b>B</b> b``." _u_:\n10. ` b``? `a. `code`.;':
        '`x; y` (x).`\ne.g. (x).`\n |\n<b>B</b> b``." _u_:\n10. ` b``? `a. `code`.;',
    '| ` Ok.:\n10. "Q." ``.**\n </a> \\`':
        '| ` Ok.:\n10. "Q." ``.**\n </a> \\`',
}


def test_examples():
    """C-20.7 where lines break and where they do not."""
    cases = {
        # Sentences and semicolons; a list item continues, indented to its content.
        "- **C-1.1** A. B; c! D? E.": "- **C-1.1** A.\n  B;\n  c!\n  D?\n  E.",
        "1. A one. Two.": "1. A one.\n   Two.",
        "  Indented. Paragraph.": "  Indented.\n  Paragraph.",
        # No break after an abbreviation, before a lowercase word, or inside code.
        "Use e.g. `x` or i.e. Y. Done.": "Use e.g. `x` or i.e. Y.\nDone.",
        "Version 2.1.11. then on.": "Version 2.1.11. then on.",
        "Run `a. B; c` now. Next.": "Run `a. B; c` now.\nNext.",
        "Run ``a `b. C` d`` now. Next.": "Run ``a `b. C` d`` now.\nNext.",
        # A next part that would start a block stays on its line.
        "Ends. - not an item. 1. Not a list. # Not a heading. > No quote.":
            "Ends. - not an item. 1.\nNot a list. # Not a heading. > No quote.",
        "One; - two; | three": "One; - two; | three",
        "One. <b>Two</b> three.": "One. <b>Two</b> three.",
        "Closing (quote.) Next. \"Quoted.\" Then.": "Closing (quote.)\nNext.\n\"Quoted.\"\nThen.",
        # Joined first: a paragraph's existing line breaks are not kept.
        "A\nb. C\n d.": "A b.\nC d.",
        # Left alone: tables, headings, fences, hard breaks, tabs, code, quotes, HTML.
        "| a. B | c; d |": "| a. B | c; d |",
        "# A. B; c": "# A. B; c",
        "```\nA. B; c\n```": "```\nA. B; c\n```",
        "A.  \nB. C.": "A.  \nB. C.",
        "A.\tB. C.": "A.\tB. C.",
        "    code. More; code.": "    code. More; code.",
        "> A. B.": "> A. B.",
        "<div>A. B.</div>": "<div>A. B.</div>",
        # A line that may start a block ends the paragraph before it.
        "Title. Here\n---": "Title.\nHere\n---",
        "Setext. Title\n===\na. B.": "Setext.\nTitle\n===\na.\nB.",
        "A. B\n* * *\nC. D": "A.\nB\n* * *\nC.\nD",
        "- **C-1.1** a.\nLazy. Line\n===": "- **C-1.1** a.\n  Lazy.\n  Line\n===",
        "A. B\n> quote. C": "A.\nB\n> quote. C",
        # Review r1b of #218: each of these once rendered differently after a relayout.
        "A; ***": "A; ***",
        "[a]: /u\n[b]: /v\n\n[a] [b]": "[a]: /u\n[b]: /v\n\n[a] [b]",
        "~~~\n```\nA. B\n~~~": "~~~\n```\nA. B\n~~~",
        "````\n```\nA. B\n````": "````\n```\nA. B\n````",
        "-     A. B": "-     A. B",
        "- A.  \n  2. B": "- A.  \n  2. B",
        "A. B | C\n---|---\nD | E": "A. B | C\n---|---\nD | E",
        "<pre>\n\nA. B\n</pre>": "<pre>\n\nA. B\n</pre>",
        "`a\n b`": "`a\n b`",
        '[a](u "A; B") then. More.': '[a](u "A; B") then.\nMore.',
        "\\`a `X.  Y`": "\\`a `X.  Y`",
        "Tag <span title=\"A. B\">x</span>. Next.": "Tag <span title=\"A. B\">x</span>.\nNext.",
        # Found by the generated documents below while review r1b was being fixed.
        **FOUND_BY_GENERATION,
        **REVIEW_R2,
    }
    for given_, expected in cases.items():
        assert layout.relayout(given_) == expected, given_
        assert layout.relayout(expected) == expected, expected


# Generated documents: paragraphs, list items and the blocks relayout leaves alone, made of
# words, punctuation, code spans and abbreviations, so the splitter's rules all get used.
WORD = st.sampled_from([
    "a", "the", "Admission", "C-6.9", "e.g.", "i.e.", "etc.", "vs.", "v1.", "2.1.11.", "(x)",
    "`code`", "`a. B`", "``x ` y``", "`", "**bold**", "*em*", "_u_", "[link](u)", "\"Q.\"",
    "(see C-5.5.)", "-", "--", "1.", "2)", "#", ">", "<b>", "|", "=", "---", "&lt;", "'s",
    '[t](u "A; B. C")', "<span>", "</span>", "\\`", "&quot;", "***", "* * *", "___", "~~~",
    "```", "<x y=\"a. B\">", "\\", "[r]", "x|y",
])
END = st.sampled_from([".", ";", "!", "?", ",", ":", "", ".)", ".\"", "...", ".;"])
SENTENCE = st.builds(lambda ws, e: " ".join(ws) + e, st.lists(WORD, min_size=1, max_size=6), END)
SPACING = st.sampled_from([" ", "  ", "\n", "\n  ", "\n   "])


@st.composite
def paragraph(draw):
    parts = draw(st.lists(SENTENCE, min_size=1, max_size=5))
    text = parts[0]
    for part in parts[1:]:
        text += draw(SPACING) + part
    prefix = draw(st.sampled_from(["", "- ", "- **C-1.1** ", "1. ", "  ", "  - ", "* "]))
    return prefix + text


BLOCK = st.one_of(
    paragraph(), paragraph(), paragraph(),
    st.just(""), st.just("## Heading. With; marks"), st.just("| a. B | c; D |"),
    st.just("```\nA. B; c\n```"), st.just("    code. Line; two"), st.just("> quote. Two"),
    st.just("A.  \nhard break. B"), st.just("Setext. Title\n==="),
    st.just("~~~\n```\nA. B\n~~~"), st.just("````\n```\nA. B\n````"),
    st.just("<pre>\n\nA. B; c\n</pre>"), st.just("<!--\nA. B\n-->"), st.just("<div>\n\nA. B\n</div>"),
    st.just("A. B | C\n---|---\nD. E | F"), st.just("[r]: /u \"A. B\"\n[s]: /v"),
    st.just("-     A. B"), st.just("- A.  \n  2. B"), st.just("A.\\\nB. C"),
)
DOCUMENT = st.lists(BLOCK, min_size=1, max_size=8).map("\n".join)


def _structure(text: str) -> list[str]:
    """The lines relayout must leave as they are, in order."""
    lines = text.split("\n")
    inside = set()
    for start, end in layout._paragraphs(lines):
        inside.update(range(start, end))
    return [line for i, line in enumerate(lines) if i not in inside]


@SETTINGS
@given(DOCUMENT)
def test_relayout_is_idempotent(document):
    """C-20.7 laying out a laid-out document changes nothing."""
    once = layout.relayout(document)
    assert layout.relayout(once) == once
    assert layout.unlaid(once) == []


@SETTINGS
@given(DOCUMENT)
def test_relayout_keeps_every_paragraph_and_its_words(document):
    """C-20.7 the same paragraphs with the same words, and every other line untouched."""
    once = layout.relayout(document)
    assert layout.paragraph_texts(once) == layout.paragraph_texts(document)
    assert _structure(once) == _structure(document)
    assert layout.words(once) == layout.words(document)


@SETTINGS
@given(DOCUMENT)
def test_relayout_never_starts_a_block(document):
    """C-20.7 no line relayout adds can start a list, quote, heading, table, fence or HTML."""
    lines = document.split("\n")
    for start, end in layout._paragraphs(lines):
        before = lines[start:end]
        after = layout.relayout_paragraph(before)
        assert after[0].startswith(layout._prefix(before[0])[0])
        if after != before:
            for line in after[1:]:
                assert not layout.starts_block(line), line


@SETTINGS
@given(st.lists(SENTENCE, min_size=1, max_size=8))
def test_sentences_join_back(parts):
    """C-20.7 splitting a line into sentences loses nothing: rejoined, it is the line."""
    text = " ".join(" ".join(parts).split())
    pieces = layout.sentences(text)
    assert " ".join(pieces) == text
    assert all(piece and piece == piece.strip() for piece in pieces)


@SETTINGS
@given(DOCUMENT)
def test_clauses_survive_relayout(document):
    """C-20.7 every clause reads the same, whole, before and after."""
    assert layout.clauses(layout.relayout(document)) == layout.clauses(document)


@SETTINGS
@given(DOCUMENT)
def test_relayout_renders_the_same(document):
    """C-20.7 differential against a real renderer, not this module's own block reader:
    markdown-it-py (CommonMark with GFM tables) renders the document the same before and
    after, tags, attributes and code exactly, whitespace in text read as one space."""
    markdown_it = pytest.importorskip("markdown_it")
    render = markdown_it.MarkdownIt("commonmark").enable("table").render
    assert signature(render(layout.relayout(document))) == signature(render(document))


def test_the_contract_renders_the_same_on_one_line_per_paragraph():
    """C-20.7 the laid-out contract renders as it would with each paragraph on one line."""
    markdown_it = pytest.importorskip("markdown_it")
    render = markdown_it.MarkdownIt("commonmark").enable("table").render
    lines, out, done = CONTRACT.split("\n"), [], 0
    for start, end in layout._paragraphs(lines):
        first, _ = layout._prefix(lines[start])
        out += lines[done:start]
        out.append(first + " ".join([lines[start][len(first):].strip(),
                                     *(line.strip() for line in lines[start + 1:end])]))
        done = end
    joined = "\n".join(out + lines[done:])
    assert joined != CONTRACT
    assert signature(render(joined)) == signature(render(CONTRACT))


def test_the_found_inputs_render_the_same():
    """C-20.7 each input generation found, and each review r1b case above, renders the
    same before and after (markdown-it-py; cmark-gfm was checked by hand, see the report)."""
    markdown_it = pytest.importorskip("markdown_it")
    render = markdown_it.MarkdownIt("commonmark").enable("table").render
    for document in [*FOUND_BY_GENERATION, *REVIEW_R2]:
        assert signature(render(layout.relayout(document))) == signature(render(document)), document
