"""What a Markdown document means once rendered, for comparing before and after a relayout.

`signature(html)` walks the HTML a renderer produced and keeps every tag, every attribute
(link titles and destinations included) and the exact text inside `code`, `pre`,
`script`, `style` and `textarea`. Elsewhere it reads each run of whitespace in text as
one space and drops text that is only whitespace, because a soft line break and a space
render alike. Two documents with equal signatures render the same.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

EXACT = {"code", "pre", "script", "style", "textarea"}


class _Tokens(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple] = []

    def handle_starttag(self, tag, attrs):
        self.tokens.append(("start", tag, tuple(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.tokens.append(("empty", tag, tuple(attrs)))

    def handle_endtag(self, tag):
        self.tokens.append(("end", tag))

    def handle_data(self, data):
        if self.tokens and self.tokens[-1][0] == "text":
            self.tokens[-1] = ("text", self.tokens[-1][1] + data)
        else:
            self.tokens.append(("text", data))

    def handle_comment(self, data):
        self.tokens.append(("comment", data))


def signature(html: str) -> list[tuple]:
    parser = _Tokens()
    parser.feed(html)
    parser.close()
    out, exact = [], 0
    for token in parser.tokens:
        if token[0] == "start":
            exact += token[1] in EXACT
        elif token[0] == "end":
            exact = max(0, exact - (token[1] in EXACT))
        elif token[0] == "text" and not exact:
            text = re.sub(r"\s+", " ", token[1])
            if text == " " or not text:
                continue
            token = ("text", text)
        out.append(token)
    return out
