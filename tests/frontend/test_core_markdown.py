"""The Markdown the conversation view renders (C-29.8): blocks, inlines, bounds."""

from __future__ import annotations

import uuid

import pytest

from tests.frontend.conftest import needs_swift, run_probe

pytestmark = needs_swift


def md(core_probe, tmp_path, text: str) -> list[dict]:
    path = tmp_path / f"{uuid.uuid4().hex}.md"
    path.write_text(text)
    return run_probe(core_probe, "markdown", path)


def t(text: str) -> dict:
    return {"text": text}


def para(*inlines) -> dict:
    return {"type": "paragraph", "content": list(inlines)}


def test_headings(core_probe, tmp_path):
    blocks = md(core_probe, tmp_path, "# One\n## Two ##\n###### Six\n####### seven\n#nospace\nSetext\n===\nAnother\n---\n")
    assert blocks[:3] == [{"type": "heading", "level": 1, "content": [t("One")]},
                          {"type": "heading", "level": 2, "content": [t("Two")]},
                          {"type": "heading", "level": 6, "content": [t("Six")]}]
    assert blocks[3] == {"type": "heading", "level": 1, "content": [t("####### seven"), {"break": "soft"}, t("#nospace"),
                                                                    {"break": "soft"}, t("Setext")]}
    assert blocks[4] == {"type": "heading", "level": 2, "content": [t("Another")]}


def test_paragraphs_breaks_and_rules(core_probe, tmp_path):
    blocks = md(core_probe, tmp_path, "line one\nline two  \nline three\\\nfour\n\n***\n\n- - -\nafter\n")
    assert blocks[0] == para(t("line one"), {"break": "soft"}, t("line two"), {"break": "hard"}, t("line three"),
                             {"break": "hard"}, t("four"))
    assert blocks[1] == {"type": "rule"} and blocks[2] == {"type": "rule"}
    assert blocks[3] == para(t("after"))


def test_lists_nest_order_and_check(core_probe, tmp_path):
    text = ("- alpha\n- beta\n  - inner *one*\n  - inner two\n- [x] done\n- [ ] todo\n\n"
            "3. third\n4. fourth\n\n* loose one\n\n* loose two\n")
    blocks = md(core_probe, tmp_path, text)
    bullets, ordered, loose = blocks
    assert bullets["ordered"] is False and bullets["tight"] is True and len(bullets["items"]) == 4
    assert bullets["items"][0] == {"blocks": [para(t("alpha"))], "checked": None}
    nested = bullets["items"][1]["blocks"]
    assert nested[0] == para(t("beta")) and nested[1]["type"] == "list"
    assert nested[1]["items"][0]["blocks"] == [para(t("inner "), {"emphasis": [t("one")]})]
    assert [i["checked"] for i in bullets["items"][2:]] == [True, False]
    assert bullets["items"][2]["blocks"] == [para(t("done"))]
    assert ordered["ordered"] is True and ordered["start"] == 3 and len(ordered["items"]) == 2
    assert loose["tight"] is False and len(loose["items"]) == 2


def test_a_list_can_interrupt_a_paragraph_but_only_from_one(core_probe, tmp_path):
    blocks = md(core_probe, tmp_path, "Steps:\n- first\n- second\n\nThe year\n2026. was long\n")
    assert blocks[0] == para(t("Steps:"))
    assert blocks[1]["type"] == "list" and len(blocks[1]["items"]) == 2
    assert blocks[2] == para(t("The year"), {"break": "soft"}, t("2026. was long"))


def test_code_blocks(core_probe, tmp_path):
    text = ("```swift\nlet x = 1\n\n  indented\n```\n\n~~~\n```not a close```\n~~~\n\n    four spaces\n    more\n\n"
            "```python\nstill streaming\n")
    blocks = md(core_probe, tmp_path, text)
    assert blocks[0] == {"type": "code", "language": "swift", "text": "let x = 1\n\n  indented", "closed": True}
    assert blocks[1] == {"type": "code", "language": None, "text": "```not a close```", "closed": True}
    assert blocks[2] == {"type": "code", "language": None, "text": "four spaces\nmore", "closed": True}
    assert blocks[3] == {"type": "code", "language": "python", "text": "still streaming\n", "closed": False}


def test_inline_formatting(core_probe, tmp_path):
    blocks = md(core_probe, tmp_path,
                "a **strong** and *em* and _em_ and ***both*** and ~~gone~~ and `co*de*` and snake_case_name "
                "and 2*3*4 and \\*literal\\* and ``a ` b``")
    assert blocks == [para(
        t("a "), {"strong": [t("strong")]}, t(" and "), {"emphasis": [t("em")]}, t(" and "), {"emphasis": [t("em")]},
        t(" and "), {"emphasis": [{"strong": [t("both")]}]}, t(" and "), {"strike": [t("gone")]}, t(" and "),
        {"code": "co*de*"}, t(" and snake_case_name and 2"), {"emphasis": [t("3")]}, t("4 and *literal* and "),
        {"code": "a ` b"})]


def test_links(core_probe, tmp_path):
    blocks = md(core_probe, tmp_path,
                "See [the *docs*](https://example.com/a_(b) \"title\") and <https://x.io/p?q=1> and <me@example.com> "
                "and https://github.com/org/repo/pull/7. Also (www.example.org) and ![a diagram](https://i/p.png) "
                "and [not a link] and [dangling](")
    content = blocks[0]["content"]
    assert content[1] == {"link": "https://example.com/a_(b)", "label": [t("the "), {"emphasis": [t("docs")]}]}
    assert content[3] == {"link": "https://x.io/p?q=1", "label": [t("https://x.io/p?q=1")]}
    assert content[5] == {"link": "mailto:me@example.com", "label": [t("me@example.com")]}
    assert content[7] == {"link": "https://github.com/org/repo/pull/7", "label": [t("https://github.com/org/repo/pull/7")]}
    assert content[8] == t(". Also (")
    assert content[9] == {"link": "https://www.example.org", "label": [t("www.example.org")]}
    assert content[11] == {"image": "https://i/p.png", "alt": "a diagram"}
    assert content[12] == t(" and [not a link] and [dangling](")


def test_quotes(core_probe, tmp_path):
    blocks = md(core_probe, tmp_path, "> quoted **text**\nlazily continued\n> - a list\n>\n> > nested\n\nafter\n")
    quote = blocks[0]
    assert quote["type"] == "quote"
    inner = quote["blocks"]
    assert inner[0] == para(t("quoted "), {"strong": [t("text")]}, {"break": "soft"}, t("lazily continued"))
    assert inner[1]["type"] == "list"
    assert inner[2] == {"type": "quote", "blocks": [para(t("nested"))]}
    assert blocks[1] == para(t("after"))


def test_tables(core_probe, tmp_path):
    text = ("| Name | Count | Note |\n|:-----|------:|:---:|\n| a | 1 | `x|y` |\n| b \\| c | 2 |\n\nafter\n"
            "\nno | table\nhere\n")
    blocks = md(core_probe, tmp_path, text)
    table = blocks[0]
    assert table["type"] == "table" and table["alignments"] == ["left", "right", "center"]
    assert table["header"] == [[t("Name")], [t("Count")], [t("Note")]]
    assert table["rows"][0] == [[t("a")], [t("1")], [{"code": "x|y"}]]
    assert table["rows"][1] == [[t("b | c")], [t("2")], []]
    assert blocks[1] == para(t("after"))
    assert blocks[2] == para(t("no | table"), {"break": "soft"}, t("here"))


def test_html_and_entities_are_text(core_probe, tmp_path):
    blocks = md(core_probe, tmp_path, "<script>alert(1)</script> &amp; &lt;b&gt; &#39;q&#39; &#x2014; &bogus;\n")
    assert blocks == [para(t("<script>alert(1)</script> & <b> 'q' — &bogus;"))]


def test_c29_8_long_code_shows_a_bounded_part(core_probe, tmp_path):
    path = tmp_path / "code.txt"
    path.write_text("\n".join(f"line {n}" for n in range(100)))
    assert run_probe(core_probe, "bounds", path, 40) == {"shown_lines": 40, "hidden_lines": 60}
    path.write_text("short\ncode")
    assert run_probe(core_probe, "bounds", path, 40) == {"shown_lines": 2, "hidden_lines": 0}


def test_inlines_become_presentation_intents(core_probe, tmp_path):
    path = tmp_path / "inline.md"
    path.write_text("plain **bold** *it* `code` ~~no~~ [site](https://example.com) [bad](javascript:alert(1))\nnext")
    runs = run_probe(core_probe, "attributed", path)
    by_text = {run["text"]: run for run in runs}
    assert by_text["bold"]["intents"] == ["strong"] and by_text["it"]["intents"] == ["emphasized"]
    assert by_text["code"]["intents"] == ["code"] and by_text["no"]["intents"] == ["strikethrough"]
    assert by_text["site"]["link"] == "https://example.com"
    bad = next(run for run in runs if "bad" in run["text"])
    assert bad["link"] is None                                   # only web and mail links open
    assert "".join(run["text"] for run in runs).endswith("\nnext")


@pytest.mark.parametrize("text", ["", "\n\n", "   ", "*", "**", "[", "`", "> ", "- ", "1.", "|", "```", "#"])
def test_degenerate_input_does_not_fail(core_probe, tmp_path, text):
    assert isinstance(md(core_probe, tmp_path, text), list)


# --- C-29.8: bounded cost. Before the bounds, 20 000 nested quotes overflowed the
# stack, and 16 000 "[a](" took about half a minute on an idle machine.

NESTING = 32


def block_depth(blocks: list[dict]) -> int:
    deepest = 0
    for block in blocks:
        if block["type"] == "quote":
            deepest = max(deepest, 1 + block_depth(block["blocks"]))
        elif block["type"] == "list":
            deepest = max([deepest] + [1 + block_depth(item["blocks"]) for item in block["items"]])
    return deepest


def inline_depth(inlines: list[dict]) -> int:
    deepest = 0
    for inline in inlines:
        for key in ("emphasis", "strong", "strike"):
            if key in inline:
                deepest = max(deepest, 1 + inline_depth(inline[key]))
        if "link" in inline:
            deepest = max(deepest, 1 + inline_depth(inline["label"]))
    return deepest


def innermost(blocks: list[dict]) -> dict:
    block = blocks[0]
    while block["type"] in ("quote", "list"):
        block = (block["blocks"] if block["type"] == "quote" else block["items"][0]["blocks"])[0]
    return block


def md_bounded(core_probe, tmp_path, text: str) -> list[dict]:
    """Parse in well under 10 s even on a loaded machine (each case takes
    milliseconds; the unbounded scans took tens of seconds)."""
    path = tmp_path / f"{uuid.uuid4().hex}.md"
    path.write_text(text)
    return run_probe(core_probe, "markdown", path, timeout=10)


@pytest.mark.parametrize("unit", ["> ", ">", "- ", "1. ", "> - "])
def test_c29_8_deep_block_nesting_stops_at_the_bound(core_probe, tmp_path, unit):
    blocks = md_bounded(core_probe, tmp_path, unit * 20_000 + "x")
    assert block_depth(blocks) == NESTING
    deepest = innermost(blocks)
    assert deepest["type"] == "paragraph" and deepest["content"][0]["text"].endswith("x")   # the rest is text


def test_c29_8_deep_inline_nesting_stops_at_the_bound(core_probe, tmp_path):
    emphasis = md_bounded(core_probe, tmp_path, "*a " * 600 + "a* " * 600)
    assert inline_depth(emphasis[0]["content"]) == NESTING
    links = md_bounded(core_probe, tmp_path, "[" * 5_000 + "x" + "](u)" * 5_000)
    assert NESTING - 2 <= inline_depth(links[0]["content"]) <= NESTING
    shallow = md_bounded(core_probe, tmp_path, "*a " * 20 + "a* " * 20)
    assert inline_depth(shallow[0]["content"]) == 20                                      # below it, unchanged


@pytest.mark.parametrize("unit", ["[", "[a", "[a](", "![", "[a](x \"", "[a](<"])
def test_c29_8_unclosed_links_are_text_without_a_slow_scan(core_probe, tmp_path, unit):
    text = unit * (60_000 // len(unit))
    assert md_bounded(core_probe, tmp_path, text) == [para(t(text))]


@pytest.mark.parametrize("text", ["(http://" * 7_500, "``a`" * 15_000, "`" + "a``" * 20_000, "*_" * 30_000])
def test_c29_8_other_long_runs_parse_in_bounded_time(core_probe, tmp_path, text):
    blocks = md_bounded(core_probe, tmp_path, text)
    assert len(blocks) == 1 and blocks[0]["type"] == "paragraph" and blocks[0]["content"]


def test_c29_8_a_url_with_many_closing_parens_is_linear(core_probe, tmp_path):
    blocks = md_bounded(core_probe, tmp_path, "see http://a.b/c" + ")" * 60_000)
    content = blocks[0]["content"]
    assert content[1] == {"link": "http://a.b/c", "label": [t("http://a.b/c")]}
    assert content[2] == t(")" * 60_000)
