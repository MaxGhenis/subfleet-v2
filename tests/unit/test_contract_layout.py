"""C-20.6: the contract starts a line at each sentence and after each semicolon.

Git merges line by line, so the layout decides how often two pull requests conflict on
the contract. `tools/contract_layout.py` moves only line breaks: every paragraph keeps
its words, a list item's later lines continue it, and no line it makes can start a
Markdown block of its own. These tests pin the contract's layout and those properties.
"""

from __future__ import annotations

import re
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools import contract_layout as layout

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = (ROOT / "docs" / "acceptance-contract.md").read_text(encoding="utf-8")
SETTINGS = settings(max_examples=300, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow])


def test_the_contract_is_one_sentence_per_line():
    """C-20.6 `contract_layout.py` would change nothing; run it with --write if this fails."""
    assert layout.unlaid(CONTRACT) == []
    assert layout.relayout(CONTRACT) == CONTRACT


def test_the_cli_names_a_paragraph_to_fix(tmp_path, capsys):
    """C-20.6 the check exits 1 and names the line; --write fixes it."""
    path = tmp_path / "contract.md"
    path.write_text("# T\n\n- **C-1.1** One. Two; three.\n- **C-1.2** Fine.\n")
    assert layout.main([str(path)]) == 1
    assert f"{path}:3: not one sentence per line" in capsys.readouterr().err
    assert layout.main(["--write", str(path)]) == 0
    assert path.read_text() == "# T\n\n- **C-1.1** One.\n  Two;\n  three.\n- **C-1.2** Fine.\n"
    assert layout.main([str(path)]) == 0


def test_every_clause_is_read_whole():
    """C-20.6 `clauses` reads each `- **C-x.y**` item with all its lines, each id once.

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


def test_examples():
    """C-20.6 where lines break and where they do not."""
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
    """C-20.6 laying out a laid-out document changes nothing."""
    once = layout.relayout(document)
    assert layout.relayout(once) == once
    assert layout.unlaid(once) == []


@SETTINGS
@given(DOCUMENT)
def test_relayout_keeps_every_paragraph_and_its_words(document):
    """C-20.6 the same paragraphs with the same words, and every other line untouched."""
    once = layout.relayout(document)
    assert layout.paragraph_texts(once) == layout.paragraph_texts(document)
    assert _structure(once) == _structure(document)
    assert layout.words(once) == layout.words(document)


@SETTINGS
@given(DOCUMENT)
def test_relayout_never_starts_a_block(document):
    """C-20.6 no line relayout adds can start a list, quote, heading, table, fence or HTML."""
    lines = document.split("\n")
    for start, end in layout._paragraphs(lines):
        before = lines[start:end]
        after = layout.relayout_paragraph(before)
        assert after[0].startswith(layout._prefix(before[0])[0])
        if after != before:
            for line in after[1:]:
                assert not layout.BLOCK_START.match(line.lstrip()), line
                assert not layout.ITEM.match(line), line


@SETTINGS
@given(st.lists(SENTENCE, min_size=1, max_size=8))
def test_sentences_join_back(parts):
    """C-20.6 splitting a line into sentences loses nothing: rejoined, it is the line."""
    text = " ".join(" ".join(parts).split())
    pieces = layout.sentences(text)
    assert " ".join(pieces) == text
    assert all(piece and piece == piece.strip() for piece in pieces)


@SETTINGS
@given(DOCUMENT)
def test_clauses_survive_relayout(document):
    """C-20.6 every clause reads the same, whole, before and after."""
    assert layout.clauses(layout.relayout(document)) == layout.clauses(document)
