"""Seeded differential fuzz of `tools/contract_layout.relayout` (C-20.7).

Generates Markdown documents, relays them out, and renders both with markdown-it-py
(CommonMark with tables) and cmark-gfm with footnotes on (GitHub's renderer); any
difference in `tests/markdown_signature.py`'s signature is a failure, printed once,
minimized. Adapted from the fuzzer review r2 of #218 wrote.

    uv run --no-project --with markdown-it-py==4.2.0 --with cmarkgfm==2024.11.20 \\
        python tools/contract_layout_fuzz.py exotic 1 3000

`real` draws words like the contract's; `exotic` adds stray delimiters, raw HTML, escapes,
tabs and the like. NOFN=1 leaves footnotes out. Not part of the suite: it needs cmarkgfm,
which is not a dependency.
"""
import sys, random, json, os
NOFN = os.environ.get("NOFN") == "1"
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from tools import contract_layout as layout
from tests.markdown_signature import signature
import markdown_it, cmarkgfm
from cmarkgfm.cmark import Options
MI = markdown_it.MarkdownIt("commonmark").enable("table")
OPT = Options.CMARK_OPT_UNSAFE | Options.CMARK_OPT_FOOTNOTES
def mi(t): return MI.render(t)
def gfm(t): return cmarkgfm.github_flavored_markdown_to_html(t, options=OPT)
def bad(doc):
    out = layout.relayout(doc)
    if out == doc: return []
    return [n for n, r in (("mi", mi), ("gfm", gfm)) if signature(r(out)) != signature(r(doc))]

REAL_WORDS = [w for w in ["a", "the", "job", "Admission", "C-6.9", "e.g.", "i.e.", "etc.", "vs.", "v1.", "2.1.11.",
              "`code`", "`a. B`", "`x; y`", "``a ` b``", "**bold**", "**C-1.2**", "*em*", "_u_",
              "[link](u)", "[C-5.5](#c-5-5)", "[t](u \"T. X\")", "(see C-5.5.)", "\"Q.\"", "'s", "(x)",
              "D-MRG3", "#218", "1.", "2)", "10:05Z", "&amp;", "&lt;b&gt;", "<https://a.b/c>", "x|y", "~~gone~~",
              "[^1]", "<br>", "<b>B</b>", "--", "—", "…", "No.", "St.", "Mr.", "U.S.", "A.", "B.", "I.", "Ok."] if not (NOFN and "[^" in w)]
EXOTIC_WORDS = REAL_WORDS + ["`", "``", "\\`", "\\*", "\\\\", "<span title=\"a>b\">", "<a title=\"`\">", "</a>",
              "<!-- c > d -->", "<http://a`b>", "[a](u \"`\")", "*", "_", "***", "---", "===", "#", ">", "|", "<",
              "<pre>", "</pre>", "&#96;", "&#59;", "&nbsp;", "[x]:", "[^2]:", "~~~", "```", "\\", "  ", "\t"]
ENDS = [".", ";", "!", "?", ",", ":", "", ".)", ".\"", "...", ".;", ".*", ".**", ".`"]

def sentence(rng, words):
    return " ".join(rng.choice(words) for _ in range(rng.randint(1, 6))) + rng.choice(ENDS)
def para(rng, words, cont_indent=""):
    text = sentence(rng, words)
    for _ in range(rng.randint(0, 5)):
        sep = rng.choice([" ", " ", "  ", "\n" + cont_indent, "\n", "\n   "])
        text += sep + sentence(rng, words)
    return text
def block(rng, words, depth=0):
    k = rng.random()
    if k < 0.30:
        return para(rng, words)
    if k < 0.55:                                   # list item, maybe nested content
        marker = rng.choice(["- ", "* ", "+ ", "1. ", "2. ", "1) ", "10. ", "-   ", "- **C-1.1** "])
        ind = " " * len(marker)
        body = marker + para(rng, words, ind)
        for _ in range(rng.randint(0, 2)):
            sub = rng.random()
            if sub < 0.3 and depth < 2:
                inner = block(rng, words, depth + 1)
                body += "\n" + rng.choice(["", "\n"]) + "\n".join(ind + l if l else l for l in inner.split("\n"))
            elif sub < 0.5:
                body += "\n" + ind + "| a. B | c; D |\n" + ind + "|---|---|\n" + ind + "| E. F | g |"
            elif sub < 0.6:
                body += "\n" + ind + "> " + para(rng, words)
            elif sub < 0.7:
                body += "\n\n" + ind + para(rng, words, ind)
            elif sub < 0.8:
                body += "\n" + para(rng, words)          # lazy line(s)
            else:
                body += "\n" + ind + "```\n" + ind + "A. B\n" + ind + "```"
        return body
    if k < 0.62:
        return "> " + para(rng, words).replace("\n", "\n" + rng.choice(["> ", "", ">"]))
    if k < 0.67:
        return "| a. B | c; D |\n|---|:-:|\n| E. F | " + sentence(rng, words) + " |"
    if k < 0.70:
        return "a. B | c\n---|---\nE. F | g"
    if k < 0.74:
        return rng.choice(["```", "~~~", "````"]) + "\nA. B; c\n" + rng.choice(["```", "~~~", "````"])
    if k < 0.77:
        return "    code. More; code."
    if k < 0.80:
        return rng.choice(["## Heading. With; marks", "Setext. Title\n===", "Setext. Two\n---"])
    if k < 0.83:
        return rng.choice(["***", "---", "* * *", "___"])
    if k < 0.86:
        return rng.choice(["<div>\n\nA. B\n</div>", "<!--\nA. B\n-->", "<details>\n<summary>S. T</summary>\n\nA. B\n\n</details>"])
    if k < 0.89:
        return "[r]: /u \"A. B\"" if NOFN else rng.choice(["[r]: /u \"A. B\"", "[^1]: A note. Two.", "[^n]: " + para(rng, words, "    ")])
    if k < 0.92:
        return para(rng, words) + "  \n" + para(rng, words)
    return ""
def document(rng, words):
    return "\n".join(block(rng, words) for _ in range(rng.randint(1, 6)))

def minimize(doc):
    """Greedy: drop lines, then words, while the failure stays."""
    changed = True
    while changed:
        changed = False
        lines = doc.split("\n")
        for i in range(len(lines)):
            cand = "\n".join(lines[:i] + lines[i + 1:])
            if cand and bad(cand):
                doc, changed = cand, True; break
        if changed: continue
        toks = doc.split(" ")
        for i in range(len(toks)):
            cand = " ".join(toks[:i] + toks[i + 1:])
            if cand and bad(cand):
                doc, changed = cand, True; break
    return doc

if __name__ == "__main__":
    mode, seed, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    words = REAL_WORDS if mode == "real" else EXOTIC_WORDS
    rng = random.Random(seed)
    found = {}
    for n in range(count):
        doc = document(rng, words)
        if bad(doc):
            m = minimize(doc)
            key = layout.relayout(m)
            if m not in found:
                found[m] = n
                print(json.dumps({"n": n, "bad": bad(m), "min": m, "out": layout.relayout(m)}), flush=True)
    print(f"# mode={mode} seed={seed} count={count} failures(minimized, distinct)={len(found)}")
